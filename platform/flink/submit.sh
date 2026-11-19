#!/usr/bin/env bash
# =============================================================================
# Odisha Power Outage Dashboard - submit the Flink SQL job
# =============================================================================
# Renders platform/flink/jobs/user_report_alerts.sql with the runtime secrets
# (Flink SQL cannot read environment variables), submits it with the Flink SQL
# Client as a detached streaming job and verifies that the job shows up in the
# JobManager REST API.
#
# The script is IDEMPOTENT: it is safe to call on a timer (compose calls it
# every 180 s). If a job named ${JOB_NAME} is already created/initializing/
# running/restarting/reconciling it logs that and exits 0 without submitting a
# second copy.
#
# Environment variables:
#   POSTGRES_PASSWORD   required - rendered into the JDBC sink password
#   KAFKA_BOOTSTRAP     default kafka:9092 - rendered into the Kafka connectors
#   FLINK_REST_URL      default http://flink-jobmanager:8081
#   FLINK_JOB_NAME      default odisha-user-report-alerts
#   FLINK_SQL_FILE      default <script dir>/jobs/user_report_alerts.sql
#   SQL_CLIENT_BIN      default /opt/flink/bin/sql-client.sh
#   READY_TIMEOUT       default 120 (seconds to wait for the JobManager)
#   VERIFY_TIMEOUT      default 60  (seconds to wait for the job to appear)
#   SQL_CLIENT_JAVA_OPTS optional extra JVM options for the SQL client
#
# Usage:
#   POSTGRES_PASSWORD=... ./submit.sh
#   ./submit.sh --help
#
# Exit codes: 0 success / already running, 1 submission or verification
# failure, 2 bad usage / missing environment.
# =============================================================================
set -euo pipefail

