-- ============================================================================
-- Odisha Power Outage Dashboard - Flink SQL NRT user-report alerting
-- ============================================================================
-- Target  : Apache Flink 1.20.1 (SQL Client / Table planner), 1 job
-- Job name: odisha-user-report-alerts  (SET 'pipeline.name' below)
-- Submit  : platform/flink/submit.sh renders the KAFKA_BOOTSTRAP and
--           POSTGRES_PASSWORD environment variables, then runs
--             sql-client.sh -Drest.address=... -Drest.port=... -f /tmp/job.sql
--
-- Pipeline (single Flink job thanks to EXECUTE STATEMENT SET):
--
--   Kafka topic user-reports (JSON, 3 partitions)
--        |
--        v
--   user_reports            -- event time reported_at (TIMESTAMP_LTZ(3)),
--        |                     watermark = reported_at - 30 s,
--        |                     json.ignore-parse-errors, ISO-8601 timestamps
--        v
--   report_windows          -- 10-minute tumbling windows per district
--        |                     COUNT(DISTINCT report_id)  -> report_count
--        |                     COUNT(DISTINCT reporter)   -> unique_reporters
--        |                     AVG(severity)              -> avg_severity
--        |
--        +--> report_spike_alerts     --> Kafka topic outage-alerts
--        |       report_count >= 3  (3..7 warning, >= 8 critical)
--        |
--        +--> high_severity_alerts    --> Kafka topic outage-alerts
--        |       avg_severity >= 4
--        |
--        +--> INSERT INTO flink_report_windows (JDBC, PostgreSQL)
--                realtime.flink_report_windows (idempotent upsert)
--
-- Alert JSON (Kafka outage-alerts), window times as ISO-8601 UTC strings:
--   {"alert_id":"flink-<district>-<window_end_epoch_ms>", "district":"Khordha",
--    "alert_type":"report_spike", "severity":"warning", "message":"...",
--    "report_count":7, "window_start":"2026-11-19T12:30:00.000Z",
--    "window_end":"2026-11-19T12:40:00.000Z"}
--
-- Design notes / deliberate deviations (see platform/flink/README.md):
--   * report_id de-duplication is done by the window aggregate itself using
--     COUNT(DISTINCT report_id). A separate ROW_NUMBER() de-duplication view
--     CANNOT be used here: in Flink 1.20 a Window TVF aggregation rejects
--     changelog (update/delete) input such as the one a Deduplicate/Rank node
--     produces (FLINK-22909, FLIP-392). Folding de-duplication into the
--     aggregate keeps the input append-only and produces identical counts:
--     replayed reports with the same report_id are counted once per window.
--   * alert_id epoch milliseconds are computed with
--     UNIX_TIMESTAMP(DATE_FORMAT(CAST(window_end AS TIMESTAMP), fmt), fmt)*1000
--     instead of UNIX_TIMESTAMP(CAST(window_end AS STRING)): CAST to STRING
--     appends a fractional part ("...:00.000"), while the one-argument
--     UNIX_TIMESTAMP only accepts "yyyy-MM-dd HH:mm:ss" and would silently
--     return Long.MIN_VALUE. Both DATE_FORMAT and UNIX_TIMESTAMP use
--     table.local-time-zone, so the round-trip is time-zone correct; tumbling
--     window boundaries are whole seconds, so *1000 is exact.
--   * window_start / window_end are cast to TIMESTAMP_LTZ(3) so that the Kafka
--     JSON sink emits "...Z" instants and the JDBC sink writes timestamptz.
--   * the JDBC sink declares PRIMARY KEY (district, window_start) NOT ENFORCED
--     => the connector runs in upsert mode (PostgreSQL
--     INSERT .. ON CONFLICT .. DO UPDATE SET). The PostgreSQL table already has
--     that primary key, so plain append-only inserts would crash the job with a
--     duplicate-key error whenever a window is re-emitted after a restart.
--     Upsert mode makes window writes idempotent. Remove the PRIMARY KEY line
--     to force the append-only behaviour described in the contract, if needed.
--   * EXECUTE STATEMENT SET submits all three INSERTs as ONE job named
--     odisha-user-report-alerts and lets the optimizer share the Kafka source
--     (important: three separate jobs using the same consumer group would keep
--     re-balancing partitions away from each other and each would see only a
--     subset of the reports).
--   * two Kafka sink table objects point at the same topic because report_spike
--     and high_severity are emitted by two separate INSERT statements; using
--     two table objects keeps the statement set valid in every Flink 1.20
--     planner without relying on multiple inserts into one sink table.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 0. Session configuration
-- ---------------------------------------------------------------------------
SET 'pipeline.name' = 'odisha-user-report-alerts';

