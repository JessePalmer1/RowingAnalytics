"""In-app import: everything `erg sync`, `classify` and `metrics` do, triggered from the browser.

One background job per athlete, run in a thread inside the web process, with progress the UI
can poll. It is safe to re-run: every step is idempotent, and on an up-to-date account the
whole thing is one page of workouts and no stroke fetches.
"""

import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from erg.config import get_settings
from erg.db import session_scope
from erg.metrics_runner import compute_load, compute_workout_metrics
from erg.pipeline import classify_all
from erg.services import client_for_athlete
from erg.sync import backfill, fetch_strokes

log = logging.getLogger(__name__)

STAGES = ("workouts", "strokes", "classify", "metrics", "done")


@dataclass
class ImportStatus:
    athlete_id: int
    state: str = "idle"  # idle | running | done | error
    stage: str | None = None
    done: int = 0  # progress within the stage, where it is countable
    total: int = 0
    workouts: int | None = None  # workouts in the logbook
    new_workouts: int | None = None
    errors: list[str] = field(default_factory=list)
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def as_dict(self) -> dict:
        return asdict(self)


_jobs: dict[int, ImportStatus] = {}
_lock = threading.Lock()


def status(athlete_id: int) -> ImportStatus:
    with _lock:
        return _jobs.get(athlete_id) or ImportStatus(athlete_id)


def start(athlete_id: int, run_in_thread: bool = True) -> ImportStatus:
    """Start an import unless one is already running for this athlete."""
    with _lock:
        job = _jobs.get(athlete_id)
        if job and job.state == "running":
            return job
        job = ImportStatus(athlete_id, state="running", stage="workouts", started_at=datetime.now(timezone.utc))
        _jobs[athlete_id] = job
    if run_in_thread:
        threading.Thread(target=_run, args=(job,), name=f"import-{athlete_id}", daemon=True).start()
    else:
        _run(job)
    return job


def _set(job: ImportStatus, **changes) -> None:
    with _lock:
        for key, value in changes.items():
            setattr(job, key, value)


def _run(job: ImportStatus) -> None:
    settings = get_settings()
    client = client_for_athlete(settings, job.athlete_id)
    try:
        _set(job, stage="workouts", done=0, total=0)
        with session_scope() as s:
            stats = backfill(s, client, settings)
        _set(job, workouts=stats.fetched, new_workouts=stats.inserted + stats.updated)

        _set(job, stage="strokes", done=0, total=0)
        with session_scope() as s:
            strokes = fetch_strokes(
                s, client, job.athlete_id, on_progress=lambda done, total: _set(job, done=done, total=total)
            )
        if strokes.errors:
            # Missing strokes for a few workouts shouldn't fail the import; report them.
            _set(job, errors=[f"workout {wid}: {msg}" for wid, msg in strokes.errors.items()])

        _set(job, stage="classify", done=0, total=0)
        with session_scope() as s:
            classify_all(s, job.athlete_id)

        _set(job, stage="metrics")
        with session_scope() as s:
            compute_workout_metrics(s, job.athlete_id)
            compute_load(s, job.athlete_id)

        _set(job, state="done", stage="done", finished_at=datetime.now(timezone.utc))
    except Exception as exc:  # the thread must always leave a final state behind
        log.exception("import failed for athlete %s", job.athlete_id)
        _set(job, state="error", error=str(exc), finished_at=datetime.now(timezone.utc))
    finally:
        client.close()
