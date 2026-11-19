#!/usr/bin/env python3
"""Scrape the Kavach outage feed and register a ``raw.scrape_batches`` row.

This is the Airflow-friendly wrapper around the repository scraper
(``scrap.py`` at the repository root): it reuses that module instead of
duplicating the ASP.NET session/table-extraction logic, writes the CSV files
into ``$SCRAPED_DIR`` and records the batch lifecycle in Postgres.

Batch lifecycle (see platform/CONTRACT.md):

1. ``scrape_official.py`` inserts ``raw.scrape_batches`` with ``status='running'``
   *before* loading rows (here, before/while scraping);
2. ``load_official.py`` updates the same row to ``status='success'`` (or
   ``failed``) with ``finished_at`` and ``row_count``.

The batch id is derived from the Airflow DAG run (see
:func:`odisha_common.batch_id_for_context`), i.e. the ``data_interval_start``
floored to the minute as UTC ISO-8601 ``...Z``.  Retries therefore reuse the
same batch id and the whole ingest is idempotent.

Standalone usage::

    python scrape_official.py --batch-id 2026-11-19T12:30:00Z
    python scrape_official.py --discoms CODL SODL --output-dir /tmp/csvs
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Sequence

_HERE = Path(__file__).resolve().parent
for _candidate in (_HERE, _HERE.parent / "dags"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import odisha_common as oc  # noqa: E402  (path bootstrap must run first)


# ---------------------------------------------------------------------------
# Legacy scraper loading
# ---------------------------------------------------------------------------

def _scraper_candidates() -> list[Path]:
    """Possible locations of the repository ``scrap.py``."""
    return [
        oc.REPO_DIR / "scrap.py",
        Path(__file__).resolve().parents[3] / "scrap.py",  # repo checkout layout
        Path("/opt/odisha/scrap.py"),
    ]


def load_scraper_module() -> ModuleType:
    """Import the repository scraper (``scrap.py``) as a module.

    The scraper lives at the repository root and is reused verbatim; when the
    module is not importable from ``sys.path`` (e.g. different mount layout) it is
    loaded directly from the expected file path so Docker and local runs behave
    identically.
    """
    repo = str(oc.REPO_DIR)
    if repo not in sys.path:
        sys.path.insert(0, repo)
    try:
        import scrap  # type: ignore  # noqa: F401 - imported for side effect below

        return scrap
    except ImportError:
        pass
    for candidate in _scraper_candidates():
        if not candidate.is_file():
            continue
        spec = importlib.util.spec_from_file_location("odisha_legacy_scrap", candidate)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    raise RuntimeError(
        "Could not import the repository scrap.py; looked at: "
        + ", ".join(str(path) for path in _scraper_candidates())
        + ". Mount the repository at $REPO_DIR."
    )


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def clean_csv_dir(csv_dir: Path) -> list[str]:
    """Remove CSV files from a previous batch; returns the removed file names."""
    removed: list[str] = []
    for csv_file in sorted(Path(csv_dir).glob("*.csv")):
        try:
            csv_file.unlink()
            removed.append(csv_file.name)
        except OSError as exc:  # pragma: no cover - defensive
            oc.log(f"Could not remove stale CSV {csv_file}: {exc!r}")
    return removed


def count_csv_rows(csv_files: Sequence[Path]) -> int:
    """Count data rows (excluding headers) across the scraped CSV files."""
    total = 0
    for csv_file in csv_files:
        try:
            with open(csv_file, newline="", encoding="utf-8-sig") as handle:
                total += max(0, sum(1 for _ in handle) - 1)
        except OSError as exc:
            oc.log(f"Could not count rows in {csv_file}: {exc!r}")
    return total


# ---------------------------------------------------------------------------
# Batch helpers
# ---------------------------------------------------------------------------

def _batch_detail(batch_id: str, **extra: Any) -> dict[str, Any]:
    detail: dict[str, Any] = {"batch_id": batch_id, "source_url": oc.SCRAPE_URL}
    detail.update(extra)
    return detail


def scrape(
    batch_id: str | None = None,
    output_dir: str | Path | None = None,
    discoms: Sequence[str] | None = None,
    keep_existing: bool = False,
) -> dict[str, Any]:
    """Run the scraper and return a JSON-serialisable summary.

    Raises :class:`RuntimeError` when no tables were scraped so Airflow retries
    the task instead of silently marking an empty batch as successful.
    """
    batch_id = batch_id or oc.iso_z(oc.utc_now().replace(second=0, microsecond=0))
    target_dir = oc.ensure_dir(Path(output_dir) if output_dir else oc.scraped_data_dir(create=True))
    discom_list = list(discoms) if discoms else list(oc.SCRAPE_DISCOMS)

    # Contract: create the batch row with status='running' before loading rows.
    oc.mark_scrape_batch(
        batch_id,
        "running",
        detail=_batch_detail(batch_id, scraped_dir=str(target_dir), discoms=discom_list),
    )

    try:
        removed = [] if keep_existing else clean_csv_dir(target_dir)
        oc.log(f"Scraping {len(discom_list)} DISCOMs from {oc.SCRAPE_URL} into {target_dir}")
        scraper = load_scraper_module()
        if discom_list:
            scraper.DISCOMS = discom_list
        scraper.scrape_all(output_folder=str(target_dir))

        csv_files = sorted(target_dir.glob("*.csv"))
        row_count = count_csv_rows(csv_files)
        if not csv_files:
            raise RuntimeError(
                f"Scraper produced no CSV tables in {target_dir}; the Kavach site may be "
                "unreachable or its table markup changed."
            )
        detail = _batch_detail(
            batch_id,
            scraped_dir=str(target_dir),
            discoms=discom_list,
            files=[path.name for path in csv_files],
            file_count=len(csv_files),
            rows=row_count,
            removed_old_files=len(removed),
        )
        # Keep status='running': load_official flips it to 'success' once the rows
        # are in raw.official_outages (contract ordering).
        oc.mark_scrape_batch(batch_id, "running", row_count=row_count, detail=detail)
        summary = {
            "batch_id": batch_id,
            "scraped_dir": str(target_dir),
            "file_count": len(csv_files),
            "files": [path.name for path in csv_files],
            "rows": row_count,
            "removed_old_files": removed,
        }
        oc.log(f"Scrape complete: {summary['file_count']} files / {row_count} rows (batch {batch_id})")
        return summary
    except Exception as exc:
        oc.mark_scrape_batch(
            batch_id,
            "failed",
            detail=_batch_detail(batch_id, error=repr(exc), scraped_dir=str(target_dir)),
            finished=True,
        )
        raise


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scrape kavach.tpodisha.com into CSVs and register a raw.scrape_batches row.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--batch-id", default=None, help="batch id (UTC ISO8601 ending in Z); defaults to now")
    parser.add_argument("--output-dir", default=None, help="CSV output folder (default: $SCRAPED_DIR resolution)")
    parser.add_argument(
        "--discoms",
        nargs="+",
        default=None,
        help=f"override DISCOM list (default: {' '.join(oc.SCRAPE_DISCOMS)})",
    )
    parser.add_argument("--keep-existing", action="store_true", help="do not delete CSVs from previous batches")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = scrape(
        batch_id=args.batch_id,
        output_dir=args.output_dir,
        discoms=args.discoms,
        keep_existing=args.keep_existing,
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
