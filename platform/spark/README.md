# Spark jobs — Odisha power-outage platform

Spark 3.5.4 (Java 17) jobs for the Odisha outage platform. They are the only
components that write the `realtime`, `ml` and `ops.job_runs` data the Go API
serves to the dashboard. Everything here follows `platform/CONTRACT.md`.

| File | Purpose | Writes |
|---|---|---|
| `jobs/stream_live_metrics.py` | Structured Streaming: Kafka → 1-minute district windows | `realtime.report_minute`, `realtime.district_live_metrics`, `realtime.alerts`, `ops.pipeline_heartbeat` |
| `jobs/daily_batch.py` | Daily batch: KMeans clusters, ETR regression, next-24h risk, Parquet archive | `ml.outage_clusters`, `ml.etr_model_metrics`, `ml.district_risk_forecast`, `ops.job_runs`, Parquet lake |

Only `pyspark` and `psycopg2` are used (no pandas, no UDFs, no extra packages).

---

## 1. Runtime prerequisites

Both jobs run inside the existing **`odisha-airflow:2.10.5`** image (built from
`platform/airflow/Dockerfile`) — there is no separate Spark image and nothing
extra needs to be installed for these scripts. That image already provides:

* Apache Airflow 2.10.5 on **Python 3.12**, **OpenJDK 17** (`JAVA_HOME` pre-set),
  `pyspark==3.5.4` and `psycopg2-binary==2.9.10`.
* The **Postgres JDBC driver** at the contract path
  `/opt/jdbc/postgresql-42.7.4.jar`.
* The Kafka connector `org.apache.spark:spark-sql-kafka-0-10_2.12:3.5.4` is
  fetched by `--packages` on the first start; the compose service keeps
  `/root/.ivy2` on a named volume so later restarts start offline.
* Only `pyspark` + `psycopg2` are imported — no pandas/pandas-on-Spark, no UDFs,
  no extra packages.

Runtime layout provided by `platform/docker-compose.yml` (lead-owned):

| Host | Container path | Service | Used by |
|---|---|---|---|
| repository root | `/opt/odisha` (read-only) | `spark-stream` | streaming job, `/opt/odisha/platform/spark/jobs/stream_live_metrics.py` |
| repository root | `/opt/odisha` (read-write) | `airflow` | daily job, `/opt/odisha/platform/spark/jobs/daily_batch.py` |
| `odisha_spark_ckpt` volume | `/opt/spark-checkpoints` | `spark-stream` | stream checkpoints |
| `odisha_spark_ivy` volume | `/root/.ivy2` | `spark-stream` | `--packages` cache |
| JDBC jar | `/opt/jdbc/postgresql-42.7.4.jar` | both | JDBC reads/writes |
| `odisha_app_data` volume | `/opt/odisha/data` (Airflow) / `/opt/odisha-data` (app, spark-stream) | both | Parquet archive (`DATA_DIR/lake/outages`) |

### Environment variables (defaults in the scripts)

