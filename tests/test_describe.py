"""Descriptions for the session shapes that actually appear in the logbook."""

import pytest

from erg.describe import describe, fmt_distance, fmt_time


def reps(*specs, rests):
    """specs like "time:600" or "distance:5000"; rests in seconds, one per rep."""
    rows = []
    for spec, rest in zip(specs, rests, strict=True):
        kind, value = spec.split(":")
        rows.append(
            {
                "kind": "interval",
                "target_type": kind,
                "time_s": float(value) if kind == "time" else 0.0,
                "distance_m": int(value) if kind == "distance" else 0,
                "rest_time_s": float(rest),
            }
        )
    return rows


def intervals(rows, workout_type="VariableInterval", strokes=None):
    return describe(workout_type, 60, 1800, 8000, rows, strokes)


@pytest.mark.parametrize(
    "seconds,text",
    [(600, "10'"), (30, '30"'), (100, "1'40\""), (3600, "60'"), (90, "1'30\""), (569, "9'29\"")],
)
def test_time_notation(seconds, text):
    assert fmt_time(seconds) == text


def test_distance_notation():
    assert fmt_distance(750, use_k=True) == "750m"
    assert fmt_distance(1500, use_k=True) == "1500m"
    assert fmt_distance(5000, use_k=True) == "5k"
    assert fmt_distance(1000, use_k=False) == "1000m"


def test_uniform_time_intervals():
    assert intervals(reps(*["time:1200"] * 3, rests=[120] * 3), "FixedTimeInterval") == "3x20' / 2'r"
    assert intervals(reps(*["time:100"] * 20, rests=[20] * 20), "FixedTimeInterval") == "20x1'40\" / 20\"r"


def test_uniform_distance_intervals():
    assert intervals(reps(*["distance:1000"] * 4, rests=[180] * 4), "FixedDistanceInterval") == "4x1000m / 3'r"
    assert intervals(reps(*["distance:5000"] * 3, rests=[120] * 3), "FixedDistanceInterval") == "3x5k / 2'r"


def test_final_rest_is_ignored():
    # The logbook often records 0 (or a different value) after the last rep.
    assert intervals(reps("time:900", "time:900", rests=[180, 0])) == "2x15' / 3'r"


def test_ladder_with_stepped_rest():
    rows = reps("distance:5000", "distance:4000", "distance:3000", "distance:2000", "distance:1000",
                rests=[180, 150, 120, 90, 0])
    assert intervals(rows) == "5k-4k-3k-2k-1k / 1'30\"-3'r"


def test_repeated_sets_with_longer_rest_between():
    rest_pattern = ([30] * 9 + [120]) * 3
    assert intervals(reps(*["time:60"] * 30, rests=rest_pattern)) == "3x(10x1' / 30\"r) / 2'r"


def test_repeated_block_with_one_rest_throughout():
    rows = reps("time:900", "time:780", "time:600", "time:900", "time:780", "time:600", rests=[180] * 6)
    assert intervals(rows) == "2x(15'-13'-10') / 3'r"


def test_mixed_runs():
    rows = reps("time:900", "time:600", "time:300", "time:300", rests=[300] * 4)
    assert intervals(rows) == "15' + 10' + 2x5' / 5'r"


def test_irregular_rest_is_averaged():
    # ErgData logs the rest actually taken on variable intervals with open rest.
    rows = reps(*["distance:750"] * 6, rests=[198, 205, 319, 167, 129, 325])
    assert intervals(rows) == "6x750m / ~3'24\"r"


def test_truncated_fixed_interval_is_rebuilt_from_stroke_count():
    rows = reps("distance:4000", rests=[180])
    assert intervals(rows, "FixedDistanceInterval", strokes=2) == "2x4k / 3'r (1 rep from strokes)"


def test_truncated_variable_interval_reports_the_gap():
    rows = reps(*["distance:750"] * 5, rests=[198, 205, 319, 167, 129])
    assert intervals(rows, strokes=6).endswith("+ 1 more in strokes")


@pytest.mark.parametrize(
    "workout_type,time_s,distance_m,text",
    [
        ("FixedDistanceSplits", 384, 2000, "2k"),
        ("FixedDistanceSplits", 1249, 6000, "6k"),
        ("FixedDistanceSplits", 1233, 4824, "4824m"),
        ("FixedTimeSplits", 1800, 7110, "30'"),
        ("FixedTimeSplits", 60, 344, "1'"),
        ("JustRow", 968, 4025, "just row · 4025m"),
    ],
)
def test_solo_pieces(workout_type, time_s, distance_m, text):
    # Split rows of a solo piece are divisions of one piece, not reps.
    splits = [{"kind": "split", "target_type": "distance", "time_s": 96.0, "distance_m": 500, "rest_time_s": 0.0}]
    assert describe(workout_type, 0, time_s, distance_m, splits) == text
