"""Runs classification and eligibility over stored workouts. No API calls."""

import logging
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import Integer, case, delete, func, literal_column, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from erg.classify import (
    BASELINE_MIN_WORK_S,
    CLASSIFIER_VERSION,
    Features,
    classify,
    is_solo,
    match_test_distance,
    steady_baseline,
)
from erg.eligibility import ELIGIBILITY_VERSION, EligibilityInputs, evaluate
from erg.models import (
    Athlete,
    ClassificationOverride,
    IntervalSplit,
    Stroke,
    Workout,
    WorkoutClassification,
    WorkoutEligibility,
)

log = logging.getLogger(__name__)


@dataclass
class ClassifyStats:
    workouts: int = 0
    classes: Counter = field(default_factory=Counter)
    overridden: int = 0
    low_confidence: list[tuple[int, str, float, str]] = field(default_factory=list)
    eligible: Counter = field(default_factory=Counter)
    steady_pace_auto: Decimal | None = None
    steady_pace: Decimal | None = None  # override if set, else auto
    interval_threshold: Decimal | None = None
    baseline_sessions: int = 0


def _stroke_work_totals(session: Session, athlete_id: int) -> dict[int, tuple[Decimal, Decimal]]:
    """Work distance/time summed over intervals, for workouts whose C2 summary is truncated."""
    per_interval = (
        select(
            Stroke.workout_id.label("workout_id"),
            func.max(Stroke.d_m).label("d"),
            func.max(Stroke.t_s).label("t"),
        )
        .join(Workout, Workout.id == Stroke.workout_id)
        .where(Workout.athlete_id == athlete_id, Stroke.is_rest.is_(False))
        .group_by(Stroke.workout_id, Stroke.interval_idx)
        .subquery()
    )
    rows = session.execute(
        select(per_interval.c.workout_id, func.sum(per_interval.c.d), func.sum(per_interval.c.t)).group_by(
            per_interval.c.workout_id
        )
    ).all()
    return {wid: (d, t) for wid, d, t in rows}


def _stroke_aggregates(session: Session, athlete_id: int) -> dict[int, dict]:
    rows = session.execute(
        select(
            Stroke.workout_id,
            func.count().label("stored"),
            func.count(Stroke.hr).label("with_hr"),
            func.count(case((Stroke.is_rest & Stroke.hr.is_not(None), 1))).label("rest_with_hr"),
            (func.max(Stroke.interval_idx) + 1).label("intervals"),
        )
        .join(Workout, Workout.id == Stroke.workout_id)
        .where(Workout.athlete_id == athlete_id)
        .group_by(Stroke.workout_id)
    ).all()
    return {r.workout_id: r._mapping for r in rows}


def _interval_hr_pairs(session: Session, athlete_id: int) -> dict[int, int]:
    """Intervals carrying both ending and rest HR — the only usable source for HRR."""
    rows = session.execute(
        select(IntervalSplit.workout_id, func.count().cast(Integer))
        .join(Workout, Workout.id == IntervalSplit.workout_id)
        .where(
            Workout.athlete_id == athlete_id,
            IntervalSplit.kind == "interval",
            IntervalSplit.hr_ending.is_not(None),
            IntervalSplit.hr_rest.is_not(None),
        )
        .group_by(IntervalSplit.workout_id)
    ).all()
    return {wid: count for wid, count in rows}


def _summary_interval_counts(session: Session, athlete_id: int) -> dict[int, int]:
    rows = session.execute(
        select(IntervalSplit.workout_id, func.count().cast(Integer))
        .join(Workout, Workout.id == IntervalSplit.workout_id)
        .where(Workout.athlete_id == athlete_id, IntervalSplit.kind == "interval")
        .group_by(IntervalSplit.workout_id)
    ).all()
    return {wid: count for wid, count in rows}


