from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .evidence import utc_now
from .sampling import iter_bulk

UA = "builderr-signalpost/1.0 (+mailto:akaykatia9@gmail.com)"

BRREG_ENTITY_BULK_CSV = "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv"
BRREG_UPDATES = "https://data.brreg.no/enhetsregisteret/api/oppdateringer/enheter"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
DIBK_ENTERPRISES = "https://sgregister.dibk.no/api/enterprises"

# Wikidata property IDs used for the reference pack. All are standard, widely-used properties:
# P2333 Norwegian organisation number, P856 official website, P154 logo image, P2013 Facebook ID,
# P2003 Instagram username, P2397 YouTube channel ID, P4264 LinkedIn company page ID.
WIKIDATA_QUERY = """
SELECT ?orgnr ?companyLabel ?website ?logo ?facebook ?instagram ?youtube ?linkedin ?article WHERE {
  ?company wdt:P2333 ?orgnr .
  OPTIONAL { ?company wdt:P856 ?website . }
  OPTIONAL { ?company wdt:P154 ?logo . }
  OPTIONAL { ?company wdt:P2013 ?facebook . }
  OPTIONAL { ?company wdt:P2003 ?instagram . }
  OPTIONAL { ?company wdt:P2397 ?youtube . }
  OPTIONAL { ?company wdt:P4264 ?linkedin . }
  OPTIONAL { ?article schema:about ?company ; schema:isPartOf <https://en.wikipedia.org/> . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en,nb,no". }
}
"""


