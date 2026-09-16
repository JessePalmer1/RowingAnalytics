import secrets
from dataclasses import asdict

from fastapi import Cookie, FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from erg.c2 import oauth
from erg.c2.client import C2Client
from erg.config import get_settings
from erg.db import session_scope
from erg.models import Athlete
from erg.services import client_for_athlete
from erg.sync import backfill, upsert_athlete
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
