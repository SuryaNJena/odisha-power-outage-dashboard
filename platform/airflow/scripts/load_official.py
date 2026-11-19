#!/usr/bin/env python3
"""Load scraped outage CSVs into ``raw.official_outages`` (Airflow ingest step).

The parsing semantics are copied from the historical ``process.py`` at the
repository root; when that file is importable its ``parse_raw_row`` / ``row_hash``
functions are reused directly, so the row content and the sha1 ``row_hash`` are
byte-for-byte identical to the legacy pipeline.  A verbatim fallback copy lives
below for deployments where ``process.py`` is not mounted.

Circle schema (``Name of Circle`` header present), column indices::

    [1] circle (upper)  [2] division  [3] gss  [4] voltage  [5] feeder
    [6] start  [7] end  [8] reason  [9] areas  [10] remarks

Division / emergency schema (no circle header), column indices::

    [0] date  [1] division  [2] subdivision  [3] section  [4] otype
    [5] feeder  [6] cause  [7] areas  [8] start time  [9] end time  [10] remarks
    reason = "otype - cause";  circle = "EMERGENCY";  gss = section

``row_hash`` = sha1("|".join(strip(field) for field in
(schema_type, circle, division, subdivision, section, gss, voltage, feeder,
start, end, reason, areas))) - no batch id, otype or remarks, so the same
logical outage keeps a stable hash across batches (dbt dedupes on it).

Standalone usage::

    python load_official.py --batch-id 2026-11-19T12:30:00Z
    python load_official.py --batch-id ... --publish-kafka --limit 100
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterator, Sequence

_HERE = Path(__file__).resolve().parent
for _candidate in (_HERE, _HERE.parent / "dags"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import odisha_common as oc  # noqa: E402  (path bootstrap must run first)

INSERT_SQL = """
INSERT INTO raw.official_outages
    (batch_id, row_hash, source_file, schema_type, circle, division,
     subdivision, section, otype, gss, voltage, feeder, start_raw, end_raw,
     reason, areas, remarks, raw_row)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (batch_id, row_hash) DO NOTHING
"""


# ---------------------------------------------------------------------------
# process.py reuse (preferred) with a verbatim fallback
# ---------------------------------------------------------------------------

def load_legacy_process() -> ModuleType | None:
    """Import the repository's ``process.py`` if available (reuses its parsing)."""
    candidates = [
        oc.REPO_DIR / "process.py",
        Path(__file__).resolve().parents[3] / "process.py",  # repo checkout layout
        Path("/opt/odisha/process.py"),
    ]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            spec = importlib.util.spec_from_file_location("odisha_legacy_process", candidate)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            if hasattr(module, "parse_raw_row") and hasattr(module, "row_hash"):
                return module
        except Exception as exc:  # noqa: BLE001 - fall back to the embedded copy
            oc.log(f"Could not import {candidate} ({exc!r}); using embedded parser")
    return None


def _fallback_parse_raw_row(row: list, is_circle_schema: bool) -> dict | None:
    """Verbatim copy of ``process.parse_raw_row`` (kept in sync, see module doc)."""
    if is_circle_schema:
        return {
            "schema_type": "circle",
            "circle": row[1].strip().upper(),
            "division": (row[2].strip() if len(row) > 2 else ""),
            "subdivision": "",
            "section": "",
            "otype": "",
            "gss": (row[3].strip() if len(row) > 3 else ""),
            "voltage": (row[4].strip() if len(row) > 4 else ""),
            "feeder": (row[5].strip() if len(row) > 5 else ""),
            "start": (row[6].strip() if len(row) > 6 else ""),
            "end": (row[7].strip() if len(row) > 7 else ""),
            "reason": (row[8].strip() if len(row) > 8 else ""),
            "areas": (row[9].strip() if len(row) > 9 else ""),
            "remarks": (row[10].strip() if len(row) > 10 else ""),
        }
    # Emergency shutdown schema:
    # Date, Division, Subdivision, Section, Type, Feeder, Cause, Areas, From, To, Remarks
    date = row[0].strip()
    start_t = row[8].strip() if len(row) > 8 else ""
    end_t = row[9].strip() if len(row) > 9 else ""
    otype = row[4].strip() if len(row) > 4 else ""
    cause = row[6].strip() if len(row) > 6 else ""
    reason = " - ".join(x for x in (otype, cause) if x)
    return {
        "schema_type": "division",
        "circle": "EMERGENCY",
        "division": row[1].strip(),
        "subdivision": (row[2].strip() if len(row) > 2 else ""),
        "section": (row[3].strip() if len(row) > 3 else ""),
        "otype": otype,
        "gss": (row[3].strip() if len(row) > 3 else ""),
        "voltage": "",
        "feeder": (row[5].strip() if len(row) > 5 else ""),
        "start": f"{date} {start_t}".strip(),
        "end": f"{date} {end_t}".strip(),
        "reason": reason,
        "areas": (row[7].strip() if len(row) > 7 else ""),
        "remarks": (row[10].strip() if len(row) > 10 else ""),
    }