-- Store/emit/compare all event and window timestamps in UTC. Container TZ is
-- Asia/Kolkata (compose), so this must be set explicitly: TIMESTAMP_LTZ <->
-- TIMESTAMP casts and DATE_FORMAT/UNIX_TIMESTAMP use this session time zone.
SET 'table.local-time-zone' = 'UTC';
SET 'execution.runtime-mode' = 'streaming';

-- Mark Kafka partitions without traffic as idle after 30 s so that watermarks
-- keep advancing and the 10-minute windows close on time even when only a
-- subset of the 3 partitions receives reports.
SET 'table.exec.source.idle-timeout' = '30s';

-- Tolerate transient Kafka/PostgreSQL outages. The compose jobmanager sets
-- execution.checkpointing.interval=30s globally; checkpointing is not repeated
-- here so the cluster configuration stays authoritative.
SET 'restart-strategy.type' = 'failure-rate';
SET 'restart-strategy.failure-rate.max-failures-per-interval' = '10';
SET 'restart-strategy.failure-rate.failure-rate-interval' = '10min';
SET 'restart-strategy.failure-rate.delay' = '5s';

-- ---------------------------------------------------------------------------
-- 1. Source: user-reports (flat JSON produced by the Go API)
--    Payload contract:
--      {"report_id":"uuid","district":"Khordha","gss":"","feeder":"",
--       "comment":"no power","severity":3,"reporter":"web","lat":null,
--       "lon":null,"reported_at":"2026-11-19T12:34:56Z"}
--    reported_at is ISO-8601 (RFC 3339) with a trailing Z, so TIMESTAMP_LTZ(3)
--    + json.timestamp-format.standard=ISO-8601 is the only combination the
--    Flink JSON format parses correctly (a TIMESTAMP column would reject the
--    trailing Z).
-- ---------------------------------------------------------------------------
CREATE TABLE user_reports (
  report_id   STRING,
  district    STRING,
  gss         STRING,
  feeder      STRING,
  `comment`   STRING,   -- COMMENT is a reserved word in Flink SQL: quote it
  severity    INT,
  reporter    STRING,
  lat         DOUBLE,
  lon         DOUBLE,
  reported_at TIMESTAMP_LTZ(3),
  WATERMARK FOR reported_at AS reported_at - INTERVAL '30' SECOND
) WITH (
  'connector'                          = 'kafka',
  'topic'                              = 'user-reports',
  'properties.bootstrap.servers'       = '${KAFKA_BOOTSTRAP}',
  'properties.group.id'                = 'flink-user-report-alerts',
  'scan.startup.mode'                  = 'earliest-offset',
  'format'                             = 'json',
  'json.ignore-parse-errors'           = 'true',
  'json.fail-on-missing-field'         = 'false',
  'json.timestamp-format.standard'     = 'ISO-8601'
);

-- ---------------------------------------------------------------------------
-- 2. Kafka sink: outage-alerts (one "spike" INSERT + one "high severity"
--    INSERT => two table objects for the same physical topic)
-- ---------------------------------------------------------------------------
CREATE TABLE outage_alerts_spike (
  alert_id     STRING,
  district     STRING,
  alert_type   STRING,
  severity     STRING,
  message      STRING,
  report_count INT,
  window_start TIMESTAMP_LTZ(3),
  window_end   TIMESTAMP_LTZ(3)
) WITH (
  'connector'                      = 'kafka',
  'topic'                          = 'outage-alerts',
  'properties.bootstrap.servers'   = '${KAFKA_BOOTSTRAP}',
  'format'                         = 'json',
  'json.timestamp-format.standard' = 'ISO-8601',
  'sink.delivery-guarantee'        = 'at-least-once'
);

