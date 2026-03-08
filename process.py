#!/usr/bin/env python3
"""
process.py — Reads all CSVs from scrappedData/, aggregates outage counts
by district, and writes public/outage_data.json for the static frontend.
Run this locally or via GitHub Actions after scrap.py.
"""

import csv
import json
import os
from pathlib import Path
from datetime import datetime, timezone

# Maps the "Name of Circle" CSV column to an Odisha district name.
CIRCLE_TO_DISTRICT = {
    "BBSR-1": "Khordha",
    "BBSR-2": "Khordha",
    "CUTTACK": "Cuttack",
    "PARADEEP": "Jagatsinghpur",
    "DHENKANAL": "Dhenkanal",
    "BHADRAK": "Bhadrak",
    "BARIPADA": "Mayurbhanj",
    "BALASORE": "Balasore",
    "BALESHWAR": "Balasore",
    "KEONJHAR": "Keonjhar",
    "KENDUJHAR": "Keonjhar",
    "JAJPUR": "Jajpur",
    "BARGARH": "Bargarh",
    "SAMBALPUR": "Sambalpur",
    "ROURKELA": "Sundargarh",
    "BOLANGIR": "Bolangir",
    "BALANGIR": "Bolangir",
    "KALAHANDI": "Kalahandi",
    "JEYPORE": "Koraput",
    "BERHAMPUR": "Ganjam",
    "BERHAMPUR CITY": "Ganjam",
    "ASKA": "Ganjam",
    "BHANJANAGAR": "Kandhamal",
    "RAYAGADA": "Rayagada",
    "JHARSUGUDA": "Jharsuguda",
    "KENDRAPARA": "Kendrapara",
    "NAYAGARH": "Nayagarh",
    "PURI": "Puri",
    "ANGUL": "Angul",
    "BAUDH": "Boudh",
    "GAJAPATI": "Gajapati",
    "SUBARNAPUR": "Subarnapur",
    "SONEPUR": "Subarnapur",
    "NUAPADA": "Nuapada",
    "MALKANGIRI": "Malkangiri",
    "NABARANGPUR": "Nabarangpur",
    "KORAPUT": "Koraput",
}


def load_csvs(data_dir: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    csv_dir = Path(data_dir)
    if not csv_dir.exists():
        print(f"Warning: {data_dir} not found")
        return counts

    for csv_file in sorted(csv_dir.glob("*.csv")):
        print(f"Processing {csv_file.name} …")
        try:
            with open(csv_file, newline="", encoding="utf-8-sig") as f:
                reader = csv.reader(f)
                for i, row in enumerate(reader):
                    if i == 0 or len(row) < 2:
                        continue  # skip header
                    circle = row[1].strip().upper()
                    district = CIRCLE_TO_DISTRICT.get(circle)
                    if district:
                        counts[district] = counts.get(district, 0) + 1
                    else:
                        print(f"  Unknown circle: {row[1]!r}")
        except Exception as e:
            print(f"  Error reading {csv_file.name}: {e}")

    return counts


def main():
    script_dir = Path(__file__).parent
    scraped_dir = script_dir / "scrappedData"
    output_path = script_dir / "public" / "outage_data.json"

    counts = load_csvs(str(scraped_dir))

    districts = [
        {"name": name, "value": count}
        for name, count in sorted(counts.items(), key=lambda x: -x[1])
    ]

    total = sum(d["value"] for d in districts)
    most_affected = districts[0]["name"] if districts else ""

    payload = {
        "districts": districts,
        "total": total,
        "mostAffected": most_affected,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    print(f"\n✅ Wrote {len(districts)} districts, {total} total outages → {output_path}")


if __name__ == "__main__":
    main()
