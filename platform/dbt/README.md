# dbt project — Odisha power outage platform

dbt-postgres owns every transformation in the platform: district resolution,
feed parsing, dedupe, facts, reliability indices, risk and the API marts.
Contract: `platform/CONTRACT.md`. DDL + reference seed: `platform/postgres/init/`.

## Run it

```bash
# env comes from compose (POSTGRES_HOST/PORT/DB/USER/PASSWORD); no credentials in repo
export DBT_PROFILES_DIR=platform/dbt
dbt seed --project-dir platform/dbt --profiles-dir platform/dbt   # bootstrap/daily only
dbt run  --project-dir platform/dbt --profiles-dir platform/dbt
dbt test --project-dir platform/dbt --profiles-dir platform/dbt
```

Airflow invokes the isolated dbt venv with the repo mounted at `/opt/odisha`:

```
/opt/dbt/bin/dbt --project-dir /opt/odisha/platform/dbt \
                 --profiles-dir  /opt/odisha/platform/dbt \
                 --target-path   /opt/odisha/data/dbt/target \
                 --log-path      /opt/odisha/data/dbt/logs \
                 run --target dev
```

Targets: profile `odisha`, target `dev`, database `odisha`, default schema
`analytics`, `threads: 4`. Project compatible with dbt-postgres 1.9 → 1.11
(classic `config-version: 2`, no packages).

> **Fresh database:** `dbt seed` is required **once before** the first
> `dbt run`/`dbt build`. The models reference `seeds.district_profile`, so a
> manual `dbt run` against an un-seeded database fails with "relation
> seeds.district_profile does not exist". The `odisha_bootstrap` and
> `odisha_dbt_daily` DAGs run `dbt seed` first; the 30-minute ingest DAG runs
> `dbt run` only (it relies on the bootstrap seed).

## Lineage

```
sources (raw)                         staging (views)                analytics (tables)               marts (tables)
─────────────────────────────────     ──────────────────────         ────────────────────────         ──────────────────────
raw.scrape_batches  ───────────────►  stg_official_outages ────────► dim_district                    map_dots
raw.official_outages ──────────────►      │  (district resolution,  fct_outage_events ─┐             outage_cause_daily
raw.district_aliases ──────────────►      │   parsing, geocoding)                   │
raw.circle_exact ──────────────────►      │                                         │
raw.district_centroids ────────────►      │                                         │
raw.station_geocodes ──────────────►      │                                         │
seeds.district_profile ────────────►  stg_district_profile ──────► dim_district    │
                                      │                                               │
                                      └──► stg_outage_events ────────────────────────┤
                                             (dedupe by row_hash, first/last/active)   │
                                                                                      ▼
sources (raw/realtime/ml)                     analytics (tables)                 marts (tables)
raw.weather_hourly ────────────────►  stg_weather_hourly ──► district_risk_daily
realtime.district_live_metrics ─────────────────────────────► district_live_snapshot
analytics.fct_outage_events / stg_outage_events ────────────► outage_cause_daily & reliability_daily
```

Materialization per contract: `staging` = view, `analytics`/`marts` = table,
seeds = table. Schema routing is done by `macros/generate_schema_name.sql`, which
uses the configured `+schema` verbatim (`staging`, `analytics`, `marts`, `seeds`)
instead of concatenating with `target.schema`.

## Model surface consumed by the API (contract columns)

| Model | Columns |
|---|---|
| `marts.map_dots` | `district, circle, division, gss, voltage, feeder, start_text, end_text, outage_date, reason, areas, lat, lon, geocoded, is_planned, duration_minutes, affected_customers, cause_category, etr` |
| `analytics.dim_district` | `district, zone, customers, population, urban_share, lat, lon` |
| `analytics.fct_outage_events` | `row_hash, district, start_ts, end_ts, duration_minutes, is_planned, cause_category, voltage, feeder, gss, areas, reason, first_seen, last_seen, is_active` |
| `analytics.reliability_daily` | `district, metric_date, saidi_minutes, saifi, caidi_minutes, asai_pct, outage_count, customers_affected` |
| `analytics.district_live_snapshot` | `district, active_outages, affected_customers, avg_etr_minutes, risk_score, updated_at` |
| `analytics.district_risk_daily` | `district, risk_date, risk_score, risk_level, max_precip_mm, max_wind_kph, mean_temp_c, expected_outages` |
| `marts.outage_cause_daily` | `metric_date, cause_category, outage_count, affected_customers` |

