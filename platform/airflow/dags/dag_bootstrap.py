"""Airflow DAG ``odisha_bootstrap`` - one-off manual bootstrap (schedule=None).

Run this once after ``docker compose up`` on a fresh stack: it creates the Kafka
topics, loads the reference tables from ``data/mappings/*.json``, performs the first
scrape/load, and builds the first dbt models so the Go API/frontend have data.

Trigger it from the host::

    docker compose exec airflow airflow dags unpause odisha_bootstrap
    docker compose exec airflow airflow dags trigger odisha_bootstrap

or from the Airflow UI ("Trigger DAG").  Every step is idempotent, so it is safe to
re-run after a wipe/restore.
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
# odisha_bootstrap

Manual, one-off first-run bootstrap for a fresh Odisha platform deployment
(`schedule=None`, nothing runs automatically).

## Task graph

```
ensure_kafka_topics -> load_reference -> scrape_official -> load_official
                    -> dbt_seed -> dbt_run -> dbt_test -> publish_mart -> publish_official_kafka
```

| task | what it does |
|---|---|
| `ensure_kafka_topics` | idempotently create the 5 contract topics (3 partitions, replication 1, 24h retention) |
| `load_reference` | `data/mappings/*.json` -> `raw.district_aliases`, `raw.circle_exact`, `raw.district_centroids`, `raw.station_geocodes` |
| `scrape_official` | first scrape of `kavach.tpodisha.com` |
| `load_official` | first load into `raw.official_outages` |
| `dbt_seed` | load `district_profile.csv` into the dbt seeds schema |
| `dbt_run` | first full dbt build |
| `dbt_test` | validate the freshly built models |
| `publish_mart` | write `$DATA_DIR/mart/outages.json` for the Go API/static fallback |
| `publish_official_kafka` | publish the first batch to Kafka `official-outages` |

All steps are idempotent (upserts / `ON CONFLICT DO NOTHING` / topic-exists checks),
so re-running this DAG is safe.
"""


def run_ensure_topics(**context) -> dict:
    """Create any missing Kafka topics (idempotent)."""
    import kafka_util

    return kafka_util.ensure_topics()


def run_load_reference(**context) -> dict:
    """Load the JSON reference mappings into the raw reference tables."""
    import load_reference

    return load_reference.load_reference()


def run_scrape(**context) -> dict:
    """First scrape with a deterministic batch id."""
    import scrape_official

    return scrape_official.scrape(batch_id=oc.batch_id_for_context(context))


def run_load(**context) -> dict:
    """First raw load for the bootstrap batch."""
    import load_official

    task_instance = context["ti"]
    scrape_summary = task_instance.xcom_pull(task_ids="scrape_official") or {}
    batch_id = scrape_summary.get("batch_id") or oc.batch_id_for_context(context)
    return load_official.load(batch_id=batch_id, csv_dir=scrape_summary.get("scraped_dir"))


def run_publish_mart(**context) -> dict:
    """Write the first datamart JSON consumed by the Go API/frontend fallback."""
    import publish_mart

    return publish_mart.publish()


def run_publish_kafka(**context) -> dict:
    """Publish the bootstrap batch to Kafka official-outages."""
    import load_official

    task_instance = context["ti"]
    load_summary = task_instance.xcom_pull(task_ids="load_official") or {}
    batch_id = load_summary.get("batch_id") or oc.batch_id_for_context(context)
    return load_official.publish_batch_to_kafka(batch_id)


with DAG(
    dag_id="odisha_bootstrap",
    description="Manual first-run bootstrap: Kafka topics, reference data, first scrape/load, dbt, mart JSON, Kafka",
    schedule=None,
    start_date=oc.DAG_START_DATE,
    catchup=False,
    max_active_runs=1,
    default_args=oc.DEFAULT_ARGS,
    tags=[*oc.DAG_TAGS, "bootstrap", "manual"],
    doc_md=DOC_MD,
) as dag:
    ensure_kafka_topics = PythonOperator(
        task_id="ensure_kafka_topics",
        python_callable=run_ensure_topics,
        execution_timeout=timedelta(minutes=5),
        doc_md="Idempotently create the contract Kafka topics.",
    )
    load_reference = PythonOperator(
        task_id="load_reference",
        python_callable=run_load_reference,
        execution_timeout=timedelta(minutes=10),
        doc_md="Upsert data/mappings/*.json into the raw reference tables.",
    )
    scrape_official = PythonOperator(
        task_id="scrape_official",
        python_callable=run_scrape,
        execution_timeout=timedelta(minutes=15),
        doc_md="First scrape of the Kavach outage feed.",
    )
    load_official = PythonOperator(
        task_id="load_official",
        python_callable=run_load,
        execution_timeout=timedelta(minutes=10),
        doc_md="Load the first scrape batch into raw.official_outages.",
    )
    dbt_seed = BashOperator(
        task_id="dbt_seed",
        bash_command=oc.dbt_command("seed"),
        execution_timeout=timedelta(minutes=30),
        doc_md="Seed district_profile.csv into the dbt seeds schema.",
    )
    dbt_run = BashOperator(
        task_id="dbt_run",
        bash_command=oc.dbt_command("run"),
        execution_timeout=timedelta(minutes=45),
        doc_md="First full dbt build.",
    )
    dbt_test = BashOperator(
        task_id="dbt_test",
        bash_command=oc.dbt_command("test"),
        execution_timeout=timedelta(minutes=30),
        doc_md="Validate the freshly built models.",
    )
    publish_mart = PythonOperator(
        task_id="publish_mart",
        python_callable=run_publish_mart,
        execution_timeout=timedelta(minutes=5),
        doc_md="Write the first $DATA_DIR/mart/outages.json.",
    )
    publish_official_kafka = PythonOperator(
        task_id="publish_official_kafka",
        python_callable=run_publish_kafka,
        execution_timeout=timedelta(minutes=10),
        doc_md="Publish the first official-outage batch to Kafka.",
    )

    (
        ensure_kafka_topics
        >> load_reference
        >> scrape_official
        >> load_official
        >> dbt_seed
        >> dbt_run
        >> dbt_test
        >> publish_mart
        >> publish_official_kafka
    )
