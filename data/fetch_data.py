"""Download the raw data into data/raw/. Run once, before build_mappings.py.

1. setlist.fm: find each venue in data/venues.csv, then pull every setlist there for 2022-2025.
   Needs SETLISTFM_API_KEY in .env (free at setlist.fm/settings/api). Rate-limited, ~10 minutes.
   Every page is cached, so you can stop and re-run.
2. Billboard Hot 100: every weekly chart since 1958, one CSV from utdata/rwd-billboard-data.

    uv run data/fetch_data.py
"""

import csv
import json
import os
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

DATA = Path(__file__).parent  # this script lives in data/
RAW = DATA / "raw"
VENUES_CSV = DATA / "venues.csv"
YEARS = [2022, 2023, 2024, 2025]
SETLISTFM = "https://api.setlist.fm/rest/1.0"
SLEEP_S = 1.1  # setlist.fm returns 429 at ~2 requests/second
HOT100_URL = "https://raw.githubusercontent.com/utdata/rwd-billboard-data/main/data-out/hot-100-current.csv"
NYC_CITIES = {"new york", "brooklyn", "queens", "bronx", "the bronx", "flushing", "forest hills"}


def get(client: httpx.Client, path: str, params: dict) -> httpx.Response:
    """GET with a pause after every call and backoff on 429."""
    for attempt in range(6):
        r = client.get(f"{SETLISTFM}{path}", params=params)
        time.sleep(SLEEP_S)
        if r.status_code != 429:
            return r
        time.sleep(2**attempt + 1)
    r.raise_for_status()
    return r


def find_venue_ids(client: httpx.Client) -> list[dict]:
    """Fill in setlistfm_id for each venue. Check the picks by hand: setlist.fm has near-duplicates
    ('Hulu Theater at Madison Square Garden'), so we prefer an exact name match."""
    rows = list(csv.DictReader(VENUES_CSV.open()))
    for row in rows:
        if row["setlistfm_id"]:
            continue
        r = get(client, "/search/venues", {"name": row["setlistfm_search_name"], "countryCode": "US"})
        venues = [] if r.status_code == 404 else r.json().get("venue", [])
        nyc = [v for v in venues if v["city"].get("stateCode") == "NY" and v["city"]["name"].lower() in NYC_CITIES]
        key = row["setlistfm_search_name"].lower()
        nyc.sort(key=lambda v: (v["name"].lower() != key, not v["name"].lower().startswith(key)))
        if nyc:
            row["setlistfm_id"] = nyc[0]["id"]
            print(f"{row['venue_name']:32} -> {nyc[0]['name']} ({nyc[0]['id']})")
        else:
            print(f"!! {row['venue_name']}: no match, fill setlistfm_id by hand")
    with VENUES_CSV.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    return rows


def fetch_setlists(client: httpx.Client, venues: list[dict]):
    out = RAW / "setlists"
    out.mkdir(parents=True, exist_ok=True)
    for v in venues:
        if not v["setlistfm_id"]:
            continue
        for year in YEARS:
            page, pages = 1, 1
            while page <= pages:
                path = out / f"{v['setlistfm_id']}_{year}_p{page:03d}.json"
                if not path.exists():
                    r = get(client, "/search/setlists", {"venueId": v["setlistfm_id"], "year": year, "p": page})
                    if r.status_code == 404:  # no setlists that year
                        break
                    r.raise_for_status()
                    path.write_text(r.text)
                body = json.loads(path.read_text())
                pages = -(-body["total"] // body["itemsPerPage"])
                page += 1
            print(f"{v['venue_name']:32} {year}: {pages} pages")


def fetch_hot100():
    path = RAW / "hot-100.csv"
    if path.exists():
        return
    r = httpx.get(HOT100_URL, timeout=120, follow_redirects=True)
    r.raise_for_status()
    path.write_bytes(r.content)
    print(f"Billboard Hot 100: {path.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    key = os.getenv("SETLISTFM_API_KEY")
    if not key:
        raise SystemExit("Set SETLISTFM_API_KEY in .env")
    RAW.mkdir(parents=True, exist_ok=True)
    with httpx.Client(headers={"x-api-key": key, "Accept": "application/json"}, timeout=30) as client:
        fetch_setlists(client, find_venue_ids(client))
    fetch_hot100()