def classify_all(session: Session, athlete_id: int) -> ClassifyStats:
    stats = ClassifyStats()
    athlete = session.get(Athlete, athlete_id)
    if athlete is None:
        raise LookupError(f"athlete {athlete_id} not found")
    strokes = _stroke_aggregates(session, athlete_id)
    stroke_totals = _stroke_work_totals(session, athlete_id)
    summary_intervals = _summary_interval_counts(session, athlete_id)
    hr_pairs = _interval_hr_pairs(session, athlete_id)
    overrides = dict(
        session.execute(
            select(ClassificationOverride.workout_id, ClassificationOverride.workout_class)
            .join(Workout, Workout.id == ClassificationOverride.workout_id)
            .where(Workout.athlete_id == athlete_id)
        ).all()
    )
    now = datetime.now(timezone.utc)
    workouts = session.execute(select(Workout).where(Workout.athlete_id == athlete_id)).scalars().all()

    # Pass 1: features for every workout, using stroke totals where the C2 summary is truncated.
    features: dict[int, Features] = {}
    for w in workouts:
        agg = strokes.get(w.id, {})
        pace, pace_from_strokes = w.avg_pace_s_500, False
        if pace is None:
            d, t = stroke_totals.get(w.id, (None, None))
            if d and t and d > 0:
                pace, pace_from_strokes = t * 500 / d, True
        features[w.id] = Features(
            workout_id=w.id,
            work_time_s=w.work_time_s,
            work_distance_m=w.work_distance_m,
            rest_time_s=w.rest_time_s,
            workout_type=w.workout_type,
            avg_pace_s_500=pace,
            pace_from_strokes=pace_from_strokes,
            stroke_interval_count=agg.get("intervals") or 0,
            summary_interval_count=summary_intervals.get(w.id, 0),
        )

    # Pass 2: learn this athlete's steady pace from their own history. Tests and short solo
    # pieces are excluded, and so is anything overridden to a test, so tests never skew it.
    baseline_paces = [
        f.avg_pace_s_500
        for wid, f in features.items()
        if f.avg_pace_s_500 is not None
        and f.work_time_s >= BASELINE_MIN_WORK_S
        and not (is_solo(f) and match_test_distance(f.work_distance_m))
        and not overrides.get(wid, "").startswith("test_")
    ]
    athlete.steady_pace_auto = steady_baseline(baseline_paces)
    threshold = athlete.interval_threshold
    stats.steady_pace_auto = athlete.steady_pace_auto
    stats.steady_pace = athlete.effective_steady_pace
    stats.interval_threshold = threshold
    stats.baseline_sessions = len(baseline_paces)

    # Pass 3: classify every piece against it. Rows are collected and written in bulk:
    # one upsert per row costs a round trip each, which dominated the time of a UI override.
    classification_rows: list[dict] = []
    eligibility_rows: list[dict] = []
    for w in workouts:
        agg = strokes.get(w.id, {})
        result = classify(replace(features[w.id], interval_threshold_s_500=threshold))
        classification_rows.append(
            {
                "workout_id": w.id,
                "workout_class": result.workout_class,
                "confidence": Decimal(str(result.confidence)),
                "reason": result.reason,
                "classifier_version": CLASSIFIER_VERSION,
                "classified_at": now,
            }
        )

        effective_class = overrides.get(w.id, result.workout_class)
        if w.id in overrides:
            stats.overridden += 1
        elif result.confidence < 0.7:
            stats.low_confidence.append((w.id, result.workout_class, result.confidence, result.reason))
        stats.workouts += 1
        stats.classes[effective_class] += 1

        for e in evaluate(
            EligibilityInputs(
                workout_id=w.id,
                workout_class=effective_class,
                is_continuous=w.rest_time_s == 0,
                work_time_s=w.work_time_s,
                work_distance_m=w.work_distance_m,
                avg_watts=w.avg_watts,
                hr_avg=w.hr_avg,
                stroke_count=w.stroke_count,
                strokes_stored=agg.get("stored") or 0,
                strokes_with_hr=agg.get("with_hr") or 0,
                rest_strokes_with_hr=agg.get("rest_with_hr") or 0,
                interval_hr_pairs=hr_pairs.get(w.id, 0),
                stroke_warning=w.stroke_warning,
            )
        ):
            eligibility_rows.append(
                {
                    "workout_id": w.id,
                    "metric": e.metric,
                    "eligible": e.eligible,
                    "reason": e.reason,
                    "eligibility_version": ELIGIBILITY_VERSION,
                    "computed_at": now,
                }
            )
            if e.eligible:
                stats.eligible[e.metric] += 1

    _bulk_upsert(session, WorkoutClassification, classification_rows, ["workout_id"])
    _bulk_upsert(session, WorkoutEligibility, eligibility_rows, ["workout_id", "metric"])
    session.commit()
    return stats


BULK_CHUNK = 1000


def _bulk_upsert(session: Session, model, rows: list[dict], keys: list[str]) -> None:
    """Multi-row INSERT ... ON CONFLICT DO UPDATE, chunked under Postgres's parameter cap."""
    for start in range(0, len(rows), BULK_CHUNK):
        chunk = rows[start : start + BULK_CHUNK]
        stmt = insert(model).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=keys,
            set_={col: stmt.excluded[col] for col in chunk[0] if col not in keys},
        )
        session.execute(stmt)


