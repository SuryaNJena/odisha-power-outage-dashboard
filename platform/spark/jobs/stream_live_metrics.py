#!/usr/bin/env python3
"""Odisha power-outage platform -- Spark Structured Streaming NRT metrics.

Consumes the Kafka topics ``user-reports`` and ``outage-alerts`` and keeps the
Postgres ``realtime`` tables (read by the Go API / dashboard) up to date.

Stream query graph
------------------
::

    user-reports (Kafka)
        -> from_json(explicit schema) -> event_ts = reported_at (fallback record ts)
        -> watermark(event_ts, 10 minutes)
        -> 1-minute tumbling window per district
             report_count = count(*)
             unique_reporters = approx_count_distinct(reporter)
             avg_severity = avg(severity)
        -> foreachBatch(report_sink)
             * upsert realtime.report_minute          (bucket, district) unique
             * recompute last-15-minute aggregates from realtime.report_minute
               and upsert realtime.district_live_metrics
             * keep ops.pipeline_heartbeat('spark-stream') fresh

    outage-alerts (Kafka)
        -> from_json(explicit schema)
        -> foreachBatch(alert_sink)
             * insert realtime.alerts on conflict (alert_id) do nothing
             * keep ops.pipeline_heartbeat('spark-stream') fresh

The two streams are independent (no stream-stream join); they have separate
checkpoints.  Both run with a ``processingTime='30 seconds'`` trigger and write
through psycopg2 in short-lived transactions.

Run it with ``spark-submit`` (never ``python``); see ``platform/spark/README.md``
for the exact commands, Java 17 / Spark 3.5.4 notes and memory settings.

Environment defaults (all overridable by CLI flags)
---------------------------------------------------
``KAFKA_BOOTSTRAP``             default ``kafka:9092``
``DATABASE_URL``                used to derive the JDBC url when not given
``POSTGRES_HOST/PORT/DB``       default ``postgres:5432/odisha``
``POSTGRES_USER/PASSWORD``      default ``odisha`` / ``odisha``
``SPARK_CHECKPOINT_DIR``        default ``/opt/spark-checkpoints/stream-live``
``SPARK_TRIGGER_INTERVAL``      default ``30 seconds``
``SPARK_MAX_OFFSETS_PER_TRIGGER`` default ``5000``
``SPARK_KAFKA_TOPIC_REPORTS``   default ``user-reports``
``SPARK_KAFKA_TOPIC_ALERTS``    default ``outage-alerts``
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

import psycopg2
from psycopg2 import errors as pg_errors
from psycopg2.extras import execute_values
from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import (
    DoubleType,
    IntegerType,
    StringType,
    StructField,
    StructType,
)

LOG = logging.getLogger("odisha.spark.stream")

APP_NAME = "odisha-spark-stream-live-metrics"

# ---------------------------------------------------------------------------
# Contract constants
# ---------------------------------------------------------------------------
HEARTBEAT_COMPONENT = "spark-stream"
DEFAULT_BOOTSTRAP = "kafka:9092"
DEFAULT_CHECKPOINT_DIR = "/opt/spark-checkpoints/stream-live"
DEFAULT_TRIGGER_INTERVAL = "30 seconds"
DEFAULT_MAX_OFFSETS_PER_TRIGGER = 5000
DEFAULT_TOPIC_REPORTS = "user-reports"
DEFAULT_TOPIC_ALERTS = "outage-alerts"
REPORT_WINDOW = "1 minute"
# 1-minute watermark keeps the NRT dashboard fresh (a 1-minute window is
# emitted about a minute after it closes). Raise this only if reports can arrive
# more than a minute out of order.
WATERMARK = "1 minute"
LIVE_WINDOW_MINUTES = 15
REPORT_MINUTE_RETENTION_HOURS = 24

#: ISO8601 candidates, tried in order, each with an exact Spark SQL pattern.
#: No optional ``[...]`` pattern sections are used (not universally supported);
#: ``XXX`` accepts both ``Z`` and ``+05:30`` style offsets.
_TIMESTAMP_FORMATS = (
    "yyyy-MM-dd'T'HH:mm:ss.SSSXXX",
    "yyyy-MM-dd'T'HH:mm:ssXXX",
    "yyyy-MM-dd'T'HH:mm:ss.SSSSSSXXX",
    "yyyy-MM-dd'T'HH:mm:ss.SSS",
    "yyyy-MM-dd'T'HH:mm:ss.SSSSSS",
    "yyyy-MM-dd'T'HH:mm:ss",
    "yyyy-MM-dd HH:mm:ss.SSS",
    "yyyy-MM-dd HH:mm:ss",
)

#: Explicit Kafka payload schemas (do not let Spark infer these).
USER_REPORT_SCHEMA = StructType(
    [
        StructField("report_id", StringType(), True),
        StructField("district", StringType(), True),
        StructField("gss", StringType(), True),
        StructField("feeder", StringType(), True),
        StructField("comment", StringType(), True),
        StructField("severity", IntegerType(), True),
        StructField("reporter", StringType(), True),
        StructField("lat", DoubleType(), True),
        StructField("lon", DoubleType(), True),
        StructField("reported_at", StringType(), True),
    ]
)

OUTAGE_ALERT_SCHEMA = StructType(
    [
        StructField("alert_id", StringType(), True),
        StructField("district", StringType(), True),
        StructField("alert_type", StringType(), True),
        StructField("severity", StringType(), True),
        StructField("message", StringType(), True),
        StructField("report_count", IntegerType(), True),
        StructField("window_start", StringType(), True),
        StructField("window_end", StringType(), True),
    ]
)

# ---------------------------------------------------------------------------
# SQL (schema qualified, matches platform/CONTRACT.md)
# ---------------------------------------------------------------------------
UPSERT_REPORT_MINUTE = """
insert into realtime.report_minute (bucket, district, report_count, avg_severity)
values %s
on conflict (bucket, district) do update
set report_count = excluded.report_count,
    avg_severity = excluded.avg_severity