# Directory of this script (used to locate jobs/user_report_alerts.sql).
case "${BASH_SOURCE[0]}" in
  */*) SCRIPT_DIR="${BASH_SOURCE[0]%/*}" ;;
  *)   SCRIPT_DIR="." ;;
esac

POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-}"
KAFKA_BOOTSTRAP="${KAFKA_BOOTSTRAP:-kafka:9092}"
FLINK_REST_URL="${FLINK_REST_URL:-http://flink-jobmanager:8081}"
JOB_NAME="${FLINK_JOB_NAME:-odisha-user-report-alerts}"
SQL_FILE="${FLINK_SQL_FILE:-${SCRIPT_DIR}/jobs/user_report_alerts.sql}"
SQL_CLIENT_BIN="${SQL_CLIENT_BIN:-/opt/flink/bin/sql-client.sh}"
READY_TIMEOUT="${READY_TIMEOUT:-120}"
VERIFY_TIMEOUT="${VERIFY_TIMEOUT:-60}"
POLL_INTERVAL="${POLL_INTERVAL:-2}"

# States in which a job already exists / is being brought up and must not be
# submitted again.
ACTIVE_STATES=" CREATED INITIALIZING RUNNING RESTARTING RECONCILING CANCELLING "

log()  { printf '[flink-submit] %s\n' "$*"; }
warn() { printf '[flink-submit] WARN: %s\n' "$*" >&2; }
fail() { printf '[flink-submit] ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  printf '%s\n' \
    'Usage: submit.sh [options]' \
    '' \
    'Render and submit the Odisha Flink SQL job (odisha-user-report-alerts).' \
    '' \
    'Options:' \
    '  --rest-url <url>        JobManager REST base URL (default: $FLINK_REST_URL)' \
    '  --job-name <name>       expected Flink job name (default: $FLINK_JOB_NAME)' \
    '  --sql-file <path>       SQL template (default: <script dir>/jobs/user_report_alerts.sql)' \
    '  --sql-client <path>     sql-client.sh executable (default: /opt/flink/bin/sql-client.sh)' \
    '  --ready-timeout <s>     max seconds to wait for the JobManager REST API (default 120)' \
    '  --verify-timeout <s>    max seconds to wait for the submitted job (default 60)' \
    '  -h, --help              show this help' \
    '' \
    'Required environment:' \
    '  POSTGRES_PASSWORD       database password rendered into the SQL (never logged)' \
    '' \
    'Optional environment:' \
    '  KAFKA_BOOTSTRAP         default kafka:9092' \
    '  FLINK_REST_URL          default http://flink-jobmanager:8081' \
    '  FLINK_SQL_FILE          override the SQL template path' \
    '  SQL_CLIENT_JAVA_OPTS    extra JVM options for the SQL client process' \
    '' \
    'Behaviour:' \
    '  * waits for GET <rest-url>/overview' \
    '  * if a job named <job-name> is already CREATED/INITIALIZING/RUNNING/' \
    '    RESTARTING/RECONCILING it exits 0 without submitting a duplicate' \
    '  * otherwise renders the SQL with the KAFKA_BOOTSTRAP and POSTGRES_PASSWORD' \
    '    environment variables, submits it detached with sql-client.sh -f and' \
    '    polls GET <rest-url>/jobs/overview for up to <verify-timeout> seconds' \
    '  * exits non-zero if the job never appears' \
    '' \
    'Run it inside a container that can reach the JobManager REST API and has' \
    'the Flink distribution + connector jars (the odisha-flink image does).'
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --rest-url)       FLINK_REST_URL="${2:?--rest-url needs a value}"; shift 2 ;;
      --rest-url=*)     FLINK_REST_URL="${1#*=}"; shift ;;
      --job-name)       JOB_NAME="${2:?--job-name needs a value}"; shift 2 ;;
      --job-name=*)     JOB_NAME="${1#*=}"; shift ;;
      --sql-file)       SQL_FILE="${2:?--sql-file needs a value}"; shift 2 ;;
      --sql-file=*)     SQL_FILE="${1#*=}"; shift ;;
      --sql-client)     SQL_CLIENT_BIN="${2:?--sql-client needs a value}"; shift 2 ;;
      --sql-client=*)   SQL_CLIENT_BIN="${1#*=}"; shift ;;
      --ready-timeout)  READY_TIMEOUT="${2:?--ready-timeout needs a value}"; shift 2 ;;
      --ready-timeout=*) READY_TIMEOUT="${1#*=}"; shift ;;
      --verify-timeout) VERIFY_TIMEOUT="${2:?--verify-timeout needs a value}"; shift 2 ;;
      --verify-timeout=*) VERIFY_TIMEOUT="${1#*=}"; shift ;;
      -h|--help)        usage; exit 0 ;;
      *)                usage >&2; fail "unknown argument: $1" ;;
    esac
  done

  SQL_CLIENT_JAVA_OPTS="${SQL_CLIENT_JAVA_OPTS:-}"
  FLINK_REST_URL="${FLINK_REST_URL%/}"

  [ -n "$JOB_NAME" ] || fail "--job-name must not be empty"
  [ -r "$SQL_FILE" ] || fail "SQL template not found or unreadable: $SQL_FILE"
  [ -x "$SQL_CLIENT_BIN" ] || fail "sql-client.sh not found or not executable: $SQL_CLIENT_BIN"
  command -v curl >/dev/null 2>&1 || fail "curl is required to talk to the Flink REST API"
  [ -n "$POSTGRES_PASSWORD" ] || fail "POSTGRES_PASSWORD environment variable is required (compose injects it)"
  case "$READY_TIMEOUT" in
    ''|*[!0-9]*) fail "--ready-timeout must be a number of seconds, got '$READY_TIMEOUT'" ;;
  esac
  case "$VERIFY_TIMEOUT" in
    ''|*[!0-9]*) fail "--verify-timeout must be a number of seconds, got '$VERIFY_TIMEOUT'" ;;
  esac
  case "$POLL_INTERVAL" in
    ''|*[!0-9]*) fail "POLL_INTERVAL must be a number of seconds, got '$POLL_INTERVAL'" ;;
  esac
  [ "$POLL_INTERVAL" -ge 1 ] || fail "POLL_INTERVAL must be >= 1"
}

# ---------------------------------------------------------------------------
# REST helpers (the compose network only exposes compact JSON, no jq needed)
# ---------------------------------------------------------------------------

# Split <rest-url> into FLINK_REST_HOST / FLINK_REST_PORT for sql-client -D.
derive_rest_host_port() {
  local rest="$FLINK_REST_URL"
  rest="${rest#*://}"          # drop scheme
  rest="${rest%%/*}"           # drop path
  FLINK_REST_HOST="${rest%%:*}"
  FLINK_REST_PORT="8081"
  case "$rest" in
    *:*) FLINK_REST_PORT="${rest##*:}" ;;
  esac
  [ -n "$FLINK_REST_HOST" ] || fail "cannot parse host from FLINK_REST_URL='$FLINK_REST_URL'"
}

wait_for_jobmanager() {
  local elapsed=0
  log "waiting up to ${READY_TIMEOUT}s for JobManager REST API at ${FLINK_REST_URL}/overview ..."
  until curl -fsS --max-time 5 "${FLINK_REST_URL}/overview" >/dev/null 2>&1; do
    [ "$elapsed" -lt "$READY_TIMEOUT" ] || \
      fail "JobManager REST API not reachable at ${FLINK_REST_URL}/overview after ${READY_TIMEOUT}s"
    sleep 2
    elapsed=$((elapsed + 2))
  done
  log "JobManager REST API is reachable."
}

jobs_overview() {
  curl -fsS --max-time 10 "${FLINK_REST_URL}/jobs/overview" 2>/dev/null || true
}

# Print one JSON object per line for jobs whose name matches JOB_NAME.
matching_job_lines() {
  local json="$1"
  [ -n "$json" ] || return 0
  # Flink REST returns a compact {"jobs":[{...},{...}]} document.
  printf '%s' "$json" \
    | sed 's/},{/}\n{/g' \
    | grep -F "\"name\":\"${JOB_NAME}\"" \
    || true
}

# Extract a string field from one compact job JSON object.
job_field() {
  local line="$1" field="$2"
  printf '%s' "$line" | sed -n "s/.*\"${field}\":\"\([^\"]*\)\".*/\1/p" | head -n 1
}

