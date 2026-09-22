"""Derived metrics. Pure functions over stroke series and interval summaries.

Everything here works on the WORK portion only: rest strokes are excluded by the caller,
because session averages that include rest make EF and decoupling meaningless.
"""

import math
import statistics
from dataclasses import dataclass
from decimal import Decimal

from erg.normalize import watts_from_pace

METRIC_VERSION = 1

MAX_STROKE_GAP_S = 10.0  # a longer gap is a pause, not a stroke interval
FADE_WINDOW_M = 200.0
FADE_THRESHOLD = 1.03  # 3% slower than the best window
TRIMP_RESTING_HR_DEFAULT = 60  # assumption; override per athlete when known


@dataclass(frozen=True)
class StrokePoint:
    interval_idx: int
    t_s: float
    d_m: float
    pace_s_500: float | None
    spm: int | None
    hr: int | None


@dataclass
class WorkSample:
    """One stroke's contribution, weighted by how long it lasted."""

    dt_s: float
    distance_m: float
    watts: float | None
    hr: int | None
    pace_s_500: float | None
    spm: int | None
    elapsed_d_m: float


def work_samples(points: list[StrokePoint]) -> list[WorkSample]:
    """Turn work strokes into time-weighted samples, handling per-interval t/d resets."""
    samples: list[WorkSample] = []
    prev_t = prev_d = 0.0
    current_idx = None
    elapsed_d = 0.0
    for p in points:
        if p.interval_idx != current_idx:
            current_idx, prev_t, prev_d = p.interval_idx, 0.0, 0.0
        dt = p.t_s - prev_t
        dd = p.d_m - prev_d
        prev_t, prev_d = max(prev_t, p.t_s), max(prev_d, p.d_m)
        if dt <= 0 or dt > MAX_STROKE_GAP_S or dd < 0:
            continue  # jitter or a pause: no usable interval
        elapsed_d += dd
        samples.append(
            WorkSample(
                dt_s=dt,
                distance_m=dd,
                watts=float(watts_from_pace(Decimal(str(p.pace_s_500)))) if p.pace_s_500 else None,
                hr=p.hr,
                pace_s_500=p.pace_s_500,
                spm=p.spm,
                elapsed_d_m=elapsed_d,
            )
        )
    return samples


def _weighted_mean(values: list[tuple[float, float]]) -> float | None:
    """values: (value, weight)."""
    total_w = sum(w for _, w in values)
    if total_w <= 0:
        return None
    return sum(v * w for v, w in values) / total_w


def efficiency_factor(samples: list[WorkSample]) -> tuple[float | None, float | None, float | None]:
    """EF = average watts / average HR over the work portion. Returns (ef, watts, hr)."""
    usable = [s for s in samples if s.watts is not None and s.hr]
    if not usable:
        return None, None, None
    watts = _weighted_mean([(s.watts, s.dt_s) for s in usable])
    hr = _weighted_mean([(float(s.hr), s.dt_s) for s in usable])
    if not watts or not hr:
        return None, None, None
    return watts / hr, watts, hr


def decoupling_pct(samples: list[WorkSample]) -> tuple[float | None, float | None, float | None]:
    """(EF_first_half - EF_second_half) / EF_first_half * 100, split by work time."""
    usable = [s for s in samples if s.watts is not None and s.hr]
    total = sum(s.dt_s for s in usable)
    if total <= 0:
        return None, None, None

    half, run = total / 2, 0.0
    first: list[WorkSample] = []
    second: list[WorkSample] = []
    for s in usable:
        (first if run < half else second).append(s)
        run += s.dt_s
    ef1, _, _ = efficiency_factor(first)
    ef2, _, _ = efficiency_factor(second)
    if not ef1 or not ef2:
        return None, ef1, ef2
    return (ef1 - ef2) / ef1 * 100, ef1, ef2


def pace_shape(samples: list[WorkSample]) -> dict[str, float | None]:
    """Pace/rate variability, split halves by distance, and where the piece started to fade."""
    paced = [s for s in samples if s.pace_s_500]
    out: dict[str, float | None] = {
        "pace_cv": None,
        "spm_cv": None,
        "first_half_pace": None,
        "second_half_pace": None,
        "fade_onset_m": None,
    }
    if len(paced) < 4:
        return out

    paces = [s.pace_s_500 for s in paced]
    out["pace_cv"] = statistics.stdev(paces) / statistics.fmean(paces)
    rates = [float(s.spm) for s in paced if s.spm]
    if len(rates) >= 4 and statistics.fmean(rates) > 0:
        out["spm_cv"] = statistics.stdev(rates) / statistics.fmean(rates)

    total_d = paced[-1].elapsed_d_m
    mid = total_d / 2
    first = [s for s in paced if s.elapsed_d_m <= mid]
    second = [s for s in paced if s.elapsed_d_m > mid]
    if first and second:
        out["first_half_pace"] = _weighted_mean([(s.pace_s_500, s.dt_s) for s in first])
        out["second_half_pace"] = _weighted_mean([(s.pace_s_500, s.dt_s) for s in second])

    out["fade_onset_m"] = _fade_onset(paced, total_d)
    return out


