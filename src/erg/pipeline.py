"""Runs classification and eligibility over stored workouts. No API calls."""

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import Integer, case, func, literal_column, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from erg.classify import CLASSIFIER_VERSION, Features, classify
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


def best_2k_pace(session: Session, athlete_id: int) -> Decimal | None:
    """Fastest continuous 2k, used as the pace yardstick when HR is missing."""
    return session.execute(
        select(func.min(Workout.avg_pace_s_500)).where(
            Workout.athlete_id == athlete_id,
            Workout.rest_time_s == 0,
            Workout.work_distance_m.between(1960, 2040),
        )
    ).scalar()


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
    max_hr = athlete.effective_max_heart_rate if athlete else None
    reference_pace = best_2k_pace(session, athlete_id)
    strokes = _stroke_aggregates(session, athlete_id)
    stroke_totals = _stroke_work_totals(session, athlete_id)
    summary_intervals = _summary_interval_counts(session, athlete_id)
    hr_pairs = _interval_hr_pairs(session, athlete_id)
    overrides = dict(
        session.execute(
            select(ClassificationOverride.workout_id, ClassificationOverride.workout_class)
        ).all()
    )
    now = datetime.now(timezone.utc)

    for w in session.execute(select(Workout).where(Workout.athlete_id == athlete_id)).scalars():
        agg = strokes.get(w.id, {})
        # The C2 summary is truncated on some workouts (plan §1.7); fall back to stroke totals.
        pace, pace_from_strokes = w.avg_pace_s_500, False
        if pace is None:
            d, t = stroke_totals.get(w.id, (None, None))
            if d and t and d > 0:
                pace, pace_from_strokes = t * 500 / d, True
        result = classify(
            Features(
                workout_id=w.id,
                work_time_s=w.work_time_s,
                work_distance_m=w.work_distance_m,
                rest_time_s=w.rest_time_s,
                workout_type=w.workout_type,
                avg_pace_s_500=pace,
                pace_from_strokes=pace_from_strokes,
                hr_avg=w.hr_avg,
                max_heart_rate=max_hr,
                best_2k_pace_s_500=reference_pace,
                stroke_interval_count=agg.get("intervals") or 0,
                summary_interval_count=summary_intervals.get(w.id, 0),
            )
        )
        session.execute(
            insert(WorkoutClassification)
            .values(
                workout_id=w.id,
                workout_class=result.workout_class,
                confidence=Decimal(str(result.confidence)),
                reason=result.reason,
                classifier_version=CLASSIFIER_VERSION,
                classified_at=now,
            )
            .on_conflict_do_update(
                index_elements=[WorkoutClassification.workout_id],
                set_={
                    "workout_class": result.workout_class,
                    "confidence": Decimal(str(result.confidence)),
                    "reason": result.reason,
                    "classifier_version": CLASSIFIER_VERSION,
                    "classified_at": now,
                },
            )
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
            session.execute(
                insert(WorkoutEligibility)
                .values(
                    workout_id=w.id,
                    metric=e.metric,
                    eligible=e.eligible,
                    reason=e.reason,
                    eligibility_version=ELIGIBILITY_VERSION,
                    computed_at=now,
                )
                .on_conflict_do_update(
                    index_elements=[WorkoutEligibility.workout_id, WorkoutEligibility.metric],
                    set_={
                        "eligible": e.eligible,
                        "reason": e.reason,
                        "eligibility_version": ELIGIBILITY_VERSION,
                        "computed_at": now,
                    },
                )
            )
            if e.eligible:
                stats.eligible[e.metric] += 1

    session.commit()
    return stats


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
