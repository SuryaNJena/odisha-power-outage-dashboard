# Kafka + Flink NRT user-report pipeline

Streaming layer of the Odisha power-outage platform. It consumes user outage
reports from Kafka, de-duplicates and aggregates them per district in
10-minute tumbling event-time windows, publishes `outage-alerts` and persists
window metrics into PostgreSQL for the Go API / dashboard.

```
Go API --POST /api/report--> Kafka user-reports
                                   |
                        +----------+-----------+
                        |  Flink 1.20.1 SQL    |
                        |  odisha-user-report-alerts
                        +----------+-----------+
                                   |
                 +-----------------+------------------+
                 v                 v                  v
        Kafka outage-alerts  Kafka outage-alerts  Postgres realtime.
        (report_spike)       (high_severity)     flink_report_windows
                 |                 |
                 +--------+--------+
                          v
             Spark streaming (realtime.alerts) / Go API
```

| File | Purpose |
|---|---|
| `../kafka/topics.sh` | creates/verifies the 5 contract topics (run by compose service `kafka-init`) |
| `Dockerfile` | `flink:1.20.1-scala_2.12` + Kafka SQL connector, JDBC connector, PostgreSQL driver (verified downloads into `/opt/flink/lib`) |
| `jobs/user_report_alerts.sql` | the whole job: source, window aggregate, alert views, sinks, `EXECUTE STATEMENT SET` |
| `submit.sh` | renders secrets into the SQL, submits detached via `sql-client.sh -f`, verifies via REST, idempotent |
| `README.md` | this file |

---

## 1. Connector jars (verified on Maven Central, 2026-11-19)

| Artifact | URL | Size | SHA-1 |
|---|---|---|---|
| `flink-sql-connector-kafka-3.3.0-1.20.jar` | `https://repo1.maven.org/maven2/org/apache/flink/flink-sql-connector-kafka/3.3.0-1.20/flink-sql-connector-kafka-3.3.0-1.20.jar` | 5,587,110 B | `9e7e2bb762e6cb489bcc76f2637e824fbb6f08c3` |
| `flink-connector-jdbc-3.4.0-1.20.jar` | `https://repo1.maven.org/maven2/org/apache/flink/flink-connector-jdbc/3.4.0-1.20/flink-connector-jdbc-3.4.0-1.20.jar` | 444,657 B | `ba3da4030b448481f2cf46aa7a0eb270241c5fa1` |
| `postgresql-42.7.4.jar` | `https://repo1.maven.org/maven2/org/postgresql/postgresql/42.7.4/postgresql-42.7.4.jar` | 1,086,687 B | `264310fd7b2cd76738787dc0b9f7ea2e3b11adc1` |

**JDBC connector version correction.** The originally suggested
`flink-connector-jdbc/3.2.0-1.20` **does not exist** on Maven Central (HTTP
404; `maven-metadata.xml` lists `3.2.0-1.18`, `3.2.0-1.19`, `3.3.0-1.19`,
`3.3.0-1.20`, `3.4.0-1.20`). The Flink 1.20 JDBC connector documentation pins
`3.4.0-1.20`, so that is what is baked. `3.3.0-1.20` is a drop-in alternative
(both are the shaded artifact).

**Is the JDBC connector a fat jar?** `flink-connector-jdbc-3.4.0-1.20.jar` is
the Maven artifact named *"Flink : Connectors : JDBC : Shaded"*. Its POM shades
`flink-connector-jdbc-core` **plus all dialect modules**, including
`flink-connector-jdbc-postgres` (the PostgreSQL dialect is therefore present;
`flink-connector-jdbc-core` / `flink-connector-jdbc-postgres` jars are **not**
needed additionally). It is a thin shim over Flink itself (Flink core is
`provided`), which is why it is ~435 KB, not > 1 MB. The Dockerfile enforces
per-artifact size floors: 5 MB for Kafka, 1 MiB for the PostgreSQL driver and
400 KB for the JDBC shaded jar, plus ZIP magic and the published SHA-1. Build
fails on any mismatch, so a truncated download/HTML error page can never end
up in `/opt/flink/lib`.

