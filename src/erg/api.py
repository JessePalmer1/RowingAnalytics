import secrets
from dataclasses import asdict

from fastapi import Cookie, FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from erg.c2 import oauth
from erg.c2.client import C2Client
from erg.config import get_settings
from erg.db import session_scope
from erg.models import Athlete, IntervalSplit, Stroke, Workout
from erg.services import client_for_athlete
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
