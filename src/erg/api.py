import secrets
from dataclasses import asdict

from fastapi import Body, Cookie, FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from erg.c2 import oauth
from erg.c2.client import C2Client
from erg.config import get_settings
from erg.db import session_scope
from erg.models import (
    Athlete,
    ClassificationOverride,
    IntervalSplit,
    Stroke,
    Workout,
    WorkoutClassification,
    WorkoutEligibility,
)
from erg.services import client_for_athlete
from erg.pipeline import classify_all, effective_class, set_override
from erg.strokes import downsample, with_elapsed
from erg.sync import backfill, fetch_strokes, upsert_athlete
from erg.tokens import store_token

app = FastAPI(title="Erg Analytics")

STATE_COOKIE = "c2_oauth_state"


@app.get("/auth/login")
def login():
    settings = get_settings()
    state = secrets.token_urlsafe(24)
    resp = RedirectResponse(oauth.authorize_url(settings, state))
    resp.set_cookie(STATE_COOKIE, state, max_age=600, httponly=True, samesite="lax")
    return resp


@app.get("/auth/callback")
def callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    c2_oauth_state: str | None = Cookie(default=None),
):
    if error:
        raise HTTPException(400, f"authorization denied: {error}")
    if not code or not state or not c2_oauth_state or not secrets.compare_digest(state, c2_oauth_state):
        raise HTTPException(400, "invalid OAuth state")

    settings = get_settings()
    token = oauth.exchange_code(settings, code)
    client = C2Client(settings, lambda: token.access_token)
    try:
        me = client.get_me()
    finally:
        client.close()

    with session_scope() as s:
        athlete_id = upsert_athlete(s, me)
        store_token(s, athlete_id, token)

    resp = RedirectResponse(f"/athletes/{athlete_id}")
    resp.delete_cookie(STATE_COOKIE)
    return resp


@app.get("/athletes/{athlete_id}")
def get_athlete(athlete_id: int):
    with session_scope() as s:
        athlete = s.execute(select(Athlete).where(Athlete.id == athlete_id)).scalar_one_or_none()
        if athlete is None:
            raise HTTPException(404, "athlete not found")
        return {
            "id": athlete.id,
            "username": athlete.username,
            "max_heart_rate": athlete.max_heart_rate,
            "weight_g": athlete.weight_g,
        }


@app.post("/athletes/{athlete_id}/backfill")
def run_backfill(athlete_id: int):
    settings = get_settings()
    client = client_for_athlete(settings, athlete_id)
    try:
        with session_scope() as s:
            stats = backfill(s, client, settings)
    finally:
        client.close()
    return asdict(stats)


@app.post("/athletes/{athlete_id}/strokes/fetch")
def run_stroke_fetch(athlete_id: int, limit: int | None = None):
    settings = get_settings()
    client = client_for_athlete(settings, athlete_id)
    try:
        with session_scope() as s:
            stats = fetch_strokes(s, client, athlete_id, limit=limit)
    finally:
        client.close()
    return asdict(stats)


@app.get("/workouts/{workout_id}/strokes")
def get_strokes(
    workout_id: int,
    downsample_to: int | None = Query(None, alias="downsample", ge=3, description="max points (LTTB on time vs pace)"),
    include_rest: bool = True,
):
    with session_scope() as s:
        workout = s.get(Workout, workout_id)
        if workout is None:
            raise HTTPException(404, "workout not found")
        strokes = s.execute(select(Stroke).where(Stroke.workout_id == workout_id).order_by(Stroke.seq)).scalars().all()
        intervals = s.execute(
            select(IntervalSplit).where(IntervalSplit.workout_id == workout_id).order_by(IntervalSplit.idx)
        ).scalars().all()

        points = with_elapsed(strokes)
        if not include_rest:
            points = [p for p in points if not p["is_rest"]]
        total = len(points)
        if downsample_to:
            points = downsample(points, downsample_to)

        return {
            "workout_id": workout_id,
            "workout_type": workout.workout_type,
            "stroke_status": workout.stroke_status,
            "stroke_warning": workout.stroke_warning,
            "intervals": [
                {
                    "idx": i.idx,
                    "kind": i.kind,
                    "target_type": i.target_type,
                    "time_s": float(i.time_s),
                    "distance_m": i.distance_m,
                    "rest_time_s": float(i.rest_time_s),
                    "hr_avg": i.hr_avg,
                    "hr_rest": i.hr_rest,
                }
                for i in intervals
            ],
            "total_points": total,
            "points": points,
        }


