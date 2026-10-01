"""In-app import: everything `erg sync`, `classify` and `metrics` do, triggered from the browser.

The import is a resumable state machine stored in the `import_job` table. The UI (or the MCP
`sync` tool) keeps calling `step()`, and each call does at most `budget_s` seconds of work
before returning progress. Nothing runs in the background, which is what serverless hosting
needs: a function is frozen between requests and capped at a few minutes per request, so a
long import has to be a series of short requests. It works identically on a laptop.

Every stage is idempotent, so re-running on an up-to-date account is one page of workouts
and no stroke fetches.
"""

import logging
import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from erg.config import get_settings
from erg.db import session_scope
from erg.metrics_runner import compute_load, compute_workout_metrics
from erg.models import ImportJob
from erg.pipeline import classify_all
from erg.services import client_for_athlete
from erg.sync import backfill, fetch_strokes, pending_stroke_count

log = logging.getLogger(__name__)

STAGES = ("workouts", "strokes", "classify", "metrics", "done")
DEFAULT_BUDGET_S = 25.0  # well inside serverless request limits


def as_dict(job: ImportJob | None, athlete_id: int) -> dict:
    if job is None:
        return {"athlete_id": athlete_id, "state": "idle", "stage": None, "done": 0, "total": 0,
                "workouts": None, "new_workouts": None, "errors": [], "error": None,
                "started_at": None, "finished_at": None}
    return {
        "athlete_id": job.athlete_id,
        "state": job.state,
        "stage": job.stage,
        "done": job.done,
        "total": job.total,
        "workouts": job.workouts,
        "new_workouts": job.new_workouts,
        "errors": list(job.errors or []),
        "error": job.error,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }


def status(athlete_id: int) -> dict:
    with session_scope() as s:
        return as_dict(s.get(ImportJob, athlete_id), athlete_id)


def begin(athlete_id: int) -> dict:
    """Start a fresh import, unless one is already running."""
    with session_scope() as s:
        job = s.get(ImportJob, athlete_id)
        if job is not None and job.state == "running":
            return as_dict(job, athlete_id)
        if job is None:
            job = ImportJob(athlete_id=athlete_id)
            s.add(job)
        job.state, job.stage, job.done, job.total = "running", "workouts", 0, 0
        job.workouts = job.new_workouts = job.error = job.finished_at = None
        job.errors = []
        job.started_at = datetime.now(timezone.utc)
        s.flush()
        return as_dict(job, athlete_id)


def step(athlete_id: int, budget_s: float = DEFAULT_BUDGET_S) -> dict:
    """Advance the import by up to `budget_s` seconds of work and return its status.

    The job row is locked with SKIP LOCKED, so a second caller arriving at the same moment
    (two open tabs) usually just gets the status back. The stages commit as they go, which
    releases that lock, so overlap is still possible; it is harmless because stroke claims use
    SKIP LOCKED per workout and every stage is idempotent.
    """
    deadline = time.monotonic() + budget_s
    with session_scope() as s:
        job = s.execute(
            select(ImportJob).where(ImportJob.athlete_id == athlete_id).with_for_update(skip_locked=True)
        ).scalar_one_or_none()
        if job is None:
            return as_dict(s.get(ImportJob, athlete_id), athlete_id)
        if job.state != "running":
            return as_dict(job, athlete_id)
        try:
            _advance(s, job, deadline)
        except Exception as exc:  # always leave a readable final state
            log.exception("import failed for athlete %s", athlete_id)
            s.rollback()
            job = s.get(ImportJob, athlete_id)
            job.state, job.error = "error", str(exc)
            job.finished_at = datetime.now(timezone.utc)
        return as_dict(job, athlete_id)


def run_to_completion(athlete_id: int, budget_s: float = DEFAULT_BUDGET_S) -> dict:
    """Begin and keep stepping until finished. For the CLI, tests and the MCP sync tool."""
    result = begin(athlete_id)
    while result["state"] == "running":
        result = step(athlete_id, budget_s)
    return result


def _advance(s: Session, job: ImportJob, deadline: float) -> None:
    """Run stages until the budget is spent or the import finishes. Commits as it goes."""
    settings = get_settings()
    client = client_for_athlete(settings, job.athlete_id)
    try:
        first = True
        # At least one unit of work per step, so even a step with no time left moves forward.
        while job.state == "running" and (first or time.monotonic() < deadline):
            first = False
            if job.stage == "workouts":
                stats = backfill(s, client, settings)
                job.workouts, job.new_workouts = stats.fetched, stats.inserted + stats.updated
                job.stage, job.done, job.total = "strokes", 0, 0
            elif job.stage == "strokes":
                def progress(done: int, total: int) -> None:
                    # Counts accumulate across steps: this call's batch adds to what's done.
                    job.total = max(job.total, base + total)
                    job.done = base + done

                base = job.done
                strokes = fetch_strokes(s, client, job.athlete_id, on_progress=progress, deadline=deadline)
                if strokes.errors:
                    job.errors = list(job.errors or []) + [f"workout {w}: {m}" for w, m in strokes.errors.items()]
                # Check the queue itself: a batch that fetched nothing may simply have run
                # out of time, which is not the same as having nothing left to fetch.
                if pending_stroke_count(s, job.athlete_id) == 0:
                    job.stage = "classify"
            elif job.stage == "classify":
                classify_all(s, job.athlete_id)
                job.stage = "metrics"
            elif job.stage == "metrics":
                compute_workout_metrics(s, job.athlete_id)
                compute_load(s, job.athlete_id)
                job.state, job.stage = "done", "done"
                job.finished_at = datetime.now(timezone.utc)
            s.commit()
    finally:
        client.close()
