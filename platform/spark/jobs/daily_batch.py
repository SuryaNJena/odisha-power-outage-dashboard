#!/usr/bin/env python3
"""Odisha power-outage platform -- daily Spark batch job (ML + archive).

Reads the dbt-owned tables from Postgres over JDBC, computes the daily ML
outputs required by ``platform/CONTRACT.md`` and archives the day's outage
events as Parquet.

What it produces
----------------
``ml.outage_clusters``
    KMeans (k=8, seed=42) over outage point features ``(lat, lon, local_hour)``.
    Rows: ``cluster_id, lat, lon, outage_count, dominant_district,
    dominant_hour``.  Replaced in full inside one transaction.

``ml.etr_model_metrics``
    LinearRegression of ``duration_minutes`` on
    ``(voltage_class, is_planned, local_hour, district_index)``.
    Row: ``model_date, mae_minutes, r2, sample_count`` (evaluated with
    ``RegressionEvaluator`` on a held-out split when there is enough data).

``ml.district_risk_forecast``
    Next-24h district risk: latest weather forecast per district combined with
    the 30-day outage count.  Row: ``district, forecast_date, risk_score,
    risk_level, expected_outages, max_precip_mm, max_wind_kph``.

Parquet archive
    ``<archive-dir>/dt=YYYY-MM-DD/`` (default ``/opt/odisha/data/lake/outages``)
    written in ``overwrite`` mode for the archived day.

``ops.job_runs``
    One row per run: ``job_name='spark-daily'``, ``run_id`` (date by default),
    ``status``, ``started_at``/``finished_at`` and a JSON ``detail`` document.

Resilience
----------
Every source read is best effort: a missing/empty/unreadable table is logged
and its dependent step is skipped, but the job still writes the ``ops.job_runs``
row and exits ``0`` when the sources are merely empty.  Invalid/persist-level
errors exit non-zero after writing a ``failed`` row.

Run it with ``spark-submit`` (never ``python``); see ``platform/spark/README.md``.

Environment defaults (all overridable by CLI flags)
---------------------------------------------------
``DATABASE_URL`` / ``POSTGRES_HOST`` / ``POSTGRES_PORT`` / ``POSTGRES_DB``
``POSTGRES_USER`` / ``POSTGRES_PASSWORD``
``SPARK_JDBC_URL`` / ``SPARK_JDBC_USER`` / ``SPARK_JDBC_PASSWORD``
``SPARK_LAKE_DIR``       default ``/opt/odisha/data/lake/outages``
``SPARK_RUN_DATE``       default: today (UTC) when the job starts
``SPARK_KAFKA_BOOTSTRAP`` accepted for CLI parity with the streaming job
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

import psycopg2
from psycopg2.extras import execute_values
from pyspark.ml.clustering import KMeans
from pyspark.ml.evaluation import RegressionEvaluator
from pyspark.ml.feature import StringIndexer, VectorAssembler
from pyspark.ml.regression import LinearRegression
from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F
from pyspark.sql.window import Window

LOG = logging.getLogger("odisha.spark.daily")

APP_NAME = "odisha-spark-daily-batch"
JOB_NAME = "spark-daily"
LOCAL_TIMEZONE = "Asia/Kolkata"  # display/business timezone from the contract
FORECAST_HOURS = 24
LOOKBACK_DAYS = 30
CLUSTER_K = 8
RANDOM_SEED = 42
#: Shared data volume root: Airflow mounts ``odisha_app_data`` at ``DATA_DIR``
#: (``/opt/odisha/data``) and the Go app mounts the same volume at
#: ``/opt/odisha-data``; ``SPARK_LAKE_DIR`` overrides this.
DEFAULT_ARCHIVE_DIR = os.path.join(
    os.getenv("DATA_DIR", "/opt/odisha/data"), "lake", "outages"
)

# Required source columns (minimal sets so small schema drift does not kill the
# job; unknown extra columns are ignored).
FCT_REQUIRED = ("district", "start_ts", "duration_minutes", "is_planned", "voltage")
DIM_REQUIRED = ("district", "lat", "lon")
WEATHER_REQUIRED = ("district", "observed_at", "precipitation_mm", "wind_kph")
RELIABILITY_REQUIRED = ("district", "metric_date", "outage_count")

# ---------------------------------------------------------------------------
# SQL (schema-qualified, matches platform/CONTRACT.md)
# ---------------------------------------------------------------------------
UPSERT_JOB_RUN = """
insert into ops.job_runs (job_name, run_id, status, started_at, finished_at, detail)
values (%s, %s, %s, %s, %s, %s::jsonb)
on conflict (job_name, run_id) do update
set status = excluded.status,
    started_at = coalesce(ops.job_runs.started_at, excluded.started_at),
    finished_at = excluded.finished_at,
    detail = excluded.detail
