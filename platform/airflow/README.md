# Airflow orchestration — Odisha power-outage platform

Airflow 2.10.5 image + DAGs + standalone scripts that orchestrate scraping
(`kavach.tpodisha.com`), hourly weather forecasts (Open-Meteo), raw loading into
Postgres, dbt runs/tests, mart-JSON publication for the Go API/static frontend,
Kafka publication and platform health.

This directory is the **only write scope of the airflow-engineer** (see
`platform/CONTRACT.md`); the compose file, DDL, dbt project, Flink/Spark jobs,
Go API and frontend are owned by their respective authors.

---

## 1. Files

```
platform/airflow/
├── Dockerfile                  # apache/airflow:2.10.5-python3.12 + JDK17 + /opt/dbt venv + JDBC jar
├── requirements.txt            # Airflow-env pip deps (constraint-compatible pins)
├── requirements-dbt.txt        # isolated /opt/dbt venv deps (dbt-postgres~=1.9)
├── README.md                   # this file
├── dags/
│   ├── odisha_common.py        # shared config/helpers (env, Postgres, Kafka, dbt, spark, flink, ops)
│   ├── dag_official_ingest.py  # odisha_official_ingest   */30
│   ├── dag_weather_ingest.py   # odisha_weather_ingest    5 * * * *
│   ├── dag_dbt_daily.py        # odisha_dbt_daily         30 2 * * *
│   ├── dag_spark_batch.py      # odisha_spark_batch       0 3 * * *
│   ├── dag_platform_health.py  # odisha_platform_health   */10
│   └── dag_bootstrap.py        # odisha_bootstrap         schedule=None (manual)
└── scripts/
    ├── scrape_official.py      # reuses repo scrap.py -> CSVs + raw.scrape_batches
    ├── load_official.py        # CSVs -> raw.official_outages (process.py parse/row_hash semantics)
    ├── load_reference.py       # data/mappings/*.json -> raw reference tables
    ├── fetch_weather.py        # Open-Meteo (one multi-coordinate request) -> raw.weather_hourly
    ├── publish_mart.py         # marts.map_dots -> $DATA_DIR/mart/outages.json
    └── kafka_util.py           # idempotent topic creation + retrying JSON producer
```

## 2. Build & runtime

Build **from the repository root** (the build context must contain `scrap.py`,
`process.py` and `data/mappings/`; the root `.dockerignore` keeps the context small
but must not exclude `data/mappings`):

```bash
docker build -f platform/airflow/Dockerfile -t odisha-airflow:2.10.5 .
```

`platform/docker-compose.yml` (owned by the lead) builds exactly this and runs:

* **runtime:** `airflow db migrate` → admin user → **`airflow standalone`** with
  `LocalExecutor`; metadata DB = Postgres database `airflow`;
* **user:** `user: "0:0"` (root). This is intentional for this demo stack: the
  repository is bind-mounted read-write at `/opt/odisha` and the shared data volume
  is mounted at `/opt/odisha/data`, so root avoids UID-50000 permission problems.
  The official Apache Airflow image entrypoint merely execs the command — it does
  **not** drop privileges, so `standalone` runs as root and logs a harmless
  "running as root" warning. For a production deployment use the `airflow` user and
  pre-create/chown the writable directories instead.
* **ports:** `127.0.0.1:8080`, healthcheck `curl -fsS http://localhost:8080/health`
  (curl is installed by the image).
* **mounts:** repo `:/opt/odisha` (rw), `platform/airflow/dags:/opt/airflow/dags:ro`,
  `data/mappings:/opt/odisha/data/mappings:ro`, `odisha_app_data:/opt/odisha/data`,
  airflow logs volume.

The image contains:

| Component | Location | Notes |
|---|---|---|
| OpenJDK 17 JRE | `JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64` | Spark 3.5 requires Java 8/11/17 |
| Postgres JDBC driver | `/opt/jdbc/postgresql-42.7.4.jar` | contract path, baked from Maven Central |
| PySpark 3.5.4 + psycopg2/requests/bs4/lxml/pandas/kafka-python-ng | Airflow env | `requirements.txt` |
| dbt CLI | `/opt/dbt/bin/dbt` (`dbt-postgres~=1.9`) | isolated venv; DAGs call it as a subprocess |
| `scrap.py`, `process.py` | `/opt/odisha/` | offline fallbacks; normally shadowed by the repo bind-mount |
| `data/mappings/*.json` | `/opt/airflow/mappings/` | fallback for the volume-shadowed `/opt/odisha/data/mappings` |
| DAGs / scripts | `/opt/airflow/dags`, `/opt/airflow/scripts` | dags are bind-mounted in compose; scripts come from the image (rebuild after editing scripts) |

## 3. DAGs