def _fallback_row_hash(*fields: Any) -> str:
    """Verbatim copy of ``process.row_hash`` (sha1 over stripped pipe-joined fields)."""
    raw = "|".join((field or "").strip() for field in fields)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def parser_functions() -> tuple[Callable[..., dict | None], Callable[..., str], str]:
    """Return ``(parse_raw_row, row_hash, source)`` preferring the legacy module."""
    legacy = load_legacy_process()
    if legacy is not None:
        return legacy.parse_raw_row, legacy.row_hash, "process.py"
    return _fallback_parse_raw_row, _fallback_row_hash, "embedded-fallback"


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------

def iter_csv_records(
    csv_files: Sequence[Path],
    parse_raw_row: Callable[..., dict | None],
    row_hash: Callable[..., str],
) -> Iterator[dict[str, Any]]:
    """Yield normalised outage records from every CSV file.

    Reproduces ``process.ingest_csvs`` row handling exactly: UTF-8-BOM tolerant,
    header detected by the ``Name of Circle`` column, rows shorter than 2 columns
    skipped, parse errors per file logged and skipped.
    """
    for csv_file in csv_files:
        try:
            with open(csv_file, newline="", encoding="utf-8-sig") as handle:
                reader = csv.reader(handle)
                header = next(reader, [])
                is_circle_schema = any("Name of Circle" in (column or "").strip() for column in header)
                oc.log(f"Parsing {csv_file.name} (schema={'circle' if is_circle_schema else 'division'})")
                for line_number, row in enumerate(reader, start=2):
                    if len(row) < 2:
                        continue
                    rec = parse_raw_row(row, is_circle_schema)
                    if rec is None:
                        continue
                    digest = row_hash(
                        rec["schema_type"],
                        rec["circle"],
                        rec["division"],
                        rec["subdivision"],
                        rec["section"],
                        rec["gss"],
                        rec["voltage"],
                        rec["feeder"],
                        rec["start"],
                        rec["end"],
                        rec["reason"],
                        rec["areas"],
                    )
                    yield {
                        "source_file": csv_file.name,
                        "line_number": line_number,
                        "row_hash": digest,
                        "record": rec,
                        "raw_row": row,
                    }
        except Exception as exc:  # noqa: BLE001 - keep loading the other files
            oc.log(f"Error reading {csv_file.name}: {exc!r}")


# ---------------------------------------------------------------------------
# Batch helpers
# ---------------------------------------------------------------------------

def resolve_batch_id(batch_id: str | None) -> str:
    """Use the provided batch id, else the most recent running batch, else now."""
    if batch_id:
        return batch_id
    try:
        rows = oc.execute_fetch_all(
            "SELECT batch_id FROM raw.scrape_batches WHERE status = 'running' "
            "ORDER BY started_at DESC LIMIT 1"
        )
        if rows:
            return str(rows[0]["batch_id"])
    except Exception as exc:  # noqa: BLE001 - fall through to a fresh batch id
        oc.log(f"Could not read running batches ({exc!r}); starting a new batch")
    return oc.iso_z(oc.utc_now().replace(second=0, microsecond=0))


def _ensure_batch_row(batch_id: str) -> None:
    """Make sure a ``raw.scrape_batches`` row exists before loading rows."""
    rows = oc.execute_fetch_all(
        "SELECT batch_id, status FROM raw.scrape_batches WHERE batch_id = %s",
        (batch_id,),
    )
    if not rows:
        oc.mark_scrape_batch(batch_id, "running", detail={"origin": "load_official"})


