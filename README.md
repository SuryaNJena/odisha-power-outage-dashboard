# Odisha Live Power Outage Dashboard + Data Platform

Live planned & ongoing power outage map for Odisha, plus a near-real-time
crowd-report pipeline and a batch analytics/ML layer.

- **Production:** <https://odishablackouts.356189.xyz> (Cloudflare Tunnel →
  `127.0.0.1:3001` on the self-hosted Debian server)
- **App:** Next.js 15 static export + Go API on a single port (`3001`)
- **Data platform:** Airflow 2.10, dbt-postgres, PostgreSQL 16, Kafka 3.9 (KRaft),
  Flink 1.20 (SQL), Spark 3.5 (Structured Streaming + batch ML)
- **Operator UIs (localhost only):** Airflow `:8080`, Flink `:8081`

```
                     every 30 min                            hourly
kavach.tpodisha.com ──► Airflow odisha_official_ingest   Airflow odisha_weather_ingest ──► Open-Meteo
                              │                                  │
                              ▼                                  ▼
                        raw.official_outages              raw.weather_hourly
                              │                                  │
                              └────────────► dbt ◄───────────────┘
                                    staging → analytics → marts
                                              │
                                              ├──► Go API /api/mapdata, /api/reliability, /api/risk
                                              └──► mart/outages.json (shared volume + static fallback)

browser ──POST /api/report──► Go API ──► Kafka user-reports
                                             │
                       ┌─────────────────────┴─────────────────────┐
                       ▼                                           ▼
             Flink SQL: 10-min windows                  Spark Structured Streaming
             spike/high-severity alerts                 1-min windows, reconciliation
                       │                                           │
                       ├──► Kafka outage-alerts ───────────────────┤
                       └──► realtime.flink_report_windows           └──► realtime.district_live_metrics
                                                                        realtime.report_minute
                                                                        realtime.alerts
                                                                        ops.pipeline_heartbeat
```

## Repository layout

```
backend/                 Go API (mapdata, realtime, reliability, risk, report, health)
src/app/                 Next.js app: / (map), /live (NRT), /reliability (SAIDI/SAIFI)
platform/
  docker-compose.yml     full stack (app, postgres, kafka, flink, airflow, spark-stream)
  CONTRACT.md            interface contract used to build the platform
  FEATURES.md            feature map + inspiration from foreign outage services
  postgres/init/         schemas, raw/realtime/ml DDL, reference-data seed
  airflow/               image, DAGs, scrape/weather/dbt/publish/Kafka scripts
  dbt/                   dbt-postgres project: staging, analytics, marts, tests, seed
  flink/                 image with SQL connector jars, SQL job, submit/watchdog
  spark/                 Structured Streaming sink + daily batch ML jobs
scripts/deploy.ps1       one-command deploy from Windows
scripts/deploy_remote.sh server-side build/stage/compose-up
```

## Deploy

Prereqs: SSH alias `ssh.356189.xyz` (see `~/.ssh/config`), Docker on the server,
Go + Node/`tar` locally.

```powershell
powershell -File scripts\deploy.ps1
```

The script builds the linux Go binary, packs the repo, uploads it, builds the
Next.js export inside a `node:20-alpine` container on the server, stages the app
image and runs `docker compose up -d --build`. First run takes 10–20 minutes
(base images + Airflow image with Java/PySpark/dbt).

Then bootstrap the data platform once:

```bash
ssh ssh.356189.xyz
cd ~/odisha-platform/platform
docker compose exec airflow airflow dags unpause odisha_bootstrap
docker compose exec airflow airflow dags trigger odisha_bootstrap
docker compose ps
curl -s localhost:3001/api/health | jq
```

`odisha_bootstrap` creates Kafka topics, loads reference data, performs the first
scrape, runs `dbt seed/run/test`, publishes the map mart JSON and sends the first
batch to Kafka. It is idempotent.

## Airflow DAGs

