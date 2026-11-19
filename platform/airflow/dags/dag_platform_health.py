"""Airflow DAG ``odisha_platform_health`` - every 10 minutes.

Monitors the platform and records heartbeats in Postgres:

* Postgres connectivity;
* Kafka broker + required topic existence;
* Flink job presence in the cluster overview - **re-submits the SQL job** when it
  is missing (when a submission path is configured);
* Spark streaming heartbeat freshness (`ops.pipeline_heartbeat` row written by
  `spark-stream`).

Heartbeats are upserted into ``ops.pipeline_heartbeat`` and the run is recorded in
``ops.job_runs``.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

_DAGS_DIR = Path(__file__).resolve().parent
if str(_DAGS_DIR) not in sys.path:
    sys.path.insert(0, str(_DAGS_DIR))

from airflow import DAG  # noqa: E402
from airflow.operators.python import PythonOperator  # noqa: E402

import odisha_common as oc  # noqa: E402

oc.ensure_scripts_on_path()

DOC_MD = """
# odisha_platform_health

10-minute platform heartbeat + self-healing check.

**Schedule:** `*/10 * * * *` IST.

## Task graph

```
collect_health -> emit_heartbeats
```

| component | check | self-healing |
|---|---|---|
| `postgres` | `SELECT 1` | - |
| `kafka` | broker reachable + all 5 contract topics exist | topics are created by `odisha_bootstrap` / publishers |
| `flink` | job `odisha-user-report-alerts` appears RUNNING in `GET /jobs/overview` | when missing, re-submits `platform/flink/jobs/user_report_alerts.sql` via the Flink SQL Gateway (`FLINK_SQL_GATEWAY_URL`) or an operator-provided `FLINK_RESUBMIT_CMD` |
| `spark` | `ops.pipeline_heartbeat('spark-stream').last_seen` is younger than `SPARK_HEARTBEAT_MAX_AGE_S` (600s) | - (Spark streaming writes its own heartbeat; Airflow never overwrites it) |

## Flink re-submission

In the deployed compose stack the `flink-submit` sidecar already re-runs
`platform/flink/submit.sh` idempotently every 180s, so a missing job is normally
recovered there. This DAG adds an Airflow-side belt-and-braces path when one of the
following is configured:

* `FLINK_SQL_GATEWAY_URL=http://flink-jobmanager:8083` - recommended if you enable
  the Flink SQL Gateway; the DAG opens a SQL Gateway session and executes the
  statements of the rendered SQL file (the file must set `pipeline.name` to
  `odisha-user-report-alerts` - the shipped SQL does).
* `FLINK_RESUBMIT_CMD="<shell command>"` - any command that re-submits the job from
  inside the Airflow container.