| Variable | Default | Job | Meaning |
|---|---|---|---|
| `KAFKA_BOOTSTRAP` | `kafka:9092` | stream | Kafka bootstrap servers |
| `DATABASE_URL` | — | both | parsed for host/port/db/user/pw defaults |
| `POSTGRES_HOST` / `POSTGRES_PORT` / `POSTGRES_DB` | `postgres` / `5432` / `odisha` | both | JDBC url defaults |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` | `odisha` / `odisha` | both | credentials defaults |
| `SPARK_JDBC_URL` / `SPARK_JDBC_USER` / `SPARK_JDBC_PASSWORD` | — | both | explicit overrides |
| `SPARK_CHECKPOINT_DIR` | `/opt/spark-checkpoints/stream-live` | stream | checkpoint root |
| `SPARK_TRIGGER_INTERVAL` | `30 seconds` | stream | `processingTime` trigger |
| `SPARK_MAX_OFFSETS_PER_TRIGGER` | `5000` | stream | Kafka `maxOffsetsPerTrigger` |
| `SPARK_KAFKA_TOPIC_REPORTS` / `SPARK_KAFKA_TOPIC_ALERTS` | `user-reports` / `outage-alerts` | stream | topics |
| `SPARK_HEARTBEAT_INTERVAL` | `20` | stream | keepalive heartbeat seconds |
| `SPARK_LAKE_DIR` | `$DATA_DIR/lake/outages` = `/opt/odisha/data/lake/outages` | daily | Parquet archive root |
| `DATA_DIR` | `/opt/odisha/data` | daily | shared-volume root the archive path is built from |
| `SPARK_RUN_DATE` | today (UTC) | daily | business date of the run |
| `SPARK_ARCHIVE_DATE` | `run_date - 1 day` | daily | event date to archive |
| `SPARK_RUN_ID` | `run_date` | daily | `ops.job_runs.run_id` |
| `SPARK_CLUSTER_K` / `SPARK_RANDOM_SEED` | `8` / `42` | daily | KMeans / split seed |
| `SPARK_LOG_LEVEL` / `SPARK_PY_LOG_LEVEL` | `WARN` / `INFO` | both | logging |

---

## 2. spark-submit commands

### 2.1 Streaming job (`spark-stream`, long running)

```bash
# Exactly what the `spark-stream` compose service runs (bash -c entrypoint):
SPARK_HOME=$(python3 -c 'import pyspark,os;print(os.path.dirname(pyspark.__file__))')
exec "$SPARK_HOME/bin/spark-submit" \
  --master 'local[2]' \
  --packages org.apache.spark:spark-sql-kafka-0-10_2.12:3.5.4 \
  --jars /opt/jdbc/postgresql-42.7.4.jar \
  --conf spark.sql.shuffle.partitions=4 \
  --conf spark.driver.memory=512m \
  --conf spark.executor.memory=512m \
  --conf spark.ui.enabled=false \
  /opt/odisha/platform/spark/jobs/stream_live_metrics.py
```

No application arguments are needed in compose: Kafka bootstrap, JDBC url,
credentials and the checkpoint dir all come from the shared environment
(`KAFKA_BOOTSTRAP`, `POSTGRES_*`/`DATABASE_URL`, `SPARK_CHECKPOINT_DIR`).
For a manual/debug run with explicit parameters:

```bash
"$SPARK_HOME/bin/spark-submit" --master 'local[2]' \
  --packages org.apache.spark:spark-sql-kafka-0-10_2.12:3.5.4 \
  --jars /opt/jdbc/postgresql-42.7.4.jar \
  --conf spark.sql.shuffle.partitions=4 --conf spark.driver.memory=512m \
  --conf spark.sql.streaming.stopTimeout=30s \
  /opt/odisha/platform/spark/jobs/stream_live_metrics.py \
  --bootstrap kafka:9092 \
  --jdbc-url jdbc:postgresql://postgres:5432/odisha \
  --jdbc-user odisha --jdbc-password "$POSTGRES_PASSWORD" \
  --checkpoint-dir /opt/spark-checkpoints/stream-live
```

* `--master 'local[2]'`: the host has 4 vCPU shared with Postgres/Kafka/Flink/
  Airflow; two cores keeps the streaming driver responsive and leaves headroom.
  Never use `local[*]` on this box.
* `spark.driver.memory=512m`: the streaming state is tiny (1-minute windows,
  10-minute watermark, ~30 districts). 512m heap is enough; the JVM adds
  ~384 MiB of overhead inside the 1200m container limit.
* `--packages` needs Maven Central once (first container start). `odisha_spark_ivy`
  keeps the resolved connector so restarts do not need the network.
* The scripts force the Spark session timezone to UTC; add
  `--conf spark.driver.extraJavaOptions=-Duser.timezone=UTC` only if the JVM
  default from `TZ=Asia/Kolkata` ever leaks into third-party logging.

### 2.2 Daily batch job (Airflow DAG `odisha_spark_batch`, 03:00 IST)

```bash
# Exactly what odisha_common.spark_submit_argv() asks Airflow to run:
SPARK_HOME=$(python3 -c 'import pyspark,os;print(os.path.dirname(pyspark.__file__))')
"$SPARK_HOME/bin/spark-submit" \
  --master 'local[2]' \
  --jars /opt/jdbc/postgresql-42.7.4.jar \
  /opt/odisha/platform/spark/jobs/daily_batch.py \
  --jdbc-url jdbc:postgresql://postgres:5432/odisha
