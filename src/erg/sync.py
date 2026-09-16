import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import delete, insert as sa_insert, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from erg.c2.client import C2Client, C2Error
from erg.config import Settings
from erg.models import Athlete, IntervalSplit, Stroke, Workout
from erg.normalize import normalize_athlete, normalize_interval_splits, normalize_workout
from erg.strokes import parse_strokes

log = logging.getLogger(__name__)

MAX_STROKE_ATTEMPTS = 5


@dataclass
class BackfillStats:
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    conflicts: list[int] = field(default_factory=list)  # result ids rejected by the dedupe constraint
    pending_stroke_fetch: list[int] = field(default_factory=list)


@dataclass
class StrokeFetchStats:
    fetched: int = 0
    strokes: int = 0
    missing: list[int] = field(default_factory=list)
    errors: dict[int, str] = field(default_factory=dict)
    warnings: dict[int, str] = field(default_factory=dict)


def upsert_athlete(session: Session, payload: dict[str, Any]) -> int:
    values = normalize_athlete(payload)
    stmt = insert(Athlete).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Athlete.id],
        set_={k: stmt.excluded[k] for k in values if k != "id"} | {"updated_at": literal_column("now()")},
        where=Athlete.raw.is_distinct_from(stmt.excluded.raw),
    )
    session.execute(stmt)
    return values["id"]


def replace_interval_splits(session: Session, payload: dict[str, Any]) -> None:
    session.execute(delete(IntervalSplit).where(IntervalSplit.workout_id == payload["id"]))
    rows = normalize_interval_splits(payload)
    if rows:
        session.execute(sa_insert(IntervalSplit), rows)


def upsert_workout(session: Session, values: dict[str, Any]) -> str:
    """Returns 'inserted', 'updated' or 'unchanged'. Only rewrites when the raw payload changed.

    A new or changed workout is (re)queued for stroke fetch.
    """
    values = values | {
        "stroke_status": "pending" if values["has_strokes"] else "not_available",
        "stroke_attempts": 0,
        "stroke_error": None,
    }
    stmt = insert(Workout).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Workout.id],
        set_={k: stmt.excluded[k] for k in values if k != "id"} | {"updated_at": literal_column("now()")},
        where=Workout.raw.is_distinct_from(stmt.excluded.raw),
    ).returning(literal_column("(xmax = 0)").label("inserted"))
    row = session.execute(stmt).first()
    if row is None:
        return "unchanged"
    replace_interval_splits(session, values["raw"])
    return "inserted" if row.inserted else "updated"


def backfill(session: Session, client: C2Client, settings: Settings) -> BackfillStats:
    stats = BackfillStats()
    athlete_id = upsert_athlete(session, client.get_me())
    session.commit()

    for payload in client.iter_results("me", machine_type="rower"):
        stats.fetched += 1
        values = normalize_workout(payload, athlete_id, settings.default_timezone)
        try:
            with session.begin_nested():
                outcome = upsert_workout(session, values)
        except IntegrityError:
            log.warning("result %s collides with an existing workout on date+time+distance; skipped", payload["id"])
            stats.conflicts.append(payload["id"])
            continue
        setattr(stats, outcome, getattr(stats, outcome) + 1)
        if outcome != "unchanged" and values["has_strokes"]:
            stats.pending_stroke_fetch.append(values["id"])
    session.commit()
    return stats


def renormalize(session: Session, settings: Settings) -> int:
    """Re-derive normalized columns and interval splits from stored raw payloads. No API calls."""
    workouts = session.execute(select(Workout.id, Workout.athlete_id, Workout.raw)).all()
    for w in workouts:
        values = normalize_workout(w.raw, w.athlete_id, settings.default_timezone)
        values.pop("id")
        session.execute(update(Workout).where(Workout.id == w.id).values(**values))
        replace_interval_splits(session, w.raw)
    session.commit()
    return len(workouts)


def _claim_next(session: Session, athlete_id: int, retry_errors: bool, exclude: set[int]) -> Workout | None:
    statuses = ["pending", "error"] if retry_errors else ["pending"]
    return session.execute(
        select(Workout)
        .where(
            Workout.athlete_id == athlete_id,
            Workout.stroke_status.in_(statuses),
            Workout.stroke_attempts < MAX_STROKE_ATTEMPTS,
            Workout.id.not_in(exclude),
        )
        .order_by(Workout.ended_at_utc.desc())
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()


def store_strokes(session: Session, workout: Workout, raw_strokes: list[dict[str, Any]]) -> tuple[int, str | None]:
    summary = normalize_interval_splits(workout.raw)
    is_interval = bool(summary) and summary[0]["kind"] == "interval"
    work_times = [row["time_s"] for row in summary] if is_interval else None

    parsed = parse_strokes(workout.id, raw_strokes, work_times)
    session.execute(delete(Stroke).where(Stroke.workout_id == workout.id))
    if parsed.rows:
        session.execute(sa_insert(Stroke), parsed.rows)
    return len(parsed.rows), "; ".join(parsed.warnings) or None


def fetch_strokes(
    session: Session, client: C2Client, athlete_id: int, limit: int | None = None, retry_errors: bool = True
) -> StrokeFetchStats:
    """Drain the stroke queue for one athlete. Each workout commits independently, so a crash loses at most one."""
    stats = StrokeFetchStats()
    tried: set[int] = set()  # a failed workout is retried on the next run, not in a tight loop
    while limit is None or len(tried) < limit:
        workout = _claim_next(session, athlete_id, retry_errors, tried)
        if workout is None:
            session.rollback()
            break
        tried.add(workout.id)
        now = datetime.now(timezone.utc)
        try:
            raw_strokes = client.get_strokes(workout.id)
        except C2Error as exc:
            if exc.status == 404:
                # stroke_data: true can still 404; that's a property of the workout, not a transient failure.
                workout.stroke_status, workout.stroke_error = "missing", str(exc)
                stats.missing.append(workout.id)
            else:
                workout.stroke_status, workout.stroke_error = "error", str(exc)
                workout.stroke_attempts += 1
                stats.errors[workout.id] = str(exc)
            session.commit()
            continue
        except httpx.TransportError as exc:
            workout.stroke_status, workout.stroke_error = "error", f"transport: {exc}"
            workout.stroke_attempts += 1
            stats.errors[workout.id] = str(exc)
            session.commit()
            continue

        count, warning = store_strokes(session, workout, raw_strokes)
        workout.stroke_status = "fetched"
        workout.stroke_error = None
        workout.stroke_warning = warning
        workout.strokes_fetched_at = now
        session.commit()
        stats.fetched += 1
        stats.strokes += count
        if warning:
            stats.warnings[workout.id] = warning
            log.warning("workout %s: %s", workout.id, warning)
    return stats
