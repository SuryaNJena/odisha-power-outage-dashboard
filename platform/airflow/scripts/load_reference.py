#!/usr/bin/env python3
"""Load the historical JSON mappings into the raw reference tables.

Source files (``data/mappings/*.json`` in the repository)::

    district_aliases.json    -> raw.district_aliases  (alias, district)
    circle_to_district.json  -> raw.circle_exact      (circle, district)
    district_centroids.json  -> raw.district_centroids(district, lat, lon)
    geocode_cache.json       -> raw.station_geocodes  (station_norm, district, lat, lon, display)

Normalisations are identical to the dbt models (see platform/CONTRACT.md):

* alias        = ``regexp_replace(lower(alias), '[^a-z0-9]+', '', 'g')``
* circle       = ``upper(trim(circle))``
* station_norm = ``regexp_replace(lower(station), '[^a-z0-9]+', '', 'g')``

Alias precedence matches ``process.py``: canonical district spellings and their
variants first, then the town aliases (first writer wins).  All writes are
idempotent upserts, so the DAG can be re-run at any time and it also coexists
with ``platform/postgres/init/03_reference_seed.sql``.

Standalone usage::

    python load_reference.py
    python load_reference.py --mappings-dir /opt/airflow/mappings --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

_HERE = Path(__file__).resolve().parent
for _candidate in (_HERE, _HERE.parent / "dags"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import odisha_common as oc  # noqa: E402  (path bootstrap must run first)

# Old district spellings -> canonical names (must match process.py so the geocode
# cache keys are migrated exactly like the legacy pipeline did).
LEGACY_DISTRICT_RENAME = {
    "Balasore": "Baleshwar",
    "Bolangir": "Balangir",
    "Jagatsinghpur": "Jagatsinghapur",
    "Keonjhar": "Kendujhar",
    "Nabarangpur": "Nabarangapur",
    "Deogarh": "Debagarh",
    "Sonepur": "Subarnapur",
}

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize(text: Any) -> str:
    """dbt-compatible normalisation: lowercase, strip everything non-alphanumeric."""
    return _NON_ALNUM.sub("", str(text or "").lower())


# ---------------------------------------------------------------------------
# Builders (pure, unit-testable, no database access)
# ---------------------------------------------------------------------------

def build_aliases(aliases_payload: dict) -> dict[str, str]:
    """Build the ``alias -> canonical district`` map (process.py precedence)."""
    alias_to_canon: dict[str, str] = {}
    for canon, variants in (aliases_payload.get("districts") or {}).items():
        # The canonical spelling is itself a valid alias, then every variant.
        for value in [canon, *(variants or [])]:
            key = normalize(value)
            if key:
                alias_to_canon.setdefault(key, canon)
    for town, canon in (aliases_payload.get("towns") or {}).items():
        key = normalize(town)
        if key:
            alias_to_canon.setdefault(key, canon)
    return alias_to_canon


def build_circle_exact(circle_payload: dict, aliases_payload: dict | None = None) -> dict[str, str]:
    """Build the ``CIRCLE -> district`` exact lookup (upper-cased keys).

    ``circle_to_district.json`` is the primary source; ``district_aliases.json``
    carries a slightly larger ``circleExact`` block (process.py used that one), so
    its extra entries are merged in without overriding the dedicated file.
    """
    circles: dict[str, str] = {}
    for circle, district in (circle_payload or {}).items():
        key = str(circle or "").strip().upper()
        if key:
            circles[key] = district
    if aliases_payload:
        for circle, district in (aliases_payload.get("circleExact") or {}).items():
            key = str(circle or "").strip().upper()
            if key:
                circles.setdefault(key, district)
    return circles


def build_centroids(centroids_payload: dict) -> dict[str, tuple[float, float] | None]:
    """Build the ``district -> (lat, lon)`` map (``None`` when not numeric)."""
    centroids: dict[str, tuple[float, float] | None] = {}
    for district, coords in (centroids_payload or {}).items():
        try:
            lat = float(coords["lat"])
            lon = float(coords["lon"])
        except (KeyError, TypeError, ValueError):
            centroids[district] = None
        else:
            centroids[district] = (lat, lon)
    return centroids


def build_station_geocodes(geocode_payload: dict) -> dict[tuple[str, str], dict[str, Any]]:
    """Build ``(station_norm, district) -> {lat, lon, display}`` from the cache."""
    stations: dict[tuple[str, str], dict[str, Any]] = {}
    for key, value in (geocode_payload or {}).items():
        if "|" not in str(key):
            continue
        station, district = str(key).rsplit("|", 1)
        district = LEGACY_DISTRICT_RENAME.get(district.strip(), district.strip())
        station_norm = normalize(station)
        if not station_norm or not district:
            continue
        entry = value if isinstance(value, dict) else {}
        lat = entry.get("lat")
        lon = entry.get("lon")
        stations[(station_norm, district)] = {
            "lat": float(lat) if lat is not None else None,
            "lon": float(lon) if lon is not None else None,
            "display": str(entry.get("display") or ""),
        }
    return stations


# ---------------------------------------------------------------------------
# Database writes
# ---------------------------------------------------------------------------

ALIAS_UPSERT = """
INSERT INTO raw.district_aliases (alias, district)
VALUES (%s, %s)
ON CONFLICT (alias) DO UPDATE SET district = excluded.district
"""

CIRCLE_UPSERT = """
INSERT INTO raw.circle_exact (circle, district)
VALUES (%s, %s)
ON CONFLICT (circle) DO UPDATE SET district = excluded.district
"""

CENTROID_UPSERT = """
INSERT INTO raw.district_centroids (district, lat, lon)
VALUES (%s, %s, %s)
ON CONFLICT (district) DO UPDATE SET lat = excluded.lat, lon = excluded.lon
"""

STATION_UPSERT = """
INSERT INTO raw.station_geocodes (station_norm, district, lat, lon, display)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (station_norm, district) DO UPDATE
   SET lat = COALESCE(excluded.lat, raw.station_geocodes.lat),
       lon = COALESCE(excluded.lon, raw.station_geocodes.lon),
       display = COALESCE(NULLIF(excluded.display, ''), raw.station_geocodes.display)
