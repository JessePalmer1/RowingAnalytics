from decimal import Decimal

import pytest

from erg.classify import Features, classify, match_test_distance, steady_baseline
from erg.eligibility import Eligibility, EligibilityInputs, evaluate

THRESHOLD = Decimal("114.1")  # steady 2:04.1 minus a 10s margin


def feat(**kw):
    base = dict(
        workout_id=1,
        work_time_s=Decimal(1800),
        work_distance_m=7500,
        rest_time_s=Decimal(0),
        workout_type="FixedTimeSplits",
        avg_pace_s_500=Decimal(120),
        stroke_interval_count=1,
        summary_interval_count=0,
        interval_threshold_s_500=THRESHOLD,
    )
    return Features(**(base | kw))


# ---- rule 1: solo test distances are tests, whatever the pace ---------------

@pytest.mark.parametrize("distance,expected", [(2000, "test_2k"), (6000, "test_6k"), (10000, "test_10k")])
def test_solo_test_distance_is_a_test_regardless_of_pace(distance, expected):
    for pace in (Decimal(95), Decimal(125), Decimal(160)):  # race pace, steady, paddle
        result = classify(feat(work_distance_m=distance, avg_pace_s_500=pace, work_time_s=Decimal(distance) * pace / 500))
        assert result.workout_class == expected
        assert result.reason == f"solo {distance}m"


def test_test_distance_tolerance_is_two_percent():
    assert match_test_distance(1970) == (2000, "test_2k")
    assert match_test_distance(6110) == (6000, "test_6k")
    assert match_test_distance(1950) is None
    assert match_test_distance(5000) is None


def test_test_distance_broken_into_intervals_is_not_a_test():
    # 2x1000m is 2000m of work, but not a solo 2k.
    broken = feat(work_distance_m=2000, rest_time_s=Decimal(240), avg_pace_s_500=Decimal(89))
    assert classify(broken).workout_class == "interval"
    by_strokes = feat(work_distance_m=6000, stroke_interval_count=3, avg_pace_s_500=Decimal(118))
    assert classify(by_strokes).workout_class == "steady"


def test_a_2k_test_is_never_a_short_piece():
    # 6:24 is under the 10-minute floor, but 2000m solo is a test first.
    assert classify(feat(work_distance_m=2000, work_time_s=Decimal(384))).workout_class == "test_2k"


# ---- rule 2: short solo pieces ----------------------------------------------

def test_short_solo_piece():
    assert classify(feat(work_time_s=Decimal(60), work_distance_m=344, avg_pace_s_500=Decimal(87))).workout_class == "short_piece"
    # Short but broken into intervals is judged on pace instead.
    short_intervals = feat(work_time_s=Decimal(480), work_distance_m=2600, rest_time_s=Decimal(480), avg_pace_s_500=Decimal(92))
    assert classify(short_intervals).workout_class == "interval"


# ---- rule 3: pace against the athlete's own threshold -----------------------

def test_faster_than_threshold_is_interval_whatever_the_shape():
    assert classify(feat(avg_pace_s_500=Decimal(110))).workout_class == "interval"  # solo 5k-ish at 1:50
    assert classify(feat(avg_pace_s_500=Decimal(110), rest_time_s=Decimal(600))).workout_class == "interval"


def test_slower_than_threshold_is_steady_whatever_the_shape():
    # A 4x15' or 4x3k at 2:00 is steady work even with rests between.
    shaped = classify(feat(avg_pace_s_500=Decimal(120), rest_time_s=Decimal(600)))
    assert shaped.workout_class == "steady" and "broken into intervals" in shaped.reason
    assert classify(feat(avg_pace_s_500=Decimal(120))).workout_class == "steady"


def test_threshold_boundary():
    assert classify(feat(avg_pace_s_500=Decimal("114.0"))).workout_class == "interval"
    assert classify(feat(avg_pace_s_500=Decimal("114.1"))).workout_class == "steady"  # at threshold counts as steady


def test_threshold_is_personal():
    # The same 1:58 session is steady for one athlete and interval work for a slower one.
    same_piece = dict(avg_pace_s_500=Decimal(118))
    assert classify(feat(**same_piece, interval_threshold_s_500=Decimal(114))).workout_class == "steady"
    assert classify(feat(**same_piece, interval_threshold_s_500=Decimal(125))).workout_class == "interval"


def test_without_a_baseline_falls_back_on_shape_with_low_confidence():
    solo = classify(feat(interval_threshold_s_500=None))
    broken = classify(feat(interval_threshold_s_500=None, rest_time_s=Decimal(600)))
    assert (solo.workout_class, broken.workout_class) == ("steady", "interval")
    assert solo.confidence < 0.7 and broken.confidence < 0.7


