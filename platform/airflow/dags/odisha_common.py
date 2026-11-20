"""Shared configuration and helpers for the Odisha power-outage data platform.

This module is imported both by the Airflow DAGs (``platform/airflow/dags``) and
by the standalone scripts in ``platform/airflow/scripts``.  Everything is driven
by environment variables with safe, non-secret defaults so that:

* Airflow DAG parsing stays fast (heavy libraries such as ``psycopg2``,
  ``pyspark`` and ``kafka`` are imported lazily inside the functions);
* every script can be executed directly with ``python script.py --help``;
* no credentials are hardcoded anywhere.

Environment variables (all optional, sensible defaults in parentheses)::

    POSTGRES_HOST (postgres)          POSTGRES_PORT (5432)
    POSTGRES_DB (odisha)              POSTGRES_USER (odisha)
    POSTGRES_PASSWORD ("")            DATABASE_URL (built from the above)
    KAFKA_BOOTSTRAP (kafka:9092)
    REPO_DIR (/opt/odisha)            DATA_DIR (/opt/odisha/data)
    SCRAPED_DIR (auto: repo scrappedData, falling back to DATA_DIR/scrappedData)
    MAPPINGS_DIR (auto-detected)
    DBT_BIN (/opt/dbt/bin/dbt)        DBT_PROJECT_DIR (/opt/odisha/platform/dbt)
    DBT_PROFILES_DIR (=project dir)   DBT_TARGET_PATH (DATA_DIR/dbt/target)
    DBT_LOG_PATH (DATA_DIR/dbt/logs)
    JDBC_JAR (/opt/jdbc/postgresql-42.7.4.jar)
    SPARK_MASTER (local[2])           SPARK_EXTRA_CONF ("")
    FLINK_REST_URL (http://flink-jobmanager:8081)
    FLINK_JOB_NAME (odisha-user-report-alerts)
    FLINK_SQL_GATEWAY_URL ("")        FLINK_RESUBMIT_CMD ("")
    TZ (Asia/Kolkata) - schedules are always evaluated in IST.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote, quote_plus, urlsplit
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Environment / static configuration
# ---------------------------------------------------------------------------

APP_NAME = "odisha-platform"
IST = ZoneInfo(os.environ.get("TZ") or "Asia/Kolkata")


def _env(name: str, default: str) -> str:
    """Return env var ``name`` if set and non-empty, else ``default``."""
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except (TypeError, ValueError):
        return default


# --- filesystem layout -----------------------------------------------------
REPO_DIR = Path(_env("REPO_DIR", "/opt/odisha"))
DATA_DIR = Path(_env("DATA_DIR", str(REPO_DIR / "data")))
MART_DIR = DATA_DIR / "mart"
SCRAPED_DIR_ENV = os.environ.get("SCRAPED_DIR") or ""
MAPPINGS_DIR_ENV = os.environ.get("MAPPINGS_DIR") or ""

#: Location of ``odisha_common.py`` itself (the Airflow dags folder).
DAGS_DIR = Path(__file__).resolve().parent
#: Repository root when running from a checkout (``<repo>/platform/airflow/dags``).
_CHECKOUT_ROOT = Path(__file__).resolve().parents[3]

# --- Postgres --------------------------------------------------------------
POSTGRES_HOST = _env("POSTGRES_HOST", "postgres")
POSTGRES_PORT = _env_int("POSTGRES_PORT", 5432)
POSTGRES_DB = _env("POSTGRES_DB", "odisha")
POSTGRES_USER = _env("POSTGRES_USER", "odisha")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD") or ""
POSTGRES_CONNECT_TIMEOUT = _env_int("POSTGRES_CONNECT_TIMEOUT", 10)

# --- Kafka -----------------------------------------------------------------
KAFKA_BOOTSTRAP = _env("KAFKA_BOOTSTRAP", "kafka:9092")
KAFKA_TOPIC_PARTITIONS = _env_int("KAFKA_TOPIC_PARTITIONS", 3)
KAFKA_TOPIC_REPLICATION = _env_int("KAFKA_TOPIC_REPLICATION", 1)
KAFKA_RETENTION_MS = _env_int("KAFKA_RETENTION_MS", 24 * 60 * 60 * 1000)

TOPIC_USER_REPORTS = "user-reports"
TOPIC_OFFICIAL_OUTAGES = "official-outages"
TOPIC_WEATHER = "weather-observations"
TOPIC_ALERTS = "outage-alerts"
TOPIC_FLINK_WINDOWS = "flink-report-windows"

ALL_TOPICS: tuple[str, ...] = (
    TOPIC_USER_REPORTS,
    TOPIC_OFFICIAL_OUTAGES,
    TOPIC_WEATHER,
    TOPIC_ALERTS,
    TOPIC_FLINK_WINDOWS,
)

# --- dbt (isolated virtualenv baked into the Airflow image) ----------------
DBT_BIN = _env("DBT_BIN", "/opt/dbt/bin/dbt")
DBT_PROJECT_DIR = Path(_env("DBT_PROJECT_DIR", str(REPO_DIR / "platform" / "dbt")))
DBT_PROFILES_DIR = Path(_env("DBT_PROFILES_DIR", str(DBT_PROJECT_DIR)))
DBT_TARGET_PATH = Path(_env("DBT_TARGET_PATH", str(DATA_DIR / "dbt" / "target")))
DBT_LOG_PATH = Path(_env("DBT_LOG_PATH", str(DATA_DIR / "dbt" / "logs")))

# --- Spark ----------------------------------------------------------------
JDBC_JAR = Path(_env("JDBC_JAR", "/opt/jdbc/postgresql-42.7.4.jar"))
SPARK_JOBS_DIR = Path(_env("SPARK_JOBS_DIR", str(REPO_DIR / "platform" / "spark" / "jobs")))
SPARK_MASTER = _env("SPARK_MASTER", "local[2]")
SPARK_EXTRA_CONF = _env("SPARK_EXTRA_CONF", "")
SPARK_SUBMIT_TIMEOUT = _env_int("SPARK_SUBMIT_TIMEOUT", 3600)

# --- Flink ----------------------------------------------------------------
FLINK_REST_URL = _env("FLINK_REST_URL", "http://flink-jobmanager:8081").rstrip("/")
FLINK_JOB_NAME = _env("FLINK_JOB_NAME", "odisha-user-report-alerts")
FLINK_SQL_GATEWAY_URL = _env("FLINK_SQL_GATEWAY_URL", "").rstrip("/")
FLINK_RESUBMIT_CMD = _env("FLINK_RESUBMIT_CMD", "")
FLINK_RESUBMIT_WAIT_SECONDS = _env_int("FLINK_RESUBMIT_WAIT_SECONDS", 120)
FLINK_SQL_FILE = Path(
    _env("FLINK_SQL_FILE", str(REPO_DIR / "platform" / "flink" / "jobs" / "user_report_alerts.sql"))
)
FLINK_HTTP_TIMEOUT = _env_int("FLINK_HTTP_TIMEOUT", 5)

# --- health thresholds ----------------------------------------------------
SPARK_HEARTBEAT_MAX_AGE_S = _env_int("SPARK_HEARTBEAT_MAX_AGE_S", 10 * 60)
FLINK_HEARTBEAT_MAX_AGE_S = _env_int("FLINK_HEARTBEAT_MAX_AGE_S", 10 * 60)
HEARTBEAT_STALE_SECONDS = _env_int("HEARTBEAT_STALE_SECONDS", 15 * 60)

# --- scraper --------------------------------------------------------------
SCRAPE_URL = "https://kavach.tpodisha.com"
SCRAPE_DISCOMS = ("CODL", "NODL", "WODL", "SODL")

# --- weather --------------------------------------------------------------
OPEN_METEO_URL = _env("OPEN_METEO_URL", "https://api.open-meteo.com/v1/forecast")
WEATHER_FORECAST_DAYS = _env_int("WEATHER_FORECAST_DAYS", 2)
WEATHER_TIMEZONE = _env("WEATHER_TIMEZONE", "Asia/Kolkata")
WEATHER_KAFKA_HOURS = _env_int("WEATHER_KAFKA_HOURS", 24)
WEATHER_REQUEST_TIMEOUT = _env_int("WEATHER_REQUEST_TIMEOUT", 60)
WEATHER_MAX_COORDS_PER_REQUEST = _env_int("WEATHER_MAX_COORDS_PER_REQUEST", 30)

# --- Airflow DAG defaults -------------------------------------------------
DAG_START_DATE = datetime(2025, 1, 1, tzinfo=IST)
DEFAULT_ARGS: dict[str, Any] = {
    "owner": "odisha-platform",
    "depends_on_past": False,
    "retries": 2,
    "retry_delay": timedelta(minutes=2),
    "email_on_failure": False,
    "email_on_retry": False,
}
DAG_TAGS: tuple[str, ...] = ("odisha", "platform")


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def utc_now() -> datetime:
    """Timezone-aware UTC ``now``."""
    return datetime.now(timezone.utc)


def iso_z(moment: datetime | None = None) -> str:
    """Format a datetime as an ISO-8601 UTC string with a ``Z`` suffix."""
    moment = moment or utc_now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | datetime | None) -> datetime | None:
    """Parse an ISO-8601 string into an aware UTC datetime (``None`` on failure)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def seconds_since(moment: datetime | None) -> float:
    """Age of ``moment`` in seconds (``inf`` when unknown)."""
    if moment is None:
        return float("inf")
    return (utc_now() - moment).total_seconds()


