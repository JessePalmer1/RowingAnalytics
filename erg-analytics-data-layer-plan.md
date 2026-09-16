# Erg Analytics Platform — Data Layer Specification

**Scope of this document:** the ingestion + storage + derived-metrics backend that everything else hangs off. Frontends (race replay, meters tracker, MCP server, trend digest) are specified only as consumers at the end.

**Core principle:** the data layer computes and stores *derived metrics that are invisible from a single workout*. That is the entire reason this platform is useful rather than a reskin of the Concept2 logbook.

---

## 1. Source system: Concept2 Logbook API

Base: `https://log.concept2.com`

### 1.1 Auth
- OAuth2. Grant types available to all apps: **Authorization Code** + **Refresh**.
- Register at the Concept2 API key portal → get Client ID + Client Secret. Register your redirect URI.
- **Develop against the development server first**, then contact Concept2 for live API approval. Dev database is periodically reset — never rely on data persisting there.
- Scopes, comma-separated: `user:read,results:read` (add `results:write` only if/when you POST workouts).
  - Requesting `results:write` implies `results:read`.
  - **Do not omit the scope param** — it silently defaults to `user:read,results:write` for backwards compatibility.
  - You cannot *add* scopes later without re-running the authorization flow.
- Access token: `POST /oauth/access_token`. Short-lived (`expires_in`, e.g. 604800s). Refresh tokens last **one year** and rotate on each use — store the new refresh token every time.
- Header: `Authorization: Bearer <token>`, plus `Accept: application/vnd.c2logbook.v1+json` to pin the API version.

### 1.2 Endpoints used
| Endpoint | Purpose |
|---|---|
| `GET /api/users/me` | Profile: id, max_heart_rate, weight, gender, dob |
| `GET /api/users/{user}/results` | Paginated workout list. Filters: `from`, `to`, `type`, `updated_after` |
| `GET /api/users/{user}/results/{id}` | Single result; `?include=strokes,user` embeds strokes |
| `GET /api/users/{user}/results/{id}/strokes` | Stroke array |
| `GET /api/users/{user}/results/{id}/export/{csv\|fit\|tcx}` | File export (useful for reconciliation/backfill) |
| `GET /api/challenges/*` | **No auth required** — current/upcoming/recent/season |

Pagination: `?page=N&number=M`, default 50, **max 250**. Response carries `meta.pagination` with `total`, `total_pages`, and `links.next`.

Rate limiting: **not currently enforced**, but explicitly reserved. Build a token-bucket limiter and backoff anyway — do not hammer it.

### 1.3 Webhooks
Register in the self-service developer portal. Fires on `result-added`, `result-updated`, `result-deleted` for any user who has authorized your client. Payload for add/update is the same result object you'd fetch directly; delete sends only `result_id`.

**Note:** the webhook payload does **not** include stroke data. On receipt, enqueue a job to fetch strokes separately.

### 1.4 Data model quirks (all of these will bite)
- **`date` is the END of the workout, not the start**, as stored in the monitor. It is in the user's local time; a separate `timezone` field (tz database format) and `date_utc` may be present. Historic rows may have `timezone: null`. Normalize carefully and store both local and UTC.
- **Units:** `time` is tenths of a second. `distance` is meters. Stroke `t` is tenths of a second, `d` is **decimeters**, `p` is pace in tenths of a second per 500m. User `weight` is **decigrams** (7500 = 75kg).
- **Interval workouts:** top-level `distance`/`time` are **work only**; `rest_distance`/`rest_time` are separate. Stroke `t`/`d` **reset to 0 at each interval**, and are cumulative-within-interval, not deltas.
- **`stroke_data` is a boolean flag** on the result. If false, skip the stroke fetch. If true and the fetch still 404s ("This workout does not have any stroke data associated with it"), handle gracefully — this has historically been inconsistent.
- **Duplicates:** the logbook rejects a POST with the same date+time+distance with `409`.
- `workout_type` enum includes `JustRow`, `FixedDistanceSplits`, `FixedTimeSplits`, `FixedDistanceInterval`, `FixedTimeInterval`, `VariableInterval`, `unknown`. Older rows are often `unknown` — do not trust it as your only classifier.
- `source` tells you the origin (`ErgData`, `Web`, etc.). Web-entered rows have no strokes and often no HR.