When neither is set the DAG records `flink: degraded` with an explanatory detail
and keeps running (the next scheduler cycle retries the presence check); the
platform is still monitored and visible in the Airflow UI.  Airflow never
overwrites the `spark-stream` heartbeat row - it is written by the Spark streaming
job and monitored as `spark` here.
"""


def collect_health(**context) -> dict[str, Any]:
    """Probe Postgres, Kafka, Flink and Spark; returns a structured health snapshot."""
    import kafka_util

    result: dict[str, Any] = {
        "checked_at": oc.iso_z(),
        "dag_run_id": context.get("run_id"),
        "components": {},
    }
    components = result["components"]

    # --- Postgres -----------------------------------------------------------
    try:
        oc.execute_fetch_all("SELECT 1 AS ok")
        components["postgres"] = {"ok": True}
    except Exception as exc:  # noqa: BLE001 - recorded in the heartbeat/UI
        components["postgres"] = {"ok": False, "error": repr(exc)}

    # --- Kafka --------------------------------------------------------------
    components["kafka"] = kafka_util.check_topics()

    # --- Flink --------------------------------------------------------------
    components["flink"] = oc.ensure_flink_job()

    # --- Spark streaming heartbeat -----------------------------------------
    try:
        heartbeat = oc.read_heartbeat("spark-stream")
        if heartbeat is None:
            components["spark"] = {"ok": False, "status": "missing", "detail": "no ops.pipeline_heartbeat row"}
        else:
            last_seen = oc.parse_iso(heartbeat.get("last_seen"))
            age = oc.seconds_since(last_seen)
            components["spark"] = {
                "ok": age <= oc.SPARK_HEARTBEAT_MAX_AGE_S,
                "status": heartbeat.get("status"),
                "last_seen": oc.iso_z(last_seen) if last_seen else None,
                "age_seconds": round(age, 1) if age != float("inf") else None,
                "max_age_seconds": oc.SPARK_HEARTBEAT_MAX_AGE_S,
                "detail": heartbeat.get("detail"),
            }
    except Exception as exc:  # noqa: BLE001 - recorded in the heartbeat/UI
        components["spark"] = {"ok": False, "error": repr(exc)}

    ok_components = [name for name, component in components.items() if component.get("ok")]
    unhealthy = [name for name, component in components.items() if not component.get("ok")]
    result["unhealthy"] = unhealthy
    result["ok"] = not unhealthy
    result["summary"] = f"ok={ok_components} unhealthy={unhealthy}"
    oc.log(f"Health snapshot: {result['summary']}")
    return result


def emit_heartbeats(**context) -> dict[str, Any]:
    """Persist the health snapshot to ops.pipeline_heartbeat / ops.job_runs."""
    task_instance = context["ti"]
    health = task_instance.xcom_pull(task_ids="collect_health") or {}
    components = health.get("components") or {}
    overall = "ok" if health.get("ok") else "degraded"
    detail = {
        "unhealthy": health.get("unhealthy"),
        "checked_at": health.get("checked_at"),
        "components": components,
    }

    # Never write the "spark-stream" component: that row is owned by the Spark
    # streaming job and is the very signal this DAG monitors.
    for component_name in ("postgres", "kafka", "flink", "spark"):
        component = components.get(component_name) or {}
        write_name = "spark-monitor" if component_name == "spark" else component_name
        status = "ok" if component.get("ok") else "degraded"
        oc.write_heartbeat(write_name, status, detail=component)
    oc.write_heartbeat("airflow", overall, detail=detail)

    dag_run = context.get("dag_run")
    started_at = getattr(dag_run, "start_date", None)
    try:
        oc.record_job_run(
            job_name="odisha_platform_health",
            run_id=str(context.get("run_id") or ""),
            status=overall,
            started_at=started_at,
            finished_at=oc.utc_now(),
            detail=detail,
        )
    except Exception as exc:  # noqa: BLE001 - heartbeats already written; don't mask them
        oc.log(f"WARNING: could not write ops.job_runs row: {exc!r}")

    if overall != "ok":
        oc.log(f"Platform degraded: {health.get('summary')}")
    return {"overall": overall, "unhealthy": health.get("unhealthy")}


with DAG(
    dag_id="odisha_platform_health",
    description="10-minute heartbeat: Postgres/Kafka/Flink/Spark checks, Flink SQL re-submit if missing",
    schedule="*/10 * * * *",
    start_date=oc.DAG_START_DATE,
    catchup=False,
    max_active_runs=1,
    default_args=oc.DEFAULT_ARGS,
    tags=[*oc.DAG_TAGS, "health", "ops"],
    doc_md=DOC_MD,
) as dag:
    collect_health_task = PythonOperator(
        task_id="collect_health",
        python_callable=collect_health,
        execution_timeout=timedelta(minutes=5),
        doc_md="Probe Postgres/Kafka/Flink/Spark and attempt Flink SQL re-submission if the job is missing.",
    )
    emit_heartbeats_task = PythonOperator(
        task_id="emit_heartbeats",
        python_callable=emit_heartbeats,
        execution_timeout=timedelta(minutes=5),
        doc_md="Upsert ops.pipeline_heartbeat rows and record this health run in ops.job_runs.",
    )

    collect_health_task >> emit_heartbeats_task
