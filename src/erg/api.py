import secrets
from dataclasses import asdict
from datetime import date as Date
from pathlib import Path

from fastapi import Body, Cookie, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select

from erg.c2 import oauth
from erg.c2.client import C2Client
from erg.config import get_settings
from erg.db import session_scope
from erg.models import (
    Athlete,
    ClassificationOverride,
    DailyLoad,
    IntervalSplit,
    RollingMetric,
    Stroke,
    Workout,
    WorkoutClassification,
    WorkoutEligibility,
    WorkoutMetric,
)
from erg.services import client_for_athlete
from erg.metrics_runner import ACUTE_DAYS, CHRONIC_DAYS
from erg.pipeline import classify_all, effective_class, set_override
from erg.compare import DEFAULT_POINTS, DEFAULT_SEGMENT_M, common_grid, resample, split_attribution, track_from_samples
from erg.metrics import StrokePoint, work_samples
from erg.strokes import downsample, with_elapsed
from erg.summary import week_summary
from erg.sync import backfill, fetch_strokes, upsert_athlete
from erg.tokens import store_token

app = FastAPI(title="Erg Analytics")

WEB_DIR = Path(__file__).parent / "web"
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/", include_in_schema=False)
@app.get("/replay", include_in_schema=False)
def replay_ui():
    """Race replay: distance-aligned ghost racing over your own pieces."""
    return FileResponse(WEB_DIR / "index.html")

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


@app.get("/workouts/compare")
def compare_workouts(
    ids: str = Query(..., description="comma-separated workout ids; the first is the reference"),
    points: int = Query(DEFAULT_POINTS, ge=10, le=2000),
    segment_m: float = Query(DEFAULT_SEGMENT_M, gt=0),
):
    """Distance-aligned series plus split attribution, ready to overlay."""
    try:
        workout_ids = [int(part) for part in ids.split(",") if part.strip()]
    except ValueError:
        raise HTTPException(400, "ids must be comma-separated integers") from None
    if not 2 <= len(workout_ids) <= 6:
        raise HTTPException(400, "compare between 2 and 6 workouts")

    with session_scope() as s:
        pieces, tracks = [], []
        for workout_id in workout_ids:
            w = s.get(Workout, workout_id)
            if w is None:
                raise HTTPException(404, f"workout {workout_id} not found")
            strokes = s.execute(
                select(Stroke).where(Stroke.workout_id == workout_id, Stroke.is_rest.is_(False)).order_by(Stroke.seq)
            ).scalars().all()
            if not strokes:
                raise HTTPException(400, f"workout {workout_id} has no stroke data to compare")
            samples = work_samples(
                [
                    StrokePoint(
                        interval_idx=st.interval_idx,
                        t_s=float(st.t_s),
                        d_m=float(st.d_m),
                        pace_s_500=float(st.pace_s_500) if st.pace_s_500 is not None else None,
                        spm=st.spm,
                        hr=st.hr,
                    )
                    for st in strokes
                ]
            )
            cls, _ = effective_class(s, workout_id)
            tracks.append(track_from_samples(samples))
            pieces.append(
                {
                    "workout_id": w.id,
                    "date": w.ended_at_local.date(),
                    "class": cls,
                    "work_distance_m": w.work_distance_m,
                    "work_time_s": float(w.work_time_s),
                    "avg_pace_s_500": float(w.avg_pace_s_500) if w.avg_pace_s_500 else None,
                    "drag_factor": w.drag_factor,
                    "hr_avg": w.hr_avg,
                    "comments": w.comments,
                }
            )

        grid = common_grid(tracks, points)
        if not grid:
            raise HTTPException(400, "no common distance to compare over")
        aligned_distance = grid[-1]

        reference = tracks[0]
        for i, (piece, track) in enumerate(zip(pieces, tracks)):
            piece["series"] = resample(track, grid)
            piece["aligned_time_s"] = piece["series"]["time_s"][-1]
            piece["is_reference"] = i == 0
            piece["splits"] = split_attribution(reference, track, aligned_distance, segment_m)
            piece["total_delta_s"] = (
                None
                if piece["aligned_time_s"] is None or pieces[0]["aligned_time_s"] is None
                else round(piece["aligned_time_s"] - pieces[0]["aligned_time_s"], 2)
            )

        drags = {p["drag_factor"] for p in pieces if p["drag_factor"]}
        return {
            "aligned_distance_m": aligned_distance,
            "segment_m": segment_m,
            "reference_id": workout_ids[0],
            "pieces": pieces,
            "note": (
                f"drag factor differs across these pieces ({sorted(drags)}); pace is not strictly comparable"
                if len(drags) > 1
                else None
            ),
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
            "metrics": _metrics_dict(s.get(WorkoutMetric, workout_id)),
        }