is_active_state() {
  case " ${ACTIVE_STATES} " in
    *" $1 "*) return 0 ;;
    *)        return 1 ;;
  esac
}

# Returns 0 and exports FOUND_JID/FOUND_STATE if a matching job is active.
find_active_job() {
  local json line state jid
  json="$(jobs_overview)"
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    state="$(job_field "$line" state)"
    if is_active_state "$state"; then
      jid="$(job_field "$line" jid)"
      FOUND_JID="$jid"
      FOUND_STATE="$state"
      return 0
    fi
  done < <(matching_job_lines "$json")
  return 1
}

# ---------------------------------------------------------------------------
# SQL rendering
# ---------------------------------------------------------------------------
escape_sed_replacement() {
  # Escape backslash, ampersand and the | delimiter used by the s||| command.
  printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'
}

render_sql() {
  local template="$1" out="$2"
  local pw_sql pw_escaped kafka_escaped

  # The password is embedded inside a single-quoted SQL string literal, so SQL
  # quote escaping ('' for ') must be applied before substitution. Passwords
  # with newlines are not supported (compose .env keeps them URL-safe).
  pw_sql="${POSTGRES_PASSWORD//\'/\'\'}"

  if command -v envsubst >/dev/null 2>&1; then
    log "rendering SQL with envsubst"
    KAFKA_BOOTSTRAP="$KAFKA_BOOTSTRAP" POSTGRES_PASSWORD="$pw_sql" \
      envsubst '${KAFKA_BOOTSTRAP} ${POSTGRES_PASSWORD}' <"$template" >"$out"
  else
    log "rendering SQL with sed (envsubst not installed)"
    pw_escaped="$(escape_sed_replacement "$pw_sql")"
    kafka_escaped="$(escape_sed_replacement "$KAFKA_BOOTSTRAP")"
    sed -e "s|\${POSTGRES_PASSWORD}|${pw_escaped}|g" \
        -e "s|\${KAFKA_BOOTSTRAP}|${kafka_escaped}|g" \
        "$template" >"$out"
  fi

  chmod 600 "$out"
  if grep -qF '${POSTGRES_PASSWORD}' "$out" || grep -qF '${KAFKA_BOOTSTRAP}' "$out"; then
    fail "SQL rendering left unresolved placeholders in $out"
  fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
  parse_args "$@"
  derive_rest_host_port

  log "rest=${FLINK_REST_URL} job='${JOB_NAME}' sql='${SQL_FILE}'"

  wait_for_jobmanager

  # Idempotency: never submit a second copy of the job.
  if find_active_job; then
    log "job '${JOB_NAME}' is already ${FOUND_STATE} (jid=${FOUND_JID:-unknown}) - skipping submission"
    exit 0
  fi

  local tmpdir rendered_sql sql_client_log rc=0
  tmpdir="$(mktemp -d "${TMPDIR:-/tmp}/odisha-flink-submit.XXXXXX")"
  # shellcheck disable=SC2064
  trap "rm -rf '${tmpdir}'" EXIT INT TERM
  rendered_sql="${tmpdir}/job.sql"
  sql_client_log="${tmpdir}/sql-client.log"

  log "rendering SQL template ${SQL_FILE}"
  render_sql "$SQL_FILE" "$rendered_sql"

  # The Flink SQL Client runs in embedded mode: it submits the statement set to
  # the cluster described by the (session) configuration. In the flink-submit
  # container the shipped flink-conf.yaml still points at localhost, so the
  # JobManager address from FLINK_REST_URL is injected as session config.
  log "submitting statement set to ${FLINK_REST_HOST}:${FLINK_REST_PORT} via ${SQL_CLIENT_BIN}"
  set +e
  if [ -n "$SQL_CLIENT_JAVA_OPTS" ]; then
    # shellcheck disable=SC2086
    "$SQL_CLIENT_BIN" \
      "-Drest.address=${FLINK_REST_HOST}" \
      "-Drest.port=${FLINK_REST_PORT}" \
      -hist "${tmpdir}/sql-history" \
      -f "$rendered_sql" >"$sql_client_log" 2>&1
  else
    "$SQL_CLIENT_BIN" \
      "-Drest.address=${FLINK_REST_HOST}" \
      "-Drest.port=${FLINK_REST_PORT}" \
      -hist "${tmpdir}/sql-history" \
      -f "$rendered_sql" >"$sql_client_log" 2>&1
  fi
  rc=$?
  set -e

  if [ "$rc" -ne 0 ]; then
    printf '[flink-submit] sql-client exited with code %s. Last 80 log lines:\n' "$rc" >&2
    tail -n 80 "$sql_client_log" >&2 || true
    exit 1
  fi
  if grep -qiE '^(ERROR|Failed|could not|Exception)' "$sql_client_log"; then
    warn "sql-client log contains error markers; verifying via REST anyway"
  fi

  # The SQL Client submits asynchronously and may exit 0 even after a statement
  # failed, therefore the authoritative check is the JobManager REST API.
  local deadline=$((SECONDS + VERIFY_TIMEOUT))
  log "waiting up to ${VERIFY_TIMEOUT}s for job '${JOB_NAME}' to appear in /jobs/overview ..."
  while [ "$SECONDS" -lt "$deadline" ]; do
    if find_active_job; then
      log "job '${JOB_NAME}' is ${FOUND_STATE} (jid=${FOUND_JID:-unknown})"
      log "job submitted successfully; it will keep running detached after this script exits"
      exit 0
    fi
    sleep "$POLL_INTERVAL"
  done

  printf '[flink-submit] job %s did not appear within %ss. Last 80 sql-client log lines:\n' \
    "'${JOB_NAME}'" "$VERIFY_TIMEOUT" >&2
  tail -n 80 "$sql_client_log" >&2 || true
  exit 1
}

# Allow the helper functions to be sourced by tests without submitting anything.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
