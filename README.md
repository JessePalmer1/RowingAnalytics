# RowingAnalytics
experimenting with concept2 API for training data analytics projects

See [erg-analytics-data-layer-plan.md](erg-analytics-data-layer-plan.md) for the full spec. **Current status: Phase 1 (ingest).**

## Setup

```sh
uv sync
docker compose up -d                 # Postgres (creates `erg` and `erg_test`)
cp .env.example .env                 # fill in C2 client id/secret + TOKEN_ENCRYPTION_KEY
uv run alembic upgrade head
```

Register an app at the Concept2 developer portal with redirect URI `http://localhost:8000/auth/callback`, pointed at the **dev server** (`https://log-dev.concept2.com`).

## Run

```sh
uv run uvicorn erg.api:app --reload  # then visit http://localhost:8000/auth/login
uv run erg backfill                  # page all rower results into Postgres (safe to re-run)
```

## Test

```sh
uv run pytest                        # DB tests skip if Postgres isn't running
```

## Layout

- `src/erg/c2/` — Concept2 client: OAuth, rate-limited/retrying API access, pagination
- `src/erg/normalize.py` — raw C2 units/timezones → normalized values (the only place raw units exist)
- `src/erg/tokens.py` — encrypted token storage; refresh with rotation under a row lock
- `src/erg/sync.py` — idempotent athlete/workout upserts and backfill
- `src/erg/api.py`, `src/erg/cli.py` — FastAPI app and `erg` CLI
