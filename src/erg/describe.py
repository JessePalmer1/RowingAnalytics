"""Human-readable workout descriptions in rowing notation.

Built from the per-interval summary Concept2 stores with each workout, so a session reads
the way it was programmed rather than as a total:

    3x20' / 2'r                 uniform intervals
    3x(10x1' / 30"r) / 2'r      repeated sets with a longer rest between them
    5k-4k-3k-2k-1k / 1'30"-3'r  a ladder
    6x750m / ~3'44"r            rest varied (ErgData logs the rest actually taken)
    30'  ·  6k  ·  just row     solo pieces
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Rep:
    target: str  # "20'", "1000m", "5k"
    rest_s: float  # rest after this rep; the final rep's rest is ignored


def fmt_time(seconds: float | Decimal) -> str:
    """600 -> 10', 30 -> 30", 100 -> 1'40", 3600 -> 60'."""
    total = int(round(float(seconds)))
    minutes, secs = divmod(total, 60)
    if minutes and secs:
        return f"{minutes}'{secs:02d}\""
    if minutes:
        return f"{minutes}'"
    return f'{secs}"'


def fmt_distance(metres: int, use_k: bool) -> str:
    if use_k and metres >= 1000 and metres % 1000 == 0:
        return f"{metres // 1000}k"
    return f"{metres}m"


def _reps(intervals: Sequence[dict]) -> list[Rep]:
    # Use "k" throughout when the session has long distance reps, so a ladder reads
    # 5k-4k-3k-2k-1k rather than 5k-4k-3k-2k-1000m.
    distances = [int(i["distance_m"]) for i in intervals if i.get("target_type") == "distance"]
    use_k = bool(distances) and max(distances) >= 2000
    reps = []
    for i in intervals:
        if i.get("target_type") == "distance":
            target = fmt_distance(int(i["distance_m"]), use_k)
        else:
            target = fmt_time(i["time_s"])
        reps.append(Rep(target, float(i.get("rest_time_s") or 0)))
    return reps


def _rest(rests: Sequence[float]) -> str:
    """Rest between reps: exact when uniform, a range for ladders, ~mean when irregular."""
    rests = [r for r in rests if r > 0]
    if not rests:
        return ""
    if len(set(rests)) == 1:
        return f" / {fmt_time(rests[0])}r"
    ordered = sorted(rests)
    # Monotonic rests (a ladder stepping its rest down or up) read best as a range.
    if list(rests) == ordered or list(rests) == ordered[::-1]:
        return f" / {fmt_time(ordered[0])}-{fmt_time(ordered[-1])}r"
    return f" / ~{fmt_time(sum(rests) / len(rests))}r"


def _describe_reps(reps: list[Rep]) -> str:
    n = len(reps)
    if n == 0:
        return ""
    targets = [r.target for r in reps]
    inner_rests = [r.rest_s for r in reps[:-1]]  # the last rep's rest never happened
    uniform = len(set(targets)) == 1

    if uniform and (n == 1 or len(set(inner_rests)) <= 1):
        return targets[0] if n == 1 else f"{n}x{targets[0]}{_rest(inner_rests)}"

    # Repeated sets, checked before falling back to an averaged rest: 30x1' with a longer
    # rest after every 10th is 3x(10x1' / 30"r) / 2'r, not 30x1' / ~36"r. Takes the smallest
    # block that tiles the session with identical reps and rests inside every block and one
    # rest between blocks.
    for size in range(2, n // 2 + 1):
        if n % size:
            continue
        blocks = [reps[i : i + size] for i in range(0, n, size)]
        if any([r.target for r in b] != targets[:size] for b in blocks):
            continue
        within = {tuple(r.rest_s for r in b[:-1]) for b in blocks}
        between = {b[-1].rest_s for b in blocks[:-1]}
        if len(within) == 1 and len(between) == 1:
            between_rest = between.pop()
            if set(next(iter(within))) <= {between_rest}:
                # One rest throughout: 2x(15'-13'-10') / 3'r, not 2x(15'-13'-10' / 3'r) / 3'r.
                inner = _describe_reps([Rep(r.target, 0) for r in blocks[0]])
            else:
                inner = _describe_reps(blocks[0])
            if " " in inner or "x" in inner or "-" in inner:
                inner = f"({inner})"
            return f"{len(blocks)}x{inner}{_rest([between_rest])}"

    if uniform:
        return f"{n}x{targets[0]}{_rest(inner_rests)}"  # same reps, irregular rest

    # Otherwise run-length encode: 2x10' + 3x5', or a ladder 5k-4k-3k-2k-1k.
    groups: list[tuple[int, str]] = []
    for t in targets:
        if groups and groups[-1][1] == t:
            groups[-1] = (groups[-1][0] + 1, t)
        else:
            groups.append((1, t))
    if all(count == 1 for count, _ in groups):
        body = "-".join(t for _, t in groups)
    else:
        body = " + ".join(t if count == 1 else f"{count}x{t}" for count, t in groups)
    return body + _rest(inner_rests)


def describe(
    workout_type: str | None,
    rest_time_s: float | Decimal,
    work_time_s: float | Decimal,
    work_distance_m: int,
    intervals: Sequence[dict],
    stroke_interval_count: int | None = None,
) -> str:
    """One line describing what the session was.

    `intervals` are interval_split rows (normalize.normalize_interval_splits). Split rows of
    solo pieces are divisions of one piece, not reps, so only `kind == "interval"` counts.
    """
    reps_rows = [i for i in intervals if i.get("kind") == "interval"]
    solo = not reps_rows and float(rest_time_s or 0) == 0

    if solo:
        if workout_type == "FixedTimeSplits" and work_time_s:
            return fmt_time(work_time_s)
        if workout_type == "JustRow":
            return f"just row · {work_distance_m}m" if work_distance_m else "just row"
        if work_distance_m:
            return fmt_distance(work_distance_m, use_k=work_distance_m >= 2000)
        return fmt_time(work_time_s) if work_time_s else "–"

    reps = _reps(reps_rows)
    # Some logbook summaries are truncated: the strokes show more reps than were recorded.
    missing = (stroke_interval_count or 0) - len(reps)
    if missing > 0 and reps and workout_type in ("FixedDistanceInterval", "FixedTimeInterval"):
        # Fixed intervals share one target and rest by definition, so the missing reps are
        # known: rebuild the full session.
        reps += [Rep(reps[0].target, reps[0].rest_s)] * missing
        return _describe_reps(reps) + f" ({missing} rep{'s' if missing > 1 else ''} from strokes)"
    text = _describe_reps(reps) if reps else "intervals"
    if missing > 0:
        text += f" + {missing} more in strokes"
    return text


def describe_workout(workout, stroke_interval_count: int | None = None) -> str:
    """describe() for a stored Workout, reading its reps from the raw payload."""
    from erg.normalize import normalize_interval_splits

    return describe(
        workout.workout_type,
        workout.rest_time_s,
        workout.work_time_s,
        workout.work_distance_m,
        normalize_interval_splits(workout.raw),
        stroke_interval_count,
    )