"""

DELETE_OUTAGE_CLUSTERS = "delete from ml.outage_clusters"

INSERT_OUTAGE_CLUSTERS = """
insert into ml.outage_clusters
    (cluster_id, lat, lon, outage_count, dominant_district, dominant_hour)
values %s
"""

DELETE_ETR_FOR_DATE = "delete from ml.etr_model_metrics where model_date = %s"

INSERT_ETR = """
insert into ml.etr_model_metrics (model_date, mae_minutes, r2, sample_count)
values (%s, %s, %s, %s)
"""

DELETE_RISK_FOR_DATE = "delete from ml.district_risk_forecast where forecast_date = %s"

INSERT_RISK = """
insert into ml.district_risk_forecast
    (district, forecast_date, risk_score, risk_level, expected_outages,
     max_precip_mm, max_wind_kph)
values %s
"""


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------
def _json_safe(value: Any) -> Any:
    """Recursively make ``value`` JSON serialisable (NaN/Inf -> None)."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


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
    _host, _port, _db, url_user, url_password = _database_url_parts()
    user = os.getenv("SPARK_JDBC_USER") or os.getenv("POSTGRES_USER") or url_user
    password = os.getenv("SPARK_JDBC_PASSWORD")
    if password is None:
        password = os.getenv("POSTGRES_PASSWORD")
    if password is None:
        password = url_password
    return user or "odisha", password or "odisha"


@dataclass(frozen=True)
class DbConfig:
    """Postgres connection settings for the psycopg2 sinks and JDBC reads."""

    jdbc_url: str
    user: str
    password: str
    connect_timeout: int = 10
    application_name: str = "odisha-spark-daily"

    @property
    def dsn(self) -> str:
        return jdbc_url_to_dsn(self.jdbc_url)

    def jdbc_properties(self) -> Dict[str, str]:
        return {
            "user": self.user,
            "password": self.password,
            "driver": "org.postgresql.Driver",
            "fetchsize": "5000",
            "connectTimeout": "10",
        }


def connect_db(cfg: DbConfig) -> "psycopg2.extensions.connection":
    return psycopg2.connect(
        dsn=cfg.dsn,
        user=cfg.user or None,
        password=cfg.password or None,
        connect_timeout=cfg.connect_timeout,
        application_name=cfg.application_name,
    )


def run_with_retries(
    func: Any,
    attempts: int = 3,
    base_delay: float = 1.0,
    label: str = "postgres",
) -> Any:
    last_exc: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except psycopg2.OperationalError as exc:
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


# ---------------------------------------------------------------------------
# Spark / JDBC readers
# ---------------------------------------------------------------------------
def build_spark_session() -> SparkSession:
    return (
        SparkSession.builder.appName(APP_NAME)
        .config("spark.sql.session.timeZone", "UTC")
        .config("spark.sql.shuffle.partitions", os.getenv("SPARK_SHUFFLE_PARTITIONS", "4"))
        .config("spark.sql.adaptive.enabled", "true")
        .getOrCreate()
    )


@dataclass
class LoadedTable:
    """Result of a best-effort JDBC read."""

    name: str
    df: Optional[DataFrame] = None
    row_count: int = 0
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.df is not None

    def unpersist(self) -> None:
        if self.df is not None:
            try:
                self.df.unpersist()
            except Exception:  # noqa: BLE001 - shutdown best effort
                pass


def read_table(
    spark: SparkSession,
    cfg: DbConfig,
    table: str,
    required_columns: Tuple[str, ...],
    problems: List[str],
) -> LoadedTable:
    """Read a Postgres table over JDBC; degrade gracefully on any failure."""
    try:
        df = spark.read.jdbc(
            url=cfg.jdbc_url, table=table, properties=cfg.jdbc_properties()
        )
    except Exception as exc:  # noqa: BLE001 - missing table / dbt not run yet
        message = "{}: read failed: {}".format(table, exc)
        LOG.warning(message)
        problems.append(message)
        return LoadedTable(table, None, 0, str(exc))

    missing = [column for column in required_columns if column not in df.columns]
    if missing:
        message = "{}: missing required columns {}".format(table, missing)
        LOG.warning(message)
        problems.append(message)
        return LoadedTable(table, None, 0, message)

    try:
        df = df.cache()
        row_count = int(df.count())
    except Exception as exc:  # noqa: BLE001 - reading the contents failed
        message = "{}: count failed: {}".format(table, exc)
        LOG.warning(message)
        problems.append(message)
        return LoadedTable(table, None, 0, str(exc))

    if row_count == 0:
        LOG.info("%s is empty -- dependent steps will be skipped", table)
    else:
        LOG.info("%s loaded: %s rows", table, row_count)
    return LoadedTable(table, df, row_count)


