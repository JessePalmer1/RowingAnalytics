"""Convert raw Concept2 payloads into our units at the boundary.

Nothing downstream of this module ever sees raw C2 units:
  time: tenths of a second -> seconds
  distance: meters (unchanged); stroke `d`: decimeters -> meters
  weight: decagrams -> grams (docs say "decigrams", but 7500 = 75 kg only holds for decagrams)
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

HR_MIN = 90
HR_MAX = 210
# Rest HR is sampled after recovery, so a 90 bpm floor would discard real values.
HR_REST_MIN = 40

WATTS_CONSTANT = Decimal("2.80")


def tenths_to_seconds(tenths: int | None) -> Decimal:
    return Decimal(tenths or 0) / 10


def c2_weight_to_grams(value: int | None) -> int | None:
    return None if value is None else value * 10


def parse_c2_datetime(value: str) -> datetime:
    # C2 format: "2025-09-12 18:30:00"
    return datetime.fromisoformat(value)


def validate_hr(bpm: int | None, lo: int = HR_MIN, hi: int = HR_MAX) -> int | None:
    if not bpm or bpm < lo or bpm > hi:
        return None
    return bpm


def hr_quality(raw_avg: int | None) -> str:
    if not raw_avg:
        return "absent"  # 0 or missing: strap not worn
    return "valid" if validate_hr(raw_avg) is not None else "invalid"


def pace_s_per_500(time_s: Decimal, distance_m: int) -> Decimal | None:
    if distance_m <= 0 or time_s <= 0:
        return None
    return time_s * 500 / distance_m


def watts_from_pace(pace_s_500: Decimal) -> Decimal:
    return WATTS_CONSTANT / (pace_s_500 / 500) ** 3


def _q(value: Decimal | None, places: str) -> Decimal | None:
    return None if value is None else value.quantize(Decimal(places), rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class ResolvedTime:
    ended_at_local: datetime
    ended_at_utc: datetime
    tz: str
    tz_source: str


def resolve_end_time(payload: dict[str, Any], default_tz: str) -> ResolvedTime:
    """C2 `date` is the END of the workout, in the athlete's local time."""
    local = parse_c2_datetime(payload["date"])
    tz_name = payload.get("timezone")
    tz_source = "payload"
    try:
        zone = ZoneInfo(tz_name) if tz_name else None
    except (ZoneInfoNotFoundError, ValueError):
        zone = None
    if zone is None:
        tz_name, tz_source = default_tz, "athlete_default"
        zone = ZoneInfo(default_tz)

    if payload.get("date_utc"):
        utc = parse_c2_datetime(payload["date_utc"]).replace(tzinfo=timezone.utc)
    else:
        utc = local.replace(tzinfo=zone).astimezone(timezone.utc)

    return ResolvedTime(local, utc, tz_name, tz_source)


def normalize_athlete(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": payload["id"],
        "username": payload.get("username"),
        "max_heart_rate": payload.get("max_heart_rate"),
        "weight_g": c2_weight_to_grams(payload.get("weight")),
        "gender": payload.get("gender"),
        "dob": payload.get("dob"),
        "raw": payload,
    }


def normalize_workout(payload: dict[str, Any], athlete_id: int, default_tz: str) -> dict[str, Any]:
    t = resolve_end_time(payload, default_tz)

    # Interval workouts: top-level time/distance are WORK only; rest is separate.
    work_time_s = tenths_to_seconds(payload.get("time"))
    work_distance_m = int(payload.get("distance") or 0)
    rest_time_s = tenths_to_seconds(payload.get("rest_time"))
    rest_distance_m = int(payload.get("rest_distance") or 0)

    pace = pace_s_per_500(work_time_s, work_distance_m)
    # The results payload carries no average watts, so it is always derived from work pace.
    watts = watts_from_pace(pace) if pace is not None else None

    hr = payload.get("heart_rate") or {}

    return {
        "id": payload["id"],
        "athlete_id": athlete_id,
        "ended_at_local": t.ended_at_local,
        "ended_at_utc": t.ended_at_utc,
        "tz": t.tz,
        "tz_source": t.tz_source,
        "started_at_utc": t.ended_at_utc - timedelta(seconds=float(work_time_s + rest_time_s)),
        "machine": payload.get("type"),
        "workout_type": payload.get("workout_type"),
        "source": payload.get("source"),
        "work_time_s": work_time_s,
        "work_distance_m": work_distance_m,
        "rest_time_s": rest_time_s,
        "rest_distance_m": rest_distance_m,
        "avg_spm": payload.get("stroke_rate"),
        "stroke_count": payload.get("stroke_count"),
        "drag_factor": payload.get("drag_factor"),
        "avg_pace_s_500": _q(pace, "0.01"),
        "avg_watts": _q(watts, "0.1"),
        "watts_derived": watts is not None,
        "hr_avg": validate_hr(hr.get("average")),
        "hr_ending": validate_hr(hr.get("ending")),
        "hr_rest": validate_hr(hr.get("rest"), lo=HR_REST_MIN),
        "hr_quality": hr_quality(hr.get("average")),
        "calories": payload.get("calories_total"),
        "comments": payload.get("comments"),
        "has_strokes": bool(payload.get("stroke_data")),
        "raw": payload,
    }