def batch_id_for_context(context: Mapping[str, Any] | None = None) -> str:
    """Deterministic scrape-batch id for an Airflow task instance.

    Uses the DAG run's ``data_interval_start`` (falling back to logical/execution
    date) floored to the minute.  Retries of the same run therefore reuse the same
    batch id, which keeps ``raw.scrape_batches`` / ``raw.official_outages``
    idempotent.  When executed outside Airflow, the current UTC minute is used.
    """
    context = context or {}
    moment = (
        context.get("data_interval_start")
        or context.get("logical_date")
        or context.get("execution_date")
    )
    if moment is None:
        # Manual ``dag_run.conf`` override, e.g. {"batch_id": "2026-11-19T12:30:00Z"}.
        dag_run = context.get("dag_run")
        conf = getattr(dag_run, "conf", None) or {}
        override = conf.get("batch_id") if isinstance(conf, Mapping) else None
        if override:
            parsed = parse_iso(override)
            if parsed is not None:
                return iso_z(parsed)
        moment = utc_now()
    if not isinstance(moment, datetime):
        moment = parse_iso(moment) or utc_now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return iso_z(moment.astimezone(timezone.utc).replace(second=0, microsecond=0))


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

def ensure_dir(path: Path | str) -> Path:
    """``mkdir -p`` and return the directory as a :class:`Path`."""
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def _is_writable_dir(path: Path, create: bool = True) -> bool:
    """Return True when ``path`` is (or can become) a writable directory."""
    try:
        if create:
            path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            return False
        probe = path / f".write-probe-{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def scraped_data_dir(create: bool = True) -> Path:
    """Resolve the CSV output folder used by the scraper.

    Defaults to ``$REPO_DIR/scrappedData`` (the historical repo location), but
    transparently falls back to ``$DATA_DIR/scrappedData`` when the repository
    bind-mount is read-only for the Airflow user.  Both the scraper and the
    loader use this helper, so they always agree on the location.
    """
    if SCRAPED_DIR_ENV:
        candidates = [Path(SCRAPED_DIR_ENV)]
    else:
        candidates = [
            REPO_DIR / "scrappedData",
            DATA_DIR / "scrappedData",
            Path("/tmp") / "odisha-scrappedData",
        ]
    for candidate in candidates:
        if _is_writable_dir(candidate, create=create) or (
            not create and candidate.is_dir()
        ):
            return candidate
    # Last resort: let the caller fail with a clear message about the path.
    return candidates[0]