```

Credentials are **not** on the command line: the DAG passes only `--jdbc-url`,
so the job falls back to the inherited `POSTGRES_USER`/`POSTGRES_PASSWORD`
(and `DATABASE_URL` when present). The daily job does not read Kafka, so no
`--packages` is required. It runs inside the Airflow container, which has the
`odisha_app_data` volume at `/opt/odisha/data`, so `DATA_DIR=/opt/odisha/data`
is picked up automatically and the archive lands in
`/opt/odisha/data/lake/outages/dt=...`.

Defaults: `--run-date` = today (UTC), `--archive-date` = yesterday, `--run-id` =
run date. Rerunning the same day updates the same `ops.job_runs` row (pass
`--run-id` for a unique row per attempt).

Backfill / re-run examples:

```bash
# Rebuild ML for a specific business date and archive that date
"$SPARK_HOME/bin/spark-submit" --master 'local[2]' \
  --jars /opt/jdbc/postgresql-42.7.4.jar \
  /opt/odisha/platform/spark/jobs/daily_batch.py \
  --run-date 2026-11-19 --archive-date 2026-11-19 --run-id backfill-2026-11-19

# Tune KMeans / seed
"$SPARK_HOME/bin/spark-submit" --master 'local[2]' \
  --jars /opt/jdbc/postgresql-42.7.4.jar \
  /opt/odisha/platform/spark/jobs/daily_batch.py --k 8 --seed 42
```

### 2.3 Useful flags (both jobs)

```bash
python -m py_compile platform/spark/jobs/stream_live_metrics.py   # syntax check
"$SPARK_HOME/bin/spark-submit" /opt/odisha/platform/spark/jobs/daily_batch.py --help
docker compose logs -f spark-stream                               # streaming job log
```

---

## 3. Streaming query graph

```
                 ┌──────────────────────────────────────────────────────────────┐
Kafka            │ user-reports                                                 │
topic            │   value → from_json(explicit schema)                         │
                 │   event_ts = coalesce(reported_at parsed, Kafka record ts)   │
                 │   filter null/blank district, watermark 10 minutes           │
                 │   groupBy(window(event_ts, '1 minute'), district)            │
                 │      count(*)                              → report_count     │
                 │      approx_count_distinct(reporter)       → unique_reporters│
                 │      avg(severity)                         → avg_severity     │
                 │   foreachBatch(report_sink)      (every 30 s)                │
                 │      1. upsert realtime.report_minute                        │
                 │      2. recompute 15-min view from realtime.report_minute    │
                 │         and upsert realtime.district_live_metrics            │
                 │      3. upsert ops.pipeline_heartbeat('spark-stream','ok')   │
                 └──────────────────────────────────────────────────────────────┘

                 ┌──────────────────────────────────────────────────────────────┐
Kafka            │ outage-alerts                                                │
topic            │   value → from_json(explicit schema)                         │
                 │   foreachBatch(alert_sink)       (every 30 s)                │
                 │      1. insert realtime.alerts ... on conflict do nothing    │
                 │      2. heartbeat                                            │
                 └──────────────────────────────────────────────────────────────┘
