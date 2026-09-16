from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from erg import crypto
from erg.c2 import oauth
from erg.config import Settings
from erg.models import OAuthToken

REFRESH_MARGIN = timedelta(minutes=5)


def store_token(session: Session, athlete_id: int, token: oauth.TokenResponse) -> None:
    values = {
        "athlete_id": athlete_id,
        "access_token": crypto.encrypt(token.access_token),
        "refresh_token": crypto.encrypt(token.refresh_token),
        "expires_at": token.expires_at,
        "scopes": token.scopes,
    }
    stmt = insert(OAuthToken).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[OAuthToken.athlete_id],
        set_={k: stmt.excluded[k] for k in values if k != "athlete_id"} | {"updated_at": datetime.now(timezone.utc)},
    )
    session.execute(stmt)


def get_access_token(
    session: Session, settings: Settings, athlete_id: int, http: httpx.Client | None = None
) -> str:
    """Return a valid access token, refreshing if near expiry.

    The row is locked so concurrent callers can't both spend the same refresh token
    (it rotates on use, so the loser would be locked out). The rotated token is committed
    before returning: losing it means losing access.
    """
    row = session.execute(
        select(OAuthToken).where(OAuthToken.athlete_id == athlete_id).with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise LookupError(f"no OAuth token stored for athlete {athlete_id}; run the authorization flow")

    if row.expires_at - REFRESH_MARGIN > datetime.now(timezone.utc):
        session.commit()  # release lock
        return crypto.decrypt(row.access_token)

    new = oauth.refresh(settings, crypto.decrypt(row.refresh_token), http=http)
    store_token(session, athlete_id, new)
    session.commit()
    return new.access_token