def _metrics_dict(m) -> dict | None:
    if m is None:
        return None
    as_float = lambda v: float(v) if v is not None else None  # noqa: E731
    return {
        "ef": as_float(m.ef),
        "work_watts": as_float(m.work_watts),
        "work_hr": as_float(m.work_hr),
        "decoupling_pct": as_float(m.decoupling_pct),
        "ef_first_half": as_float(m.ef_first_half),
        "ef_second_half": as_float(m.ef_second_half),
        "pace_cv": as_float(m.pace_cv),
        "spm_cv": as_float(m.spm_cv),
        "first_half_pace": as_float(m.first_half_pace),
        "second_half_pace": as_float(m.second_half_pace),
        "fade_onset_m": m.fade_onset_m,
        "dps_m": as_float(m.dps_m),
        "dps_cv": as_float(m.dps_cv),
        "dps_source": m.dps_source,
        "hrr_bpm": as_float(m.hrr_bpm),
        "hrr_rest_s": as_float(m.hrr_rest_s),
        "hrr_intervals": m.hrr_intervals,
        "kj": as_float(m.kj),
        "trimp": as_float(m.trimp),
        "metric_version": m.metric_version,
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


METRIC_COLUMNS = {
    "ef": WorkoutMetric.ef,
    "decoupling": WorkoutMetric.decoupling_pct,
    "hrr": WorkoutMetric.hrr_bpm,
    "dps": WorkoutMetric.dps_m,
    "pace_cv": WorkoutMetric.pace_cv,
    "kj": WorkoutMetric.kj,
    "trimp": WorkoutMetric.trimp,
}


@app.get("/metrics/trend")
def metric_trend(
    name: str = Query("ef", description=f"one of {', '.join(METRIC_COLUMNS)}"),
    workout_class: str | None = Query(None, alias="class"),
    from_: Date | None = Query(None, alias="from"),
    to: Date | None = None,
    hrr_rest_s: float | None = Query(None, description="HRR only compares at matched rest length"),
):
    """One point per eligible workout. No smoothing: the caller decides how to present it."""
    column = METRIC_COLUMNS.get(name)
    if column is None:
        raise HTTPException(400, f"unknown metric {name!r}; try one of {', '.join(METRIC_COLUMNS)}")

    with session_scope() as s:
        effective = func.coalesce(ClassificationOverride.workout_class, WorkoutClassification.workout_class)
        q = (
            select(Workout.id, Workout.ended_at_local, effective, column, Workout.drag_factor, Workout.work_time_s)
            .join(WorkoutMetric, WorkoutMetric.workout_id == Workout.id)
            .join(WorkoutClassification, WorkoutClassification.workout_id == Workout.id, isouter=True)
            .join(ClassificationOverride, ClassificationOverride.workout_id == Workout.id, isouter=True)
            .where(column.is_not(None))
            .order_by(Workout.ended_at_local)
        )
        if workout_class:
            q = q.where(effective == workout_class)
        if from_:
            q = q.where(Workout.ended_at_local >= from_)
        if to:
            q = q.where(Workout.ended_at_local <= to)
        if name == "hrr" and hrr_rest_s is not None:
            q = q.where(WorkoutMetric.hrr_rest_s == hrr_rest_s)

        points = [
            {
                "workout_id": wid,
                "date": ended.date(),
                "class": cls,
                "value": float(value),
                "drag_factor": drag,
                "work_time_s": float(work_time),
            }
            for wid, ended, cls, value, drag, work_time in s.execute(q).all()
        ]
    note = None
    if name == "hrr" and hrr_rest_s is None:
        note = "HR recovery depends on rest length; filter with hrr_rest_s to compare like with like"
    return {"metric": name, "points": points, "note": note}


@app.get("/load/daily")
def load_daily(from_: Date | None = Query(None, alias="from"), to: Date | None = None):
    with session_scope() as s:
        q = select(DailyLoad).order_by(DailyLoad.date)
        if from_:
            q = q.where(DailyLoad.date >= from_)
        if to:
            q = q.where(DailyLoad.date <= to)
        return [
            {
                "date": d.date,
                "sessions": d.sessions,
                "work_time_s": float(d.work_time_s),
                "work_distance_m": d.work_distance_m,
                "kj": float(d.kj) if d.kj is not None else None,
                "trimp": float(d.trimp) if d.trimp is not None else None,
            }
            for d in s.execute(q).scalars()
        ]


@app.get("/load/acwr")
def load_acwr(date: Date | None = None):
    """Acute:chronic workload ratio — a descriptive load-balance indicator, not advice."""
    with session_scope() as s:
        q = select(RollingMetric).where(RollingMetric.metric_name.in_(["acwr", "kj", "monotony"]))
        if date:
            q = q.where(RollingMetric.date == date)
        else:
            latest = s.execute(select(func.max(RollingMetric.date))).scalar()
            if latest is None:
                return {}
            q = q.where(RollingMetric.date == latest)
        rows = s.execute(q).scalars().all()
        if not rows:
            raise HTTPException(404, "no rolling metrics for that date")
        out = {"date": rows[0].date, "acute_days": ACUTE_DAYS, "chronic_days": CHRONIC_DAYS}
        for r in rows:
            key = r.metric_name if r.metric_name != "kj" else f"kj_{r.window_days}d"
            out[key] = float(r.value) if r.value is not None else None
        return out


@app.get("/summary/week")
def summary_week(athlete_id: int | None = None, date: Date | None = None):
    """Digest payload for one Monday-Sunday week. Descriptive only, no recommendations."""
    with session_scope() as s:
        if athlete_id is None:
            athlete_id = s.execute(select(Athlete.id).order_by(Athlete.id).limit(1)).scalar()
            if athlete_id is None:
                raise HTTPException(404, "no athlete in the database")
        return week_summary(s, athlete_id, date)
