import logging
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from erg.c2.ratelimit import TokenBucket
from erg.config import Settings

log = logging.getLogger(__name__)

ACCEPT = "application/vnd.c2logbook.v1+json"
MAX_PAGE_SIZE = 250
RETRY_STATUSES = {429, 500, 502, 503, 504}


class C2Error(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"Concept2 API returned {status}: {body[:500]}")
        self.status = status


class C2Client:
    """Thin Logbook API client. `token_provider` is called per request so refreshes take effect mid-backfill."""

    def __init__(
        self,
        settings: Settings,
        token_provider: Callable[[], str],
        http: httpx.Client | None = None,
        limiter: TokenBucket | None = None,
        max_retries: int = 5,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base_url = settings.c2_base_url.rstrip("/")
        self._token = token_provider
        self._http = http or httpx.Client(timeout=30)
        self._limiter = limiter or TokenBucket(settings.c2_rate_per_sec, settings.c2_burst)
        self._max_retries = max_retries
        self._sleep = sleep

    def close(self) -> None:
        self._http.close()

    def _get(self, url: str, params: dict | None = None) -> dict[str, Any]:
        if not url.startswith("http"):
            url = f"{self.base_url}{url}"
        for attempt in range(self._max_retries + 1):
            self._limiter.acquire()
            try:
                resp = self._http.get(
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {self._token()}", "Accept": ACCEPT},
                )
            except httpx.TransportError as exc:
                if attempt == self._max_retries:
                    raise
                log.warning("transport error on %s (%s); retrying", url, exc)
                self._sleep(min(2**attempt, 60))
                continue
            if resp.status_code in RETRY_STATUSES and attempt < self._max_retries:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else min(2**attempt, 60)
                log.warning("%s on %s; retrying in %.1fs", resp.status_code, url, delay)
                self._sleep(delay)
                continue
            if resp.status_code != 200:
                raise C2Error(resp.status_code, resp.text)
            return resp.json()
        raise AssertionError("unreachable")

    def get_me(self) -> dict[str, Any]:
        return self._get("/api/users/me")["data"]

    def iter_results(
        self,
        user: str | int = "me",
        machine_type: str | None = "rower",
        updated_after: str | None = None,
        page_size: int = MAX_PAGE_SIZE,
    ) -> Iterator[dict[str, Any]]:
        params: dict[str, Any] = {"number": min(page_size, MAX_PAGE_SIZE)}
        if machine_type:
            params["type"] = machine_type
        if updated_after:
            params["updated_after"] = updated_after  # GMT
        page = 1
        while True:
            body = self._get(f"/api/users/{user}/results", {**params, "page": page})
            rows = body.get("data") or []
            yield from rows
            pagination = (body.get("meta") or {}).get("pagination") or {}
            # links.next is only a "more pages" signal; we build page URLs ourselves so
            # filters and page size can't be dropped by however the server renders it.
            has_next = bool((pagination.get("links") or {}).get("next")) or pagination.get(
                "current_page", page
            ) < pagination.get("total_pages", 0)
            if not rows or not has_next:
                return
            page += 1