### 1.5 Known data-quality reality (from the existing season CSV)
Measured on 104 sessions, Sept 2025 – Apr 2026:
- HR field is **populated on all 104 but physiologically plausible (90–210 bpm) on only ~76** — the rest are zeros/garbage from the strap not being worn. **Always validate HR, never trust presence.**
- Drag factor drifts (104–121 observed). Watts are drag-independent; pace comparisons across different drag are not. Store drag and expose it as a comparability filter.
- Only ~11 pieces are continuous, ≥15 min, with usable HR *and* watts. Steady-state analysis operates on a much smaller subset than total session count.
- Watts present on 101/104.

**Design consequence:** every derived metric must carry an explicit `eligible` flag and a `reason_ineligible`, not silently drop rows.

---

## 2. Architecture

```
                  ┌───────────────────────────┐
   OAuth flow ───▶│  Auth service             │ tokens, refresh rotation
                  └────────────┬──────────────┘
                               │
  C2 Webhook ──▶ ┌─────────────▼─────────────┐
                 │  Ingestion API            │ verify, enqueue
                 └─────────────┬─────────────┘
                               │  job queue
                 ┌─────────────▼─────────────┐
   Backfill  ───▶│  Sync workers             │ fetch result + strokes
   (paginated)   └─────────────┬─────────────┘
                               │
                 ┌─────────────▼─────────────┐
                 │  Normalizer               │ units, tz, interval split
                 └─────────────┬─────────────┘
                               │
                 ┌─────────────▼─────────────┐
                 │  Postgres (raw + norm)    │
                 └─────────────┬─────────────┘
                               │
                 ┌─────────────▼─────────────┐
                 │  Metrics engine           │ per-workout + rolling
                 └─────────────┬─────────────┘
                               │
                 ┌─────────────▼─────────────┐
                 │  Query API (REST)         │──▶ replay UI, tracker,
                 └───────────────────────────┘    MCP server, digest
```

### 2.1 Stack recommendation
- **Python + FastAPI** — matches your existing analysis work, and `pyconcept2` exists as a typed client with pydantic models (handles pagination, stroke fetch, file export).
- **Postgres** — relational fits this cleanly; JSONB column for the raw payload so you never lose fidelity.
- **Redis + RQ/Celery** (or Postgres-backed queue like `pgqueuer` to avoid another service) for async stroke fetching.
- **Alembic** migrations.
- Deploy: single container + managed Postgres is entirely sufficient. Do not over-engineer.

*Alternative if you want the frontend tightly coupled:* Next.js + Auth.js, which ships a **built-in Concept2 OAuth provider** (`@auth/core/providers/concept2`), removing most OAuth boilerplate. Trade-off: your metrics work is Python-shaped, so you'd end up with two runtimes.

---

## 3. Schema

