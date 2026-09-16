from datetime import datetime, timedelta, timezone

import httpx
import respx
from conftest import result_payload
from sqlalchemy import select

from erg import crypto
from erg.c2 import oauth
from erg.c2.client import C2Client
from erg.models import OAuthToken, Workout
from erg.sync import backfill
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
