import os

from cryptography.fernet import Fernet

os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://erg:erg@localhost:5432/erg_test"
)
os.environ["TOKEN_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
os.environ["C2_BASE_URL"] = "https://c2.test"
os.environ["C2_CLIENT_ID"] = "client"
os.environ["C2_CLIENT_SECRET"] = "secret"
os.environ["DEFAULT_TIMEZONE"] = "America/New_York"

import pytest  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from erg.config import get_settings  # noqa: E402
from erg.models import Base  # noqa: E402


@pytest.fixture
def settings():
    get_settings.cache_clear()
    return get_settings()


def result_payload(**overrides):
    """A realistic Concept2 result as returned by /api/users/{user}/results."""
    base = {
        "id": 1001,
        "user_id": 42,
        "date": "2026-02-10 18:30:00",
        "timezone": "Europe/London",
        "date_utc": "2026-02-10 18:30:00",
        "distance": 2000,
        "type": "rower",
        "time": 4200,  # 7:00.0 in tenths
        "workout_type": "FixedDistanceSplits",
        "source": "ErgData",
        "comments": None,
        "stroke_data": True,
        "stroke_rate": 30,
        "stroke_count": 210,
        "drag_factor": 118,
        "calories_total": 130,
        "heart_rate": {"average": 172, "ending": 185, "rest": 0},
        "rest_distance": 0,
        "rest_time": 0,
    }
    base.update(overrides)
    return base


@pytest.fixture(scope="session")
def engine():
    eng = create_engine(os.environ["DATABASE_URL"])
    try:
        with eng.connect():
            pass
    except OperationalError:
        pytest.skip("test database unavailable (run `docker compose up -d`)")
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE athlete, oauth_token, workout, interval_split, stroke CASCADE"))
    with Session(engine, expire_on_commit=False) as session:
        yield session
