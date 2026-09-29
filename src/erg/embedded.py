"""Local mode: a real Postgres, embedded, with no Docker and nothing left behind.

pgserver ships Postgres binaries as a pip package (Windows, macOS, Linux). The database
lives in a temporary directory that is deleted when the process exits, so each run starts
clean and nothing about another person's training data outlives the session.

Using real Postgres rather than SQLite means every query, upsert and JSONB column behaves
exactly as it does against the persistent database — there is only one dialect to support.
"""

import atexit
import logging
import shutil
import tempfile
import threading

log = logging.getLogger(__name__)

_lock = threading.Lock()
_url: str | None = None


def database_url() -> str:
    """Start the embedded server once per process and return a SQLAlchemy URL for it."""
    global _url
    with _lock:
        if _url is None:
            try:
                import pgserver
            except ImportError:
                raise RuntimeError(
                    "Local mode needs the embedded Postgres (pgserver), which only supports "
                    "Python 3.11 and 3.12. Run through `uv run` so .python-version picks 3.12, "
                    "or set DATABASE_URL to use your own Postgres."
                ) from None

            data_dir = tempfile.mkdtemp(prefix="erg-local-")
            log.info("starting embedded Postgres in %s", data_dir)
            server = pgserver.get_server(data_dir, cleanup_mode="stop")
            # Stop the server before removing its files; registered after get_server's own
            # handler so it runs first (atexit is last-in, first-out).
            atexit.register(shutil.rmtree, data_dir, ignore_errors=True)
            atexit.register(server.cleanup)
            _url = server.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
        return _url


def create_schema(engine) -> None:
    """A fresh embedded database has no history to migrate, so build the schema directly."""
    from erg.models import Base

    Base.metadata.create_all(engine)
