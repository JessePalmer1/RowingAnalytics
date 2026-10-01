import httpx
import pytest
import respx
from conftest import result_payload
from fastapi.testclient import TestClient
from sqlalchemy import update

from erg import api, session
from erg.c2.client import C2Client
from erg.models import Athlete, OAuthToken, Workout
from erg.session import SESSION_COOKIE
from erg.sync import backfill
from test_client import NoLimit
from test_db import fake_c2

# Every endpoint that touches training data.
PROTECTED = [
    ("GET", "/athletes/me"),
    ("GET", "/workouts"),
    ("GET", "/workouts/1"),
    ("GET", "/workouts/1/strokes"),
    ("GET", "/workouts/compare?ids=1,2"),
    ("GET", "/metrics/trend?name=ef"),
    ("GET", "/load/daily"),
    ("GET", "/load/acwr"),
    ("GET", "/summary/week"),
    ("POST", "/athletes/me/backfill"),
    ("POST", "/athletes/me/strokes/fetch"),
]


def sign_in(client: TestClient, athlete_id: int) -> None:
    client.cookies.set(SESSION_COOKIE, session.mint(athlete_id))


def test_session_round_trip(settings):
    assert session.read(session.mint(42, settings), settings) == 42


def test_tampered_or_missing_sessions_are_rejected(settings):
    cookie = session.mint(42, settings)
    assert session.read(None, settings) is None
    assert session.read("", settings) is None
    assert session.read(cookie[:-4] + "aaaa", settings) is None  # signature broken
    assert session.read("42", settings) is None  # not signed at all


@pytest.mark.parametrize("method,path", PROTECTED)
def test_endpoints_require_a_session(db, settings, method, path):
    with TestClient(api.app) as client:
        assert client.request(method, path).status_code == 401


@respx.mock
def test_athletes_cannot_see_each_others_workouts(db, settings):
    # Athlete 42 backfills normally.
    fake_c2([result_payload(id=1), result_payload(id=2, date="2026-02-11 07:00:00", date_utc="2026-02-11 07:00:00")])
    client = C2Client(settings, lambda: "tok", http=httpx.Client(), limiter=NoLimit())
    backfill(db, client, settings)

    # A second athlete with one workout of their own.
    db.add(Athlete(id=99, username="someone-else", raw={"id": 99}))
    db.flush()
    db.execute(update(Workout).where(Workout.id == 2).values(athlete_id=99))
    db.commit()

    with TestClient(api.app) as http:
        sign_in(http, 42)
        mine = http.get("/workouts").json()
        assert [w["id"] for w in mine] == [1]

        assert http.get("/workouts/1").status_code == 200
        # Someone else's workout reads as missing, not forbidden: no existence leak.
        assert http.get("/workouts/2").status_code == 404
        assert http.get("/workouts/2/strokes").status_code == 404
        assert http.post("/workouts/2/classification", json={"workout_class": "steady"}).status_code == 404
        # Ownership is checked before anything else, so the other athlete's id 404s here too.
        assert http.get("/workouts/compare?ids=2,1").status_code == 404

        assert http.get("/athletes/me").json()["id"] == 42

        sign_in(http, 99)
        assert [w["id"] for w in http.get("/workouts").json()] == [2]
        assert http.get("/workouts/1").status_code == 404


@respx.mock
def test_callback_signs_the_athlete_in(db, settings, monkeypatch):
    from erg import importer

    started = []
    monkeypatch.setattr(importer, "begin", lambda athlete_id: started.append(athlete_id))
    fake_c2([])
    respx.post("https://c2.test/oauth/access_token").mock(
        return_value=httpx.Response(
            200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 604800}
        )
    )
    with TestClient(api.app) as http:
        login = http.get("/auth/login", follow_redirects=False)
        state = login.cookies["c2_oauth_state"]
        http.cookies.set("c2_oauth_state", state)

        done = http.get(f"/auth/callback?code=abc&state={state}", follow_redirects=False)
        assert done.status_code == 307 and done.headers["location"] == "/replay"
        assert session.read(done.cookies[SESSION_COOKIE], settings) == 42
        assert started == [42]  # signing in kicks off the import

        http.cookies.set(SESSION_COOKIE, done.cookies[SESSION_COOKIE])
        assert http.get("/athletes/me").status_code == 200
        assert db.get(OAuthToken, 42) is not None

        http.post("/auth/logout")
        http.cookies.clear()
        assert http.get("/athletes/me").status_code == 401
