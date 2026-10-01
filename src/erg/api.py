import secrets
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import date as Date
from decimal import Decimal
from pathlib import Path

from fastapi import Body, Cookie, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select

from erg.c2 import oauth
from erg.c2.client import C2Client
from erg.config import get_settings
from erg.db import session_scope
from erg.models import (
    Athlete,
    ClassificationOverride,
    DailyLoad,
    IntervalSplit,
    RollingMetric,
    Stroke,
    Workout,
    WorkoutClassification,
    WorkoutEligibility,
    WorkoutMetric,
)
from erg.services import client_for_athlete
from erg.metrics_runner import ACUTE_DAYS, CHRONIC_DAYS
from erg.classify import CLASSES
from erg.describe import describe_workout
from erg import importer
from erg.pipeline import (
    clear_override,
    effective_class,
    effective_classes,
    reclassify_and_refresh,
    set_classification_settings,
    set_override,
)
from erg.compare import DEFAULT_POINTS, DEFAULT_SEGMENT_M, common_grid, resample, split_attribution, track_from_samples
from erg.metrics import StrokePoint, work_samples
from erg.strokes import downsample, with_elapsed
from erg.summary import week_summary
from erg.sync import backfill, fetch_strokes, upsert_athlete
from erg.session import clear as clear_session, current_athlete, issue as issue_session
from erg.tokens import store_token

@asynccontextmanager
async def lifespan(_app):
    # The MCP SDK's HTTP transport needs its session manager running for the app's lifetime.
    from erg.mcp_server import start

    async with start().run():
        yield


app = FastAPI(title="Erg Analytics", lifespan=lifespan)

WEB_DIR = Path(__file__).parent / "web"
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.middleware("http")
async def revalidate_static(request, call_next):
    """Browsers otherwise keep serving an old app.js after an update. no-cache still uses the
    ETag, so an unchanged file costs a 304, not a download."""
    response = await call_next(request)
    if request.url.path.startswith("/static/") or request.url.path in ("/", "/replay"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/", include_in_schema=False)
@app.get("/replay", include_in_schema=False)
def replay_ui():
    """Race replay: distance-aligned ghost racing over your own pieces."""
    return FileResponse(WEB_DIR / "index.html")

STATE_COOKIE = "c2_oauth_state"


MISSING_KEYS_PAGE = """<!doctype html><meta charset="utf-8"><title>Concept2 keys missing</title>
<link rel="stylesheet" href="/static/app.css">
<header><h1>Concept2 keys missing</h1></header>
<section class="panel"><p>This app needs a Concept2 client ID and secret before anyone can sign in.</p>
<p>Add them to a file called <code>.env</code> in the project folder:</p>
<pre>C2_CLIENT_ID=...
C2_CLIENT_SECRET=...</pre>
<p>then restart the app. If someone shared this project with you, ask them for these two values.</p></section>"""


@app.get("/status")
def app_status():
    """Public: what the UI needs to know before anyone signs in."""
    settings = get_settings()
    return {
        "local_mode": settings.local_mode,
        "concept2_configured": bool(settings.c2_client_id and settings.c2_client_secret),
    }


@app.get("/auth/login")
def login():
    settings = get_settings()
    if not (settings.c2_client_id and settings.c2_client_secret):
        return HTMLResponse(MISSING_KEYS_PAGE, status_code=503)
    state = secrets.token_urlsafe(24)
    resp = RedirectResponse(oauth.authorize_url(settings, state))
    resp.set_cookie(STATE_COOKIE, state, max_age=600, httponly=True, samesite="lax")
    return resp


@app.get("/auth/callback")
def callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    c2_oauth_state: str | None = Cookie(default=None),
):
    if error:
        raise HTTPException(400, f"authorization denied: {error}")
    if not code or not state or not c2_oauth_state or not secrets.compare_digest(state, c2_oauth_state):
        raise HTTPException(400, "invalid OAuth state")

    settings = get_settings()
    token = oauth.exchange_code(settings, code)
    client = C2Client(settings, lambda: token.access_token)
    try:
        me = client.get_me()
    finally:
        client.close()

    with session_scope() as s:
        athlete_id = upsert_athlete(s, me)
        store_token(s, athlete_id, token)

    # Import (or catch up) straight away, so a first-time user never lands on an empty page.
    importer.begin(athlete_id)

    resp = RedirectResponse("/replay")
    resp.delete_cookie(STATE_COOKIE)
    issue_session(resp, athlete_id, settings)
    return resp