def _local_hour(column: str) -> F.Column:
    """Hour of day in Asia/Kolkata from a UTC timestamp column."""
    return F.hour(F.from_utc_timestamp(F.col(column), LOCAL_TIMEZONE))


# ---------------------------------------------------------------------------
# ML step 1 -- KMeans outage clusters
# ---------------------------------------------------------------------------
def compute_outage_clusters(
    events: Optional[DataFrame],
    dim_district: Optional[DataFrame],
    k: int = CLUSTER_K,
    seed: int = RANDOM_SEED,
) -> Optional[List[Tuple[Any, ...]]]:
    """KMeans over (lat, lon, local_hour) -> rows for ml.outage_clusters."""
    if events is None or dim_district is None:
        LOG.info("skipping outage clusters (missing fct_outage_events/dim_district)")
        return None

    events_with_hour = (
        events.select(
            F.col("district").cast("string").alias("district"),
            _local_hour("start_ts").alias("hour"),
        )
        .filter(F.col("district").isNotNull() & F.col("hour").isNotNull())
        .filter(F.col("district") != "")
    )
    geo = (
        dim_district.select(
            F.col("district").cast("string").alias("district"),
            F.col("lat").cast("double").alias("lat"),
            F.col("lon").cast("double").alias("lon"),
        )
        .filter(F.col("lat").isNotNull() & F.col("lon").isNotNull())
        .filter(F.col("district").isNotNull() & (F.col("district") != ""))
    )
    points = events_with_hour.join(geo, on="district", how="inner")
    sample_count = int(points.count())
    if sample_count == 0:
        LOG.info("skipping outage clusters: no geocoded outage events")
        return None
    if sample_count < k:
        LOG.warning(
            "skipping outage clusters: only %s geocoded events for k=%s",
            sample_count, k,
        )
        return None

    assembler = VectorAssembler(
        inputCols=["lat", "lon", "hour"], outputCol="features"
    )
    features = assembler.transform(points).select(
        "features", "district", "lat", "lon", "hour"
    )
    model = KMeans(
        featuresCol="features",
        predictionCol="cluster_id",
        k=int(k),
        seed=int(seed),
        maxIter=50,
    ).fit(features)
    labeled = (
        model.transform(features)
        .select(
            F.col("cluster_id").cast("int").alias("cluster_id"),
            F.col("district").cast("string").alias("district"),
            F.col("lat").cast("double").alias("lat"),
            F.col("lon").cast("double").alias("lon"),
            F.col("hour").cast("int").alias("hour"),
        )
        .cache()
    )
    try:
        counts = labeled.groupBy("cluster_id").agg(
            F.count(F.lit(1)).cast("int").alias("outage_count"),
            F.avg("lat").cast("double").alias("lat"),
            F.avg("lon").cast("double").alias("lon"),
        )
        dominant_district = (
            labeled.groupBy("cluster_id", "district")
            .agg(F.count(F.lit(1)).alias("district_count"))
            .withColumn(
                "rn",
                F.row_number().over(
                    Window.partitionBy("cluster_id").orderBy(
                        F.col("district_count").desc(), F.col("district").asc()
                    )
                ),
            )
            .filter(F.col("rn") == 1)
            .select("cluster_id", F.col("district").alias("dominant_district"))
        )
        dominant_hour = (
            labeled.groupBy("cluster_id", "hour")
            .agg(F.count(F.lit(1)).alias("hour_count"))
            .withColumn(
                "rn",
                F.row_number().over(
                    Window.partitionBy("cluster_id").orderBy(
                        F.col("hour_count").desc(), F.col("hour").asc()
                    )
                ),
            )
            .filter(F.col("rn") == 1)
            .select(
                "cluster_id",
                F.col("hour").cast("int").alias("dominant_hour"),
            )
        )
        result = (
            counts.join(dominant_district, "cluster_id", "left")
            .join(dominant_hour, "cluster_id", "left")
            .orderBy("cluster_id")
        )
        rows: List[Tuple[Any, ...]] = []
        for row in result.collect():
            rows.append(
                (
                    int(row["cluster_id"]),
                    float(row["lat"]),
                    float(row["lon"]),
                    int(row["outage_count"]),
                    row["dominant_district"],
                    int(row["dominant_hour"])
                    if row["dominant_hour"] is not None
                    else None,
                )
            )
        LOG.info("KMeans produced %s outage clusters from %s points", len(rows), sample_count)
        return rows
    finally:
        labeled.unpersist()