| dag id | schedule (IST) | task graph |
|---|---|---|
| `odisha_official_ingest` | `*/30 * * * *` | `scrape_official → load_official → dbt_run → dbt_test → publish_mart → publish_official_kafka` |
| `odisha_weather_ingest` | `5 * * * *` | `fetch_weather → publish_weather_kafka` |
| `odisha_dbt_daily` | `30 2 * * *` | `dbt_seed → dbt_run → dbt_test → dbt_docs_generate` |
| `odisha_spark_batch` | `0 3 * * *` | `spark_daily_batch` (pyspark `spark-submit`) |
| `odisha_platform_health` | `*/10 * * * *` | `collect_health → emit_heartbeats` |
| `odisha_bootstrap` | `None` (manual) | `ensure_kafka_topics → load_reference → scrape_official → load_official → dbt_seed → dbt_run → dbt_test → publish_mart → publish_official_kafka` |

All DAGs: `catchup=False`, `max_active_runs=1`, `retries=2` with a 2-minute delay,
owner `odisha-platform`, `doc_md` documentation, `start_date` in
`ZoneInfo("Asia/Kolkata")`. The six dag ids and schedules are part of the contract.

### Key semantics

* **Deterministic batch ids.** `odisha_common.batch_id_for_context` uses the DAG
  run's `data_interval_start` floored to the minute, UTC ISO-8601 with `Z`
  (e.g. `2026-11-19T12:00:00Z`), so retries reuse the same
  `raw.scrape_batches` row and `raw.official_outages` loads are idempotent
  (`ON CONFLICT (batch_id, row_hash) DO NOTHING`). Manual runs may override with
  `{"batch_id": "..."}` in `dag_run.conf`.
* **process.py parity.** `load_official.py` imports `parse_raw_row` / `row_hash`
  from the repository `process.py` when present (byte-identical parsing: circle
  vs division schema indices, `otype - cause` reason, sha1 over
  schema_type/circle/division/subdivision/section/gss/voltage/feeder/start/end/
  reason/areas) with a verbatim fallback copy.
* **Batch lifecycle.** scrape inserts `status='running'` before loading, load
  flips the row to `success` (or `failed`) with `finished_at`, `row_count` and a
  merged `detail` jsonb. An empty-but-valid scrape is a success with
  `row_count=0`, so `marts.map_dots` can legitimately be empty instead of stale.
* **dbt gating.** `dbt_test` runs before `publish_mart`; a failing data test
  prevents a broken mart from reaching the Go API/static fallback (the previous
  JSON stays in place).
* **Mart JSON.** `publish_mart.py` reads `marts.map_dots`, maps columns to the
  legacy Go `Dot` JSON shape (`start`/`end` keep the original feed strings from
  `start_text`/`end_text`; timestamptz-only columns are rendered back to
  `DD-MON-YYYY HH:MM` IST), aggregates `districts` by count desc, and atomically
  writes `$DATA_DIR/mart/outages.json` (`updatedAt`/`total`/`districts`/`dots`).
  Null coordinates fall back to a deterministic district-centroid jitter so the Go
  `float64` lat/lon never becomes 0/0.
* **Weather.** `fetch_weather.py` issues one multi-coordinate Open-Meteo request
  (`latitude=a,b,c…&longitude=…`, `forecast_days=2`,
  `timezone=Asia/Kolkata`, hourly `precipitation,temperature_2m,wind_speed_10m,
  relative_humidity_2m,weather_code`), converts IST hour strings to UTC
  timestamptz and upserts `(district, observed_at)`. Centroids come from
  `raw.district_centroids` (JSON file fallback). The next 24 h are published to
  Kafka `weather-observations`.
* **Spark.** The daily batch locates `spark-submit` at runtime via
  `os.path.dirname(pyspark.__file__)/bin/spark-submit`, always passes
  `--jars /opt/jdbc/postgresql-42.7.4.jar`, and calls
  `/opt/odisha/platform/spark/jobs/daily_batch.py --jdbc-url
  jdbc:postgresql://postgres:5432/odisha`. **Credentials are never on the command
  line** — the Spark job reads `POSTGRES_*`/`DATABASE_URL` from the inherited env.
  Defaults (overridable): `SPARK_MASTER=local[2]`,
  `SPARK_EXTRA_CONF=spark.ui.enabled=false;spark.sql.shuffle.partitions=8;spark.driver.memory=768m`
  (fits the 2 GB Airflow container).
* **dbt invocation.** `/opt/dbt/bin/dbt <cmd> --project-dir /opt/odisha/platform/dbt
  --profiles-dir /opt/odisha/platform/dbt --target-path /opt/odisha/data/dbt/target
  --log-path /opt/odisha/data/dbt/logs --no-use-colors`; dbt artefacts live on the
  shared volume so the repo mount never needs to be writable for dbt.
