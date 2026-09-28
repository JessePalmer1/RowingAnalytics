"""Session classification.

Classes: test_2k | test_6k | test_10k | interval | steady | short_piece | unknown

Rules, in order:
  1. A solo (no rest) piece at 2k, 6k or 10k is a test, whatever the pace.
  2. A solo piece under 10 minutes is a short piece (warm-up, cool-down, sprint).
  3. Everything else is judged against the athlete's own steady-state pace. Rowing more
     than a margin (default 10s/500m) faster than it is interval work (UT1 up to
     anaerobic threshold and above), whether the session was continuous or broken up.
     Otherwise it is steady.

The steady baseline is personal: the mean of the athlete's slower sessions after
dropping outlier paddles. It can be overridden per athlete, and any single piece can
be overridden by hand (classification_override).
"""

import statistics
from dataclasses import dataclass
from decimal import Decimal

CLASSIFIER_VERSION = 2

TEST_DISTANCES = {2000: "test_2k", 6000: "test_6k", 10000: "test_10k"}
CLASSES = ("test_2k", "test_6k", "test_10k", "interval", "steady", "short_piece", "unknown")
DISTANCE_TOLERANCE = 0.02  # ±2% counts as "that distance"

SHORT_PIECE_S = 600  # under 10 minutes of solo work

DEFAULT_INTERVAL_MARGIN_S = Decimal(10)
BASELINE_MIN_WORK_S = 600  # sessions shorter than this don't say much about steady pace
BASELINE_MIN_SESSIONS = 5
BASELINE_SLOW_FRACTION = 0.5  # the slower half of sessions defines steady state


@dataclass(frozen=True)
class Features:
    workout_id: int
    work_time_s: Decimal
    work_distance_m: int
    rest_time_s: Decimal
    workout_type: str | None
    avg_pace_s_500: Decimal | None  # work-only pace; from strokes when the summary is truncated
    pace_from_strokes: bool = False
    stroke_interval_count: int = 0  # intervals detected in the stroke stream
    summary_interval_count: int = 0  # rows in interval_split of kind 'interval'
    interval_threshold_s_500: Decimal | None = None  # faster than this is interval work


@dataclass(frozen=True)
class Classification:
    workout_class: str
    confidence: float
    reason: str


def mmss(pace: Decimal | float) -> str:
    total = float(pace)
    return f"{int(total // 60)}:{total % 60:04.1f}"


def is_solo(f: Features) -> bool:
    """One continuous piece: no rest, no interval structure."""
    return not (
        f.rest_time_s > 0
        or (f.workout_type or "").endswith("Interval")
        or f.summary_interval_count > 1
        or f.stroke_interval_count > 1
    )


def match_test_distance(distance_m: int) -> tuple[int, str] | None:
    for target, name in TEST_DISTANCES.items():
        if abs(distance_m - target) <= target * DISTANCE_TOLERANCE:
            return target, name
    return None


def steady_baseline(paces: list[Decimal]) -> Decimal | None:
    """The athlete's steady-state pace: mean of their slower sessions, outliers removed.

    `paces` are work paces of non-test sessions of at least 10 minutes. Very slow outliers
    (paddles, cool-downs) sit above the upper Tukey fence and are dropped first, so they
    can't drag the baseline slower.
    """
    if len(paces) < BASELINE_MIN_SESSIONS:
        return None
    ordered = sorted(float(p) for p in paces)
    q1, _, q3 = statistics.quantiles(ordered, n=4)
    fence = q3 + 1.5 * (q3 - q1)
    kept = [p for p in ordered if p <= fence]
    slow = kept[int(len(kept) * (1 - BASELINE_SLOW_FRACTION)) :]
    if not slow:
        return None
    return Decimal(str(round(statistics.fmean(slow), 2)))


def classify(f: Features) -> Classification:
    solo = is_solo(f)

    # 1. Tests are defined by distance alone.
    if solo and (match := match_test_distance(f.work_distance_m)):
        target, name = match
        return Classification(name, 0.95, f"solo {target}m")

    # 2. Short solo pieces.
    if solo and 0 < f.work_time_s < SHORT_PIECE_S:
        return Classification("short_piece", 0.8, f"solo, under {SHORT_PIECE_S // 60} min")

    shape = "solo" if solo else "broken into intervals"
    suffix = " (pace from strokes)" if f.pace_from_strokes else ""

    if f.avg_pace_s_500 is None:
        if not solo:
            return Classification("interval", 0.5, "rest periods present, no pace to judge intensity")
        return Classification("unknown", 0.0, "no work time or distance recorded")

    if f.interval_threshold_s_500 is None:
        # Not enough history to know this athlete's steady pace yet.
        if not solo:
            return Classification("interval", 0.5, f"{shape}, steady baseline not established yet")
        return Classification("steady", 0.5, f"{shape}, steady baseline not established yet")

    # 3. Intensity against the athlete's own steady pace.
    pace = f.avg_pace_s_500
    threshold = f.interval_threshold_s_500
    if pace < threshold:
        return Classification(
            "interval", 0.9, f"{shape}, {mmss(pace)}/500m is faster than the {mmss(threshold)} threshold{suffix}"
        )
    return Classification(
        "steady", 0.9, f"{shape}, {mmss(pace)}/500m is at or slower than the {mmss(threshold)} threshold{suffix}"
    )
