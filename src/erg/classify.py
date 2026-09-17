"""Session classification.

Classes: test_2k | test_6k | test_10k | interval | steady | short_piece | unknown

Intensity comes from HR when it is valid (the most reliable signal), and falls back to
pace relative to the athlete's best 2k when it is not. Every result carries a confidence
and the reason, and any of them can be overridden by hand (classification_override).
"""

from dataclasses import dataclass
from decimal import Decimal

CLASSIFIER_VERSION = 1

TEST_DISTANCES = {2000: "test_2k", 6000: "test_6k", 10000: "test_10k"}
DISTANCE_TOLERANCE = 0.02  # ±2% counts as "that distance"

# Fractions of max HR. Below TEST_HR_FLOOR a piece at a test distance is treated as steady.
TEST_HR_FRACTION = 0.82
STEADY_HR_FRACTION = 0.75

# Pace ceilings for a test at each distance, as a multiple of the athlete's best 2k pace.
# Rowing-standard deltas: 6k ≈ 2k + 8s/500m, 10k ≈ 2k + 15-18s/500m.
TEST_PACE_RATIO = {2000: Decimal("1.06"), 6000: Decimal("1.18"), 10000: Decimal("1.26")}

SHORT_PIECE_S = 600  # under 10 minutes of work is a warm-up, cool-down or short sprint

# Athlete rule: anything averaging slower than 1:55/500m is steady state, whatever its shape.
# A 4x15' or 4x3k at 2:00 is steady work even when HR drifts above 150.
STEADY_PACE_S_500 = Decimal(115)


@dataclass(frozen=True)
class Features:
    workout_id: int
    work_time_s: Decimal
    work_distance_m: int
    rest_time_s: Decimal
    workout_type: str | None
    avg_pace_s_500: Decimal | None  # work-only pace; from strokes when the summary is truncated
    pace_from_strokes: bool = False
    hr_avg: int | None = None  # already validated; None when absent/implausible
    max_heart_rate: int | None = None
    best_2k_pace_s_500: Decimal | None = None
    stroke_interval_count: int = 0  # intervals detected in the stroke stream
    summary_interval_count: int = 0  # rows in interval_split of kind 'interval'


def _mmss(pace: Decimal) -> str:
    total = float(pace)
    return f"{int(total // 60)}:{total % 60:04.1f}"


@dataclass(frozen=True)
class Classification:
    workout_class: str
    confidence: float
    reason: str


def _is_interval(f: Features) -> bool:
    return (
        f.rest_time_s > 0
        or (f.workout_type or "").endswith("Interval")
        or f.summary_interval_count > 1
        or f.stroke_interval_count > 1
    )


def _test_distance(distance_m: int) -> tuple[int, str] | None:
    for target, name in TEST_DISTANCES.items():
        if abs(distance_m - target) <= target * DISTANCE_TOLERANCE:
            return target, name
    return None


def classify(f: Features) -> Classification:
    interval_shaped = _is_interval(f)

    # A 2k test is ~6:30, so test distances are never "short pieces".
    if not interval_shaped and 0 < f.work_time_s < SHORT_PIECE_S and not _test_distance(f.work_distance_m):
        return Classification("short_piece", 0.8, f"continuous, under {SHORT_PIECE_S // 60} min")

    # Pace rules everything: slower than 1:55/500m is steady work, interval-shaped or not.
    if f.avg_pace_s_500 and f.avg_pace_s_500 > STEADY_PACE_S_500:
        suffix = " (pace from strokes)" if f.pace_from_strokes else ""
        shape = "interval-shaped but " if interval_shaped else ""
        return Classification("steady", 0.9, f"{shape}{_mmss(f.avg_pace_s_500)}/500m, slower than 1:55{suffix}")

    if interval_shaped:
        # Faster than 1:55 with rests: hard interval work, any shape from 4x10' to 20x30".
        return Classification("interval", 0.95, "rest periods present, faster than 1:55/500m")

    if f.work_time_s <= 0 or f.work_distance_m <= 0:
        return Classification("unknown", 0.0, "no work time or distance recorded")

    match = _test_distance(f.work_distance_m)
    if match:
        target, name = match
        hr_ceiling = f.max_heart_rate
        if f.hr_avg and hr_ceiling:
            fraction = f.hr_avg / hr_ceiling
            if fraction >= TEST_HR_FRACTION:
                return Classification(name, 0.9, f"{target}m at {fraction:.0%} of max HR")
            if fraction < STEADY_HR_FRACTION:
                return Classification("steady", 0.8, f"{target}m but only {fraction:.0%} of max HR")
            return Classification(name, 0.6, f"{target}m at {fraction:.0%} of max HR (borderline)")

        # No usable HR: fall back to pace relative to the athlete's best 2k.
        if f.avg_pace_s_500 and f.best_2k_pace_s_500:
            ratio = f.avg_pace_s_500 / f.best_2k_pace_s_500
            if ratio <= TEST_PACE_RATIO[target]:
                return Classification(name, 0.6, f"{target}m at {ratio:.2f}x best 2k pace, no HR")
            return Classification("steady", 0.6, f"{target}m but {ratio:.2f}x best 2k pace, no HR")
        return Classification(name, 0.4, f"{target}m, no HR or pace reference")

    if f.hr_avg and f.max_heart_rate:
        fraction = f.hr_avg / f.max_heart_rate
        if fraction >= TEST_HR_FRACTION:
            return Classification(
                "steady", 0.5, f"continuous at {fraction:.0%} of max HR — hard, but not a test distance"
            )
        return Classification("steady", 0.9, f"continuous, {fraction:.0%} of max HR")

    return Classification("steady", 0.7, "continuous, no HR")
