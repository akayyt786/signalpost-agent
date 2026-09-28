from __future__ import annotations

import re
import socket
import urllib.parse
from typing import Any, Callable

from .envelope import EnvelopeBuilder
from .evidence import utc_now
from .identity import assess_social_identity, classify_publication_verdict
from .sourcepacks import ReferencePack
from .website import fetch_website

COMPANY_OWNED = "company_owned"
VERDICT_RANK = {"verified": 3, "corroborated": 2, "conflict": 1, "ambiguous": 0}
NEWS_TERMS = ("news", "aktuelt", "nyheter", "press")

# Companies whose registered email lives on one of these domains do not carry a corporate-domain
# candidate: the domain is a shared consumer/webmail provider, not evidence of company ownership.
FREEMAIL_DOMAINS = {
    "gmail.com", "hotmail.com", "hotmail.no", "outlook.com", "online.no", "icloud.com",
    "yahoo.com", "yahoo.no", "live.no", "live.com", "msn.com", "me.com", "protonmail.com",
    "broadpark.no", "start.no", "getmail.no", "c2i.net",
}

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
PHONE_RE = re.compile(r"(?:\+47[\s.-]?)?\b\d{2}[\s.-]?\d{2}[\s.-]?\d{2}[\s.-]?\d{2}\b")
LEGAL_SUFFIX_RE = re.compile(r"\b(as|asa|ans|da|enk|sa|nuf|sti)\b")


