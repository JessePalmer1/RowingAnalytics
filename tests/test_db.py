from datetime import datetime, timedelta, timezone

import httpx
import respx
from conftest import result_payload
from sqlalchemy import select

from erg import crypto
from erg.c2 import oauth
from erg.c2.client import C2Client
from erg.models import IntervalSplit, OAuthToken, Stroke, Workout
from erg.sync import MAX_STROKE_ATTEMPTS, backfill, fetch_strokes
from erg.tokens import get_access_token, store_token
from test_client import NoLimit


def fake_c2(results):
    respx.get("https://c2.test/api/users/me").mock(
        return_value=httpx.Response(200, json={"data": {"id": 42, "username": "jess", "weight": 7500}})
    )
    respx.get("https://c2.test/api/users/me/results").mock(
        return_value=httpx.Response(
            200, json={"data": results, "meta": {"pagination": {"current_page": 1, "total_pages": 1, "links": {}}}}
        )
    )


def run(db, settings):
    client = C2Client(settings, lambda: "tok", http=httpx.Client(), limiter=NoLimit())
    return backfill(db, client, settings)


@respx.mock
def test_backfill_is_idempotent(db, settings):
    fake_c2([
        result_payload(id=1),
        result_payload(id=2, date="2026-02-11 07:00:00", date_utc="2026-02-11 07:00:00", stroke_data=False),
    ])

    first = run(db, settings)
    assert (first.fetched, first.inserted, first.updated, first.unchanged) == (2, 2, 0, 0)
    assert first.pending_stroke_fetch == [1]
    stamps = dict(db.execute(select(Workout.id, Workout.updated_at)).all())

    second = run(db, settings)
    assert (second.inserted, second.updated, second.unchanged) == (0, 0, 2)
    assert second.pending_stroke_fetch == []
    db.expire_all()
    assert dict(db.execute(select(Workout.id, Workout.updated_at)).all()) == stamps


@respx.mock
def test_edited_result_is_updated(db, settings):
    fake_c2([result_payload(id=1)])
    run(db, settings)

    respx.clear()
    fake_c2([result_payload(id=1, comments="felt good", heart_rate={"average": 0})])
    stats = run(db, settings)
    assert stats.updated == 1
    w = db.get(Workout, 1, populate_existing=True)
    assert (w.comments, w.hr_avg, w.hr_quality) == ("felt good", None, "absent")


@respx.mock
def test_dedupe_collision_is_skipped_not_fatal(db, settings):
    fake_c2([result_payload(id=1), result_payload(id=99), result_payload(id=3, date="2026-03-01 09:00:00")])
    stats = run(db, settings)
    assert stats.conflicts == [99]
    assert sorted(db.execute(select(Workout.id)).scalars()) == [1, 3]


@respx.mock
def test_refresh_persists_rotated_refresh_token(db, settings):
    fake_c2([])
    run(db, settings)  # creates athlete 42
    expiring = datetime.now(timezone.utc) + timedelta(minutes=1)
    store_token(db, 42, oauth.TokenResponse("old-access", "old-refresh", expiring, "user:read"))
    db.commit()

    token_route = respx.post("https://c2.test/oauth/access_token").mock(
        return_value=httpx.Response(
            200, json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 604800}
        )
    )
    assert get_access_token(db, settings, 42) == "new-access"
    sent = dict(httpx.QueryParams(token_route.calls.last.request.content.decode()))
    assert (sent["grant_type"], sent["refresh_token"]) == ("refresh_token", "old-refresh")

    row = db.get(OAuthToken, 42, populate_existing=True)
    assert crypto.decrypt(row.refresh_token) == "new-refresh"
    assert row.refresh_token != "new-refresh"  # encrypted at rest

    # Still-valid token: no second refresh.
    assert get_access_token(db, settings, 42) == "new-access"
    assert token_route.call_count == 1


INTERVAL_WORKOUT = {
    "intervals": [
        {"type": "time", "time": 1000, "distance": 481, "rest_time": 200, "heart_rate": {}},
        {"type": "time", "time": 1000, "distance": 478, "rest_time": 200, "heart_rate": {}},
    ]
}
STROKES = [
    {"t": 10, "d": 35, "p": 0, "spm": 0, "hr": 80},
    {"t": 1000, "d": 4810, "p": 1040, "spm": 29, "hr": 160},
    {"t": 1180, "d": 5390, "p": 2000, "spm": 18, "hr": 150},
    {"t": 7, "d": 31, "p": 1100, "spm": 30, "hr": 140},
    {"t": 1000, "d": 4780, "p": 1045, "spm": 29, "hr": 165},
]