```

Two independent streams, two independent checkpoints
(`<SPARK_CHECKPOINT_DIR>/user-reports` and `<SPARK_CHECKPOINT_DIR>/outage-alerts`).
There is no stream-stream join.

Also running: a daemon **keepalive heartbeat** thread writes
`ops.pipeline_heartbeat` every 20 s with the last batch id/age. That way
`/api/health` still reports `spark: ok` when no reports are flowing and
`degraded` (instead of dying) while upstream dbt tables do not exist yet.

### Kafka source options (unchanged from the contract)

`startingOffsets=earliest`, `failOnDataLoss=false`,
`maxOffsetsPerTrigger=5000`. `earliest` only takes effect for a brand-new
checkpoint — a restarted query resumes from the checkpointed offsets.

---

## 4. SQL written by the streaming job

`realtime.report_minute` — one row per (bucket = 1-minute window start, district):

```sql
insert into realtime.report_minute (bucket, district, report_count, avg_severity)
values %s
on conflict (bucket, district) do update
set report_count = excluded.report_count,
    avg_severity  = excluded.avg_severity
where realtime.report_minute.report_count is distinct from excluded.report_count
   or realtime.report_minute.avg_severity is distinct from excluded.avg_severity;
```

`realtime.district_live_metrics` — recomputed from the last 15 minutes of
`realtime.report_minute` in the same transaction as the insert above:

```sql
with win as (
    select district, sum(report_count)::int as report_count
    from realtime.report_minute
    where bucket >= now() - interval '15 minutes' and bucket <= now()
    group by district
),
batch_unique as (                          -- unique_reporters ≈ max of the batch
    select key as district, value::int as unique_reporters
    from jsonb_each_text(%(unique_json)s::jsonb)
),
latest_risk as (
    select distinct on (district) district, risk_score
    from analytics.district_risk_daily order by district, risk_date desc
)
insert into realtime.district_live_metrics
    (district, report_count, unique_reporters, official_outages,
     unconfirmed_reports, risk_score, window_start, window_end, updated_at)
select w.district, w.report_count,
       coalesce(bu.unique_reporters,
                least(coalesce(ex.unique_reporters, w.report_count), w.report_count))::int,
       coalesce(s.active_outages, 0)::int,
       greatest(w.report_count - coalesce(s.active_outages, 0), 0)::int,
       coalesce(r.risk_score, ex.risk_score),
       now() - interval '15 minutes', now(), now()
from win w
left join batch_unique bu on bu.district = w.district
left join analytics.district_live_snapshot s on s.district = w.district
left join latest_risk r on r.district = w.district
left join realtime.district_live_metrics ex on ex.district = w.district
on conflict (district) do update
set report_count        = excluded.report_count,
    unique_reporters    = excluded.unique_reporters,
    official_outages    = excluded.official_outages,
    unconfirmed_reports = excluded.unconfirmed_reports,
    risk_score          = coalesce(excluded.risk_score,
                                   realtime.district_live_metrics.risk_score),
    window_start        = excluded.window_start,
    window_end          = excluded.window_end,
    updated_at          = now();
```

Each batch also (in the same transaction):

* zeroes districts whose last report fell out of the 15-minute window
  (`report_count = unique_reporters = unconfirmed_reports = 0`) so the API never
  shows phantom reports;
* prunes `realtime.report_minute` older than 24 hours (the `/api/realtime` trend
  only needs recent buckets);
* upserts the heartbeat:
  `insert into ops.pipeline_heartbeat (component, status, last_seen, detail)
   values ('spark-stream', 'ok', now(), $json::jsonb)
   on conflict (component) do update set ...` with `detail = {"stream",
   "batch_id", "rows", "minute_rows", "districts"}`.

`realtime.alerts`:

```sql
insert into realtime.alerts
    (alert_id, district, alert_type, severity, message, report_count,
     window_start, window_end, source)
