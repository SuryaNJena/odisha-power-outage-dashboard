#!/usr/bin/env python3
"""Fetch hourly weather forecasts for every Odisha district from Open-Meteo.

* Reads the 30 district centroids from ``raw.district_centroids`` (falling back to
  ``data/mappings/district_centroids.json`` when the table is empty).
* Issues **one** multi-coordinate Open-Meteo request (comma-separated
  ``latitude``/``longitude``) for the hourly variables
  ``precipitation, temperature_2m, wind_speed_10m, relative_humidity_2m,
  weather_code`` with ``forecast_days=2`` and ``timezone=Asia/Kolkata``.
* Upserts ``raw.weather_hourly`` on ``(district, observed_at)`` with UTC
  timestamptz values.
* Publishes the upcoming hours to the ``weather-observations`` Kafka topic (one
  JSON document per district-hour).

Split into two DAG tasks: :func:`fetch_and_load` (Open-Meteo -> raw) and
:func:`publish_recent_weather` (raw -> Kafka), each independently retryable.

Standalone usage::

    python fetch_weather.py                     # fetch + upsert + publish
    python fetch_weather.py --no-kafka          # fetch + upsert only
    python fetch_weather.py --dry-run           # fetch + print a sample, no writes
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

_HERE = Path(__file__).resolve().parent
for _candidate in (_HERE, _HERE.parent / "dags"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import odisha_common as oc  # noqa: E402  (path bootstrap must run first)

HOURLY_VARIABLES = (
    "precipitation",
    "temperature_2m",
    "wind_speed_10m",
    "relative_humidity_2m",
    "weather_code",
)

WEATHER_UPSERT = """
INSERT INTO raw.weather_hourly
    (district, observed_at, temperature_c, precipitation_mm, wind_kph,
     humidity_pct, weather_code, source)
VALUES %s
ON CONFLICT (district, observed_at) DO UPDATE
   SET temperature_c = excluded.temperature_c,
       precipitation_mm = excluded.precipitation_mm,
       wind_kph = excluded.wind_kph,
       humidity_pct = excluded.humidity_pct,
       weather_code = excluded.weather_code,
       source = excluded.source,
       fetched_at = now()
