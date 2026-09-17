"""Per-metric eligibility.

Every workout gets a row per metric saying eligible yes/no and, when no, why.
Nothing is silently dropped: a metric that cannot be computed must say so.
"""

from dataclasses import dataclass
from decimal import Decimal

ELIGIBILITY_VERSION = 1

EF_MIN_WORK_S = 900  # 15 min
DECOUPLING_MIN_WORK_S = 1200  # under ~20 min the number is noise (plan §5.2)
STROKE_HR_COVERAGE = 0.8
PACING_MIN_STROKES = 30

METRICS = ("ef", "decoupling", "hrr", "pacing", "dps")


@dataclass(frozen=True)
class EligibilityInputs:
    workout_id: int
    workout_class: str
    is_continuous: bool  # no rest periods
    work_time_s: Decimal
    work_distance_m: int
    avg_watts: Decimal | None
    hr_avg: int | None
    stroke_count: int | None
    strokes_stored: int
    strokes_with_hr: int
    rest_strokes_with_hr: int
    stroke_warning: str | None
    interval_hr_pairs: int = 0  # intervals with both ending and rest HR in the C2 summary


@dataclass(frozen=True)
class Eligibility:
    metric: str
    eligible: bool
    reason: str | None  # why not, when ineligible


def _ef(i: EligibilityInputs) -> str | None:
    if i.workout_class != "steady":
        return f"not a steady piece ({i.workout_class})"
    if i.work_time_s < EF_MIN_WORK_S:
        return f"under {EF_MIN_WORK_S // 60} min of work"
    if i.avg_watts is None:
        return "no watts"
    if i.is_continuous:
        if i.hr_avg is None:
            return "no valid average HR"
        return None
    # Interval-shaped steady work: session averages include rest, so EF must come from
    # work strokes only, which needs stroke HR.
    if not i.strokes_stored:
        return "interval-shaped steady session with no stroke data"
    if i.strokes_with_hr < i.strokes_stored * STROKE_HR_COVERAGE:
        return f"interval-shaped steady session, stroke HR covers only {i.strokes_with_hr / i.strokes_stored:.0%}"
    return None


def _decoupling(i: EligibilityInputs) -> str | None:
    if reason := _ef(i):
        return reason
    if not i.is_continuous:
        return "interval-shaped session — decoupling needs one continuous piece"
    if i.work_time_s < DECOUPLING_MIN_WORK_S:
        return f"under {DECOUPLING_MIN_WORK_S // 60} min — decoupling is not meaningful"
    if not i.strokes_stored:
        return "no stroke data to split into halves"
    if i.strokes_with_hr < i.strokes_stored * STROKE_HR_COVERAGE:
        return f"stroke HR covers only {i.strokes_with_hr / i.strokes_stored:.0%} of the piece"
    return None


def _hrr(i: EligibilityInputs) -> str | None:
    """HR recovery comes from the C2 per-interval summary (ending HR vs rest HR).

    Stroke data almost never samples far enough into a rest to measure this, so the
    summary is the only usable source. Any session with rest periods qualifies, including
    interval-shaped steady work.
    """
    if i.is_continuous:
        return f"continuous piece ({i.workout_class}) — no rest periods to recover across"
    if not i.interval_hr_pairs:
        return "no ending/rest HR pair in the interval summary"
    return None


def _pacing(i: EligibilityInputs) -> str | None:
    if i.workout_class in ("unknown", "short_piece"):
        return f"not a ratable piece ({i.workout_class})"
    if i.strokes_stored < PACING_MIN_STROKES:
        return f"only {i.strokes_stored} strokes stored"
    if i.stroke_warning:
        return f"stroke parse warning: {i.stroke_warning}"
    return None


def _dps(i: EligibilityInputs) -> str | None:
    if not i.stroke_count:
        return "no stroke count"
    if i.work_distance_m <= 0:
        return "no work distance"
    return None


RULES = {"ef": _ef, "decoupling": _decoupling, "hrr": _hrr, "pacing": _pacing, "dps": _dps}


def evaluate(i: EligibilityInputs) -> list[Eligibility]:
    out = []
    for metric in METRICS:
        reason = RULES[metric](i)
        out.append(Eligibility(metric, reason is None, reason))
    return out
