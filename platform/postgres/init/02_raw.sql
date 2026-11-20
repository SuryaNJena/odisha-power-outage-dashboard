-- 02_raw.sql — raw / realtime / ops / ml tables for the Odisha outage platform.
--
-- Runs after 01_schemas.sql on the odisha database. Idempotent (IF NOT EXISTS).
-- Column names and types follow platform/CONTRACT.md exactly; dbt models in
-- platform/dbt/models/sources.yml declare these as sources.
--
-- Writers:
--   raw.*    : Airflow loaders (official ingest, weather ingest, bootstrap)
--   realtime.*: Spark Structured Streaming / Flink JDBC sinks
--   ops.*    : Airflow + Spark jobs
--   ml.*     : Spark daily batch (dbt only reads ml.* for reference)
--
-- Timestamps are timestamptz (UTC on the wire); the display timezone is
-- Asia/Kolkata and dbt converts at the presentation/aggregation boundary.

-- ===========================================================================
-- raw (landing zone)
-- ===========================================================================

CREATE TABLE IF NOT EXISTS raw.scrape_batches (
    batch_id    text PRIMARY KEY,                       -- ISO8601 UTC, e.g. 2026-11-19T12:30:00Z
    started_at  timestamptz NOT NULL,
    finished_at timestamptz,
    source      text NOT NULL DEFAULT 'kavach',
    row_count   integer DEFAULT 0,
    status      text NOT NULL DEFAULT 'running',         -- running | success | failed
    detail      jsonb
);
CREATE INDEX IF NOT EXISTS idx_scrape_batches_status
    ON raw.scrape_batches (status, started_at DESC);

CREATE TABLE IF NOT EXISTS raw.official_outages (
    id          bigserial PRIMARY KEY,
    batch_id    text NOT NULL,
    row_hash    text NOT NULL,
    source_file text,
    schema_type text NOT NULL,                           -- 'circle' | 'division'
    circle      text,
    division    text,
    subdivision text,
    section     text,
    otype       text,
    gss         text,
    voltage     text,
    feeder      text,
    start_raw   text,
    end_raw     text,
    reason      text,
    areas       text,
    remarks     text,
    raw_row     jsonb,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (batch_id, row_hash)
);
CREATE INDEX IF NOT EXISTS idx_official_outages_batch
    ON raw.official_outages (batch_id);
CREATE INDEX IF NOT EXISTS idx_official_outages_row_hash
    ON raw.official_outages (row_hash);

