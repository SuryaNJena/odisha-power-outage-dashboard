# Odisha Outage Platform — Build Contract

Runtime scope: the platform schemas and streaming contracts below describe full
mode. Laptop mode uses the same PostgreSQL reports and official feed tables,
plus `ops.map_refresh(id, attempted, status)` for global refresh admission.
See [the runtime diagrams](ARCHITECTURE.md) for active services in each mode.


This file is the single source of truth for parallel work on the data platform.
Read it fully before writing code. Do not deviate from names, schemas, ports, or
paths defined here; integration depends on them.

## Mission

The existing app (Next.js static export + Go backend on :3001 + Python scraper)
becomes a small production data platform:

- **Airflow** orchestrates scraping (`kavach.tpodisha.com`) and hourly weather
  fetches (Open-Meteo), loads raw data into Postgres, runs dbt, publishes marts.
- **dbt (dbt-postgres)** owns all transformation: district resolution, parsing,
  dedupe, facts, aggregates, reliability indices, map marts.
- **Kafka** carries user outage reports, official outage snapshots, weather.
- **Flink SQL** consumes `user-reports`, de-duplicates, windows per district,
  detects emerging outages, emits `outage-alerts` + window metrics.
- **Spark** provides (a) daily batch ML/analytics and (b) a Structured Streaming
  job that sinks NRT metrics into Postgres for the dashboard.
- **Go API + Next.js** expose NRT metrics, alerts, reliability and risk pages.

## Ownership (do not edit outside your scope)

| Area | Paths | Owner |
|---|---|---|
| dbt, raw DDL, seeds | `platform/dbt/**`, `platform/postgres/init/**` | dbt-engineer |
| Airflow image, DAGs, loaders | `platform/airflow/**` | airflow-engineer |
| Kafka + Flink | `platform/kafka/**`, `platform/flink/**` | flink-engineer |
| Spark jobs | `platform/spark/**` | spark-engineer |
| Frontend pages | `src/app/**` | web-engineer |
| Compose, Go API, deploy, integration | `platform/docker-compose.yml`, `backend/**`, `scripts/**`, `README.md` | lead |

Never edit `platform/docker-compose.yml` (lead owns it). If you need a service
setting, put it in the contract notes at the end of your task and message lead.

## Runtime / versions

- Host: Debian 13, 4 vCPU, 7.5 GB RAM, Docker 29, compose v2.
- Postgres 16 (`postgres:16-alpine`), Kafka 3.9 KRaft (`apache/kafka:3.9.0`),
  Flink 1.20.1, Spark 3.5.4, Airflow 2.10.5 (python 3.12).
- All containers on one bridge network `odisha_net`.
- Timezone: `Asia/Kolkata` for display; store timestamps as `timestamptz` UTC.

## Services / hostnames / ports (inside compose network)

| Service | Hostname | Port | Published on host |
|---|---|---|---|
| app (Go) | `app` | 3001 | `127.0.0.1:3001` |
| postgres | `postgres` | 5432 | none |
| kafka | `kafka` | 9092 | none |
| flink jobmanager | `flink-jobmanager` | 8081 (REST/UI) | `127.0.0.1:8081` |
| flink taskmanager | `flink-taskmanager` | — | none |
| airflow | `airflow` | 8080 | `127.0.0.1:8080` |
| spark stream | `spark-stream` | — | none |

Shared env (compose injects into every service):

```
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_DB=odisha
POSTGRES_USER=odisha
POSTGRES_PASSWORD=<from .env>
DATABASE_URL=postgresql://odisha:<pw>@postgres:5432/odisha
KAFKA_BOOTSTRAP=kafka:9092
TZ=Asia/Kolkata
```

Extra for Airflow:

```
AIRFLOW_DB_CONN=postgresql+psycopg2://odisha:<pw>@postgres:5432/airflow
AIRFLOW__CORE__EXECUTOR=LocalExecutor
AIRFLOW__CORE__LOAD_EXAMPLES=False
AIRFLOW__CORE__DAGS_ARE_PAUSED_AT_CREATION=False
AIRFLOW__WEBSERVER__EXPOSE_CONFIG=False
```

Shared volume `odisha_app_data` mounted at `/opt/odisha/data` in Airflow and
`/app/data` in the app: Airflow writes `mart/outages.json` there; the Go app
reads it as a fallback when Postgres is unavailable.