def _mappings_candidates() -> list[Path]:
    candidates: list[Path] = []
    if MAPPINGS_DIR_ENV:
        candidates.append(Path(MAPPINGS_DIR_ENV))
    candidates.extend(
        [
            REPO_DIR / "data" / "mappings",
            Path("/opt/airflow/mappings"),
            DATA_DIR / "mappings",
            _CHECKOUT_ROOT / "data" / "mappings",
        ]
    )
    seen: set[str] = set()
    unique: list[Path] = []
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


#: Mapping files shipped in the repository and baked into the image as fallback.
MAPPING_FILES: tuple[str, ...] = (
    "district_aliases.json",
    "circle_to_district.json",
    "district_centroids.json",
    "geocode_cache.json",
)


def mappings_dir(required: Sequence[str] = ("district_aliases.json", "district_centroids.json")) -> Path:
    """Resolve the folder holding the historical JSON mappings.

    Prefers ``$MAPPINGS_DIR``, then the repository folder, then a copy baked into
    the Airflow image (``/opt/airflow/mappings``) because the shared
    ``odisha_app_data`` volume shadows ``/opt/odisha/data`` inside the container.
    """
    searched: list[Path] = []
    best: tuple[int, Path] | None = None
    for candidate in _mappings_candidates():
        searched.append(candidate)
        if not candidate.is_dir():
            continue
        present = sum(1 for name in MAPPING_FILES if (candidate / name).is_file())
        if present == len(MAPPING_FILES):
            return candidate
        if all((candidate / name).is_file() for name in required):
            if best is None or present > best[0]:
                best = (present, candidate)
    if best is not None:
        return best[1]
    raise FileNotFoundError(
        "Could not find district mapping JSONs; searched: "
        + ", ".join(str(path) for path in searched)
        + ". Set MAPPINGS_DIR or mount ./data/mappings into the Airflow container."
    )


