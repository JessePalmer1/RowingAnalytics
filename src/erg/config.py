from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    c2_base_url: str = "https://log-dev.concept2.com"
    c2_client_id: str = ""
    c2_client_secret: str = ""
    c2_redirect_uri: str = "http://localhost:8000/auth/callback"
    c2_scopes: str = "user:read,results:read"

    # Self-imposed: C2 doesn't enforce rate limits yet but reserves the right to.
    c2_rate_per_sec: float = 2.0
    c2_burst: int = 5

    database_url: str = "postgresql+psycopg://erg:erg@localhost:5432/erg"
    token_encryption_key: str = ""
    default_timezone: str = "America/New_York"


@lru_cache
def get_settings() -> Settings:
    return Settings()
