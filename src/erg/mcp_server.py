"""MCP server: gives Claude direct, read-mostly access to the analytics.

Served over streamable HTTP at /mcp from inside the web app, in stateless mode so it runs on
serverless hosting. Tools wrap the analytical layer (rowing notation, metrics, split
attribution), not raw tables, so Claude asks questions in rowing terms.

Auth: each athlete mints a personal token in the UI ("Connect Claude"). It is accepted as a
bearer header (Claude Code) or a `?key=` query parameter (phone connectors can't send
headers). Only a hash is stored. The token resolves to one athlete; every tool is scoped to it.
"""

import hashlib
import secrets
import time
from datetime import date as Date

from fastapi.encoders import jsonable_encoder
from fastapi import HTTPException
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import select
from starlette.responses import JSONResponse

from erg.db import session_scope
from erg.models import McpToken

INSTRUCTIONS = """Rowing analytics for the signed-in athlete's Concept2 logbook.

Conventions: paces are seconds per 500m (shown as m:ss.s), distances in metres, times in seconds.
Workouts are described in rowing notation: 3x20' / 2'r is three 20-minute pieces with 2 minutes
rest; 5k-4k-3k-2k-1k is a ladder; ~ before a rest means the rest varied.
Classes: test_2k, test_6k, test_10k (solo test distances), interval (faster than the athlete's
interval threshold), steady, short_piece, unknown.
Metrics: EF = watts per heartbeat on steady work; decoupling = % drop in EF from first to second
half (under ~5% is good durability); HR recovery = bpm drop across interval rests, only comparable
at the same rest length; DPS = metres per stroke.

Framing: this is descriptive analytics of the athlete's own training. Describe what the data
shows; do not prescribe training, readiness or health conclusions."""