def load_json(path: Path | str) -> Any:
    """Read a UTF-8 JSON file."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json_atomic(path: Path | str, payload: Any, indent: int = 2) -> Path:
    """Atomically replace ``path`` with ``payload`` encoded as JSON.

    The write goes to a temporary file in the same directory followed by
    :func:`os.replace`, so consumers (the Go API reading the file as a Postgres
    fallback) never observe a partially written document.
    """
    target = Path(path)
    ensure_dir(target.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=indent, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return target


def read_scripts_dir() -> Path:
    """Return the folder containing the standalone platform scripts."""
    candidates: list[Path] = []
    env_dir = os.environ.get("PLATFORM_SCRIPTS_DIR")
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.extend(
        [
            DAGS_DIR.parent / "scripts",  # <airflow>/scripts next to <airflow>/dags
            Path("/opt/airflow/scripts"),
            REPO_DIR / "platform" / "airflow" / "scripts",
            _CHECKOUT_ROOT / "platform" / "airflow" / "scripts",
        ]
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


def ensure_scripts_on_path() -> Path:
    """Add the scripts folder to ``sys.path`` so DAG files can import scripts."""
    scripts = read_scripts_dir()
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    return scripts


def tail_text(text: str | None, lines: int = 40) -> str:
    """Return the last ``lines`` lines of ``text`` (useful in error messages)."""
    if not text:
        return ""
    return "\n".join(str(text).rstrip().splitlines()[-lines:])


# ---------------------------------------------------------------------------
# Postgres helpers
# ---------------------------------------------------------------------------

def postgres_config(dbname: str | None = None) -> dict[str, Any]:
    """Connection kwargs from env (or from ``DATABASE_URL`` when set alone)."""
    dbname = dbname or POSTGRES_DB
    database_url = os.environ.get("DATABASE_URL") or ""
    host_from_env = os.environ.get("POSTGRES_HOST")
    if database_url and not host_from_env:
        parts = urlsplit(database_url)
        cfg: dict[str, Any] = {
            "host": parts.hostname or POSTGRES_HOST,
            "port": parts.port or POSTGRES_PORT,
            "dbname": dbname,
            "user": parts.username or POSTGRES_USER,
            "password": parts.password or POSTGRES_PASSWORD,
        }
    else:
        cfg = {
            "host": POSTGRES_HOST,
            "port": POSTGRES_PORT,
            "dbname": dbname,
            "user": POSTGRES_USER,
            "password": POSTGRES_PASSWORD,
        }
    cfg["connect_timeout"] = POSTGRES_CONNECT_TIMEOUT
    cfg["application_name"] = f"{APP_NAME}-airflow"
    if not cfg.get("password"):
        cfg.pop("password", None)
    return cfg


def database_url(dbname: str | None = None) -> str:
    """SQLAlchemy/psycopg2 style ``postgresql://`` URL built from env."""
    cfg = postgres_config(dbname)
    auth = quote(cfg["user"], safe="")
    if cfg.get("password"):
        auth += ":" + quote_plus(str(cfg["password"]))
    return f"postgresql://{auth}@{cfg['host']}:{cfg['port']}/{cfg['dbname']}"


def jdbc_url(dbname: str | None = None) -> str:
    """JDBC URL used by Spark (``jdbc:postgresql://host:port/db``)."""
    cfg = postgres_config(dbname)
    return f"jdbc:postgresql://{cfg['host']}:{cfg['port']}/{cfg['dbname']}"


def pg_connect(dbname: str | None = None, autocommit: bool = False):
    """Open a psycopg2 connection using the platform env configuration."""
    import psycopg2  # imported lazily: DAG parsing must stay cheap

    conn = psycopg2.connect(**postgres_config(dbname))
    conn.autocommit = autocommit
    return conn


class _ConnectionContext:
    """Context manager yielding a committed/rolled-back psycopg2 connection."""

    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        self._conn.close()
        return False


def db_connection(dbname: str | None = None, autocommit: bool = False):
    """``with db_connection() as conn:`` helper with commit/rollback handling."""
    return _ConnectionContext(pg_connect(dbname=dbname, autocommit=autocommit))


def psycopg2_json(payload: Any):
    """Wrap a Python object for a psycopg2 ``jsonb`` parameter."""
    from psycopg2.extras import Json

    return Json(payload)