# ---------------------------------------------------------------------------
# ML step 2 -- LinearRegression ETR metrics
# ---------------------------------------------------------------------------
def compute_etr_metrics(
    events: Optional[DataFrame],
) -> Optional[Tuple[float, float, int]]:
    """Regress duration_minutes -> (mae_minutes, r2, sample_count)."""
    if events is None:
        LOG.info("skipping ETR model (missing fct_outage_events)")
        return None

    base = events.select(
        F.col("duration_minutes").cast("double").alias("duration_minutes"),
        F.col("is_planned"),
        _local_hour("start_ts").alias("hour"),
        F.regexp_extract(
            F.coalesce(F.col("voltage").cast("string"), F.lit("")),
            r"(?i)(\d+)\s*kv",
            1,
        )
        .cast("int")
        .alias("voltage_kv"),
        F.col("district").cast("string").alias("district"),
    ).filter(
        F.col("duration_minutes").isNotNull()
        & (F.col("duration_minutes") >= 0)
        & F.col("district").isNotNull()
        & (F.col("district") != "")
    )
    base = (
        base.withColumn(
            "voltage_class",
            F.when(F.col("voltage_kv").isNull(), F.lit(0))
            .when(F.col("voltage_kv") <= 11, F.lit(1))
            .when(F.col("voltage_kv") <= 33, F.lit(2))
            .otherwise(F.lit(3)),
        )
        .withColumn(
            "planned_flag",
            F.coalesce(F.col("is_planned").cast("int"), F.lit(0)),
        )
        .withColumn(
            "hour_num",
            F.coalesce(F.col("hour").cast("int"), F.lit(0)),
        )
    )
    sample_count = int(base.count())
    if sample_count < 2:
        LOG.info(
            "skipping ETR model: only %s labelled outage events", sample_count
        )
        return None

    indexer = StringIndexer(
        inputCol="district", outputCol="district_idx", handleInvalid="keep"
    )
    indexed = indexer.fit(base).transform(base)
    assembler = VectorAssembler(
        inputCols=["voltage_class", "planned_flag", "hour_num", "district_idx"],
        outputCol="features",
    )
    assembled = (
        assembler.transform(indexed)
        .select(F.col("features"), F.col("duration_minutes").alias("label"))
        .cache()
    )
    try:
        total = int(assembled.count())
        train, test = (
            assembled.randomSplit([0.8, 0.2], seed=RANDOM_SEED)
            if total >= 10
            else (assembled, assembled)
        )
        if int(train.count()) == 0:
            train = assembled
        if int(test.count()) == 0:
            test = assembled
        model = LinearRegression(
            featuresCol="features",
            labelCol="label",
            predictionCol="prediction",
            maxIter=100,
            regParam=0.1,
            elasticNetParam=0.0,
        ).fit(train)
        predictions = model.transform(test)
        mae = float(
            RegressionEvaluator(
                labelCol="label", predictionCol="prediction", metricName="mae"
            ).evaluate(predictions)
        )
        r2 = float(
            RegressionEvaluator(
                labelCol="label", predictionCol="prediction", metricName="r2"
            ).evaluate(predictions)
        )
        if not math.isfinite(mae) or not math.isfinite(r2):
            LOG.warning(
                "ETR metrics are not finite (mae=%s r2=%s) -- skipping", mae, r2
            )
            return None
        LOG.info(
            "ETR model: mae=%.3f r2=%.4f samples=%s", mae, r2, total
        )
        return (round(mae, 4), round(r2, 6), int(total))
    finally:
        assembled.unpersist()


# ---------------------------------------------------------------------------
# ML step 3 -- next-24h district risk forecast
# ---------------------------------------------------------------------------
def _weather_features(weather: DataFrame) -> Optional[DataFrame]:
    """Latest forecast fetch per district -> max precip/wind for next 24h."""
    prepared = weather
    if "fetched_at" not in prepared.columns:
        prepared = prepared.withColumn("fetched_at", F.col("observed_at"))
    prepared = (
        prepared.select(
            F.col("district").cast("string").alias("district"),
            F.col("observed_at").cast("timestamp").alias("observed_at"),
            F.coalesce(
                F.col("precipitation_mm").cast("double"), F.lit(0.0)
            ).alias("precipitation_mm"),
            F.coalesce(F.col("wind_kph").cast("double"), F.lit(0.0)).alias(
                "wind_kph"
            ),
            F.col("temperature_c").cast("double").alias("temperature_c"),
            F.coalesce(F.col("fetched_at"), F.col("observed_at"))
            .cast("timestamp")
            .alias("fetched_at"),
        )
        .filter(F.col("district").isNotNull() & (F.col("district") != ""))
        .filter(F.col("observed_at").isNotNull())
    )
    latest_fetch = prepared.groupBy("district").agg(
        F.max("fetched_at").alias("latest_fetched_at")
    )
    current = prepared.join(latest_fetch, on="district", how="inner").filter(
        F.col("fetched_at") == F.col("latest_fetched_at")
    )
    horizon_start = F.current_timestamp()
    horizon_end = F.current_timestamp() + F.expr(
        "INTERVAL {} HOURS".format(FORECAST_HOURS)
    )
    future = current.filter(
        (F.col("observed_at") >= horizon_start)
        & (F.col("observed_at") < horizon_end)
    )

    def _aggregate(frame: DataFrame) -> DataFrame:
        return frame.groupBy("district").agg(
            F.round(F.max("precipitation_mm"), 2).cast("float").alias("max_precip_mm"),
            F.round(F.max("wind_kph"), 2).cast("float").alias("max_wind_kph"),
            F.round(F.avg("temperature_c"), 2).cast("float").alias("mean_temp_c"),
            F.count(F.lit(1)).cast("int").alias("forecast_hours"),
        )

    future_agg = _aggregate(future)
    future_districts = future_agg.select("district")
    missing_districts = (
        current.select("district")
        .distinct()
        .join(future_districts, on="district", how="left_anti")
    )
    fallback_agg = _aggregate(current.join(missing_districts, on="district", how="inner"))
    weather_agg = future_agg.unionByName(fallback_agg)
    if int(weather_agg.count()) == 0:
        return None
    return weather_agg