## Key definitions / decisions

### Feed timestamp parsing (`macros/parse_feed_timestamp.sql`)
The Kavach feed has two IST wall-clock formats:
`DD-MON-YYYY HH:MM` (circle schema, e.g. `28-AUG-2026 08:00`) and
`DD-MM-YYYY HH:MM` (division/emergency schema, e.g. `26-08-2026 09:21`).

* Text is lower-cased, trimmed, `/` and `.` separators are unified to `-`, and
  whitespace collapsed.
* A `CASE` uses `to_timestamp(..., 'DD-MON-YYYY HH24:MI')` or
  `to_timestamp(..., 'DD-MM-YYYY HH24:MI')`.
* **Each branch is guarded by an anchored regex that validates day (01–31),
  month (01–12), hour (00–23) and minute (00–59)**. Postgres `to_timestamp()`
  raises on malformed input, so the regex guard is what prevents one bad feed
  row from aborting the whole dbt query — it yields `NULL` instead.
* Values are parsed as wall-clock and pinned with
  `::timestamp AT TIME ZONE 'Asia/Kolkata'`, so the result is a deterministic
  UTC instant regardless of the Postgres session timezone.
* `start_raw` / `end_raw` are preserved untouched for `marts.map_dots`
  (`start_text` / `end_text`), because the frontend parses the two raw formats
  itself.

### Duration, planned flag, cause taxonomy
* `duration_minutes = max(0, (end_ts - start_ts) in minutes)`; `NULL` when the
  end timestamp is missing. For the same-date division feed an end time earlier
  than start means midnight crossing, so one day is added before the clamp.
* `is_planned = (schema_type = 'circle')` — the circle schema is the planned
  outage notification feed, the division schema is the emergency shutdown feed.
* `cause_category` (`macros/classify_cause.sql`) buckets the free-text reason
  (plus outage type) into:
  `weather | tree_trimming | equipment_failure | maintenance | project_work | line_work | other`.
  Tests enforce this list with `accepted_values`.

### District resolution (`models/staging/stg_official_outages.sql`)
Mirrors `process.py.resolve_district()` exactly:
1. Normalize with `regexp_replace(lower(x), '[^a-z0-9]+', '', 'g')`.
2. For fields in priority order **division → subdivision → section → circle**,
   pick the **longest** `raw.district_aliases.alias` contained in the normalized
   field (aliases are seeded pre-normalized, incl. town aliases).
3. Fall back to exact upper-case circle in `raw.circle_exact`.
4. Fall back to `'Unknown'`.

Geocoding: normalized `gss` is joined to `raw.station_geocodes`
(`station_norm`, `district`); when there is no geocode row the district centroid
from `raw.district_centroids` is used, then the seed lat/lon, with
`geocoded = false`.

### Affected-customer estimate (`macros/estimated_affected_customers.sql`)
The feed has no customer counts, so for each outage:

```
share    = min(1, voltage_share * min(2, 1 + 0.02 * affected_area_count))
affected = min(district_customers, max(1, round(district_customers * share)))
```

`voltage_share`: 66/132/220/400 kV 2.0 %, 33 kV 1.0 %, 11 kV 0.35 %, LT/sub-11 kV
0.10 %, unknown 0.20 %. `district_customers` comes from
`seeds/district_profile.csv` (`customers` estimate). This feeds
`map_dots.affected_customers`, `reliability_daily` and `outage_cause_daily`.

### Dedupe / active flag (`models/staging/stg_outage_events.sql`)
`raw.official_outages.row_hash` is stable across batches (same sha1 recipe as
`process.py`; the Airflow loader preserves this). The view keeps the latest
payload per `row_hash` over **successful** batches only, and adds
`first_seen`/`last_seen` (earliest/latest batch start) and
`is_active = row still present in the latest successful batch`.

Published history is bounded by the `history_lookback_days` var (default 120
days) so the 30-minute `dbt run` stays cheap as the raw table grows (~1 scrape
batch every 30 min): `raw.official_outages` keeps the complete history, dbt
republishes the trailing window. The window is larger than
`reliability_lookback_days` (90), so reliability always has its full input.
`marts.map_dots` is not affected — it only reads the latest batch.

