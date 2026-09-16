from decimal import Decimal
from types import SimpleNamespace

from conftest import result_payload

from erg.normalize import normalize_interval_splits, normalize_workout
from erg.strokes import downsample, lttb_indices, parse_strokes, with_elapsed


def s(t, d, p=1050, spm=28, hr=150):
    return {"t": t, "d": d, "p": p, "spm": spm, "hr": hr}


def test_interval_boundary_requires_t_and_d_to_reset():
    raw = [
        s(10, 35, p=0, spm=0, hr=80),  # first stroke: no pace/rate yet
        s(500, 2400),
        s(1000, 4810),  # end of work (100s)
        s(1180, 5390),  # rest paddling
        s(1134, 5402),  # t jitters backwards while d rises: NOT a new interval
        s(7, 31),  # both reset: interval 2
        s(990, 4800),
    ]
    parsed = parse_strokes(1, raw, [Decimal(100), Decimal(100)])
    assert [r["interval_idx"] for r in parsed.rows] == [0, 0, 0, 0, 0, 1, 1]
    assert [r["is_rest"] for r in parsed.rows] == [False, False, False, True, True, False, False]
    assert parsed.interval_count == 2 and parsed.warnings == []

    first = parsed.rows[0]
    assert (first["pace_s_500"], first["spm"], first["hr"]) == (None, None, 80)  # low HR is valid per-stroke
    assert parsed.rows[1]["d_m"] == Decimal("240") and parsed.rows[1]["pace_s_500"] == Decimal("105")


def test_no_strap_hr_is_null():
    parsed = parse_strokes(1, [s(10, 30, hr=0), s(40, 120, hr=0)], None)
    assert all(r["hr"] is None for r in parsed.rows)


def test_interval_count_mismatch_is_warned():
    parsed = parse_strokes(1, [s(10, 30), s(900, 4000)], [Decimal(90), Decimal(90)])
    assert "detected 1 intervals" in parsed.warnings[0]


def test_resets_in_non_interval_workout_are_warned_and_never_rest():
    parsed = parse_strokes(1, [s(100, 400), s(5, 20)], None)
    assert parsed.warnings and not any(r["is_rest"] for r in parsed.rows)


def test_empty_stroke_array():
    parsed = parse_strokes(1, [], None)
    assert parsed.rows == [] and parsed.interval_count == 0


def test_elapsed_offsets_use_interval_max_not_last_stroke():
    rows = [
        SimpleNamespace(seq=0, interval_idx=0, is_rest=False, t_s=Decimal(50), d_m=Decimal(240), pace_s_500=Decimal(105), spm=28, hr=150),
        SimpleNamespace(seq=1, interval_idx=0, is_rest=True, t_s=Decimal("118.0"), d_m=Decimal(540), pace_s_500=Decimal(200), spm=18, hr=160),
        SimpleNamespace(seq=2, interval_idx=0, is_rest=True, t_s=Decimal("113.4"), d_m=Decimal(541), pace_s_500=None, spm=None, hr=None),
        SimpleNamespace(seq=3, interval_idx=1, is_rest=False, t_s=Decimal(2), d_m=Decimal(10), pace_s_500=Decimal(120), spm=30, hr=140),
    ]
    out = with_elapsed(rows)
    assert out[3]["elapsed_t_s"] == 120.0  # 118 (max) + 2, not 113.4 + 2
    assert out[3]["elapsed_d_m"] == 551.0
    assert out[0]["watts"] == 302.3 and out[2]["watts"] is None


def test_lttb_keeps_endpoints_and_the_spike():
    xs = list(range(100))
    ys = [100.0] * 100
    ys[57] = 200.0
    idx = lttb_indices(xs, ys, 10)
    assert len(idx) == 10 and idx[0] == 0 and idx[-1] == 99 and 57 in idx


def test_downsample_drops_paceless_points_and_passes_through_when_small():
    pts = [{"elapsed_t_s": float(i), "pace_s_500": None if i == 0 else 100.0} for i in range(5)]
    assert len(downsample(pts, 50)) == 4


def test_interval_splits_from_raw():
    payload = result_payload(
        workout={
            "intervals": [
                {"type": "time", "time": 1000, "distance": 481, "rest_time": 200, "rest_distance": 44,
                 "stroke_rate": 29, "calories_total": 38,
                 "heart_rate": {"average": 146, "max": 163, "ending": 163, "rest": 155, "min": 102}},
                {"type": "time", "time": 1000, "distance": 478, "rest_time": 200, "stroke_rate": 29, "heart_rate": {}},
            ]
        }
    )
    rows = normalize_interval_splits(payload)
    assert [(r["kind"], r["target_type"], r["time_s"], r["rest_time_s"]) for r in rows] == [
        ("interval", "time", Decimal(100), Decimal(20)),
        ("interval", "time", Decimal(100), Decimal(20)),
    ]
    assert (rows[0]["hr_avg"], rows[0]["hr_rest"], rows[1]["hr_avg"], rows[1]["rest_distance_m"]) == (146, 155, None, 0)
    assert normalize_interval_splits(result_payload(workout={"splits": [{"time": 2106, "distance": 1000}]}))[0]["kind"] == "split"
    assert normalize_interval_splits(result_payload()) == []


def test_drag_factor_range():
    def drag(v):
        return normalize_workout(result_payload(drag_factor=v), 42, "UTC")["drag_factor"]

    assert (drag(90), drag(220), drag(225)) == (90, 220, 225)
    assert (drag(89), drag(226), drag(None)) == (None, None, None)
