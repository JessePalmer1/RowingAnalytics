"""Local mode: embedded database, generated secrets, in-app import."""

import httpx
import pytest
import respx
from conftest import result_payload
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text

from erg import api, importer
from erg.config import Settings, finalize
from erg.models import Workout, WorkoutClassification, WorkoutMetric
from test_db import INTERVAL_WORKOUT, STROKES, fake_c2, strokes_url
from test_session import sign_in


# ---- configuration ----------------------------------------------------------

def test_no_database_url_means_local_mode_with_a_generated_key():
    s = finalize(Settings(_env_file=None, database_url="", token_encryption_key=""))
    assert s.local_mode
    assert len(s.token_encryption_key) == 44  # a Fernet key


def test_a_persistent_database_never_gets_a_throwaway_key():
    s = finalize(Settings(_env_file=None, database_url="postgresql+psycopg://x/y", token_encryption_key=""))
    assert not s.local_mode and s.token_encryption_key == ""


def test_status_is_public(db, settings):
    with TestClient(api.app) as http:
        body = http.get("/status").json()
    assert body == {"local_mode": False, "concept2_configured": True}


def test_login_explains_missing_keys_instead_of_redirecting(db, settings, monkeypatch):
    monkeypatch.setattr(settings, "c2_client_secret", "")
    with TestClient(api.app) as http:
        resp = http.get("/auth/login", follow_redirects=False)
    assert resp.status_code == 503 and "C2_CLIENT_SECRET" in resp.text


# ---- in-app import ------------------------------------------------------------

def mock_logbook():
    fake_c2([
        result_payload(id=1, distance=2000, time=3840, workout_type="FixedDistanceSplits"),
        result_payload(id=2, date="2026-02-11 07:00:00", date_utc="2026-02-11 12:00:00",
                       workout_type="FixedTimeInterval", rest_time=400, workout=INTERVAL_WORKOUT),
    ])
    respx.get(strokes_url(1)).mock(return_value=httpx.Response(200, json={"data": STROKES}))
    respx.get(strokes_url(2)).mock(return_value=httpx.Response(200, json={"data": STROKES}))


@respx.mock
def test_import_runs_the_whole_pipeline(db, settings, monkeypatch):
    from erg import services
    from test_client import NoLimit
    from erg.c2.client import C2Client

    monkeypatch.setattr(
        importer, "client_for_athlete",
        lambda s, a: C2Client(s, lambda: "tok", http=httpx.Client(), limiter=NoLimit()),
    )
    mock_logbook()
    progress = []
    real_set = importer._set
    monkeypatch.setattr(importer, "_set", lambda job, **kw: (progress.append(kw), real_set(job, **kw)))

    job = importer.start(42, run_in_thread=False)

    assert job.state == "done" and job.error is None
    assert job.workouts == 2 and job.new_workouts == 2
    assert db.execute(select(func.count()).select_from(Workout)).scalar() == 2
    assert db.execute(select(func.count()).select_from(WorkoutClassification)).scalar() == 2
    assert db.execute(select(func.count()).select_from(WorkoutMetric)).scalar() == 2
    # Stroke progress was reported as it went: 0 of 2, then 1, then 2.
    stroke_progress = [(p["done"], p["total"]) for p in progress if "done" in p and p.get("total")]
    assert stroke_progress == [(0, 2), (1, 2), (2, 2)]
    stages = [p["stage"] for p in progress if "stage" in p]
    assert stages == ["workouts", "strokes", "classify", "metrics", "done"]


@respx.mock
def test_import_failure_leaves_a_readable_error(db, settings, monkeypatch):
    from test_client import NoLimit
    from erg.c2.client import C2Client

    monkeypatch.setattr(
        importer, "client_for_athlete",
        lambda s, a: C2Client(s, lambda: "tok", http=httpx.Client(), limiter=NoLimit(), sleep=lambda _: None),
    )
    respx.get("https://c2.test/api/users/me").mock(return_value=httpx.Response(401, text="token revoked"))
    job = importer.start(42, run_in_thread=False)
    assert job.state == "error" and "401" in job.error


def test_import_endpoints_need_a_session_and_report_status(db, settings, monkeypatch):
    calls = []
    monkeypatch.setattr(importer, "start", lambda athlete_id: calls.append(athlete_id) or importer.ImportStatus(athlete_id, state="running"))
    with TestClient(api.app) as http:
        assert http.post("/athletes/me/import").status_code == 401
        sign_in(http, 42)
        assert http.post("/athletes/me/import").json()["state"] == "running"
        assert http.get("/athletes/me/import").json()["athlete_id"] == 42
    assert calls == [42]


def test_a_second_start_while_running_returns_the_same_job():
    job = importer.ImportStatus(7, state="running", stage="strokes")
    importer._jobs[7] = job
    try:
        assert importer.start(7) is job
    finally:
        importer._jobs.pop(7, None)


# ---- the embedded database itself ---------------------------------------------

@pytest.mark.slow
def test_embedded_postgres_starts_and_holds_the_schema():
    pytest.importorskip("pgserver")
    from erg import embedded

    engine = create_engine(embedded.database_url())
    embedded.create_schema(engine)
    with engine.connect() as conn:
        tables = set(conn.execute(text("select tablename from pg_tables where schemaname = 'public'")).scalars())
        # Postgres-only features the app relies on behave as they do in production.
        inserted = conn.execute(text(
            "insert into athlete (id, raw) values (1, '{}'::jsonb) "
            "on conflict (id) do update set raw = excluded.raw returning (xmax = 0)"
        )).scalar()
    assert {"athlete", "workout", "stroke", "workout_metric", "classification_override"} <= tables
    assert inserted is True
    engine.dispose()
