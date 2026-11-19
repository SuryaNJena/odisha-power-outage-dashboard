"""Airflow DAG ``odisha_weather_ingest`` - hourly at :05 IST.

Pipeline: Open-Meteo multi-coordinate forecast for the 30 district centroids ->
``raw.weather_hourly`` upsert -> Kafka ``weather-observations`` (upcoming hours).
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

_DAGS_DIR = Path(__file__).resolve().parent
if str(_DAGS_DIR) not in sys.path:
    sys.path.insert(0, str(_DAGS_DIR))

from airflow import DAG  # noqa: E402
from airflow.operators.python import PythonOperator  # noqa: E402

import odisha_common as oc  # noqa: E402

oc.ensure_scripts_on_path()

DOC_MD = """
# odisha_weather_ingest

Hourly weather forecast ingest used by `analytics.district_risk_daily` and the
Spark risk forecast.

**Schedule:** `5 * * * *` IST (hourly at :05).

## Task graph

```
fetch_weather -> publish_weather_kafka
```

| task | what it does |
|---|---|
| `fetch_weather` | one Open-Meteo request for all district centroids (`latitude=a,b,c&longitude=...`), hourly `precipitation`, `temperature_2m`, `wind_speed_10m`, `relative_humidity_2m`, `weather_code`, `forecast_days=2`, `timezone=Asia/Kolkata`; upserts `raw.weather_hourly` on `(district, observed_at)` |
| `publish_weather_kafka` | publishes the freshly loaded upcoming 24h (configurable) as one JSON per district-hour to Kafka `weather-observations` |

Centroids come from `raw.district_centroids` (loaded by `odisha_bootstrap` from
`data/mappings/district_centroids.json`); the JSON file is used as a fallback if
the table is empty.

## Env

`WEATHER_FORECAST_DAYS` (2), `WEATHER_KAFKA_HOURS` (24),
`WEATHER_REQUEST_TIMEOUT` (60), `WEATHER_MAX_COORDS_PER_REQUEST` (30),
`KAFKA_BOOTSTRAP`, `POSTGRES_*`.
"""


def run_fetch_weather(**context) -> dict:
    """Fetch the Open-Meteo forecast and upsert raw.weather_hourly."""
    import fetch_weather

    return fetch_weather.fetch_and_load()


def run_publish_weather(**context) -> dict:
    """Publish recently refreshed weather rows to Kafka."""
    import fetch_weather

    return fetch_weather.publish_recent_weather()


with DAG(
    dag_id="odisha_weather_ingest",
    description="Hourly Open-Meteo district forecast -> raw.weather_hourly -> Kafka weather-observations",
    schedule="5 * * * *",
    start_date=oc.DAG_START_DATE,
    catchup=False,
    max_active_runs=1,
    default_args=oc.DEFAULT_ARGS,
    tags=[*oc.DAG_TAGS, "ingest", "weather", "kafka"],
    doc_md=DOC_MD,
) as dag:
    fetch_weather = PythonOperator(
        task_id="fetch_weather",
        python_callable=run_fetch_weather,
        execution_timeout=timedelta(minutes=10),
        doc_md="Fetch Open-Meteo forecasts for all district centroids and upsert raw.weather_hourly.",
    )
    publish_weather_kafka = PythonOperator(
        task_id="publish_weather_kafka",
        python_callable=run_publish_weather,
        execution_timeout=timedelta(minutes=10),
        doc_md="Publish the upcoming forecast hours to Kafka weather-observations.",
    )

    fetch_weather >> publish_weather_kafka