@app.get("/workouts/{workout_id}")
def get_workout(workout_id: int):
    with session_scope() as s:
        w = s.get(Workout, workout_id)
        if w is None:
            raise HTTPException(404, "workout not found")
        cls, overridden = effective_class(s, workout_id)
        classification = s.get(WorkoutClassification, workout_id)
        eligibility = s.execute(
            select(WorkoutEligibility).where(WorkoutEligibility.workout_id == workout_id)
        ).scalars().all()
        return {
            "id": w.id,
            "ended_at_local": w.ended_at_local,
            "ended_at_utc": w.ended_at_utc,
            "workout_type": w.workout_type,
            "work_time_s": float(w.work_time_s),
            "work_distance_m": w.work_distance_m,
            "rest_time_s": float(w.rest_time_s),
            "avg_pace_s_500": float(w.avg_pace_s_500) if w.avg_pace_s_500 else None,
            "avg_watts": float(w.avg_watts) if w.avg_watts else None,
            "avg_spm": w.avg_spm,
            "hr_avg": w.hr_avg,
            "hr_quality": w.hr_quality,
            "drag_factor": w.drag_factor,
            "comments": w.comments,
            "stroke_status": w.stroke_status,
            "stroke_warning": w.stroke_warning,
            "classification": {
                "class": cls,
                "overridden": overridden,
                "classifier_class": classification.workout_class if classification else None,
                "confidence": float(classification.confidence) if classification else None,
                "reason": classification.reason if classification else None,
            },
            "eligibility": {e.metric: {"eligible": e.eligible, "reason": e.reason} for e in eligibility},
        }


@app.post("/workouts/{workout_id}/classification")
def override_classification(workout_id: int, workout_class: str = Body(embed=True), note: str | None = Body(None, embed=True)):
    """Manual override. Always wins over the classifier and survives recomputation."""
    with session_scope() as s:
        w = s.get(Workout, workout_id)
        if w is None:
            raise HTTPException(404, "workout not found")
        set_override(s, workout_id, workout_class, note)
        classify_all(s, w.athlete_id)  # refresh eligibility, which depends on class
        cls, _ = effective_class(s, workout_id)
    return {"workout_id": workout_id, "class": cls, "overridden": True}


@app.get("/workouts")
def list_workouts(
    workout_class: str | None = Query(None, alias="class"),
    eligible_for: str | None = Query(None, description="metric name, e.g. ef or decoupling"),
    limit: int = Query(50, le=250),
):
    with session_scope() as s:
        # Manual overrides win over the classifier, so filter on the effective class.
        effective = func.coalesce(ClassificationOverride.workout_class, WorkoutClassification.workout_class)
        q = (
            select(Workout, WorkoutClassification, effective.label("effective_class"))
            .join(WorkoutClassification, WorkoutClassification.workout_id == Workout.id, isouter=True)
            .join(ClassificationOverride, ClassificationOverride.workout_id == Workout.id, isouter=True)
        )
        if workout_class:
            q = q.where(effective == workout_class)
        if eligible_for:
            q = q.join(
                WorkoutEligibility,
                (WorkoutEligibility.workout_id == Workout.id) & (WorkoutEligibility.metric == eligible_for),
            ).where(WorkoutEligibility.eligible)
        rows = s.execute(q.order_by(Workout.ended_at_utc.desc()).limit(limit)).all()
        return [
            {
                "id": w.id,
                "date": w.ended_at_local.date(),
                "class": eff,
                "confidence": float(c.confidence) if c else None,
                "overridden": eff != (c.workout_class if c else None),
                "work_distance_m": w.work_distance_m,
                "work_time_s": float(w.work_time_s),
                "avg_pace_s_500": float(w.avg_pace_s_500) if w.avg_pace_s_500 else None,
                "hr_avg": w.hr_avg,
            }
            for w, c, eff in rows
        ]
