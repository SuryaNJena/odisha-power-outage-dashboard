#!/usr/bin/env bash
# Remote-side deploy for the Odisha outage platform.
# Runs on the server from ~/odisha-platform (uploaded by scripts/deploy.ps1).
#   1. builds the Next.js static export in a clean node:20-alpine container
#   2. validates the export (pages present, no localhost API baked in)
#   3. stages the app Docker build context (out/ + linux binary + data)
#   4. builds and starts the compose stack
set -euo pipefail

ROOT="${ROOT:-$HOME/odisha-platform}"
COMPOSE_FILE="$ROOT/platform/docker-compose.yml"
cd "$ROOT"
# Never let the dev-only .env.local leak into the production build.
rm -f .env.local

if [ ! -f platform/.env ]; then
  echo "ERROR: platform/.env is missing (POSTGRES_PASSWORD required)" >&2
  exit 1
fi
if [ ! -x platform/app/odisha-backend ]; then
  echo "ERROR: platform/app/odisha-backend missing — upload the binary first" >&2
  exit 1
fi

echo "==> [1/4] frontend build (node:20-alpine, same-origin API)"
docker volume create odisha_node_modules >/dev/null
docker volume create odisha_npm_cache >/dev/null
# out/ is produced by a root container, so remove it from a container too.
clean_dir() {
  if [ -e "$1" ]; then
    docker run --rm -v "$ROOT:/src" alpine:3.20 rm -rf "/src/$1"
  fi
}
clean_dir out
docker run --rm \
  -v "$ROOT:/src" \
  -v odisha_node_modules:/src/node_modules \
  -v odisha_npm_cache:/root/.npm \
  -w /src \
  -e NEXT_PUBLIC_BACKEND_URL= \
  -e NEXT_PUBLIC_USE_BASE_PATH= \
  node:20-alpine sh -lc 'npm ci --no-audit --no-fund && npm run build'

echo "==> [2/4] validating export"
for page in index.html live.html reliability.html; do
  if [ ! -f "out/$page" ]; then
    echo "ERROR: out/$page missing from the static export" >&2
    exit 1
  fi
done
if grep -rIl 'localhost:3001' out/_next >/dev/null 2>&1; then
  echo "ERROR: localhost:3001 was baked into the static export" >&2
  exit 1
fi
echo "    export ok: $(du -sh out | cut -f1), $(find out -name '*.html' | wc -l) html pages"

echo "==> [3/4] staging app build context"
rm -rf platform/app/out
cp -a out platform/app/out
mkdir -p platform/app/data/mart platform/app/data/mappings
cp -a data/mappings/. platform/app/data/mappings/
cp -a data/mart/outages.json platform/app/data/mart/outages.json 2>/dev/null || true

echo "==> [4/4] compose build + up"
docker compose -f "$COMPOSE_FILE" build app airflow
docker compose -f "$COMPOSE_FILE" up -d --remove-orphans
docker compose -f "$COMPOSE_FILE" ps

echo "==> waiting for the Airflow metadata DB and resetting the admin password"
for _ in $(seq 1 30); do
  if docker compose -f "$COMPOSE_FILE" exec -T airflow airflow users list >/dev/null 2>&1; then
    break
  fi
  sleep 5
done
docker compose -f "$COMPOSE_FILE" exec -T airflow \
  airflow users reset-password -u admin -p "${AIRFLOW_ADMIN_PASSWORD:-admin}" || true

cat <<'EOF'

Next step (first deploy only):
  cd ~/odisha-platform/platform
  docker compose exec airflow airflow dags trigger odisha_bootstrap
  # or POST /api/refresh once the app is up
EOF
