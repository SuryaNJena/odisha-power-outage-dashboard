#!/usr/bin/env python3
"""
process.py — Ingest scraped CSVs into SQLite, resolve each outage to a canonical
Odisha district, geocode it (free OSM Nominatim API, cached), and write the final
datamart:

  data/odisha.db            -> SQLite store (raw scraped rows + enriched outages)
  data/mart/outages.json    -> full enriched outage records + district aggregates
  public/outage_data.json   -> same full datamart snapshot (static Pages fallback)

District resolution is *generalized* instead of relying only on the circle name:
every record is matched against data/mappings/district_aliases.json (spelling
variants + town aliases) across its circle / division / subdivision / section
fields, longest match wins. So e.g. a row in the PARADEEP circle whose division
is "KED-I, KENDRAPARA" is correctly assigned to Kendrapara, and "Balasore" /
"Baleshwar" both resolve to the canonical map name Baleshwar.

Run after scrap.py. Dependencies: pip install -r requirements.txt
"""

import csv
import hashlib
import json
import os
import random
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

DB_PATH = Path("data/odisha.db")
ALIASES_PATH = Path("data/mappings/district_aliases.json")
GEOCODE_CACHE_PATH = Path("data/mappings/geocode_cache.json")
CENTROIDS_PATH = Path("data/mappings/district_centroids.json")
MART_PATH = Path("data/mart/outages.json")
OUTPUT_PATH = Path("public/outage_data.json")
NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "odisha-power-outage-dashboard/1.0 (self-hosted dashboard)"

# Old district spellings -> canonical names. Used to migrate the geocode cache
# keys so previously-geocoded substations keep their cached coordinates.
LEGACY_DISTRICT_RENAME = {
    "Balasore": "Baleshwar",
    "Bolangir": "Balangir",
    "Jagatsinghpur": "Jagatsinghapur",
    "Keonjhar": "Kendujhar",
    "Nabarangpur": "Nabarangapur",
    "Deogarh": "Debagarh",
    "Sonepur": "Subarnapur",
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS raw_outages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id      TEXT NOT NULL,          -- scrape batch (ISO timestamp)
    row_hash      TEXT NOT NULL,          -- content hash for dedupe
    source_file   TEXT,
    schema_type   TEXT,                   -- 'circle' | 'division'
    circle        TEXT,
    division      TEXT,
    subdivision   TEXT,
    section       TEXT,
    otype         TEXT,
    gss           TEXT,
    voltage       TEXT,
    feeder        TEXT,
    start         TEXT,
    end           TEXT,
    reason        TEXT,
    areas         TEXT,
    remarks       TEXT,
    raw_row       TEXT,                   -- full raw row as JSON
    ingested_at   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_batch_hash ON raw_outages(batch_id, row_hash);

CREATE TABLE IF NOT EXISTS outages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_id        INTEGER UNIQUE,         -- FK to raw_outages.id
    batch_id      TEXT,
    district      TEXT,                   -- canonical district name
    circle        TEXT,
    division      TEXT,
    gss           TEXT,
    voltage       TEXT,
    feeder        TEXT,
    start         TEXT,
    end           TEXT,
    date          TEXT,                   -- YYYY-MM-DD derived from start
    reason        TEXT,
    areas         TEXT,
    lat           REAL,
    lon           REAL,
    geocoded      INTEGER,
    processed_at  TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------

def connect_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


def row_hash(*fields) -> str:
    raw = "|".join((f or "").strip() for f in fields)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def read_meta(conn, key, default=""):
    cur = conn.execute("SELECT value FROM meta WHERE key = ?", (key,))
    row = cur.fetchone()
    return row[0] if row else default


def write_meta(conn, key, value):
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )

