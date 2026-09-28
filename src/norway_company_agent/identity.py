from __future__ import annotations

import re
import unicodedata
import urllib.parse
from typing import Any

from .input_batch import valid_check_digit


LEGAL_AND_GENERIC = {
    "as", "asa", "ans", "da", "enk", "iks", "sa", "sam", "sti", "stiftelsen",
    "nuf", "ab", "b", "v", "limited", "ltd", "inc", "plc", "the", "og", "and",
}


def _tokens(value: Any) -> list[str]:
    text = str(value or "").translate(str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    return [token for token in re.findall(r"[a-z0-9]+", text) if token not in LEGAL_AND_GENERIC and len(token) > 1]


def _structured_names(value: Any) -> list[str]:
    names: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"name", "legalName", "alternateName"} and isinstance(child, str):
                names.append(child)
            else:
                names.extend(_structured_names(child))
    elif isinstance(value, list):
        for child in value:
            names.extend(_structured_names(child))
    return names


def assess_website_identity(profile: dict[str, Any]) -> dict[str, Any]:
    website = profile.get("evidence", {}).get("website", {})
    value = website.get("value") or {}
    core = _tokens(profile.get("name"))
    hostname = urllib.parse.urlparse(value.get("final_url") or website.get("source_url") or "").hostname or ""
    structured_names = _structured_names(value.get("structured_organisations") or [])
    rendered = value.get("js_fallback") or {}
    homepage_identity_parts = [
        value.get("title"), value.get("description"), value.get("identity_text_excerpt"), hostname, *structured_names,
        rendered.get("title"),
    ]
    candidate_parts = [
        *homepage_identity_parts, value.get("main_text_excerpt"),
        *[page.get("title") for page in value.get("pages", [])],
        *[page.get("main_text_excerpt") for page in value.get("pages", [])],
        *[page.get("identity_text_excerpt") for page in value.get("pages", [])],
    ]
    candidate_parts.append(rendered.get("main_text_excerpt"))
    candidate_text = " ".join(str(part or "") for part in candidate_parts)
    homepage_candidate_text = " ".join(str(part or "") for part in [*homepage_identity_parts, value.get("main_text_excerpt"), rendered.get("main_text_excerpt")])
    normalized_candidate_text = " ".join(_tokens(candidate_text))
    candidate_tokens = set(_tokens(candidate_text))
    org_digits = re.sub(r"\D", "", str(profile.get("organisation_number") or ""))
    compact_candidate = re.sub(r"\D", "", candidate_text)
    compact_homepage_candidate = re.sub(r"\D", "", homepage_candidate_text)
    overlap = sorted(set(core) & candidate_tokens)
    ratio = len(overlap) / len(set(core)) if core else 0.0
    reasons = []
    parked_markers = (
        "domain is for sale", "domain for sale", "hugedomains", "parked at", "miss hosting",
        "her flytter snart en ny gjest", "has been informing visitors",
        "find the best information and most relevant links on all topics related to",
    )
    normalized_raw = unicodedata.normalize("NFKD", candidate_text).encode("ascii", "ignore").decode().casefold()
    homepage_token_sets = [set(_tokens(part)) for part in homepage_identity_parts if part]
    exact_homepage_name = bool(core and any(set(core).issubset(tokens) for tokens in homepage_token_sets))
    substantive_homepage = len(str(value.get("main_text_excerpt") or "").strip()) >= 100
    is_business_sports_club = bool(re.search(r"(?:^|\s)B\.?\s*I\.?\s*L\.?(?:\s|$)", str(profile.get("name") or ""), re.I))
    if any(marker in normalized_raw for marker in parked_markers):
        score = 0.1
        reasons.append("captured page is a parked, for-sale, or generic hosting placeholder")
    elif is_business_sports_club and "bedriftsidrett" not in normalized_candidate_text and "b i l" not in normalized_candidate_text:
        score = 0.3
        reasons.append("business sports-club entity points to the operating company's site without club evidence")
    elif org_digits and org_digits in compact_homepage_candidate:
        score = 1.0
        reasons.append("exact organisation number appears in homepage identity evidence")
    elif len(core) >= 2 and exact_homepage_name:
        score = 0.95
        reasons.append("all normalized legal-name tokens appear together in homepage identity evidence")
    elif len(core) == 1 and exact_homepage_name and substantive_homepage:
        score = 0.95
        reasons.append("single distinctive legal-name token appears in homepage identity evidence with substantive content")
    elif ratio >= 0.75 and len(overlap) >= 2:
        score = 0.85
        reasons.append("most legal-name tokens appear, but exact identity is incomplete")
    elif ratio >= 0.5 and len(overlap) >= 2:
        score = 0.65
        reasons.append("partial legal-name overlap only")
    else:
        score = 0.3
        reasons.append("registry-linked URL lacks strong exact-entity identity evidence")
    status = "exact" if score >= 0.9 else "review" if score >= 0.8 else "related_or_uncertain"
    return {
        "status": status,
        "score": score,
        "publishable": status == "exact",
        "legal_name_tokens": core,
        "matched_tokens": overlap,
        "reasons": reasons,
        "method": "deterministic_name_org_evidence_v2",
    }