values %s
on conflict (alert_id) do nothing;
```

---

## 5. Daily batch job details

### 5.1 Reads (JDBC, graceful)

`analytics.fct_outage_events`, `analytics.dim_district`, `raw.weather_hourly`,
`analytics.reliability_daily` via `spark.read.jdbc` with
`/opt/jdbc/postgresql-42.7.4.jar`. Each read is best effort:

* missing table (dbt not run yet) or missing required column → warning, the
  dependent step is skipped, the job continues;
* empty table → logged, dependent step skipped, **job still exits 0**;
* the `ops.job_runs` row is always written (status `success`/`failed`, times,
  JSON detail with row counts, warnings, errors).

### 5.2 `ml.outage_clusters` (KMeans)

Features `(lat, lon, local_hour)`; `local_hour` is the outage start hour in
`Asia/Kolkata` (local business hour; timestamps stay UTC in Postgres). `lat/lon`
come from `analytics.dim_district` (centroids) joined on district. `k=8`,
`seed=42`, `maxIter=50`. For each cluster:

* `lat`, `lon` = mean of assigned points (== KMeans centroid),
* `outage_count` = number of points,
* `dominant_district` / `dominant_hour` = most frequent district / local hour.

Written with `delete from ml.outage_clusters` + bulk insert in one transaction.
Skipped (with a warning) when there are fewer than `k` geocoded outage events or
no `dim_district` rows.

### 5.3 `ml.etr_model_metrics` (LinearRegression)

Label `duration_minutes` (non-null, `>= 0`). Features:
`voltage_class` (0 unknown/LT, 1 ≤11 kV, 2 ≤33 kV, 3 higher), `is_planned` (0/1),
`hour` (IST), `district_idx` (`StringIndexer(handleInvalid='keep')`).
`regParam=0.1`, `elasticNetParam=0`, `maxIter=100`. 80/20 `randomSplit` with
seed 42 when there are ≥10 labelled rows (otherwise fit/evaluate on all rows);
`mae_minutes` and `r2` come from `RegressionEvaluator` on the evaluation split.
Upserted per `model_date` (`delete where model_date = ...` + insert).

### 5.4 `ml.district_risk_forecast` (next 24h)

Per district (all `analytics.dim_district` districts, or the union of
weather/outage districts if the dimension is empty):

```
max_precip_mm / max_wind_kph = max over the next 24 h of the *latest weather
                               fetch per district* (fallback: latest fetch rows)
outage_30d                  = fct_outage_events starting in the last 30 days
                              (fallback: sum of reliability_daily.outage_count)
