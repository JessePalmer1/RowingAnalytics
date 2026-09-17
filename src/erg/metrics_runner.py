"""Computes and stores derived metrics, daily load and rolling windows. No API calls."""

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date as Date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from erg import metrics
from erg.eligibility import METRICS as METRIC_NAMES
from erg.metrics import METRIC_VERSION, StrokePoint
from erg.models import (
    Athlete,
    DailyLoad,
    IntervalSplit,
    RollingMetric,
    Stroke,
    Workout,
    WorkoutEligibility,
    WorkoutMetric,
)

log = logging.getLogger(__name__)

ACUTE_DAYS = 7
CHRONIC_DAYS = 28


@dataclass
class MetricStats:
    workouts: int = 0
    computed: Counter = field(default_factory=Counter)
    days: int = 0
    rolling_rows: int = 0


def _d(value: float | None, places: str = "0.0001") -> Decimal | None:
    return None if value is None else Decimal(str(round(value, len(places.split(".")[1]))))


def _eligible_map(session: Session, athlete_id: int) -> dict[int, set[str]]:
    rows = session.execute(
        select(WorkoutEligibility.workout_id, WorkoutEligibility.metric)
        .join(Workout, Workout.id == WorkoutEligibility.workout_id)
        .where(Workout.athlete_id == athlete_id, WorkoutEligibility.eligible)
    ).all()
    out: dict[int, set[str]] = {}
    for workout_id, metric in rows:
        out.setdefault(workout_id, set()).add(metric)
    return out


def compute_workout_metrics(session: Session, athlete_id: int) -> MetricStats:
    """One row per workout. Only eligible metrics are computed; the rest stay null."""
    stats = MetricStats()
    athlete = session.get(Athlete, athlete_id)
    max_hr = athlete.effective_max_heart_rate if athlete else None
    resting_hr = (athlete.resting_hr_override if athlete else None) or metrics.TRIMP_RESTING_HR_DEFAULT
    eligible = _eligible_map(session, athlete_id)
    now = datetime.now(timezone.utc)

    for w in session.execute(select(Workout).where(Workout.athlete_id == athlete_id)).scalars():
        allowed = eligible.get(w.id, set())
        points = [
            StrokePoint(
                interval_idx=s.interval_idx,
                t_s=float(s.t_s),
                d_m=float(s.d_m),
                pace_s_500=float(s.pace_s_500) if s.pace_s_500 is not None else None,
                spm=s.spm,
                hr=s.hr,
            )
            for s in session.execute(
                select(Stroke).where(Stroke.workout_id == w.id, Stroke.is_rest.is_(False)).order_by(Stroke.seq)
            ).scalars()
        ]
        samples = metrics.work_samples(points)
        values: dict[str, Decimal | int | None] = {}

        if "ef" in allowed:
            ef, watts, hr = metrics.efficiency_factor(samples)
            if ef is None and w.hr_avg and w.avg_watts:  # no strokes: fall back to session averages
                ef, watts, hr = float(w.avg_watts) / w.hr_avg, float(w.avg_watts), float(w.hr_avg)
            values |= {"ef": _d(ef), "work_watts": _d(watts, "0.1"), "work_hr": _d(hr, "0.1")}

        if "decoupling" in allowed:
            pct, ef1, ef2 = metrics.decoupling_pct(samples)
            values |= {"decoupling_pct": _d(pct, "0.01"), "ef_first_half": _d(ef1), "ef_second_half": _d(ef2)}

        if "pacing" in allowed:
            shape = metrics.pace_shape(samples)
            values |= {
                "pace_cv": _d(shape["pace_cv"]),
                "spm_cv": _d(shape["spm_cv"]),
                "first_half_pace": _d(shape["first_half_pace"], "0.01"),
                "second_half_pace": _d(shape["second_half_pace"], "0.01"),
                "fade_onset_m": int(shape["fade_onset_m"]) if shape["fade_onset_m"] is not None else None,
            }

        if "dps" in allowed:
            values["dps_m"] = _d(metrics.distance_per_stroke(float(w.work_distance_m), w.stroke_count), "0.001")

        if "hrr" in allowed:
            intervals = session.execute(
                select(IntervalSplit.rest_time_s, IntervalSplit.hr_ending, IntervalSplit.hr_rest)
                .where(IntervalSplit.workout_id == w.id, IntervalSplit.kind == "interval")
                .order_by(IntervalSplit.idx)
            ).all()
            drop, rest_s, n = metrics.hr_recovery([(float(r or 0), e, rest) for r, e, rest in intervals])
            values |= {"hrr_bpm": _d(drop, "0.1"), "hrr_rest_s": _d(rest_s, "0.1"), "hrr_intervals": n or None}

        kj = metrics.kilojoules(samples)
        if kj is None and w.avg_watts:
            kj = float(w.avg_watts) * float(w.work_time_s) / 1000
        values["kj"] = _d(kj, "0.1")
        values["trimp"] = _d(
            metrics.trimp(float(w.work_time_s), w.hr_avg, max_hr, resting_hr), "0.01"
        )

        row = {"workout_id": w.id, "computed_at": now, "metric_version": METRIC_VERSION} | values
        session.execute(
            insert(WorkoutMetric)
            .values(**row)
            .on_conflict_do_update(
                index_elements=[WorkoutMetric.workout_id],
                set_={k: v for k, v in row.items() if k != "workout_id"},
            )
        )
        stats.workouts += 1
        for name in METRIC_NAMES:
            key = {"ef": "ef", "decoupling": "decoupling_pct", "pacing": "pace_cv", "dps": "dps_m", "hrr": "hrr_bpm"}[name]
            if values.get(key) is not None:
                stats.computed[name] += 1

    session.commit()
    return stats


