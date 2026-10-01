/# RowingAnalytics
experimenting with concept2 API for training data analytics projects

See [erg-analytics-data-layer-plan.md](erg-analytics-data-layer-plan.md) for the full spec. **Current status: Phases 1-4 done** (ingest, strokes, classification + eligibility, metrics engine), plus the weekly summary and the race replay UI.

## Sharing it with friends (local mode)

They need only [uv](https://docs.astral.sh/uv/getting-started/installation/) and the Concept2 client ID
and secret, which you send them privately. Nothing else: no Docker, no Python install.

```sh
git clone <this repo> && cd RowingAnalytics
run.cmd          # Windows
./run.sh         # macOS / Linux
```

The first run asks for the two Concept2 values and saves them to `.env`. The browser opens, they click
**Connect Concept2**, sign in with their own account, and their logbook imports with a progress bar
(about a minute per 100 workouts). Each person only ever sees their own data.

Local mode uses an embedded Postgres in a temporary folder: nothing is kept after the app closes, and
the next launch imports again. It runs on port 8000, which is the redirect URI registered with Concept2.

## Deploy to Vercel (free tier)

The FastAPI app deploys as-is (`app.py` is the entrypoint); it needs a hosted Postgres.

1. **Database.** In the Vercel project: *Storage → Create → Neon* (free). This sets `DATABASE_URL`.
2. **Schema.** From this folder, with Neon's URL (use the *direct*, non-pooled one for migrations):
   ```powershell
   $env:DATABASE_URL = "postgresql://...neon.tech/...?sslmode=require"; uv run alembic upgrade head
   ```
3. **Concept2.** In the developer portal, add the redirect URI `https://<your-app>.vercel.app/auth/callback`.
4. **Environment variables** (Project → Settings → Environment Variables):
   `C2_CLIENT_ID`, `C2_CLIENT_SECRET`, `C2_REDIRECT_URI=https://<your-app>.vercel.app/auth/callback`,
   `TOKEN_ENCRYPTION_KEY` (generate a new one), `SESSION_SECRET` (any long random string), `SECURE_COOKIES=true`.
5. **Deploy.** Import the GitHub repo in Vercel (it redeploys on every push), or run `npx vercel --prod`.

Then open the site, connect Concept2, and the import runs in steps from the page.

## Setup (persistent database)


```sh
uv sync
docker compose up -d                 # Postgres (creates `erg` and `erg_test`)
cp .env.example .env                 # fill in C2 client id/secret, TOKEN_ENCRYPTION_KEY, and DATABASE_URL
uv run alembic upgrade head
```

Register an app at https://log.concept2.com/developers/keys with redirect URI `http://localhost:8000/auth/callback`. The app only reads data, so no live-API approval is needed; that is required only for apps that write results.

## Run

Double-click **`run.cmd`**, or from a terminal:

```powershell
.\run.cmd              # start everything and open the race replay UI
.\run.cmd -Sync        # pull new workouts from Concept2 first, then recompute
.\run.cmd -Recompute   # re-run classification + metrics before starting
.\run.cmd -NoBrowser   # don't open a browser
.\run.cmd -StopDb      # also stop Postgres when the server exits
.\run.cmd -Port 8001   # use a different port
```

It starts Docker Desktop and Postgres if needed, applies migrations, launches the API and
opens http://localhost:8000/replay. On a fresh database it opens the Concept2 login instead.
Ctrl+C stops the server; Postgres keeps running unless `-StopDb` is passed.

### Or by hand

```sh
uv run uvicorn erg.api:app --reload  # race replay UI: http://localhost:8000/replay
                                     # first run: http://localhost:8000/auth/login
                                     # all data endpoints need a session: /auth/login first
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
uv run erg override 123456 --clear   # back to the classifier
uv run erg settings --steady-pace 2:04 --margin 10   # or --auto to use the learned steady pace
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
- `src/erg/session.py` — signed session cookie; every data endpoint resolves the athlete from it
- `src/erg/sync.py` — idempotent athlete/workout upserts, backfill, stroke fetch queue
- `src/erg/strokes.py` — interval-aware stroke parsing (work/rest labels), elapsed offsets, LTTB downsampling
- `src/erg/describe.py` — workout descriptions in rowing notation (3x20' / 2'r, 5k-4k-3k-2k-1k, 3x(10x1' / 30"r) / 2'r)
- `src/erg/classify.py` — session classification: solo 2k/6k/10k are tests; otherwise interval vs steady against a learned per-athlete steady pace
- `src/erg/eligibility.py` — per-metric eligibility rules, each with a reason when ineligible
- `src/erg/pipeline.py` — runs classification + eligibility over stored workouts; manual overrides
- `src/erg/metrics.py` — derived metrics as pure functions (EF, decoupling, pacing, HRR, TRIMP, ACWR)
- `src/erg/metrics_runner.py` — computes and stores metrics, daily load and rolling windows
- `src/erg/summary.py` — weekly digest payload (descriptive only)
- `src/erg/compare.py` — distance-aligned resampling + split attribution for ghost racing
- `src/erg/web/` — UI served at `/replay`: race replay tab and a Pieces tab for classification overrides and settings (vanilla JS + canvas)
- `src/erg/embedded.py` — local mode: embedded Postgres (pgserver) in a temp folder, deleted on exit
- `src/erg/importer.py` — in-app import (workouts, strokes, classify, metrics) with progress, started at sign-in
- `src/erg/api.py`, `src/erg/cli.py` — FastAPI app and `erg` CLI