def execute_fetch_all(sql: str, params: Sequence[Any] | None = None, dbname: str | None = None) -> list[dict]:
    """Run a read-only query and return a list of dict rows."""
    from psycopg2.extras import RealDictCursor

    with db_connection(dbname=dbname) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            if cur.description is None:
                return []
            return [dict(row) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# dbt helpers (the dbt CLI lives in an isolated venv at /opt/dbt)
# ---------------------------------------------------------------------------

def dbt_base_args() -> list[str]:
    """Global dbt flags pointing at the repo project and writable runtime dirs.

    ``--target-path`` / ``--log-path`` are redirected into ``$DATA_DIR`` so dbt
    never needs write access to the (possibly read-only) repository bind-mount.
    Paths are rendered POSIX-style so the command is identical when inspected from
    a non-Linux host (e.g. a Windows dev checkout) and inside the container.
    """
    return [
        "--project-dir",
        DBT_PROJECT_DIR.as_posix(),
        "--profiles-dir",
        DBT_PROFILES_DIR.as_posix(),
        "--target-path",
        DBT_TARGET_PATH.as_posix(),
        "--log-path",
        DBT_LOG_PATH.as_posix(),
        "--no-use-colors",
    ]


def dbt_argv(*subcommand: str) -> list[str]:
    """Build ``/opt/dbt/bin/dbt <subcommand...> <global flags>`` as an argv list."""
    return [DBT_BIN, *subcommand, *dbt_base_args()]


def dbt_command(*subcommand: str) -> str:
    """Shell-safe single string for a dbt invocation (used by BashOperator).

    ``PYTHONNOUSERSITE=1`` is required in this image: the container runs as
    root with the image's HOME=/home/airflow, so Python adds
    /home/airflow/.local/lib/python3.12/site-packages to sys.path and the
    Airflow-installed typing_extensions shadows the dbt venv's newer one
    (ImportError: cannot import name 'evaluate_forward_ref'). Disabling the
    user site for the dbt subprocess only keeps Airflow's own environment
    untouched.
    """
    return (
        "PYTHONPATH=/opt/dbt/lib/python3.12/site-packages "
        "PYTHONNOUSERSITE=1 "
        + shlex.join(dbt_argv(*subcommand))
    )


# ---------------------------------------------------------------------------
# Spark helpers
# ---------------------------------------------------------------------------

def spark_submit_bin() -> str:
    """Locate ``spark-submit`` inside the pip-installed PySpark distribution."""
    import pyspark  # imported lazily (heavy and unnecessary during DAG parsing)

    candidate = Path(os.path.dirname(pyspark.__file__)) / "bin" / "spark-submit"
    if not candidate.is_file():
        raise RuntimeError(
            f"spark-submit not found at {candidate}; is pyspark installed in the Airflow environment?"
        )
    return str(candidate)


def spark_submit_argv(job_file: Path | str, app_args: Sequence[str] = ()) -> list[str]:
    """Full ``spark-submit`` argv for one of the platform Spark jobs.

    Always passes the baked Postgres JDBC driver via ``--jars``.  Credentials are
    *not* placed on the command line: the Spark jobs fall back to the shared
    container environment (``DATABASE_URL`` / ``POSTGRES_*``) for JDBC access.
    """
    job_path = Path(job_file)
    if not job_path.is_file():
        raise FileNotFoundError(
            f"Spark job not found: {job_path}. The repository should be mounted at {REPO_DIR}."
        )
    if not JDBC_JAR.is_file():
        raise FileNotFoundError(f"Postgres JDBC driver missing: {JDBC_JAR}")
    argv = [spark_submit_bin(), "--master", SPARK_MASTER, "--jars", JDBC_JAR.as_posix()]
    for conf in _split_confs(SPARK_EXTRA_CONF):
        argv.extend(["--conf", conf])
    argv.append(job_path.as_posix())
    argv.extend(str(arg) for arg in app_args)
    return argv


def _split_confs(raw: str) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(";") if part.strip()]