precip_norm  = least(max_precip_mm / 50, 1)
wind_norm    = least(max_wind_kph / 70, 1)
weather_score = 0.65 * precip_norm + 0.35 * wind_norm
outage_norm  = least(outage_30d / max(1, max(outage_30d over districts)), 1)
risk_score   = clamp(0.60 * weather_score + 0.40 * outage_norm, 0, 1)   [0..1]
risk_level   = low (<0.25), moderate (<0.50), high (<0.75), severe (>=0.75)
expected_outages = round(outage_30d / 30 * (1 + 1.5 * weather_score))
forecast_date = run_date + 1 day
```

Replaced per `forecast_date` in one transaction. If there is no weather and no
outage history at all, the step is skipped with a warning.

### 5.5 Parquet archive

`<SPARK_LAKE_DIR>/dt=YYYY-MM-DD/` (default `/opt/odisha/data/lake/outages`,
exposed to the Go app under `/app/data` through the shared `odisha_app_data`
volume). The archived day defaults to `run_date - 1 day` (the completed UTC day
for a 03:00 IST run); pass `--archive-date` to backfill. Written with
`mode("overwrite")` for that day (partition-level idempotency); all
`analytics.fct_outage_events` columns are kept. `partitionBy district` is left
off — the `dt=` partition is the contract path; add it in `write_parquet_archive`
if a lake consumer needs district sub-partitions.

### 5.6 `ops.job_runs`

`job_name='spark-daily'`, `run_id` = `--run-id`/`SPARK_RUN_ID` (default: run
date), `status` (`running` → `success`/`failed`), `started_at`/`finished_at`,
and a JSON `detail` with run dates, source row counts, cluster/ETR/risk/archive
summaries, warnings and errors. Written best-effort at start and in the final
`ml` transaction, so the dashboard/logs always see a row.

---

## 6. Empty-table / degraded behaviour (summary)

| Situation | Streaming job | Daily job |
|---|---|---|
| Kafka topic empty | keeps running, heartbeat stays fresh | n/a |
| dbt tables missing | sink logs, heartbeat status `degraded`, retries every batch; never crash-loops | read warning, dependent ML steps skipped |
| source tables empty | n/a | all ML/archive steps skipped with warnings, `ops.job_runs` row written, **exit 0** |
| Postgres down | connection retries (3× backoff) then query fails → container restart | retries up to 3×, then `failed` job_runs (best effort) and exit 1 |
| malformed Kafka JSON/timestamp | row parsed to nulls and filtered out; `timeParserPolicy=CORRECTED` prevents aborts | n/a |
| duplicate alert_id | `on conflict do nothing` | n/a |

---

## 7. Checkpoint notes

* Checkpoints live in `SPARK_CHECKPOINT_DIR` (default
  `/opt/spark-checkpoints/stream-live`), one subdirectory per query
  (`user-reports/`, `outage-alerts/`). Compose already mounts the
  `odisha_spark_ckpt` named volume on `/opt/spark-checkpoints` — keep it that
  way: the container writable layer would lose Kafka offsets on recreate.
* The checkpoint stores the Kafka offsets, the watermark state and the query
  schema. Deleting it makes the query restart at `startingOffsets=earliest`.
* **Do not** reuse a checkpoint after changing the query graph, the JSON schema,
  the watermark/window values or the Spark minor version — delete the old
  directory before restarting (otherwise the query fails to start with a
  `SparkException: ... incompatible` state error).
* Two queries must never share a checkpoint directory.
* A checkpoint is not a backup: `realtime.alerts` dedupes by `alert_id` and the
  metric tables are upserts, so replays are safe; the streaming job is
  at-least-once, not exactly-once, against Postgres.
* Graceful stop: `docker stop` → SIGTERM → Spark's shutdown hook stops both
  queries (`stopTimeout=30s`); the Python SIGTERM handler also calls
  `query.stop()`. The compose service should set `stop_grace_period: 45s` so
  Docker's 10 s default does not pre-empt the flush (see §10).
* To reset the stream deliberately: `docker compose stop spark-stream`, remove
  the checkpoint volume contents (`docker compose run --rm --entrypoint bash
  spark-stream -c 'rm -rf /opt/spark-checkpoints/stream-live/*'`), start it again.

---

## 8. Memory / parallelism tuning for a 4 vCPU / 7.5 GB host

The host also runs Postgres, Kafka, Flink and Airflow, so Spark gets a small
slice: **local[2] + 512m driver heap** for both jobs.

| Setting | Value | Why |
|---|---|---|
| `--master` | `local[2]` | leave 2 vCPU for Postgres/Kafka/Flink/Airflow |
| `spark.driver.memory` | `512m` | windows/state are tiny; 512m heap + ~384m JVM overhead inside the 1200m `mem_limit` |
| `spark.executor.memory` | `512m` | inert in local mode; kept so the compose command stays cluster-ready |
| `spark.sql.shuffle.partitions` | `4` | small data; 200 default partitions would waste time |
| `spark.driver.maxResultSize` | `256m` (default) | safety net for the tiny `collect()` calls |
| `spark.sql.adaptive.enabled` | `true` | already set for the daily job |
| `spark.sql.streaming.stopTimeout` | `30s` | graceful shutdown bound (set in the script) |

The daily job only collects at most ~30 district rows / 8 cluster rows; the
`fct_outage_events` table is the only potentially large source. If it grows past
a few hundred thousand rows, raise the daily driver to `1g` and
`spark.sql.shuffle.partitions` to `8` before touching the streaming job.

---

## 9. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `ClassNotFoundException: org.postgresql.Driver` | missing `--jars /opt/jdbc/postgresql-42.7.4.jar` |
| `NoClassDefFoundError ... kafka` / `KafkaDataConsumer` | missing `--packages org.apache.spark:spark-sql-kafka-0-10_2.12:3.5.4` (or no Maven access; bake the package into the image) |
| `ModuleNotFoundError: psycopg2` | wrong interpreter: spark-submit must use the image's `python3` where `psycopg2-binary==2.9.10` is installed (the `odisha-airflow` image does this) |
| heartbeat `degraded` in `/api/health` | `analytics.*` tables do not exist yet (dbt run pending) or the DB is unreachable; the stream retries every 30 s |
| `SparkException: ... checkpoint ... incompatible` | old checkpoint after code/schema change — delete the checkpoint dir |
| Kafka warnings `group.id not set` | expected: offsets are owned by the checkpoint, not a consumer group |
| daily job `ops.job_runs.status = failed` | inspect `detail.errors`/`problems` in `ops.job_runs`; JDBC/`ml` write failures fail the run, missing/empty sources do not |
| wrong hours in `ml.outage_clusters.dominant_hour` | hours are IST (`Asia/Kolkata`); raw timestamps stay UTC |

---

## 10. Compose / Airflow integration (verified against the repo)

These scripts are already wired into the lead-owned `platform/docker-compose.yml`
and the airflow-engineer's `platform/airflow/**`; this section records what was
verified and the few residual recommendations.

Verified:

1. `spark-stream` runs the `odisha-airflow:2.10.5` image (PySpark 3.5.4, Java 17,
   Python 3.12, psycopg2-binary 2.9.10, JDBC jar at the contract path), with the
   repository mounted read-only at `/opt/odisha`, the `odisha_spark_ckpt` volume
   at `/opt/spark-checkpoints`, the `odisha_spark_ivy` volume at `/root/.ivy2`
   and the common env (`POSTGRES_*`, `DATABASE_URL`, `KAFKA_BOOTSTRAP`,
   `TZ=Asia/Kolkata`). The job needs no application arguments in compose — all
   defaults come from those env vars.
2. The Airflow DAG `odisha_spark_batch` calls
   `odisha_common.run_spark_submit("<repo>/platform/spark/jobs/daily_batch.py",
   app_args=["--jdbc-url", jdbc_url()])`, which prepends
   `--master local[2] --jars /opt/jdbc/postgresql-42.7.4.jar` and leaves
   credentials in the environment — both flags are supported by
   `daily_batch.py`. The Airflow container mounts the shared
   `odisha_app_data` volume at `/opt/odisha/data`, so `DATA_DIR` drives the
   default archive root (`/opt/odisha/data/lake/outages`).
3. The DDL in `platform/postgres/init/02_raw.sql` matches every table/column the
   jobs write (`realtime.*`, `ops.*`, `ml.*`), including the `ml` tables without
   primary keys (hence the DELETE+INSERT-per-batch strategies).

Residual recommendations for the lead (compose is lead-owned, scripts need no
change):

* Add `stop_grace_period: 45s` to `spark-stream` so `docker stop` lets the
  `stopTimeout=30s` graceful query stop complete (Docker's default is 10 s).
* Optionally add a `healthcheck` for `spark-stream`, e.g.
  `pg_isready`-independent: check the heartbeat row freshness with a tiny python
  one-liner, or rely on the Go API's `/api/health` (it already flags `stale`
  after 3 minutes without a heartbeat).
* The first `spark-stream` start downloads `spark-sql-kafka-0-10_2.12:3.5.4`
  from Maven Central; the `odisha_spark_ivy` volume keeps it afterwards. For a
  fully offline deploy, pre-bake the two jars into the image and switch
  `--packages` to `--jars`.
* `spark-stream` currently depends on `airflow: service_started`; this is only
  for image/build ordering — the job itself waits/retries on Postgres and Kafka.
* No Spark service needs the `odisha_app_data` volume for the *streaming* job;
  the daily job gets it through the Airflow container.
