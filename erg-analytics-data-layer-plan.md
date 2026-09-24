# Erg Analytics Platform — Data Layer Specification

**Scope of this document:** the ingestion + storage + derived-metrics backend that everything else hangs off. Frontends (race replay, meters tracker, MCP server, trend digest) are specified only as consumers at the end.

**Core principle:** the data layer computes and stores *derived metrics that are invisible from a single workout*. That is the entire reason this platform is useful rather than a reskin of the Concept2 logbook.

---

## 1. Source system: Concept2 Logbook API

Base: `https://log.concept2.com`

### 1.1 Auth
- OAuth2. Grant types available to all apps: **Authorization Code** + **Refresh**.
- Register at the Concept2 API key portal → get Client ID + Client Secret. Register your redirect URI.
- **Read-only apps may use the live API (`log.concept2.com`) directly — no approval needed.** Only apps that *write* results must first develop against the dev server (`log-dev.concept2.com`, separate accounts, periodically reset) and then email ranking@concept2.com for live approval. This project is read-only and is connected to the live API.
- App registration (API key portal, `/developers/keys`): platform **Browser**, redirect URI `http://localhost:8000/auth/callback`, webhook URL left blank until Phase 5.
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

The webhook URL must be **publicly reachable** — Concept2 cannot call `localhost`. Requires a deployment or a tunnel before Phase 5.

### 1.4 Data model quirks (all of these will bite)
- **`date` is the END of the workout, not the start**, as stored in the monitor. It is in the user's local time; a separate `timezone` field (tz database format) and `date_utc` may be present. Historic rows may have `timezone: null`. Normalize carefully and store both local and UTC.
- **Units:** `time` is tenths of a second. `distance` is meters. Stroke `t` is tenths of a second, `d` is **decimeters**, `p` is pace in tenths of a second per 500m. User `weight` is **decagrams**, not decigrams as the docs say (7500 = 75kg; verified: live profile 8754 = 87.5kg).
- **No average watts in the results payload.** Only `wattminutes_total`, on a handful of rows. Watts are always derived from work pace.
- **Interval workouts:** top-level `distance`/`time` are **work only**; `rest_distance`/`rest_time` are separate. Stroke `t`/`d` **reset to 0 at each interval**, and are cumulative-within-interval, not deltas.
- **Per-interval summaries** live in `raw.workout.intervals` (interval workouts) or `raw.workout.splits` (split workouts): per-row `type` (`time`|`distance`), `time`, `distance`, `rest_time`, `rest_distance`, `stroke_rate`, `calories_total`, and `heart_rate` {average, min, max, ending, rest}. `heart_rate` is `{}` when no strap.
- Top-level `rest_time` includes rest after the **final** interval even though no strokes are recorded there, so `started_at = ended - (work + rest)` can be early by one rest period.
- **`stroke_data` is a boolean flag** on the result. If false, skip the stroke fetch. If true and the fetch still 404s ("This workout does not have any stroke data associated with it"), handle gracefully — this has historically been inconsistent.
- **Duplicates:** the logbook rejects a POST with the same date+time+distance with `409`.
- `workout_type` enum includes `JustRow`, `FixedDistanceSplits`, `FixedTimeSplits`, `FixedDistanceInterval`, `FixedTimeInterval`, `VariableInterval`, `unknown`. Older rows are often `unknown` — do not trust it as your only classifier.
- `source` tells you the origin (`ErgData`, `Web`, etc.). Web-entered rows have no strokes and often no HR.

### 1.5 Known data-quality reality (from the existing season CSV)
Measured on 104 sessions, Sept 2025 – Apr 2026:
- HR field is **populated on all 104 but physiologically plausible (90–210 bpm) on only ~76** — the rest are zeros/garbage from the strap not being worn. **Always validate HR, never trust presence.**
- Drag factor drifts (104–121 observed in the CSV; 94–200 in the live logbook, see §1.6). Watts are drag-independent; pace comparisons across different drag are not. Store drag and expose it as a comparability filter.
- Only ~11 pieces are continuous, ≥15 min, with usable HR *and* watts. Steady-state analysis operates on a much smaller subset than total session count.
- Watts present on 101/104.

**Design consequence:** every derived metric must carry an explicit `eligible` flag and a `reason_ineligible`, not silently drop rows.

### 1.6 Live-data findings (first backfill, 2026-09-16)
**The C2 profile is not authoritative.** Max HR reads 199 there but is actually 193; weight reads 87.5 kg but is actually 200 lb (90.7 kg). Corrections live in `athlete.*_override` (`erg profile --max-hr 193 --weight-lb 200`) and are never overwritten by sync. Anything intensity-related must use the effective values.

