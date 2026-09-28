#!/usr/bin/env python3
"""Resumable catch-up crawler for the NAV job vacancy feed.

The feed (https://pam-stilling-feed.nav.no) is a strictly forward, append-only changelog with no
backward or date-jump pagination (verified live 2026-09-28 against the real API and its OpenAPI
spec: `If-Modified-Since` only re-checks a page you already hold, and `?last` returns only the
single newest change event). Reaching "now" from page 0 requires walking forward through what is
very likely several hundred thousand to a few million historical change-events, so this script is
designed to be run repeatedly (ideally daily) across the competition's remaining run window,
resuming from `data/nav-jobs-cursor.json` each time and making bounded, honest progress.

Usage:
    uv run python scripts/build_nav_index.py --pages 500 \
        --index data/nav-jobs-index.jsonl.gz --cursor data/nav-jobs-cursor.json
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.evidence import utc_now  # noqa: E402

UA = "builderr-signalpost/1.0 (+mailto:akaykatia9@gmail.com)"
TOKEN_URL = "https://pam-stilling-feed.nav.no/api/publicToken"
FEED_URL = "https://pam-stilling-feed.nav.no/api/v1/feed"


def fetch_public_token() -> str:
    request = urllib.request.Request(TOKEN_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=15) as response:
        body = response.read().decode("utf-8")
    # Response is a short human-readable line followed by the token; the token is the last line.
    return body.strip().splitlines()[-1].strip()


def fetch_page(url: str, token: str) -> dict[str, Any] | None:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return None
        raise


def fetch_entry_detail(detail_url: str, token: str) -> dict[str, Any] | None:
    url = f"https://pam-stilling-feed.nav.no{detail_url}" if detail_url.startswith("/") else detail_url
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read())
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return None


def load_index(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    index: dict[str, dict[str, Any]] = {}
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                index[row["ad_uuid"]] = row
    return index


def save_index(path: Path, index: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as handle:
        for row in index.values():
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Resumable NAV job-feed catch-up crawler")
    parser.add_argument("--index", default="data/nav-jobs-index.jsonl.gz")
    parser.add_argument("--cursor", default="data/nav-jobs-cursor.json")
    parser.add_argument("--pages", type=int, default=500)
    parser.add_argument("--time-limit", type=float, default=300.0, help="Wall-clock seconds before stopping early")
    args = parser.parse_args()

    index_path = Path(args.index)
    cursor_path = Path(args.cursor)
    index = load_index(index_path)
    cursor = json.loads(cursor_path.read_text(encoding="utf-8")) if cursor_path.exists() else {}
    url = cursor.get("next_url") or FEED_URL

    token = fetch_public_token()
    started = time.monotonic()
    pages_processed = 0
    upserts = 0
    deletions = 0
    detail_fetches = 0
    detail_missing_orgnr = 0
    caught_up = False

    for _ in range(args.pages):
        if time.monotonic() - started >= args.time_limit:
            break
        page = fetch_page(url, token)
        if page is None:
            break
        pages_processed += 1
        for item in page.get("items", []):
            entry = item["_feed_entry"]
            ad_uuid = entry["uuid"]
            if entry.get("status") == "ACTIVE":
                previously_seen = index.get(ad_uuid)
                if previously_seen is not None and previously_seen.get("sist_endret") == entry.get("sistEndret"):
                    continue  # unchanged since last seen active; no need to re-fetch the detail
                detail = fetch_entry_detail(item["url"], token)
                detail_fetches += 1
                ad_content = (detail or {}).get("ad_content") or {}
                employer = ad_content.get("employer") or {}
                orgnr = employer.get("orgnr")
                if not orgnr:
                    detail_missing_orgnr += 1
                    continue  # cannot be published without an organisation number to match against
                index[ad_uuid] = {
                    "ad_uuid": ad_uuid,
                    "orgnr": orgnr,
                    "title": ad_content.get("title") or entry.get("title"),
                    "business_name": employer.get("name") or entry.get("businessName"),
                    "municipal": entry.get("municipal"),
                    "published": ad_content.get("published"),
                    "expires": ad_content.get("expires"),
                    "sist_endret": entry.get("sistEndret"),
                    "detail_url": item["url"],
                }
                upserts += 1
            elif ad_uuid in index:
                del index[ad_uuid]
                deletions += 1
        next_url = page.get("next_url")
        next_id = page.get("next_id")
        if not next_url or not next_id:
            caught_up = True
            break
        url = f"https://pam-stilling-feed.nav.no{next_url}" if next_url.startswith("/") else next_url

    save_index(index_path, index)
    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    cursor_path.write_text(json.dumps({
        "next_url": None if caught_up else url,
        "caught_up": caught_up,
        "pages_processed_this_run": pages_processed,
        "index_size": len(index),
        "updated_at": utc_now(),
    }, indent=2), encoding="utf-8")

    print(json.dumps({
        "pages_processed_this_run": pages_processed,
        "upserts_this_run": upserts,
        "deletions_this_run": deletions,
        "detail_fetches_this_run": detail_fetches,
        "detail_missing_orgnr_this_run": detail_missing_orgnr,
        "index_size": len(index),
        "caught_up": caught_up,
        "elapsed_s": round(time.monotonic() - started, 1),
    }, indent=2))


if __name__ == "__main__":
    main()
