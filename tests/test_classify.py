from decimal import Decimal

from erg.classify import Features, classify
from erg.eligibility import Eligibility, EligibilityInputs, evaluate

MAX_HR = 193
BEST_2K = Decimal("96")


def feat(**kw):
    base = dict(
        workout_id=1,
        work_time_s=Decimal(1200),
        work_distance_m=5000,
        rest_time_s=Decimal(0),
        workout_type="FixedDistanceSplits",
        avg_pace_s_500=Decimal(110),  # 1:50 — faster than the steady cutoff
        hr_avg=140,
        max_heart_rate=MAX_HR,
        best_2k_pace_s_500=BEST_2K,
        stroke_interval_count=1,
        summary_interval_count=0,
    )
    return Features(**(base | kw))


def test_interval_detected_by_rest_time_type_or_strokes():
    assert classify(feat(rest_time_s=Decimal(240))).workout_class == "interval"
    assert classify(feat(workout_type="VariableInterval")).workout_class == "interval"
    assert classify(feat(stroke_interval_count=6)).workout_class == "interval"
    assert classify(feat(summary_interval_count=4)).workout_class == "interval"


def test_intervals_of_any_shape_share_one_class():
    # 4x10min and 20x30s are both hard efforts at or above threshold.
    long_intervals = feat(work_time_s=Decimal(2400), work_distance_m=10000, rest_time_s=Decimal(600))
    short_intervals = feat(work_time_s=Decimal(600), work_distance_m=2600, rest_time_s=Decimal(600), hr_avg=180)
    assert classify(long_intervals).workout_class == classify(short_intervals).workout_class == "interval"


def test_pace_slower_than_155_is_steady_whatever_the_shape():
    # Athlete rule: 4x15' or 4x3k at 2:00 is steady work even with rests and HR over 150.
    shaped = feat(rest_time_s=Decimal(600), avg_pace_s_500=Decimal(120), hr_avg=160)
    assert classify(shaped).workout_class == "steady"
    assert "interval-shaped but" in classify(shaped).reason
    # Just inside the cutoff stays interval work.
    assert classify(feat(rest_time_s=Decimal(600), avg_pace_s_500=Decimal("115.0"))).workout_class == "interval"
    assert classify(feat(rest_time_s=Decimal(600), avg_pace_s_500=Decimal("115.1"))).workout_class == "steady"


def test_stroke_derived_pace_is_marked_in_the_reason():
    # Truncated summaries have no totals; pace comes from the stroke stream instead.
    f = feat(work_time_s=Decimal(0), work_distance_m=0, rest_time_s=Decimal(480),
             avg_pace_s_500=Decimal(125), pace_from_strokes=True)
    result = classify(f)
    assert result.workout_class == "steady" and "pace from strokes" in result.reason


def test_test_distances_by_heart_rate():
    assert classify(feat(work_distance_m=2000, work_time_s=Decimal(384), avg_pace_s_500=Decimal(96), hr_avg=178)).workout_class == "test_2k"
    assert classify(feat(work_distance_m=6000, work_time_s=Decimal(1254), avg_pace_s_500=Decimal("104.5"), hr_avg=179)).workout_class == "test_6k"
    assert classify(feat(work_distance_m=10000, work_time_s=Decimal(2238), avg_pace_s_500=Decimal("111.9"), hr_avg=166)).workout_class == "test_10k"
    # ±2% still counts as the distance.
    assert classify(feat(work_distance_m=1970, avg_pace_s_500=Decimal(96), hr_avg=180)).workout_class == "test_2k"
    assert classify(feat(work_distance_m=1900, avg_pace_s_500=Decimal(96), hr_avg=180)).workout_class != "test_2k"


def test_a_2k_test_is_never_a_short_piece():
    # 6:24 is under the 10-minute floor but 2000m is a test distance.
    assert classify(feat(work_distance_m=2000, work_time_s=Decimal(384), avg_pace_s_500=Decimal(96), hr_avg=178)).workout_class == "test_2k"


def test_easy_piece_at_a_test_distance_is_steady():
    # Caught by the pace rule before HR is even consulted.
    easy_6k = feat(work_distance_m=6000, work_time_s=Decimal(1500), avg_pace_s_500=Decimal(125), hr_avg=140)
    assert classify(easy_6k).workout_class == "steady"


def test_borderline_effort_is_flagged_low_confidence():
    result = classify(feat(work_distance_m=6000, avg_pace_s_500=Decimal(110), hr_avg=152))  # 79% of max
    assert result.workout_class == "test_6k" and result.confidence < 0.7


def test_pace_fallback_when_hr_missing():
    fast = classify(feat(work_distance_m=6000, hr_avg=None, avg_pace_s_500=Decimal("104.5")))
    slow = classify(feat(work_distance_m=6000, hr_avg=None, avg_pace_s_500=Decimal("125")))
    assert (fast.workout_class, slow.workout_class) == ("test_6k", "steady")
    assert fast.confidence < 0.7  # no HR: always worth a look


def test_steady_and_short_pieces():
    assert classify(feat(work_time_s=Decimal(2820), hr_avg=141)).workout_class == "steady"
    assert classify(feat(work_time_s=Decimal(60), work_distance_m=344, avg_pace_s_500=Decimal(87))).workout_class == "short_piece"


def test_unknown_when_totals_are_missing():
    # The 5 truncated-summary workouts have zero totals; the interval check catches them first.
    assert classify(feat(work_time_s=Decimal(0), work_distance_m=0)).workout_class == "unknown"
    assert classify(feat(work_time_s=Decimal(0), work_distance_m=0, rest_time_s=Decimal(480))).workout_class == "interval"


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


def test_hrr_needs_intervals_with_rest_hr():
    assert elig(workout_class="interval", rest_strokes_with_hr=12)["hrr"].eligible
    assert elig(workout_class="interval")["hrr"].reason == "no HR recorded during rest periods"
    assert elig()["hrr"].reason == "not an interval session (steady)"


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
