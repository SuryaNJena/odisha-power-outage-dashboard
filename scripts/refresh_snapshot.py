"""Load an explicit refresh into PostgreSQL and publish the map read cache."""
import csv
import fcntl
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "platform/airflow/scripts"))
sys.path.insert(0, str(ROOT / "platform/airflow/dags"))
import odisha_common as oc
import scrape_official
import load_official
import publish_mart


def build(folder):
    scraped = scrape_official.scrape(output_dir=folder)
    # A failed DISCOM request must never replace the map with a partial feed.
    for i, discom in enumerate(oc.SCRAPE_DISCOMS, 1):
        files = sorted(folder.glob(f"url_{i}_table_*.csv"))
        if not files:
            raise ValueError(f"Missing feed for {discom}")
        for path in files:
            with path.open(encoding="utf-8-sig", newline="") as f:
                header = next(csv.reader(f), [])
            if len(header) < 10 or header[1].strip().lower() not in {"name of circle", "division"}:
                raise ValueError(f"Unexpected columns for {discom}")
    load_official.load(batch_id=scraped["batch_id"], csv_dir=folder)
    # Reuse the existing staging views and rebuild only the map mart.
    subprocess.run(oc.dbt_command("run", "--select", "map_dots", "--threads", "1"),
                   shell=True, check=True)
    centroids = publish_mart.load_centroids()
    result = publish_mart.build_mart([
        publish_mart.row_to_dot(row, centroids)
        for row in publish_mart.fetch_map_dots()
    ])
    if not result["dots"]:
        raise ValueError("No usable outages; retaining the last snapshot")
    return result


def main():
    os.chdir(ROOT)
    target = Path("/snapshot/mart/outages.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    with (target.parent / ".refresh.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with tempfile.TemporaryDirectory() as temp:
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