## Postgres schemas

One database `odisha` with schemas: `raw`, `staging`, `analytics`, `marts`,
`realtime`, `ml`, `ops`. A second database `airflow` is Airflow metadata.

### raw (owned by dbt-engineer, DDL in `platform/postgres/init/02_raw.sql`)

```sql
raw.scrape_batches(
  batch_id text primary key,          -- ISO8601 UTC, e.g. 2026-11-19T12:30:00Z
  started_at timestamptz not null,
  finished_at timestamptz,
  source text not null default 'kavach',
  row_count integer default 0,
  status text not null default 'running',   -- running|success|failed
  detail jsonb
)

raw.official_outages(
  id bigserial primary key,
  batch_id text not null,
  row_hash text not null,
  source_file text,
  schema_type text not null,          -- 'circle' | 'division'
  circle text, division text, subdivision text, section text,
  otype text, gss text, voltage text, feeder text,
  start_raw text, end_raw text,
  reason text, areas text, remarks text,
  raw_row jsonb,
  ingested_at timestamptz not null default now(),
  unique (batch_id, row_hash)
)
create index on raw.official_outages (batch_id);
create index on raw.official_outages (row_hash);

raw.district_aliases(alias text primary key, district text not null)  -- normalized alias
raw.circle_exact(circle text primary key, district text not null)     -- upper-case circle
raw.district_centroids(district text primary key, lat double precision, lon double precision)
raw.station_geocodes(
  station_norm text not null, district text not null,
  lat double precision, lon double precision, display text,
  primary key (station_norm, district)
)

raw.user_reports(
  id bigserial primary key,
  report_id uuid not null unique,
  district text, gss text, feeder text,
  comment text, severity integer not null default 3,
  reporter text, lat double precision, lon double precision,
  reported_at timestamptz not null default now(),
  source text not null default 'web',
  kafka_offset bigint,
  created_at timestamptz not null default now()
)

raw.weather_hourly(
  id bigserial primary key,
  district text not null,
  observed_at timestamptz not null,
  temperature_c real, precipitation_mm real, wind_kph real,
  humidity_pct real, weather_code integer,
  source text not null default 'open-meteo',
  fetched_at timestamptz not null default now(),
  unique (district, observed_at)
)
```

### realtime (written by Spark streaming, read by Go API)

```sql
realtime.district_live_metrics(
  district text primary key,
  report_count integer not null default 0,      -- last 15 min
  unique_reporters integer not null default 0,
  official_outages integer not null default 0,
  unconfirmed_reports integer not null default 0,
  risk_score real,
  window_start timestamptz,
  window_end timestamptz,
  updated_at timestamptz not null default now()
)

realtime.report_minute(
  bucket timestamptz not null,
  district text not null,
  report_count integer not null,
  avg_severity real,
  primary key (bucket, district)
)

realtime.alerts(
  alert_id text primary key,
  district text,
  alert_type text not null,        -- report_spike | high_severity | official_outage
  severity text not null,          -- info|warning|critical
  message text not null,
  report_count integer,
  window_start timestamptz,
  window_end timestamptz,
  created_at timestamptz not null default now(),
  source text not null default 'flink'
)

realtime.flink_report_windows(
  district text, window_start timestamptz, window_end timestamptz,
  report_count integer, unique_reporters integer, avg_severity real,
  primary key (district, window_start)
)
```

### ops

```sql
ops.pipeline_heartbeat(component text primary key, status text not null,
  last_seen timestamptz not null default now(), detail jsonb)
ops.job_runs(job_name text, run_id text, status text not null,
  started_at timestamptz, finished_at timestamptz, detail jsonb,
  primary key (job_name, run_id))
```

### analytics / marts (written by dbt)

Required models and columns consumed by the API:

- `marts.map_dots` — one row per outage in the **latest successful batch**:
  `district, circle, division, gss, voltage, feeder, start_text, end_text,
   outage_date (date), reason, areas, lat, lon, geocoded (bool),
   is_planned (bool), duration_minutes (numeric), affected_customers (int),
   cause_category (text), etr (timestamptz)`.
  `start_text` / `end_text` must keep the original feed strings (frontend parses
  `DD-MON-YYYY HH:MM` and `DD-MM-YYYY HH:MM`).
