"""Airflow DAG ``odisha_spark_batch`` - daily PySpark batch at 03:00 IST.

Submits ``platform/spark/jobs/daily_batch.py`` with the baked JDBC driver.  The
``spark-submit`` binary is discovered at task runtime from the pip-installed
PySpark distribution (``os.path.dirname(pyspark.__file__)/bin/spark-submit``), so
no Spark distribution has to be installed system-wide.
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
# odisha_spark_batch

Daily Spark batch for ML/analytics outputs:
`ml.outage_clusters`, `ml.etr_model_metrics`, `ml.district_risk_forecast`, plus the
parquet archive under `/opt/odisha/data/lake/outages/dt=YYYY-MM-DD/`.

**Schedule:** `0 3 * * *` IST (after the 02:30 dbt rebuild).

## What runs

```
<pyspark>/bin/spark-submit \
    --master local[2] \
    --jars /opt/jdbc/postgresql-42.7.4.jar \
    /opt/odisha/platform/spark/jobs/daily_batch.py \
    --jdbc-url jdbc:postgresql://postgres:5432/odisha
```

* Postgres credentials are **not** placed on the command line - the job reads
  `POSTGRES_*` / `DATABASE_URL` from the inherited container environment.
* `JAVA_HOME` is baked into the image (OpenJDK 17, required by Spark 3.5).
* The job is written to be robust to empty source tables (logs and exits 0), so an
  empty platform does not page anyone.

## Env

`SPARK_MASTER` (local[2]), `SPARK_EXTRA_CONF` (`;`-separated `--conf` values),
`SPARK_SUBMIT_TIMEOUT` (3600), `JDBC_JAR`, `SPARK_JOBS_DIR`, `REPO_DIR`.
"""


def run_spark_batch(**context) -> dict:
    """Submit the daily PySpark batch job and surface its output tail."""
    job_file = oc.SPARK_JOBS_DIR / "daily_batch.py"
    app_args = ["--jdbc-url", oc.jdbc_url()]
    output = oc.run_spark_submit(job_file, app_args=app_args)
    output_tail = oc.tail_text(output, 20)
    oc.log(f"daily_batch.py finished; output tail:\n{output_tail}")
    return {"job_file": str(job_file), "jdbc_url": oc.jdbc_url(), "output_tail": output_tail}


with DAG(
    dag_id="odisha_spark_batch",
    description="Daily spark-submit of platform/spark/jobs/daily_batch.py (ML tables + parquet archive)",
    schedule="0 3 * * *",
    start_date=oc.DAG_START_DATE,
    catchup=False,
    max_active_runs=1,
    default_args=oc.DEFAULT_ARGS,
    tags=[*oc.DAG_TAGS, "spark", "ml", "daily"],
    doc_md=DOC_MD,
) as dag:
    spark_daily_batch = PythonOperator(
        task_id="spark_daily_batch",
        python_callable=run_spark_batch,
        execution_timeout=timedelta(hours=2),
        doc_md="Run platform/spark/jobs/daily_batch.py via pyspark's spark-submit with the Postgres JDBC jar.",
    )
