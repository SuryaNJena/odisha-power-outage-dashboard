"""Airflow DAG ``odisha_dbt_daily`` - full dbt build at 02:30 IST.

Runs the complete transformation project (seeds -> models -> tests -> docs) once a
day so analytics/marts are rebuilt independently of the 30-minute raw ingest.
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

import odisha_common as oc  # noqa: E402

oc.ensure_scripts_on_path()

DOC_MD = """
# odisha_dbt_daily

Full dbt refresh of the Odisha platform once a day.

**Schedule:** `30 2 * * *` IST.

## Task graph

```
dbt_seed -> dbt_run -> dbt_test -> dbt_docs_generate
```

| task | command |
|---|---|
| `dbt_seed` | `/opt/dbt/bin/dbt seed --project-dir /opt/odisha/platform/dbt --profiles-dir /opt/odisha/platform/dbt` (idempotent district profile seed) |
| `dbt_run` | `dbt run` - all staging/analytics/marts models |
| `dbt_test` | `dbt test` - keys, accepted values, custom reliability/duration assertions |
| `dbt_docs_generate` | `dbt docs generate` - writes manifest/catalog to `$DATA_DIR/dbt/target` |

dbt artifacts (`target/`, `logs/`) are redirected into `$DATA_DIR/dbt` so the
repository mount can be read-only. Profile/env driven by `POSTGRES_*`.

The official 30-minute ingest DAG already runs `dbt run` + `dbt test`; this DAG is
the heavier scheduled rebuild (and re-seeds the district profile).
"""

DBT_TASK_TIMEOUT = timedelta(minutes=45)

with DAG(
    dag_id="odisha_dbt_daily",
    description="Daily full dbt seed/run/test/docs for the Odisha platform",
    schedule="30 2 * * *",
    start_date=oc.DAG_START_DATE,
    catchup=False,
    max_active_runs=1,
    default_args=oc.DEFAULT_ARGS,
    tags=[*oc.DAG_TAGS, "dbt", "daily"],
    doc_md=DOC_MD,
) as dag:
    dbt_seed = BashOperator(
        task_id="dbt_seed",
        bash_command=oc.dbt_command("seed"),
        execution_timeout=DBT_TASK_TIMEOUT,
        doc_md="Load dbt seeds (district_profile.csv) into the seeds schema.",
    )
    dbt_run = BashOperator(
        task_id="dbt_run",
        bash_command=oc.dbt_command("run"),
        execution_timeout=DBT_TASK_TIMEOUT,
        doc_md="Build every dbt model (staging -> analytics -> marts).",
    )
    dbt_test = BashOperator(
        task_id="dbt_test",
        bash_command=oc.dbt_command("test"),
        execution_timeout=DBT_TASK_TIMEOUT,
        doc_md="Run the full dbt test suite.",
    )
    dbt_docs_generate = BashOperator(
        task_id="dbt_docs_generate",
        bash_command=oc.dbt_command("docs", "generate"),
        execution_timeout=DBT_TASK_TIMEOUT,
        doc_md="Generate dbt docs/catalog artifacts under $DATA_DIR/dbt/target.",
    )

    dbt_seed >> dbt_run >> dbt_test >> dbt_docs_generate
