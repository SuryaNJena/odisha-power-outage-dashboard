# syntax=docker/dockerfile:1
#
# Odisha power-outage platform -- Apache Airflow image.
#
# BUILD FROM THE REPOSITORY ROOT (the build context must contain scrap.py,
# process.py and data/mappings/):
#
#   docker build -f platform/airflow/Dockerfile -t odisha-airflow:2.10.5 .
#
# Runtime (configured by platform/docker-compose.yml, owned by the lead):
#   * `airflow standalone` with LocalExecutor, metadata DB = postgres/airflow;
#   * Apache Airflow 2.10.5 on Python 3.12;
#   * OpenJDK 17 for Spark 3.5.4, Java home pre-set;
#   * isolated dbt venv at /opt/dbt (`dbt-postgres~=1.9`) so dbt's dependency tree
#     can never clash with Airflow's;
#   * PySpark + Postgres JDBC driver baked in for the daily spark-submit job.

ARG AIRFLOW_VERSION=2.10.5
ARG PYTHON_VERSION=3.12
# NOTE: the JDBC jar path is part of the platform contract.
ARG POSTGRES_JDBC_VERSION=42.7.4

FROM apache/airflow:${AIRFLOW_VERSION}-python${PYTHON_VERSION}

# Re-declare build args so they are usable inside RUN steps.
ARG AIRFLOW_VERSION
ARG PYTHON_VERSION
ARG POSTGRES_JDBC_VERSION

USER root
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Kolkata \
    JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64 \
    JDBC_DRIVER_VERSION=${POSTGRES_JDBC_VERSION} \
    JDBC_JAR=/opt/jdbc/postgresql-42.7.4.jar \
    DBT_BIN=/opt/dbt/bin/dbt \
    PYTHONPATH=/opt/airflow/scripts \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ---------------------------------------------------------------------------
# OS packages: OpenJDK 17 (Spark 3.5 requires Java 8/11/17), curl+procps for
# Airflow/Spark/BashOperator health and process management.
# ---------------------------------------------------------------------------
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        openjdk-17-jre-headless \
        curl \
        procps \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# The python:* base has `python`; make sure `python3` is on PATH for operators.
RUN command -v python3 >/dev/null 2>&1 || ln -sf "$(command -v python)" /usr/local/bin/python3

# ---------------------------------------------------------------------------
# Isolated dbt virtualenv (/opt/dbt).  Airflow never imports this venv; the DAGs
# shell out to /opt/dbt/bin/dbt so dbt and Airflow dependencies stay separated.
# ---------------------------------------------------------------------------
COPY platform/airflow/requirements-dbt.txt /tmp/requirements-dbt.txt
RUN python -m venv /opt/dbt \
 && /opt/dbt/bin/pip install --no-cache-dir --upgrade pip setuptools wheel \
 && /opt/dbt/bin/pip install --no-cache-dir -r /tmp/requirements-dbt.txt \
 && rm -rf /tmp/requirements-dbt.txt

# ---------------------------------------------------------------------------
# Postgres JDBC driver for Spark (contract path /opt/jdbc/postgresql-42.7.4.jar).
# ---------------------------------------------------------------------------
RUN mkdir -p /opt/jdbc \
 && curl -fsSL --retry 5 --retry-delay 3 --connect-timeout 20 \
        -o "/opt/jdbc/postgresql-${POSTGRES_JDBC_VERSION}.jar" \
        "https://repo1.maven.org/maven2/org/postgresql/postgresql/${POSTGRES_JDBC_VERSION}/postgresql-${POSTGRES_JDBC_VERSION}.jar" \
 && test -s "/opt/jdbc/postgresql-${POSTGRES_JDBC_VERSION}.jar"

# ---------------------------------------------------------------------------
# Airflow-environment Python dependencies (pyspark, psycopg2, scrapers, kafka).
# Pinned versions match the Apache Airflow 2.10.5 constraints file, so the
# resolution is reproducible and does not disturb the base image.
# ---------------------------------------------------------------------------
COPY platform/airflow/requirements.txt /tmp/requirements.txt
# The Airflow base image wraps `pip` with a root guard; `python -m pip` bypasses
# that wrapper and is the supported way to install into the image as root here.
RUN python -m pip install --no-cache-dir -r /tmp/requirements.txt \
        --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-${PYTHON_VERSION}.txt" \
 || python -m pip install --no-cache-dir -r /tmp/requirements.txt \
 && rm -f /tmp/requirements.txt

# ---------------------------------------------------------------------------
# Platform code and offline fallbacks.
#   * DAGs -> Airflow's dags folder;
#   * scripts -> /opt/airflow/scripts (also importable via PYTHONPATH);
#   * scrap.py / process.py -> /opt/odisha (reused by scrape_official/load_official;
#     shadowed by the repo bind-mount in compose, identical content);
#   * data/mappings -> /opt/airflow/mappings (fallback when the shared
#     odisha_app_data volume shadows /opt/odisha/data inside the container).
# ---------------------------------------------------------------------------
COPY platform/airflow/dags/ /opt/airflow/dags/
COPY platform/airflow/scripts/ /opt/airflow/scripts/
COPY scrap.py /opt/odisha/scrap.py
COPY process.py outage_rows.py /opt/odisha/
COPY outage_rows.py /opt/airflow/scripts/
COPY data/mappings/ /opt/airflow/mappings/

# Writable runtime locations.  The shared named volume mount /opt/odisha/data
# inherits this ownership on first use; compose may run as root (see README).
RUN mkdir -p \
        /opt/odisha/data/mart \
        /opt/odisha/data/dbt/target \
        /opt/odisha/data/dbt/logs \
        /opt/odisha/data/lake \
        /opt/odisha/scrappedData \
        /opt/airflow/scripts \
        /opt/airflow/mappings \
 && chown -R airflow:root \
        /opt/dbt /opt/jdbc /opt/odisha \
        /opt/airflow/dags /opt/airflow/scripts /opt/airflow/mappings \
 && chmod -R a+rX /opt/dbt /opt/jdbc

# Airflow defaults required by the platform contract (compose may reiterate them).
ENV AIRFLOW__CORE__EXECUTOR=LocalExecutor \
    AIRFLOW__CORE__LOAD_EXAMPLES=False \
    AIRFLOW__CORE__DAGS_ARE_PAUSED_AT_CREATION=False \
    AIRFLOW__WEBSERVER__EXPOSE_CONFIG=False

# Conservative Spark runtime defaults for the 2 GB Airflow container: the daily
# batch runs as a local-mode spark-submit subprocess inside Airflow.  Override via
# compose/env (``;``-separated --conf values, see odisha_common.SPARK_EXTRA_CONF).
ENV SPARK_MASTER="local[2]" \
    SPARK_EXTRA_CONF="spark.ui.enabled=false;spark.sql.shuffle.partitions=8;spark.driver.memory=768m" \
    SPARK_SUBMIT_TIMEOUT=3600

# Build-time sanity checks for everything the DAGs shell out to.
RUN java -version \
 && bash --version | head -n 1 \
 && curl --version | head -n 1 \
 && python3 --version \
 && /opt/dbt/bin/dbt --version \
 && ls -l /opt/jdbc/postgresql-42.7.4.jar

# Run as the Airflow user by default (compose overrides to root for the rw repo
# bind mount in this demo deployment -- documented in platform/airflow/README.md).
USER airflow

WORKDIR /opt/airflow
