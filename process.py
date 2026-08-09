#!/usr/bin/env python3
"""
process.py — Reads scraped CSVs, enriches each outage with a lat/lon location
via the free OSM Nominatim geocoding API (cached in data/mappings/geocode_cache.json),
and writes the final datamart:

  data/mart/outages.json        -> full enriched outage records + district aggregates
  public/outage_data.json       -> lightweight aggregate snapshot (back-compat)

Run after scrap.py. Dependencies: pip install -r requirements.txt
"""

import csv
import json
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# Maps the "Name of Circle" CSV column to an Odisha district name.
CIRCLE_TO_DISTRICT = {
    "BBSR-1": "Khordha",
    "BBSR-2": "Khordha",
    "CUTTACK": "Cuttack",
    "PARADEEP": "Jagatsinghpur",
    "DHENKANAL": "Dhenkanal",
    "BHADRAK": "Bhadrak",
    "BARIPADA": "Mayurbhanj",
    "BALASORE": "Balasore",
    "BALESHWAR": "Balasore",
    "KEONJHAR": "Keonjhar",
    "KENDUJHAR": "Keonjhar",
    "JAJPUR": "Jajpur",
    "BARGARH": "Bargarh",
    "SAMBALPUR": "Sambalpur",
    "ROURKELA": "Sundargarh",
    "BOLANGIR": "Bolangir",
    "BALANGIR": "Bolangir",
    "KALAHANDI": "Kalahandi",
    "JEYPORE": "Koraput",
    "BERHAMPUR": "Ganjam",
    "BERHAMPUR CITY": "Ganjam",
    "ASKA": "Ganjam",
    "BHANJANAGAR": "Kandhamal",
    "RAYAGADA": "Rayagada",
    "JHARSUGUDA": "Jharsuguda",
    "KENDRAPARA": "Kendrapara",
    "NAYAGARH": "Nayagarh",
    "PURI": "Puri",
    "GANJAM": "Ganjam",
    "KORAPUT": "Koraput",
    "MALKANGIRI": "Malkangiri",
    "NABARANGPUR": "Nabarangpur",
    "NUAPADA": "Nuapada",
    "SONEPUR": "Subarnapur",
    "ANGUL": "Angul",
    "BAUDH": "Boudh",
    "GAJAPATI": "Gajapati",
    "SUBARNAPUR": "Subarnapur",
}

GEOCODE_CACHE_PATH = Path("data/mappings/geocode_cache.json")
CENTROIDS_PATH = Path("data/mappings/district_centroids.json")
MART_PATH = Path("data/mart/outages.json")
OUTPUT_PATH = Path("public/outage_data.json")
NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "odisha-power-outage-dashboard/1.0 (self-hosted dashboard)"


