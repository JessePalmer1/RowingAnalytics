"""Vercel entrypoint. Vercel looks for a FastAPI instance named `app` in app.py; the real
application lives in src/erg (a src layout), so put src on the import path and re-export it."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from erg.api import app  # noqa: E402,F401
