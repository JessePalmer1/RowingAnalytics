from datetime import datetime, timezone
from decimal import Decimal

from conftest import result_payload

from erg.normalize import normalize_athlete, normalize_workout, watts_from_pace


def norm(**overrides):
    return normalize_workout(result_payload(**overrides), athlete_id=42, default_tz="America/New_York")


def test_units_converted_at_boundary():
    w = norm()
    assert w["work_time_s"] == Decimal("420")
    assert w["work_distance_m"] == 2000
    assert w["avg_pace_s_500"] == Decimal("105.00")


def test_athlete_weight_7500_is_75kg():
    assert normalize_athlete({"id": 1, "weight": 7500})["weight_g"] == 75_000
    assert normalize_athlete({"id": 1})["weight_g"] is None


def test_watts_from_pace_matches_concept2_formula():
    # 2:00/500m is the canonical 202.5 W reference point.
    assert round(watts_from_pace(Decimal(120)), 1) == Decimal("202.5")
    assert norm()["avg_watts"] == Decimal("302.3")  # 1:45/500m


def test_date_is_end_of_workout_and_start_is_derived():
    w = norm(date="2026-02-10 18:30:00", date_utc="2026-02-10 18:30:00", timezone="Europe/London")
    assert w["ended_at_local"] == datetime(2026, 2, 10, 18, 30)
    assert w["ended_at_utc"] == datetime(2026, 2, 10, 18, 30, tzinfo=timezone.utc)
    assert w["started_at_utc"] == datetime(2026, 2, 10, 18, 23, tzinfo=timezone.utc)


def test_interval_top_level_is_work_only_and_rest_counts_toward_start():
    # 4x1000m / 3:00r: 4000m work in 14:00, 9:00 rest.
    w = norm(workout_type="FixedDistanceInterval", distance=4000, time=8400, rest_time=5400, rest_distance=120)
    assert w["work_distance_m"] == 4000
    assert w["rest_time_s"] == Decimal("540")
    assert w["rest_distance_m"] == 120
    assert w["avg_pace_s_500"] == Decimal("105.00")  # rest excluded from pace
    assert (w["ended_at_utc"] - w["started_at_utc"]).total_seconds() == 840 + 540


def test_utc_computed_from_payload_timezone_when_date_utc_missing():
    w = norm(date="2026-07-01 07:00:00", date_utc=None, timezone="America/Los_Angeles")
    assert w["ended_at_utc"] == datetime(2026, 7, 1, 14, 0, tzinfo=timezone.utc)
    assert (w["tz"], w["tz_source"]) == ("America/Los_Angeles", "payload")


def test_null_timezone_falls_back_to_default_and_records_it():
    w = norm(date="2026-01-15 06:00:00", date_utc=None, timezone=None)
    assert w["ended_at_utc"] == datetime(2026, 1, 15, 11, 0, tzinfo=timezone.utc)
    assert (w["tz"], w["tz_source"]) == ("America/New_York", "athlete_default")


def test_unknown_timezone_string_falls_back():
    w = norm(date_utc=None, timezone="Not/AZone")
    assert w["tz_source"] == "athlete_default"


def test_hr_validation():
    good = norm(heart_rate={"average": 150, "ending": 170, "rest": 72})
    assert (good["hr_avg"], good["hr_ending"], good["hr_rest"], good["hr_quality"]) == (150, 170, 72, "valid")

    strap_off = norm(heart_rate={"average": 0, "ending": 0, "rest": 0})
    assert (strap_off["hr_avg"], strap_off["hr_quality"]) == (None, "absent")

    garbage = norm(heart_rate={"average": 45, "ending": 240})
    assert (garbage["hr_avg"], garbage["hr_ending"], garbage["hr_quality"]) == (None, None, "invalid")

    assert norm(heart_rate=None)["hr_quality"] == "absent"


def test_web_entry_without_strokes_or_distance():
    w = norm(source="Web", stroke_data=False, heart_rate=None, distance=0, stroke_count=None)
    assert w["has_strokes"] is False
    assert w["avg_pace_s_500"] is None and w["avg_watts"] is None and w["watts_derived"] is False