def compute_load(session: Session, athlete_id: int) -> MetricStats:
    """Daily aggregates, then rolling windows over them."""
    stats = MetricStats()
    now = datetime.now(timezone.utc)

    rows = session.execute(
        select(Workout.ended_at_local, Workout.work_time_s, Workout.work_distance_m, WorkoutMetric.kj, WorkoutMetric.trimp)
        .join(WorkoutMetric, WorkoutMetric.workout_id == Workout.id, isouter=True)
        .where(Workout.athlete_id == athlete_id)
    ).all()

    # kj/trimp stay null for a day where no session could produce them, rather than
    # reading as a genuine zero.
    daily: dict[Date, dict[str, Decimal | int | None]] = {}
    for ended_at, work_time, distance, kj, trimp_value in rows:
        day = daily.setdefault(
            ended_at.date(),
            {"sessions": 0, "work_time_s": Decimal(0), "work_distance_m": 0, "kj": None, "trimp": None},
        )
        day["sessions"] += 1
        day["work_time_s"] += work_time
        day["work_distance_m"] += distance
        for key, value in (("kj", kj), ("trimp", trimp_value)):
            if value is not None:
                day[key] = (day[key] or Decimal(0)) + value

    session.execute(delete(DailyLoad).where(DailyLoad.athlete_id == athlete_id))
    if daily:
        session.execute(
            insert(DailyLoad),
            [{"athlete_id": athlete_id, "date": day, "computed_at": now, **vals} for day, vals in daily.items()],
        )
    stats.days = len(daily)

    # Rolling windows run over calendar days, so rest days count as zero load.
    session.execute(delete(RollingMetric).where(RollingMetric.athlete_id == athlete_id))
    rolling: list[dict] = []
    if daily:
        start, end = min(daily), max(daily)
        kj_by_day = {day: float(vals["kj"] or 0) for day, vals in daily.items()}
        for offset in range((end - start).days + 1):
            day = start + timedelta(days=offset)
            acute_days = [kj_by_day.get(day - timedelta(days=i), 0.0) for i in range(ACUTE_DAYS)]
            chronic_days = [kj_by_day.get(day - timedelta(days=i), 0.0) for i in range(CHRONIC_DAYS)]
            acute = sum(acute_days) / ACUTE_DAYS
            chronic = sum(chronic_days) / CHRONIC_DAYS
            for name, window, value in (
                ("kj", ACUTE_DAYS, acute),
                ("kj", CHRONIC_DAYS, chronic),
                ("acwr", CHRONIC_DAYS, metrics.acwr(acute, chronic)),
                ("monotony", ACUTE_DAYS, metrics.monotony(acute_days)),
            ):
                rolling.append(
                    {
                        "athlete_id": athlete_id,
                        "date": day,
                        "metric_name": name,
                        "window_days": window,
                        "value": _d(value, "0.0001"),
                        "computed_at": now,
                    }
                )
        session.execute(insert(RollingMetric), rolling)
    stats.rolling_rows = len(rolling)
    session.commit()
    return stats
