# RowingAnalytics
experimenting with concept2 API for training data analytics projects

See [erg-analytics-data-layer-plan.md](erg-analytics-data-layer-plan.md) for the full spec. **Current status: Phases 1-4 done** (ingest, strokes, classification + eligibility, metrics engine), plus the weekly summary and the race replay UI.

## Setup

```sh
uv sync
docker compose up -d                 # Postgres (creates `erg` and `erg_test`)
cp .env.example .env                 # fill in C2 client id/secret + TOKEN_ENCRYPTION_KEY
uv run alembic upgrade head
```

Register an app at https://log.concept2.com/developers/keys with redirect URI `http://localhost:8000/auth/callback`. The app only reads data, so no live-API approval is needed; that is required only for apps that write results.

## Run

Double-click **`run.cmd`**, or from a terminal:

```powershell
.un.cmd              # start everything and open the race replay UI
.un.cmd -Sync        # pull new workouts from Concept2 first, then recompute
.un.cmd -Recompute   # re-run classification + metrics before starting
.un.cmd -NoBrowser   # don't open a browser
.un.cmd -StopDb      # also stop Postgres when the server exits
.un.cmd -Port 8001   # use a different port
```

It starts Docker Desktop and Postgres if needed, applies migrations, launches the API and
opens http://localhost:8000/replay. On a fresh database it opens the Concept2 login instead.
Ctrl+C stops the server; Postgres keeps running unless `-StopDb` is passed.

### Or by hand

```sh
uv run uvicorn erg.api:app --reload  # race replay UI: http://localhost:8000/replay
                                     # first run: http://localhost:8000/auth/login
                                     # GET /workouts?class=steady&eligible_for=ef
                                     # GET /metrics/trend?name=ef&class=steady
                                     # GET /load/daily?from=2026-09-01   GET /load/acwr
                                     # GET /summary/week      GET /workouts/compare?ids=1,2,3
                                     # GET /workouts/{id}   (+ classification + eligibility)
                                     # GET /workouts/{id}/strokes?downsample=200&include_rest=false
                                     # POST /workouts/{id}/classification  {"workout_class": "steady"}
uv run erg sync                      # after logging new workouts: backfill + fetch-strokes
uv run erg backfill                  # page all rower results into Postgres (safe to re-run)
uv run erg fetch-strokes             # drain the stroke fetch queue (new/edited workouts)
uv run erg profile --max-hr 193 --weight-lb 200   # corrections to the C2 profile
uv run erg classify                  # classify sessions + flag metric eligibility (no API calls)
uv run erg override 123456 steady --note "hard steady, not a test"
uv run erg metrics                   # EF, decoupling, HRR, pacing, DPS, daily load, ACWR
uv run erg week                      # weekly summary (same payload as /summary/week)
uv run erg renormalize               # re-derive normalized columns from stored raw payloads (no API calls)
```

## Test

```sh
uv run pytest                        # DB tests skip if Postgres isn't running
```

## Layout

- `src/erg/c2/` — Concept2 client: OAuth, rate-limited/retrying API access, pagination
- `src/erg/normalize.py` — raw C2 units/timezones → normalized values (the only place raw units exist)
- `src/erg/tokens.py` — encrypted token storage; refresh with rotation under a row lock
- `src/erg/sync.py` — idempotent athlete/workout upserts, backfill, stroke fetch queue
- `src/erg/strokes.py` — interval-aware stroke parsing (work/rest labels), elapsed offsets, LTTB downsampling
- `src/erg/classify.py` — session classification (test_2k/6k/10k, interval, steady, short_piece) with confidence
- `src/erg/eligibility.py` — per-metric eligibility rules, each with a reason when ineligible
- `src/erg/pipeline.py` — runs classification + eligibility over stored workouts; manual overrides
- `src/erg/metrics.py` — derived metrics as pure functions (EF, decoupling, pacing, HRR, TRIMP, ACWR)
- `src/erg/metrics_runner.py` — computes and stores metrics, daily load and rolling windows
- `src/erg/summary.py` — weekly digest payload (descriptive only)
- `src/erg/compare.py` — distance-aligned resampling + split attribution for ghost racing
- `src/erg/web/` — race replay UI (vanilla JS + canvas, served at `/replay`)
- `src/erg/api.py`, `src/erg/cli.py` — FastAPI app and `erg` CLI