```sql
athlete (
  id                bigint primary key,        -- C2 user id
  username          text,
  max_heart_rate    int,                       -- from profile, nullable
  weight_g          int,                       -- normalized from decigrams
  created_at        timestamptz
)

oauth_token (
  athlete_id        bigint references athlete,
  access_token      text,                      -- encrypted at rest
  refresh_token     text,                      -- encrypted; ROTATES on refresh
  expires_at        timestamptz,
  scopes            text
)

workout (
  id                bigint primary key,        -- C2 result id
  athlete_id        bigint references athlete,
  ended_at_local    timestamp,                 -- C2 `date` = END of workout
  ended_at_utc      timestamptz,
  tz                text,
  started_at_utc    timestamptz,               -- DERIVED: ended - (work+rest)
  machine           text,                      -- rower/skierg/bike/etc
  workout_type      text,
  source            text,
  work_time_s       numeric,                   -- from tenths
  work_distance_m   int,
  rest_time_s       numeric,
  rest_distance_m   int,
  avg_spm           int,
  stroke_count      int,
  drag_factor       int,
  avg_watts         numeric,                   -- derived if absent
  hr_avg            int,
  hr_ending         int,
  hr_rest           int,
  calories          int,
  comments          text,
  has_strokes       bool,
  raw               jsonb,                     -- full original payload
  ingested_at       timestamptz,
  unique (athlete_id, ended_at_local, work_distance_m)   -- mirrors C2 dedupe
)

interval_split (
  id, workout_id, idx, type,                   -- 'split' | 'interval'
  time_s, distance_m, rest_time_s, rest_distance_m,
  spm, hr_avg, hr_ending, hr_rest, calories
)

stroke (
  workout_id  bigint,
  interval_idx smallint,                       -- strokes reset per interval
  seq          int,
  t_s          numeric,                        -- cumulative within interval
  d_m          numeric,                        -- from decimeters
  pace_s_500   numeric,
  spm          smallint,
  hr           smallint,
  primary key (workout_id, interval_idx, seq)
)
-- consider TimescaleDB hypertable or monthly partitions if this grows large;
-- ~230 strokes per 2k, ~1000+ for a 15k. 104 sessions ≈ 50-80k rows. Fine as plain table.

workout_metric (
  workout_id   bigint primary key,
  eligible_steady      bool,
  ineligible_reason    text,
  ef                   numeric,   -- efficiency factor
  decoupling_pct       numeric,
  pace_cv              numeric,
  spm_cv               numeric,
  dps_m                numeric,   -- distance per stroke
  hrr_60               int,       -- HR recovery, 60s
  fade_onset_m         int,
  first_half_pace      numeric,
  second_half_pace     numeric,
  computed_at          timestamptz,
  metric_version       int        -- bump to trigger recompute
)

daily_load (
  athlete_id, date,
  sessions, work_time_s, work_distance_m,
  kj, trimp, session_count
)

rolling_metric (
  athlete_id, date, metric_name, window_days, value
)
```

**`metric_version`** is important: when you change a metric definition, bump the version and let a backfill job recompute rather than silently mixing definitions.

---

## 4. Ingestion pipeline

### 4.1 Backfill (one-time per athlete)
1. `GET /api/users/me` → upsert athlete.
2. Page `GET /api/users/me/results?type=rower&number=250` until `links.next` is absent.
3. Upsert each workout (raw payload → `raw` jsonb, normalized fields alongside).
4. For each with `stroke_data == true`, enqueue a stroke-fetch job.
5. Stroke worker: fetch, split by interval, bulk insert.
6. Enqueue metrics computation.

Idempotency: upsert on C2 result id. Re-running backfill must be a no-op.

### 4.2 Incremental sync
Two mechanisms, both required — webhooks can be missed, polling is the safety net:
- **Webhook** → verify → enqueue fetch job (payload lacks strokes).
- **Nightly reconciliation poll** using `updated_after` (**note: this filter is in GMT** — convert before sending). Catches anything the webhook dropped and picks up edits.

### 4.3 Normalization rules
- Convert tenths → seconds, decimeters → meters, decigrams → grams at the boundary. **Nothing downstream ever sees raw C2 units.**
- Derive `started_at_utc = ended_at_utc - (work_time + rest_time)`. Flag it as derived; it's approximate for interval workouts where rest handling varies.
- If `timezone` is null, fall back to athlete's default tz; record that you did.
- Derive watts from pace when absent: `watts = 2.80 / pace_per_metre³`, i.e. for pace in seconds per 500m, `watts = 2.80 / (pace/500)³`.
- **HR validation:** mark HR null if outside 90–210 or equal to 0. Track `hr_quality` per workout.

### 4.4 Session classification
Do not trust `workout_type` alone. Classify into: `test_2k`, `benchmark_other` (5k/6k/30min), `interval`, `steady`, `warmup_short`, `unknown`.

Signals: description regex (`\d+\s*x`, rest tokens `/…r`), interval count from splits, work duration, pace CV across strokes, proximity of pace to known PB. Emit a confidence score; allow manual override stored in a `classification_override` table. **Manual override matters** — you will disagree with the classifier and you need to win.

