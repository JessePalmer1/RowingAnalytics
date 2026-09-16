"""Parse Concept2 stroke arrays and shape them for charts.

Stroke quirks this handles (see plan §1.7):
  - t/d reset at each interval, and interval streams include rest-period strokes
  - t jitters backwards near interval ends while d keeps rising, so a boundary
    requires t AND d to both decrease
  - p=0 / spm=0 on the first strokes and hr=0 without a strap mean "no value"
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from erg.normalize import STROKE_HR_MAX, STROKE_HR_MIN, validate_hr, watts_from_pace


@dataclass
class ParsedStrokes:
    rows: list[dict[str, Any]] = field(default_factory=list)
    interval_count: int = 0
    warnings: list[str] = field(default_factory=list)


def parse_strokes(
    workout_id: int, raw_strokes: list[dict[str, Any]], interval_work_times_s: list[Decimal] | None
) -> ParsedStrokes:
    """`interval_work_times_s` is per-interval work time for interval workouts, or None for splits/JustRow."""
    result = ParsedStrokes()
    if not raw_strokes:
        return result

    interval_idx = 0
    prev = None
    for seq, s in enumerate(raw_strokes):
        t, d = s.get("t") or 0, s.get("d") or 0
        if prev is not None and t < (prev.get("t") or 0) and d < (prev.get("d") or 0):
            interval_idx += 1
        prev = s

        t_s = Decimal(t) / 10
        is_rest = False
        if interval_work_times_s is not None and interval_idx < len(interval_work_times_s):
            is_rest = t_s > interval_work_times_s[interval_idx]

        p = s.get("p") or 0
        result.rows.append(
            {
                "workout_id": workout_id,
                "interval_idx": interval_idx,
                "seq": seq,
                "t_s": t_s,
                "d_m": Decimal(d) / 10,
                "pace_s_500": Decimal(p) / 10 if p > 0 else None,
                "spm": s.get("spm") or None,
                "hr": validate_hr(s.get("hr"), lo=STROKE_HR_MIN, hi=STROKE_HR_MAX),
                "is_rest": is_rest,
            }
        )

    result.interval_count = interval_idx + 1
    if interval_work_times_s is None:
        if result.interval_count > 1:
            result.warnings.append(f"{result.interval_count - 1} t/d resets in a non-interval workout")
    elif result.interval_count != len(interval_work_times_s):
        result.warnings.append(
            f"detected {result.interval_count} intervals in strokes, summary lists {len(interval_work_times_s)}; "
            "rest labels past the summary's intervals are unreliable"
        )
    return result


def with_elapsed(rows: list[Any]) -> list[dict[str, Any]]:
    """Add workout-wide elapsed time/distance by offsetting each interval by the previous intervals' ends.

    Accepts Stroke ORM rows (or anything with the same attributes), ordered by seq.
    """
    out = []
    t_offset = d_offset = Decimal(0)
    current_idx = None
    max_t = max_d = Decimal(0)
    for r in rows:
        if current_idx is not None and r.interval_idx != current_idx:
            # t jitters backwards near interval ends, so an interval ends at its max, not its last stroke.
            t_offset += max_t
            d_offset += max_d
            max_t = max_d = Decimal(0)
        current_idx = r.interval_idx
        max_t, max_d = max(max_t, r.t_s), max(max_d, r.d_m)
        out.append(
            {
                "seq": r.seq,
                "interval_idx": r.interval_idx,
                "is_rest": r.is_rest,
                "t_s": float(r.t_s),
                "d_m": float(r.d_m),
                "elapsed_t_s": float(t_offset + r.t_s),
                "elapsed_d_m": float(d_offset + r.d_m),
                "pace_s_500": float(r.pace_s_500) if r.pace_s_500 is not None else None,
                "watts": round(float(watts_from_pace(r.pace_s_500)), 1) if r.pace_s_500 else None,
                "spm": r.spm,
                "hr": r.hr,
            }
        )
    return out


def lttb_indices(xs: list[float], ys: list[float], threshold: int) -> list[int]:
    """Largest-Triangle-Three-Buckets: indices of `threshold` points that preserve the visual shape."""
    n = len(xs)
    if threshold >= n or threshold < 3:
        return list(range(n))

    selected = [0]
    bucket_size = (n - 2) / (threshold - 2)
    a = 0
    for i in range(threshold - 2):
        start = int(i * bucket_size) + 1
        end = int((i + 1) * bucket_size) + 1
        next_start, next_end = end, min(int((i + 2) * bucket_size) + 1, n)
        avg_x = sum(xs[next_start:next_end]) / (next_end - next_start)
        avg_y = sum(ys[next_start:next_end]) / (next_end - next_start)

        best, best_area = start, -1.0
        for j in range(start, end):
            area = abs((xs[a] - avg_x) * (ys[j] - ys[a]) - (xs[a] - xs[j]) * (avg_y - ys[a]))
            if area > best_area:
                best, best_area = j, area
        selected.append(best)
        a = best
    selected.append(n - 1)
    return selected


def downsample(points: list[dict[str, Any]], threshold: int) -> list[dict[str, Any]]:
    """Downsample on elapsed time vs pace. Strokes without pace can't be placed and are dropped."""
    usable = [p for p in points if p["pace_s_500"] is not None]
    if threshold >= len(usable):
        return usable
    idx = lttb_indices([p["elapsed_t_s"] for p in usable], [p["pace_s_500"] for p in usable], threshold)
    return [usable[i] for i in idx]