mcp = FastMCP(
    "erg-analytics",
    instructions=INSTRUCTIONS,
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
    # The SDK's DNS-rebinding guard only admits localhost hosts by default, which would also
    # reject the deployed domain. It protects unauthenticated local servers from malicious web
    # pages; this endpoint demands a secret token on every request, which such a page can't know.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

SYNC_BUDGET_S = 200.0  # stay inside one request's time limit; call sync again to continue


# ---- tokens -------------------------------------------------------------------

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_token(athlete_id: int) -> str:
    """A new personal token for this athlete; replaces (revokes) any previous one."""
    token = "erg_" + secrets.token_urlsafe(32)
    with session_scope() as s:
        row = s.get(McpToken, athlete_id)
        if row is None:
            s.add(McpToken(athlete_id=athlete_id, token_hash=_hash(token)))
        else:
            row.token_hash = _hash(token)
    return token


def revoke_token(athlete_id: int) -> None:
    with session_scope() as s:
        row = s.get(McpToken, athlete_id)
        if row is not None:
            s.delete(row)


def athlete_for_token(token: str | None) -> int | None:
    if not token:
        return None
    with session_scope() as s:
        return s.execute(select(McpToken.athlete_id).where(McpToken.token_hash == _hash(token))).scalar()


def _token_from_scope(scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            text = value.decode()
            if text.lower().startswith("bearer "):
                return text[7:].strip()
    from urllib.parse import parse_qs

    keys = parse_qs(scope.get("query_string", b"").decode()).get("key")
    return keys[0] if keys else None


_inner_app = None


def start():
    """A fresh transport for this app lifetime. The SDK's session manager can only run once per
    instance, and the web app's lifespan can run more than once in a process (tests, restarts)."""
    global _inner_app
    mcp._session_manager = None
    _inner_app = mcp.streamable_http_app()
    return mcp.session_manager


class TokenAuth:
    """ASGI wrapper: rejects requests without a valid token, and records whose they are."""

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            athlete_id = athlete_for_token(_token_from_scope(scope))
            if athlete_id is None:
                response = JSONResponse(
                    {"error": "missing or invalid token; create one with Connect Claude in the app"},
                    status_code=401,
                )
                await response(scope, receive, send)
                return
            scope.setdefault("state", {})["athlete_id"] = athlete_id
        if _inner_app is None:
            start()
        await _inner_app(scope, receive, send)


def _athlete(ctx: Context) -> int:
    request = ctx.request_context.request
    athlete_id = getattr(getattr(request, "state", None), "athlete_id", None) if request else None
    if athlete_id is None:
        raise ValueError("not authenticated")
    return athlete_id


def _call(fn, **kwargs):
    """Run an API function and turn its HTTP errors into tool errors."""
    try:
        return jsonable_encoder(fn(**kwargs))
    except HTTPException as exc:
        raise ValueError(str(exc.detail)) from None


# ---- tools --------------------------------------------------------------------

@mcp.tool()
def find_pieces(
    ctx: Context,
    workout_class: str | None = None,
    description_contains: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    min_distance_m: int | None = None,
    max_distance_m: int | None = None,
    limit: int = 25,
) -> list[dict]:
    """Search the athlete's workouts, newest first.

    workout_class: test_2k, test_6k, test_10k, interval, steady, short_piece or unknown.
    description_contains: matches rowing notation, e.g. "3x20'", "5k-4k", "x500m".
    Dates are YYYY-MM-DD (inclusive).
    """
    from erg.api import list_workouts

    rows = _call(list_workouts, workout_class=workout_class, eligible_for=None, limit=1000, athlete_id=_athlete(ctx))
    needle = (description_contains or "").lower()
    out = []
    for w in rows:
        if needle and needle not in (w.get("description") or "").lower():
            continue
        if from_date and w["date"] < from_date:
            continue
        if to_date and w["date"] > to_date:
            continue
        if min_distance_m is not None and w["work_distance_m"] < min_distance_m:
            continue
        if max_distance_m is not None and w["work_distance_m"] > max_distance_m:
            continue
        out.append({k: w.get(k) for k in (
            "id", "date", "description", "class", "overridden", "work_distance_m", "work_time_s",
            "avg_pace_s_500", "avg_spm", "hr_avg",
        )})
        if len(out) >= limit:
            break
    return out


@mcp.tool()
def get_piece(ctx: Context, workout_id: int) -> dict:
    """One workout in full: description, totals, class and why, every metric, and the
    per-interval summary (time, distance, rest, HR) from the logbook."""
    from erg.api import get_strokes, get_workout

    athlete_id = _athlete(ctx)
    piece = _call(get_workout, workout_id=workout_id, athlete_id=athlete_id)
    strokes = _call(get_strokes, workout_id=workout_id, downsample_to=3, include_rest=False, athlete_id=athlete_id)
    piece["intervals"] = strokes["intervals"]
    return piece


@mcp.tool()
def compare_pieces(ctx: Context, workout_ids: list[int], segment_m: int = 500) -> dict:
    """Race two to six pieces against each other on a common distance grid. The first is the
    reference. Returns each piece's total and per-segment time gained or lost (positive = slower
    than the reference), i.e. where the difference came from."""
    from erg.api import compare_workouts

    result = _call(
        compare_workouts,
        ids=",".join(str(i) for i in workout_ids),
        points=50,
        segment_m=float(segment_m),
        athlete_id=_athlete(ctx),
    )
    for piece in result["pieces"]:
        piece.pop("series", None)  # chart data; too bulky for a conversation
    result.pop("note", None)
    return result


@mcp.tool()
def get_trend(
    ctx: Context,
    metric: str = "ef",
    workout_class: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    hrr_rest_s: float | None = None,
) -> dict:
    """One value per eligible workout over time.

    metric: ef, decoupling, hrr, dps, pace_cv, kj or trimp. For hrr, pass hrr_rest_s (e.g. 120)
    to compare like with like; recovery depends heavily on rest length.
    """
    from erg.api import metric_trend

    return _call(
        metric_trend,
        name=metric,
        workout_class=workout_class,
        from_=Date.fromisoformat(from_date) if from_date else None,
        to=Date.fromisoformat(to_date) if to_date else None,
        hrr_rest_s=hrr_rest_s,
        athlete_id=_athlete(ctx),
    )


@mcp.tool()
def weekly_summary(ctx: Context, date: str | None = None) -> dict:
    """Monday-Sunday summary for the week containing `date` (YYYY-MM-DD; default the latest
    week trained): volume, class mix, EF against the prior 4 weeks, decoupling, HR recovery,
    training load, and data-quality notes."""
    from erg.api import summary_week

    return _call(summary_week, date=Date.fromisoformat(date) if date else None, athlete_id=_athlete(ctx))


@mcp.tool()
def training_load(ctx: Context, from_date: str | None = None, to_date: str | None = None) -> dict:
    """Daily load (sessions, metres, seconds, kJ, TRIMP) plus the latest acute:chronic ratio and
    monotony. Descriptive only."""
    from erg.api import load_acwr, load_daily

    athlete_id = _athlete(ctx)
    daily = _call(
        load_daily,
        from_=Date.fromisoformat(from_date) if from_date else None,
        to=Date.fromisoformat(to_date) if to_date else None,
        athlete_id=athlete_id,
    )
    return {"daily": daily, "latest": _call(load_acwr, date=None, athlete_id=athlete_id)}


@mcp.tool()
def sync_from_concept2(ctx: Context) -> dict:
    """Fetch new or edited workouts from Concept2, then reclassify and recompute metrics.
    Returns progress; if state is still 'running', call it again to continue."""
    from erg import importer

    athlete_id = _athlete(ctx)
    result = importer.begin(athlete_id)
    deadline = time.monotonic() + SYNC_BUDGET_S
    while result["state"] == "running" and time.monotonic() < deadline:
        result = importer.step(athlete_id)
    return jsonable_encoder(result)


def asgi_app():
    """The MCP endpoint as an ASGI app, behind token auth. Mount at /mcp; call start() from the
    web app's lifespan and run the returned session manager for its duration."""
    return TokenAuth()