def set_override(session: Session, workout_id: int, workout_class: str, note: str | None = None) -> None:
    session.execute(
        insert(ClassificationOverride)
        .values(workout_id=workout_id, workout_class=workout_class, note=note)
        .on_conflict_do_update(
            index_elements=[ClassificationOverride.workout_id],
            set_={"workout_class": workout_class, "note": note, "created_at": literal_column("now()")},
        )
    )
    session.commit()


def clear_override(session: Session, workout_id: int) -> bool:
    """Hand the piece back to the classifier. Returns whether there was an override."""
    removed = session.execute(
        delete(ClassificationOverride).where(ClassificationOverride.workout_id == workout_id)
    ).rowcount
    session.commit()
    return bool(removed)


def set_classification_settings(
    session: Session,
    athlete_id: int,
    steady_pace_s_500: Decimal | None = None,
    interval_margin_s: Decimal | None = None,
    clear_steady_pace: bool = False,
) -> Athlete:
    """Per-athlete classification settings. Rerun classify_all afterwards."""
    athlete = session.get(Athlete, athlete_id)
    if athlete is None:
        raise LookupError(f"athlete {athlete_id} not found")
    if clear_steady_pace:
        athlete.steady_pace_override = None
    elif steady_pace_s_500 is not None:
        if not 60 <= steady_pace_s_500 <= 300:
            raise ValueError("steady pace must be between 1:00 and 5:00 per 500m")
        athlete.steady_pace_override = steady_pace_s_500
    if interval_margin_s is not None:
        if not 0 <= interval_margin_s <= 60:
            raise ValueError("interval margin must be between 0 and 60 seconds")
        athlete.interval_margin_s = interval_margin_s
    session.commit()
    return athlete


def effective_class(session: Session, workout_id: int) -> tuple[str | None, bool]:
    """Returns (class, overridden)."""
    override = session.get(ClassificationOverride, workout_id)
    if override:
        return override.workout_class, True
    row = session.get(WorkoutClassification, workout_id)
    return (row.workout_class if row else None), False


LB_TO_G = Decimal("453.59237")


def set_profile(
    session: Session,
    athlete_id: int,
    max_hr: int | None = None,
    weight_lb: Decimal | None = None,
    resting_hr: int | None = None,
) -> Athlete:
    """Athlete-supplied corrections to the C2 profile. Never overwritten by sync."""
    athlete = session.get(Athlete, athlete_id)
    if athlete is None:
        raise LookupError(f"athlete {athlete_id} not found")
    if max_hr is not None:
        athlete.max_heart_rate_override = max_hr
    if weight_lb is not None:
        athlete.weight_g_override = int(weight_lb * LB_TO_G)
    if resting_hr is not None:
        athlete.resting_hr_override = resting_hr
    session.commit()
    return athlete


def effective_classes(session: Session, athlete_id: int) -> dict[int, str | None]:
    """workout_id -> class, with manual overrides applied."""
    effective = func.coalesce(ClassificationOverride.workout_class, WorkoutClassification.workout_class)
    rows = session.execute(
        select(Workout.id, effective)
        .join(WorkoutClassification, WorkoutClassification.workout_id == Workout.id, isouter=True)
        .join(ClassificationOverride, ClassificationOverride.workout_id == Workout.id, isouter=True)
        .where(Workout.athlete_id == athlete_id)
    ).all()
    return dict(rows)


def reclassify_and_refresh(
    session: Session,
    athlete_id: int,
    before: dict[int, str | None],
    full_metrics: bool = False,
) -> tuple[ClassifyStats, list[int]]:
    """Reclassify, then recompute metrics for every piece whose class moved.

    `before` must be snapshotted with effective_classes() BEFORE the change is written: an
    override is visible the moment it is committed, so a snapshot taken here would already
    include it and the overridden piece would silently keep stale metrics.

    A single override can also move the learned steady baseline (a piece overridden to a
    test stops counting towards it), which can reclassify other pieces, so the diff is taken
    across the whole history rather than assumed to be one workout.
    """
    from erg.metrics_runner import compute_load, compute_workout_metrics

    stats = classify_all(session, athlete_id)
    after = effective_classes(session, athlete_id)
    changed = sorted(wid for wid, cls in after.items() if before.get(wid) != cls)
    if full_metrics:
        compute_workout_metrics(session, athlete_id)
    elif changed:
        compute_workout_metrics(session, athlete_id, changed)
    if full_metrics or changed:
        compute_load(session, athlete_id)
    return stats, changed