def ingest_csvs(data_dir: str, conn: sqlite3.Connection) -> int:
    """Read every CSV into the raw_outages table (deduped by content hash).

    Returns the number of *new* rows inserted this batch. Each run gets a fresh
    batch_id so the datamart is always built from the latest scrape while older
    batches stay in SQLite as raw-data history.
    """
    csv_dir = Path(data_dir)
    if not csv_dir.exists():
        print(f"Warning: {data_dir} not found")
        return 0

    batch_id = datetime.now(timezone.utc).isoformat(timespec="seconds")
    now = batch_id
    new_rows = 0

    for csv_file in sorted(csv_dir.glob("*.csv")):
        print(f"Ingesting {csv_file.name} …")
        try:
            with open(csv_file, newline="", encoding="utf-8-sig") as f:
                reader = csv.reader(f)
                header = next(reader, [])
                is_circle_schema = any(
                    "Name of Circle" in (h or "").strip() for h in header
                )

                for i, row in enumerate(reader, start=2):
                    if len(row) < 2:
                        continue

                    rec = parse_raw_row(row, is_circle_schema)
                    if rec is None:
                        continue

                    h = row_hash(
                        rec["schema_type"],
                        rec["circle"],
                        rec["division"],
                        rec["subdivision"],
                        rec["section"],
                        rec["gss"],
                        rec["voltage"],
                        rec["feeder"],
                        rec["start"],
                        rec["end"],
                        rec["reason"],
                        rec["areas"],
                    )
                    conn.execute(
                        """INSERT OR IGNORE INTO raw_outages
                           (batch_id, row_hash, source_file, schema_type, circle,
                            division, subdivision, section, otype, gss, voltage,
                            feeder, start, end, reason, areas, remarks, raw_row,
                            ingested_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            batch_id,
                            h,
                            csv_file.name,
                            rec["schema_type"],
                            rec["circle"],
                            rec["division"],
                            rec["subdivision"],
                            rec["section"],
                            rec["otype"],
                            rec["gss"],
                            rec["voltage"],
                            rec["feeder"],
                            rec["start"],
                            rec["end"],
                            rec["reason"],
                            rec["areas"],
                            rec["remarks"],
                            json.dumps(row, ensure_ascii=False),
                            now,
                        ),
                    )
                    new_rows += 1
        except Exception as e:
            print(f"  Error reading {csv_file.name}: {e}")

    conn.commit()
    if new_rows > 0:
        write_meta(conn, "last_batch_id", batch_id)
        conn.commit()
    return new_rows


def parse_raw_row(row: list, is_circle_schema: bool) -> dict | None:
    """Convert one raw CSV row into a normalized dict (returns None if unusable)."""
    if is_circle_schema:
        return {
            "schema_type": "circle",
            "circle": row[1].strip().upper(),
            "division": (row[2].strip() if len(row) > 2 else ""),
            "subdivision": "",
            "section": "",
            "otype": "",
            "gss": (row[3].strip() if len(row) > 3 else ""),
            "voltage": (row[4].strip() if len(row) > 4 else ""),
            "feeder": (row[5].strip() if len(row) > 5 else ""),
            "start": (row[6].strip() if len(row) > 6 else ""),
            "end": (row[7].strip() if len(row) > 7 else ""),
            "reason": (row[8].strip() if len(row) > 8 else ""),
            "areas": (row[9].strip() if len(row) > 9 else ""),
            "remarks": (row[10].strip() if len(row) > 10 else ""),
        }
    # Emergency shutdown schema:
    # Date, Division, Subdivision, Section, Type, Feeder, Cause, Areas, From, To, Remarks
    date = row[0].strip()
    start_t = row[8].strip() if len(row) > 8 else ""
    end_t = row[9].strip() if len(row) > 9 else ""
    otype = row[4].strip() if len(row) > 4 else ""
    cause = row[6].strip() if len(row) > 6 else ""
    reason = " - ".join(x for x in (otype, cause) if x)
    return {
        "schema_type": "division",
        "circle": "EMERGENCY",
        "division": row[1].strip(),
        "subdivision": (row[2].strip() if len(row) > 2 else ""),
        "section": (row[3].strip() if len(row) > 3 else ""),
        "otype": otype,
        "gss": (row[3].strip() if len(row) > 3 else ""),
        "voltage": "",
        "feeder": (row[5].strip() if len(row) > 5 else ""),
        "start": f"{date} {start_t}".strip(),
        "end": f"{date} {end_t}".strip(),
        "reason": reason,
        "areas": (row[7].strip() if len(row) > 7 else ""),
        "remarks": (row[10].strip() if len(row) > 10 else ""),
    }


def load_raw_rows(conn: sqlite3.Connection, batch_id: str | None) -> list[dict]:
    """Load raw records for processing, newest batch first."""
    if batch_id:
        rows = conn.execute(
            "SELECT * FROM raw_outages WHERE batch_id = ?", (batch_id,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM raw_outages").fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM raw_outages LIMIT 1").description]
    return [dict(zip(cols, r)) for r in rows]


# ---------------------------------------------------------------------------
# District resolution
# ---------------------------------------------------------------------------

def load_aliases(aliases_path: Path) -> dict:
    """Load the alias mapping and build alias -> canonical district lookups."""
    data = json.loads(aliases_path.read_text(encoding="utf-8"))
    alias_to_canon = {}
    for canon, variants in (data.get("districts") or {}).items():
        for v in variants:
            alias_to_canon.setdefault(_norm(v), canon)
    for town, canon in (data.get("towns") or {}).items():
        alias_to_canon.setdefault(_norm(town), canon)
    return {
        "alias_to_canon": alias_to_canon,
        "circle_exact": data.get("circleExact") or {},
    }


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def resolve_district(
    aliases: dict, circle: str, division: str, subdivision: str = "", section: str = ""
) -> str | None:
    """Resolve a record to a canonical district.

    Priority:
      1. division -> subdivision -> section  (finer-grained admin signals)
      2. circle name
    Each field is matched by *longest* alias substring so "KED-I, KENDRAPARA"
    inside a PARADEEP circle correctly wins over the generic circle mapping.
    Falls back to an exact circle lookup.
    """
    alias_to_canon = aliases["alias_to_canon"]
    # Longest alias first so e.g. 'jagatsinghpur' wins over shorter matches.
    ordered = sorted(alias_to_canon.items(), key=lambda kv: -len(kv[0]))

    for text in (division, subdivision, section, circle):
        t = _norm(text)
        if not t:
            continue
        for alias, canon in ordered:
            if alias and alias in t:
                return canon

    return aliases["circle_exact"].get((circle or "").strip().upper())


# ---------------------------------------------------------------------------
# Geocoding (unchanged behaviour, cached)
# ---------------------------------------------------------------------------

def clean_station(name: str) -> str:
    """Strip noise like (GSS), (PSS), (ODSSP) etc. for better geocoding."""
    cleaned = re.sub(r"\([^)]*\)", "", name or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def load_geocode_cache() -> dict:
    cache = {}
    if GEOCODE_CACHE_PATH.exists():
        cache = json.loads(GEOCODE_CACHE_PATH.read_text(encoding="utf-8"))
    return migrate_geocode_cache(cache)


def save_geocode_cache(cache: dict) -> None:
    GEOCODE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    GEOCODE_CACHE_PATH.write_text(
        json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def migrate_geocode_cache(cache: dict) -> dict:
    """Re-key cache entries that used pre-canonical district spellings so the
    existing geocoded coordinates stay usable after the district rename."""
    migrated = {}
    for key, value in cache.items():
        if "|" in key:
            station, district = key.rsplit("|", 1)
            district = LEGACY_DISTRICT_RENAME.get(district, district)
            key = f"{station}|{district}"
        migrated.setdefault(key, value)
    return migrated


def geocode(query: str) -> dict | None:
    """Query OSM Nominatim for a single location. Returns {lat, lon, display} or None."""
    try:
        resp = requests.get(
            NOMINATIM,
            params={"q": query, "format": "json", "limit": 1, "countrycodes": "in"},
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
        resp.raise_for_status()
        results = resp.json()
        if results:
            return {
                "lat": float(results[0]["lat"]),
                "lon": float(results[0]["lon"]),
                "display": results[0].get("display_name", ""),
            }
    except requests.exceptions.RequestException as e:
        print(f"    geocode error for {query!r}: {e}")
    return None


def locate(station: str, district: str, cache: dict) -> dict:
    """Return geocoded location for a station, using cache or live Nominatim."""
    key = f"{station}|{district}"
    if key in cache:
        return cache[key]

    query = f"{clean_station(station)}, {district}, Odisha, India"
    print(f"  Geocoding: {query}")
    result = geocode(query)
    time.sleep(1.1)  # Nominatim fair-use rate limit
    entry = result or {"lat": None, "lon": None, "display": ""}
    cache[key] = entry
    save_geocode_cache(cache)
    return entry


def centroid_for(district: str, centroids: dict) -> dict:
    c = centroids.get(district)
    if c:
        return {"lat": c["lat"], "lon": c["lon"]}
    return {"lat": 20.5, "lon": 84.5}  # Odisha-ish fallback


def disperse(lat, lon, geocoded: bool):
    """Jitter a point so dots don't stack exactly on top of each other."""
    if geocoded:
        radius = 0.02
    else:
        radius = 0.18  # centroid fallback: spread wider inside the district
    return lat + random.uniform(-radius, radius), lon + random.uniform(-radius, radius)


def extract_date(start: str) -> str:
    """Derive a YYYY-MM-DD date from '13-AUG-2026 10:00' or '12-08-2026 21:58'."""
    m = re.match(r"^(\d{1,2})[- ]([A-Za-z]{3})[- ](\d{4})", (start or "").strip())
    if m:
        months = {
            "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
            "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
        }
        mon = months.get(m.group(2).upper())
        if mon:
            return f"{int(m.group(3)):04d}-{mon:02d}-{int(m.group(1)):02d}"
    m = re.match(r"^(\d{1,2})-(\d{1,2})-(\d{4})", (start or "").strip())
    if m:
        return f"{int(m.group(3)):04d}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return ""


# ---------------------------------------------------------------------------
# Datamart build (SQLite -> JSON)
# ---------------------------------------------------------------------------

def write_outages_table(
    conn: sqlite3.Connection,
    raw_rows: list[dict],
    aliases: dict,
    centroids: dict,
    cache: dict,
) -> tuple[int, int]:
    """Resolve + geocode each raw row and store the enriched result in SQLite."""
    conn.execute("DELETE FROM outages")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    total = 0
    unknown = 0
    for raw in raw_rows:
        district = resolve_district(
            aliases,
            raw["circle"],
            raw["division"],
            raw["subdivision"],
            raw["section"],
        )
        if not district:
            print(f"  Unknown district: circle={raw['circle']!r} division={raw['division']!r}")
            unknown += 1
            continue

        loc = locate(raw["gss"], district, cache)
        if loc.get("lat") is not None:
            lat, lon = disperse(loc["lat"], loc["lon"], True)
            geocoded = True
        else:
            cen = centroid_for(district, centroids)
            lat, lon = disperse(cen["lat"], cen["lon"], False)
            geocoded = False

        conn.execute(
            """INSERT INTO outages
               (raw_id, batch_id, district, circle, division, gss, voltage, feeder,
                start, end, date, reason, areas, lat, lon, geocoded, processed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                raw["id"],
                raw["batch_id"],
                district,
                raw["circle"],
                raw["division"],
                raw["gss"],
                raw["voltage"],
                raw["feeder"],
                raw["start"],
                raw["end"],
                extract_date(raw["start"]),
                raw["reason"],
                raw["areas"],
                round(lat, 5),
                round(lon, 5),
                int(geocoded),
                now,
            ),
        )
        total += 1
    conn.commit()
    return total, unknown


def load_processed(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT district, circle, division, gss, voltage, feeder, start, end,
                  date, reason, areas, lat, lon, geocoded
           FROM outages"""
    ).fetchall()
    return [
        {
            "district": r[0],
            "circle": r[1],
            "division": r[2],
            "gss": r[3],
            "voltage": r[4],
            "feeder": r[5],
            "start": r[6],
            "end": r[7],
            "date": r[8],
            "reason": r[9],
            "areas": r[10],
            "lat": r[11],
            "lon": r[12],
            "geocoded": bool(r[13]),
        }
        for r in rows
    ]


def main():
    script_dir = Path(__file__).parent
    os.chdir(script_dir)

    conn = connect_db(DB_PATH)
    aliases = load_aliases(ALIASES_PATH)
    cache = load_geocode_cache()

    centroids = {}
    if CENTROIDS_PATH.exists():
        centroids = json.loads(CENTROIDS_PATH.read_text(encoding="utf-8"))

    # 1) Ingest fresh CSVs (if present) — keeps raw history in SQLite.
    new_rows = ingest_csvs("scrappedData", conn)
    if new_rows == 0:
        print("No new CSV rows — falling back to the latest SQLite batch.")

    # 2) Process the newest batch (or all rows if no batch was recorded).
    batch_id = read_meta(conn, "last_batch_id") or None
    raw_rows = load_raw_rows(conn, batch_id)
    print(f"\nProcessing {len(raw_rows)} raw outage records from SQLite …")

    total, unknown = write_outages_table(conn, raw_rows, aliases, centroids, cache)

    # 3) Export the datamart JSONs the frontend/backend consume.
    dots = load_processed(conn)
    counts: dict[str, int] = {}
    for d in dots:
        counts[d["district"]] = counts.get(d["district"], 0) + 1
    districts = [
        {"name": name, "value": count}
        for name, count in sorted(counts.items(), key=lambda x: -x[1])
    ]
    mart = {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "total": len(dots),
        "districts": districts,
        "dots": dots,
    }

    MART_PATH.parent.mkdir(parents=True, exist_ok=True)
    MART_PATH.write_text(json.dumps(mart, indent=2, ensure_ascii=False), encoding="utf-8")

    # Same full datamart in public/ so the static GitHub Pages build can render
    # the map + date filter without a backend (frontend falls back to DATA_URL).
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(mart, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Cache may have been migrated/re-keyed — persist it.
    save_geocode_cache(cache)
    conn.close()

    print(f"\nOK: {len(districts)} districts, {mart['total']} total outages "
          f"({unknown} unassigned)")
    print(f"  -> {DB_PATH}")
    print(f"  -> {MART_PATH}")
    print(f"  -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()