- `analytics.dim_district` — `district, zone, customers, population, urban_share, lat, lon`.
- `analytics.fct_outage_events` — deduped history by `row_hash`:
  `row_hash, district, start_ts, end_ts, duration_minutes, is_planned,
   cause_category, voltage, feeder, gss, areas, reason, first_seen, last_seen, is_active`.
- `analytics.reliability_daily` — `district, metric_date, saidi_minutes, saifi,
   caidi_minutes, asai_pct, outage_count, customers_affected`.
- `analytics.district_live_snapshot` — `district, active_outages,
   affected_customers, avg_etr_minutes, risk_score, updated_at`.
- `analytics.district_risk_daily` — `district, risk_date, risk_score, risk_level,
   max_precip_mm, max_wind_kph, mean_temp_c, expected_outages`.
- `marts.outage_cause_daily` — `metric_date, cause_category, outage_count,
   affected_customers`.

### ml (written by Spark batch)

- `ml.outage_clusters` — `cluster_id int, lat, lon, outage_count, dominant_district, dominant_hour`.
- `ml.etr_model_metrics` — `model_date date, mae_minutes, r2, sample_count`.
- `ml.district_risk_forecast` — `district, forecast_date, risk_score, risk_level,
   expected_outages, max_precip_mm, max_wind_kph`.

## Kafka topics (3 partitions, replication 1, 24h retention unless noted)

| Topic | Producer | Consumer | Payload |
|---|---|---|---|
| `user-reports` | Go API | Flink, Spark | flat JSON (below) |
| `official-outages` | Airflow | Spark (optional) | one JSON per outage row, 30m |
| `weather-observations` | Airflow | Spark (optional) | one JSON per district-hour |
| `outage-alerts` | Flink | Spark, Go (optional) | alert JSON |
| `flink-report-windows` | Flink | Spark | window JSON |

`user-reports`:
```json
{"report_id":"uuid","district":"Khordha","gss":"","feeder":"","comment":"no power",
 "severity":3,"reporter":"web","lat":null,"lon":null,
 "reported_at":"2026-11-19T12:34:56Z"}
```
`outage-alerts`:
```json
{"alert_id":"flink-<district>-<window_end_epoch>","district":"Khordha",
 "alert_type":"report_spike","severity":"warning","message":"...",
 "report_count":7,"window_start":"...","window_end":"..."}
```

## dbt

- Project dir `platform/dbt`, profile `odisha`, target `dev`, threads 4.
- `dbt_project.yml` uses `+materialized: view` for staging, `table` for marts.
- Schema routing: `models/staging` → `staging`, `models/marts` → `marts`,
  `models/analytics` → `analytics`, seeds → `seeds`. Implement
  `generate_schema_name` macro in `macros/`.
- Sources declared in `models/sources.yml` over `raw` + `realtime` + `ml`.
- District resolution in SQL: normalize text with
  `regexp_replace(lower(x), '[^a-z0-9]+', '', 'g')`, match `raw.district_aliases`
  longest alias first, field priority division → subdivision → section → circle,
  then `raw.circle_exact`, then `'Unknown'`.
- Seeds: `district_profile.csv` (district, zone, customers, population,
  urban_share, lat, lon) — 30 Odisha districts; values may be estimates but
  plausible.
- Tests: `not_null`/`unique` on keys, `accepted_values` on risk_level and
  cause_category, custom `assert_duration_non_negative`,
  `assert_reliability_range` (0 <= asai_pct <= 100).

## Airflow DAGs (dag ids are part of the contract)

| dag id | schedule (IST) | purpose |
|---|---|---|
| `odisha_official_ingest` | `*/30 * * * *` | scrape → load raw → dbt run/test → publish mart JSON → Kafka |
| `odisha_weather_ingest` | `5 * * * *` | Open-Meteo hourly forecast → raw + Kafka |
| `odisha_dbt_daily` | `30 2 * * *` | full dbt run + tests + docs generate |
| `odisha_spark_batch` | `0 3 * * *` | spark-submit daily batch job |
| `odisha_platform_health` | `*/10 * * * *` | heartbeat checks, re-submit Flink SQL if job missing |
| `odisha_bootstrap` | `None` (manual) | load reference data, first scrape, first dbt run |

