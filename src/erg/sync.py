import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import literal_column
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from erg.c2.client import C2Client
from erg.config import Settings
from erg.models import Athlete, Workout
from erg.normalize import normalize_athlete, normalize_workout

log = logging.getLogger(__name__)


@dataclass
class BackfillStats:
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    conflicts: list[int] = field(default_factory=list)  # result ids rejected by the dedupe constraint
    pending_stroke_fetch: list[int] = field(default_factory=list)


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


def upsert_workout(session: Session, values: dict[str, Any]) -> str:
    """Returns 'inserted', 'updated' or 'unchanged'. Only rewrites when the raw payload changed."""
    stmt = insert(Workout).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Workout.id],
        set_={k: stmt.excluded[k] for k in values if k != "id"} | {"updated_at": literal_column("now()")},
        where=Workout.raw.is_distinct_from(stmt.excluded.raw),
    ).returning(literal_column("(xmax = 0)").label("inserted"))
    row = session.execute(stmt).first()
    if row is None:
        return "unchanged"
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
            stats.pending_stroke_fetch.append(values["id"])  # consumed by the Phase 2 stroke worker
    session.commit()
    return stats
