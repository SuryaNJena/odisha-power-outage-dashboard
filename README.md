# Odisha Live Power Outage Dashboard

Live planned &amp; ongoing power outage map for Odisha — Next.js frontend, Go backend, Python scrape/process pipeline.

## Architecture

```
kavach.tpodisha.com  →  scrap.py        →  scrappedData/*.csv            (landing, gitignored)
                        process.py      →  data/odisha.db                (SQLite: raw + enriched)
                                          data/mart/outages.json         (datamart, OSM-geocoded)
                                          public/outage_data.json        (static snapshot for Pages)
                        backend/main.go →  :3001  serves UI + /api/*     (single port)
                        Next.js         →  out/  (static export, served by Go)
```

### SQLite raw-data store

`process.py` ingests every scraped row into `data/odisha.db` before processing:

- `raw_outages` — one row per raw outage, deduped by content hash and tagged with a
  per-run `batch_id`, so raw history accumulates across scrapes.
- `outages` — enriched rows (canonical district, lat/lon, `date`).
- `meta` — bookkeeping (latest batch id).

The datamart is always built from the **newest batch**; if `scrappedData/` is missing,
`process.py` falls back to the latest batch already in SQLite.

### Generalized district resolution

`process.py` no longer trusts only the circle name. It resolves each record against
`data/mappings/district_aliases.json` across its **division → subdivision → section →
circle** fields (longest alias substring wins), then falls back to an exact circle map:

- A row in the `PARADEEP` circle whose division is `KED-I, KENDRAPARA` → **Kendrapara**.
- `ANED, ANGUL` → **Angul**, `NYED, NAYAGARH` → **Nayagarh**, `PED, PURI` → **Puri**,
  `JED,JHARSUGUDA` → **Jharsuguda**, `DED,DEOGARH` → **Debagarh**, `SED,SONEPUR` → **Subarnapur**.
- Spelling variants are normalized to the map's official GeoJSON names:
  `Balasore`/`Baleswar` → **Baleshwar**, `Bolangir` → **Balangir**, `Keonjhar` → **Kendujhar**,
  `Jagatsinghpur` → **Jagatsinghapur**, `Nabarangpur` → **Nabarangapur** (aliases pulled from
  Wikipedia's district list + OSM + the feed itself).
- Town aliases (e.g. `Kuakhia` → Kendrapara, `Rairangpur` → Mayurbhanj) cover divisions
  named after towns rather than districts.

District names in the datamart therefore always match the GeoJSON district polygons, so the
choropleth fills and hover tooltips line up with the outage dots.

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

## Frontend date filter

A date-range control sits just above the map. It drives everything downstream:

- Outage dots on the map (re-projected live without restarting the pulse animation),
- the district choropleth heat values,
- the header stats and the details table.

Presets (`Today`, `Next 7 days`, `Next 30 days`, `All`) plus free-form From/To date
inputs are supported. Date parsing lives in `src/app/dateUtils.js` and handles both
feed formats (`13-AUG-2026 10:00` and `12-08-2026 21:58`); enriched dots also carry a
precomputed `date` (`YYYY-MM-DD`) field.

If no backend is reachable (e.g. the static GitHub Pages build), the frontend falls
back to `public/outage_data.json`, which `process.py` now writes as a full snapshot
(dots included), so the map + date filter work without a server too.

## Useful scripts

- `python scrap.py` — pull raw outage tables.
- `python process.py` — ingest raw CSVs into SQLite, resolve districts (alias
  matching), geocode (OSM Nominatim, cached), write `data/odisha.db` +
  `data/mart/outages.json` + `public/outage_data.json`. Falls back to the latest
  SQLite batch automatically when `scrappedData/` is empty.
- `node scripts/compute_centroids.mjs` — regenerate district centroid fallbacks.

