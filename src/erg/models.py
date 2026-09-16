from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Athlete(Base):
    __tablename__ = "athlete"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)  # C2 user id
    username: Mapped[str | None] = mapped_column(Text)
    max_heart_rate: Mapped[int | None] = mapped_column(Integer)
    weight_g: Mapped[int | None] = mapped_column(Integer)  # normalized from C2 decagrams (7500 = 75 kg)
    gender: Mapped[str | None] = mapped_column(Text)
    dob: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class OAuthToken(Base):
    __tablename__ = "oauth_token"

    athlete_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("athlete.id", ondelete="CASCADE"), primary_key=True)
    access_token: Mapped[str] = mapped_column(Text)  # Fernet-encrypted
    refresh_token: Mapped[str] = mapped_column(Text)  # Fernet-encrypted; rotates on every refresh
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    scopes: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Workout(Base):
    __tablename__ = "workout"
    __table_args__ = (
        # Mirrors C2's own dedupe rule (date + time + distance).
        UniqueConstraint("athlete_id", "ended_at_local", "work_time_s", "work_distance_m", name="uq_workout_c2_dedupe"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)  # C2 result id
    athlete_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("athlete.id", ondelete="CASCADE"), index=True)

    ended_at_local: Mapped[datetime] = mapped_column(DateTime(timezone=False))  # C2 `date` = END of workout
    ended_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    tz: Mapped[str] = mapped_column(Text)
    tz_source: Mapped[str] = mapped_column(Text)  # 'payload' | 'athlete_default'
    started_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # derived, approximate for intervals

    machine: Mapped[str | None] = mapped_column(Text)
    workout_type: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(Text)

    work_time_s: Mapped[Decimal] = mapped_column(Numeric(10, 1))
    work_distance_m: Mapped[int] = mapped_column(Integer)
    rest_time_s: Mapped[Decimal] = mapped_column(Numeric(10, 1))
    rest_distance_m: Mapped[int] = mapped_column(Integer)

    avg_spm: Mapped[int | None] = mapped_column(Integer)
    stroke_count: Mapped[int | None] = mapped_column(Integer)
    drag_factor: Mapped[int | None] = mapped_column(Integer)  # null when outside 90-225
    avg_pace_s_500: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    avg_watts: Mapped[Decimal | None] = mapped_column(Numeric(8, 1))
    watts_derived: Mapped[bool] = mapped_column(Boolean, default=False)

    hr_avg: Mapped[int | None] = mapped_column(Integer)  # null when implausible
    hr_ending: Mapped[int | None] = mapped_column(Integer)
    hr_rest: Mapped[int | None] = mapped_column(Integer)
    hr_quality: Mapped[str] = mapped_column(Text)  # 'valid' | 'partial' | 'invalid' | 'absent'

    calories: Mapped[int | None] = mapped_column(Integer)
    comments: Mapped[str | None] = mapped_column(Text)
    has_strokes: Mapped[bool] = mapped_column(Boolean, default=False)

    # Stroke fetch queue: 'not_available' | 'pending' | 'fetched' | 'missing' | 'error'
    stroke_status: Mapped[str] = mapped_column(Text, server_default="not_available", index=True)
    stroke_attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    stroke_error: Mapped[str | None] = mapped_column(Text)
    stroke_warning: Mapped[str | None] = mapped_column(Text)  # parse anomalies on an otherwise successful fetch
    strokes_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    raw: Mapped[dict] = mapped_column(JSONB)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class IntervalSplit(Base):
    """One row per entry of raw.workout.intervals or raw.workout.splits."""

    __tablename__ = "interval_split"

    workout_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("workout.id", ondelete="CASCADE"), primary_key=True)
    idx: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(Text)  # 'interval' | 'split'
    target_type: Mapped[str | None] = mapped_column(Text)  # 'time' | 'distance'
    time_s: Mapped[Decimal] = mapped_column(Numeric(10, 1))  # work time
    distance_m: Mapped[int] = mapped_column(Integer)
    rest_time_s: Mapped[Decimal] = mapped_column(Numeric(10, 1))
    rest_distance_m: Mapped[int] = mapped_column(Integer)
    spm: Mapped[int | None] = mapped_column(Integer)
    hr_avg: Mapped[int | None] = mapped_column(Integer)
    hr_max: Mapped[int | None] = mapped_column(Integer)
    hr_ending: Mapped[int | None] = mapped_column(Integer)
    hr_rest: Mapped[int | None] = mapped_column(Integer)
    calories: Mapped[int | None] = mapped_column(Integer)


class Stroke(Base):
    __tablename__ = "stroke"

    workout_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("workout.id", ondelete="CASCADE"), primary_key=True)
    interval_idx: Mapped[int] = mapped_column(SmallInteger, primary_key=True)  # t/d reset per interval
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)  # order within the whole workout
    t_s: Mapped[Decimal] = mapped_column(Numeric(8, 1))  # cumulative within interval, includes rest
    d_m: Mapped[Decimal] = mapped_column(Numeric(8, 1))  # cumulative within interval, from decimeters
    pace_s_500: Mapped[Decimal | None] = mapped_column(Numeric(6, 1))
    spm: Mapped[int | None] = mapped_column(SmallInteger)
    hr: Mapped[int | None] = mapped_column(SmallInteger)
    is_rest: Mapped[bool] = mapped_column(Boolean)