def clean_station(name: str) -> str:
    """Strip noise like (GSS), (PSS), (ODSSP) etc. for better geocoding."""
    cleaned = re.sub(r"\([^)]*\)", "", name or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def infer_district_from_division(division: str) -> str | None:
    """Infer district from a 'Division' column like 'ANED, ANGUL' or 'BCDD-II, BBSR'."""
    if not division:
        return None
    upper = division.upper()
    # Bhubaneswar-area codes
    if any(t in ("BBSR", "BHUBANESWAR", "BHUBNESWAR", "BCDD") for t in re.split(r"[,/\\-]", upper)):
        return "Khordha"
    # Try the tail after the comma, then substring match against known districts
    parts = [p.strip() for p in upper.split(",")]
    tail = parts[-1] if parts else ""
    for d in set(CIRCLE_TO_DISTRICT.values()):
        if tail == d.upper() or d.upper() in upper:
            return d
    return None


def load_csvs(data_dir: str) -> list[dict]:
    """Read every CSV into a list of raw outage records.

    The source site returns two table shapes:
      A) 'Name of Circle' schema (scheduled outages)
      B) 'Division' schema (emergency shutdowns) — different column layout
    """
    records = []
    csv_dir = Path(data_dir)
    if not csv_dir.exists():
        print(f"Warning: {data_dir} not found")
        return records

    for csv_file in sorted(csv_dir.glob("*.csv")):
        print(f"Processing {csv_file.name} …")
        try:
            with open(csv_file, newline="", encoding="utf-8-sig") as f:
                reader = csv.reader(f)
                header = next(reader, [])
                is_circle_schema = any("Name of Circle" in (h or "").strip() for h in header)

                for i, row in enumerate(reader, start=2):
                    if len(row) < 2:
                        continue

                    if is_circle_schema:
                        circle = row[1].strip().upper()
                        district = CIRCLE_TO_DISTRICT.get(circle)
                        if not district:
                            print(f"  Unknown circle: {row[1]!r} (row {i} of {csv_file.name})")
                            continue
                        records.append({
                            "district": district,
                            "circle": circle,
                            "division": (row[2].strip() if len(row) > 2 else ""),
                            "gss": (row[3].strip() if len(row) > 3 else ""),
                            "voltage": (row[4].strip() if len(row) > 4 else ""),
                            "feeder": (row[5].strip() if len(row) > 5 else ""),
                            "start": (row[6].strip() if len(row) > 6 else ""),
                            "end": (row[7].strip() if len(row) > 7 else ""),
                            "reason": (row[8].strip() if len(row) > 8 else ""),
                            "areas": (row[9].strip() if len(row) > 9 else ""),
                        })
                    else:
                        # Emergency shutdown schema:
                        # Date, Division, Subdivision, Section, Type, Feeder, Cause, Areas, From, To, Remarks
                        date = row[0].strip()
                        division = row[1].strip()
                        district = infer_district_from_division(division)
                        if not district:
                            print(f"  Unknown division: {row[1]!r} (row {i} of {csv_file.name})")
                            continue
                        start_t = row[8].strip() if len(row) > 8 else ""
                        end_t = row[9].strip() if len(row) > 9 else ""
                        otype = row[4].strip() if len(row) > 4 else ""
                        cause = row[6].strip() if len(row) > 6 else ""
                        reason = " - ".join(x for x in (otype, cause) if x)
                        records.append({
                            "district": district,
                            "circle": "EMERGENCY",
                            "division": division,
                            "gss": (row[3].strip() if len(row) > 3 else ""),
                            "voltage": "",
                            "feeder": (row[5].strip() if len(row) > 5 else ""),
                            "start": f"{date} {start_t}".strip(),
                            "end": f"{date} {end_t}".strip(),
                            "reason": reason,
                            "areas": (row[7].strip() if len(row) > 7 else ""),
                        })
        except Exception as e:
            print(f"  Error reading {csv_file.name}: {e}")
    return records



def load_geocode_cache() -> dict:
    if GEOCODE_CACHE_PATH.exists():
        return json.loads(GEOCODE_CACHE_PATH.read_text(encoding="utf-8"))
    return {}


def save_geocode_cache(cache: dict) -> None:
    GEOCODE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    GEOCODE_CACHE_PATH.write_text(
        json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8"
    )


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


def build_mart(records: list[dict], centroids: dict, cache: dict) -> dict:
    dots = []
    for r in records:
        loc = locate(r["gss"], r["district"], cache)
        if loc.get("lat") is not None:
            lat, lon = disperse(loc["lat"], loc["lon"], True)
            dots.append({**r, "lat": round(lat, 5), "lon": round(lon, 5), "geocoded": True})
        else:
            cen = centroid_for(r["district"], centroids)
            lat, lon = disperse(cen["lat"], cen["lon"], False)
            dots.append({**r, "lat": round(lat, 5), "lon": round(lon, 5), "geocoded": False})

    counts: dict[str, int] = {}
    for d in dots:
        counts[d["district"]] = counts.get(d["district"], 0) + 1

    districts = [
        {"name": name, "value": count}
        for name, count in sorted(counts.items(), key=lambda x: -x[1])
    ]

    return {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "total": len(dots),
        "districts": districts,
        "dots": dots,
    }


def main():
    script_dir = Path(__file__).parent
    os.chdir(script_dir)

    scraped_dir = Path("scrappedData")
    records = load_csvs(str(scraped_dir))

    cache = load_geocode_cache()
    centroids = {}
    if CENTROIDS_PATH.exists():
        centroids = json.loads(CENTROIDS_PATH.read_text(encoding="utf-8"))

    print(f"\nEnriching {len(records)} outage records with locations …")
    mart = build_mart(records, centroids, cache)

    MART_PATH.parent.mkdir(parents=True, exist_ok=True)
    MART_PATH.write_text(json.dumps(mart, indent=2, ensure_ascii=False), encoding="utf-8")

    aggregate = {
        "districts": mart["districts"],
        "total": mart["total"],
        "mostAffected": mart["districts"][0]["name"] if mart["districts"] else "",
        "updatedAt": mart["updatedAt"],
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(aggregate, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print(f"\nOK: Wrote {len(mart['districts'])} districts, {mart['total']} total outages")
    print(f"  -> {MART_PATH}")
    print(f"  -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()