"""


# ---------------------------------------------------------------------------
# District centroids
# ---------------------------------------------------------------------------

def district_centroids(mappings_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """Return ``[{district, lat, lon}]`` from Postgres, falling back to the JSON file."""
    try:
        rows = oc.execute_fetch_all(
            "SELECT district, lat, lon FROM raw.district_centroids "
            "WHERE lat IS NOT NULL AND lon IS NOT NULL ORDER BY district"
        )
        centroids = [
            {"district": row["district"], "lat": float(row["lat"]), "lon": float(row["lon"])}
            for row in rows
        ]
        if centroids:
            return centroids
        oc.log("raw.district_centroids is empty; falling back to the mapping JSON file")
    except Exception as exc:  # noqa: BLE001 - mapping-file fallback below
        oc.log(f"Could not read raw.district_centroids ({exc!r}); using the mapping JSON file")

    mappings = Path(mappings_dir) if mappings_dir else oc.mappings_dir()
    payload = oc.load_json(mappings / "district_centroids.json")
    centroids = []
    for district, coords in payload.items():
        try:
            centroids.append({"district": district, "lat": float(coords["lat"]), "lon": float(coords["lon"])})
        except (KeyError, TypeError, ValueError):
            oc.log(f"Skipping centroid without coordinates: {district}")
    if not centroids:
        raise RuntimeError(f"No usable district centroids in {mappings / 'district_centroids.json'}")
    return sorted(centroids, key=lambda item: item["district"])


# ---------------------------------------------------------------------------
# Open-Meteo request
# ---------------------------------------------------------------------------

def _request_forecast(coords: Sequence[dict[str, Any]], forecast_days: int, attempts: int = 4) -> list[dict]:
    """Call Open-Meteo once for a list of coordinates and return the per-location payloads."""
    import requests

    params = {
        "latitude": ",".join(f"{coord['lat']:.5f}" for coord in coords),
        "longitude": ",".join(f"{coord['lon']:.5f}" for coord in coords),
        "hourly": ",".join(HOURLY_VARIABLES),
        "forecast_days": str(forecast_days),
        "timezone": oc.WEATHER_TIMEZONE,
    }
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(oc.OPEN_METEO_URL, params=params, timeout=oc.WEATHER_REQUEST_TIMEOUT)
            if response.status_code in {429, 500, 502, 503, 504}:
                raise RuntimeError(f"Open-Meteo transient HTTP {response.status_code}")
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict):
                # A single coordinate returns an object instead of a list.
                payload = [payload]
            if not isinstance(payload, list):
                raise RuntimeError(f"Unexpected Open-Meteo payload type: {type(payload).__name__}")
            return payload
        except Exception as exc:  # noqa: BLE001 - retry with backoff
            last_error = exc
            oc.log(f"Open-Meteo request attempt {attempt}/{attempts} failed: {exc!r}")
            if attempt < attempts:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Open-Meteo request failed after {attempts} attempts: {last_error!r}")


def fetch_forecast(coords: Sequence[dict[str, Any]], forecast_days: int | None = None) -> list[dict]:
    """Fetch the forecast for all coordinates, chunking if the API returns a mismatch."""
    forecast_days = forecast_days or oc.WEATHER_FORECAST_DAYS
    if not coords:
        return []
    chunk_size = max(1, oc.WEATHER_MAX_COORDS_PER_REQUEST)
    payloads: list[dict] = []
    for start in range(0, len(coords), chunk_size):
        chunk = list(coords[start : start + chunk_size])
        chunk_payload = _request_forecast(chunk, forecast_days)
        if len(chunk_payload) != len(chunk):
            raise RuntimeError(
                f"Open-Meteo returned {len(chunk_payload)} locations for {len(chunk)} coordinates"
            )
        payloads.extend(chunk_payload)
    oc.log(
        f"Open-Meteo: {len(payloads)} locations, {forecast_days} forecast days "
        f"(timezone {oc.WEATHER_TIMEZONE})"
    )
    return payloads


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _to_utc(value: str) -> datetime | None:
    """Parse an Open-Meteo local timestamp (``YYYY-MM-DDTHH:MM``, IST) to UTC."""
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=oc.IST)
    return moment.astimezone(timezone.utc)


def _series(hourly: dict, name: str, index: int) -> Any:
    values = hourly.get(name) or []
    if index >= len(values):
        return None
    return values[index]


def parse_hourly_rows(
    coords: Sequence[dict[str, Any]], payloads: Sequence[dict]
) -> list[dict[str, Any]]:
    """Flatten Open-Meteo per-location payloads into ``raw.weather_hourly`` rows."""
    rows: list[dict[str, Any]] = []
    for coord, payload in zip(coords, payloads):
        hourly = payload.get("hourly") or {}
        times = hourly.get("time") or []
        if not times:
            oc.log(f"No hourly data returned for {coord['district']}")
            continue
        for index, local_time in enumerate(times):
            observed_at = _to_utc(local_time)
            if observed_at is None:
                continue
            weather_code = _series(hourly, "weather_code", index)
            rows.append(
                {
                    "district": coord["district"],
                    "observed_at": observed_at,
                    "temperature_c": _series(hourly, "temperature_2m", index),
                    "precipitation_mm": _series(hourly, "precipitation", index),
                    "wind_kph": _series(hourly, "wind_speed_10m", index),
                    "humidity_pct": _series(hourly, "relative_humidity_2m", index),
                    "weather_code": int(weather_code) if weather_code is not None else None,
                }
            )
    if not rows:
        raise RuntimeError("Open-Meteo returned no usable hourly rows")
    return rows


# ---------------------------------------------------------------------------
# Persistence / Kafka
# ---------------------------------------------------------------------------

def upsert_weather(rows: Sequence[dict[str, Any]]) -> int:
    """Upsert weather rows into ``raw.weather_hourly`` (unique district+observed_at)."""
    from psycopg2.extras import execute_values

    values = [
        (
            row["district"],
            row["observed_at"],
            row["temperature_c"],
            row["precipitation_mm"],
            row["wind_kph"],
            row["humidity_pct"],
            row["weather_code"],
            "open-meteo",
        )
        for row in rows
    ]
    with oc.db_connection() as conn:
        with conn.cursor() as cur:
            execute_values(cur, WEATHER_UPSERT, values, page_size=1000)
    oc.log(f"Upserted {len(values)} weather rows into raw.weather_hourly")
    return len(values)


def fetch_and_load(forecast_days: int | None = None) -> dict[str, Any]:
    """Task 1: Open-Meteo -> ``raw.weather_hourly``."""
    coords = district_centroids()
    payloads = fetch_forecast(coords, forecast_days=forecast_days)
    rows = parse_hourly_rows(coords, payloads)
    upsert_weather(rows)
    return {
        "districts": len(coords),
        "rows": len(rows),
        "forecast_days": forecast_days or oc.WEATHER_FORECAST_DAYS,
        "first_hour": min(row["observed_at"] for row in rows).isoformat(),
        "last_hour": max(row["observed_at"] for row in rows).isoformat(),
    }


def publish_recent_weather(kafka_hours: int | None = None, max_age_minutes: int = 30) -> dict[str, Any]:
    """Task 2: freshly loaded ``raw.weather_hourly`` rows -> ``weather-observations``."""
    import kafka_util

    kafka_hours = kafka_hours or oc.WEATHER_KAFKA_HOURS
    rows = oc.execute_fetch_all(
        """
        SELECT district, observed_at, temperature_c, precipitation_mm, wind_kph,
               humidity_pct, weather_code
          FROM raw.weather_hourly
         WHERE fetched_at >= now() - (%s * interval '1 minute')
           AND observed_at >= now() - interval '1 hour'
           AND observed_at <= now() + (%s * interval '1 hour')
         ORDER BY observed_at, district
        """,
        (max_age_minutes, kafka_hours),
    )
    events = [
        {
            "district": row["district"],
            "observed_at": oc.iso_z(row["observed_at"]),
            "temperature_c": row["temperature_c"],
            "precipitation_mm": row["precipitation_mm"],
            "wind_kph": row["wind_kph"],
            "humidity_pct": row["humidity_pct"],
            "weather_code": row["weather_code"],
            "source": "open-meteo",
        }
        for row in rows
    ]
    published = kafka_util.publish_many(oc.TOPIC_WEATHER, events, key_field="district")
    oc.log(f"Published {published} weather observations to {oc.TOPIC_WEATHER} (next {kafka_hours}h)")
    return {"published": published, "topic": oc.TOPIC_WEATHER, "kafka_hours": kafka_hours}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch Open-Meteo forecasts for Odisha districts into raw.weather_hourly (+ Kafka).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--forecast-days", type=int, default=None, help=f"days to fetch (default {oc.WEATHER_FORECAST_DAYS})")
    parser.add_argument("--kafka-hours", type=int, default=None, help=f"hours of forecast to publish (default {oc.WEATHER_KAFKA_HOURS})")
    parser.add_argument("--no-kafka", action="store_true", help="skip Kafka publication")
    parser.add_argument("--dry-run", action="store_true", help="fetch and print a sample without writing")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.dry_run:
        coords = district_centroids()
        payloads = fetch_forecast(coords, forecast_days=args.forecast_days)
        rows = parse_hourly_rows(coords, payloads)
        print(json.dumps({"districts": len(coords), "rows": len(rows), "sample": rows[:2]}, indent=2, default=str))
        return 0
    summary: dict[str, Any] = {"fetch": fetch_and_load(forecast_days=args.forecast_days)}
    if not args.no_kafka:
        summary["kafka"] = publish_recent_weather(kafka_hours=args.kafka_hours)
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