### Reliability (`models/analytics/reliability_daily.sql`)
Per district × IST day, over the trailing 90 days, zero-filled:
`SAIDI_min = Σ(duration_min × C_i) / N`, `SAIFI = Σ(C_i) / N`,
`CAIDI_min = SAIDI / SAIFI`, `ASAI_% = 100 × (1 − SAIDI/1440)` (floored at 0).
`C_i` = estimated affected customers of event *i*, `N` = district customers
(seed). Event durations are capped at 1440 min and outages are attributed to the
IST date of `start_ts`; planned and unplanned outages are both included.
Typical daily values: SAIDI < 30 min, SAIFI < 0.5, CAIDI 30–300 min,
ASAI 99–100 %; `tests/assert_reliability_range.sql` enforces the hard bounds
(SAIDI/SAIFI/CAIDI ≥ 0, 0 ≤ ASAI ≤ 100).

### Risk (`district_risk_daily`, `district_live_snapshot`)
* `district_risk_daily` joins `raw.weather_hourly` to the outage history:
  `risk_score = 0.30·min(1, precip/40) + 0.25·min(1, wind/50)
  + 0.10·min(1, max(0, temp−32)/8) + 0.35·min(1, expected_outages/8)`,
  where `expected_outages` is the mean daily outage count over the previous 14
  days. `risk_level` (aligned with the Go API + Spark):
  `severe ≥ 0.75 > high ≥ 0.50 > moderate ≥ 0.25 > low` (values
  `low | moderate | high | severe`).
  Rows exist for the last 30 days plus 3 forecast days.
* `district_live_snapshot.risk_score = 0.45·min(1, active_outages/10)
  + 0.35·min(1, affected_customers/customers)
  + 0.20·min(1, realtime reports last 15 min/10)`.
* `marts.map_dots` is exactly the latest **successful** scrape batch, excluding
  rows whose district resolved to `'Unknown'` (the map must not render a bogus
  district; unresolved rows are kept in `fct_outage_events`). An empty successful
  batch yields an empty mart, not an error.
* fct/analytics history is the trailing `history_lookback_days` (120) window of
  successful batches; raw history is never deleted by dbt.

### Seeds
`seeds/district_profile.csv` — 30 canonical Odisha districts (OSM spellings used
by the map GeoJSON): discom `zone` (central=TPCODL, north=TPNODL, south=TPSODL,
west=TPWODL), estimated `customers` (≈ population/4.3 households × 0.88
connections), 2011-census `population`, plausible `urban_share`, centroid
`lat`/`lon` from `data/mappings/district_centroids.json`. All values are
estimates, intended to be internally consistent rather than authoritative.

## Tests

Generic (`models/**/schema.yml`, `seeds/schema.yml`): `unique`/`not_null` on keys,
`accepted_values` for `cause_category`, `risk_level` and `zone`, plus
`relationships` from reliability/risk districts to `dim_district`.

Singular (`tests/`):
* `assert_duration_non_negative` — `duration_minutes >= 0` in fct + map_dots.
* `assert_reliability_range` — 0 ≤ ASAI ≤ 100, SAIDI/SAIFI/CAIDI ≥ 0, unique
  (district, metric_date) grain.
* `assert_risk_score_range` — risk scores in 0..1 and valid `risk_level`.
* `assert_map_dots_matches_latest_batch` — no join fan-out (severity `warn`:
  a new batch can land between model and test in a live pipeline).
* `assert_district_profile_covers_30` — dimension covers all 30 districts.

Cold start: with empty/scrape-less raw tables every model builds empty or
zero-filled results, and the tests above pass (the `warn` test is tolerant of
concurrent batches). `dbt seed` must run at least once (the 30-minute DAG runs
`dbt run` only; the bootstrap/daily DAGs seed first).

## Notes for integration

* `profiles.yml` reads `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`,
  `POSTGRES_USER`, `POSTGRES_PASSWORD` with safe defaults; password defaults to
  empty so a misconfigured deployment fails loudly instead of silently using a
  baked-in credential.
* `raw.user_reports` is declared as a source (and available to future models) but
  is not truncated by dbt.
* `ml.*` tables are created by `platform/postgres/init/02_raw.sql` for Spark.
