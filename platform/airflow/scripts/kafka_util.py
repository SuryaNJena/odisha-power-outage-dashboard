#!/usr/bin/env python3
"""Kafka helpers for the Odisha outage platform.

* :func:`ensure_topics` - idempotent topic creation through ``KafkaAdminClient``
  (3 partitions, replication 1, 24h retention unless overridden by env).
* :func:`get_producer` - shared JSON byte producer with retries and idempotence.
* :func:`publish_many` - publish a batch of JSON documents and surface failures.

The module is import-safe without a broker (connections are lazy) and can be run
directly::

    python kafka_util.py --ensure-topics
    python kafka_util.py --list
    python kafka_util.py --wait 60

Environment: ``KAFKA_BOOTSTRAP`` (default ``kafka:9092``), plus the topic knobs
``KAFKA_TOPIC_PARTITIONS``, ``KAFKA_TOPIC_REPLICATION``, ``KAFKA_RETENTION_MS``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

_HERE = Path(__file__).resolve().parent
for _candidate in (_HERE, _HERE.parent / "dags"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import odisha_common as oc  # noqa: E402  (path bootstrap must run first)

_PRODUCER = None
_ADMIN = None
_ENSURED_TOPICS: set[str] = set()


# ---------------------------------------------------------------------------
# Topic management
# ---------------------------------------------------------------------------

def _admin_client():
    """Create (and cache) a ``KafkaAdminClient``."""
    global _ADMIN
    if _ADMIN is None:
        from kafka.admin import KafkaAdminClient

        _ADMIN = KafkaAdminClient(
            bootstrap_servers=oc.KAFKA_BOOTSTRAP,
            client_id="odisha-airflow-admin",
            request_timeout_ms=15000,
            api_version_auto_timeout_ms=10000,
        )
    return _ADMIN


def ensure_topics(topics: Sequence[str] | None = None) -> dict[str, Any]:
    """Create any missing topics; safe to call on every pipeline run.

    ``kafka-python-ng``'s ``KafkaAdminClient.create_topics`` blocks until the
    controller responds and raises the broker error directly (instead of
    returning futures), so a duplicate-topic race surfaces as
    ``TopicAlreadyExistsError`` and is treated as success.
    """
    from kafka.admin import NewTopic
    from kafka.errors import TopicAlreadyExistsError

    requested = list(topics or oc.ALL_TOPICS)
    admin = _admin_client()
    existing = set(admin.list_topics())
    missing = [name for name in requested if name not in existing]
    created: list[str] = []
    if missing:
        new_topics = [
            NewTopic(
                name=name,
                num_partitions=oc.KAFKA_TOPIC_PARTITIONS,
                replication_factor=oc.KAFKA_TOPIC_REPLICATION,
                topic_configs={
                    "retention.ms": str(oc.KAFKA_RETENTION_MS),
                    "cleanup.policy": "delete",
                },
            )
            for name in missing
        ]
        try:
            admin.create_topics(new_topics=new_topics, validate_only=False)
            created = list(missing)
        except TopicAlreadyExistsError:
            # A concurrent producer/bootstrap created them first; re-list below.
            created = []
    present = set(admin.list_topics())
    return {
        "requested": requested,
        "created": sorted(created),
        "already_existing": sorted(set(requested) & existing),
        "missing": sorted(name for name in requested if name not in present),
    }


def list_topics() -> list[str]:
    """Return all topic names visible on the broker."""
    return sorted(_admin_client().list_topics())


def check_topics(topics: Sequence[str] | None = None) -> dict[str, Any]:
    """Health-check helper: report whether the contract topics exist."""
    requested = list(topics or oc.ALL_TOPICS)
    try:
        present = set(list_topics())
        missing = [name for name in requested if name not in present]
        return {"ok": not missing, "topics": requested, "missing": missing}
    except Exception as exc:  # noqa: BLE001 - health checks must not explode
        return {"ok": False, "topics": requested, "missing": requested, "error": repr(exc)}


def wait_for_kafka(timeout_seconds: int = 120, interval_seconds: float = 3.0) -> bool:
    """Block until the Kafka broker answers, or return ``False`` on timeout."""
    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            list_topics()
            return True
        except Exception as exc:  # noqa: BLE001 - retry loop
            last_error = exc
            time.sleep(interval_seconds)
    oc.log(f"Kafka not reachable after {timeout_seconds}s: {last_error!r}")
    return False


# ---------------------------------------------------------------------------
# Producer
# ---------------------------------------------------------------------------

def get_producer():
    """Return the shared ``KafkaProducer`` (created on first use)."""
    global _PRODUCER
    if _PRODUCER is not None:
        return _PRODUCER
    from kafka import KafkaProducer

    # NOTE: kafka-python-ng supports the classic producer config only (no
    # enable_idempotence / delivery_timeout_ms); at-least-once delivery is
    # provided by acks=all + retries + a single in-flight request per connection.
    _PRODUCER = KafkaProducer(
        bootstrap_servers=oc.KAFKA_BOOTSTRAP,
        client_id="odisha-airflow-producer",
        acks="all",
        retries=5,
        linger_ms=50,
        max_in_flight_requests_per_connection=1,
        request_timeout_ms=30000,
        value_serializer=lambda value: json.dumps(
            value, default=str, ensure_ascii=False
        ).encode("utf-8"),
        key_serializer=lambda key: str(key).encode("utf-8") if key is not None else None,
    )
    return _PRODUCER


def publish_many(
    topic: str,
    events: Iterable[Mapping[str, Any]],
    key_field: str | None = None,
    *,
    ensure: bool = True,
    flush_timeout: float = 120.0,
) -> int:
    """Publish JSON documents to ``topic``; raises when the broker rejects any.

    ``key_field`` selects the event field used as the Kafka message key (a stable
    partition key, e.g. ``district`` for weather or ``row_hash`` for outages).
    """
    if ensure and topic not in _ENSURED_TOPICS:
        ensure_topics([topic])
        _ENSURED_TOPICS.add(topic)
    producer = get_producer()
    futures = []
    count = 0
    for event in events:
        key = event.get(key_field) if key_field else None
        futures.append(producer.send(topic, value=dict(event), key=key))
        count += 1
    if not futures:
        return 0
    producer.flush(timeout=flush_timeout)
    errors: list[str] = []
    for future in futures:
        try:
            future.get(timeout=flush_timeout)
        except Exception as exc:  # noqa: BLE001 - collected and raised together
            errors.append(repr(exc))
    if errors:
        raise RuntimeError(
            f"Kafka publish to {topic!r} failed for {len(errors)}/{count} messages; "
            f"first error: {errors[0]}"
        )
    return count


def publish_json(topic: str, event: Mapping[str, Any], key: str | None = None) -> None:
    """Publish a single JSON document (convenience wrapper around :func:`publish_many`)."""
    payload = dict(event)
    if key is not None:
        payload.setdefault("_key", key)
    publish_many(topic, [payload], key_field="_key" if key is not None else None)


def close() -> None:
    """Flush and close the cached producer/admin clients."""
    global _PRODUCER, _ADMIN
    if _PRODUCER is not None:
        try:
            _PRODUCER.flush(timeout=30)
            _PRODUCER.close(timeout=30)
        except Exception:  # noqa: BLE001 - best effort cleanup
            pass
        _PRODUCER = None
    if _ADMIN is not None:
        try:
            _ADMIN.close()
        except Exception:  # noqa: BLE001 - best effort cleanup
            pass
        _ADMIN = None
    _ENSURED_TOPICS.clear()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ensure-topics", action="store_true", help="create any missing platform topics (default)")
    parser.add_argument("--list", action="store_true", help="list existing topics and exit")
    parser.add_argument("--wait", type=int, default=0, metavar="SECONDS", help="wait for the broker before acting")
    parser.add_argument("--topics", nargs="*", default=None, help="override the topic list")
    args = parser.parse_args(argv)
    if args.wait:
        if not wait_for_kafka(args.wait):
            return 1
    if args.list:
        print(json.dumps(list_topics(), indent=2))
        return 0
    result = ensure_topics(args.topics)
    print(json.dumps(result, indent=2))
    return 0 if not result["missing"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