@app.post("/auth/logout")
def logout():
    resp = JSONResponse({"signed_out": True})
    clear_session(resp)
    return resp


@app.get("/athletes/me")
def get_athlete(athlete_id: int = Depends(current_athlete)):
    with session_scope() as s:
        athlete = s.get(Athlete, athlete_id)
        if athlete is None:
            raise HTTPException(404, "athlete not found")
        return {
            "id": athlete.id,
            "username": athlete.username,
            "max_heart_rate": athlete.effective_max_heart_rate,
            "weight_g": athlete.effective_weight_g,
            "workouts": s.execute(
                select(func.count()).select_from(Workout).where(Workout.athlete_id == athlete_id)
            ).scalar(),
            "local_mode": get_settings().local_mode,
        }


@app.post("/athletes/me/import")
def start_import(athlete_id: int = Depends(current_athlete)):
    """Pull workouts and strokes from Concept2, then classify and compute metrics, in the background."""
    importer.begin(athlete_id)
    return importer.step(athlete_id, budget_s=10)


@app.post("/athletes/me/mcp-token")
def create_mcp_token(request: Request, athlete_id: int = Depends(current_athlete)):
    """Mint a personal token for the MCP endpoint (replacing any previous one). Shown once."""
    from erg.mcp_server import issue_token

    token = issue_token(athlete_id)
    base = str(request.base_url).rstrip("/")
    return {
        "token": token,
        "connector_url": f"{base}/mcp/?key={token}",
        "claude_code_command": (
            f'claude mcp add --transport http erg {base}/mcp/ --header "Authorization: Bearer {token}"'
        ),
    }


@app.delete("/athletes/me/mcp-token")
def delete_mcp_token(athlete_id: int = Depends(current_athlete)):
    from erg.mcp_server import revoke_token

    revoke_token(athlete_id)
    return {"revoked": True}


@app.post("/athletes/me/import/step")
def import_step(athlete_id: int = Depends(current_athlete)):
    """Do the next batch of import work (up to ~25s) and return progress. The page calls this
    repeatedly; nothing runs in the background, so it works on serverless hosting."""
    return importer.step(athlete_id)


@app.get("/athletes/me/import")
def import_status(athlete_id: int = Depends(current_athlete)):
    return importer.status(athlete_id)


@app.post("/athletes/me/backfill")
def run_backfill(athlete_id: int = Depends(current_athlete)):
    settings = get_settings()
    client = client_for_athlete(settings, athlete_id)
    try:
        with session_scope() as s:
            stats = backfill(s, client, settings)
    finally:
        client.close()
    return asdict(stats)


@app.post("/athletes/me/strokes/fetch")
def run_stroke_fetch(limit: int | None = None, athlete_id: int = Depends(current_athlete)):
    settings = get_settings()
    client = client_for_athlete(settings, athlete_id)
    try:
        with session_scope() as s:
            stats = fetch_strokes(s, client, athlete_id, limit=limit)
    finally:
        client.close()
    return asdict(stats)


def stroke_interval_counts(s, workouts) -> dict[int, int]:
    """Intervals seen in the stroke stream, only for workouts whose summary is truncated
    (those carry a stroke_warning). Everyone else's summary is complete, so skip the query."""
    warned = [w.id for w in workouts if w.stroke_warning]
    if not warned:
        return {}
    return dict(
        s.execute(
            select(Stroke.workout_id, func.max(Stroke.interval_idx) + 1)
            .where(Stroke.workout_id.in_(warned))
            .group_by(Stroke.workout_id)
        ).all()
    )


def owned_workout(s, workout_id: int, athlete_id: int) -> Workout:
    """A workout the signed-in athlete owns. Someone else's id reads as missing."""
    workout = s.get(Workout, workout_id)
    if workout is None or workout.athlete_id != athlete_id:
        raise HTTPException(404, "workout not found")
    return workout