def compute_risk_forecast(
    events: Optional[DataFrame],
    dim_district: Optional[DataFrame],
    weather: Optional[DataFrame],
    reliability: Optional[DataFrame],
    forecast_date: date,
) -> Optional[List[Tuple[Any, ...]]]:
    """Next-24h district risk rows for ml.district_risk_forecast."""
    weather_agg = _weather_features(weather) if weather is not None else None
    if weather_agg is None:
        LOG.info("no usable weather forecast -- risk uses outage history only")

    outage_counts: Optional[DataFrame] = None
    if events is not None:
        cutoff = F.current_timestamp() - F.expr(
            "INTERVAL {} DAYS".format(LOOKBACK_DAYS)
        )
        recent = events.filter(
            F.col("start_ts").isNotNull()
            & (F.col("start_ts").cast("timestamp") >= cutoff)
        )
        outage_counts = recent.groupBy(
            F.col("district").cast("string").alias("district")
        ).agg(F.count(F.lit(1)).cast("int").alias("outage_30d"))
        if int(outage_counts.count()) == 0:
            outage_counts = None
    if outage_counts is None and reliability is not None:
        LOG.info("no recent outage events -- falling back to reliability_daily")
        cutoff_date = F.date_sub(F.current_date(), LOOKBACK_DAYS)
        recent_rel = reliability.filter(
            F.col("metric_date").cast("date") >= cutoff_date
        )
        outage_counts = recent_rel.groupBy(
            F.col("district").cast("string").alias("district")
        ).agg(
            F.coalesce(F.sum("outage_count"), F.lit(0)).cast("int").alias("outage_30d")
        )
        if int(outage_counts.count()) == 0:
            outage_counts = None

    if weather_agg is None and outage_counts is None:
        LOG.info(
            "skipping risk forecast: no weather forecast and no 30-day outage data"
        )
        return None

    base: Optional[DataFrame] = None
    if dim_district is not None:
        candidate = (
            dim_district.select(
                F.col("district").cast("string").alias("district")
            )
            .filter(F.col("district").isNotNull() & (F.col("district") != ""))
            .distinct()
        )
        if int(candidate.count()) > 0:
            base = candidate
    if base is None:
        parts = [
            frame.select("district").filter(F.col("district").isNotNull())
            for frame in (weather_agg, outage_counts)
            if frame is not None
        ]
        if not parts:
            LOG.info("skipping risk forecast: no district universe available")
            return None
        base = parts[0]
        for part in parts[1:]:
            base = base.unionByName(part)
        base = base.distinct()
        if int(base.count()) == 0:
            return None

    joined = base
    if weather_agg is not None:
        joined = joined.join(weather_agg, on="district", how="left")
    else:
        joined = (
            joined.withColumn("max_precip_mm", F.lit(0.0).cast("float"))
            .withColumn("max_wind_kph", F.lit(0.0).cast("float"))
            .withColumn("mean_temp_c", F.lit(None).cast("float"))
            .withColumn("forecast_hours", F.lit(0).cast("int"))
        )
    if outage_counts is not None:
        joined = joined.join(outage_counts, on="district", how="left")
    else:
        joined = joined.withColumn("outage_30d", F.lit(0).cast("int"))

    max_outage_row = joined.agg(
        F.coalesce(F.max("outage_30d"), F.lit(0)).alias("max_outage")
    ).collect()[0]
    max_outage = max(1, int(max_outage_row["max_outage"] or 0))

    scored = (
        joined.withColumn(
            "max_precip_mm", F.coalesce(F.col("max_precip_mm"), F.lit(0.0))
        )
        .withColumn("max_wind_kph", F.coalesce(F.col("max_wind_kph"), F.lit(0.0)))
        .withColumn("outage_30d", F.coalesce(F.col("outage_30d"), F.lit(0)))
        .withColumn(
            "precip_norm",
            F.least(F.col("max_precip_mm") / F.lit(50.0), F.lit(1.0)),
        )
        .withColumn(
            "wind_norm",
            F.least(F.col("max_wind_kph") / F.lit(70.0), F.lit(1.0)),
        )
        .withColumn(
            "weather_score",
            F.lit(0.65) * F.col("precip_norm") + F.lit(0.35) * F.col("wind_norm"),
        )
        .withColumn(
            "outage_norm",
            F.least(
                F.col("outage_30d").cast("double") / F.lit(float(max_outage)),
                F.lit(1.0),
            ),
        )
        .withColumn(
            "risk_score",
            F.least(
                F.greatest(
                    F.lit(0.60) * F.col("weather_score")
                    + F.lit(0.40) * F.col("outage_norm"),
                    F.lit(0.0),
                ),
                F.lit(1.0),
            ),
        )
        .withColumn("risk_score", F.round(F.col("risk_score"), 4))
        .withColumn(
            "risk_level",
            F.when(F.col("risk_score") < 0.25, F.lit("low"))
            .when(F.col("risk_score") < 0.50, F.lit("moderate"))
            .when(F.col("risk_score") < 0.75, F.lit("high"))
            .otherwise(F.lit("severe")),
        )
        .withColumn(
            "expected_outages",
            F.greatest(
                F.round(
                    F.col("outage_30d").cast("double")
                    / F.lit(float(LOOKBACK_DAYS))
                    * (F.lit(1.0) + F.lit(1.5) * F.col("weather_score"))
                ),
                F.lit(0.0),
            ).cast("int"),
        )
        .select(
            F.col("district"),
            F.col("risk_score").cast("float").alias("risk_score"),
            F.col("risk_level"),
            F.col("expected_outages").cast("int").alias("expected_outages"),
            F.col("max_precip_mm").cast("float").alias("max_precip_mm"),
            F.col("max_wind_kph").cast("float").alias("max_wind_kph"),
        )
        .orderBy("district")
    )
    rows: List[Tuple[Any, ...]] = []
    for row in scored.collect():
        rows.append(
            (
                row["district"],
                forecast_date,
                float(row["risk_score"]),
                row["risk_level"],
                int(row["expected_outages"]),
                float(row["max_precip_mm"]),
                float(row["max_wind_kph"]),
            )
        )
    LOG.info("risk forecast computed for %s districts", len(rows))
    return rows