The Kafka SQL connector is the fat jar (Kafka client + JSON format included),
so no extra `kafka-clients`/`flink-json` jars are required.

## 2. Job graph

`jobs/user_report_alerts.sql` compiles to **one Flink job** named
`odisha-user-report-alerts` (`SET 'pipeline.name'`), thanks to
`EXECUTE STATEMENT SET`:

```
KafkaSource(user-reports, group flink-user-report-alerts,
            earliest-offset, 3 partitions)
  -> WatermarkAssigner (reported_at - 30 s, TIMESTAMP_LTZ(3))
  -> WindowAggregate TUMBLE(10 min) GROUP BY window_start, window_end, district
        COUNT(DISTINCT report_id) -> report_count      (de-dup by report_id)
        COUNT(DISTINCT reporter)  -> unique_reporters
        AVG(CAST(severity AS DOUBLE)) -> avg_severity
       |
       +-> [filter count >= 3]        -> KafkaSink outage-alerts (report_spike)
       +-> [filter avg_severity >= 4] -> KafkaSink outage-alerts (high_severity)
       +-> JdbcSink realtime.flink_report_windows (upsert)
```

The Kafka source is shared by all three insert branches by the statement-set
optimizer. This matters: three *separate* jobs would all use consumer group
`flink-user-report-alerts` and would rebalance partitions away from each other,
so each job would see only a subset of the reports.

Alert payload (Kafka `outage-alerts`, JSON with ISO-8601 UTC timestamps):

```json
{"alert_id":"flink-Khordha-1763553600000",
 "district":"Khordha","alert_type":"report_spike","severity":"warning",
 "message":"Report spike in Khordha: 7 outage reports in the 10-minute window ending 2026-11-19 12:40:00 UTC (avg severity 3.43)",
 "report_count":7,
 "window_start":"2026-11-19T12:30:00.000Z","window_end":"2026-11-19T12:40:00.000Z"}
```

Severity mapping:

| condition | alert_type | severity |
|---|---|---|
| `3 <= report_count <= 7` | `report_spike` | `warning` |
| `report_count >= 8` | `report_spike` | `critical` |
| `avg_severity >= 4` | `high_severity` | `warning` (`critical` if `avg_severity >= 4.5`) |

`alert_id` is `flink-<district>-<window_end_epoch_ms>`. High-severity alerts
append `-severity` because `realtime.alerts.alert_id` is a PostgreSQL primary
key and both alert kinds can describe the same district/window.

## 3. Design decisions that deviate from the task text (and why)