def test_stroke_derived_pace_is_marked_in_the_reason():
    f = feat(work_time_s=Decimal(0), work_distance_m=0, rest_time_s=Decimal(480),
             avg_pace_s_500=Decimal(125), pace_from_strokes=True)
    result = classify(f)
    assert result.workout_class == "steady" and "pace from strokes" in result.reason


def test_unknown_when_nothing_to_judge():
    assert classify(feat(work_time_s=Decimal(0), work_distance_m=0, avg_pace_s_500=None)).workout_class == "unknown"
    no_pace_intervals = feat(work_time_s=Decimal(0), work_distance_m=0, rest_time_s=Decimal(480), avg_pace_s_500=None)
    assert classify(no_pace_intervals).workout_class == "interval"


# ---- the steady baseline ----------------------------------------------------

def test_baseline_is_the_mean_of_the_slower_half():
    paces = [Decimal(p) for p in (100, 110, 115, 118, 120, 122, 124, 126)]
    # Slower half: 120, 122, 124, 126 -> 123.
    assert steady_baseline(paces) == Decimal("123.0")


def test_baseline_ignores_slow_outliers():
    typical = [Decimal(p) for p in (112, 115, 118, 119, 120, 121, 122, 124, 125, 126)]
    with_paddles = typical + [Decimal(160), Decimal(163)]
    # The paddles sit above the Tukey fence and are dropped entirely, so the baseline is
    # exactly what it would be without them: the slower half 121..126, mean 123.6.
    assert steady_baseline(typical) == Decimal("123.6")
    assert steady_baseline(with_paddles) == Decimal("123.6")


def test_baseline_needs_enough_history():
    assert steady_baseline([Decimal(120)] * 4) is None
    assert steady_baseline([Decimal(120)] * 5) == Decimal("120.0")


def elig(**kw):
    base = dict(
        workout_id=1,
        workout_class="steady",
        is_continuous=True,
        work_time_s=Decimal(1800),
        work_distance_m=7000,
        avg_watts=Decimal(200),
        hr_avg=145,
        stroke_count=500,
        strokes_stored=500,
        strokes_with_hr=500,
        rest_strokes_with_hr=0,
        stroke_warning=None,
    )
    return {e.metric: e for e in evaluate(EligibilityInputs(**(base | kw)))}


def test_ef_needs_steady_15min_hr_and_watts():
    assert elig()["ef"] == Eligibility("ef", True, None)
    assert elig(workout_class="test_2k")["ef"].reason == "not a steady piece (test_2k)"
    assert elig(work_time_s=Decimal(600))["ef"].reason == "under 15 min of work"
    assert elig(hr_avg=None)["ef"].reason == "no valid average HR"
    assert elig(avg_watts=None)["ef"].reason == "no watts"


def test_decoupling_enforces_the_20_minute_floor():
    assert elig()["decoupling"].eligible
    assert "not meaningful" in elig(work_time_s=Decimal(1000))["decoupling"].reason
    assert "stroke HR covers" in elig(strokes_with_hr=100)["decoupling"].reason
    assert elig(strokes_stored=0, strokes_with_hr=0)["decoupling"].reason == "no stroke data to split into halves"


def test_interval_shaped_steady_needs_stroke_hr_and_cannot_decouple():
    shaped = dict(is_continuous=False, work_time_s=Decimal(2700))
    assert elig(**shaped)["ef"].eligible  # EF from work strokes only
    assert "needs one continuous piece" in elig(**shaped)["decoupling"].reason
    assert "no stroke data" in elig(**shaped, strokes_stored=0, strokes_with_hr=0)["ef"].reason


def test_hrr_needs_rest_periods_with_summary_hr_pairs():
    # Source is the C2 per-interval summary, not stroke HR, so interval-shaped steady
    # sessions qualify too.
    assert elig(is_continuous=False, interval_hr_pairs=5)["hrr"].eligible
    assert elig(workout_class="interval", is_continuous=False, interval_hr_pairs=5)["hrr"].eligible
    assert elig(is_continuous=False)["hrr"].reason == "no ending/rest HR pair in the interval summary"
    assert "no rest periods" in elig(interval_hr_pairs=5)["hrr"].reason


def test_pacing_and_dps():
    assert elig()["pacing"].eligible and elig()["dps"].eligible
    assert elig(strokes_stored=10)["pacing"].reason == "only 10 strokes stored"
    assert "stroke parse warning" in elig(stroke_warning="detected 6 intervals")["pacing"].reason
    assert elig(stroke_count=None)["dps"].reason == "no stroke count"
    assert elig(work_distance_m=0)["dps"].reason == "no work distance"


def test_every_metric_reports_a_reason_when_ineligible():
    for e in evaluate(
        EligibilityInputs(1, "unknown", True, Decimal(0), 0, None, None, None, 0, 0, 0, "warned")
    ):
        assert not e.eligible and e.reason