def _email_domain_candidate(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    domain = email.rsplit("@", 1)[-1].strip().lower()
    if not domain or domain in FREEMAIL_DOMAINS:
        return None
    return f"https://{domain}/"


def _dns_resolves(hostname: str) -> bool:
    try:
        socket.getaddrinfo(hostname, 443)
        return True
    except OSError:
        return False


def _name_slug(name: str) -> str:
    text = str(name or "").casefold()
    text = text.translate(str.maketrans({"ø": "o", "å": "a", "æ": "ae"}))
    text = LEGAL_SUFFIX_RE.sub("", text)
    return re.sub(r"[^a-z0-9]+", "", text)


def build_candidates(
    identity_source: dict[str, Any],
    reference_pack: ReferencePack,
    org: str,
    *,
    nav_homepage: str | None = None,
    dns_resolver: Callable[[str], bool] = _dns_resolves,
) -> list[tuple[str, str]]:
    """[(url, tier_name), ...] in priority order, deduplicated by registered host.

    Ladder: registry website > corporate-domain email > DIBK/Wikidata/NAV cross-reference >
    DNS-verified name guess (only when nothing else produced a candidate).
    """
    candidates: list[tuple[str, str]] = []
    seen_hosts: set[str] = set()

    def add(raw: str | None, tier: str) -> None:
        if not raw:
            return
        normalized = raw if raw.startswith(("http://", "https://")) else f"https://{raw}"
        host = (urllib.parse.urlparse(normalized).hostname or "").lower()
        host = host[4:] if host.startswith("www.") else host
        if not host or host in seen_hosts:
            return
        seen_hosts.add(host)
        candidates.append((normalized, tier))

    add(identity_source.get("website"), "registry_website")
    add(_email_domain_candidate(identity_source.get("email")), "registry_email_domain")
    dibk_row = reference_pack.dibk.get(org)
    if dibk_row and dibk_row.get("www"):
        add(dibk_row["www"], "dibk_www")
    wikidata_row = reference_pack.wikidata.get(org)
    if wikidata_row and wikidata_row.get("websites"):
        add(wikidata_row["websites"][0], "wikidata_website")
    add(nav_homepage, "nav_job_ad_homepage")
    if not candidates:
        slug = _name_slug(identity_source.get("name"))
        if slug:
            guess_host = f"{slug}.no"
            if dns_resolver(guess_host):
                add(guess_host, "name_guess_dns_verified")
    return candidates


def _extract_contacts(website_value: dict[str, Any]) -> tuple[list[str], list[str]]:
    texts = [website_value.get("main_text_excerpt") or "", website_value.get("description") or ""]
    texts.extend(str(page.get("main_text_excerpt") or "") for page in website_value.get("pages", []))
    blob = " ".join(texts)
    emails = list(dict.fromkeys(match.group(0).lower() for match in EMAIL_RE.finditer(blob)))[:3]
    phones = list(dict.fromkeys(re.sub(r"[\s.-]", " ", match.group(0)).strip() for match in PHONE_RE.finditer(blob) if len(re.sub(r"\D", "", match.group(0))) == 8))[:3]
    return emails, phones


def populate_website(
    builder: EnvelopeBuilder,
    org: str,
    identity_source: dict[str, Any],
    reference_pack: ReferencePack,
    *,
    nav_homepage: str | None = None,
    fetcher: Callable[[str], tuple[dict[str, Any], dict[str, Any]]] = fetch_website,
    max_candidates: int = 2,
) -> None:
    """Populate official_website, site_description, social_profiles, contact_points and
    public_activity. Only a `verified` or `corroborated` verdict ever publishes a website claim;
    `conflict` and `ambiguous` always resolve to `not_available` on official_website and
    `not_applicable` on the four fields that only a verified site unlocks.
    """
    name = str(identity_source.get("name") or "")
    business_address = identity_source.get("business_address")
    phone = identity_source.get("phone")
    candidates = build_candidates(identity_source, reference_pack, org, nav_homepage=nav_homepage)

    downstream_families = ("site_description", "social_profiles", "contact_points", "public_activity")

    if not candidates:
        builder.set_availability("official_website", "not_available", "no_candidate_source")
        for family in downstream_families:
            builder.set_availability(family, "not_applicable", "no_verified_website")
        return

    best: tuple[int, dict[str, Any], dict[str, Any], str, str, dict[str, Any]] | None = None
    for url, tier in candidates[:max_candidates]:
        record, metrics = fetcher(url)
        builder.add_operations(requests=metrics["requests"], bytes_=metrics["bytes"])
        if record.get("status") != "available":
            continue
        website_value = record["value"]
        verdict = classify_publication_verdict(org, name, website_value, business_address=business_address, phone=phone)
        rank = VERDICT_RANK[verdict["verdict"]]
        if best is None or rank > best[0]:
            best = (rank, verdict, website_value, url, tier, record)
        if verdict["verdict"] == "verified":
            break

    if best is None:
        builder.set_availability("official_website", "not_available", "no_candidate_reachable")
        for family in downstream_families:
            builder.set_availability(family, "not_applicable", "no_verified_website")
        return

    _rank, verdict, website_value, url, tier, record = best
    retrieved_at = record.get("retrieved_at", utc_now())
    website_evidence_id = builder.add_evidence(
        source_url=url, final_url=website_value.get("final_url"), source_class=COMPANY_OWNED,
        access_policy="company_site_terms", retrieved_at=retrieved_at, http_status=200,
        content_sha256=website_value.get("content_sha256"), claim_span=verdict.get("proof_span"),
    )

    if verdict["verdict"] not in ("verified", "corroborated"):
        reason = "foreign_org_number_on_page" if verdict["verdict"] == "conflict" else verdict["reason"]
        builder.set_availability("official_website", "not_available", reason, checked_at=retrieved_at)
        for family in downstream_families:
            builder.set_availability(family, "not_applicable", "unlocked_only_by_verified_website")
        return

    confidence = 1.0 if verdict["verdict"] == "verified" else 0.9
    builder.add_claim(
        field="official_website", value=website_value.get("final_url"), availability="available",
        confidence=confidence, method=f"identity_gate_{verdict['verdict']}_v1:{tier}", evidence_ids=[website_evidence_id],
    )
    reason = "verified_by_organisation_number" if verdict["verdict"] == "verified" else "corroborated_by_name_and_address_or_phone"
    builder.set_availability("official_website", "available", reason, checked_at=retrieved_at)

    if verdict["verdict"] != "verified":
        # corroborated unlocks the website claim only; description/social/contact/activity all
        # require the stronger organisation-number proof.
        for family in downstream_families:
            builder.set_availability(family, "not_applicable", "unlocked_only_by_verified_website")
        return

    description = website_value.get("description")
    if description:
        builder.add_claim(field="site_description", value=description, availability="available", method="site_meta_description_v1", evidence_ids=[website_evidence_id])
        builder.set_availability("site_description", "available", "found", checked_at=retrieved_at)
    else:
        builder.set_availability("site_description", "not_available", "no_description_found", checked_at=retrieved_at)

    social_links = website_value.get("social_links") or []
    published_socials = 0
    for link in social_links:
        assessment = assess_social_identity({"name": name}, link)
        if assessment["publishable"]:
            builder.add_claim(field="social_profiles", subkey=link["platform"], value=link["url"], availability="available", method="site_outbound_link_identity_gated_v1", evidence_ids=[website_evidence_id])
            published_socials += 1
    builder.set_availability("social_profiles", "available" if published_socials else "not_available", "found" if published_socials else "no_publishable_social_links", checked_at=retrieved_at)

    emails, phones = _extract_contacts(website_value)
    for index, site_email in enumerate(emails):
        builder.add_claim(field="contact_points", subkey=f"email:{index}", value=site_email, availability="available", method="site_text_contact_extraction_v1", evidence_ids=[website_evidence_id])
    for index, site_phone in enumerate(phones):
        builder.add_claim(field="contact_points", subkey=f"phone:{index}", value=site_phone, availability="available", method="site_text_contact_extraction_v1", evidence_ids=[website_evidence_id])
    contacts_found = bool(emails or phones)
    builder.set_availability("contact_points", "available" if contacts_found else "not_available", "found" if contacts_found else "no_contact_details_found_on_site", checked_at=retrieved_at)

    news_pages = [page for page in website_value.get("pages", []) if any(term in page.get("url", "").lower() for term in NEWS_TERMS)]
    for page in news_pages:
        builder.add_claim(
            field="public_activity", subkey=page.get("url"),
            value={"title": page.get("title"), "url": page.get("url"), "excerpt": (page.get("main_text_excerpt") or "")[:300]},
            availability="available", method="site_news_page_v1", evidence_ids=[website_evidence_id],
        )
    builder.set_availability("public_activity", "available" if news_pages else "not_available", "found" if news_pages else "no_news_or_press_page_found", checked_at=retrieved_at)
