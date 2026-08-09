# Odisha Live Power Outage Dashboard

Live planned &amp; ongoing power outage map for Odisha — Next.js frontend, Go backend, Python scrape/process pipeline.

## Architecture

```
kavach.tpodisha.com  →  scrap.py        →  scrappedData/*.csv        (landing)
                        process.py      →  data/mart/outages.json    (datamart, OSM-geocoded)
                        backend/main.go →  :3001  serves UI + /api/*  (single port)
                        Next.js         →  out/  (static export, served by Go)
```

## Where to set ports

- **Backend / single deployment port:** `backend/main.go`
  - Read from the `PORT` environment variable, default `3001`.
  - The Go server serves **both** the UI (from `out/`) and the API, so this is the only port you need to expose.
- **Frontend dev server port:** `package.json` → `"dev": "next dev -p 3000"` (dev only — hot reload).
- **Base URLs:** `src/app/config.js`
  - `NEXT_PUBLIC_BACKEND_URL` — where the frontend calls the API. Defaults to **same-origin** (`/api/mapdata`) which is what the one-tunnel deployment uses. `.env.local` sets it to `http://localhost:3001` for local dev.
  - `NEXT_PUBLIC_BASE_PATH` / `NEXT_PUBLIC_USE_BASE_PATH` — only for subpath hosting (e.g. GitHub Pages), not needed for a normal tunnel/domain.

## Local development

Prereqs: Node.js, Go, Python 3.11+.

```powershell
pip install -r requirements.txt        # once
python scrap.py                        # fetch outage tables (CSVs)
python process.py                      # build geocoded datamart
```

Terminal 1 — backend API (port 3001):
```powershell
cd backend
go run .
```

Terminal 2 — frontend dev server (port 3000, hot reload):
```powershell
npm run dev
```
The `.env.local` file points the dev frontend at `http://localhost:3001`.

Open http://localhost:3000

## One tunnel for everything (Cloudflare Tunnel)

The Go backend serves the exported frontend + API on a single port, so one
tunnel exposes the whole app.

```powershell
# Build the static frontend for same-origin API access
$env:NEXT_PUBLIC_BACKEND_URL=""
npm run build

# Start the Go server (serves out/ + /api/*)
cd backend
go run .
```

Then point your tunnel at:
```
http://localhost:3001
```

Visitors get both the UI and the API from the same origin — no per-page base
URL config needed.

If you later split the API onto its own subdomain, build with:
```powershell
$env:NEXT_PUBLIC_BACKEND_URL="https://api.yourdomain.com"
npm run build
```

## Data refresh

- Backend refreshes on a 6-hour schedule, or automatically when data is older
  than 1 hour (it re-runs `scrap.py` + `process.py`).
- Force a refresh: `POST /api/refresh`.
- Report wrong data: the dashboard's report button → `POST /api/report`
  (stored in `data/reports.json`).

## Useful scripts

- `python scrap.py` — pull raw outage tables.
- `python process.py` — geocode (OSM Nominatim, cached) + build `data/mart/outages.json`.
- `node scripts/compute_centroids.mjs` — regenerate district centroid fallbacks.