DAGs live in `platform/airflow/dags/`, shared helpers in `odisha_common.py`.
No hardcoded credentials; read env vars with sane defaults.

## Flink

- Dockerfile in `platform/flink/Dockerfile` based on `flink:1.20.1-scala_2.12`,
  bakes SQL connector jars (Kafka, JDBC, Postgres driver) from Maven Central.
- SQL job `platform/flink/jobs/user_report_alerts.sql` with:
  - Kafka source `user-reports` (JSON, event time on `reported_at`, watermark 30s)
  - de-dupe by `report_id`
  - 10-minute tumbling windows per district → counts
  - alert when `count >= 3` (report_spike/warning) or `count >= 8` (critical)
  - sink `outage-alerts` (Kafka JSON)
  - sink `realtime.flink_report_windows` (JDBC, append-only)
- `platform/flink/submit.sh` submits via `sql-client.sh -f` (detached).
- Job name: `odisha-user-report-alerts`.

## Spark

- `platform/spark/jobs/stream_live_metrics.py`: Structured Streaming, Kafka
  `user-reports` + `outage-alerts`, 1-minute windows, writes
  `realtime.district_live_metrics`, `realtime.report_minute`, `realtime.alerts`,
  updates `ops.pipeline_heartbeat('spark-stream')`. Checkpoint under
  `/opt/spark-checkpoints`.
- `platform/spark/jobs/daily_batch.py`: reads Postgres, writes `ml.outage_clusters`,
  `ml.etr_model_metrics`, `ml.district_risk_forecast`, and archives parquet under
  `/opt/odisha/data/lake/outages/dt=YYYY-MM-DD/`.
- Both accept `--jdbc-url`, `--jdbc-user`, `--jdbc-password`, `--bootstrap`.
  Defaults come from env.

## Go API (lead owns; do not edit)

Endpoints the frontend may call: `/api/mapdata`, `/api/realtime`,
`/api/reliability`, `/api/risk`, `/api/report` (POST), `/api/health`.

`/api/realtime` shape (frontend contract):
```json
{"available":true,"updatedAt":"...","windowMinutes":15,
 "totals":{"reports":12,"districts":5,"alerts":2,"officialOutages":31},
 "districts":[{"district":"Khordha","reports":5,"uniqueReporters":4,
   "officialOutages":2,"unconfirmedReports":3,"riskScore":0.42,
   "riskLevel":"moderate","updatedAt":"..."}],
 "alerts":[{"alertId":"...","district":"...","alertType":"report_spike",
   "severity":"warning","message":"...","reportCount":4,
   "windowStart":"...","windowEnd":"...","createdAt":"..."}],
 "trend":[{"bucket":"2026-11-19T12:30:00Z","reports":3}],
 "pipeline":{"kafka":"ok","flink":"ok","spark":"ok","lastSparkUpdate":"..."}}
```

`/api/reliability` shape:
```json
{"available":true,"days":30,
 "districts":[{"district":"Khordha","saidiMinutes":123.4,"saifi":1.2,
   "caidiMinutes":102.8,"asaiPct":99.9,"outageCount":9,"customersAffected":12000}],
 "trend":[{"date":"2026-11-01","saidiMinutes":4.2,"saifi":0.01}]}
```

`/api/risk` shape:
```json
{"available":true,"updatedAt":"...","districts":[{"district":"...",
 "riskScore":0.5,"riskLevel":"moderate","expectedOutages":2,
 "maxPrecipMm":8.2,"maxWindKph":35,"forecastDate":"2026-11-20"}]}
```

## Coding rules

- Python 3.12, PEP8-ish, type hints where useful, no secrets in code.
- Every script must run standalone with `--help` or documented env vars.
- SQL: lowercase keywords is fine, but be consistent; always schema-qualify.
- JS: existing app uses plain JS (no TS), inline styles + `globals.css` classes.
- No new npm dependencies unless unavoidable; ECharts is already installed.
- Do not add files outside your scope; do not touch `node_modules`, `.next`,
  `out`, `data/odisha.db`.
- Local Windows sandbox blocks capturing external-process stdout; if you must
  run a command, wrap it as `cmd /c "prog args > .tmp\log.txt 2>&1"` and read
  the file. Do not run Docker (lead deploys).
