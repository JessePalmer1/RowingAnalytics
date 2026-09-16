import httpx
import pytest
import respx

from erg.c2 import oauth
from erg.c2.client import ACCEPT, C2Client, C2Error
from erg.c2.ratelimit import TokenBucket


class NoLimit:
    def acquire(self):
        pass


def make_client(settings, token="tok", **kw):
    return C2Client(settings, lambda: token, http=httpx.Client(), limiter=NoLimit(), sleep=lambda s: None, **kw)


def page(rows, current, total):
    links = {"next": f"https://c2.test/api/users/me/results?page={current + 1}"} if current < total else {}
    return {"data": rows, "meta": {"pagination": {"current_page": current, "total_pages": total, "links": links}}}


@respx.mock
def test_paginates_until_no_next_and_keeps_filters(settings):
    route = respx.get("https://c2.test/api/users/me/results").mock(
        side_effect=[
            httpx.Response(200, json=page([{"id": 1}, {"id": 2}], 1, 3)),
            httpx.Response(200, json=page([{"id": 3}], 2, 3)),
            httpx.Response(200, json=page([{"id": 4}], 3, 3)),
        ]
    )
    ids = [r["id"] for r in make_client(settings).iter_results()]
    assert ids == [1, 2, 3, 4]
    assert route.call_count == 3
    for i, call in enumerate(route.calls, start=1):
        q = call.request.url.params
        assert (q["page"], q["number"], q["type"]) == (str(i), "250", "rower")
        assert call.request.headers["Accept"] == ACCEPT
        assert call.request.headers["Authorization"] == "Bearer tok"


@respx.mock
def test_retries_on_503_honoring_retry_after(settings):
    sleeps = []
    respx.get("https://c2.test/api/users/me").mock(
        side_effect=[
            httpx.Response(503, headers={"Retry-After": "7"}),
            httpx.Response(200, json={"data": {"id": 42}}),
        ]
    )
    client = C2Client(settings, lambda: "t", http=httpx.Client(), limiter=NoLimit(), sleep=sleeps.append)
    assert client.get_me() == {"id": 42}
    assert sleeps == [7.0]


@respx.mock
def test_non_retryable_error_raises(settings):
    respx.get("https://c2.test/api/users/me").mock(return_value=httpx.Response(401, text="nope"))
    with pytest.raises(C2Error) as exc:
        make_client(settings).get_me()
    assert exc.value.status == 401


def test_token_bucket_throttles_after_burst():
    now = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        now[0] += s

    bucket = TokenBucket(rate_per_sec=2, burst=2, clock=lambda: now[0], sleep=sleep)
    for _ in range(4):
        bucket.acquire()
    assert slept == [0.5, 0.5]


def test_authorize_url_always_sends_explicit_scope(settings):
    url = httpx.URL(oauth.authorize_url(settings, "abc"))
    assert url.params["scope"] == "user:read,results:read"
    assert url.params["state"] == "abc"
    assert url.params["response_type"] == "code"
