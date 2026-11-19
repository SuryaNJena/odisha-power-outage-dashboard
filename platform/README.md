# Platform runbook

Everything here runs on the self-hosted Debian server under
`~/odisha-platform/platform` via `docker compose`. The public app is
<https://odishablackouts.356189.xyz> (Cloudflare Tunnel → `127.0.0.1:3001`).

## Services

| Container | Role | Published |
|---|---|---|
| `odisha-app` | Go API + Next.js static export | `127.0.0.1:3001` |
| `odisha-postgres` | warehouse (`odisha`) + Airflow metadata (`airflow`) | — |
| `odisha-kafka` | KRaft broker, 5 topics | — |
| `odisha-flink-jm` / `odisha-flink-tm` | Flink 1.20 session cluster | `127.0.0.1:8081` |
| `odisha-flink-submit` | watchdog: submits/repairs the Flink SQL job every 180 s | — |
| `odisha-airflow` | Airflow 2.10.5 standalone (scheduler + webserver + triggerer) | `127.0.0.1:8080` |
| `odisha-spark-stream` | Spark 3.5.4 Structured Streaming NRT sink | — |

## Data flow

1. `odisha_official_ingest` (`*/30`) scrapes Kavach → `raw.official_outages`,
   runs `dbt seed/run/test`, writes `data/mart/outages.json`, publishes
   `official-outages` to Kafka.
2. `odisha_weather_ingest` (`5 * * * *`) fetches Open-Meteo for 30 district
   centroids → `raw.weather_hourly` + `weather-observations`.
3. `POST /api/report` → Kafka `user-reports` + `raw.user_reports`.
4. Flink reads `user-reports`, 10-minute windows per district, emits
   `outage-alerts` (spike ≥3, high severity avg ≥4) and upserts
   `realtime.flink_report_windows`.
5. Spark streaming reads `user-reports`/`outage-alerts` → `realtime.report_minute`,
   `realtime.district_live_metrics`, `realtime.alerts`, heartbeat.
6. Spark daily batch (`0 3 * * *`) trains KMeans + ETR regression, writes
   `ml.*`, Parquet archive and `ops.job_runs`.
7. Go API serves marts and realtime tables to `/`, `/live`, `/reliability`.

## Health and verification

```bash
cd ~/odisha-platform/platform
docker compose ps
bash ~/odisha-platform/scripts/verify.sh        # if the repo scripts were uploaded
curl -s localhost:3001/api/health
curl -s localhost:3001/api/realtime | jq .pipeline
curl -s localhost:8081/jobs/overview | jq '.jobs[] | {name,state}'
docker compose logs -f airflow spark-stream flink-submit
```

## Known operational notes (from the first deployment)

- **dbt and the Airflow user site**: the Airflow image installs packages in
  `/home/airflow/.local`; when a task runs as root with `HOME=/home/airflow`,
  Python puts that directory on `sys.path` and its `typing_extensions` shadows
  the dbt venv's newer one. `odisha_common.dbt_command()` therefore prefixes
  dbt invocations with
  `PYTHONPATH=/opt/dbt/lib/python3.12/site-packages PYTHONNOUSERSITE=1`.
  Keep that if you edit the helper.
- **Flink SQL reserved words**: `comment` must be backticked in DDL.
- **Flink JDBC sink**: does not support `TIMESTAMP_LTZ`; the sink uses
  `TIMESTAMP(3)` with `options=-c timezone=UTC` in the JDBC URL.
- **Flink taskmanager memory**: `taskmanager.memory.process.size` is a *process*
  size; framework heap/off-heap, managed and network memory must fit inside the
  derived Flink memory. Values are tuned in `docker-compose.yml`.
- **Kafka topic config**: use `kafka-configs.sh` (not `kafka-topics.sh --alter
  --add-config`) to set `retention.ms`.
- **Spark watermark**: NRT windows use a 1-minute watermark so the live
  dashboard is not 10 minutes behind.
- **Spark checkpoints**: changing the streaming query requires clearing
  `odisha_spark_ckpt/stream-live` (replays from earliest offsets).
- **Root Airflow**: the container runs as `0:0` so the rw repo bind mount and
  named volumes are writable without host-side chowns. Fine for this demo host;
  use the airflow UID and pre-created writable dirs for a hardened deployment.

## Reset / recovery

```bash
# rebuild one service after editing its files
docker compose up -d --build app airflow
docker compose restart spark-stream flink-submit

# rebuild the analytics layer from raw data
docker compose exec airflow bash -lc \
  'PYTHONPATH= PYTHONNOUSERSITE=1 /opt/dbt/bin/dbt seed --project-dir /opt/odisha/platform/dbt --profiles-dir /opt/odisha/platform/dbt'
docker compose exec airflow bash -lc \
  'PYTHONPATH= PYTHONNOUSERSITE=1 /opt/dbt/bin/dbt run --project-dir /opt/odisha/platform/dbt --profiles-dir /opt/odisha/platform/dbt'

# WARNING: wipes the warehouse, Airflow metadata and checkpoints
docker compose down -v && docker compose up -d
```