# ---------------------------------------------------------------------------
# Parquet archive
# ---------------------------------------------------------------------------
def write_parquet_archive(
    events: Optional[DataFrame],
    archive_date: date,
    archive_dir: str,
) -> Tuple[Optional[str], Optional[int]]:
    """Archive the events of ``archive_date`` under ``<archive_dir>/dt=...``.

    Returns ``(path, row_count)`` or ``(None, None)`` when there is nothing to
    archive.  ``overwrite`` makes the daily job idempotent for a given day.
    """
    if events is None:
        return None, None
    path = os.path.join(archive_dir, "dt={}".format(archive_date.isoformat()))
    archive_day = F.to_date(F.lit(archive_date.isoformat()))
    day_events = events.filter(F.to_date(F.col("start_ts")) == archive_day)
    row_count = int(day_events.count())
    LOG.info("archiving %s outage events for %s to %s", row_count, archive_date, path)
    day_events.write.mode("overwrite").parquet(path)
    return path, row_count


# ---------------------------------------------------------------------------
# Postgres persistence
# ---------------------------------------------------------------------------
@dataclass
class BatchOutputs:
    """Everything the daily run computed, ready for a single transaction."""

    clusters: Optional[List[Tuple[Any, ...]]] = None
    etr: Optional[Tuple[date, float, float, int]] = None
    risk: Optional[List[Tuple[Any, ...]]] = None
    risk_date: Optional[date] = None
    archive_path: Optional[str] = None
    archive_rows: Optional[int] = None


def write_job_run(
    cfg: DbConfig,
    run_id: str,
    status: str,
    started_at: Optional[datetime],
    finished_at: Optional[datetime],
    detail: Dict[str, Any],
    required: bool = True,
) -> None:
    """Upsert the ops.job_runs row (``required=False`` = best effort)."""
    payload = json.dumps(_json_safe(detail), default=str)

    def _tx() -> None:
        conn = connect_db(cfg)
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        UPSERT_JOB_RUN,
                        (JOB_NAME, run_id, status, started_at, finished_at, payload),
                    )
        finally:
            conn.close()

    if required:
        run_with_retries(_tx, attempts=3, label="ops.job_runs")
        return
    try:
        run_with_retries(_tx, attempts=2, base_delay=1.0, label="ops.job_runs")
    except Exception as exc:  # noqa: BLE001 - metrics table may not exist yet
        LOG.warning("could not write ops.job_runs (%s): %s", status, exc)


