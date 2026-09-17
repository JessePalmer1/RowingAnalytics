# RowingAnalytics
experimenting with concept2 API for training data analytics projects

See [erg-analytics-data-layer-plan.md](erg-analytics-data-layer-plan.md) for the full spec. **Current status: Phases 1 (ingest) and 2 (strokes) done.**

## Setup

```sh
uv sync
docker compose up -d                 # Postgres (creates `erg` and `erg_test`)
cp .env.example .env                 # fill in C2 client id/secret + TOKEN_ENCRYPTION_KEY
uv run alembic upgrade head
```

Register an app at https://log.concept2.com/developers/keys with redirect URI `http://localhost:8000/auth/callback`. The app only reads data, so no live-API approval is needed; that is required only for apps that write results.

## Run

```sh
uv run uvicorn erg.api:app --reload  # then visit http://localhost:8000/auth/login
                                     # strokes: GET /workouts/{id}/strokes?downsample=200&include_rest=false
uv run erg sync                      # after logging new workouts: backfill + fetch-strokes
uv run erg backfill                  # page all rower results into Postgres (safe to re-run)
uv run erg fetch-strokes             # drain the stroke fetch queue (new/edited workouts)
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
- `src/erg/api.py`, `src/erg/cli.py` — FastAPI app and `erg` CLI