def strokes_url(wid):
    return f"https://c2.test/api/users/me/results/{wid}/strokes"


@respx.mock
def test_stroke_fetch_queue(db, settings):
    fake_c2([
        result_payload(id=1, workout_type="FixedTimeInterval", workout=INTERVAL_WORKOUT),
        result_payload(id=2, date="2026-02-11 07:00:00", date_utc="2026-02-11 07:00:00"),  # will 404
        result_payload(id=3, date="2026-02-12 07:00:00", date_utc="2026-02-12 07:00:00"),  # non-retryable API error
        result_payload(id=4, date="2026-02-13 07:00:00", date_utc="2026-02-13 07:00:00", stroke_data=False),
    ])
    run(db, settings)
    assert db.query(IntervalSplit).filter_by(workout_id=1).count() == 2

    respx.get(strokes_url(1)).mock(return_value=httpx.Response(200, json={"data": STROKES}))
    respx.get(strokes_url(2)).mock(
        return_value=httpx.Response(404, json={"message": "This workout does not have any stroke data associated with it"})
    )
    s3 = respx.get(strokes_url(3)).mock(return_value=httpx.Response(400, text="bad"))
    no_strokes = respx.get(strokes_url(4))

    client = C2Client(settings, lambda: "tok", http=httpx.Client(), limiter=NoLimit(), sleep=lambda s: None)
    stats = fetch_strokes(db, client, 42)
    assert (stats.fetched, stats.strokes, stats.missing, list(stats.errors)) == (1, 5, [2], [3])
    assert s3.call_count == 1  # failure isn't retried in a tight loop
    assert not no_strokes.called

    db.expire_all()
    status = dict(db.execute(select(Workout.id, Workout.stroke_status)).all())
    assert status == {1: "fetched", 2: "missing", 3: "error", 4: "not_available"}
    rows = db.execute(select(Stroke.interval_idx, Stroke.is_rest, Stroke.hr).where(Stroke.workout_id == 1).order_by(Stroke.seq)).all()
    assert [tuple(r) for r in rows] == [(0, False, 80), (0, False, 160), (0, True, 150), (1, False, 140), (1, False, 165)]

    # Re-running retries only the errored one; fetched and missing are left alone.
    stats = fetch_strokes(db, client, 42)
    assert (stats.fetched, list(stats.errors)) == (0, [3])
    db.expire_all()
    assert db.get(Workout, 3).stroke_attempts == 2

    # Gives up after MAX_STROKE_ATTEMPTS.
    for _ in range(MAX_STROKE_ATTEMPTS):
        fetch_strokes(db, client, 42)
    assert s3.call_count == MAX_STROKE_ATTEMPTS


@respx.mock
def test_refetch_replaces_strokes_and_edit_requeues(db, settings):
    fake_c2([result_payload(id=1, workout_type="FixedTimeInterval", workout=INTERVAL_WORKOUT)])
    run(db, settings)
    respx.get(strokes_url(1)).mock(return_value=httpx.Response(200, json={"data": STROKES}))
    client = C2Client(settings, lambda: "tok", http=httpx.Client(), limiter=NoLimit())
    fetch_strokes(db, client, 42)

    # Unchanged backfill leaves it fetched.
    run(db, settings)
    db.expire_all()
    assert db.get(Workout, 1).stroke_status == "fetched"

    # An edit in the logbook re-queues, and the refetch replaces rather than duplicates.
    respx.get("https://c2.test/api/users/me/results").mock(
        return_value=httpx.Response(200, json={
            "data": [result_payload(id=1, workout_type="FixedTimeInterval", workout=INTERVAL_WORKOUT, comments="edited")],
            "meta": {"pagination": {"current_page": 1, "total_pages": 1, "links": {}}},
        })
    )
    run(db, settings)
    db.expire_all()
    assert db.get(Workout, 1).stroke_status == "pending"
    fetch_strokes(db, client, 42)
    assert db.query(Stroke).filter_by(workout_id=1).count() == len(STROKES)
