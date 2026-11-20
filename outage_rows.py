"""Shared Kavach feed parser and stable row identity for both ingestion paths."""
import hashlib


def row_hash(*fields) -> str:
    raw = "|".join((f or "").strip() for f in fields)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def parse_raw_row(row: list, is_circle_schema: bool) -> dict | None:
    """Convert one raw CSV row into a normalized dict (returns None if unusable)."""
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
