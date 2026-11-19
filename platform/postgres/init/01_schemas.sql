-- 01_schemas.sql — databases + schemas for the Odisha outage platform.
--
-- Runs on the official postgres:16-alpine image entrypoint as $POSTGRES_USER
-- (superuser) connected to $POSTGRES_DB (odisha), i.e. via `psql -f`.
-- Idempotent: safe to re-run against an existing cluster.
--
-- NOTE: this file contains one psql meta-command (\gexec) because CREATE
-- DATABASE cannot run inside a transaction block and Postgres has no
-- CREATE DATABASE IF NOT EXISTS. The official postgres image always runs
-- *.sql init files with psql, so this is safe. If you execute this file with a
-- non-psql SQL runner, replace the \gexec block with:
--
--   CREATE EXTENSION IF NOT EXISTS dblink;
--   DO $$
--   BEGIN
--     IF NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'airflow') THEN
--       PERFORM dblink_exec('dbname=' || quote_ident(current_database()),
--                           'CREATE DATABASE airflow');
--     END IF;
--   END $$;

-- ---------------------------------------------------------------------------
-- 1. Airflow metadata database (kept separate from the analytics database).
--    compose/Airflow uses: postgresql+psycopg2://odisha:<pw>@postgres:5432/airflow
-- ---------------------------------------------------------------------------
SELECT 'CREATE DATABASE airflow'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'airflow')
\gexec

-- ---------------------------------------------------------------------------
-- 2. Schemas in the "odisha" database.
--    raw       : land-zone tables written by Airflow loaders (DDL: 02_raw.sql)
--    staging   : dbt staging views
--    analytics : dbt analytics tables (facts, reliability, risk, live snapshot)
--    marts     : dbt API-facing marts (map dots, cause rollups)
--    realtime  : Spark Structured Streaming / Flink sinks, read by the Go API
--    ml        : Spark batch ML outputs
--    ops       : pipeline heartbeats / job runs
--    seeds     : dbt seed tables (district_profile)
-- ---------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS analytics;
CREATE SCHEMA IF NOT EXISTS marts;
CREATE SCHEMA IF NOT EXISTS realtime;
CREATE SCHEMA IF NOT EXISTS ml;
CREATE SCHEMA IF NOT EXISTS ops;
CREATE SCHEMA IF NOT EXISTS seeds;

COMMENT ON SCHEMA raw       IS 'Landing zone: scraped official outages, reference mappings, user reports, weather (Airflow writes).';
COMMENT ON SCHEMA staging   IS 'dbt staging views: normalization, parsing, district resolution, geocoding.';
COMMENT ON SCHEMA analytics IS 'dbt analytics tables: dims, deduped facts, reliability, live snapshot, risk.';
COMMENT ON SCHEMA marts     IS 'dbt API-facing marts: map dots and cause rollups (Go API reads).';
COMMENT ON SCHEMA realtime  IS 'Near-real-time metrics written by Spark streaming/Flink, read by the Go API.';
COMMENT ON SCHEMA ml        IS 'Batch ML outputs written by Spark.';
COMMENT ON SCHEMA ops       IS 'Operational pipeline bookkeeping (heartbeats, job runs).';
COMMENT ON SCHEMA seeds     IS 'dbt seed tables (district_profile).';