@app.get("/workouts/{workout_id}/strokes")
def get_strokes(
    workout_id: int,
    downsample_to: int | None = Query(None, alias="downsample", ge=3, description="max points (LTTB on time vs pace)"),
    include_rest: bool = True,
    athlete_id: int = Depends(current_athlete),
):
    with session_scope() as s:
        workout = owned_workout(s, workout_id, athlete_id)
        strokes = s.execute(select(Stroke).where(Stroke.workout_id == workout_id).order_by(Stroke.seq)).scalars().all()
        intervals = s.execute(
            select(IntervalSplit).where(IntervalSplit.workout_id == workout_id).order_by(IntervalSplit.idx)
        ).scalars().all()

        points = with_elapsed(strokes)
        if not include_rest:
            points = [p for p in points if not p["is_rest"]]
        total = len(points)
        if downsample_to:
            points = downsample(points, downsample_to)

        return {
            "workout_id": workout_id,
            "workout_type": workout.workout_type,
            "stroke_status": workout.stroke_status,
            "stroke_warning": workout.stroke_warning,
            "intervals": [
                {
                    "idx": i.idx,
                    "kind": i.kind,
                    "target_type": i.target_type,
                    "time_s": float(i.time_s),
                    "distance_m": i.distance_m,
                    "rest_time_s": float(i.rest_time_s),
                    "hr_avg": i.hr_avg,
                    "hr_rest": i.hr_rest,
                }
                for i in intervals
            ],
            "total_points": total,
            "points": points,
        }


@app.get("/workouts/compare")
def compare_workouts(
    ids: str = Query(..., description="comma-separated workout ids; the first is the reference"),
    points: int = Query(DEFAULT_POINTS, ge=10, le=2000),
    segment_m: float = Query(DEFAULT_SEGMENT_M, gt=0),
    athlete_id: int = Depends(current_athlete),
):
    """Distance-aligned series plus split attribution, ready to overlay."""
    try:
        workout_ids = [int(part) for part in ids.split(",") if part.strip()]
    except ValueError:
        raise HTTPException(400, "ids must be comma-separated integers") from None
    if not 2 <= len(workout_ids) <= 6:
        raise HTTPException(400, "compare between 2 and 6 workouts")

    with session_scope() as s:
        pieces, tracks = [], []
        for workout_id in workout_ids:
            w = owned_workout(s, workout_id, athlete_id)
            strokes = s.execute(
                select(Stroke).where(Stroke.workout_id == workout_id, Stroke.is_rest.is_(False)).order_by(Stroke.seq)
            ).scalars().all()
            if not strokes:
                raise HTTPException(400, f"workout {workout_id} has no stroke data to compare")
            samples = work_samples(
                [
                    StrokePoint(
                        interval_idx=st.interval_idx,
                        t_s=float(st.t_s),
                        d_m=float(st.d_m),
                        pace_s_500=float(st.pace_s_500) if st.pace_s_500 is not None else None,
                        spm=st.spm,
                        hr=st.hr,
                    )
                    for st in strokes
                ]
            )
            cls, _ = effective_class(s, workout_id)
            tracks.append(
                track_from_samples(samples, float(w.work_distance_m), float(w.work_time_s))
            )
            pieces.append(
                {
                    "workout_id": w.id,
                    "date": w.ended_at_local.date(),
                    "class": cls,
                    "description": describe_workout(w, stroke_interval_counts(s, [w]).get(w.id)),
                    "work_distance_m": w.work_distance_m,
                    "work_time_s": float(w.work_time_s),
                    "avg_pace_s_500": float(w.avg_pace_s_500) if w.avg_pace_s_500 else None,
                    "drag_factor": w.drag_factor,
                    "hr_avg": w.hr_avg,
                    "comments": w.comments,
                }
            )

        grid = common_grid(tracks, points)
        if not grid:
            raise HTTPException(400, "no common distance to compare over")
        aligned_distance = grid[-1]

        reference = tracks[0]
        for i, (piece, track) in enumerate(zip(pieces, tracks)):
            piece["series"] = resample(track, grid)
            piece["aligned_time_s"] = piece["series"]["time_s"][-1]
            piece["is_reference"] = i == 0
            piece["end_anchored"] = track.anchored
            piece["splits"] = split_attribution(reference, track, aligned_distance, segment_m)
            piece["total_delta_s"] = (
                None
                if piece["aligned_time_s"] is None or pieces[0]["aligned_time_s"] is None
                else round(piece["aligned_time_s"] - pieces[0]["aligned_time_s"], 2)
            )

        drags = {p["drag_factor"] for p in pieces if p["drag_factor"]}
        return {
            "aligned_distance_m": aligned_distance,
            "segment_m": segment_m,
            "reference_id": workout_ids[0],
            "pieces": pieces,
            "note": (
                f"drag factor differs across these pieces ({sorted(drags)}); pace is not strictly comparable"
                if len(drags) > 1
                else None
            ),
        }


