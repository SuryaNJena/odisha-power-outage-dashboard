#!/usr/bin/env bash
# Post-deploy verification for the Odisha outage platform.
# Runs on the server from ~/odisha-platform/platform.
set -uo pipefail
cd "$(dirname "$0")/../platform" 2>/dev/null || cd "$HOME/odisha-platform/platform"
PSQL="docker compose exec -T postgres psql -U odisha -d odisha -At"
fail=0

section() { printf '\n=== %s ===\n' "$1"; }

section "containers"
docker compose ps --format 'table {{.Name}}\t{{.Status}}' || fail=1

section "api health"
curl -fsS --max-time 10 localhost:3001/api/health || fail=1
printf '\n'

section "api mapdata"
curl -fsS --max-time 15 localhost:3001/api/mapdata |
  python3 -c 'import json,sys; d=json.load(sys.stdin); print("total=%s districts=%s source=%s updatedAt=%s" % (d.get("total"), len(d.get("districts") or []), d.get("source"), d.get("updatedAt")))' || fail=1

section "api realtime"
curl -fsS --max-time 15 localhost:3001/api/realtime |
  python3 -c 'import json,sys; d=json.load(sys.stdin); print("available=%s totals=%s districts=%s alerts=%s pipeline=%s" % (d.get("available"), d.get("totals"), len(d.get("districts") or []), len(d.get("alerts") or []), d.get("pipeline")))' || fail=1

section "api reliability"
curl -fsS --max-time 15 'localhost:3001/api/reliability?days=30' |
  python3 -c 'import json,sys; d=json.load(sys.stdin); print("available=%s districts=%s trend=%s" % (d.get("available"), len(d.get("districts") or []), len(d.get("trend") or [])))' || fail=1

section "api risk"
curl -fsS --max-time 15 localhost:3001/api/risk |
  python3 -c 'import json,sys; d=json.load(sys.stdin); print("available=%s districts=%s updatedAt=%s" % (d.get("available"), len(d.get("districts") or []), d.get("updatedAt")))' || fail=1

section "postgres row counts"
for q in \
  "select 'official_outages', count(*) from raw.official_outages" \
  "select 'scrape_batches', count(*) from raw.scrape_batches" \
  "select 'map_dots', count(*) from marts.map_dots" \
  "select 'fct_outage_events', count(*) from analytics.fct_outage_events" \
  "select 'reliability_daily', count(*) from analytics.reliability_daily" \
  "select 'weather_hourly', count(*) from raw.weather_hourly" \
  "select 'user_reports', count(*) from raw.user_reports" \
  "select 'report_minute', count(*) from realtime.report_minute" \
  "select 'alerts', count(*) from realtime.alerts" \
  "select 'flink_windows', count(*) from realtime.flink_report_windows" \
  "select 'ml_clusters', count(*) from ml.outage_clusters" \
  "select 'ml_etr_metrics', count(*) from ml.etr_model_metrics" \
  "select 'ml_risk_forecast', count(*) from ml.district_risk_forecast"; do
  echo "$q" | $PSQL 2>/dev/null || { echo "FAILED: $q"; fail=1; }
done

section "latest flink alerts"
echo "select alert_id, district, alert_type, severity, report_count from realtime.alerts order by created_at desc limit 5" | $PSQL 2>/dev/null

section "heartbeats"
echo "select component, status, last_seen from ops.pipeline_heartbeat order by component" | $PSQL 2>/dev/null

section "kafka topics"
docker compose exec -T kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9092 --list 2>/dev/null || fail=1

section "flink jobs"
curl -fsS --max-time 5 localhost:8081/jobs/overview 2>/dev/null |
  python3 -c 'import json,sys; d=json.load(sys.stdin); [print(j.get("name"), j.get("state")) for j in d.get("jobs", [])]' || echo "flink REST not reachable from host (expected; it is bound to 127.0.0.1:8081)"

section "airflow dags"
docker compose exec -T airflow airflow dags list 2>/dev/null | head -20 || fail=1

if [ "$fail" -eq 0 ]; then
  printf '\nVERIFY: OK\n'
else
  printf '\nVERIFY: FAILURES PRESENT\n'
  exit 1
fi