CREATE TABLE outage_alerts_severity (
  alert_id     STRING,
  district     STRING,
  alert_type   STRING,
  severity     STRING,
  message      STRING,
  report_count INT,
  window_start TIMESTAMP_LTZ(3),
  window_end   TIMESTAMP_LTZ(3)
) WITH (
  'connector'                      = 'kafka',
  'topic'                          = 'outage-alerts',
  'properties.bootstrap.servers'   = '${KAFKA_BOOTSTRAP}',
  'format'                         = 'json',
  'json.timestamp-format.standard' = 'ISO-8601',
  'sink.delivery-guarantee'        = 'at-least-once'
);

-- ---------------------------------------------------------------------------
-- 3. JDBC sink: realtime.flink_report_windows (PostgreSQL)
--    PRIMARY KEY NOT ENFORCED => upsert mode (INSERT .. ON CONFLICT .. DO
--    UPDATE SET) => re-emitted windows are idempotent after a restart.
--    The password placeholder is rendered from the POSTGRES_PASSWORD
--    environment variable by submit.sh (Flink SQL cannot read env vars).
-- ---------------------------------------------------------------------------
CREATE TABLE flink_report_windows (
  district         STRING,
  window_start     TIMESTAMP(3),
  window_end       TIMESTAMP(3),
  report_count     INT,
  unique_reporters INT,
  avg_severity     FLOAT,
  PRIMARY KEY (district, window_start) NOT ENFORCED
) WITH (
  'connector'                  = 'jdbc',
  -- options=-c timezone=UTC keeps pgjdbc from shifting the UTC window
  -- boundaries by the JVM/container time zone (Asia/Kolkata) when writing
  -- TIMESTAMP(3) values into a timestamptz column. The JDBC connector does not
  -- support TIMESTAMP_LTZ columns, hence the explicit zone pin.
  'url'                        = 'jdbc:postgresql://postgres:5432/odisha?options=-c%20timezone%3DUTC',
  'table-name'                 = 'realtime.flink_report_windows',
  'username'                   = 'odisha',
  'password'                   = '${POSTGRES_PASSWORD}',
  'driver'                     = 'org.postgresql.Driver',
  'sink.buffer-flush.max-rows' = '100',
  'sink.buffer-flush.interval' = '1s',
  'sink.max-retries'           = '3'
);

-- ---------------------------------------------------------------------------
-- 4. De-duplicated 10-minute tumbling window metrics per district
--
--    The window TVF consumes the append-only Kafka source directly; that is a
--    hard requirement of Flink 1.20 window TVF aggregation. De-duplication by
--    report_id is therefore expressed as COUNT(DISTINCT report_id) inside the
--    aggregate (one count per distinct report in the window), which is also
--    the only way to keep the source append-only.
-- ---------------------------------------------------------------------------
CREATE VIEW report_windows AS
SELECT
  CAST(window_start AS TIMESTAMP_LTZ(3)) AS window_start,
  CAST(window_end   AS TIMESTAMP_LTZ(3)) AS window_end,
  -- Epoch milliseconds of window_end, used as the alert_id suffix.
  -- window_end is TIMESTAMP(3) in the session time zone (UTC) for both
  -- TIMESTAMP and TIMESTAMP_LTZ event times, so:
  --   CAST -> TIMESTAMP(3): identity / instant rendered in session tz
  --   DATE_FORMAT(..., 'yyyy-MM-dd HH:mm:ss'): whole-second UTC wall clock
  --   UNIX_TIMESTAMP(str, same_format): parses with table.local-time-zone
  -- => exact epoch seconds; *1000 is exact because windows are minute-aligned.
  UNIX_TIMESTAMP(
    DATE_FORMAT(CAST(window_end AS TIMESTAMP(3)), 'yyyy-MM-dd HH:mm:ss'),
    'yyyy-MM-dd HH:mm:ss'
  ) * 1000 AS window_end_epoch_ms,
  district,
  CAST(COUNT(DISTINCT report_id) AS INT)   AS report_count,
  CAST(COUNT(DISTINCT reporter)  AS INT)   AS unique_reporters,
  -- CAST to DOUBLE so AVG always returns a fractional average regardless of
  -- AVG(INT) type inference; it is stored as `real` in PostgreSQL below.
  AVG(CAST(severity AS DOUBLE))            AS avg_severity
