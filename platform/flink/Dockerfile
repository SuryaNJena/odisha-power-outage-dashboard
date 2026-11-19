# syntax=docker/dockerfile:1
# =============================================================================
# Odisha Power Outage Dashboard - Flink 1.20.1 image with SQL connector jars
# =============================================================================
# Built from the compose build context ./flink (see platform/docker-compose.yml)
# and used for the three Flink roles: jobmanager, taskmanager and flink-submit.
#
# Jars are downloaded from Maven Central and verified twice before they are
# accepted into /opt/flink/lib:
#   1. minimum size (catches truncation / HTML error pages),
#   2. SHA-1 published by Maven Central (catches corruption / tampering),
# plus a ZIP magic check so an HTML error body can never masquerade as a jar.
#
# Version notes (verified against repo1.maven.org on 2026-11-19):
#   * flink-sql-connector-kafka 3.3.0-1.20  -> 5,587,110 B  (fat SQL jar: Kafka
#     client + JSON/CSV/Avro format factories are bundled).
#   * flink-connector-jdbc 3.4.0-1.20       -> 444,657 B. This is the current
#     release for Flink 1.20 (the 3.2.0-1.20 path in an early task draft does
#     NOT exist on Maven Central; Flink 1.20 docs pin 3.4.0-1.20 and 3.3.0-1.20
#     is also published). The artifact is the shaded "Flink : Connectors :
#     JDBC : Shaded" jar: it bundles flink-connector-jdbc-core and every
#     dialect module including flink-connector-jdbc-postgres, so a single jar
#     is enough for the PostgreSQL JDBC sink (no extra core/postgres jars).
#     Because it is a ~430 KB module jar by design, its size floor below is
#     400 KB rather than the 1 MB used for the other two artifacts.
#   * postgresql 42.7.4                    -> 1,086,687 B (JDBC driver).
# =============================================================================
FROM flink:1.20.1-scala_2.12

ARG KAFKA_SQL_CONNECTOR_URL=https://repo1.maven.org/maven2/org/apache/flink/flink-sql-connector-kafka/3.3.0-1.20/flink-sql-connector-kafka-3.3.0-1.20.jar
ARG JDBC_CONNECTOR_URL=https://repo1.maven.org/maven2/org/apache/flink/flink-connector-jdbc/3.4.0-1.20/flink-connector-jdbc-3.4.0-1.20.jar
ARG POSTGRES_DRIVER_URL=https://repo1.maven.org/maven2/org/postgresql/postgresql/42.7.4/postgresql-42.7.4.jar

# Published Maven Central SHA-1 checksums for the exact artifacts above.
ARG KAFKA_SQL_CONNECTOR_SHA1=9e7e2bb762e6cb489bcc76f2637e824fbb6f08c3
ARG JDBC_CONNECTOR_SHA1=ba3da4030b448481f2cf46aa7a0eb270241c5fa1
ARG POSTGRES_DRIVER_SHA1=264310fd7b2cd76738787dc0b9f7ea2e3b11adc1

# The official Flink image runs as the unprivileged `flink` user; installing
# packages and writing /opt/flink/lib must happen as root for the build.
USER root

# curl is required by the jar downloads below and by the compose healthcheck
# (`curl -fsS http://localhost:8081/overview`). ca-certificates is required to
# verify repo1.maven.org over HTTPS.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates; \
    rm -rf /var/lib/apt/lists/*

# Download -> size check -> ZIP magic check -> SHA-1 check -> move into lib.
# Any failure aborts the docker build, so a broken image can never be tagged.
RUN set -eux; \
    lib=/opt/flink/lib; \
    mkdir -p "$lib"; \
    fetch_jar() { \
      url="$1"; out="$2"; min_bytes="$3"; sha1="$4"; \
      echo "==> downloading ${url}"; \
      curl -fsSL --retry 5 --retry-delay 2 --connect-timeout 20 --max-time 300 \
           -o "${out}.part" "$url"; \
      size="$(wc -c < "${out}.part" | tr -d ' ')"; \
      echo "==> ${out##*/}: ${size} bytes (minimum accepted: ${min_bytes})"; \
      if [ "${size}" -lt "${min_bytes}" ]; then \
        echo "ERROR: ${url}: downloaded ${size} bytes, expected at least ${min_bytes}" >&2; \
        exit 1; \
      fi; \
      magic="$(head -c 2 "${out}.part")"; \
      if [ "${magic}" != "PK" ]; then \
        echo "ERROR: ${url}: not a jar/ZIP file (magic '${magic}')" >&2; \
        exit 1; \
      fi; \
      echo "${sha1}  ${out}.part" | sha1sum -c -; \
      mv "${out}.part" "${out}"; \
    }; \
    fetch_jar "$KAFKA_SQL_CONNECTOR_URL" \
              "$lib/flink-sql-connector-kafka-3.3.0-1.20.jar" \
              5000000 "$KAFKA_SQL_CONNECTOR_SHA1"; \
    fetch_jar "$JDBC_CONNECTOR_URL" \
              "$lib/flink-connector-jdbc-3.4.0-1.20.jar" \
              400000 "$JDBC_CONNECTOR_SHA1"; \
    fetch_jar "$POSTGRES_DRIVER_URL" \
              "$lib/postgresql-42.7.4.jar" \
              1048576 "$POSTGRES_DRIVER_SHA1"; \
    chown flink:flink "$lib"/*.jar; \
    printf '==> connector jars available in %s:\n' "$lib"; \
    ls -l "$lib"

# Restore the unprivileged runtime user of the base image (entrypoint expects
# to run as `flink`; /opt/flink is owned by flink and remains writable).
USER flink