where realtime.report_minute.report_count is distinct from excluded.report_count
   or realtime.report_minute.avg_severity is distinct from excluded.avg_severity
"""

#: Recomputes the live 15-minute view from realtime.report_minute and upserts
#: realtime.district_live_metrics.  ``%(unique_json)s`` is a JSON object
#: ``{"district": unique_reporters}`` taken from the current micro-batch (the
#: contract allows approximating unique_reporters by the max of the batch).
UPSERT_DISTRICT_LIVE_METRICS = """
with win as (
    select district,
           sum(report_count)::int as report_count
    from realtime.report_minute
    where bucket >= now() - interval '15 minutes'
      and bucket <= now()
    group by district
),
batch_unique as (
    select key as district,
           value::int as unique_reporters
    from jsonb_each_text(%(unique_json)s::jsonb)
),
latest_risk as (
    select distinct on (district) district, risk_score
    from analytics.district_risk_daily
    order by district, risk_date desc
)
insert into realtime.district_live_metrics
    (district, report_count, unique_reporters, official_outages,
     unconfirmed_reports, risk_score, window_start, window_end, updated_at)
select w.district,
       w.report_count,
       coalesce(bu.unique_reporters,
                least(coalesce(ex.unique_reporters, w.report_count),
                      w.report_count))::int as unique_reporters,
       coalesce(s.active_outages, 0)::int as official_outages,
       greatest(w.report_count - coalesce(s.active_outages, 0), 0)::int
           as unconfirmed_reports,
       coalesce(r.risk_score, ex.risk_score) as risk_score,
       now() - interval '15 minutes' as window_start,
       now() as window_end,
       now() as updated_at
from win w
left join batch_unique bu on bu.district = w.district
left join analytics.district_live_snapshot s on s.district = w.district
left join latest_risk r on r.district = w.district
left join realtime.district_live_metrics ex on ex.district = w.district
on conflict (district) do update
set report_count = excluded.report_count,
    unique_reporters = excluded.unique_reporters,
    official_outages = excluded.official_outages,
    unconfirmed_reports = excluded.unconfirmed_reports,
    risk_score = coalesce(excluded.risk_score,
                          realtime.district_live_metrics.risk_score),
    window_start = excluded.window_start,
    window_end = excluded.window_end,
    updated_at = now()
"""

#: Districts that dropped out of the live 15-minute window go back to zero so
#: the dashboard never shows phantom reports that are older than the window.
ZERO_QUIET_DISTRICTS = """
update realtime.district_live_metrics dlm
set report_count = 0,
    unique_reporters = 0,
    unconfirmed_reports = 0,
    window_start = now() - interval '15 minutes',
    window_end = now(),
    updated_at = now()
where dlm.report_count > 0
  and not exists (
      select 1
      from realtime.report_minute rm
      where rm.district = dlm.district
        and rm.bucket >= now() - interval '15 minutes'
  )
"""

PRUNE_REPORT_MINUTE = """
delete from realtime.report_minute
where bucket < now() - interval '24 hours'
"""

INSERT_ALERTS = """
insert into realtime.alerts
    (alert_id, district, alert_type, severity, message, report_count,
     window_start, window_end, source)
values %s
on conflict (alert_id) do nothing
"""

UPSERT_HEARTBEAT = """
insert into ops.pipeline_heartbeat (component, status, last_seen, detail)
values ('spark-stream', %s, now(), %s::jsonb)
on conflict (component) do update
set status = excluded.status,
    last_seen = excluded.last_seen,
    detail = excluded.detail