---

## 5. Derived metrics (the actual product)

Each metric stores an eligibility flag. Never compute silently on invalid inputs.

### 5.1 Efficiency Factor (EF)
`EF = avg_watts / avg_hr` over the eligible portion.
- Eligibility: continuous piece, ≥15 min, valid HR, watts present, HR within an aerobic band.
- **Store drag factor alongside** — EF is comparable across pieces, pace is not.
- Trend EF over time at matched intensity. This is the headline fitness signal.

### 5.2 Aerobic decoupling (Pw:HR)
`decoupling% = (EF_first_half − EF_second_half) / EF_first_half × 100`
- Split the eligible steady portion in half; compute EF on each.
- **Validity floor: efforts under ~20 minutes are not meaningful.** Enforce it — mark shorter pieces ineligible rather than reporting a noisy number.
- Interpretation: **under ~5% indicates good aerobic durability** at that duration/output; larger drift means you faded. Falling decoupling across a base block is evidence the aerobic base is genuinely developing.
- Only valid on steady, sub-threshold efforts — never on tests or variable work.
- *This is the single most useful metric here and it is invisible in the Concept2 logbook.*

### 5.3 HR recovery (HRR)
From stroke HR during rest intervals: `HRR60 = hr_peak_at_interval_end − hr_at_60s_into_rest`.
- Only computable on interval workouts with stroke HR through the rest period.
- Trend over time at matched work intensity. A classic fitness marker, and it is **already sitting in your logbook unused**.

### 5.4 Pacing shape
- `pace_cv` = stdev/mean of stroke pace over the work portion.
- `fade_onset_m`: first distance where a rolling pace window degrades >X% from the piece's best window and never recovers.
- First-half vs second-half split differential (negative/even/positive split).
- Across *all* test pieces: is fly-and-die systematic or was it one bad day? Single-workout view cannot answer this; the platform can.

### 5.5 Technique proxies
- `dps_m` = distance per stroke = `work_distance_m / stroke_count`, and per-stroke `d`-delta series.
- **DPS at matched stroke rate**, trended — separates technique drift from fitness change.
- Pace-per-stroke-rate efficiency curve per athlete.

### 5.6 Load aggregates
- Daily: work seconds, meters, kJ (`watts × seconds / 1000`).
- TRIMP where HR valid.
- **ACWR** = 7-day load ÷ 28-day load (a fast/slow moving-average ratio). Present as a descriptive load-balance indicator only.
- Monotony = mean daily load ÷ stdev of daily load, over 7 days.

### 5.7 Anomaly detection
Baseline = rolling median + MAD of EF, HR-at-pace, and DPS over the trailing N eligible sessions of the same class. Flag deviations beyond a threshold. Output is descriptive ("this steady state ran 8% higher HR at your usual pace"), never prescriptive.

> **Framing constraint:** every metric is descriptive analytics on your own training data. No readiness scores, no train/rest recommendations, no injury or health inference. Training decisions stay with you and your coaches.

---

## 6. Query API surface

```
GET  /athletes/me
GET  /workouts?from&to&type&class&eligible_steady
GET  /workouts/{id}                     # + metrics
GET  /workouts/{id}/strokes?downsample=N
GET  /workouts/compare?ids=1,2,3        # distance-aligned for ghost racing
GET  /metrics/trend?name=ef&window=90d&class=steady
GET  /metrics/decoupling?from&to
GET  /load/daily?from&to
GET  /load/acwr?date
GET  /summary/week?date                 # digest payload
GET  /challenges/progress               # joins public C2 challenges to your meters
POST /workouts/{id}/classification      # manual override
```

`/workouts/compare` doing **distance-aligned interpolation server-side** is what makes ghost racing trivial on the frontend — resample each piece onto a common distance grid and return aligned arrays.

Downsampling strokes matters: a 15k has thousands of strokes; use largest-triangle-three-buckets or simple decimation for chart payloads.

---

## 7. Build phases

**Phase 1 — Ingest (the foundation).** OAuth flow with token refresh + rotation, backfill pager, workout normalizer, Postgres schema, idempotent upserts. *Done when:* your full season is in the database and re-running backfill changes nothing.