def run_spark_submit(
    job_file: Path | str,
    app_args: Sequence[str] = (),
    timeout: int | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Run ``spark-submit`` synchronously and return its stdout.

    Raises :class:`RuntimeError` with the tail of the driver output when the job
    fails, so the Airflow task log and error message are actionable.
    """
    argv = spark_submit_argv(job_file, app_args)
    child_env = dict(os.environ)
    if env:
        child_env.update(env)
    child_env.setdefault("JAVA_HOME", _detect_java_home() or "")
    log(f"Running: {shlex.join(argv)}")
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout or SPARK_SUBMIT_TIMEOUT,
        env=child_env,
    )
    log(completed.stdout or "")
    if completed.returncode != 0:
        raise RuntimeError(
            f"spark-submit failed (exit {completed.returncode}) for {job_file}:\n"
            f"{tail_text(completed.stderr or completed.stdout, 80)}"
        )
    return completed.stdout or ""


def _detect_java_home() -> str | None:
    """Best-effort JAVA_HOME detection for the Debian Airflow image."""
    candidates = [
        os.environ.get("JAVA_HOME"),
        "/usr/lib/jvm/java-17-openjdk-amd64",
        "/usr/lib/jvm/java-17-openjdk",
        "/usr/lib/jvm/default-java",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_dir():
            return candidate
    return None


# ---------------------------------------------------------------------------
# Flink helpers (health DAG: presence check + best-effort resubmission)
# ---------------------------------------------------------------------------

def flink_jobs_overview() -> list[dict]:
    """Return the Flink cluster's job overview entries."""
    import requests

    response = requests.get(f"{FLINK_REST_URL}/jobs/overview", timeout=FLINK_HTTP_TIMEOUT)
    response.raise_for_status()
    return list(response.json().get("jobs") or [])


def find_flink_job() -> dict | None:
    """Find the expected Flink SQL job by name in the cluster overview."""
    try:
        for job in flink_jobs_overview():
            if job.get("name") == FLINK_JOB_NAME:
                return job
    except Exception as exc:  # noqa: BLE001 - health check must not explode
        log(f"Flink REST check failed: {exc!r}")
        return None
    return None


def split_sql_statements(sql_text: str) -> list[str]:
    """Split a Flink SQL file into statements on ``;`` while respecting comments/strings."""
    statements: list[str] = []
    buffer: list[str] = []
    index = 0
    in_single = False
    in_double = False
    length = len(sql_text)
    while index < length:
        char = sql_text[index]
        nxt = sql_text[index + 1] if index + 1 < length else ""
        if in_single:
            buffer.append(char)
            if char == "'":
                in_single = False
            index += 1
            continue
        if in_double:
            buffer.append(char)
            if char == '"':
                in_double = False
            index += 1
            continue
        if char == "-" and nxt == "-":
            newline = sql_text.find("\n", index)
            if newline == -1:
                break
            index = newline + 1
            continue
        if char == "/" and nxt == "*":
            end = sql_text.find("*/", index + 2)
            index = length if end == -1 else end + 2
            continue
        if char == "'":
            in_single = True
        elif char == '"':
            in_double = True
        elif char == ";":
            statement = "".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer = []
            index += 1
            continue
        buffer.append(char)
        index += 1
    trailing = "".join(buffer).strip()
    if trailing:
        statements.append(trailing)
    return statements


def render_flink_sql(sql_text: str) -> str:
    """Render ``${VAR}`` placeholders in the Flink SQL template from the environment.

    ``platform/flink/submit.sh`` renders ``${KAFKA_BOOTSTRAP}`` and
    ``${POSTGRES_PASSWORD}`` before handing the file to ``sql-client.sh``; the
    health DAG's gateway re-submission path performs the same rendering so the
    template can be executed as-is.
    """
    from string import Template

    values = {
        "KAFKA_BOOTSTRAP": KAFKA_BOOTSTRAP,
        "POSTGRES_PASSWORD": POSTGRES_PASSWORD,
        "DATABASE_URL": os.environ.get("DATABASE_URL", ""),
        "POSTGRES_HOST": POSTGRES_HOST,
        "POSTGRES_PORT": str(POSTGRES_PORT),
        "POSTGRES_DB": POSTGRES_DB,
        "POSTGRES_USER": POSTGRES_USER,
    }
    rendered = Template(sql_text).safe_substitute(values)
    unresolved = sorted(set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", rendered)))
    if unresolved:
        raise RuntimeError(
            "Flink SQL template still has unresolved environment placeholders: "
            + ", ".join(unresolved)
        )
    return rendered


def _submit_flink_sql_via_gateway(sql_text: str) -> dict[str, Any]:
    """Submit the Flink SQL job through the Flink SQL Gateway REST API (v1)."""
    import requests

    base = FLINK_SQL_GATEWAY_URL
    session = requests.post(f"{base}/v1/sessions", json={"properties": {}}, timeout=15)
    session.raise_for_status()
    session_handle = session.json().get("sessionHandle")
    if not session_handle:
        raise RuntimeError(f"SQL gateway did not return a sessionHandle: {session.text[:300]}")
    statements = split_sql_statements(render_flink_sql(sql_text))
    if not statements:
        raise RuntimeError(f"No SQL statements found in {FLINK_SQL_FILE}")
    statement_handles: list[str] = []
    for statement in statements:
        response = requests.post(
            f"{base}/v1/sessions/{session_handle}/statements",
            json={"statement": statement},
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"SQL gateway rejected statement ({response.status_code}): "
                f"{statement[:120]!r} -> {response.text[:300]}"
            )
        statement_handles.append(response.json().get("statementHandle", ""))
    # Wait for the final statement (the INSERTs / job submission) to finish.
    deadline = time.time() + FLINK_RESUBMIT_WAIT_SECONDS
    last_handle = statement_handles[-1]
    while time.time() < deadline:
        status_response = requests.get(
            f"{base}/v1/sessions/{session_handle}/statements/{last_handle}/status",
            timeout=10,
        )
        status_response.raise_for_status()
        status = str(status_response.json().get("status", "")).upper()
        if status == "FINISHED":
            return {"ok": True, "method": "sql-gateway", "statements": len(statements)}
        if status in {"ERROR", "CANCELED", "TIMEOUT", "CLOSED"}:
            raise RuntimeError(f"SQL gateway reported status {status} for the last statement")
        time.sleep(2)
    raise TimeoutError(
        f"Flink SQL job was not submitted within {FLINK_RESUBMIT_WAIT_SECONDS}s via the SQL gateway"
    )


def resubmit_flink_job() -> dict[str, Any]:
    """Best-effort re-submission of the Flink SQL job if it is missing.

    In the deployed compose stack a dedicated ``flink-submit`` sidecar already
    re-runs ``platform/flink/submit.sh`` every 180s, so this is a belt-and-braces
    path for a standalone Airflow deployment.  Two mechanisms are supported (first
    one configured wins):

    1. ``FLINK_SQL_GATEWAY_URL`` - Flink SQL Gateway REST endpoint, e.g.
       ``http://flink-jobmanager:8083`` (requires ``sql-gateway.sh`` to be running
       in the Flink container).  The SQL template is rendered with the
       ``${KAFKA_BOOTSTRAP}`` / ``${POSTGRES_PASSWORD}`` environment values first.
    2. ``FLINK_RESUBMIT_CMD`` - shell command executed in the Airflow container,
       e.g. a wrapper that runs ``platform/flink/submit.sh`` where the Flink
       distribution is available.

    Returns a structured result; never raises (the health DAG records the state).
    """
    try:
        if FLINK_SQL_GATEWAY_URL:
            if not FLINK_SQL_FILE.is_file():
                raise FileNotFoundError(f"Flink SQL file missing: {FLINK_SQL_FILE}")
            sql_text = FLINK_SQL_FILE.read_text(encoding="utf-8")
            return _submit_flink_sql_via_gateway(sql_text)
        if FLINK_RESUBMIT_CMD:
            completed = subprocess.run(
                shlex.split(FLINK_RESUBMIT_CMD),
                capture_output=True,
                text=True,
                timeout=FLINK_RESUBMIT_WAIT_SECONDS,
            )
            ok = completed.returncode == 0
            return {
                "ok": ok,
                "method": "command",
                "returncode": completed.returncode,
                "output": tail_text(completed.stdout, 20),
                "error": tail_text(completed.stderr, 20) if not ok else "",
            }
        return {
            "ok": False,
            "method": "none",
            "error": (
                "Flink job is missing and no re-submission path is configured in Airflow; "
                "the compose 'flink-submit' sidecar re-runs platform/flink/submit.sh every "
                "180s. Set FLINK_SQL_GATEWAY_URL (if the SQL Gateway is enabled) or "
                "FLINK_RESUBMIT_CMD for an Airflow-side automatic re-submission."
            ),
        }
    except Exception as exc:  # noqa: BLE001 - surfaced in the heartbeat detail
        return {"ok": False, "method": "error", "error": repr(exc)}


def ensure_flink_job() -> dict[str, Any]:
    """Ensure the Flink SQL job is running, re-submitting it when missing."""
    job = find_flink_job()
    if job and str(job.get("state", "")).upper() == "RUNNING":
        return {"ok": True, "resubmitted": False, "job": job}
    if job:
        # Job exists but is not RUNNING (restarting/failing); don't resubmit blindly.
        return {"ok": False, "resubmitted": False, "job": job, "error": f"job state is {job.get('state')}"}
    resubmission = resubmit_flink_job()
    if resubmission.get("ok"):
        time.sleep(5)
        job = find_flink_job()
        return {
            "ok": bool(job) and str(job.get("state", "")).upper() == "RUNNING",
            "resubmitted": True,
            "resubmission": resubmission,
            "job": job,
        }
    return {"ok": False, "resubmitted": False, "resubmission": resubmission, "job": None}


# ---------------------------------------------------------------------------
# Ops tables (heartbeats / job runs)
# ---------------------------------------------------------------------------

def write_heartbeat(component: str, status: str, detail: Mapping[str, Any] | None = None) -> None:
    """Upsert ``ops.pipeline_heartbeat`` for ``component`` with the current time."""
    with db_connection(autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ops.pipeline_heartbeat (component, status, last_seen, detail)
                VALUES (%s, %s, now(), %s)
                ON CONFLICT (component) DO UPDATE
                   SET status = excluded.status,
                       last_seen = excluded.last_seen,
                       detail = excluded.detail
                """,
                (component, status, psycopg2_json(dict(detail or {}))),
            )


def read_heartbeat(component: str) -> dict[str, Any] | None:
    """Read one ``ops.pipeline_heartbeat`` row (``None`` when absent)."""
    rows = execute_fetch_all(
        "SELECT component, status, last_seen, detail FROM ops.pipeline_heartbeat WHERE component = %s",
        (component,),
    )
    return rows[0] if rows else None


def record_job_run(
    job_name: str,
    run_id: str,
    status: str,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    detail: Mapping[str, Any] | None = None,
) -> None:
    """Upsert ``ops.job_runs`` for a DAG/task execution."""
    with db_connection(autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ops.job_runs (job_name, run_id, status, started_at, finished_at, detail)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (job_name, run_id) DO UPDATE
                   SET status = excluded.status,
                       started_at = coalesce(excluded.started_at, ops.job_runs.started_at),
                       finished_at = excluded.finished_at,
                       detail = excluded.detail
                """,
                (
                    job_name,
                    run_id,
                    status,
                    started_at,
                    finished_at,
                    psycopg2_json(dict(detail or {})),
                ),
            )


def mark_scrape_batch(
    batch_id: str,
    status: str,
    *,
    row_count: int | None = None,
    detail: Mapping[str, Any] | None = None,
    finished: bool = False,
    conn=None,
) -> None:
    """Create/update a ``raw.scrape_batches`` row (idempotent for Airflow retries)."""
    owns_connection = conn is None
    if owns_connection:
        conn = pg_connect(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO raw.scrape_batches
                    (batch_id, started_at, finished_at, source, row_count, status, detail)
                VALUES (%s, now(), %s, 'kavach', %s, %s, %s)
                ON CONFLICT (batch_id) DO UPDATE
                   SET finished_at = excluded.finished_at,
                       row_count = COALESCE(excluded.row_count, raw.scrape_batches.row_count),
                       status = excluded.status,
                       detail = COALESCE(raw.scrape_batches.detail, '{}'::jsonb)
                                || COALESCE(excluded.detail, '{}'::jsonb)
                """,
                (
                    batch_id,
                    utc_now() if finished else None,
                    row_count or 0,
                    status,
                    psycopg2_json(dict(detail or {})),
                ),
            )
    finally:
        if owns_connection:
            conn.close()


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

def log(message: str) -> None:
    """Print with the platform prefix (visible in Airflow task logs)."""
    print(f"[{APP_NAME}] {message}", flush=True)


def config_summary() -> dict[str, Any]:
    """Non-secret configuration snapshot for logging/debugging."""
    postgres = postgres_config()
    postgres.pop("password", None)  # never leak the credential
    return {
        "repo_dir": str(REPO_DIR),
        "data_dir": str(DATA_DIR),
        "postgres": postgres,
        "postgres_password_set": bool(POSTGRES_PASSWORD),
        "kafka_bootstrap": KAFKA_BOOTSTRAP,
        "dbt_bin": DBT_BIN,
        "dbt_project_dir": str(DBT_PROJECT_DIR),
        "jdbc_jar": str(JDBC_JAR),
        "flink_rest_url": FLINK_REST_URL,
        "flink_job_name": FLINK_JOB_NAME,
        "timezone": str(IST),
    }


def main() -> int:
    """``python odisha_common.py`` prints the resolved configuration."""
    print(json.dumps(config_summary(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def optional_schedule(cron: str) -> str | None:
    return cron if os.environ.get("ODISHA_SCHEDULES", "off").lower() == "on" else None
