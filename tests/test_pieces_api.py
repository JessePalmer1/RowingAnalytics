import httpx
import respx
from conftest import result_payload
from fastapi.testclient import TestClient

from erg import api
from erg.c2.client import C2Client
from erg.pipeline import classify_all
from erg.sync import backfill
from test_client import NoLimit
from test_db import fake_c2
from test_session import sign_in

# s/500m. With the 1:50 5k (also 10+ min) the pool is 110..128; the slower half is
# 122, 124, 126, 128, so the learned steady pace is 125 and the threshold 115.
STEADY_PACES = [118, 120, 122, 124, 126, 128]


def solo(workout_id: int, day: int, distance: int, pace_s_500: float):
    return result_payload(
        id=workout_id,
        date=f"2026-03-{day:02d} 07:00:00",
        date_utc=f"2026-03-{day:02d} 12:00:00",
        workout_type="FixedTimeSplits",
        distance=distance,
        time=round(distance / 500 * pace_s_500 * 10),
    )


def load(db, settings):
    rows = [solo(i + 1, i + 1, 7000 + i * 37, pace) for i, pace in enumerate(STEADY_PACES)]
    rows.append(solo(20, 20, 5000, 110))  # solo 5k at 1:50: interval work for this athlete
    rows.append(solo(21, 21, 2000, 125))  # solo 2k at 2:05: a test, whatever the pace
    fake_c2(rows)
    client = C2Client(settings, lambda: "tok", http=httpx.Client(), limiter=NoLimit())
    backfill(db, client, settings)
    classify_all(db, 42)


def classes(http) -> dict[int, str]:
    return {w["id"]: w["class"] for w in http.get("/workouts?limit=1000").json()}


@respx.mock
def test_learned_baseline_and_threshold(db, settings):
    load(db, settings)
    with TestClient(api.app) as http:
        sign_in(http, 42)
        s = http.get("/athletes/me/settings").json()
        assert s["steady_pace_auto"] == 125.0 and s["steady_pace_override"] is None
        assert s["interval_margin_s"] == 10.0 and s["interval_threshold"] == 115.0
        c = classes(http)
        assert c[20] == "interval"  # 1:50 is faster than 1:55
        assert c[21] == "test_2k"  # solo 2000m at 2:05 is still a test
        assert all(c[i] == "steady" for i in range(1, 7))


@respx.mock
def test_override_and_reset(db, settings):
    load(db, settings)
    with TestClient(api.app) as http:
        sign_in(http, 42)
        done = http.post("/workouts/21/classification", json={"workout_class": "steady", "note": "easy 2k"}).json()
        assert done["class"] == "steady" and done["overridden"] is True

        listed = {w["id"]: w for w in http.get("/workouts?limit=1000").json()}
        assert listed[21]["class"] == "steady" and listed[21]["classifier_class"] == "test_2k"
        assert listed[21]["override_note"] == "easy 2k"

        reset = http.delete("/workouts/21/classification").json()
        assert reset == {"workout_id": 21, "class": "test_2k", "overridden": False, "reclassified": [21]}


@respx.mock
def test_an_override_that_agrees_with_the_classifier_still_counts(db, settings):
    load(db, settings)
    with TestClient(api.app) as http:
        sign_in(http, 42)
        http.post("/workouts/21/classification", json={"workout_class": "test_2k"})
        listed = {w["id"]: w for w in http.get("/workouts?limit=1000").json()}
        assert listed[21]["overridden"] is True  # the athlete's decision, even though it matches


@respx.mock
def test_invalid_class_is_rejected(db, settings):
    load(db, settings)
    with TestClient(api.app) as http:
        sign_in(http, 42)
        assert http.post("/workouts/21/classification", json={"workout_class": "sprint"}).status_code == 422


@respx.mock
def test_settings_change_reclassifies(db, settings):
    load(db, settings)
    with TestClient(api.app) as http:
        sign_in(http, 42)
        # Declare steady at 1:58: threshold 1:48, so the 1:50 piece becomes steady.
        result = http.put("/athletes/me/settings", json={"steady_pace_s_500": 118, "interval_margin_s": 10}).json()
        assert result["steady_pace"] == 118.0 and result["interval_threshold"] == 108.0
        assert result["reclassified"] == [20]
        assert classes(http)[20] == "steady"

        # A tighter margin on the learned baseline: 125 - 5 = 120, so 1:58 turns interval
        # while 2:00 sits exactly on the threshold and stays steady.
        result = http.put("/athletes/me/settings", json={"auto": True, "interval_margin_s": 5}).json()
        assert result["steady_pace_override"] is None and result["interval_threshold"] == 120.0
        c = classes(http)
        assert c[1] == "interval" and c[2] == "steady" and c[3] == "steady"


@respx.mock
def test_settings_validation(db, settings):
    load(db, settings)
    with TestClient(api.app) as http:
        sign_in(http, 42)
        assert http.put("/athletes/me/settings", json={"steady_pace_s_500": 20}).status_code == 422
        assert http.put("/athletes/me/settings", json={"interval_margin_s": 90}).status_code == 422


@respx.mock
def test_overriding_a_piece_to_a_test_moves_the_baseline(db, settings):
    load(db, settings)
    with TestClient(api.app) as http:
        sign_in(http, 42)
        # Workout 6 (2:08) is the slowest; calling it a test takes it out of the baseline:
        # the pool is 110..126, slower half 122, 124, 126 -> 124.
        http.post("/workouts/6/classification", json={"workout_class": "test_6k"})
        s = http.get("/athletes/me/settings").json()
        assert s["steady_pace_auto"] == 124.0


@respx.mock
def test_override_refreshes_the_pieces_metrics(db, settings):
    from erg.metrics_runner import compute_load, compute_workout_metrics
    from erg.models import WorkoutMetric

    load(db, settings)
    compute_workout_metrics(db, 42)
    compute_load(db, 42)
    assert db.get(WorkoutMetric, 1).ef is not None  # steady, 30 min, HR and watts: EF eligible

    with TestClient(api.app) as http:
        sign_in(http, 42)
        done = http.post("/workouts/1/classification", json={"workout_class": "interval"}).json()
        assert 1 in done["reclassified"]

    db.expire_all()
    assert db.get(WorkoutMetric, 1).ef is None  # EF is steady-only, so the stale value is gone