"""


# ---------------------------------------------------------------------------
# Errors / helpers
# ---------------------------------------------------------------------------
class MissingDependencyError(RuntimeError):
    """Raised when an upstream table/view has not been created yet (e.g. dbt
    has not run).  The stream stays alive and retries on the next batch."""


def _json_safe(value: Any) -> Any:
    """Recursively make ``value`` safe for ``json.dumps`` (NaN/Inf -> None)."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (datetime,)):
        return value.isoformat()
    return value


def _parse_timestamp(column_name: str) -> F.Column:
    """Parse an ISO8601 Kafka string column into a ``timestamp`` column.

    ``try_to_timestamp`` (Spark 3.5) returns NULL for unparseable values instead
    of raising, and the session forces ``timeParserPolicy=CORRECTED`` as a second
    guard, so a single malformed Kafka message can never kill the stream.
    """
    parse = getattr(F, "try_to_timestamp", None) or F.to_timestamp
    raw = F.col(column_name)
    # NOTE: the format argument must be a Column literal. Passing a plain Python
    # str makes PySpark resolve it as a column name (=> UNRESOLVED_COLUMN at
    # analysis time), which is exactly what bit the first live deploy.
    candidates = [parse(raw, F.lit(fmt)) for fmt in _TIMESTAMP_FORMATS]
    return F.coalesce(*candidates)


def _epoch_to_datetime(value: Any) -> Optional[datetime]:
    """Convert Spark epoch-seconds (UTC) to an aware ``datetime``.

    Epoch integers are used instead of collected ``timestamp`` values so the
    Python side never depends on PySpark's timestamp timezone conversion.
    """
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def jdbc_url_to_dsn(jdbc_url: str) -> str:
    """Convert a JDBC Postgres URL into a psycopg2/libpq DSN string."""
    value = (jdbc_url or "").strip()
    if value.startswith("jdbc:"):
        value = value[len("jdbc:"):]
    if value.startswith("postgres://"):
        value = "postgresql://" + value[len("postgres://"):]
    if not value.startswith("postgresql://"):
        value = "postgresql://" + value.lstrip("/")
    return value


def _database_url_parts() -> Tuple[Optional[str], Optional[int], Optional[str],
                                   Optional[str], Optional[str]]:
    """Best-effort parse of ``DATABASE_URL`` -> (host, port, db, user, password)."""
    raw = os.getenv("DATABASE_URL", "").strip()
    if not raw:
        return None, None, None, None, None
    try:
        parsed = urlparse(raw)
    except ValueError:
        return None, None, None, None, None
    host = parsed.hostname
    port = parsed.port
    database = (parsed.path or "").lstrip("/") or None
    user = unquote(parsed.username) if parsed.username else None
    password = unquote(parsed.password) if parsed.password else None
    return host, port, database, user, password


def default_jdbc_url() -> str:
    explicit = os.getenv("SPARK_JDBC_URL") or os.getenv("JDBC_URL")
    if explicit:
        return explicit
    host, port, database, _user, _password = _database_url_parts()
    host = host or os.getenv("POSTGRES_HOST", "postgres")
    port = port or int(os.getenv("POSTGRES_PORT", "5432"))
    database = database or os.getenv("POSTGRES_DB", "odisha")
    return "jdbc:postgresql://{}:{}/{}".format(host, port, database)


def default_credentials() -> Tuple[str, str]:
    host, _port, _db, url_user, url_password = _database_url_parts()
    del host
    user = os.getenv("SPARK_JDBC_USER") or os.getenv("POSTGRES_USER") or url_user
    password = os.getenv("SPARK_JDBC_PASSWORD")
    if password is None:
        password = os.getenv("POSTGRES_PASSWORD")
    if password is None:
        password = url_password
    return user or "odisha", password or "odisha"


@dataclass(frozen=True)
class DbConfig:
    """Postgres connection settings for the psycopg2 sinks."""

    jdbc_url: str
    user: str
    password: str
    connect_timeout: int = 10
    application_name: str = "odisha-spark-stream"

    @property
    def dsn(self) -> str:
        return jdbc_url_to_dsn(self.jdbc_url)


def connect_db(cfg: DbConfig) -> "psycopg2.extensions.connection":
    return psycopg2.connect(
        dsn=cfg.dsn,
        user=cfg.user or None,
        password=cfg.password or None,
        connect_timeout=cfg.connect_timeout,
        application_name=cfg.application_name,
    )


def run_with_retries(
    func: Callable[[], Any],
    attempts: int = 3,
    base_delay: float = 1.0,
    label: str = "postgres",
) -> Any:
    """Run ``func`` and retry transient connection failures with backoff."""
    last_exc: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except psycopg2.OperationalError as exc:  # connection level failure
            last_exc = exc
            if attempt >= attempts:
                raise
            delay = min(base_delay * (2 ** (attempt - 1)), 8.0)
            LOG.warning(
                "%s unavailable (attempt %s/%s): %s -- retrying in %.1fs",
                label, attempt, attempts, exc, delay,
            )
            time.sleep(delay)
    raise last_exc  # pragma: no cover - defensive


