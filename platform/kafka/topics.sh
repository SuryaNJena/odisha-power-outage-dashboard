#!/usr/bin/env bash
# =============================================================================
# Odisha Power Outage Dashboard - Kafka topic bootstrap
# =============================================================================
# Creates the five contract topics (see platform/CONTRACT.md) using the Kafka
# CLI shipped in the apache/kafka:3.9.0 image. The script is idempotent:
#   * --create --if-not-exists makes topic creation safe to re-run
#   * --alter --add-config makes the retention config converge on re-runs
#   * it waits for the broker to accept metadata requests before creating
#
# Runs INSIDE the kafka container (compose service `kafka-init`):
#   /bin/bash /opt/odisha/kafka/topics.sh
#
# Environment variables:
#   KAFKA_BOOTSTRAP   broker list (default: kafka:9092)
#   KAFKA_BIN         kafka-topics.sh path (default: /opt/kafka/bin/kafka-topics.sh)
#   PARTITIONS        partition count for new topics (default: 3)
#   REPLICATION_FACTOR replication factor for new topics (default: 1)
#   RETENTION_MS      retention.ms (default: 86400000 = 24h)
#   READY_TIMEOUT     seconds to wait for the broker (default: 180)
#   READY_POLL        seconds between readiness attempts (default: 2)
#
# Command line flags override the environment variables (see --help).
#
# Exit codes: 0 success, 1 broker/API failure, 2 bad usage.
# =============================================================================
set -euo pipefail

usage() {
  printf '%s\n' \
    'Usage: topics.sh [options]' \
    '' \
    'Create/verify the Odisha platform Kafka topics.' \
    '' \
    'Options:' \
    '  --bootstrap <host:port>       Kafka bootstrap servers (default: $KAFKA_BOOTSTRAP or kafka:9092)' \
    '  --kafka-bin <path>            kafka-topics.sh executable (default: /opt/kafka/bin/kafka-topics.sh)' \
    '  --partitions <n>              partitions for created topics (default: 3)' \
    '  --replication-factor <n>      replication factor for created topics (default: 1)' \
    '  --retention-ms <ms>           retention.ms applied to topics (default: 86400000 = 24h)' \
    '  --ready-timeout <seconds>     max wait for broker readiness (default: 180)' \
    '  --ready-poll <seconds>        poll interval while waiting (default: 2)' \
    '  -h, --help                    show this help' \
    '' \
    'Topics (3 partitions, replication factor 1):' \
    '  user-reports            Go API -> Flink, Spark      (24h retention)' \
    '  official-outages        Airflow -> Spark (optional) (24h retention)' \
    '  weather-observations    Airflow -> Spark (optional) (24h retention)' \
    '  outage-alerts           Flink -> Spark, Go          (24h retention)' \
    '  flink-report-windows    Flink -> Spark               (24h retention)' \
    '' \
    'The contract asks for 24h retention on user-reports, official-outages and' \
    'weather-observations explicitly, and 24h retention unless noted for the' \
    'remaining topics, so 24h is applied to all five for a predictable footprint.'
}

KAFKA_BOOTSTRAP="${KAFKA_BOOTSTRAP:-kafka:9092}"
KAFKA_BIN="${KAFKA_BIN:-/opt/kafka/bin/kafka-topics.sh}"
PARTITIONS="${PARTITIONS:-3}"
REPLICATION_FACTOR="${REPLICATION_FACTOR:-1}"
RETENTION_MS="${RETENTION_MS:-86400000}"
READY_TIMEOUT="${READY_TIMEOUT:-180}"
READY_POLL="${READY_POLL:-2}"

TOPICS=(
  user-reports
  official-outages
  weather-observations
  outage-alerts
  flink-report-windows
)

