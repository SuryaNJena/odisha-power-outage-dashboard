#!/usr/bin/env python3
"""Publish ``marts.map_dots`` as the JSON datamart consumed by the Go API and the
static frontend.

The output file is ``$DATA_DIR/mart/outages.json`` (shared ``odisha_app_data``
volume mounted at ``/opt/odisha/data`` in Airflow and ``/app/data`` in the app)
with exactly the historical shape produced by ``process.py``::

    {
      "updatedAt": "2026-11-19T12:30:00.123456+00:00",
      "total": 186,
      "districts": [{"name": "Khordha", "value": 34}, ...],   # sorted by count desc
      "dots": [{
          "district": "Khordha", "circle": "BBSR-1", "division": "BED, BBSR",
          "gss": "PRADHAN SAHI", "voltage": "11KV", "feeder": "Hi-Tech OH",
          "start": "28-AUG-2026 09:30",   # original feed string (start_text)
          "end":   "28-AUG-2026 10:00",   # original feed string (end_text)
          "date": "2026-08-28", "reason": "...", "areas": "...",
          "lat": 20.09, "lon": 85.51, "geocoded": false
      }, ...]
    }

The Go ``Dot`` struct only knows the keys above, so they are emitted exactly (the
frontend parses ``DD-MON-YYYY HH:MM`` / ``DD-MM-YYYY HH:MM`` from ``start`` /
``end``, which is why the raw text must be preserved).  When ``map_dots`` only
exposes timestamptz columns (``start_ts`` / ``end_ts``) they are formatted back to
the IST feed representation.

Writes are atomic (tmp file + ``os.replace``) so the Go API never reads a partially
written document.

Standalone usage::

    python publish_mart.py
    python publish_mart.py --out /tmp/outages.json --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

_HERE = Path(__file__).resolve().parent
for _candidate in (_HERE, _HERE.parent / "dags"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import odisha_common as oc  # noqa: E402  (path bootstrap must run first)

#: Keys of the Go ``Dot`` struct / legacy datamart records, in historical order.
DOT_KEYS: tuple[str, ...] = (
    "district",
    "circle",
    "division",
    "gss",
    "voltage",
    "feeder",
    "start",
    "end",
    "date",
    "reason",
    "areas",
    "lat",
    "lon",
    "geocoded",
)

#: Candidate ``marts.map_dots`` columns for each logical value (first present wins).
COLUMN_CANDIDATES: dict[str, tuple[str, ...]] = {
    "district": ("district",),
    "circle": ("circle",),
    "division": ("division",),
    "gss": ("gss",),
    "voltage": ("voltage",),
    "feeder": ("feeder",),
    "start": ("start_text", "start_raw", "start", "start_ts", "start_time"),
    "end": ("end_text", "end_raw", "end", "end_ts", "end_time"),
    "outage_date": ("outage_date", "date", "metric_date"),
    "reason": ("reason",),
    "areas": ("areas",),
    "lat": ("lat", "latitude"),
    "lon": ("lon", "longitude"),
    "geocoded": ("geocoded", "is_geocoded"),
}

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_START_RE = re.compile(r"^(\d{1,2})[- ]([A-Za-z]{3})[- ](\d{4})")
_DMY_RE = re.compile(r"^(\d{1,2})-(\d{1,2})-(\d{4})")

DEFAULT_CENTROID = (20.5, 84.5)  # Odisha-ish fallback (same as process.centroid_for)


# ---------------------------------------------------------------------------
# Value conversion helpers
# ---------------------------------------------------------------------------

def feed_text(value: Any) -> str:
    """Return the original feed string for a start/end value.

    ``map_dots`` is expected to expose the untouched feed strings as
    ``start_text`` / ``end_text``.  If only a timestamptz is available it is
    rendered back into the feed representation (``DD-MON-YYYY HH:MM``, IST).
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        moment = value
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(oc.IST).strftime("%d-%b-%Y %H:%M").upper()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return ""
    # Tolerate ISO timestamps in text columns.
    if "T" in text and len(text) >= 16 and text[4] == "-":
        parsed = oc.parse_iso(text)
        if parsed is not None:
            return parsed.astimezone(oc.IST).strftime("%d-%b-%Y %H:%M").upper()
    return text


def derive_date(start_text: str, fallback: Any = None) -> str:
    """Derive ``YYYY-MM-DD`` from the feed start string (process.extract_date logic)."""
    if isinstance(fallback, datetime):
        return fallback.astimezone(oc.IST).date().isoformat()
    if isinstance(fallback, date):
        return fallback.isoformat()
    if fallback is not None:
        text = str(fallback).strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return text
    match = _START_RE.match((start_text or "").strip())
    if match:
        month = _MONTHS.get(match.group(2).upper())
        if month:
            return f"{int(match.group(3)):04d}-{month:02d}-{int(match.group(1)):02d}"
    match = _DMY_RE.match((start_text or "").strip())
    if match:
        return f"{int(match.group(3)):04d}-{int(match.group(2)):02d}-{int(match.group(1)):02d}"
    return ""


def as_text(value: Any) -> str:
    """Render a DB value as a JSON-safe non-null string."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return feed_text(value)
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def as_float(value: Any) -> float | None:
    """Render a DB value as a finite float or ``None``."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def as_bool(value: Any) -> bool:
    """Render a DB value as a JSON boolean."""
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes", "y"}
    return bool(value)