def _missing_dependency_classes() -> Tuple[type, ...]:
    classes = []
    for name in ("UndefinedTable", "InvalidSchemaName"):
        cls = getattr(pg_errors, name, None)
        if isinstance(cls, type):
            classes.append(cls)
    return tuple(classes)


_MISSING_DEPENDENCY_CLASSES = _missing_dependency_classes()
_MISSING_DEPENDENCY_SQLSTATES = {"42P01", "3F000"}


def _is_missing_dependency(exc: BaseException) -> bool:
    """True when the failure is 'table/schema does not exist yet'."""
    if getattr(exc, "pgcode", None) in _MISSING_DEPENDENCY_SQLSTATES:
        return True
    return bool(_MISSING_DEPENDENCY_CLASSES) and isinstance(exc, _MISSING_DEPENDENCY_CLASSES)


def _write_heartbeat(cfg: DbConfig, status: str, detail: Dict[str, Any]) -> None:
    payload = json.dumps(_json_safe(detail), default=str)

    def _tx() -> None:
        conn = connect_db(cfg)
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(UPSERT_HEARTBEAT, (status, payload))
        finally:
            conn.close()

    run_with_retries(_tx, attempts=2, label="heartbeat")


def safe_heartbeat(cfg: DbConfig, status: str, detail: Dict[str, Any]) -> None:
    """Best-effort heartbeat; never let observability break the stream."""
    try:
        _write_heartbeat(cfg, status, detail)
    except Exception as exc:  # noqa: BLE001 - intentionally swallowed
        LOG.warning("could not write heartbeat (%s): %s", status, exc)