| DAG | Schedule (IST) | Purpose |
|---|---|---|
| `odisha_official_ingest` | `*/30 * * * *` | scrape → raw → dbt run/test → publish mart → Kafka |
| `odisha_weather_ingest` | `5 * * * *` | Open-Meteo hourly forecast → `raw.weather_hourly` + Kafka |
| `odisha_dbt_daily` | `30 2 * * *` | full dbt run + tests + docs |
| `odisha_spark_batch` | `0 3 * * *` | Spark ML batch (clusters, ETR model, risk forecast, Parquet lake) |
| `odisha_platform_health` | `*/10 * * * *` | heartbeats, Flink job presence, stale-pipeline detection |
| `odisha_bootstrap` | manual | first-run bootstrap |

## API

| Endpoint | Description |
|---|---|
| `GET /api/mapdata` | map dots + district heat for the latest successful batch (Postgres, snapshot fallback) |
| `GET /api/realtime` | NRT report totals, per-district reports/unconfirmed/risk, alerts, trend, pipeline health |
| `GET /api/reliability?days=7\|30\|90` | SAIDI / SAIFI / CAIDI / ASAI by district |
| `GET /api/risk` | weather-driven risk forecast (Spark ML first, dbt marts fallback) |
| `POST /api/report` | publish a crowd outage report (Kafka + `raw.user_reports`) |
| `GET /api/health` | postgres / kafka / flink / spark component health |
| `POST /api/refresh` | trigger the official-ingest DAG through the Airflow REST API |

## Operator access (no public exposure)

```bash
ssh -N -L 8080:127.0.0.1:8080 -L 8081:127.0.0.1:8081 ssh.356189.xyz
# Airflow: http://localhost:8080  (admin / value of AIRFLOW_ADMIN_PASSWORD, default admin)
# Flink:   http://localhost:8081
```

The deploy script resets the Airflow admin password to `AIRFLOW_ADMIN_PASSWORD`
(default `admin` in `platform/.env`), which is also what the Go API uses for
`POST /api/refresh`.

Kafka, Postgres and Spark are only reachable on the compose network.

## Dashboard pages

- `/` — outage map with dots + district heat, date/time knob, filters, report form.
- `/live` — NRT dashboard: reports in the last 15 min, districts reporting, active
  alerts, official outage count, pipeline status chips, per-minute trend, alert
  feed, quick report form.
- `/reliability` — 7/30/90-day SAIDI/SAIFI/CAIDI/ASAI per district, trend and
  ranking (IEEE 1366 style reliability indices).

## Local development

Prereqs: Node.js, Go, Python 3.11+.

```powershell
pip install -r requirements.txt
python scrap.py            # fetch outage tables (CSVs)
python process.py          # SQLite + geocoded JSON mart
cd backend; go run .       # API + static export on :3001
npm run dev                # Next dev server on :3000 (hot reload)
```

The legacy pipeline (`scrap.py` → `process.py` → SQLite/JSON) is still used by the
GitHub Pages workflow and as the offline fallback; the live self-hosted stack uses
Airflow + dbt + Postgres instead.

## Data refresh behaviour

- Airflow scrapes every 30 min and dbt rebuilds the marts in the same DAG run.
- If `data/odisha.db` / `scrappedData/` are missing, the Go server serves the
  committed `data/mart/outages.json` snapshot (the app never shows a blank map).
- Weather forecasts are refreshed hourly; the risk model is refreshed nightly by
  Spark.

## Useful commands

```bash
cd ~/odisha-platform/platform
docker compose ps
docker compose logs -f airflow
docker compose logs -f spark-stream flink-jobmanager
docker compose exec postgres psql -U odisha -d odisha -c "select count(*) from raw.official_outages"
docker compose exec airflow /opt/dbt/bin/dbt --project-dir /opt/odisha/platform/dbt --profiles-dir /opt/odisha/platform/dbt test
```
