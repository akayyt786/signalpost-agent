from __future__ import annotations

import gzip
import hashlib
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..envelope import EnvelopeBuilder
from ..evidence import utc_now

UA = "builderr-signalpost/1.0 (+mailto:akaykatia9@gmail.com)"
TOKEN_URL = "https://pam-stilling-feed.nav.no/api/publicToken"
FEED_BASE = "https://pam-stilling-feed.nav.no"
TERMS_URL = "https://arbeidsplassen.nav.no/vilkar-api"


def fetch_public_token() -> str | None:
    request = urllib.request.Request(TOKEN_URL, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.read().decode("utf-8").strip().splitlines()[-1].strip()
    except (urllib.error.URLError, TimeoutError):
        return None


def load_index_by_org(path: str | Path) -> dict[str, list[dict[str, Any]]]:
    """The shipped data/nav-jobs-index.jsonl.gz, grouped by orgnr for O(1) per-company lookup."""
    by_org: dict[str, list[dict[str, Any]]] = {}
    source = Path(path)
    if not source.exists():
        return by_org
    with gzip.open(source, "rt", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                org = row.get("orgnr")
                if org:
                    by_org.setdefault(org, []).append(row)
    return by_org


def make_detail_fetcher(token: str | None) -> Callable[[str], dict[str, Any] | None]:
    def fetch(detail_url: str) -> dict[str, Any] | None:
        if not token:
            return None
        url = f"{FEED_BASE}{detail_url}" if detail_url.startswith("/") else detail_url
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": UA})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.loads(response.read())
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            return None
    return fetch


def _is_expired(expires: str | None) -> bool:
    if not expires:
        return False
    try:
        text = expires[:-1] + "+00:00" if expires.endswith("Z") else expires
        return datetime.fromisoformat(text) < datetime.now(timezone.utc)
    except ValueError:
        return False


def populate_jobs(
    builder: EnvelopeBuilder,
    org: str,
    index_by_org: dict[str, list[dict[str, Any]]],
    *,
    detail_fetcher: Callable[[str], dict[str, Any] | None],
    max_ads: int = 5,
) -> None:
    """Publish job_postings only for ads whose *just re-fetched* detail confirms status == ACTIVE,
    employer.orgnr == org and expires is in the future. Never publishes contactList. An ad whose
    detail re-fetch fails is `deferred` (carried, not dropped); an ad confirmed inactive/expired is
    silently excluded from this run's claims (correct, not an error).
    """
    candidates = sorted(index_by_org.get(org, []), key=lambda row: row.get("sist_endret") or "", reverse=True)[:max_ads]
    if not candidates:
        builder.set_availability("job_postings", "not_available", "no_active_ads_in_nav_feed")
        return

    published = 0
    deferred = 0
    for row in candidates:
        detail = detail_fetcher(row["detail_url"])
        builder.add_operations(requests=1)
        if detail is None:
            deferred += 1
            continue
        if detail.get("status") != "ACTIVE":
            continue  # gone inactive since the shipped index was built
        ad_content = detail.get("ad_content") or {}
        employer = ad_content.get("employer") or {}
        if employer.get("orgnr") != org:
            continue  # defensive: the index is already orgnr-filtered, but never trust it blindly
        expires = ad_content.get("expires")
        if _is_expired(expires):
            continue
        raw_bytes = json.dumps(detail, sort_keys=True, ensure_ascii=False).encode("utf-8")
        evidence_id = builder.add_evidence(
            source_url=f"{FEED_BASE}{row['detail_url']}", source_class="official_licensed_feed",
            access_policy=TERMS_URL, retrieved_at=utc_now(), http_status=200,
            content_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        )
        builder.add_claim(
            field="job_postings", subkey=row["ad_uuid"],
            value={
                "title": ad_content.get("title"),
                "published": ad_content.get("published"),
                "expires": expires,
                "link": ad_content.get("link") or ad_content.get("applicationUrl"),
                "municipal": row.get("municipal"),
            },
            availability="available", method="nav_feed_active_ad_verbatim_v1", evidence_ids=[evidence_id],
        )
        published += 1

    if published:
        builder.set_availability("job_postings", "available", "found")
    elif deferred:
        builder.set_availability("job_postings", "not_available", "detail_refetch_failed")
    else:
        builder.set_availability("job_postings", "not_available", "indexed_ads_no_longer_active")
