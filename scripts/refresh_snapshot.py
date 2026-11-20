"""Run via the manual Compose service. Only the final JSON is persisted."""
import csv
import fcntl
import json
import os
import sqlite3
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import process
import scrap


def build(folder):
    # A failed DISCOM request must never replace the map with a partial feed.
    for i, discom in enumerate(scrap.DISCOMS, 1):
        files = sorted(folder.glob(f"url_{i}_table_*.csv"))
        if not files:
            raise ValueError(f"Missing feed for {discom}")
        for path in files:
            with path.open(encoding="utf-8-sig", newline="") as f:
                header = next(csv.reader(f), [])
            if len(header) < 10 or header[1].strip().lower() not in {"name of circle", "division"}:
                raise ValueError(f"Unexpected columns for {discom}")
    with sqlite3.connect(":memory:") as db:
        db.executescript(process.SCHEMA_SQL)
        process.ingest_csvs(str(folder), db)
        rows = process.load_raw_rows(db, None)
        process.write_outages_table(db, rows, process.load_aliases(process.ALIASES_PATH),
            json.loads(process.CENTROIDS_PATH.read_text()), process.load_geocode_cache(), cached_only=True)
        dots = process.load_processed(db)
    if not dots:
        raise ValueError("No usable outages; retaining the last snapshot")
    counts = Counter(dot["district"] for dot in dots)
    return {"updatedAt": datetime.now(timezone.utc).isoformat(), "total": len(dots),
            "districts": [{"name": k, "value": v} for k, v in counts.most_common()], "dots": dots}


def main():
    os.chdir(ROOT)
    target = Path("/snapshot/mart/outages.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    with (target.parent / ".refresh.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with tempfile.TemporaryDirectory() as temp:
            scrap.scrape_all(temp)
            data = build(Path(temp))
        with tempfile.NamedTemporaryFile(mode="w", dir=target.parent, delete=False) as out:
            tmp = Path(out.name)
        try:
            tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf8")
            tmp.chmod(0o644)
            tmp.replace(target)
        finally:
            tmp.unlink(missing_ok=True)
        print(f"Published {data['total']} outages at {data['updatedAt']}")


if __name__ == "__main__":
    main()
