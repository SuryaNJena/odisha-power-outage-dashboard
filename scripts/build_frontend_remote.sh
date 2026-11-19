#!/usr/bin/env bash
# Build the Next.js static export on the server (sandbox-free), for iteration
# without a full compose deploy. Run from anywhere; ROOT defaults to
# ~/odisha-platform.
set -euo pipefail
ROOT="${ROOT:-$HOME/odisha-platform}"
cd "$ROOT"

echo "==> node:20-alpine build (same-origin API)"
docker volume create odisha_node_modules >/dev/null
docker volume create odisha_npm_cache >/dev/null
# out/ is produced by a root container; remove it from a container too.
if [ -e out ]; then
  docker run --rm -v "$ROOT:/src" alpine:3.20 rm -rf /src/out
fi
docker run --rm \
  -v "$ROOT:/src" \
  -v odisha_node_modules:/src/node_modules \
  -v odisha_npm_cache:/root/.npm \
  -w /src \
  -e NEXT_PUBLIC_BACKEND_URL= \
  -e NEXT_PUBLIC_USE_BASE_PATH= \
  node:20-alpine sh -lc 'npm ci --no-audit --no-fund && npm run build'

echo "==> validating export"
for page in index.html live.html reliability.html; do
  test -f "out/$page" || { echo "ERROR: out/$page missing" >&2; exit 1; }
done
if grep -rIl 'localhost:3001' out/_next >/dev/null 2>&1; then
  echo "ERROR: localhost:3001 baked into the export" >&2
  exit 1
fi
echo "OK: $(du -sh out | cut -f1), $(find out -name '*.html' | wc -l) html pages"
ls -la out/*.html