CREATE TABLE IF NOT EXISTS raw.district_aliases (
    alias    text PRIMARY KEY,                           -- normalized alias (lower alnum only)
    district text NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_district_aliases_district
    ON raw.district_aliases (district);

CREATE TABLE IF NOT EXISTS raw.circle_exact (
    circle   text PRIMARY KEY,                           -- upper-case circle name
    district text NOT NULL
);

CREATE TABLE IF NOT EXISTS raw.district_centroids (
    district text PRIMARY KEY,
    lat      double precision,
    lon      double precision
);

CREATE TABLE IF NOT EXISTS raw.station_geocodes (
    station_norm text NOT NULL,                          -- normalized station name (lower alnum only)
    district     text NOT NULL,
    lat          double precision,
    lon          double precision,
    display      text,
    PRIMARY KEY (station_norm, district)
);
CREATE INDEX IF NOT EXISTS idx_station_geocodes_district
    ON raw.station_geocodes (district);

CREATE TABLE IF NOT EXISTS raw.user_reports (
    id           bigserial PRIMARY KEY,
    report_id    uuid NOT NULL UNIQUE,
    district     text,
    gss          text,
    feeder       text,
    comment      text,
    area         text,
    severity     integer NOT NULL DEFAULT 3,
    reporter     text,
    lat          double precision,
    lon          double precision,
    reported_at  timestamptz NOT NULL DEFAULT now(),
    source       text NOT NULL DEFAULT 'web',
    kafka_offset bigint,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_user_reports_district_reported
    ON raw.user_reports (district, reported_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_reports_reported_at
    ON raw.user_reports (reported_at DESC);

CREATE TABLE IF NOT EXISTS raw.weather_hourly (
    id              bigserial PRIMARY KEY,
    district        text NOT NULL,
    observed_at     timestamptz NOT NULL,
    temperature_c   real,
    precipitation_mm real,
    wind_kph        real,
    humidity_pct    real,
    weather_code    integer,
    source          text NOT NULL DEFAULT 'open-meteo',
    fetched_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (district, observed_at)
);
CREATE INDEX IF NOT EXISTS idx_weather_hourly_observed_at
    ON raw.weather_hourly (observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_weather_hourly_district_observed
    ON raw.weather_hourly (district, observed_at DESC);

-- ===========================================================================
-- realtime (Spark streaming / Flink sinks; read by the Go API)
-- ===========================================================================

CREATE TABLE IF NOT EXISTS realtime.district_live_metrics (
    district            text PRIMARY KEY,
    report_count        integer NOT NULL DEFAULT 0,      -- last 15 min
    unique_reporters    integer NOT NULL DEFAULT 0,
    official_outages    integer NOT NULL DEFAULT 0,
    unconfirmed_reports integer NOT NULL DEFAULT 0,
    risk_score          real,
    window_start        timestamptz,
    window_end          timestamptz,
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS realtime.report_minute (
    bucket        timestamptz NOT NULL,
    district      text NOT NULL,
    report_count  integer NOT NULL,
    avg_severity  real,
    PRIMARY KEY (bucket, district)
);
CREATE INDEX IF NOT EXISTS idx_report_minute_bucket
    ON realtime.report_minute (bucket DESC);

CREATE TABLE IF NOT EXISTS realtime.alerts (
    alert_id     text PRIMARY KEY,
    district     text,
    alert_type   text NOT NULL,                          -- report_spike | high_severity | official_outage
    severity     text NOT NULL,                          -- info | warning | critical
    message      text NOT NULL,
    report_count integer,
    window_start timestamptz,
    window_end   timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    source       text NOT NULL DEFAULT 'flink'
);
CREATE INDEX IF NOT EXISTS idx_alerts_created_at
    ON realtime.alerts (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_district_created
    ON realtime.alerts (district, created_at DESC);

CREATE TABLE IF NOT EXISTS realtime.flink_report_windows (
    district         text,
    window_start     timestamptz,
    window_end       timestamptz,
    report_count     integer,
    unique_reporters integer,
    avg_severity     real,
    PRIMARY KEY (district, window_start)
);

-- ===========================================================================
-- ops (pipeline bookkeeping)
-- ===========================================================================

CREATE TABLE IF NOT EXISTS ops.pipeline_heartbeat (
    component text PRIMARY KEY,
    status    text NOT NULL,
    last_seen timestamptz NOT NULL DEFAULT now(),
    detail    jsonb
);

CREATE TABLE IF NOT EXISTS ops.job_runs (
    job_name    text,
    run_id      text,
    status      text NOT NULL,
    started_at  timestamptz,
    finished_at timestamptz,
    detail      jsonb,
    PRIMARY KEY (job_name, run_id)
);
CREATE INDEX IF NOT EXISTS idx_job_runs_started_at
    ON ops.job_runs (started_at DESC);

-- ===========================================================================
-- ml (written by the Spark daily batch; the platform provisions the tables so
-- Spark JDBC overwrite/insert can target them without DDL privileges)
-- ===========================================================================

CREATE TABLE IF NOT EXISTS ml.outage_clusters (
    cluster_id        integer,
    lat               double precision,
    lon               double precision,
    outage_count      integer,
    dominant_district text,
    dominant_hour     integer
);
CREATE INDEX IF NOT EXISTS idx_outage_clusters_cluster
    ON ml.outage_clusters (cluster_id);

CREATE TABLE IF NOT EXISTS ml.etr_model_metrics (
    model_date   date,
    mae_minutes  double precision,
    r2           double precision,
    sample_count integer
);
CREATE INDEX IF NOT EXISTS idx_etr_model_metrics_date
    ON ml.etr_model_metrics (model_date DESC);

CREATE TABLE IF NOT EXISTS ml.district_risk_forecast (
    district        text,
    forecast_date   date,
    risk_score      double precision,
    risk_level      text,
    expected_outages integer,
    max_precip_mm   double precision,
    max_wind_kph    double precision
);
CREATE INDEX IF NOT EXISTS idx_district_risk_forecast_date
    ON ml.district_risk_forecast (forecast_date DESC);