118 rower sessions, 2025-09-03 → 2026-09-16, ~1,225 km:
- **All from `ErgData iOS`; no `unknown` workout_type.** Types: FixedTimeInterval 47, VariableInterval 25, FixedDistanceInterval 19, FixedTimeSplits 13, FixedDistanceSplits 11, JustRow 3. The classifier can lean on `workout_type` more than §4.4 assumed (still keep overrides).
- `date_utc` present on all rows; `timezone` null on 3.
- Strokes flagged on 113/118. HR: 85 valid, 33 absent (zeros/no strap), 0 out-of-band.
- **Drag factor observed 94–200.** Plausible range is **90–225**: ~90 for light rowing, ~220 at max drag for power tests. Values outside that are treated as invalid. High-drag power pieces are not pace-comparable with normal-drag work.

### 1.7 Stroke data reality (from sampled live workouts)
- **Interval stroke streams include the rest period.** Within an interval, `t` runs past the work time into rest (e.g. 1:40 on / 0:20 off → `t` reaches ~120s) and `d` includes rest distance. Label strokes work/rest using the interval's work `time` from `raw.workout.intervals`. This is also what makes HRR (§5.3) computable.
- **Interval boundary = `t` AND `d` both decrease.** `t` alone jitters backwards by up to ~6s near the end of an interval while `d` keeps increasing; treating that as a reset creates phantom intervals. Cross-check the detected count against `len(raw.workout.intervals)`.
- First stroke(s) have `p = 0` and `spm = 0` → null, not zero.
- `hr = 0` on every stroke when no strap → null.
- **Per-stroke HR legitimately falls below 90** (warm-up start 63 bpm, rest recovery). The 90–210 band is for session averages only; per-stroke HR uses a wide physiological band (30–230).
- `d` can overshoot the interval target distance (paddling through rest).
- **The last stroke sample lands short of the finish.** Measured on the three 2k tests: the final stroke reads 1996.7–1997.7m and 383.1–386.9s where the logbook records 2000m and 383.9–387.4s — a 2–3m, 0.5–0.8s shortfall. The stroke stream alone therefore under-reports every piece's total. Anything reporting a total time or distance must anchor the end to the logbook summary (`compare.track_from_samples`), which is only safe when the summary is *ahead* of the strokes by a plausible margin (≤50m, ≤15s) — truncated summaries (above) are behind them.
- Rest strokes are sparse (a few paddle strokes, ~1–2% of interval strokes overall) but carry HR through recovery (e.g. 163→155 bpm over a 20s rest).
- **Logbook summaries can be truncated; strokes are more complete.** 5/113 workouts have more intervals in the stroke stream than in `raw.workout.intervals`, and the extra intervals are full-effort work, not artifacts. 3 of them have top-level `time = 0` and `distance = 0` (107312625, 107817599, 113079371); 2 have totals covering only the first interval (109930824, 113030068). That's ~23 km of rowing the summary under-reports. These carry a `stroke_warning`; strokes in the unsummarized intervals cannot be labelled work/rest. **Phase 4 consequence:** load aggregates and totals must reconstruct from strokes when `stroke_warning` is set or totals are zero, not trust the summary.

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
  max_heart_rate    int,                       -- from C2 profile, nullable
  weight_g          int,                       -- normalized from decagrams (x10)
  max_heart_rate_override int,                 -- athlete-supplied; wins over the profile (193 vs C2's 199)
  weight_g_override       int,                 -- athlete-supplied (200 lb = 90,718 g vs C2's 87.5 kg)
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
  tz_source         text,                      -- 'payload' | 'athlete_default'
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
  drag_factor       int,                       -- null if outside 90-225
  avg_pace_s_500    numeric,
  avg_watts         numeric,                   -- derived from pace (not in payload)
  watts_derived     bool,
  hr_avg            int,
  hr_ending         int,
  hr_rest           int,
  hr_quality        text,                      -- 'valid' | 'invalid' | 'absent'
  calories          int,
  comments          text,
  has_strokes       bool,
  stroke_status     text,                      -- 'not_available' | 'pending' | 'fetched' | 'missing' | 'error'
  stroke_attempts   int,
  stroke_error      text,
  stroke_warning    text,                      -- parse anomalies (e.g. interval count mismatch)
  strokes_fetched_at timestamptz,
  raw               jsonb,                     -- full original payload
  ingested_at       timestamptz,
  unique (athlete_id, ended_at_local, work_time_s, work_distance_m)   -- mirrors C2 dedupe (date+time+distance)
)

interval_split (
  workout_id, idx,                             -- pk
  kind,                                        -- 'split' | 'interval'
  target_type,                                 -- 'time' | 'distance'
  time_s, distance_m, rest_time_s, rest_distance_m,
  spm, hr_avg, hr_max, hr_ending, hr_rest, calories
)

stroke (
  workout_id  bigint,
  interval_idx smallint,                       -- strokes reset per interval
  seq          int,
  t_s          numeric,                        -- cumulative within interval
  d_m          numeric,                        -- from decimeters
  pace_s_500   numeric,
  spm          smallint,
  hr           smallint,                       -- 30-230 valid; 0 -> null
  is_rest      bool,                           -- stroke taken during the interval's rest period
  primary key (workout_id, interval_idx, seq)
)
-- consider TimescaleDB hypertable or monthly partitions if this grows large;
-- ~230 strokes per 2k, ~1000+ for a 15k. 118 sessions ≈ 50-100k rows. Fine as plain table.

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
- **HR validation:** mark session/interval average and ending HR null if outside 90–210 or equal to 0. Rest HR uses 40–210 (it is sampled after recovery). Per-stroke HR uses 30–230 (§1.7). Track `hr_quality` per workout.
- **Drag validation:** mark `drag_factor` null if outside 90–225. The raw value stays in `raw`.
- Keep normalization re-runnable from `raw` (`erg renormalize`) so rule changes never need an API re-fetch.

### 4.4 Session classification
Classes (as built): `test_2k`, `test_6k`, `test_10k`, `interval`, `steady`, `short_piece`, `unknown`.

- **Pace rules everything (athlete rule):** any session averaging **slower than 1:55/500m of work pace is steady state**, whatever its shape and whatever HR did. A 4x15' or 4x3k at 2:00 with HR over 150 is steady work, not intervals. This check runs before every other signal.
- **Interval** is what remains with rest periods and work pace faster than 1:55: every shape from 4x10' to 20x30", all hard efforts at or above threshold, one class. Detected by rest time, `workout_type`, summary interval count or stroke resets.
- **Work pace falls back to the stroke stream** when the C2 summary is truncated (§1.7), so the zero-total workouts still classify. Two of the five turned out to be steady (2:07.5, 2:04.2).
- **Test distances** are matched within ±2% of 2000/6000/10000m, then confirmed by intensity.
- **Intensity comes from HR when valid**, for pieces faster than 1:55 at a test distance: ≥82% of max HR is a test; <75% means a steady piece at that distance; between the two, a test with low confidence. **Max HR is the athlete override (193), not the C2 profile value (199)** — see §3 `athlete.max_heart_rate_override`.
- **Pace fallback when HR is missing:** pace relative to the athlete's best 2k, ceilings 1.06x (2k), 1.18x (6k), 1.26x (10k) — rowing-standard deltas of 2k+8s/500m for a 6k and +15-18s for a 10k. Always low confidence: flag for review.
- **`short_piece`** is any continuous piece under 10 min that is not at a test distance (warm-up, cool-down or short sprint). Replaces the plan's original `warmup_short`. A 2k test is ~6:30, so test distances are exempt.
- Confidence is emitted on every row; anything under 0.7 is printed by `erg classify` for review. Manual override lives in `classification_override` and **always wins**, survives recomputation, and re-runs eligibility. **Manual override matters** — you will disagree with the classifier and you need to win.

---

## 5. Derived metrics (the actual product)

Each metric stores an eligibility flag. Never compute silently on invalid inputs.

Eligibility rules as built (`workout_eligibility`, one row per workout per metric, with a reason when ineligible):

| Metric | Requires |
|---|---|
| `ef` | class `steady`, ≥15 min work, watts, and HR: session average when continuous, else ≥80% stroke-HR coverage (interval-shaped steady work must be computed from work strokes only, since session averages include rest) |
| `decoupling` | everything `ef` needs, plus one continuous piece, ≥20 min, and stroke HR covering ≥80% |
| `hrr` | class `interval` with HR recorded during rest strokes |
| `pacing` | ≥30 strokes stored, a ratable class, no stroke parse warning |
| `dps` | stroke count and work distance present |

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
**Revised against live data.** The stroke-based definition (`hr at 60s into rest`) is not computable: only **1 of 118** workouts has stroke HR sampled ≥55s into a rest — rest sampling usually stops within 10–30s. Instead HRR comes from the C2 per-interval summary: `hrr = median(hr_ending − hr_rest)` across the session's intervals, which covers **30 workouts / 219 intervals**.
- **Recovery scales with rest length** (measured: 7 bpm after 20s, 14.7 after 30s, 32.7 after 90s, 48.7 after 120s, 63.6 after 180s), so `hrr_rest_s` is stored alongside and **trends are only valid at matched rest length**. `/metrics/trend?name=hrr&hrr_rest_s=180` enforces this.
- Available on any session with rest periods, including interval-shaped steady work — not just the `interval` class.
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

**Phase 1 — Ingest (the foundation).** OAuth flow with token refresh + rotation, backfill pager, workout normalizer, Postgres schema, idempotent upserts. *Done when:* your full season is in the database and re-running backfill changes nothing. **✅ Done 2026-09-16** — 118 sessions ingested from the live API; second backfill run reported 118 unchanged.

**Phase 2 — Strokes.** Stroke fetch worker, interval-aware parsing, bulk insert, `has_strokes` handling, downsampling endpoint. *Done when:* you can pull the stroke series for any 2k and plot it. **✅ Done 2026-09-16** — 113/113 workouts fetched (106,020 strokes, 0 missing, 0 errors, 5 truncated-summary warnings, §1.7); 2k 110330485 (6:23.9) plotted from `/workouts/{id}/strokes` with and without LTTB downsampling.

**Phase 3 — Classification + eligibility.** Session classifier with confidence + manual override, HR validation, eligibility flags with reasons. *Done when:* you agree with the classifier on all 118 sessions (after overrides). **Built 2026-09-17** with the athlete's 1:55 steady rule applied: 118 classified as steady 74, interval 33, test_6k 4, test_2k 3, short_piece 3, test_10k 1 (2 confirmed manual overrides). Eligible: ef 44, decoupling 6, hrr 25, pacing 105, dps 111.

**Phase 4 — Metrics engine.** EF, decoupling, HRR, pacing shape, DPS, daily load, ACWR. Versioned, recomputable, backfillable. *Done when:* you can see your EF and decoupling trend across the season and it matches the Feb-peak/April-detrain story you already know from the data. **Built 2026-09-17:** 44 EF, 6 decoupling, 30 HRR, 105 pacing, 111 DPS; 106 load days; 1,516 rolling rows. `erg metrics` recomputes everything from stored data.

Implementation notes:
- All stroke metrics are **time-weighted per stroke** (`dt` between strokes, capped at 10s) and run on **work strokes only**, so rest never dilutes EF or decoupling.
- EF falls back to session averages when a workout has no strokes.
- `kj`/`trimp` stay null rather than zero when a session cannot produce them, so daily load never reads a missing value as a real zero.
- TRIMP (Banister, male coefficients) assumes a resting HR of 60 unless `athlete.resting_hr_override` is set (`erg profile --resting-hr`).

**Phase 5 — Webhooks + live sync.** Webhook endpoint, verification, job enqueue, nightly reconciliation poll.

**Phase 6 — Query API + first frontend.** Query surface built: `/workouts`, `/workouts/{id}`, `/workouts/{id}/strokes`, `/workouts/compare`, `/metrics/trend`, `/load/daily`, `/load/acwr`, `/summary/week`. First frontend built: the race replay UI at `/replay` (see §8).

---

## 8. Downstream features (consumers of this layer)

**Race replay UI** — **built 2026-09-24**, served at `/replay` by the same FastAPI app (`src/erg/web/`, vanilla JS + canvas, no build step and no CDN). Piece picker filtered by class, animated ghost race with scrub and speed control, live gap/pace/HR readout, per-segment split table, and pace / time-delta / HR / stroke-length charts. `/workouts/compare?ids=&points=&segment_m=` does the distance-aligned interpolation and split attribution server-side, and warns when drag factor differs across the selected pieces. Verified against the three 2k tests: the April piece lost 3.67s to December, 2.4s of it after 1000m.

Totals are anchored to the logbook summary, so split tables and race times match the monitor exactly rather than ending 0.5–0.8s early on the last stroke sample (§1.7).

Still to do here: shareable permalink with a server-rendered OG card.

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
- [ ] Drag factor varies (94–200 observed; valid 90–225, power tests near 220) — gate pace comparisons on it.
- [x] ~~Production API access requires Concept2 approval~~ — only for apps that write. Read-only runs on live now.
- [x] Weight is decagrams, not decigrams (verified against live profile).
- [ ] Interval stroke streams include rest strokes — label work/rest before computing any work-portion metric.
- [ ] Stroke `t` jitters backwards near interval ends — detect boundaries on `t` and `d` together.

---

## 10. Immediate next step

~~Build Phase 1 against the development server~~ — Phase 1 is done against the live API.

~~Phase 2 — strokes~~ — done: Postgres-backed fetch queue (`stroke_status` on `workout`, `FOR UPDATE SKIP LOCKED`), `interval_split` from `raw.workout`, interval-aware parser with work/rest labelling, `GET /workouts/{id}/strokes?downsample=N&include_rest=`.

Next: **Phase 3 — classification + eligibility.** Start from `workout_type` (fully populated in live data, §1.6), add the manual override table, and handle the truncated-summary workouts from §1.7.
