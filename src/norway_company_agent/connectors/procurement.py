from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from ..envelope import EnvelopeBuilder
from ..evidence import utc_now

TED_SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"
TED_LEGAL_NOTICE = "https://ted.europa.eu/en/legal-notice"
FIELDS = (
    "publication-number", "buyer-name", "winner-identifier", "organisation-identifier-buyer",
    "notice-type", "publication-date", "notice-title",
)


def _pick_language(value: Any) -> str | None:
    """TED's text fields are keyed by 3-letter language code, e.g. {"eng": [...], "nob": [...]}.
    Prefer English, then Norwegian Bokmål, then whatever is first - never guess a value, just pick
    a deterministic display language for an otherwise-identical multilingual field."""
    if not isinstance(value, dict) or not value:
        return None
    for lang in ("eng", "nob", "nno"):
        entry = value.get(lang)
        if entry:
            return entry[0] if isinstance(entry, list) else str(entry)
    first = next(iter(value.values()), None)
    if isinstance(first, list):
        return first[0] if first else None
    return first


def _post_json(url: str, body: dict[str, Any], *, timeout: float = 25.0, attempts: int = 3) -> tuple[dict[str, Any] | None, int, int, str | None]:
    """Returns (parsed_body, requests_spent, bytes_received, error). TED requires POST with a JSON
    body; http.py's fetch_json is GET-only, so this mirrors its retry/error conventions locally
    rather than complicating a shared helper other connectors don't need."""
    payload = json.dumps(body).encode("utf-8")
    last_error = "request failed"
    for attempt in range(attempts):
        request = urllib.request.Request(
            url, data=payload, method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "builderr-signalpost-poc/0.1 (+https://builderr.ai)"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                return json.loads(raw), 1, len(raw), None
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            if exc.code == 400:
                return None, 1, len(raw), f"HTTP 400: {raw[:200]!r}"
            last_error = f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = type(exc).__name__
        if attempt + 1 < attempts:
            time.sleep(0.4 * (2**attempt))
    return None, attempts, 0, last_error


def populate_procurement(builder: EnvelopeBuilder, org: str, entity: dict[str, Any], reference_pack: Any, *, poster: Any = None) -> None:
    """public_contracts: Norwegian public-procurement contract awards this organisation won, from
    the EU's TED (Tenders Electronic Daily) Search API v3, filtered to notices where this exact
    organisation number appears in the structured `winner-identifier` field.

    Deliberately does not publish a winner *name* from TED: on framework-agreement notices with
    multiple winners, `winner-name` and `winner-identifier` are parallel arrays that are not
    guaranteed to be the same length (observed live: 10 identifiers, 4 names on a real notice), so
    there is no safe way to know which name corresponds to which identifier. The organisation
    number match alone (verbatim, exact) is the only claim precision allows; the company's own name
    already comes from Brønnøysund.

    Coverage caveat, stated honestly rather than implied: TED only receives notices above the EU
    procurement value thresholds that Norwegian buyers are required to forward to it. Doffin (the
    Norwegian national portal) publishes additional below-threshold awards TED never sees, but its
    only live JSON API is an undocumented internal endpoint of doffin.no's own single-page app
    (reachable with zero auth, but never publicly documented for third-party use, unlike this
    endpoint) - not used here on that basis. See CRAWLERS.md.
    """
    body = {
        "query": f"winner-identifier={org}",
        "fields": list(FIELDS),
        "limit": 25,
        "scope": "ALL",
        "onlyLatestVersions": True,
    }
    poster = poster or _post_json
    parsed, requests, bytes_received, error = poster(TED_SEARCH_URL, body)
    builder.add_operations(requests=requests, bytes_=bytes_received)
    if error is not None:
        builder.set_availability("public_contracts", "failed", "ted_search_api_error", checked_at=utc_now())
        return

    notices = (parsed or {}).get("notices") or []
    if not notices:
        builder.set_availability("public_contracts", "not_available", "no_won_notices_in_ted", checked_at=utc_now())
        return

    raw_bytes = json.dumps(parsed, sort_keys=True, ensure_ascii=False).encode("utf-8")
    evidence_id = builder.add_evidence(
        source_url=f"{TED_SEARCH_URL}?query=winner-identifier={org}",
        source_class="official_eu_procurement_notice_service", access_policy=TED_LEGAL_NOTICE,
        retrieved_at=utc_now(), http_status=200, content_sha256=hashlib.sha256(raw_bytes).hexdigest(),
    )
    published = 0
    for notice in notices:
        publication_number = notice.get("publication-number")
        if not publication_number:
            continue
        buyer_orgnr = (notice.get("organisation-identifier-buyer") or [None])[0]
        html_links = ((notice.get("links") or {}).get("html") or {})
        link = html_links.get("ENG") or next(iter(html_links.values()), None)
        builder.add_claim(
            field="public_contracts", subkey=publication_number,
            value={
                "publication_number": publication_number,
                "buyer_name": _pick_language(notice.get("buyer-name")),
                "buyer_organisation_number": buyer_orgnr,
                "notice_title": _pick_language(notice.get("notice-title")),
                "notice_type": notice.get("notice-type"),
                "publication_date": notice.get("publication-date"),
                "link": link,
            },
            availability="available", method="ted_search_api_v3_verbatim_v1", evidence_ids=[evidence_id],
        )
        published += 1
    builder.set_availability("public_contracts", "available" if published else "not_available", "found" if published else "no_won_notices_in_ted", checked_at=utc_now())