**Phase 2 — Strokes.** Stroke fetch worker, interval-aware parsing, bulk insert, `has_strokes` handling, downsampling endpoint. *Done when:* you can pull the stroke series for any 2k and plot it.

**Phase 3 — Classification + eligibility.** Session classifier with confidence + manual override, HR validation, eligibility flags with reasons. *Done when:* you agree with the classifier on all 104 sessions (after overrides).

**Phase 4 — Metrics engine.** EF, decoupling, HRR, pacing shape, DPS, daily load, ACWR. Versioned, recomputable, backfillable. *Done when:* you can see your EF and decoupling trend across the season and it matches the Feb-peak/April-detrain story you already know from the data.

**Phase 5 — Webhooks + live sync.** Webhook endpoint, verification, job enqueue, nightly reconciliation poll.

**Phase 6 — Query API + first frontend.**

---

## 8. Downstream features (consumers of this layer)

**Race replay UI** — `/workouts/{id}/strokes` + `/workouts/compare`. Ghost racing, split attribution ("2.4 of your 3.1s loss came between 1000–1500m"), pace-HR decoupling overlay, DPS trace. Shareable permalink with server-rendered OG card.

**Meters/challenge tracker** — `/load/daily`, `/challenges/progress`. Weekly goal projection ("at current pace you finish 14k short; you need 3 more sessions"), streaks, consistency, live webhook updates. Squad leaderboards once multiple athletes authorize.

**MCP server** — wrap the *analytical* endpoints, not raw CRUD: `get_stroke_analysis`, `compare_pieces`, `training_volume_summary`, `find_pieces(criteria)`, `get_trend`. The existing public Concept2 MCP server is thin (essentially just a profile tool), so there is real room. Challenges endpoints need no auth, so a useful subset ships before OAuth is wired.

**Trend digest (the fixed auto-coach)** — weekly pull-based summary built from `rolling_metric`, not per-workout narration. LLM receives *computed* metrics (EF trend, decoupling direction, HRR change, DPS-at-rate drift, anomaly flags) and writes the summary. The value is that these are invisible from inside any single session — which was the flaw in the per-workout version.

**Force-curve analyzer (separate track).** Force data is **not in the Logbook API** — only over PM5 Bluetooth, characteristic `0x003D` (documented, time-sampled, values repeat 2–3×, zeros at the start) and undocumented `0x0043` (one point per reading, same packet scheme). Units are pounds of force. **PM5v1 does not send force curves over BLE.** The PM5 accepts one connection at a time, so ErgData must be closed. Reference implementation: `jonstraveladventures/pm5-force-logger` (Python/bleak). Shares almost no code with this layer — treat as a second project that writes into the same database.

---

## 9. Risks and gotchas checklist

- [ ] Refresh token **rotates on every use** — persist the new one or you lose access.
- [ ] Workout `date` is the **end** of the session, in local time. Do not assume UTC. (This exact bug has hit other C2 integrators.)
- [ ] Stroke `t`/`d` **reset per interval** — naive concatenation produces a sawtooth.
- [ ] HR presence ≠ HR validity. ~27% of the season's HR values are unusable.
- [ ] `stroke_data: true` can still 404 on fetch. Handle it.
- [ ] Dev database is **wiped periodically**; don't build anything that assumes persistence there.
- [ ] Rate limiting isn't enforced *yet* — self-limit anyway; the docs reserve the right and note that abuse costs access.
- [ ] Decoupling under 20 minutes is not meaningful. Enforce the floor.
- [ ] Drag factor varies (104–121 in your data) — gate pace comparisons on it.
- [ ] Production API access requires Concept2 approval after dev work. Start that conversation early if you want this live.

---

## 10. Immediate next step

Build Phase 1 against the **development server**: OAuth round-trip, `GET /api/users/me`, paginated result backfill, normalizer, and the `workout` table. That single slice de-risks auth, pagination, units, and timezone handling — the four things most likely to be quietly wrong — before any metric work starts.