"""


def _read_payload(mappings: Path, filename: str, dry_run: bool) -> dict:
    path = mappings / filename
    if not path.is_file():
        if dry_run:
            oc.log(f"[dry-run] {filename} missing; treating as empty")
            return {}
        oc.log(f"WARNING: {filename} missing in {mappings}; skipping")
        return {}
    payload = oc.load_json(path)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object, got {type(payload).__name__}")
    return payload


def load_reference(mappings_dir: str | Path | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Load all mapping JSONs into the raw reference tables."""
    mappings = Path(mappings_dir) if mappings_dir else oc.mappings_dir()
    oc.log(f"Reference mappings: {mappings}")

    aliases_payload = _read_payload(mappings, "district_aliases.json", dry_run)
    circle_payload = _read_payload(mappings, "circle_to_district.json", dry_run)
    centroids_payload = _read_payload(mappings, "district_centroids.json", dry_run)
    geocode_payload = _read_payload(mappings, "geocode_cache.json", dry_run)

    aliases = build_aliases(aliases_payload)
    circle_exact = build_circle_exact(circle_payload, aliases_payload)
    centroids = build_centroids(centroids_payload)
    stations = build_station_geocodes(geocode_payload)

    known_districts = {district for district in (aliases_payload.get("districts") or {})}
    unknown_station_districts = sorted(
        {district for (_station, district) in stations if known_districts and district not in known_districts}
    )
    summary: dict[str, Any] = {
        "mappings_dir": str(mappings),
        "aliases": len(aliases),
        "circle_exact": len(circle_exact),
        "centroids": len(centroids),
        "station_geocodes": len(stations),
        "geocoded_stations": sum(1 for value in stations.values() if value["lat"] is not None),
        "unknown_station_districts": unknown_station_districts,
        "dry_run": dry_run,
    }
    if dry_run:
        oc.log(f"[dry-run] {json.dumps(summary)}")
        return summary
    if not aliases:
        raise RuntimeError(f"No district aliases found in {mappings}/district_aliases.json")

    with oc.db_connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(ALIAS_UPSERT, sorted(aliases.items()))
            cur.executemany(CIRCLE_UPSERT, sorted(circle_exact.items()))
            cur.executemany(
                CENTROID_UPSERT,
                [(district, lat, lon) for district, coords in sorted(centroids.items()) for lat, lon in [coords or (None, None)]],
            )
            cur.executemany(
                STATION_UPSERT,
                [
                    (station_norm, district, value["lat"], value["lon"], value["display"])
                    for (station_norm, district), value in sorted(stations.items())
                ],
            )
    oc.log(
        "Reference data loaded: "
        f"{summary['aliases']} aliases, {summary['circle_exact']} circles, "
        f"{summary['centroids']} centroids, {summary['station_geocodes']} station geocodes "
        f"({summary['geocoded_stations']} with coordinates)"
    )
    if unknown_station_districts:
        oc.log(f"WARNING: geocode cache has non-canonical districts: {unknown_station_districts}")
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Load data/mappings/*.json into the raw reference tables.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--mappings-dir", default=None, help="folder with the mapping JSON files (auto-detected)")
    parser.add_argument("--dry-run", action="store_true", help="parse the mappings and print counts without writing")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = load_reference(mappings_dir=args.mappings_dir, dry_run=args.dry_run)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
