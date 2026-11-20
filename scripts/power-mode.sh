#!/usr/bin/env bash
# Select a runtime for the Odisha project. No volume removal or Docker pruning.
set -euo pipefail
cd "$(dirname "$0")/../platform"
exec 9>"${TMPDIR:-/tmp}/odisha-power-mode.lock"
flock -n 9 || { echo "Another mode change or refresh is in progress" >&2; exit 1; }
lite=(docker compose -f docker-compose.yml)
full=(docker compose -f docker-compose.full.yml)
case "${1:-}" in
  snapshot)
    # During migration, capture exactly what the current API is serving. The
    # old Airflow publisher can leave its fallback JSON readable only by root.
    if docker inspect --format '{{.State.Running}}' odisha-app 2>/dev/null | grep -qx true &&
       ! curl -fsS localhost:3001/api/health | grep -q '"mode":"snapshot"'; then
      tmp=$(mktemp)
      trap 'rm -f "$tmp"' EXIT
      curl -fsS --max-time 30 localhost:3001/api/mapdata > "$tmp"
      python3 -c 'import json, sys; d=json.load(open(sys.argv[1])); assert d["updatedAt"] and len(d["dots"]) == d["total"] and isinstance(d["districts"],list)' "$tmp"
      docker exec -i -u 0 odisha-app sh -ec 'cd /opt/odisha-data/mart; cat > .mode-snapshot; chmod 644 .mode-snapshot; mv .mode-snapshot outages.json' < "$tmp"
    fi
    "${lite[@]}" up -d app
    curl --retry 8 --retry-connrefused --retry-delay 1 -fsS localhost:3001/api/health | grep -q '"mode":"snapshot"'
    curl -fsS localhost:3001/api/mapdata | python3 -c 'import json,sys; assert json.load(sys.stdin)["source"] == "snapshot"'
    "${full[@]}" stop spark-stream flink-submit airflow flink-taskmanager flink-jobmanager kafka-init kafka
    ;;
  full) "${lite[@]}" stop refresh; "${full[@]}" up -d ;;
  kafka-on) "${full[@]}" up -d kafka kafka-init ;;
  kafka-off)
    curl -fsS localhost:3001/api/health | grep -q '"mode":"snapshot"' || { echo "Select snapshot mode first" >&2; exit 1; }
    "${full[@]}" stop kafka-init kafka ;;
  refresh)
    curl -fsS localhost:3001/api/health | grep -q '"mode":"snapshot"' || { echo "Use the full platform refresh endpoint in full mode" >&2; exit 1; }
    curl -fsS -X POST localhost:3001/api/refresh ;;
  *) echo "Usage: bash scripts/power-mode.sh snapshot|refresh|kafka-on|kafka-off|full" >&2; exit 2 ;;
esac
