"""Signed session cookies.

The session holds only the athlete id. Every request that touches training data resolves
the athlete from the cookie, never from the URL, so one athlete can't read another's data
by guessing ids.
"""

from fastapi import Cookie, HTTPException, Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from erg.config import Settings, get_settings

SESSION_COOKIE = "erg_session"
SALT = "erg-session-v1"


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    secret = settings.session_secret or settings.token_encryption_key
    if not secret:
        raise RuntimeError("SESSION_SECRET (or TOKEN_ENCRYPTION_KEY) must be set")
    return URLSafeTimedSerializer(secret, salt=SALT)


def mint(athlete_id: int, settings: Settings | None = None) -> str:
    """The signed cookie value on its own, for callers that set the cookie themselves."""
    return _serializer(settings or get_settings()).dumps({"athlete_id": athlete_id})


def issue(response: Response, athlete_id: int, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    response.set_cookie(
        SESSION_COOKIE,
        mint(athlete_id, settings),
        max_age=settings.session_days * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookies,
        path="/",
    )


def clear(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def read(cookie: str | None, settings: Settings | None = None) -> int | None:
    if not cookie:
        return None
    settings = settings or get_settings()
    try:
        payload = _serializer(settings).loads(cookie, max_age=settings.session_days * 24 * 3600)
    except (BadSignature, SignatureExpired):
        return None
    athlete_id = payload.get("athlete_id") if isinstance(payload, dict) else None
    return int(athlete_id) if athlete_id is not None else None


def current_athlete(erg_session: str | None = Cookie(default=None)) -> int:
    """FastAPI dependency: the signed-in athlete, or 401."""
    athlete_id = read(erg_session)
    if athlete_id is None:
        raise HTTPException(401, "not signed in; visit /auth/login to connect your Concept2 account")
    return athlete_id


def optional_athlete(request: Request) -> int | None:
    return read(request.cookies.get(SESSION_COOKIE))