@app.get("/workouts/{workout_id}")
def get_workout(workout_id: int, athlete_id: int = Depends(current_athlete)):
    with session_scope() as s:
        w = owned_workout(s, workout_id, athlete_id)
        cls, overridden = effective_class(s, workout_id)
        classification = s.get(WorkoutClassification, workout_id)
        eligibility = s.execute(
            select(WorkoutEligibility).where(WorkoutEligibility.workout_id == workout_id)
        ).scalars().all()
        return {
            "id": w.id,
            "ended_at_local": w.ended_at_local,
            "ended_at_utc": w.ended_at_utc,
            "workout_type": w.workout_type,
            "description": describe_workout(w, stroke_interval_counts(s, [w]).get(w.id)),
            "work_time_s": float(w.work_time_s),
            "work_distance_m": w.work_distance_m,
            "rest_time_s": float(w.rest_time_s),
            "avg_pace_s_500": float(w.avg_pace_s_500) if w.avg_pace_s_500 else None,
            "avg_watts": float(w.avg_watts) if w.avg_watts else None,
            "avg_spm": w.avg_spm,
            "hr_avg": w.hr_avg,
            "hr_quality": w.hr_quality,
            "drag_factor": w.drag_factor,
            "comments": w.comments,
            "stroke_status": w.stroke_status,
            "stroke_warning": w.stroke_warning,
            "classification": {
                "class": cls,
                "overridden": overridden,
                "classifier_class": classification.workout_class if classification else None,
                "confidence": float(classification.confidence) if classification else None,
                "reason": classification.reason if classification else None,
            },
            "eligibility": {e.metric: {"eligible": e.eligible, "reason": e.reason} for e in eligibility},
            "metrics": _metrics_dict(s.get(WorkoutMetric, workout_id)),
        }


def _metrics_dict(m) -> dict | None:
    if m is None:
        return None
    as_float = lambda v: float(v) if v is not None else None  # noqa: E731
    return {
        "ef": as_float(m.ef),
        "work_watts": as_float(m.work_watts),
        "work_hr": as_float(m.work_hr),
        "decoupling_pct": as_float(m.decoupling_pct),
        "ef_first_half": as_float(m.ef_first_half),
        "ef_second_half": as_float(m.ef_second_half),
        "pace_cv": as_float(m.pace_cv),
        "spm_cv": as_float(m.spm_cv),
        "first_half_pace": as_float(m.first_half_pace),
        "second_half_pace": as_float(m.second_half_pace),
        "fade_onset_m": m.fade_onset_m,
        "dps_m": as_float(m.dps_m),
        "dps_cv": as_float(m.dps_cv),
        "dps_source": m.dps_source,
        "hrr_bpm": as_float(m.hrr_bpm),
        "hrr_rest_s": as_float(m.hrr_rest_s),
        "hrr_intervals": m.hrr_intervals,
        "kj": as_float(m.kj),
        "trimp": as_float(m.trimp),
        "metric_version": m.metric_version,
    }


@app.post("/workouts/{workout_id}/classification")
def override_classification(
    workout_id: int,
    workout_class: str = Body(embed=True),
    note: str | None = Body(None, embed=True),
    athlete_id: int = Depends(current_athlete),
):
    """Manual override. Always wins over the classifier and survives recomputation."""
    if workout_class not in CLASSES:
        raise HTTPException(422, f"workout_class must be one of {', '.join(CLASSES)}")
    with session_scope() as s:
        owned_workout(s, workout_id, athlete_id)
        before = effective_classes(s, athlete_id)
        set_override(s, workout_id, workout_class, note)
        _, changed = reclassify_and_refresh(s, athlete_id, before)
        cls, overridden = effective_class(s, workout_id)
    return {"workout_id": workout_id, "class": cls, "overridden": overridden, "reclassified": changed}


@app.delete("/workouts/{workout_id}/classification")
def clear_classification(workout_id: int, athlete_id: int = Depends(current_athlete)):
    """Remove a manual override and hand the piece back to the classifier."""
    with session_scope() as s:
        owned_workout(s, workout_id, athlete_id)
        before = effective_classes(s, athlete_id)
        clear_override(s, workout_id)
        _, changed = reclassify_and_refresh(s, athlete_id, before)
        cls, overridden = effective_class(s, workout_id)
    return {"workout_id": workout_id, "class": cls, "overridden": overridden, "reclassified": changed}


def _settings_payload(athlete: Athlete) -> dict:
    as_float = lambda v: float(v) if v is not None else None  # noqa: E731
    return {
        "steady_pace_auto": as_float(athlete.steady_pace_auto),
        "steady_pace_override": as_float(athlete.steady_pace_override),
        "steady_pace": as_float(athlete.effective_steady_pace),
        "interval_margin_s": as_float(athlete.interval_margin_s),
        "interval_threshold": as_float(athlete.interval_threshold),
        "classes": list(CLASSES),
    }