log()  { printf '[topics] %s\n' "$*"; }
fail() { printf '[topics] ERROR: %s\n' "$*" >&2; exit 1; }

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --bootstrap)          KAFKA_BOOTSTRAP="${2:?--bootstrap needs a value}"; shift 2 ;;
      --bootstrap=*)        KAFKA_BOOTSTRAP="${1#*=}"; shift ;;
      --kafka-bin)          KAFKA_BIN="${2:?--kafka-bin needs a value}"; shift 2 ;;
      --kafka-bin=*)        KAFKA_BIN="${1#*=}"; shift ;;
      --partitions)         PARTITIONS="${2:?--partitions needs a value}"; shift 2 ;;
      --partitions=*)       PARTITIONS="${1#*=}"; shift ;;
      --replication-factor) REPLICATION_FACTOR="${2:?--replication-factor needs a value}"; shift 2 ;;
      --replication-factor=*) REPLICATION_FACTOR="${1#*=}"; shift ;;
      --retention-ms)       RETENTION_MS="${2:?--retention-ms needs a value}"; shift 2 ;;
      --retention-ms=*)     RETENTION_MS="${1#*=}"; shift ;;
      --ready-timeout)      READY_TIMEOUT="${2:?--ready-timeout needs a value}"; shift 2 ;;
      --ready-timeout=*)    READY_TIMEOUT="${1#*=}"; shift ;;
      --ready-poll)         READY_POLL="${2:?--ready-poll needs a value}"; shift 2 ;;
      --ready-poll=*)       READY_POLL="${1#*=}"; shift ;;
      -h|--help)            usage; exit 0 ;;
      *)                    usage >&2; fail "unknown argument: $1" ;;
    esac
  done

  if [ ! -x "$KAFKA_BIN" ]; then
    fail "kafka-topics.sh not found or not executable at '$KAFKA_BIN' (are we inside the apache/kafka image?)"
  fi
  case "$PARTITIONS" in
    ''|*[!0-9]*) fail "--partitions must be a positive integer, got '$PARTITIONS'" ;;
  esac
  case "$REPLICATION_FACTOR" in
    ''|*[!0-9]*) fail "--replication-factor must be a positive integer, got '$REPLICATION_FACTOR'" ;;
  esac
  case "$RETENTION_MS" in
    ''|*[!0-9]*) fail "--retention-ms must be an integer number of milliseconds, got '$RETENTION_MS'" ;;
  esac
  [ "$PARTITIONS" -ge 1 ] || fail "--partitions must be >= 1"
  [ "$REPLICATION_FACTOR" -ge 1 ] || fail "--replication-factor must be >= 1"
}

# Poll the broker until it answers a metadata request (ListTopics) or timeout.
wait_for_broker() {
  local elapsed=0
  log "waiting up to ${READY_TIMEOUT}s for Kafka broker at ${KAFKA_BOOTSTRAP} ..."
  while ! "$KAFKA_BIN" --bootstrap-server "$KAFKA_BOOTSTRAP" --list >/dev/null 2>&1; do
    if [ "$elapsed" -ge "$READY_TIMEOUT" ]; then
      fail "Kafka broker at ${KAFKA_BOOTSTRAP} did not become ready within ${READY_TIMEOUT}s"
    fi
    sleep "$READY_POLL"
    elapsed=$((elapsed + READY_POLL))
    if [ $((elapsed % 10)) -eq 0 ]; then
      log "still waiting for ${KAFKA_BOOTSTRAP} (${elapsed}s elapsed)"
    fi
  done
  log "Kafka broker is reachable."
}

# Run a command up to 3 times with exponential backoff. Kafka's admin API can
# transiently fail right after the broker starts answering metadata requests
# (controller election, NOT_CONTROLLER, request timeouts).
run_with_retry() {
  local attempts=3 delay=2 attempt=1
  while true; do
    if "$@"; then
      return 0
    fi
    if [ "$attempt" -ge "$attempts" ]; then
      fail "command failed after ${attempts} attempts: $*"
    fi
    log "attempt ${attempt}/${attempts} failed; retrying in ${delay}s: $*"
    sleep "$delay"
    attempt=$((attempt + 1))
    delay=$((delay * 2))
  done
}

create_topics() {
  local topic
  for topic in "${TOPICS[@]}"; do
    log "ensuring topic '${topic}' (partitions=${PARTITIONS}, replication-factor=${REPLICATION_FACTOR})"
    run_with_retry "$KAFKA_BIN" --bootstrap-server "$KAFKA_BOOTSTRAP" \
      --create --if-not-exists \
      --topic "$topic" \
      --partitions "$PARTITIONS" \
      --replication-factor "$REPLICATION_FACTOR"
  done
}

apply_retention() {
  local topic
  local kafka_configs="${KAFKA_CONFIGS_BIN:-/opt/kafka/bin/kafka-configs.sh}"
  if [ ! -x "$kafka_configs" ]; then
    fail "kafka-configs.sh not found at '$kafka_configs'"
  fi
  for topic in "${TOPICS[@]}"; do
    log "ensuring retention.ms=${RETENTION_MS} on '${topic}'"
    run_with_retry "$kafka_configs" --bootstrap-server "$KAFKA_BOOTSTRAP" \
      --entity-type topics --entity-name "$topic" \
      --alter --add-config "retention.ms=${RETENTION_MS}" >/dev/null
  done
}

verify_and_list() {
  local listing topic
  listing="$("$KAFKA_BIN" --bootstrap-server "$KAFKA_BOOTSTRAP" --list)"
  for topic in "${TOPICS[@]}"; do
    if ! printf '%s\n' "$listing" | grep -qx -- "$topic"; then
      fail "topic '${topic}' is missing after create/alter"
    fi
  done
  log "topics in cluster (${KAFKA_BOOTSTRAP}):"
  printf '%s\n' "$listing"
}

main() {
  parse_args "$@"
  log "bootstrap=${KAFKA_BOOTSTRAP} kafka-topics=${KAFKA_BIN}"
  wait_for_broker
  create_topics
  apply_retention
  verify_and_list
  log "done: ${#TOPICS[@]} topics ready."
}

main "$@"
