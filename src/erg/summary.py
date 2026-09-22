"""Weekly summary: the payload the trend digest is built from.

Descriptive only. It reports what happened and how it compares to recent weeks; it makes
no readiness judgements and no training recommendations.
"""

import statistics
from datetime import date as Date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from erg.models import (
    ClassificationOverride,
    DailyLoad,
    RollingMetric,
    Workout,
    WorkoutClassification,
    WorkoutMetric,
)

BASELINE_WEEKS = 4


def week_bounds(day: Date) -> tuple[Date, Date]:
    """Monday-to-Sunday week containing `day`."""
    start = day - timedelta(days=day.weekday())
    return start, start + timedelta(days=6)


def _f(value) -> float | None:
    return float(value) if value is not None else None


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _rows(session: Session, athlete_id: int, start: Date, end: Date):
    effective = func.coalesce(ClassificationOverride.workout_class, WorkoutClassification.workout_class)
    return session.execute(
        select(Workout, WorkoutMetric, effective.label("workout_class"))
        .join(WorkoutMetric, WorkoutMetric.workout_id == Workout.id, isouter=True)
        .join(WorkoutClassification, WorkoutClassification.workout_id == Workout.id, isouter=True)
        .join(ClassificationOverride, ClassificationOverride.workout_id == Workout.id, isouter=True)
        .where(
            Workout.athlete_id == athlete_id,
            func.date(Workout.ended_at_local) >= start,
            func.date(Workout.ended_at_local) <= end,
        )
        .order_by(Workout.ended_at_local)
    ).all()


def _totals(session: Session, athlete_id: int, start: Date, end: Date) -> dict:
    row = session.execute(
        select(
            func.count(DailyLoad.date),
            func.sum(DailyLoad.sessions),
            func.sum(DailyLoad.work_time_s),
            func.sum(DailyLoad.work_distance_m),
            func.sum(DailyLoad.kj),
            func.sum(DailyLoad.trimp),
        ).where(DailyLoad.athlete_id == athlete_id, DailyLoad.date >= start, DailyLoad.date <= end)
    ).one()
    days, sessions, time_s, distance, kj, trimp = row
    return {
        "days_trained": days or 0,
        "sessions": sessions or 0,
        "work_time_s": _f(time_s) or 0.0,
        "work_distance_m": distance or 0,
        "kj": _f(kj),
        "trimp": _f(trimp),
    }


def week_summary(session: Session, athlete_id: int, day: Date | None = None) -> dict:
    if day is None:
        day = session.execute(
            select(func.max(func.date(Workout.ended_at_local))).where(Workout.athlete_id == athlete_id)
        ).scalar()
        if day is None:
            return {"week_start": None, "week_end": None, "sessions": 0}
    start, end = week_bounds(day)

    rows = _rows(session, athlete_id, start, end)
    totals = _totals(session, athlete_id, start, end)

    by_class: dict[str, dict] = {}
    ef_values, decoupling, hrr, pieces = [], [], [], []
    for w, m, workout_class in rows:
        name = workout_class or "unclassified"
        bucket = by_class.setdefault(name, {"sessions": 0, "work_distance_m": 0, "work_time_s": 0.0})
        bucket["sessions"] += 1
        bucket["work_distance_m"] += w.work_distance_m
        bucket["work_time_s"] += float(w.work_time_s)

        piece = {
            "workout_id": w.id,
            "date": w.ended_at_local.date(),
            "class": name,
            "work_distance_m": w.work_distance_m,
            "work_time_s": float(w.work_time_s),
            "avg_pace_s_500": _f(w.avg_pace_s_500),
            "hr_avg": w.hr_avg,
            "drag_factor": w.drag_factor,
            "ef": _f(m.ef) if m else None,
        }
        pieces.append(piece)
        if m is None:
            continue
        if m.ef is not None:
            ef_values.append(float(m.ef))
        if m.decoupling_pct is not None:
            decoupling.append({"workout_id": w.id, "date": w.ended_at_local.date(), "pct": float(m.decoupling_pct)})
        if m.hrr_bpm is not None:
            hrr.append(
                {
                    "workout_id": w.id,
                    "date": w.ended_at_local.date(),
                    "bpm": float(m.hrr_bpm),
                    "rest_s": _f(m.hrr_rest_s),
                    "intervals": m.hrr_intervals,
                }
            )

    # Baselines: the previous week, and the mean of the weeks before this one.
    prev_start, prev_end = week_bounds(start - timedelta(days=1))
    previous = _totals(session, athlete_id, prev_start, prev_end)
    baseline_start = start - timedelta(weeks=BASELINE_WEEKS)
    baseline_rows = _rows(session, athlete_id, baseline_start, start - timedelta(days=1))
    baseline_ef = [float(m.ef) for _, m, _ in baseline_rows if m and m.ef is not None]
    baseline_totals = _totals(session, athlete_id, baseline_start, start - timedelta(days=1))

    # Rolling rows stop at the last day with data, so use the latest day in the week that has them.
    rolling_date = session.execute(
        select(func.max(RollingMetric.date)).where(
            RollingMetric.athlete_id == athlete_id, RollingMetric.date <= end
        )
    ).scalar()
    rolling = {
        r.metric_name if r.metric_name != "kj" else f"kj_{r.window_days}d": _f(r.value)
        for r in session.execute(
            select(RollingMetric).where(RollingMetric.athlete_id == athlete_id, RollingMetric.date == rolling_date)
        ).scalars()
    }

    ef_mean = _mean(ef_values)
    ef_baseline = _mean(baseline_ef)
    return {
        "week_start": start,
        "week_end": end,
        "totals": totals,
        "by_class": by_class,
        "pieces": pieces,
        "ef": {
            "sessions": len(ef_values),
            "mean": ef_mean,
            "baseline_mean": ef_baseline,
            "baseline_weeks": BASELINE_WEEKS,
            "change_pct": (
                (ef_mean - ef_baseline) / ef_baseline * 100 if ef_mean is not None and ef_baseline else None
            ),
        },
        "decoupling": decoupling,
        "hr_recovery": hrr,
        "load": {
            "as_of": rolling_date,
            "kj_7d": rolling.get("kj_7d"),
            "kj_28d": rolling.get("kj_28d"),
            "acwr": rolling.get("acwr"),
            "monotony": rolling.get("monotony"),
        },
        "previous_week": previous,
        "baseline_per_week": {
            "work_distance_m": (
                baseline_totals["work_distance_m"] / BASELINE_WEEKS if baseline_totals["work_distance_m"] else 0
            ),
            "sessions": baseline_totals["sessions"] / BASELINE_WEEKS if baseline_totals["sessions"] else 0,
            "kj": baseline_totals["kj"] / BASELINE_WEEKS if baseline_totals["kj"] else None,
        },
        "data_quality": {
            "sessions_without_hr": sum(1 for w, _, _ in rows if w.hr_quality != "valid"),
            "sessions_with_stroke_warnings": sum(1 for w, _, _ in rows if w.stroke_warning),
        },
    }