@app.get("/athletes/me/settings")
def get_settings_endpoint(athlete_id: int = Depends(current_athlete)):
    with session_scope() as s:
        return _settings_payload(s.get(Athlete, athlete_id))


@app.put("/athletes/me/settings")
def update_settings(
    steady_pace_s_500: float | None = Body(None, embed=True, description="null or omitted: keep; use auto=true to clear"),
    auto: bool = Body(False, embed=True, description="go back to the learned steady pace"),
    interval_margin_s: float | None = Body(None, embed=True),
    athlete_id: int = Depends(current_athlete),
):
    """Change classification settings, then reclassify and recompute everything."""
    with session_scope() as s:
        before = effective_classes(s, athlete_id)
        try:
            set_classification_settings(
                s,
                athlete_id,
                Decimal(str(steady_pace_s_500)) if steady_pace_s_500 is not None else None,
                Decimal(str(interval_margin_s)) if interval_margin_s is not None else None,
                clear_steady_pace=auto,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        stats, changed = reclassify_and_refresh(s, athlete_id, before, full_metrics=True)
        payload = _settings_payload(s.get(Athlete, athlete_id))
    return payload | {"classes_count": dict(stats.classes), "reclassified": changed}


@app.get("/workouts")
def list_workouts(
    workout_class: str | None = Query(None, alias="class"),
    eligible_for: str | None = Query(None, description="metric name, e.g. ef or decoupling"),
    limit: int = Query(50, le=1000),
    athlete_id: int = Depends(current_athlete),
):
    with session_scope() as s:
        # Manual overrides win over the classifier, so filter on the effective class.
        effective = func.coalesce(ClassificationOverride.workout_class, WorkoutClassification.workout_class)
        q = (
            select(
                Workout,
                WorkoutClassification,
                effective.label("effective_class"),
                ClassificationOverride.workout_class.label("override_class"),
                ClassificationOverride.note.label("override_note"),
            )
            .join(WorkoutClassification, WorkoutClassification.workout_id == Workout.id, isouter=True)
            .join(ClassificationOverride, ClassificationOverride.workout_id == Workout.id, isouter=True)
            .where(Workout.athlete_id == athlete_id)
        )
        if workout_class:
            q = q.where(effective == workout_class)
        if eligible_for:
            q = q.join(
                WorkoutEligibility,
                (WorkoutEligibility.workout_id == Workout.id) & (WorkoutEligibility.metric == eligible_for),
            ).where(WorkoutEligibility.eligible)
        rows = s.execute(q.order_by(Workout.ended_at_utc.desc()).limit(limit)).all()
        stroke_counts = stroke_interval_counts(s, [row[0] for row in rows])
        return [
            {
                "id": w.id,
                "date": w.ended_at_local.date(),
                "class": eff,
                "classifier_class": c.workout_class if c else None,
                "classifier_reason": c.reason if c else None,
                "confidence": float(c.confidence) if c else None,
                # Presence of an override row, not a class comparison: an override can
                # agree with the classifier and still be the athlete's decision.
                "overridden": override_class is not None,
                "override_note": override_note,
                "workout_type": w.workout_type,
                "description": describe_workout(w, stroke_counts.get(w.id)),
                "work_distance_m": w.work_distance_m,
                "work_time_s": float(w.work_time_s),
                "rest_time_s": float(w.rest_time_s),
                "avg_pace_s_500": float(w.avg_pace_s_500) if w.avg_pace_s_500 else None,
                "avg_spm": w.avg_spm,
                "hr_avg": w.hr_avg,
                "has_strokes": w.has_strokes,
            }
            for w, c, eff, override_class, override_note in rows
        ]


METRIC_COLUMNS = {
    "ef": WorkoutMetric.ef,
    "decoupling": WorkoutMetric.decoupling_pct,
    "hrr": WorkoutMetric.hrr_bpm,
    "dps": WorkoutMetric.dps_m,
    "pace_cv": WorkoutMetric.pace_cv,
    "kj": WorkoutMetric.kj,
    "trimp": WorkoutMetric.trimp,
}


@app.get("/metrics/trend")
def metric_trend(
    name: str = Query("ef", description=f"one of {', '.join(METRIC_COLUMNS)}"),
    workout_class: str | None = Query(None, alias="class"),
    from_: Date | None = Query(None, alias="from"),
    to: Date | None = None,
    hrr_rest_s: float | None = Query(None, description="HRR only compares at matched rest length"),
    athlete_id: int = Depends(current_athlete),
):
    """One point per eligible workout. No smoothing: the caller decides how to present it."""
    column = METRIC_COLUMNS.get(name)
    if column is None:
        raise HTTPException(400, f"unknown metric {name!r}; try one of {', '.join(METRIC_COLUMNS)}")

    with session_scope() as s:
        effective = func.coalesce(ClassificationOverride.workout_class, WorkoutClassification.workout_class)
        q = (
            select(Workout.id, Workout.ended_at_local, effective, column, Workout.drag_factor, Workout.work_time_s)
            .join(WorkoutMetric, WorkoutMetric.workout_id == Workout.id)
            .join(WorkoutClassification, WorkoutClassification.workout_id == Workout.id, isouter=True)
            .join(ClassificationOverride, ClassificationOverride.workout_id == Workout.id, isouter=True)
            .where(column.is_not(None), Workout.athlete_id == athlete_id)
            .order_by(Workout.ended_at_local)
        )
        if workout_class:
            q = q.where(effective == workout_class)
        if from_:
            q = q.where(Workout.ended_at_local >= from_)
        if to:
            q = q.where(Workout.ended_at_local <= to)
        if name == "hrr" and hrr_rest_s is not None:
            q = q.where(WorkoutMetric.hrr_rest_s == hrr_rest_s)

        points = [
            {
                "workout_id": wid,
                "date": ended.date(),
                "class": cls,
                "value": float(value),
                "drag_factor": drag,
                "work_time_s": float(work_time),
            }
            for wid, ended, cls, value, drag, work_time in s.execute(q).all()
        ]
    note = None
    if name == "hrr" and hrr_rest_s is None:
        note = "HR recovery depends on rest length; filter with hrr_rest_s to compare like with like"
    return {"metric": name, "points": points, "note": note}


@app.get("/load/daily")
def load_daily(
    from_: Date | None = Query(None, alias="from"),
    to: Date | None = None,
    athlete_id: int = Depends(current_athlete),
):
    with session_scope() as s:
        q = select(DailyLoad).where(DailyLoad.athlete_id == athlete_id).order_by(DailyLoad.date)
        if from_:
            q = q.where(DailyLoad.date >= from_)
        if to:
            q = q.where(DailyLoad.date <= to)
        return [
            {
                "date": d.date,
                "sessions": d.sessions,
                "work_time_s": float(d.work_time_s),
                "work_distance_m": d.work_distance_m,
                "kj": float(d.kj) if d.kj is not None else None,
                "trimp": float(d.trimp) if d.trimp is not None else None,
            }
            for d in s.execute(q).scalars()
        ]


@app.get("/load/acwr")
def load_acwr(date: Date | None = None, athlete_id: int = Depends(current_athlete)):
    """Acute:chronic workload ratio — a descriptive load-balance indicator, not advice."""
    with session_scope() as s:
        q = select(RollingMetric).where(
            RollingMetric.athlete_id == athlete_id, RollingMetric.metric_name.in_(["acwr", "kj", "monotony"])
        )
        if date:
            q = q.where(RollingMetric.date == date)
        else:
            latest = s.execute(
                select(func.max(RollingMetric.date)).where(RollingMetric.athlete_id == athlete_id)
            ).scalar()
            if latest is None:
                return {}
            q = q.where(RollingMetric.date == latest)
        rows = s.execute(q).scalars().all()
        if not rows:
            raise HTTPException(404, "no rolling metrics for that date")
        out = {"date": rows[0].date, "acute_days": ACUTE_DAYS, "chronic_days": CHRONIC_DAYS}
        for r in rows:
            key = r.metric_name if r.metric_name != "kj" else f"kj_{r.window_days}d"
            out[key] = float(r.value) if r.value is not None else None
        return out


@app.get("/summary/week")
def summary_week(date: Date | None = None, athlete_id: int = Depends(current_athlete)):
    """Digest payload for one Monday-Sunday week. Descriptive only, no recommendations."""
    with session_scope() as s:
        return week_summary(s, athlete_id, date)


# MCP endpoint for Claude, behind per-athlete token auth (see erg.mcp_server).
from erg.mcp_server import asgi_app as _mcp_asgi_app  # noqa: E402

app.mount("/mcp", _mcp_asgi_app())