def _mark_batch_success(batch_id: str, parsed: int, detail: dict[str, Any]) -> None:
    with oc.db_connection(autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE raw.scrape_batches
                   SET row_count = %s,
                       status = 'success',
                       finished_at = now(),
                       detail = COALESCE(detail, '{}'::jsonb) || %s::jsonb
                 WHERE batch_id = %s
                """,
                (parsed, oc.psycopg2_json(detail), batch_id),
            )


def _mark_batch_failed(batch_id: str, error: str, detail: dict[str, Any]) -> None:
    oc.mark_scrape_batch(
        batch_id,
        "failed",
        detail={**detail, "error": error},
        finished=True,
    )


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load(batch_id: str | None = None, csv_dir: str | Path | None = None) -> dict[str, Any]:
    """Parse the batch CSVs and insert rows into ``raw.official_outages``.

    Idempotent: ``unique (batch_id, row_hash)`` plus ``ON CONFLICT DO NOTHING``
    means retries simply skip the rows that are already present.
    """
    batch_id = resolve_batch_id(batch_id)
    source_dir = Path(csv_dir) if csv_dir else oc.scraped_data_dir(create=False)
    oc.log(f"Loading batch {batch_id} from {source_dir}")
    _ensure_batch_row(batch_id)

    parse_raw_row, row_hash, parser_source = parser_functions()
    csv_files = sorted(source_dir.glob("*.csv"))
    summary: dict[str, Any] = {
        "batch_id": batch_id,
        "csv_dir": str(source_dir),
        "parser": parser_source,
        "file_count": len(csv_files),
        "files": [path.name for path in csv_files],
        "parsed": 0,
        "inserted": 0,
        "skipped_existing": 0,
    }
    if not csv_files:
        error = f"No CSV files in {source_dir}; run scrape_official.py first."
        _mark_batch_failed(batch_id, error, summary)
        raise RuntimeError(error)

    try:
        file_stats: dict[str, dict[str, int]] = {}
        with oc.db_connection() as conn:
            with conn.cursor() as cur:
                for record in iter_csv_records(csv_files, parse_raw_row, row_hash):
                    rec = record["record"]
                    cur.execute(
                        INSERT_SQL,
                        (
                            batch_id,
                            record["row_hash"],
                            record["source_file"],
                            rec["schema_type"],
                            rec["circle"],
                            rec["division"],
                            rec["subdivision"],
                            rec["section"],
                            rec["otype"],
                            rec["gss"],
                            rec["voltage"],
                            rec["feeder"],
                            rec["start"],
                            rec["end"],
                            rec["reason"],
                            rec["areas"],
                            rec["remarks"],
                            oc.psycopg2_json(record["raw_row"]),
                        ),
                    )
                    inserted = int(cur.rowcount or 0)
                    summary["parsed"] += 1
                    summary["inserted"] += inserted
                    stats = file_stats.setdefault(record["source_file"], {"parsed": 0, "inserted": 0})
                    stats["parsed"] += 1
                    stats["inserted"] += inserted
        summary["skipped_existing"] = summary["parsed"] - summary["inserted"]
        summary["per_file"] = file_stats
        _mark_batch_success(batch_id, summary["parsed"], summary)
        oc.log(
            f"Loaded batch {batch_id}: {summary['parsed']} parsed, "
            f"{summary['inserted']} new rows ({summary['skipped_existing']} already present)"
        )
        return summary
    except Exception as exc:
        _mark_batch_failed(batch_id, repr(exc), summary)
        raise


# ---------------------------------------------------------------------------
# Kafka publication (official-outages topic)
# ---------------------------------------------------------------------------

def publish_batch_to_kafka(batch_id: str, limit: int | None = None) -> dict[str, Any]:
    """Publish the rows of ``batch_id`` to the ``official-outages`` Kafka topic."""
    import kafka_util

    sql = """
        SELECT batch_id, row_hash, schema_type, circle, division, subdivision,
               section, otype, gss, voltage, feeder, start_raw, end_raw,
               reason, areas, remarks
          FROM raw.official_outages
         WHERE batch_id = %s
         ORDER BY id
    """
    params: list[Any] = [batch_id]
    if limit:
        sql += " LIMIT %s"
        params.append(int(limit))
    rows = oc.execute_fetch_all(sql, tuple(params))
    events = [
        {
            "batch_id": row["batch_id"],
            "row_hash": row["row_hash"],
            "schema_type": row["schema_type"],
            "circle": row["circle"],
            "division": row["division"],
            "subdivision": row["subdivision"],
            "section": row["section"],
            "otype": row["otype"],
            "gss": row["gss"],
            "voltage": row["voltage"],
            "feeder": row["feeder"],
            "start_raw": row["start_raw"],
            "end_raw": row["end_raw"],
            "reason": row["reason"],
            "areas": row["areas"],
            "remarks": row["remarks"],
        }
        for row in rows
    ]
    published = kafka_util.publish_many(oc.TOPIC_OFFICIAL_OUTAGES, events, key_field="row_hash")
    oc.log(f"Published {published} official outage rows to {oc.TOPIC_OFFICIAL_OUTAGES}")
    return {"batch_id": batch_id, "topic": oc.TOPIC_OFFICIAL_OUTAGES, "published": published}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Load scraped outage CSVs into raw.official_outages.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--batch-id", default=None, help="batch id (default: latest running batch, else now)")
    parser.add_argument("--csv-dir", default=None, help="CSV folder (default: $SCRAPED_DIR resolution)")
    parser.add_argument("--publish-kafka", action="store_true", help="also publish the loaded rows to Kafka")
    parser.add_argument("--limit", type=int, default=None, help="max Kafka messages to publish")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = load(batch_id=args.batch_id, csv_dir=args.csv_dir)
    if args.publish_kafka:
        summary["kafka"] = publish_batch_to_kafka(summary["batch_id"], limit=args.limit)
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