def _deterministic_jitter(seed: str) -> tuple[float, float]:
    """Stable ±0.05° jitter so centroid-fallback dots don't stack exactly."""
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()
    dx = int(digest[0:8], 16) / 0xFFFFFFFF
    dy = int(digest[8:16], 16) / 0xFFFFFFFF
    return (dx - 0.5) * 0.1, (dy - 0.5) * 0.1


def load_centroids(mappings_dir: str | Path | None = None) -> dict[str, tuple[float, float]]:
    """Best-effort district centroid lookup for coordinate fallbacks."""
    try:
        mappings = Path(mappings_dir) if mappings_dir else oc.mappings_dir()
        payload = oc.load_json(mappings / "district_centroids.json")
    except Exception as exc:  # noqa: BLE001 - centroids are only a fallback
        oc.log(f"WARNING: could not load district centroids ({exc!r}); using {DEFAULT_CENTROID}")
        return {}
    centroids: dict[str, tuple[float, float]] = {}
    for district, coords in payload.items():
        try:
            centroids[district] = (float(coords["lat"]), float(coords["lon"]))
        except (KeyError, TypeError, ValueError):
            continue
    return centroids


# ---------------------------------------------------------------------------
# Row -> dot conversion
# ---------------------------------------------------------------------------

def _pick(row: Mapping[str, Any], logical: str) -> Any:
    for column in COLUMN_CANDIDATES[logical]:
        if column in row:
            return row[column]
    return None


def row_to_dot(row: Mapping[str, Any], centroids: Mapping[str, tuple[float, float]]) -> dict[str, Any]:
    """Convert one ``marts.map_dots`` row into the legacy Go ``Dot`` JSON shape."""
    district = as_text(_pick(row, "district"))
    start = feed_text(_pick(row, "start"))
    end = feed_text(_pick(row, "end"))
    observed_date = derive_date(start, _pick(row, "outage_date"))

    lat = as_float(_pick(row, "lat"))
    lon = as_float(_pick(row, "lon"))
    geocoded = as_bool(_pick(row, "geocoded"))
    if lat is None or lon is None:
        # The Go Dot struct decodes missing coordinates as 0/0 (Gulf of Guinea), so
        # always emit a plausible district centroid instead of null.
        fallback_lat, fallback_lon = centroids.get(district, DEFAULT_CENTROID)
        offset_lat, offset_lon = _deterministic_jitter(f"{district}|{start}|{_pick(row, 'feeder')}")
        lat = round(fallback_lat + offset_lat, 5)
        lon = round(fallback_lon + offset_lon, 5)
        geocoded = False

    return {
        "district": district,
        "circle": as_text(_pick(row, "circle")),
        "division": as_text(_pick(row, "division")),
        "gss": as_text(_pick(row, "gss")),
        "voltage": as_text(_pick(row, "voltage")),
        "feeder": as_text(_pick(row, "feeder")),
        "start": start,
        "end": end,
        "date": observed_date,
        "reason": as_text(_pick(row, "reason")),
        "areas": as_text(_pick(row, "areas")),
        "lat": lat,
        "lon": lon,
        "geocoded": geocoded,
    }


def build_mart(dots: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Build the full datamart document (updatedAt/total/districts/dots)."""
    counts = Counter(dot["district"] for dot in dots if dot.get("district"))
    districts = [
        {"name": name, "value": count}
        for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    return {
        "updatedAt": oc.utc_now().isoformat(),
        "total": len(dots),
        "districts": districts,
        "dots": list(dots),
    }


# ---------------------------------------------------------------------------
# Database read
# ---------------------------------------------------------------------------

def fetch_map_dots() -> list[dict[str, Any]]:
    """Read every row of ``marts.map_dots`` as a list of dicts."""
    from psycopg2 import errors as pg_errors
    from psycopg2.extras import RealDictCursor

    try:
        with oc.db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM marts.map_dots")
                rows = [dict(row) for row in cur.fetchall()]
    except pg_errors.UndefinedTable as exc:
        raise RuntimeError(
            "marts.map_dots does not exist yet - run the dbt models first "
            "(odisha_bootstrap or odisha_dbt_daily)."
        ) from exc
    if rows and "district" not in rows[0]:
        raise RuntimeError(
            "marts.map_dots is missing the 'district' column: " + ", ".join(sorted(rows[0]))
        )
    return rows


def publish(mart_path: str | Path | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Read ``marts.map_dots`` and atomically write the datamart JSON."""
    target = Path(mart_path) if mart_path else oc.MART_DIR / "outages.json"
    centroids = load_centroids()
    rows = fetch_map_dots()
    dots = [row_to_dot(row, centroids) for row in rows]
    mart = build_mart(dots)
    summary: dict[str, Any] = {
        "mart_path": str(target),
        "total": mart["total"],
        "districts": len(mart["districts"]),
        "geocoded": sum(1 for dot in dots if dot["geocoded"]),
        "updated_at": mart["updatedAt"],
        "dry_run": dry_run,
    }
    if dry_run:
        oc.log(f"[dry-run] would write {summary}")
        return summary
    oc.write_json_atomic(target, mart)
    oc.log(
        f"Wrote {summary['mart_path']}: {summary['total']} dots across "
        f"{summary['districts']} districts ({summary['geocoded']} geocoded)"
    )
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish marts.map_dots to $DATA_DIR/mart/outages.json for the Go API/frontend.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--out", default=None, help="output path (default: $DATA_DIR/mart/outages.json)")
    parser.add_argument("--dry-run", action="store_true", help="read and summarise without writing the file")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = publish(mart_path=args.out, dry_run=args.dry_run)
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
