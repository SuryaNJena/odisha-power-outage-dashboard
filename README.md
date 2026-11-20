# Odisha Live Power Outage Dashboard + Data Platform

The laptop deployment defaults to a snapshot map with a three-hour on-demand refresh limit and
PostgreSQL-backed reports. See [laptop modes and refresh commands](platform/README.md#laptop-mode-default).
The data platform described below is available in full mode.


Live planned & ongoing power outage map for Odisha, plus a near-real-time
crowd-report pipeline and a batch analytics/ML layer.

- **Production:** <https://odishablackouts.356189.xyz> (Cloudflare Tunnel →
  `127.0.0.1:3001` on the self-hosted Debian server)
- **App:** Next.js 15 static export + Go API on a single port (`3001`)
- **Data platform:** Airflow 2.10, dbt-postgres, PostgreSQL 16, Kafka 3.9 (KRaft),
  Flink 1.20 (SQL), Spark 3.5 (Structured Streaming + batch ML)
- **Operator UIs (localhost only):** Airflow `:8080`, Flink `:8081`

[Architecture diagrams, component switches, and legacy-code inventory](platform/ARCHITECTURE.md)
show both deployments. In laptop mode, dbt rebuilds the map mart on an eligible
button click; PostgreSQL stores user reports and coordinates refreshes.
Airflow, Kafka, Flink and Spark are optional.

## Repository layout

```
backend/                 Go API (mapdata, realtime, reliability, risk, report, health)
src/app/                 Next.js app: / (map), /live (NRT), /reliability (SAIDI/SAIFI)
platform/
  docker-compose.yml     laptop mode (app, postgres, private refresh service)
  docker-compose.full.yml optional NRT and analytics stack
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

On a **new** database, initialize the full platform once to install seeds and dbt views. Existing deployments already have these objects:

```bash
ssh ssh.356189.xyz
cd ~/odisha-platform
bash scripts/power-mode.sh full
cd platform
export COMPOSE_FILE=docker-compose.full.yml
docker compose exec airflow airflow dags unpause odisha_bootstrap
docker compose exec airflow airflow dags trigger odisha_bootstrap
docker compose ps
curl -s localhost:3001/api/health | jq
```

`odisha_bootstrap` creates Kafka topics, loads reference data, performs the first
scrape, runs `dbt seed/run/test`, publishes the map mart JSON and sends the first
batch to Kafka. It is idempotent.

### GitHub Pages (static frontend, live data from the laptop)

The public site is a static Next.js export on GitHub Pages. It has no server of
its own — every `/api/*` request goes to the laptop through the Cloudflare
tunnel, which answers with `Access-Control-Allow-Origin: *`.

- Site: <https://suryanjena.github.io/odisha-power-outage-dashboard/>
- API: `https://odishablackouts.356189.xyz` → tunnel → `localhost:3001` on the laptop

`deploy-pages.yml` builds the export on every push to `master`, and
`refresh-data.yml` calls it after committing a new fallback snapshot. Build-time
wiring:

| Variable | Value | Purpose |
|---|---|---|
| `NEXT_PUBLIC_USE_BASE_PATH` | `true` | enables the project basePath |
| `NEXT_PUBLIC_BASE_PATH` | `/<repo>` | prefixes assets, links and the manifest |
| `NEXT_PUBLIC_BACKEND_URL` | tunnel origin | absolute API origin baked into the bundle |

The tunnel hostname is read from the repository variable `ODISHA_API_BASE_URL`
(Settings → Secrets and variables → Actions → Variables) and falls back to the
value above, so rotating the tunnel needs no code change.

`trailingSlash` is enabled for the Pages build only, so deep links (`/live/`,
`/reliability/`) resolve on Pages; the self-hosted build keeps `live.html`.

One-time setup: **Settings → Pages → Source: GitHub Actions**. A branch-based
source makes `actions/deploy-pages` fail with `Get Pages site failed`.

When the laptop is offline the map falls back to the committed
`public/outage_data.json` snapshot, and the Live/Reliability pages show their
"not available yet" states. The tunnel is the only public ingress, so nothing
else on the laptop is exposed.

## Airflow DAGs (full mode, schedules opt-in)

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
| `GET /api/mapdata` | map dots + district heat for the latest published snapshot in laptop mode; PostgreSQL with file fallback in full mode |
| `GET /api/map-reports` | recent community outage counts and latest report timestamps at locality or district reference points |
| `GET /api/realtime` | NRT report totals, per-district reports/unconfirmed/risk, alerts, trend, pipeline health |
| `GET /api/reliability?days=7\|30\|90` | SAIDI / SAIFI / CAIDI / ASAI by district |
| `GET /api/risk` | weather-driven risk forecast (Spark ML first, dbt marts fallback) |
| `POST /api/report` | save to PostgreSQL `raw.user_reports`; full mode also attempts Kafka delivery |
| `GET /api/health` | snapshot readability in laptop mode; platform component health in full mode |
| `GET /api/features` | frontend capabilities: analytics, reporting, manual refresh |
| `GET /api/refresh` | laptop-mode refresh status and next eligible time; read only |
| `POST /api/refresh` | laptop: shared three-hour gate, then scrape → PostgreSQL → dbt → snapshot; full: trigger Airflow official ingest |

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
scheduled `refresh-data` workflow to keep the offline fallback current; the self-hosted stack uses PostgreSQL and dbt. Airflow is optional.

## Data refresh behaviour

Use **Refresh** on the map. Page loads and status checks read saved data. The
private helper starts an ingest only when the shared three-hour window allows
it. One successful run publishes a new map for everyone. A report is independent
of the official feed. See the [runtime diagrams](platform/ARCHITECTURE.md).

## Useful commands

```bash
cd ~/odisha-platform/platform
docker compose ps
docker compose -f docker-compose.full.yml logs -f airflow
docker compose -f docker-compose.full.yml logs -f spark-stream flink-jobmanager
docker compose exec postgres psql -U odisha -d odisha -c "select count(*) from raw.official_outages"
docker compose -f docker-compose.full.yml exec airflow /opt/dbt/bin/dbt --project-dir /opt/odisha/platform/dbt --profiles-dir /opt/odisha/platform/dbt test
```
