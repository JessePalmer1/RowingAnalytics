"""Distance-aligned comparison of pieces: the server side of ghost racing.

Each piece is resampled onto a common distance grid so the frontend can overlay them
directly, and split attribution says where the time difference actually came from.
"""

from dataclasses import dataclass

from erg.metrics import WorkSample

DEFAULT_POINTS = 300
DEFAULT_SEGMENT_M = 500


@dataclass(frozen=True)
class Track:
    """A piece as cumulative time and per-stroke values against elapsed distance."""

    distance_m: list[float]
    time_s: list[float]
    pace_s_500: list[float | None]
    hr: list[int | None]
    spm: list[int | None]
    dps_m: list[float | None]


def track_from_samples(samples: list[WorkSample]) -> Track:
    distance, time, pace, hr, spm, dps = [0.0], [0.0], [None], [None], [None], [None]
    elapsed_t = 0.0
    for s in samples:
        elapsed_t += s.dt_s
        distance.append(s.elapsed_d_m)
        time.append(elapsed_t)
        pace.append(s.pace_s_500)
        hr.append(s.hr)
        spm.append(s.spm)
        dps.append(s.distance_m)
    return Track(distance, time, pace, hr, spm, dps)


def _interpolate(xs: list[float], ys: list[float | None], x: float) -> float | None:
    """Linear interpolation at x, carrying gaps through as None."""
    if not xs or x < xs[0] or x > xs[-1]:
        return None
    lo, hi = 0, len(xs) - 1
    while lo < hi - 1:  # binary search for the bracketing pair
        mid = (lo + hi) // 2
        if xs[mid] <= x:
            lo = mid
        else:
            hi = mid
    y0, y1 = ys[lo], ys[hi]
    if y0 is None:
        return y1
    if y1 is None:
        return y0
    span = xs[hi] - xs[lo]
    if span <= 0:
        return y1
    weight = (x - xs[lo]) / span
    return y0 + (y1 - y0) * weight


def resample(track: Track, grid: list[float]) -> dict[str, list[float | None]]:
    return {
        "distance_m": grid,
        "time_s": [_interpolate(track.distance_m, track.time_s, d) for d in grid],
        "pace_s_500": [_interpolate(track.distance_m, track.pace_s_500, d) for d in grid],
        "hr": [_interpolate(track.distance_m, track.hr, d) for d in grid],
        "spm": [_interpolate(track.distance_m, track.spm, d) for d in grid],
        "dps_m": [_interpolate(track.distance_m, track.dps_m, d) for d in grid],
    }


def common_grid(tracks: list[Track], points: int = DEFAULT_POINTS) -> list[float]:
    """Grid over the distance every piece covers, so nothing is extrapolated."""
    shortest = min((t.distance_m[-1] for t in tracks if len(t.distance_m) > 1), default=0.0)
    if shortest <= 0:
        return []
    step = shortest / (points - 1)
    return [round(i * step, 2) for i in range(points)]


def split_attribution(
    reference: Track, other: Track, total_m: float, segment_m: float = DEFAULT_SEGMENT_M
) -> list[dict]:
    """Where the time difference came from, segment by segment.

    Positive `delta_s` means `other` lost time to `reference` over that segment.
    """
    if total_m <= 0:
        return []
    segments = []
    start = 0.0
    while start < total_m - 1:
        end = min(start + segment_m, total_m)
        ref_split = _elapsed(reference, start, end)
        other_split = _elapsed(other, start, end)
        segments.append(
            {
                "from_m": round(start),
                "to_m": round(end),
                "reference_s": ref_split,
                "other_s": other_split,
                "delta_s": None if ref_split is None or other_split is None else round(other_split - ref_split, 2),
            }
        )
        start = end
    return segments


def _elapsed(track: Track, start_m: float, end_m: float) -> float | None:
    t0 = _interpolate(track.distance_m, track.time_s, start_m)
    t1 = _interpolate(track.distance_m, track.time_s, end_m)
    if t0 is None or t1 is None:
        return None
    return round(t1 - t0, 2)