* **Health.** Probes Postgres, Kafka (topic existence), the Flink job
  (`GET /jobs/overview`, name `odisha-user-report-alerts`) and the
  `ops.pipeline_heartbeat('spark-stream')` freshness (600 s). Writes
  `ops.pipeline_heartbeat` rows for `postgres`/`kafka`/`flink`/`airflow` and a
  `spark-monitor` row (the `spark-stream` row itself is owned by the Spark job and
  is never overwritten). If the Flink job is missing it attempts an Airflow-side
  re-submission via `FLINK_SQL_GATEWAY_URL` (rendering
  `${KAFKA_BOOTSTRAP}`/`${POSTGRES_PASSWORD}`) or `FLINK_RESUBMIT_CMD`; in the
  compose stack the dedicated `flink-submit` sidecar already re-runs
  `platform/flink/submit.sh` every 180 s.

## 4. Standalone script usage

Every script runs with plain `python` (no Airflow needed) and supports `--help`:

```bash
python scripts/scrape_official.py --batch-id 2026-11-19T12:00:00Z
python scripts/load_official.py --batch-id 2026-11-19T12:00:00Z --publish-kafka
python scripts/load_reference.py --dry-run
python scripts/fetch_weather.py --no-kafka
python scripts/publish_mart.py --dry-run
python scripts/kafka_util.py --wait 60 --ensure-topics
python dags/odisha_common.py          # prints the resolved (non-secret) configuration
```

## 5. Environment variables

Read with sane defaults; no credentials in code.

| Variable | Default | Used by |
|---|---|---|
| `POSTGRES_HOST` / `PORT` / `DB` / `USER` / `PASSWORD` | `postgres` / `5432` / `odisha` / `odisha` / *(empty)* | all loaders, health, mart |
| `DATABASE_URL` | *(built from `POSTGRES_*`)* | fallback when `POSTGRES_HOST` is unset; also read by the Spark job |
| `KAFKA_BOOTSTRAP` | `kafka:9092` | Kafka producer/admin |
| `KAFKA_TOPIC_PARTITIONS` / `KAFKA_TOPIC_REPLICATION` / `KAFKA_RETENTION_MS` | `3` / `1` / `86400000` | topic creation |
| `REPO_DIR` | `/opt/odisha` | scrap.py/process.py, dbt project, spark jobs, flink SQL |
| `DATA_DIR` | `/opt/odisha/data` | mart JSON, dbt target/log, lake dir, scraped fallback |
| `MAPPINGS_DIR` | auto-detect (`$REPO_DIR/data/mappings`, `/opt/airflow/mappings`, …) | load_reference, weather fallback |
| `SCRAPED_DIR` | `$REPO_DIR/scrappedData` (writable fallback `$DATA_DIR/scrappedData`) | scrape/load |
| `DBT_BIN` / `DBT_PROJECT_DIR` / `DBT_PROFILES_DIR` | `/opt/dbt/bin/dbt` / `/opt/odisha/platform/dbt` / same | dbt tasks |
| `DBT_TARGET_PATH` / `DBT_LOG_PATH` | `$DATA_DIR/dbt/target` / `$DATA_DIR/dbt/logs` | dbt tasks |
| `JDBC_JAR` | `/opt/jdbc/postgresql-42.7.4.jar` | Spark batch |
| `SPARK_MASTER` / `SPARK_EXTRA_CONF` / `SPARK_SUBMIT_TIMEOUT` / `SPARK_JOBS_DIR` | `local[2]` / see above / `3600` / `$REPO_DIR/platform/spark/jobs` | Spark batch |
| `FLINK_REST_URL` / `FLINK_JOB_NAME` | `http://flink-jobmanager:8081` / `odisha-user-report-alerts` | health |
| `FLINK_SQL_GATEWAY_URL` / `FLINK_RESUBMIT_CMD` / `FLINK_SQL_FILE` | *(empty)* / *(empty)* / repo Flink SQL | health re-submission |
| `SPARK_HEARTBEAT_MAX_AGE_S` | `600` | health |
| `WEATHER_FORECAST_DAYS` / `WEATHER_KAFKA_HOURS` / `WEATHER_TIMEZONE` | `2` / `24` / `Asia/Kolkata` | weather |
| `TZ` | `Asia/Kolkata` | DAG timezone anchor (schedules use `ZoneInfo("Asia/Kolkata")` explicitly) |

## 6. First run (bootstrap)

Scheduled DAGs are unpaused at creation (`DAGS_ARE_PAUSED_AT_CREATION=False`) and
raw/tables/reference DDL is applied by the Postgres init scripts, so the
`*/30`, hourly, `*/10` and 03:00 DAGs start working as soon as the stack is up.
The dbt models additionally need the seeds + first reference load, therefore run
the manual bootstrap once after `docker compose up`:

```bash
docker compose -f platform/docker-compose.yml exec airflow \
  airflow dags unpause odisha_bootstrap
docker compose -f platform/docker-compose.yml exec airflow \
  airflow dags trigger odisha_bootstrap
```

That loads reference data, performs the first scrape/load, seeds and builds dbt,
publishes the first `mart/outages.json` and the first Kafka batch. Every step is
idempotent, so re-running it is safe. Watch it in the UI at
<http://127.0.0.1:8080> (admin / `$AIRFLOW_ADMIN_PASSWORD`).

## 7. Verification performed

* `python -m py_compile` on all 13 Python files — clean.
* Offline loader/mart/weather/helpers suite: 79 checks — parser/hash parity with
  the repository `process.py` (circle + division schemas), alias/circle/centroid
  normalisation parity, feed-string preservation, mart JSON shape, Open-Meteo
  IST→UTC parsing, batch-id determinism.
* Offline DAG-structure suite (fake Airflow): 131 checks — clean imports, exact dag
  ids/schedules, task ids and edges, retries/catchup/max_active_runs/tags/doc_md,
  dbt command strings, operator callables.
* Offline `kafka_util` suite against fakes mirroring kafka-python-ng 2.2.x:
  25 checks — topic creation (partitions/replication/retention), idempotency,
  duplicate-topic race, producer config/serializers, delivery-failure surfacing.
* Cross-deliverable integration suite: 88 checks — loader SQL vs
  `platform/postgres/init/02_raw.sql`, `marts.map_dots` columns vs
  `platform/dbt/models/marts/map_dots.sql`, Go `Dot` keys vs published JSON,
  Flink SQL rendering/splitting for the health re-submission path, Spark job CLI +
  `spark-submit` argv, compose/env/volume expectations, Dockerfile sources.
* Not executed here: `docker build` and the live end-to-end stack (lead deploys).

## 8. Residual risks / notes

* **DAG code via bind-mount, scripts via image.** compose mounts the dags directory
  so DAG edits hot-reload, but `platform/airflow/scripts/**` is baked into the
  image: rebuild (`docker compose build airflow`) after changing scripts.
* **dbt version drift.** `dbt-postgres~=1.9` resolves to the current 1.1x release
  (1.11.x at the time of writing). The project stays on classic `config-version: 2`
  features; pin `dbt-postgres~=1.9.0` in `requirements-dbt.txt` if a strict 1.9.x
  is ever required.
* **dbt test gates publication.** A persistent data-test failure freezes the mart
  JSON at the last good state (by design, surfaced as a red DAG run). If operationally
  undesirable, publish_mart can be moved to `trigger_rule="all_done"`.
* **Flink self-healing** is primarily the compose `flink-submit` sidecar; the
  Airflow-side re-submission only activates with `FLINK_SQL_GATEWAY_URL` or
  `FLINK_RESUBMIT_CMD` configured (Flink's JobManager REST API cannot submit SQL).
  Until the job appears, the health DAG reports `flink: degraded` (it does not fail
  the run).
* **Scrape volatility.** `kavach.tpodisha.com` is an ASP.NET feed; a markup change or
  outage yields "no tables" → the scrape task fails (retried twice, batch marked
  `failed`) instead of silently marking an empty batch successful. Downstream DAG
  runs keep using the last successful batch.
* **Empty table on successful scrape.** Legitimately empty outage tables produce a
  `success` batch with zero rows and an empty `map_dots` → the mart JSON is
  published with `total: 0` (not stale data).
* **Airflow container memory.** `mem_limit: 2000m` hosts an Airflow LocalExecutor
  plus the PySpark daily-batch JVM; the image defaults keep the Spark driver at
  768 MB, but a very large dbt/Spark day could pressure it (raise `mem_limit` or
  lower `SPARK_EXTRA_CONF` memory if needed).
* **Kafka producer semantics.** `kafka-python-ng` 2.2.x has no idempotent-producer
  config; delivery is at-least-once (`acks=all`, `retries=5`,
  `max_in_flight_requests_per_connection=1`). Consumers must tolerate duplicates
  (the contract's upserts do).
* **Mapping data shadowing.** Because `odisha_app_data` is mounted at
  `/opt/odisha/data`, the repo's `data/mappings` is invisible inside Airflow unless
  compose nests the bind mount (it currently does:
  `../data/mappings:/opt/odisha/data/mappings:ro`). The image also bakes
  `/opt/airflow/mappings` as a fallback; both are searched by the scripts.
* **Airflow metadata DB.** compose sets
  `AIRFLOW__DATABASE__SQL_ALCHEMY_CONN` from `${POSTGRES_PASSWORD}`; the
  `airflow` database is created by `platform/postgres/init/01_schemas.sql`. If the
  Postgres volume predates that script, create the database manually before
  starting Airflow.