1. **`report_id` de-duplication is `COUNT(DISTINCT report_id)`, not a
   `ROW_NUMBER()` view.** In Flink 1.20 a Window TVF aggregation only accepts
   append-only input; a `ROW_NUMBER()`-based dedup view produces an
   update/delete changelog and the planner rejects the query
   (`StreamPhysicalWindowAggregate doesn't support consuming update and delete
   changes`). This is the long-standing limitation tracked as
   [FLINK-22909](https://issues.apache.org/jira/browse/FLINK-22909) and
   [FLIP-392](https://cwiki.apache.org/confluence/pages/viewpage.action?pageId=278467029)
   ("Window TVF Aggregation only supports append-only streams", unresolved for
   1.20). Folding distinct-counting into the window aggregate keeps the input
   append-only and produces the same numbers: a replayed report with the same
   `report_id` is counted once per window. `AVG(severity)` is still computed
   over raw rows, so a replayed report can slightly shift `avg_severity`
   (documented limitation; producer retries are rare).
2. **The JDBC sink is idempotent (upsert), not append-only.**
   `realtime.flink_report_windows` has `PRIMARY KEY (district, window_start)`.
   With a plain append-only INSERT any re-emitted window (job restart, failover,
   replayed Kafka offsets) aborts the job with a duplicate-key error. Declaring
   the same primary key in the Flink DDL switches flink-connector-jdbc to
   upsert mode (`INSERT .. ON CONFLICT (district, window_start) DO UPDATE SET
   ..`, supported by the PostgreSQL dialect), which keeps the platform healthy
   across restarts. To force append-only mode delete the
   `PRIMARY KEY (district, window_start) NOT ENFORCED` line — acceptance then
   relies on the job never reprocessing a window.
3. **Epoch milliseconds for `alert_id`.** `UNIX_TIMESTAMP(CAST(window_end AS
   STRING))` is unsafe: `CAST(TIMESTAMP_LTZ AS STRING)` includes a fractional
   part (`2026-11-19 12:40:00.000`) while the one-argument `UNIX_TIMESTAMP`
   only parses `yyyy-MM-dd HH:mm:ss` and returns `Long.MIN_VALUE` on a parse
   error. The job uses
   `UNIX_TIMESTAMP(DATE_FORMAT(CAST(window_end AS TIMESTAMP(3)), 'yyyy-MM-dd HH:mm:ss'), 'yyyy-MM-dd HH:mm:ss') * 1000`.
   Both functions use `table.local-time-zone` (pinned to `UTC` in the SQL), so
   the round-trip is timezone-correct; tumbling window boundaries are whole
   seconds, so `*1000` is exact.
4. **`TIMESTAMP_LTZ(3)` for `reported_at`, not `TIMESTAMP(3)`.** The Go API
   emits RFC 3339 (`...Z`) strings. With
   `json.timestamp-format.standard='ISO-8601'` the JSON format parses
   `TIMESTAMP_LTZ` from `yyyy-MM-ddTHH:mm:ss[.fraction]Z` (ISO_LOCAL_TIME
   accepts optional fractional seconds) and serializes it back as `...Z`. A
   `TIMESTAMP` column would reject the trailing `Z`.
5. **Two Kafka sink table objects for one topic.** `report_spike` and
   `high_severity` are two INSERT statements; two table objects mapping to
   `outage-alerts` keep the statement set portable across Flink 1.20 planner
   revisions.

## 4. How it is wired in docker compose (already in `platform/docker-compose.yml`)

* `kafka-init` runs `/bin/bash /opt/odisha/kafka/topics.sh` (volume
  `./kafka:/opt/odisha/kafka:ro`) after Kafka is healthy.
* `flink-jobmanager` / `flink-taskmanager` are built from this directory
  (`build: context: ./flink`) so all three Flink containers have the jars.
* `flink-submit` runs this directory's `submit.sh` in a 180 s loop with
  `volumes: ./flink:/opt/odisha/flink:ro` and env
  `POSTGRES_PASSWORD`, `KAFKA_BOOTSTRAP`, `FLINK_REST_URL`.
* `submit.sh` reads `jobs/user_report_alerts.sql` **relative to itself** and
  renders `${KAFKA_BOOTSTRAP}` and `${POSTGRES_PASSWORD}` into a `chmod 600`
  temp file that is deleted on exit.
* The Flink SQL Client runs in embedded mode, starts an SQL gateway inside its
  JVM and submits the statement set to the JobManager REST API. Because the
  `flink-submit` container does not run the Flink docker entrypoint, its
  `flink-conf.yaml` still says `rest.address: localhost`; `submit.sh` therefore
  passes the attached-form session options `-Drest.address=<host from
  FLINK_REST_URL>` and `-Drest.port=<port>` (the attached `-Dkey=value` form is
  the canonical Flink CLI form, see the Flink 1.20 CLI docs).
* Detached submission: the SQL Client exits after submission and the streaming
  job keeps running (`-f` mode is asynchronous for DML).

## 5. Compose / service requirements (for the compose owner)

Already satisfied by `platform/docker-compose.yml`, listed for verification:

| Requirement | Value used |
|---|---|
| image build context | `platform/flink` (Dockerfile in this directory) |
| `flink-submit` volume | `./flink:/opt/odisha/flink:ro` → `/opt/odisha/flink/submit.sh`, `/opt/odisha/flink/jobs/user_report_alerts.sql` |
| `flink-submit` env | `POSTGRES_PASSWORD` (required), `KAFKA_BOOTSTRAP=kafka:9092`, `FLINK_REST_URL=http://flink-jobmanager:8081` |
| `kafka-init` volume | `./kafka:/opt/odisha/kafka:ro` |
| JobManager REST reachable | `flink-jobmanager:8081` from `flink-submit` |
| TaskManager slots | `taskmanager.numberOfTaskSlots: 2` is enough (default job parallelism is 1) |
| Jars on JM + TM | via this image (`/opt/flink/lib`) |

Known compose gaps worth fixing (cannot be changed from this scope):

1. `flink-submit` is started with `command: /opt/odisha/flink/submit.sh`.
   The bind mount must make the file executable (run `chmod +x
   platform/flink/submit.sh` on the deploy host, or change the compose command
   to `/bin/bash /opt/odisha/flink/submit.sh || true`). `kafka/topics.sh` is
   already invoked via `/bin/bash`, so it does not need the bit.
2. `odisha_flink_ckpt` is mounted only into `flink-jobmanager`; the
   taskmanager writes checkpoint state to `file:///opt/flink/checkpoints` on
   its own container filesystem. For durable checkpoints/recovery after
   container recreation, mount `odisha_flink_ckpt:/opt/flink/checkpoints` in
   `flink-taskmanager` as well.
3. `flink-submit` has `mem_limit: 224m`. The SQL Client is a JVM that parses
   the 3-statement job; its default max heap is `mem_limit/4` ≈ 56 MB, which is
   tight for the Table planner. Recommend `mem_limit: 640m` (or set
   `SQL_CLIENT_JAVA_OPTS=-Xmx256m`, which `submit.sh` accepts via the
   `sql-client` environment).
4. The job is submitted after `kafka-init` completes and Postgres is healthy
   (compose `depends_on` already encodes this). The 180 s re-submit loop makes
   the pipeline self-healing after a Flink outage.

## 6. How to inspect alerts / windows

```bash
# 1. Job status
curl -s http://127.0.0.1:8081/jobs/overview | python3 -m json.tool
#    or the SQL client: SHOW JOBS;

# 2. Consume the alert topic (inside the kafka container)
docker compose -f platform/docker-compose.yml exec kafka \
  /opt/kafka/bin/kafka-console-consumer.sh \
  --bootstrap-server kafka:9092 --topic outage-alerts --from-beginning --max-messages 5

# 3. Window metrics in PostgreSQL
docker compose -f platform/docker-compose.yml exec postgres \
  psql -U odisha -d odisha -c \
  "SELECT district, window_start, report_count, unique_reporters, avg_severity
     FROM realtime.flink_report_windows ORDER BY window_start DESC LIMIT 20;"

# 4. Topics / retention
docker compose -f platform/docker-compose.yml exec kafka \
  /opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9092 \
  --describe --topic user-reports

# 5. Produce a test report (from any container with bash)
docker compose -f platform/docker-compose.yml exec -T kafka \
  /opt/kafka/bin/kafka-console-producer.sh --bootstrap-server kafka:9092 \
  --topic user-reports <<'JSON'
{"report_id":"11111111-1111-4111-8111-111111111111","district":"Khordha","gss":"","feeder":"","comment":"no power","severity":5,"reporter":"web","lat":null,"lon":null,"reported_at":"2026-11-19T12:31:00Z"}
JSON
# repeat with 2 more distinct report_ids in the same 10-minute window to cross
# the >= 3 warning threshold; the alert is emitted once the 30 s watermark has
# passed window_end (i.e. shortly after the next 10-minute boundary).
```

Functional expectations: alerts appear in `outage-alerts` once the watermark
passes `window_end + 30 s`; window rows are written to PostgreSQL with the
connector's 1 s buffer flush interval.

## 7. Known limitations / operational notes

* **At-least-once, not exactly-once.** The Kafka sink uses
  `sink.delivery-guarantee = at-least-once` (no 2PC transactional producer) and
  the JDBC sink is a generic sink; after a failure or restart, records since
  the last successful checkpoint can be re-processed. Duplicate Kafka alerts
  share the same `alert_id`, and downstream consumers use `realtime.alerts`
  (`alert_id` primary key) / `realtime.flink_report_windows`
  (`district, window_start` primary key) as upsert keys, so the duplicate is
  not visible in the API. The JDBC sink is idempotent via upsert.
* **No savepoint on restart.** The compose submit loop only checks
  `/jobs/overview`; when a job disappeared it submits a fresh job with no
  savepoint (`scan.startup.mode = earliest-offset`), so up to 24 h of retained
  reports can be re-windowed and re-alerted. Windows are idempotent in
  PostgreSQL; duplicate alerts carry identical `alert_id`s.
* **No durable checkpoint storage in compose yet** (see §5.2): the JobManager
  side of checkpoints lives on the `odisha_flink_ckpt` volume, the TaskManager
  state does not. Checkpoints protect against task failures within the same
  containers only.
* **Time zone.** All Flink timestamps are `TIMESTAMP_LTZ` and the session is
  pinned to UTC; `window_start`/`window_end` in alert JSON are ISO-8601 UTC
  (`...Z`) and PostgreSQL columns are `timestamptz`. Display conversion to
  `Asia/Kolkata` is the frontend/API's job.
* **Malformed reports.** `json.ignore-parse-errors='true'` +
  `json.fail-on-missing-field='false'` means a malformed `reported_at` becomes
  a null event time and the record is dropped/no-op; `district IS NULL` rows
  are filtered out of the aggregate because PostgreSQL's primary key on
  `realtime.flink_report_windows` rejects null districts.
* **`flink-report-windows` topic is created but not produced by this job.**
  The contract topic table lists Flink as its producer, while the contract
  Flink section only requires the JDBC sink; the Spark streaming job consumes
  `user-reports` + `outage-alerts` directly, so the topic is left for future
  producers (it is created by `kafka/topics.sh` as specified).
* **Job naming.** `pipeline.name` is set before the DDL/INSERTs, so the single
  statement-set job is exactly `odisha-user-report-alerts` - this is what
  `submit.sh` polls and what the Airflow `odisha_platform_health` DAG should
  look for.
* **Validation status.** The Windows sandbox used for this work blocks MSYS Git
  Bash (it cannot create its signal pipe) and WSL, so `bash -n` could not run
  here. Both scripts were instead validated with the `mvdan/sh` parser (the
  Go/shfmt/ShellCheck-family Bash grammar) and a structural assertion suite;
  both parse cleanly. Please still run
  `bash -n platform/kafka/topics.sh platform/flink/submit.sh` on the deploy host
  as a final gate. The SQL was statically reviewed against the Flink 1.20
  documentation (window TVF, statement sets, JSON ISO-8601 timestamp options,
  JDBC upsert) and all contract markers are asserted by a checker; Docker is not
  run in this sandbox, so the end-to-end acceptance test
  (fresh compose stack -> report -> alert within ~1 min) has not been executed.

## 8. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `submit.sh` exits 1, log says `Connection refused` to `flink-jobmanager:8081` | `flink-jobmanager` not healthy yet; the 180 s loop retries. Check `docker compose logs flink-jobmanager`. |
| Submit log: `Could not find any factory for identifier 'jdbc'` / `'kafka'` | connector jars missing from `/opt/flink/lib`; rebuild the image (`docker compose build flink-jobmanager`) and confirm the Dockerfile's SHA-1 checks passed. |
| Job fails at planning with `Window TVF ... doesn't support consuming update and delete changes` | a `ROW_NUMBER()` de-dup view was added in front of `TABLE(TUMBLE(...))`; remove it (de-dup is already `COUNT(DISTINCT report_id)`). |
| Job fails with `duplicate key value violates unique constraint "flink_report_windows_pkey"` | the `PRIMARY KEY ... NOT ENFORCED` line was removed from the JDBC DDL (append-only mode) and a window was re-emitted; put it back for idempotent writes. |
| No windows are emitted although reports exist | sparse traffic on a partitioned topic stalls the watermark: `table.exec.source.idle-timeout=30s` is already set. Also check that `reported_at` parses (ISO-8601 `...Z`). |
| Alerts appear in Kafka but not in `/api/realtime` | that is the Spark streaming job's responsibility (`outage-alerts` → `realtime.alerts`), see `platform/spark`. |
| `job did not appear within 60s` but SQL log has no error | check `GET /jobs/overview`; a job that immediately FAILED is still listed with state `FAILED` - inspect it in the Flink UI (`http://127.0.0.1:8081`). |