def cached_download(
    url: str, cache_dir: Path, filename: str, *, timeout: float = 300.0, accept: str | None = None, attempts: int = 3
) -> dict[str, Any]:
    """Download `url` to `cache_dir/filename`, reusing the cached copy when ETag/Last-Modified are unchanged.

    Verifies the downloaded byte count against the declared `Content-Length` (when present) before
    accepting the download; a truncated transfer is retried, not silently treated as complete. Falls
    back to a previously-downloaded copy only after every attempt fails and a cached copy exists.

    Returns {path, url, etag, last_modified, downloaded_at, sha256, bytes, from_cache}.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / filename
    meta_path = cache_dir / f"{filename}.meta.json"
    previous_meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() and target.exists() else None
    headers = {"User-Agent": UA}
    if accept:
        headers["Accept"] = accept
    if previous_meta:
        if previous_meta.get("etag"):
            headers["If-None-Match"] = previous_meta["etag"]
        if previous_meta.get("last_modified"):
            headers["If-Modified-Since"] = previous_meta["last_modified"]
    request = urllib.request.Request(url, headers=headers)
    tmp = target.with_suffix(target.suffix + ".part")
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                declared_length = response.headers.get("Content-Length")
                response_etag = response.headers.get("ETag")
                response_last_modified = response.headers.get("Last-Modified")
                written = 0
                with tmp.open("wb") as handle:
                    while True:
                        chunk = response.read(1 << 20)
                        if not chunk:
                            break
                        handle.write(chunk)
                        written += len(chunk)
                if declared_length is not None and written != int(declared_length):
                    raise OSError(f"truncated download: got {written} bytes, expected {declared_length}")
            tmp.replace(target)
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 304 and previous_meta:
                return {**previous_meta, "from_cache": True}
            last_error = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
        tmp.unlink(missing_ok=True)
    else:
        if previous_meta and target.exists():
            return {**previous_meta, "from_cache": True, "refresh_error": f"{type(last_error).__name__}: {last_error}"}
        raise RuntimeError(f"Failed to download {url} after {attempts} attempts: {last_error}") from last_error
    sha256 = hashlib.sha256()
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            sha256.update(chunk)
    meta = {
        "path": str(target),
        "url": url,
        "etag": response_etag,
        "last_modified": response_last_modified,
        "downloaded_at": utc_now(),
        "sha256": sha256.hexdigest(),
        "bytes": target.stat().st_size,
        "from_cache": False,
    }
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return meta


@dataclass
class EntityPack:
    """orgnr -> normalized bulk registry row (see sampling.normalize_row), plus the source snapshot metadata."""

    index: dict[str, dict[str, Any]] = field(default_factory=dict)
    snapshot: dict[str, Any] = field(default_factory=dict)
    scanned: int = 0

    def __contains__(self, org: str) -> bool:
        return org in self.index

    def get(self, org: str) -> dict[str, Any] | None:
        return self.index.get(org)


def build_entity_pack(wanted: Iterable[str], cache_dir: Path) -> EntityPack:
    wanted_set = set(wanted)
    snapshot = cached_download(BRREG_ENTITY_BULK_CSV, cache_dir, "enheter-bulk.csv.gz", timeout=900.0)
    index: dict[str, dict[str, Any]] = {}
    scanned = 0
    for record in iter_bulk(snapshot["path"]):
        scanned += 1
        org = record["organisation_number"]
        if org in wanted_set:
            index[org] = record
            if len(index) == len(wanted_set):
                break
    return EntityPack(index=index, snapshot=snapshot, scanned=scanned)


@dataclass
class UpdatePack:
    """Organisation numbers whose registry record changed after the entity-bulk snapshot was generated."""

    changed: dict[str, dict[str, Any]] = field(default_factory=dict)
    pages_fetched: int = 0
    source_url: str = ""


def _to_brreg_millis(value: str) -> str | None:
    """Format an ISO-8601 timestamp as yyyy-MM-dd'T'HH:mm:ss.SSS'Z', the only format the
    Brreg update feed accepts (verified live: other precisions return HTTP 400)."""
    from datetime import datetime, timezone

    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _http_last_modified_to_iso(value: str | None) -> str | None:
    if not value:
        return None
    from email.utils import parsedate_to_datetime

    try:
        return parsedate_to_datetime(value).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError):
        return None


def build_update_pack(snapshot_meta: dict[str, Any], *, max_pages: int = 20, page_size: int = 500) -> UpdatePack:
    raw_since = _http_last_modified_to_iso(snapshot_meta.get("last_modified")) or snapshot_meta.get("downloaded_at")
    since = _to_brreg_millis(raw_since) if raw_since else None
    if not since:
        return UpdatePack()
    params = urllib.parse.urlencode({"dato": since, "size": page_size})
    url = f"{BRREG_UPDATES}?{params}"
    changed: dict[str, dict[str, Any]] = {}
    pages = 0
    while url and pages < max_pages:
        request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": UA})
        try:
            with urllib.request.urlopen(request, timeout=30.0) as response:
                body = json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            break
        pages += 1
        for item in (body.get("_embedded") or {}).get("oppdaterteEnheter", []):
            org = item.get("organisasjonsnummer")
            if org:
                changed[org] = {"change_type": item.get("endringstype"), "changed_at": item.get("dato")}
        next_link = ((body.get("_links") or {}).get("next") or {}).get("href")
        url = next_link
    return UpdatePack(changed=changed, pages_fetched=pages, source_url=f"{BRREG_UPDATES}?{params}")


@dataclass
class ReferencePack:
    """orgnr -> independent cross-reference candidates (Wikidata, DIBK central-approval registry)."""

    wikidata: dict[str, dict[str, Any]] = field(default_factory=dict)
    dibk: dict[str, dict[str, Any]] = field(default_factory=dict)
    wikidata_meta: dict[str, Any] = field(default_factory=dict)
    dibk_meta: dict[str, Any] = field(default_factory=dict)


def _fetch_wikidata_rows() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    request = urllib.request.Request(
        f"{WIKIDATA_SPARQL}?{urllib.parse.urlencode({'query': WIKIDATA_QUERY, 'format': 'json'})}",
        headers={"Accept": "application/sparql-results+json", "User-Agent": UA},
    )
    with urllib.request.urlopen(request, timeout=120.0) as response:
        raw = response.read()
    body = json.loads(raw)
    return body.get("results", {}).get("bindings", []), {
        "url": WIKIDATA_SPARQL,
        "retrieved_at": utc_now(),
        "content_sha256": hashlib.sha256(raw).hexdigest(),
        "rows": len(body.get("results", {}).get("bindings", [])),
    }


def _wikidata_value(row: dict[str, Any], key: str) -> str | None:
    return (row.get(key) or {}).get("value")


def build_reference_pack(cache_dir: Path) -> ReferencePack:
    wikidata: dict[str, dict[str, Any]] = {}
    wikidata_meta: dict[str, Any] = {}
    try:
        rows, wikidata_meta = _fetch_wikidata_rows()
        for row in rows:
            org = _wikidata_value(row, "orgnr")
            if not org or len(org) != 9:
                continue
            entry = wikidata.setdefault(org, {
                "name": _wikidata_value(row, "companyLabel"),
                "websites": set(),
                "logo": _wikidata_value(row, "logo"),
                "wikipedia_article": _wikidata_value(row, "article"),
                "social": {},
            })
            website = _wikidata_value(row, "website")
            if website:
                entry["websites"].add(website)
            for platform, key in (("facebook", "facebook"), ("instagram", "instagram"), ("youtube", "youtube"), ("linkedin", "linkedin")):
                value = _wikidata_value(row, key)
                if value:
                    entry["social"][platform] = value
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        wikidata_meta = {"url": WIKIDATA_SPARQL, "error": "fetch_failed"}
    for entry in wikidata.values():
        entry["websites"] = sorted(entry["websites"])

    dibk: dict[str, dict[str, Any]] = {}
    dibk_meta: dict[str, Any] = {}
    try:
        meta = cached_download(f"{DIBK_ENTERPRISES}?Query=a", cache_dir, "dibk-enterprises.json", timeout=120.0, accept="application/json")
        dibk_meta = meta
        body = json.loads(Path(meta["path"]).read_text(encoding="utf-8"))
        for row in body.get("enterprises", []):
            org = row.get("organizational_number")
            if org:
                dibk[org] = row
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        dibk_meta = {"url": DIBK_ENTERPRISES, "error": "fetch_failed"}

    return ReferencePack(wikidata=wikidata, dibk=dibk, wikidata_meta=wikidata_meta, dibk_meta=dibk_meta)