def _digit_windows(text: str, *, min_run: int = 9) -> set[str]:
    """Every valid-length organisation-number window inside each naturally-occurring digit run.

    Operates on maximal digit runs from the *original* text (space/dot-separated groups like
    "923 609 016" or "NO 923 609 016 MVA" collapse into one run once separators are stripped
    locally around digits), not on the whole page compacted into one blob, so an org number in
    one part of the page and an unrelated phone number elsewhere never combine into a false window.
    """
    runs = re.findall(r"(?:\d[ .\-]?){%d,}" % min_run, text)
    windows: set[str] = set()
    for run in runs:
        digits = re.sub(r"\D", "", run)
        for start in range(len(digits) - 8):
            windows.add(digits[start:start + 9])
    return windows


def _span_around_digit_match(text: str, target_digits: str, *, window: int = 150) -> str:
    """A human-readable excerpt centered on where `target_digits` actually occurs in `text`
    (ignoring interleaved separators like spaces), not the first `window*2` characters of `text`.

    Long pages routinely put hundreds of characters of navigation/menu boilerplate before the
    actual organisation-number line; slicing from the start produced junk "evidence" that never
    showed the match it was supposed to prove, even though the underlying verdict was correct.
    """
    digit_positions = [i for i, ch in enumerate(text) if ch.isdigit()]
    compact = "".join(text[i] for i in digit_positions)
    match_at = compact.find(target_digits)
    if match_at == -1 or not digit_positions:
        return text[:window * 2]
    match_start = digit_positions[match_at]
    match_end = digit_positions[match_at + len(target_digits) - 1] + 1
    start = max(0, match_start - window)
    end = min(len(text), match_end + window)
    return text[start:end]


def assess_social_identity(profile: dict[str, Any], link: dict[str, str]) -> dict[str, Any]:
    core = _tokens(profile.get("name"))
    parsed = urllib.parse.urlparse(link.get("url") or "")
    handle_text = urllib.parse.unquote(parsed.path)
    handle_compact = "".join(_tokens(handle_text))
    matched = [token for token in core if token in handle_compact]
    core_compact = "".join(core)
    ratio = len(set(matched)) / len(set(core)) if core else 0.0
    if core_compact and core_compact in handle_compact:
        score = 0.98
        reason = "normalized legal-name sequence appears in the social handle"
    elif len(core) == 1 and matched:
        score = 0.95
        reason = "single distinctive legal-name token appears in the social handle"
    elif ratio >= 0.75 and len(set(matched)) >= 2:
        score = 0.9
        reason = "most legal-name tokens appear in the social handle"
    else:
        score = 0.3
        reason = "social handle lacks strong exact-entity name evidence"
    return {
        **link,
        "identity_score": score,
        "publishable": score >= 0.9,
        "matched_tokens": matched,
        "reason": reason,
        "method": "deterministic_social_handle_identity_v1",
    }


def apply_website_identity_gate(profile: dict[str, Any], website: dict[str, Any]) -> dict[str, Any]:
    if website.get("status") != "available":
        return {"website": website, "assessment": None, "quarantined_social_links": 0}
    temporary_profile = {**profile, "evidence": {**profile.get("evidence", {}), "website": website}}
    value = website.get("value") or {}
    assessment = assess_website_identity(temporary_profile)
    value["identity_assessment"] = assessment
    original = list(value.get("discovered_social_links") or value.get("social_links") or [])
    value["discovered_social_links"] = original
    social_assessments = [assess_social_identity(profile, link) for link in original]
    value["social_link_assessments"] = social_assessments
    value["social_links"] = [
        {"platform": item["platform"], "url": item["url"]}
        for item in social_assessments
        if assessment["publishable"] and item["publishable"]
    ]
    website["value"] = value
    return {
        "website": website,
        "assessment": assessment,
        "quarantined_social_links": len(original) - len(value["social_links"]),
    }


