from __future__ import annotations

from ..envelope import EnvelopeBuilder
from ..evidence import utc_now
from ..sourcepacks import ReferencePack

DIBK_URL = "https://sgregister.dibk.no/api/enterprises"
WIKIDATA_QUERY_URL = "https://query.wikidata.org/sparql"


def populate_registries(builder: EnvelopeBuilder, org: str, reference_pack: ReferencePack) -> None:
    """credentials_and_approvals (DIBK central-approval register) and external_references
    (Wikidata Wikipedia article / logo / social handles). Neither issues a new network request:
    both draw on the ReferencePack already fetched once per run by sourcepacks.py. Wikidata's
    `website` property is deliberately excluded here - it is a site_resolver candidate input
    (step 5), not an independently verified fact, and publishing it again here would imply
    confirmation it never received.
    """
    _populate_credentials(builder, org, reference_pack)
    _populate_external_references(builder, org, reference_pack)


def _populate_credentials(builder: EnvelopeBuilder, org: str, reference_pack: ReferencePack) -> None:
    row = reference_pack.dibk.get(org)
    if not row:
        builder.set_availability("credentials_and_approvals", "not_available", "not_in_dibk_central_approval_register")
        return
    status = row.get("status") or {}
    evidence_id = builder.add_evidence(
        source_url=DIBK_URL, source_class="official_licensed_register", access_policy="public_dibk_dataset",
        retrieved_at=reference_pack.dibk_meta.get("downloaded_at", utc_now()),
        content_sha256=reference_pack.dibk_meta.get("sha256"),
        locator=f"row organizational_number={org}",
    )
    builder.add_claim(
        field="credentials_and_approvals", subkey="dibk_central_approval",
        value={
            "approved": bool(status.get("approved")),
            "approval_period_to": status.get("approval_period_to"),
            "certificate_url": status.get("approval_certificate"),
        },
        availability="available", method="dibk_central_approval_verbatim_v1", evidence_ids=[evidence_id],
    )
    builder.set_availability("credentials_and_approvals", "available", "found")


def _populate_external_references(builder: EnvelopeBuilder, org: str, reference_pack: ReferencePack) -> None:
    row = reference_pack.wikidata.get(org)
    if not row:
        builder.set_availability("external_references", "not_available", "not_in_wikidata")
        return
    evidence_id = builder.add_evidence(
        source_url=WIKIDATA_QUERY_URL, source_class="official_open_knowledge_base", access_policy="CC0",
        retrieved_at=reference_pack.wikidata_meta.get("retrieved_at", utc_now()),
        content_sha256=reference_pack.wikidata_meta.get("content_sha256"),
        locator=f"orgnr={org}",
    )
    published = 0
    if row.get("wikipedia_article"):
        builder.add_claim(field="external_references", subkey="wikipedia_article", value=row["wikipedia_article"], availability="available", method="wikidata_verbatim_v1", evidence_ids=[evidence_id])
        published += 1
    if row.get("logo"):
        builder.add_claim(field="external_references", subkey="logo", value=row["logo"], availability="available", method="wikidata_verbatim_v1", evidence_ids=[evidence_id])
        published += 1
    for platform, handle in sorted((row.get("social") or {}).items()):
        builder.add_claim(field="external_references", subkey=f"social:{platform}", value=handle, availability="available", method="wikidata_verbatim_v1", evidence_ids=[evidence_id])
        published += 1
    builder.set_availability(
        "external_references", "available" if published else "not_available",
        "found" if published else "wikidata_entry_has_no_usable_references",
    )
