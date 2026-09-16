from erg.c2.client import C2Client
from erg.config import Settings
from erg.db import session_scope
from erg.tokens import get_access_token


def client_for_athlete(settings: Settings, athlete_id: int) -> C2Client:
    def token_provider() -> str:
        # Separate session so a refresh commit never interleaves with ingest transactions.
        with session_scope() as s:
            return get_access_token(s, settings, athlete_id)

    return C2Client(settings, token_provider)