PARKED_MARKERS = (
    "domain is for sale", "domain for sale", "hugedomains", "parked at", "miss hosting",
    "her flytter snart en ny gjest", "has been informing visitors",
    "find the best information and most relevant links on all topics related to",
)


def classify_publication_verdict(
    organisation_number: str,
    name: str,
    website_value: dict[str, Any],
    *,
    business_address: dict[str, Any] | None = None,
    phone: str | None = None,
) -> dict[str, Any]:
    """The step-5 publication gate: verified / corroborated / conflict / ambiguous.

    `verified` is the only verdict that unlocks site_description/social_profiles/contact_points/
    public_activity. `corroborated` unlocks only the official_website claim itself. `conflict` and
    `ambiguous` publish nothing (availability: not_available).
    """
    core = _tokens(name)
    hostname = urllib.parse.urlparse(website_value.get("final_url") or "").hostname or ""
    structured_names = _structured_names(website_value.get("structured_organisations") or [])
    homepage_identity_parts = [website_value.get("title"), website_value.get("description"), hostname, *structured_names]
    page_texts = [website_value.get("main_text_excerpt") or ""]
    page_texts.extend(str(page.get("main_text_excerpt") or "") for page in website_value.get("pages", []))
    page_titles = [str(page.get("title") or "") for page in website_value.get("pages", [])]
    full_text = " ".join(str(part or "") for part in [*homepage_identity_parts, *page_texts, *page_titles])
    normalized_full_text = unicodedata.normalize("NFKD", full_text).encode("ascii", "ignore").decode().casefold()

    if any(marker in normalized_full_text for marker in PARKED_MARKERS):
        return {"verdict": "ambiguous", "reason": "parked_or_for_sale_page", "proof_span": None, "other_org_numbers": []}

    org_digits = re.sub(r"\D", "", str(organisation_number or ""))
    found_org_numbers = _digit_windows(full_text)
    valid_found = {candidate for candidate in found_org_numbers if valid_check_digit(candidate)}

    if org_digits in valid_found:
        span_source = next((text for text in [*homepage_identity_parts, *page_texts] if org_digits in re.sub(r"\D", "", str(text or ""))), "")
        proof_span = _span_around_digit_match(str(span_source), org_digits) if span_source else None
        return {"verdict": "verified", "reason": "organisation_number_found_on_page", "proof_span": proof_span, "other_org_numbers": sorted(valid_found - {org_digits})}

    other_valid = sorted(valid_found - {org_digits})
    if other_valid:
        return {"verdict": "conflict", "reason": "different_valid_organisation_number_on_page", "proof_span": None, "other_org_numbers": other_valid}

    homepage_token_sets = [set(_tokens(part)) for part in homepage_identity_parts if part]
    exact_name_match = bool(core and any(set(core).issubset(tokens) for tokens in homepage_token_sets))
    if len(core) <= 1:
        # A single-token legal name (e.g. "NORDIC AS") never reaches corroborated: too easy to
        # collide with an unrelated company of the same short name. Organisation number required.
        return {"verdict": "ambiguous", "reason": "single_token_name_requires_organisation_number", "proof_span": None, "other_org_numbers": []}

    address_line = str((business_address or {}).get("address") or "")
    address_match = bool(address_line) and address_line.casefold() in normalized_full_text
    phone_digits = re.sub(r"\D", "", str(phone or ""))
    phone_match = bool(phone_digits) and len(phone_digits) >= 8 and phone_digits in re.sub(r"\D", "", full_text)
    if exact_name_match and (address_match or phone_match):
        return {
            "verdict": "corroborated",
            "reason": "exact_name_plus_address_or_phone_match",
            "proof_span": None,
            "other_org_numbers": [],
        }

    return {"verdict": "ambiguous", "reason": "insufficient_exact_entity_evidence", "proof_span": None, "other_org_numbers": []}
