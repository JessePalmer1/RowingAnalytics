from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    c2_base_url: str = "https://log.concept2.com"
    c2_client_id: str = ""
    c2_client_secret: str = ""
    c2_redirect_uri: str = "http://localhost:8000/auth/callback"
    c2_scopes: str = "user:read,results:read"

    # Self-imposed: C2 doesn't enforce rate limits yet but reserves the right to.
    c2_rate_per_sec: float = 2.0
    c2_burst: int = 5

    # Empty means local mode: an embedded Postgres in a temporary directory, created at
    # startup and deleted on exit (see erg.embedded). Set it to use a persistent database.
    database_url: str = ""

    # Session cookies. Falls back to the token key so local setups need no extra config;
    # set SECURE_COOKIES=true once the app is served over HTTPS.
    session_secret: str = ""
    session_days: int = 30
    secure_cookies: bool = False
    token_encryption_key: str = ""
    default_timezone: str = "America/New_York"

    @property
    def local_mode(self) -> bool:
        return not self.database_url


@lru_cache
def get_settings() -> Settings:
    return finalize(Settings())


def finalize(settings: Settings) -> Settings:
    if settings.local_mode and not settings.token_encryption_key:
        # Local mode's database is thrown away on exit, so a key that only lives as long as
        # the process loses nothing. A persistent database must set one: tokens encrypted with
        # a key that disappears on restart could never be decrypted again.
        from cryptography.fernet import Fernet

        settings.token_encryption_key = Fernet.generate_key().decode()
    return settings
