from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from erg.config import Settings


class OAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class TokenResponse:
    access_token: str
    refresh_token: str
    expires_at: datetime
    scopes: str


def authorize_url(settings: Settings, state: str) -> str:
    params = {
        "client_id": settings.c2_client_id,
        # Always explicit: omitting scope silently grants user:read,results:write.
        "scope": settings.c2_scopes,
        "response_type": "code",
        "redirect_uri": settings.c2_redirect_uri,
        "state": state,
    }
    return f"{settings.c2_base_url}/oauth/authorize?{urlencode(params)}"


def _token_request(settings: Settings, data: dict, http: httpx.Client | None) -> TokenResponse:
    body = {
        "client_id": settings.c2_client_id,
        "client_secret": settings.c2_client_secret,
        "scope": settings.c2_scopes,
        **data,
    }
    client = http or httpx.Client(timeout=30)
    try:
        resp = client.post(f"{settings.c2_base_url}/oauth/access_token", data=body)
    finally:
        if http is None:
            client.close()
    if resp.status_code != 200:
        raise OAuthError(f"token endpoint returned {resp.status_code}: {resp.text[:500]}")
    payload = resp.json()
    return TokenResponse(
        access_token=payload["access_token"],
        refresh_token=payload["refresh_token"],
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=int(payload["expires_in"])),
        scopes=payload.get("scope") or settings.c2_scopes,
    )


def exchange_code(settings: Settings, code: str, http: httpx.Client | None = None) -> TokenResponse:
    return _token_request(
        settings,
        {"grant_type": "authorization_code", "code": code, "redirect_uri": settings.c2_redirect_uri},
        http,
    )


def refresh(settings: Settings, refresh_token: str, http: httpx.Client | None = None) -> TokenResponse:
    # The returned refresh token REPLACES the old one; the caller must persist it.
    return _token_request(settings, {"grant_type": "refresh_token", "refresh_token": refresh_token}, http)
