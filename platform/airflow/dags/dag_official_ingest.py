"""Airflow DAG ``odisha_official_ingest`` - every 30 minutes.

Pipeline: scrape kavach.tpodisha.com -> load ``raw.official_outages`` -> dbt run ->
dbt test -> publish the map datamart JSON -> publish official outages to Kafka.

Idempotency: the batch id is the DAG run's ``data_interval_start`` floored to the
minute (UTC ``...Z``), so retries reuse the same ``raw.scrape_batches`` row and
``INSERT ... ON CONFLICT (batch_id, row_hash) DO NOTHING`` makes raw loads safe.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

_DAGS_DIR = Path(__file__).resolve().parent
if str(_DAGS_DIR) not in sys.path:
    sys.path.insert(0, str(_DAGS_DIR))

from airflow import DAG  # noqa: E402
from airflow.operators.bash import BashOperator  # noqa: E402
from airflow.operators.python import PythonOperator  # noqa: E402

import odisha_common as oc  # noqa: E402

oc.ensure_scripts_on_path()

DOC_MD = """
# odisha_official_ingest

Scrapes the Kavach public outage feed (`kavach.tpodisha.com`) and refreshes every
downstream artifact used by the dashboard.

**Schedule:** every 30 minutes (`*/30 * * * *`, IST).

## Task graph

```
scrape_official -> load_official -> dbt_run -> dbt_test -> publish_mart -> publish_official_kafka
```

| task | what it does |
|---|---|
| `scrape_official` | runs the repository `scrap.py` for CODL/NODL/WODL/SODL; writes CSVs to `$SCRAPED_DIR`; creates `raw.scrape_batches` (status `running`) |
| `load_official` | parses CSVs exactly like `process.py` (`parse_raw_row`, sha1 `row_hash`), inserts `raw.official_outages` `ON CONFLICT DO NOTHING`, marks the batch `success` |
| `dbt_run` | `/opt/dbt/bin/dbt run --project-dir /opt/odisha/platform/dbt --profiles-dir /opt/odisha/platform/dbt` |
| `dbt_test` | `dbt test` - data-quality gate |
| `publish_mart` | `marts.map_dots` -> `$DATA_DIR/mart/outages.json` (atomic write, Go API/static fallback shape) |
| `publish_official_kafka` | one JSON document per outage row -> Kafka `official-outages` |

## Failure behaviour

* scrape/load failures are retried twice (2 minutes apart); the batch row is set to
  `failed` with an error detail when a retry set is exhausted.
* `dbt_test` failures block publication so a broken mart never reaches the API.
* Kafka publication failures fail the run but do not corrupt raw/Postgres state.

## Env

`POSTGRES_*`/`DATABASE_URL`, `KAFKA_BOOTSTRAP`, `DATA_DIR`, `REPO_DIR`,
`DBT_*`, `SCRAPED_DIR` (see `platform/airflow/README.md`).
"""


def run_scrape(**context) -> dict:
    """Scrape the feed for this DAG run's batch id."""
    import scrape_official

    batch_id = oc.batch_id_for_context(context)
    oc.log(f"odisha_official_ingest: scraping batch {batch_id}")
    return scrape_official.scrape(batch_id=batch_id)


def run_load(**context) -> dict:
    """Load the freshly scraped CSVs into Postgres."""
    import load_official

    task_instance = context["ti"]
    scrape_summary = task_instance.xcom_pull(task_ids="scrape_official") or {}
    batch_id = scrape_summary.get("batch_id") or oc.batch_id_for_context(context)
    return load_official.load(
        batch_id=batch_id,
        csv_dir=scrape_summary.get("scraped_dir"),
    )


def run_publish_mart(**context) -> dict:
    """Publish marts.map_dots as the Go/static datamart JSON."""
    import publish_mart

    return publish_mart.publish()


def run_publish_kafka(**context) -> dict:
    """Publish this batch's raw outage rows to Kafka official-outages."""
    import load_official

    task_instance = context["ti"]
    load_summary = task_instance.xcom_pull(task_ids="load_official") or {}
    batch_id = load_summary.get("batch_id") or oc.batch_id_for_context(context)
    return load_official.publish_batch_to_kafka(batch_id)


with DAG(
    dag_id="odisha_official_ingest",
    description="Scrape Kavach official outages, load raw, dbt run/test, publish mart JSON + Kafka",
    schedule="*/30 * * * *",
    start_date=oc.DAG_START_DATE,
    catchup=False,
    max_active_runs=1,
    default_args=oc.DEFAULT_ARGS,
    tags=[*oc.DAG_TAGS, "ingest", "official", "dbt"],
    doc_md=DOC_MD,
) as dag:
    scrape_official = PythonOperator(
        task_id="scrape_official",
        python_callable=run_scrape,
        execution_timeout=timedelta(minutes=15),
        doc_md="Scrape kavach.tpodisha.com for all 4 DISCOMs and register the scrape batch.",
    )
    load_official = PythonOperator(
        task_id="load_official",
        python_callable=run_load,
        execution_timeout=timedelta(minutes=10),
        doc_md="Parse the batch CSVs with process.py semantics into raw.official_outages.",
    )
    dbt_run = BashOperator(
        task_id="dbt_run",
        bash_command=oc.dbt_command("run"),
        execution_timeout=timedelta(minutes=30),
        doc_md="Build all dbt models (staging -> analytics -> marts).",
    )
    dbt_test = BashOperator(
        task_id="dbt_test",
        bash_command=oc.dbt_command("test"),
        execution_timeout=timedelta(minutes=20),
        doc_md="Run dbt data-quality tests; failure gates mart publication.",
    )
    publish_mart = PythonOperator(
        task_id="publish_mart",
        python_callable=run_publish_mart,
        execution_timeout=timedelta(minutes=5),
        doc_md="Write $DATA_DIR/mart/outages.json from marts.map_dots (atomic).",
    )
    publish_official_kafka = PythonOperator(
        task_id="publish_official_kafka",
        python_callable=run_publish_kafka,
        execution_timeout=timedelta(minutes=10),
        doc_md="Publish one JSON document per raw outage row to Kafka official-outages.",
    )

    scrape_official >> load_official >> dbt_run >> dbt_test >> publish_mart >> publish_official_kafka
