"""
scrap.py — Fetches outage tables from kavach.tpodisha.com for all 4 DISCOMs
and saves them as CSV files into scrappedData/.

The site uses ASP.NET session tokens in URLs, but a fresh GET to the
base page will redirect and create a new session automatically.
This script handles that by first hitting the base URL to get a valid
session, then fetching each DISCOM's report page.
"""

from io import StringIO
import requests
from bs4 import BeautifulSoup
import pandas as pd
import os
from pathlib import Path

# DISCOMs to scrape (Central/Northern/Western/Southern Odisha Distribution Ltd)
DISCOMS = ["CODL", "NODL", "WODL", "SODL"]
BASE_URL = "https://kavach.tpodisha.com"
REPORT_PATH = "/Reports/PSDCases"


def get_session_url(session: requests.Session) -> str:
    """
    Performs an initial GET to the base URL to obtain a valid ASP.NET
    session and returns the redirected URL (which contains the session token).
    """
    resp = session.get(BASE_URL, timeout=30, allow_redirects=True)
    resp.raise_for_status()
    # The resolved URL now contains the session path, e.g. /(S(abc123))/
    # Strip trailing slash/path and return just the origin + session segment
    parts = resp.url.rstrip("/").split("/")
    # Find the session segment (S(...))
    session_segment = ""
    for part in parts:
        if part.startswith("(S(") or part.startswith("(s("):
            session_segment = f"/{part}"
            break
    return f"{BASE_URL}{session_segment}"


def extract_tables_from_url(session: requests.Session, url: str) -> list:
    try:
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"  Error fetching {url}: {e}")
        return []

    soup = BeautifulSoup(resp.content, "html.parser")
    tables = soup.find_all("table")
    result = []
    for table in tables:
        try:
            df = pd.read_html(StringIO(str(table)))[0]
            result.append(df)
        except (ValueError, Exception) as e:
            print(f"  Skipping table: {e}")
    return result


def scrape_all(output_folder: str = "scrappedData"):
    Path(output_folder).mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    })

    print("Establishing session with kavach.tpodisha.com …")
    try:
        base = get_session_url(session)
        print(f"Session base: {base}")
    except Exception as e:
        print(f"Warning: could not get session URL ({e}), using plain base URL")
        base = BASE_URL

    for i, discom in enumerate(DISCOMS, start=1):
        url = f"{base}{REPORT_PATH}?discom={discom}"
        print(f"\n--- [{i}/{len(DISCOMS)}] Fetching {discom}: {url} ---")
        tables = extract_tables_from_url(session, url)

        if not tables:
            print("  No tables found.")
            continue

        for j, df in enumerate(tables, start=1):
            fname = f"url_{i}_table_{j}.csv"
            fpath = os.path.join(output_folder, fname)
            try:
                df.to_csv(fpath, index=False, encoding="utf-8")
                print(f"  Saved {fname} ({len(df)} rows)")
            except Exception as e:
                print(f"  Error saving {fname}: {e}")


if __name__ == "__main__":
    scrape_all()