def persist_outputs(
    cfg: DbConfig,
    outputs: BatchOutputs,
    run_id: str,
    status: str,
    started_at: datetime,
    finished_at: datetime,
    detail: Dict[str, Any],
) -> None:
    """One transaction: ml tables + the final ops.job_runs row."""
    payload = json.dumps(_json_safe(detail), default=str)

    def _tx() -> None:
        conn = connect_db(cfg)
        try:
            with conn:
                with conn.cursor() as cur:
                    if outputs.clusters is not None:
                        cur.execute(DELETE_OUTAGE_CLUSTERS)
                        if outputs.clusters:
                            execute_values(
                                cur, INSERT_OUTAGE_CLUSTERS, outputs.clusters,
                                page_size=500,
                            )
                    if outputs.etr is not None:
                        model_date, mae, r2, sample_count = outputs.etr
                        cur.execute(DELETE_ETR_FOR_DATE, (model_date,))
                        # Single row: plain execute (execute_values requires one
                        # '%s' placeholder and this statement has four columns).
                        cur.execute(
                            INSERT_ETR, (model_date, mae, r2, sample_count)
                        )
                    if outputs.risk is not None:
                        if outputs.risk_date is not None:
                            cur.execute(DELETE_RISK_FOR_DATE, (outputs.risk_date,))
                        if outputs.risk:
                            execute_values(
                                cur, INSERT_RISK, outputs.risk, page_size=1000
                            )
                    cur.execute(
                        UPSERT_JOB_RUN,
                        (JOB_NAME, run_id, status, started_at, finished_at, payload),
                    )
        finally:
            conn.close()

    run_with_retries(_tx, attempts=3, label="ml outputs + ops.job_runs")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_date(value: str) -> date:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "expected YYYY-MM-DD, got {!r}".format(value)
        ) from exc


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Daily Spark batch job: outage clusters + ETR model metrics + "
            "next-24h district risk forecast + Parquet archive."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--bootstrap",
        default=os.getenv("KAFKA_BOOTSTRAP") or os.getenv("SPARK_KAFKA_BOOTSTRAP", "kafka:9092"),
        help="Kafka bootstrap (accepted for contract parity; unused by batch)",
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
        "--run-date",
        type=_parse_date,
        default=os.getenv("SPARK_RUN_DATE") and _parse_date(os.getenv("SPARK_RUN_DATE")),
        help="business date of this run, YYYY-MM-DD (env SPARK_RUN_DATE; default today UTC)",
    )
    parser.add_argument(
        "--archive-date",
        type=_parse_date,
        default=os.getenv("SPARK_ARCHIVE_DATE")
        and _parse_date(os.getenv("SPARK_ARCHIVE_DATE")),
        help="event date to archive, YYYY-MM-DD (default: run_date - 1 day)",
    )
    parser.add_argument(
        "--archive-dir",
        default=os.getenv("SPARK_LAKE_DIR", DEFAULT_ARCHIVE_DIR),
        help="Parquet lake root (env SPARK_LAKE_DIR)",
    )
    parser.add_argument(
        "--run-id",
        default=os.getenv("SPARK_RUN_ID"),
        help="ops.job_runs run_id (default: run_date, reruns update the row)",
    )
    parser.add_argument(
        "--k",
        type=_positive_int,
        default=int(os.getenv("SPARK_CLUSTER_K", str(CLUSTER_K))),
        help="KMeans k (env SPARK_CLUSTER_K)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=int(os.getenv("SPARK_RANDOM_SEED", str(RANDOM_SEED))),
        help="random seed for KMeans and train/test split (env SPARK_RANDOM_SEED)",
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def run(spark: SparkSession, args: argparse.Namespace) -> int:
    started_at = datetime.now(timezone.utc)
    run_date: date = args.run_date or started_at.date()
    archive_date: date = args.archive_date or (run_date - timedelta(days=1))
    forecast_date: date = run_date + timedelta(days=1)
    run_id: str = args.run_id or run_date.isoformat()

    cfg = DbConfig(
        jdbc_url=args.jdbc_url,
        user=args.jdbc_user,
        password=args.jdbc_password,
    )
    problems: List[str] = []
    errors: List[str] = []
    outputs = BatchOutputs()
    loaded: Dict[str, LoadedTable] = {}

    LOG.info(
        "%s starting: run_id=%s run_date=%s archive_date=%s forecast_date=%s",
        JOB_NAME, run_id, run_date, archive_date, forecast_date,
    )
    LOG.info("bootstrap argument (unused by batch): %s", args.bootstrap)

    initial_detail = {
        "run_id": run_id,
        "run_date": run_date,
        "archive_date": archive_date,
        "forecast_date": forecast_date,
    }
    write_job_run(
        cfg, run_id, "running", started_at, None, initial_detail, required=False
    )

    try:
        loaded["analytics.fct_outage_events"] = read_table(
            spark, cfg, "analytics.fct_outage_events", FCT_REQUIRED, problems
        )
        loaded["analytics.dim_district"] = read_table(
            spark, cfg, "analytics.dim_district", DIM_REQUIRED, problems
        )
        loaded["raw.weather_hourly"] = read_table(
            spark, cfg, "raw.weather_hourly", WEATHER_REQUIRED, problems
        )
        loaded["analytics.reliability_daily"] = read_table(
            spark, cfg, "analytics.reliability_daily", RELIABILITY_REQUIRED, problems
        )

        events = loaded["analytics.fct_outage_events"].df
        dim_district = loaded["analytics.dim_district"].df
        weather = loaded["raw.weather_hourly"].df
        reliability = loaded["analytics.reliability_daily"].df

        try:
            outputs.clusters = compute_outage_clusters(
                events, dim_district, k=args.k, seed=args.seed
            )
        except Exception as exc:  # noqa: BLE001 - keep going, record as failure
            message = "outage clusters failed: {}".format(exc)
            LOG.exception(message)
            errors.append(message)

        try:
            etr_metrics = compute_etr_metrics(events)
            if etr_metrics is not None:
                outputs.etr = (run_date, etr_metrics[0], etr_metrics[1], etr_metrics[2])
        except Exception as exc:  # noqa: BLE001
            message = "ETR model failed: {}".format(exc)
            LOG.exception(message)
            errors.append(message)

        try:
            risk_rows = compute_risk_forecast(
                events, dim_district, weather, reliability, forecast_date
            )
            outputs.risk = risk_rows
            if risk_rows is not None:
                outputs.risk_date = forecast_date
        except Exception as exc:  # noqa: BLE001
            message = "risk forecast failed: {}".format(exc)
            LOG.exception(message)
            errors.append(message)

        try:
            archive_path, archive_rows = write_parquet_archive(
                events, archive_date, args.archive_dir
            )
            outputs.archive_path = archive_path
            outputs.archive_rows = archive_rows
        except Exception as exc:  # noqa: BLE001 - archive is part of the contract
            message = "parquet archive failed: {}".format(exc)
            LOG.exception(message)
            errors.append(message)
    except Exception as exc:  # noqa: BLE001 - unexpected failure, still report
        message = "unexpected batch failure: {}".format(exc)
        LOG.exception(message)
        errors.append(message)
    finally:
        for table in loaded.values():
            table.unpersist()

    finished_at = datetime.now(timezone.utc)
    status = "failed" if errors else "success"
    detail = {
        "run_id": run_id,
        "run_date": run_date,
        "archive_date": archive_date,
        "forecast_date": forecast_date,
        "status": status,
        "counts": {
            name: table.row_count for name, table in loaded.items()
        },
        "clusters": (
            {"rows": len(outputs.clusters)} if outputs.clusters is not None else None
        ),
        "etr_model": (
            {
                "model_date": outputs.etr[0],
                "mae_minutes": outputs.etr[1],
                "r2": outputs.etr[2],
                "sample_count": outputs.etr[3],
            }
            if outputs.etr is not None
            else None
        ),
        "risk_forecast": (
            {"rows": len(outputs.risk)} if outputs.risk is not None else None
        ),
        "archive": (
            {"path": outputs.archive_path, "rows": outputs.archive_rows}
            if outputs.archive_path is not None
            else None
        ),
        "problems": problems[:50],
        "errors": errors[:50],
        "duration_seconds": round((finished_at - started_at).total_seconds(), 1),
        "spark_version": spark.version,
    }
    LOG.info(
        "%s finished: status=%s duration=%ss problems=%s errors=%s",
        JOB_NAME,
        status,
        detail["duration_seconds"],
        len(problems),
        len(errors),
    )

    try:
        persist_outputs(
            cfg, outputs, run_id, status, started_at, finished_at, detail
        )
    except Exception as exc:  # noqa: BLE001 - cannot record success, fail loudly
        LOG.exception("could not persist Spark daily outputs: %s", exc)
        write_job_run(
            cfg,
            run_id,
            "failed",
            started_at,
            finished_at,
            {**detail, "errors": errors + ["persist failed: {}".format(exc)]},
            required=False,
        )
        return 1

    if errors:
        LOG.error("%s completed with %s error(s)", JOB_NAME, len(errors))
        return 1
    LOG.info("%s completed successfully", JOB_NAME)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    spark = build_spark_session()
    spark.sparkContext.setLogLevel(str(args.spark_log_level).upper())
    try:
        return run(spark, args)
    except KeyboardInterrupt:
        LOG.warning("interrupted")
        return 1
    finally:
        try:
            spark.stop()
        except Exception:  # noqa: BLE001
            LOG.exception("error stopping SparkSession")


if __name__ == "__main__":
    sys.exit(main())