FROM TABLE(
  TUMBLE(TABLE user_reports, DESCRIPTOR(reported_at), INTERVAL '10' MINUTES)
)
WHERE district IS NOT NULL          -- rows without a district cannot be geo-attributed
GROUP BY window_start, window_end, district;

-- ---------------------------------------------------------------------------
-- 5. Report-spike alerts: >= 3 reports in 10 minutes
--    3..7  -> warning
--    >= 8  -> critical
-- ---------------------------------------------------------------------------
CREATE VIEW report_spike_alerts AS
SELECT
  CONCAT('flink-', district, '-', CAST(window_end_epoch_ms AS STRING)) AS alert_id,
  district,
  'report_spike' AS alert_type,
  CASE WHEN report_count >= 8 THEN 'critical' ELSE 'warning' END AS severity,
  CONCAT(
    'Report spike in ', district, ': ',
    CAST(report_count AS STRING),
    ' outage reports in the 10-minute window ending ',
    DATE_FORMAT(CAST(window_end AS TIMESTAMP(3)), 'yyyy-MM-dd HH:mm:ss'),
    ' UTC (avg severity ', CAST(CAST(avg_severity AS DECIMAL(4, 2)) AS STRING), ')'
  ) AS message,
  report_count,
  window_start,
  window_end
FROM report_windows
WHERE report_count >= 3;

-- ---------------------------------------------------------------------------
-- 6. High-severity alerts: avg severity >= 4 in the 10-minute window
--    alert_id gets a '-severity' suffix so it cannot collide with the
--    report_spike alert_id of the same district/window (realtime.alerts uses
--    alert_id as primary key in PostgreSQL).
-- ---------------------------------------------------------------------------
CREATE VIEW high_severity_alerts AS
SELECT
  CONCAT('flink-', district, '-', CAST(window_end_epoch_ms AS STRING), '-severity') AS alert_id,
  district,
  'high_severity' AS alert_type,
  CASE WHEN avg_severity >= 4.5 THEN 'critical' ELSE 'warning' END AS severity,
  CONCAT(
    'High severity reports in ', district, ': avg severity ',
    CAST(CAST(avg_severity AS DECIMAL(4, 2)) AS STRING),
    ' across ', CAST(report_count AS STRING),
    ' reports in the 10-minute window ending ',
    DATE_FORMAT(CAST(window_end AS TIMESTAMP(3)), 'yyyy-MM-dd HH:mm:ss'),
    ' UTC'
  ) AS message,
  report_count,
  window_start,
  window_end
FROM report_windows
WHERE avg_severity >= 4;

-- ---------------------------------------------------------------------------
-- 7. Single job: both alert streams to Kafka + window metrics to PostgreSQL
--    STATEMENT SET => one Flink job, shared Kafka source / window aggregate.
-- ---------------------------------------------------------------------------
EXECUTE STATEMENT SET
BEGIN

  INSERT INTO outage_alerts_spike
  SELECT alert_id, district, alert_type, severity, message,
         report_count, window_start, window_end
  FROM report_spike_alerts;

  INSERT INTO outage_alerts_severity
  SELECT alert_id, district, alert_type, severity, message,
         report_count, window_start, window_end
  FROM high_severity_alerts;

  INSERT INTO flink_report_windows
  SELECT district,
         CAST(window_start AS TIMESTAMP(3)),
         CAST(window_end   AS TIMESTAMP(3)),
         report_count,
         unique_reporters,
         CAST(avg_severity AS FLOAT)
  FROM report_windows;

END;