def _fade_onset(paced: list[WorkSample], total_d: float) -> float | None:
    """First distance where a rolling window is >3% slower than the best window and never recovers."""
    if total_d < FADE_WINDOW_M * 3:
        return None
    windows: list[tuple[float, float]] = []  # (window start, mean pace)
    start = 0.0
    while start + FADE_WINDOW_M <= total_d:
        chunk = [s for s in paced if start <= s.elapsed_d_m < start + FADE_WINDOW_M]
        if chunk:
            mean = _weighted_mean([(s.pace_s_500, s.dt_s) for s in chunk])
            if mean:
                windows.append((start, mean))
        start += FADE_WINDOW_M
    if len(windows) < 3:
        return None

    best = min(pace for _, pace in windows)
    limit = best * FADE_THRESHOLD
    onset = None
    for i, (start_m, pace) in enumerate(windows):
        if pace > limit:
            if onset is None:
                onset = start_m
            if all(p > limit for _, p in windows[i:]):
                return onset
        else:
            onset = None
    return None


def hr_recovery(intervals: list[tuple[float, int | None, int | None]]) -> tuple[float | None, float | None, int]:
    """HR drop across interval rests, from the C2 per-interval summary.

    intervals: (rest_time_s, hr_ending, hr_rest). Stroke data almost never samples far
    enough into rest to measure this directly (plan §5.3 assumed it would).

    Recovery depends strongly on how long the rest was, so the rest duration is returned
    alongside the drop: only compare sessions at matched rest length.
    """
    usable = [(rest_s, end - rest) for rest_s, end, rest in intervals if rest_s and end and rest]
    if not usable:
        return None, None, 0
    # Use the rest length that dominates the session.
    durations = statistics.multimode([rest_s for rest_s, _ in usable])
    rest_s = max(durations)
    drops = [drop for d, drop in usable if d == rest_s]
    return statistics.median(drops), rest_s, len(drops)


def distance_per_stroke(distance_m: float, stroke_count: int | None) -> float | None:
    """Fallback when there are no strokes. C2's stroke_count includes rest strokes, so on
    interval sessions this understates DPS by a few percent; prefer distance_per_stroke_from_samples."""
    if not stroke_count or distance_m <= 0:
        return None
    return distance_m / stroke_count


def stroke_length(samples: list[WorkSample]) -> tuple[float | None, float | None]:
    """Mean and coefficient of variation of per-stroke distance over the work portion."""
    lengths = [s.distance_m for s in samples if s.distance_m > 0]
    if len(lengths) < 4:
        return None, None
    mean = statistics.fmean(lengths)
    if mean <= 0:
        return None, None
    return mean, statistics.stdev(lengths) / mean


def trimp(duration_s: float, hr_avg: int | None, hr_max: int | None, hr_rest: int = TRIMP_RESTING_HR_DEFAULT) -> float | None:
    """Banister TRIMP (male coefficients). Needs a valid average HR."""
    if not hr_avg or not hr_max or hr_max <= hr_rest:
        return None
    reserve = (hr_avg - hr_rest) / (hr_max - hr_rest)
    if reserve <= 0:
        return None
    return duration_s / 60 * reserve * 0.64 * math.exp(1.92 * reserve)


def kilojoules(samples: list[WorkSample]) -> float | None:
    """Work done: watts x seconds / 1000."""
    usable = [s for s in samples if s.watts is not None]
    if not usable:
        return None
    return sum(s.watts * s.dt_s for s in usable) / 1000


def acwr(acute: float, chronic: float) -> float | None:
    """7-day load over 28-day load. Descriptive load balance only."""
    if chronic <= 0:
        return None
    return acute / chronic


def monotony(daily_loads: list[float]) -> float | None:
    """Mean daily load / stdev of daily load over the window. Needs variation to be defined."""
    if len(daily_loads) < 2:
        return None
    spread = statistics.stdev(daily_loads)
    if spread == 0:
        return None
    return statistics.fmean(daily_loads) / spread