@dataclass
class RuntimeState:
    """Thread-safe liveness state shared with the heartbeat keeper thread."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    batches: int = 0
    last_batch_id: Optional[int] = None
    last_batch_rows: int = 0
    last_batch_at: Optional[datetime] = None
    alert_batches: int = 0
    alert_rows: int = 0
    degraded_reason: Optional[str] = None
    last_error: Optional[str] = None

    def mark_batch(self, batch_id: int, rows: int) -> None:
        with self.lock:
            self.batches += 1
            self.last_batch_id = int(batch_id)
            self.last_batch_rows = int(rows)
            self.last_batch_at = datetime.now(timezone.utc)
            self.degraded_reason = None

    def mark_alert_batch(self, rows: int) -> None:
        with self.lock:
            self.alert_batches += 1
            self.alert_rows += int(rows)

    def mark_degraded(self, reason: str) -> None:
        with self.lock:
            self.degraded_reason = str(reason)[:500]
            self.last_batch_at = datetime.now(timezone.utc)

    def mark_error(self, reason: str) -> None:
        with self.lock:
            self.last_error = str(reason)[:500]

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            age = None
            if self.last_batch_at is not None:
                age = (datetime.now(timezone.utc) - self.last_batch_at).total_seconds()
            return {
                "batches": self.batches,
                "last_batch_id": self.last_batch_id,
                "last_batch_rows": self.last_batch_rows,
                "last_batch_age_seconds": None if age is None else round(age, 1),
                "alert_batches": self.alert_batches,
                "alert_rows": self.alert_rows,
                "degraded_reason": self.degraded_reason,
                "last_error": self.last_error,
            }


class HeartbeatKeeper(threading.Thread):
    """Fallback heartbeat so ``/api/health`` stays green on a quiet topic.

    ``foreachBatch`` also writes a heartbeat, but a low-traffic platform may go
    many trigger intervals without a data-bearing batch.  This daemon thread
    keeps ``ops.pipeline_heartbeat.last_seen`` fresh and reports ``degraded``
    when a sink is waiting for upstream tables.
    """

    def __init__(self, cfg: DbConfig, state: RuntimeState, interval: float = 20.0) -> None:
        super().__init__(name="spark-stream-heartbeat", daemon=True)
        self._cfg = cfg
        self._state = state
        self._interval = max(5.0, float(interval))
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:  # pragma: no cover - background thread
        while not self._stop.wait(self._interval):
            snapshot = self._state.snapshot()
            status = "degraded" if snapshot["degraded_reason"] else "ok"
            detail = {"stream": "keepalive", "status": status, **snapshot}
            try:
                _write_heartbeat(self._cfg, status, detail)
            except Exception as exc:  # noqa: BLE001 - keep the thread alive
                LOG.warning("keepalive heartbeat failed: %s", exc)


# ---------------------------------------------------------------------------
# Stream construction
# ---------------------------------------------------------------------------
def build_spark_session() -> SparkSession:
    return (
        SparkSession.builder.appName(APP_NAME)
        .config("spark.sql.session.timeZone", "UTC")
        .config("spark.sql.legacy.timeParserPolicy", "CORRECTED")
        .config("spark.sql.shuffle.partitions", os.getenv("SPARK_SHUFFLE_PARTITIONS", "4"))
        .config("spark.streaming.stopGracefullyOnShutdown", "true")
        .config("spark.sql.streaming.stopTimeout", os.getenv("SPARK_STOP_TIMEOUT", "30s"))
        .config("spark.sql.streaming.forceDeleteTempCheckpointLocation", "true")
        .getOrCreate()
    )


def read_kafka(
    spark: SparkSession,
    bootstrap: str,
    topic: str,
    max_offsets_per_trigger: int,
) -> DataFrame:
    """Read one Kafka topic as a stream with contract-mandated options."""
    return (
        spark.readStream.format("kafka")
        .option("kafka.bootstrap.servers", bootstrap)
        .option("subscribe", topic)
        .option("startingOffsets", "earliest")
        .option("failOnDataLoss", "false")
        .option("maxOffsetsPerTrigger", str(int(max_offsets_per_trigger)))
        .load()
    )


def build_report_windows(kafka_df: DataFrame) -> DataFrame:
    """user-reports -> 1-minute tumbling aggregates per district."""
    parsed = kafka_df.select(
        F.from_json(F.col("value").cast("string"), USER_REPORT_SCHEMA).alias("report"),
        F.col("timestamp").alias("kafka_timestamp"),
    ).select("report.*", "kafka_timestamp")

    event_ts = F.coalesce(_parse_timestamp("reported_at"), F.col("kafka_timestamp"))
    reporters = F.when(
        F.col("reporter").isNull() | (F.trim(F.col("reporter")) == ""),
        F.lit(None),
    ).otherwise(F.trim(F.col("reporter")))

    reports = (
        parsed.withColumn("event_ts", event_ts)
        .withColumn("district", F.trim(F.col("district")))
        .withColumn("severity", F.coalesce(F.col("severity").cast("int"), F.lit(3)))
        .filter(F.col("event_ts").isNotNull())
        .filter(F.col("district").isNotNull() & (F.col("district") != ""))
        .withWatermark("event_ts", WATERMARK)
    )

    windowed = (
        reports.groupBy(
            F.window(F.col("event_ts"), REPORT_WINDOW).alias("window"),
            F.col("district"),
        )
        .agg(
            F.count(F.lit(1)).alias("report_count"),
            F.approx_count_distinct(reporters, 0.05).alias("unique_reporters"),
            F.avg(F.col("severity")).alias("avg_severity"),
        )
        .select(
            F.col("window.start").cast("long").alias("window_start_epoch"),
            F.col("window.end").cast("long").alias("window_end_epoch"),
            F.col("district").cast("string").alias("district"),
            F.col("report_count").cast("int").alias("report_count"),
            F.col("unique_reporters").cast("int").alias("unique_reporters"),
            F.col("avg_severity").cast("float").alias("avg_severity"),
        )
    )
    return windowed


def build_alerts(kafka_df: DataFrame) -> DataFrame:
    """outage-alerts -> typed rows ready for the realtime.alerts sink."""
    parsed = kafka_df.select(
        F.from_json(F.col("value").cast("string"), OUTAGE_ALERT_SCHEMA).alias("alert"),
        F.col("timestamp").alias("kafka_timestamp"),
    ).select("alert.*", "kafka_timestamp")

    alerts = (
        parsed.withColumn("alert_id", F.trim(F.col("alert_id")))
        .withColumn("district", F.trim(F.col("district")))
        .withColumn("alert_type", F.trim(F.col("alert_type")))
        .withColumn(
            "severity",
            F.when(
                F.col("severity").isNull() | (F.trim(F.col("severity")) == ""),
                F.lit("info"),
            ).otherwise(F.lower(F.trim(F.col("severity")))),
        )
        .withColumn("message", F.coalesce(F.col("message"), F.lit("")))
        .withColumn("report_count", F.col("report_count").cast("int"))
        .withColumn("window_start_epoch", _parse_timestamp("window_start").cast("long"))
        .withColumn("window_end_epoch", _parse_timestamp("window_end").cast("long"))
        .filter(F.col("alert_id").isNotNull() & (F.col("alert_id") != ""))
        .filter(F.col("alert_type").isNotNull() & (F.col("alert_type") != ""))
        .select(
            "alert_id",
            "district",
            "alert_type",
            "severity",
            "message",
            "report_count",
            "window_start_epoch",
            "window_end_epoch",
        )
    )
    return alerts


# ---------------------------------------------------------------------------
# Sinks
# ---------------------------------------------------------------------------
def _report_minute_rows(batch_df: DataFrame) -> Tuple[List[Tuple[Any, ...]], Dict[str, int]]:
    """Extract (bucket, district, count, avg_severity) + max(unique) per district."""
    minute_rows: List[Tuple[Any, ...]] = []
    unique_by_district: Dict[str, int] = {}
    if batch_df is None:
        return minute_rows, unique_by_district

    for row in batch_df.collect():
        district = row["district"]
        if district is None:
            continue
        district = str(district)
        bucket = _epoch_to_datetime(row["window_start_epoch"])
        count = int(row["report_count"] or 0)
        if bucket is None or count <= 0:
            continue
        severity = row["avg_severity"]
        avg_severity = float(severity) if severity is not None else None
        minute_rows.append((bucket, district, count, avg_severity))

        unique = row["unique_reporters"]
        unique_value = int(unique) if unique is not None else 0
        previous = unique_by_district.get(district)
        unique_by_district[district] = (
            unique_value if previous is None else max(previous, unique_value)
        )
    return minute_rows, unique_by_district


def _apply_report_sink(
    cfg: DbConfig,
    minute_rows: List[Tuple[Any, ...]],
    unique_by_district: Dict[str, int],
    detail: Dict[str, Any],
) -> None:
    """Single Postgres transaction: report_minute + district_live_metrics + heartbeat."""
    unique_json = json.dumps(unique_by_district, default=str)
    heartbeat_detail = json.dumps(_json_safe(detail), default=str)

    def _tx() -> None:
        conn = connect_db(cfg)
        try:
            with conn:
                with conn.cursor() as cur:
                    if minute_rows:
                        execute_values(
                            cur, UPSERT_REPORT_MINUTE, minute_rows, page_size=1000
                        )
                    cur.execute(UPSERT_DISTRICT_LIVE_METRICS, {"unique_json": unique_json})
                    cur.execute(ZERO_QUIET_DISTRICTS)
                    cur.execute(PRUNE_REPORT_MINUTE)
                    cur.execute(UPSERT_HEARTBEAT, ("ok", heartbeat_detail))
        finally:
            conn.close()

    try:
        run_with_retries(_tx, attempts=3, label="report sink")
    except Exception as exc:  # noqa: BLE001 - classify before re-raising
        if _is_missing_dependency(exc):
            raise MissingDependencyError(str(exc)) from exc
        raise


def _alert_rows(batch_df: DataFrame) -> List[Tuple[Any, ...]]:
    rows: List[Tuple[Any, ...]] = []
    seen = set()
    if batch_df is None:
        return rows

    for row in batch_df.collect():
        alert_id = row["alert_id"]
        alert_type = row["alert_type"]
        if not alert_id or not alert_type:
            continue
        alert_id = str(alert_id).strip()
        if not alert_id or alert_id in seen:
            continue
        seen.add(alert_id)
        severity = row["severity"] or "info"
        message = row["message"] or ""
        rows.append(
            (
                alert_id,
                row["district"],
                str(alert_type),
                str(severity),
                str(message),
                row["report_count"],
                _epoch_to_datetime(row["window_start_epoch"]),
                _epoch_to_datetime(row["window_end_epoch"]),
                "flink",
            )
        )
    return rows


def _apply_alert_sink(
    cfg: DbConfig,
    alert_rows: List[Tuple[Any, ...]],
    detail: Dict[str, Any],
) -> None:
    heartbeat_detail = json.dumps(_json_safe(detail), default=str)

    def _tx() -> None:
        conn = connect_db(cfg)
        try:
            with conn:
                with conn.cursor() as cur:
                    if alert_rows:
                        execute_values(cur, INSERT_ALERTS, alert_rows, page_size=500)
                    cur.execute(UPSERT_HEARTBEAT, ("ok", heartbeat_detail))
        finally:
            conn.close()

    try:
        run_with_retries(_tx, attempts=3, label="alert sink")
    except Exception as exc:  # noqa: BLE001 - classify before re-raising
        if _is_missing_dependency(exc):
            raise MissingDependencyError(str(exc)) from exc
        raise


def make_report_batch_handler(
    cfg: DbConfig, state: RuntimeState
) -> Callable[[DataFrame, int], None]:
    """foreachBatch handler for the 1-minute district report windows."""

    def _handle(batch_df: DataFrame, batch_id: int) -> None:
        started = time.monotonic()
        minute_rows, unique_by_district = _report_minute_rows(batch_df)
        detail = {
            "stream": "user-reports",
            "batch_id": int(batch_id),
            "rows": len(minute_rows),
            "minute_rows": len(minute_rows),
            "districts": len(unique_by_district),
            "collect_ms": int((time.monotonic() - started) * 1000),
        }
        LOG.info(
            "report batch %s: %s minute rows, %s districts",
            batch_id, len(minute_rows), len(unique_by_district),
        )
        try:
            _apply_report_sink(cfg, minute_rows, unique_by_district, detail)
        except MissingDependencyError as exc:
            LOG.warning(
                "report sink waiting for upstream tables (batch %s): %s", batch_id, exc
            )
            state.mark_degraded(str(exc))
            safe_heartbeat(cfg, "degraded", {**detail, "error": str(exc)[:400]})
            return
        except Exception as exc:  # noqa: BLE001 - then re-raise to stop the query
            LOG.exception("report sink failed on batch %s", batch_id)
            state.mark_error(str(exc))
            safe_heartbeat(
                cfg, "error", {**detail, "error": "report sink failed: " + str(exc)[:400]}
            )
            raise
        state.mark_batch(int(batch_id), len(minute_rows))

    return _handle


def make_alert_batch_handler(
    cfg: DbConfig, state: RuntimeState
) -> Callable[[DataFrame, int], None]:
    """foreachBatch handler for outage-alerts."""

    def _handle(batch_df: DataFrame, batch_id: int) -> None:
        alert_rows = _alert_rows(batch_df)
        detail = {
            "stream": "outage-alerts",
            "batch_id": int(batch_id),
            "rows": len(alert_rows),
        }
        LOG.info("alert batch %s: %s rows", batch_id, len(alert_rows))
        try:
            _apply_alert_sink(cfg, alert_rows, detail)
        except MissingDependencyError as exc:
            LOG.warning(
                "alert sink waiting for realtime.alerts (batch %s): %s", batch_id, exc
            )
            state.mark_degraded(str(exc))
            safe_heartbeat(cfg, "degraded", {**detail, "error": str(exc)[:400]})
            return
        except Exception as exc:  # noqa: BLE001 - then re-raise to stop the query
            LOG.exception("alert sink failed on batch %s", batch_id)
            state.mark_error(str(exc))
            safe_heartbeat(
                cfg, "error", {**detail, "error": "alert sink failed: " + str(exc)[:400]}
            )
            raise
        state.mark_alert_batch(len(alert_rows))

    return _handle


# ---------------------------------------------------------------------------
# CLI / main
# ---------------------------------------------------------------------------
def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Spark Structured Streaming job that sinks NRT user-report and "
            "outage-alert metrics into Postgres (realtime/ops schemas)."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--bootstrap",
        default=os.getenv("KAFKA_BOOTSTRAP", DEFAULT_BOOTSTRAP),
        help="Kafka bootstrap servers (env KAFKA_BOOTSTRAP)",
    )
    parser.add_argument(
        "--jdbc-url",
        default=default_jdbc_url(),
        help="Postgres JDBC url (env SPARK_JDBC_URL/JDBC_URL/DATABASE_URL/POSTGRES_*)",
    )
    parser.add_argument(
        "--jdbc-user",
        default=default_credentials()[0],
        help="Postgres user (env SPARK_JDBC_USER/POSTGRES_USER)",
    )
    parser.add_argument(
        "--jdbc-password",
        default=default_credentials()[1],
        help="Postgres password (env SPARK_JDBC_PASSWORD/POSTGRES_PASSWORD)",
    )
    parser.add_argument(
        "--checkpoint-dir",
        default=os.getenv("SPARK_CHECKPOINT_DIR", DEFAULT_CHECKPOINT_DIR),
        help="root checkpoint dir; one subdirectory per query (env SPARK_CHECKPOINT_DIR)",
    )
    parser.add_argument(
        "--trigger",
        default=os.getenv("SPARK_TRIGGER_INTERVAL", DEFAULT_TRIGGER_INTERVAL),
        help="processingTime trigger interval (env SPARK_TRIGGER_INTERVAL)",
    )
    parser.add_argument(
        "--max-offsets-per-trigger",
        type=_positive_int,
        default=_positive_int(
            os.getenv(
                "SPARK_MAX_OFFSETS_PER_TRIGGER",
                str(DEFAULT_MAX_OFFSETS_PER_TRIGGER),
            )
        ),
        help="Kafka maxOffsetsPerTrigger (env SPARK_MAX_OFFSETS_PER_TRIGGER)",
    )
    parser.add_argument(
        "--topic-reports",
        default=os.getenv("SPARK_KAFKA_TOPIC_REPORTS", DEFAULT_TOPIC_REPORTS),
        help="Kafka user-reports topic",
    )
    parser.add_argument(
        "--topic-alerts",
        default=os.getenv("SPARK_KAFKA_TOPIC_ALERTS", DEFAULT_TOPIC_ALERTS),
        help="Kafka outage-alerts topic",
    )
    parser.add_argument(
        "--heartbeat-interval",
        type=float,
        default=float(os.getenv("SPARK_HEARTBEAT_INTERVAL", "20")),
        help="keepalive heartbeat interval in seconds",
    )
    parser.add_argument(
        "--spark-log-level",
        default=os.getenv("SPARK_LOG_LEVEL", "WARN"),
        help="Spark JVM log level (WARN/INFO/ERROR)",
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("SPARK_PY_LOG_LEVEL", "INFO"),
        help="Python logging level",
    )
    return parser.parse_args(argv)


def _ensure_dir(path: str) -> None:
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        LOG.warning("could not create checkpoint dir %s: %s", path, exc)


def _describe_queries(spark: SparkSession) -> None:
    for query in spark.streams.active:
        LOG.info(
            "streaming query '%s' (id=%s) status=%s", query.name, query.id, query.status
        )


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    spark = build_spark_session()
    spark.sparkContext.setLogLevel(str(args.spark_log_level).upper())
    LOG.info("starting %s", APP_NAME)
    LOG.info("stream graph: kafka(%s) -> watermark %s -> window %s per district",
             args.topic_reports, WATERMARK, REPORT_WINDOW)
    LOG.info("stream graph: kafka(%s) -> realtime.alerts", args.topic_alerts)
    LOG.info("checkpoint root: %s", args.checkpoint_dir)

    cfg = DbConfig(
        jdbc_url=args.jdbc_url,
        user=args.jdbc_user,
        password=args.jdbc_password,
    )
    state = RuntimeState()
    keeper = HeartbeatKeeper(cfg, state, interval=args.heartbeat_interval)
    stop_event = threading.Event()
    exit_code = 0

    def _handle_signal(signum: int, _frame: Any) -> None:
        if stop_event.is_set():
            return
        LOG.warning("received signal %s -- stopping streaming queries", signum)
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError, RuntimeError):  # not in the main thread
            LOG.debug("could not install handler for %s", sig)

    report_query = None
    alert_query = None
    try:
        _ensure_dir(args.checkpoint_dir)
        report_kafka = read_kafka(
            spark, args.bootstrap, args.topic_reports, args.max_offsets_per_trigger
        )
        report_windows = build_report_windows(report_kafka)
        report_query = (
            report_windows.writeStream.outputMode("append")
            .foreachBatch(make_report_batch_handler(cfg, state))
            .option("checkpointLocation", os.path.join(args.checkpoint_dir, "user-reports"))
            .trigger(processingTime=args.trigger)
            .queryName("odisha-user-report-minute-windows")
            .start()
        )
        LOG.info("started report query id=%s", report_query.id)

        alert_kafka = read_kafka(
            spark, args.bootstrap, args.topic_alerts, args.max_offsets_per_trigger
        )
        alert_stream = build_alerts(alert_kafka)
        alert_query = (
            alert_stream.writeStream.outputMode("append")
            .foreachBatch(make_alert_batch_handler(cfg, state))
            .option("checkpointLocation", os.path.join(args.checkpoint_dir, "outage-alerts"))
            .trigger(processingTime=args.trigger)
            .queryName("odisha-outage-alerts")
            .start()
        )
        LOG.info("started alert query id=%s", alert_query.id)

        _describe_queries(spark)
        keeper.start()
        safe_heartbeat(cfg, "ok", {"stream": "startup", "status": "ok"})

        while not stop_event.is_set():
            try:
                finished = spark.streams.awaitAnyTermination(timeout=1000)
            except Exception as exc:  # noqa: BLE001 - query failed with an exception
                LOG.error("streaming query terminated with an error: %s", exc)
                exit_code = 1
                break
            if finished:
                if stop_event.is_set():
                    LOG.info("streaming query stopped after shutdown request")
                else:
                    LOG.error("a streaming query stopped without being asked to")
                    exit_code = 1
                break

        if stop_event.is_set():
            LOG.info("shutdown requested -- stopping queries gracefully")
    except KeyboardInterrupt:
        LOG.warning("interrupted (KeyboardInterrupt) -- stopping streaming queries")
        stop_event.set()
    except Exception:
        LOG.exception("streaming job failed during startup")
        exit_code = 1
    finally:
        keeper.stop()
        for query in (report_query, alert_query):
            if query is None:
                continue
            try:
                if query.isActive:
                    query.stop()
            except Exception:  # noqa: BLE001 - best effort shutdown
                LOG.exception("error stopping query %s", getattr(query, "name", "?"))
        for query in (report_query, alert_query):
            if query is None:
                continue
            try:
                query_exc = query.exception()
            except Exception:  # noqa: BLE001
                continue
            if query_exc is not None:
                LOG.error("query '%s' failed: %s", query.name, query_exc)
                exit_code = 1
            else:
                LOG.info("query '%s' stopped", query.name)
        if stop_event.is_set():
            safe_heartbeat(
                cfg,
                "stopped",
                {"stream": "shutdown", "reason": "signal", **state.snapshot()},
            )
        elif exit_code != 0:
            safe_heartbeat(
                cfg, "error", {"stream": "shutdown", **state.snapshot()},
            )
        try:
            spark.stop()
        except Exception:  # noqa: BLE001
            LOG.exception("error stopping SparkSession")

    if exit_code == 0:
        LOG.info("streaming job stopped cleanly")
    else:
        LOG.error("streaming job stopped with errors (exit_code=%s)", exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
