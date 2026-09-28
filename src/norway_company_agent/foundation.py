from __future__ import annotations

from typing import Any, Callable

from .envelope import EnvelopeBuilder
from .evidence import utc_now
from .http import FetchResult, fetch_json
from .official import BRREG_ENTITY, fetch_official_modules, normalize_entity
from .sourcepacks import BRREG_ENTITY_BULK_CSV, EntityPack, UpdatePack

NLOD = "NLOD-2.0"


def _map_official_state(status: str, *, not_found_state: str = "not_available", not_found_reason: str = "no_record_returned") -> tuple[str, str]:
    """Map official.py's evidence status vocabulary onto the envelope availability vocabulary."""
    if status == "available":
        return "available", "found"
    if status == "not_found":
        return not_found_state, not_found_reason
    if status == "not_applicable":
        return "not_applicable", "not_applicable"
    if status == "blocked":
        return "blocked", "blocked_by_source"
    return "failed", "source_error"


def populate_foundation(
    builder: EnvelopeBuilder,
    org: str,
    entity_pack: EntityPack,
    update_pack: UpdatePack,
    *,
    fetcher: Callable[[str], FetchResult] = fetch_json,
) -> dict[str, Any]:
    """Populate legal_identity, registered_address, industry, employees, status_flags, annual_accounts,
    roles, group_structure and locations. Returns {"identity_ok": bool, "entity": dict | None} so the
    caller can decide whether to proceed to the website/external steps.
    """
    row = entity_pack.get(org)
    identity_source: dict[str, Any]
    live_refresh_needed = org in update_pack.changed or row is None

    if row is not None:
        identity_source = {
            "name": row["name"],
            "legal_form": row["legal_form"],
            "employees": row["employees"],
            "bankrupt": row["bankrupt"],
            "liquidating": row["liquidating"],
            "municipality": row["municipality"],
            "industry_code": row["industry_code"],
            "industry_label": row["industry_label"],
            "latest_submitted_accounts": row["latest_submitted_accounts"],
            "erIKonsern": None,  # bulk CSV does not carry this flag; refined below if we do a live check
        }
        raw = row.get("raw", {})
        identity_source["business_address"] = {
            "address": raw.get("forretningsadresse.adresse"),
            "postal_code": raw.get("forretningsadresse.postnummer"),
            "city": raw.get("forretningsadresse.poststed"),
            "municipality": raw.get("forretningsadresse.kommune"),
            "country": raw.get("forretningsadresse.land"),
        }
        identity_source["postal_address"] = {
            "address": raw.get("postadresse.adresse"),
            "postal_code": raw.get("postadresse.postnummer"),
            "city": raw.get("postadresse.poststed"),
            "municipality": raw.get("postadresse.kommune"),
            "country": raw.get("postadresse.land"),
        }
        identity_source["email"] = raw.get("epostadresse")
        identity_source["phone"] = raw.get("telefon")
        identity_source["website"] = row.get("website")
        registry_evidence_id = builder.add_evidence(
            source_url=BRREG_ENTITY_BULK_CSV,
            source_class="official_registry_bulk",
            access_policy=NLOD,
            retrieved_at=entity_pack.snapshot.get("downloaded_at", utc_now()),
            http_status=200,
            content_sha256=entity_pack.snapshot.get("sha256"),
            locator=f"row organisasjonsnummer={org} in {entity_pack.snapshot.get('path')}",
        )
    else:
        registry_evidence_id = None

    if live_refresh_needed:
        result = fetcher(BRREG_ENTITY.format(org=org))
        if result.status == 200:
            live = normalize_entity(result.body)
            if live.get("organisation_number") != org:
                builder.add_error(stage="identity", message=f"live registry echoed {live.get('organisation_number')!r} for requested {org!r}", field="legal_identity")
                for field in ("legal_identity", "registered_address", "industry", "employees", "status_flags", "annual_accounts", "roles", "group_structure", "locations"):
                    builder.set_availability(field, "failed", "identity_mismatch")
                return {"identity_ok": False, "entity": None}
            registry_evidence_id = builder.add_evidence(
                source_url=result.url,
                source_class="official_registry_live",
                access_policy=NLOD,
                retrieved_at=result.retrieved_at,
                http_status=result.status,
                content_sha256=result.content_sha256,
            )
            identity_source = {
                "name": live["name"],
                "legal_form": live["legal_form"],
                "employees": live["employees"],
                "bankrupt": live["bankrupt"],
                "liquidating": live["liquidating"],
                "municipality": (live.get("business_address") or {}).get("kommune"),
                "industry_code": ((live.get("industry") or {}).get("kode")),
                "industry_label": ((live.get("industry") or {}).get("beskrivelse")),
                "latest_submitted_accounts": live["latest_submitted_accounts"],
                "erIKonsern": result.body.get("erIKonsern") if isinstance(result.body, dict) else None,
                "business_address": live.get("business_address") or {},
                "postal_address": live.get("postal_address") or {},
                "email": (result.body or {}).get("epostadresse") if isinstance(result.body, dict) else None,
                "phone": (result.body or {}).get("telefon") if isinstance(result.body, dict) else None,
                "website": live.get("website"),
            }
        elif result.status in (404, 410):
            if row is None:
                builder.add_error(stage="identity", message="organisation number not found in bulk snapshot or live registry", field="legal_identity")
                for field in ("legal_identity", "registered_address", "industry", "employees", "status_flags", "annual_accounts", "roles", "group_structure", "locations"):
                    builder.set_availability(field, "not_available", "not_in_register")
                return {"identity_ok": False, "entity": None}
            # Present in yesterday's bulk snapshot but 404 live: likely deregistered since. Keep the
            # bulk identity as the anchor but flag the discrepancy via status_flags below.
            identity_source["deregistered_since_snapshot"] = True
        else:
            if row is None:
                builder.add_error(stage="identity", message=f"live registry lookup failed: HTTP {result.status} {result.error}", field="legal_identity")
                for field in ("legal_identity", "registered_address", "industry", "employees", "status_flags", "annual_accounts", "roles", "group_structure", "locations"):
                    builder.set_availability(field, "failed", "registry_fetch_failed")
                return {"identity_ok": False, "entity": None}
            # Bulk identity still usable; the live re-check itself failed transiently.
            builder.add_error(stage="identity_refresh", message=f"live registry re-check failed: HTTP {result.status} {result.error}", field="legal_identity")

    now = utc_now()
    builder.add_claim(field="legal_identity", value={"name": identity_source["name"], "legal_form": identity_source["legal_form"]}, availability="available", method="registry_verbatim_v1", evidence_ids=[registry_evidence_id], seen_at=now)
    builder.set_availability("legal_identity", "available", "found", checked_at=now)

    builder.add_claim(field="registered_address", subkey="business_address", value=identity_source["business_address"], availability="available", method="registry_verbatim_v1", evidence_ids=[registry_evidence_id], seen_at=now)
    builder.add_claim(field="registered_address", subkey="postal_address", value=identity_source["postal_address"], availability="available", method="registry_verbatim_v1", evidence_ids=[registry_evidence_id], seen_at=now)
    builder.set_availability("registered_address", "available", "found", checked_at=now)

    builder.add_claim(field="industry", value={"code": identity_source["industry_code"], "label": identity_source["industry_label"]}, availability="available", method="registry_verbatim_v1", evidence_ids=[registry_evidence_id], seen_at=now)
    builder.set_availability("industry", "available", "found", checked_at=now)

    employees_available = identity_source["employees"] is not None
    builder.add_claim(field="employees", value=identity_source["employees"], availability="available" if employees_available else "not_available", method="registry_verbatim_v1", evidence_ids=[registry_evidence_id], seen_at=now)
    builder.set_availability("employees", "available" if employees_available else "not_available", "found" if employees_available else "registry_does_not_report_employee_count", checked_at=now)

    builder.add_claim(
        field="status_flags",
        value={
            "bankrupt": identity_source["bankrupt"],
            "liquidating": identity_source["liquidating"],
            "latest_submitted_accounts": identity_source["latest_submitted_accounts"],
            "deregistered_since_snapshot": identity_source.get("deregistered_since_snapshot", False),
        },
        availability="available", method="registry_verbatim_v1", evidence_ids=[registry_evidence_id], seen_at=now,
    )
    builder.set_availability("status_flags", "available", "found", checked_at=now)

    records, metrics = fetch_official_modules(org, {"financials", "roles", "group", "locations"}, fetcher=fetcher)
    builder.add_operations(requests=len(metrics), bytes_=sum(m.bytes_received for m in metrics))

    financials = records.get("financials", {})
    fin_state, fin_reason = _map_official_state(financials.get("status", "source_error"), not_found_reason="no_filed_accounts_returned")
    if financials.get("status") == "available":
        fin_evidence_id = builder.add_evidence(
            source_url=financials["source_url"], source_class="official_annual_accounts", access_policy=NLOD,
            retrieved_at=financials.get("retrieved_at", now), http_status=200, content_sha256=financials.get("content_sha256"),
        )
        fin_records = (financials.get("value") or {}).get("records", [])
        for record in fin_records:
            period = record.get("period") or {}
            reporting_period = {"from": period.get("fraDato"), "to": period.get("tilDato")} if period else None
            for subfield in ("revenue", "operating_result", "profit_before_tax", "annual_result", "assets", "equity", "debt"):
                value = record.get(subfield)
                builder.add_claim(
                    field="annual_accounts", subkey=f"{record.get('record_id')}:{subfield}", value=value,
                    unit=record.get("currency"), reporting_period=reporting_period,
                    availability="available" if value is not None else "not_available",
                    method="regnskapsregisteret_verbatim_v1", evidence_ids=[fin_evidence_id], seen_at=now,
                )
        builder.set_availability("annual_accounts", "available" if fin_records else "not_available", "found" if fin_records else "empty_filing_returned", checked_at=now)
    else:
        builder.set_availability("annual_accounts", fin_state, fin_reason, checked_at=now)

    roles = records.get("roles", {})
    if roles.get("status") == "available":
        roles_evidence_id = builder.add_evidence(
            source_url=roles["source_url"], source_class="official_roles", access_policy=NLOD,
            retrieved_at=roles.get("retrieved_at", now), http_status=200, content_sha256=roles.get("content_sha256"),
        )
        active_roles = [role for role in (roles.get("value") or {}).get("roles", []) if not role.get("inactive")]
        for index, role in enumerate(active_roles):
            builder.add_claim(
                field="roles", subkey=f"{role.get('role_code') or 'role'}:{index}",
                value={"name": role.get("name"), "role": role.get("role"), "role_code": role.get("role_code")},
                availability="available", method="registry_roles_verbatim_v1", evidence_ids=[roles_evidence_id], seen_at=now,
            )
        builder.set_availability("roles", "available" if active_roles else "not_available", "found" if active_roles else "no_active_roles_returned", checked_at=now)
    else:
        state, reason = _map_official_state(roles.get("status", "source_error"))
        builder.set_availability("roles", state, reason, checked_at=now)

    group = records.get("group", {})
    group_state, group_reason = _map_official_state(group.get("status", "source_error"), not_found_state="not_applicable", not_found_reason="not_in_a_group")
    if group.get("status") == "available":
        group_evidence_id = builder.add_evidence(
            source_url=group["source_url"], source_class="official_group_structure", access_policy=NLOD,
            retrieved_at=group.get("retrieved_at", now), http_status=200, content_sha256=group.get("content_sha256"),
        )
        children = (group.get("value") or {}).get("children", []) if isinstance(group.get("value"), dict) else []
        for child in children:
            child_org = child.get("organisasjonsnummer")
            builder.add_claim(
                field="group_structure", subkey=child_org,
                value={"name": child.get("navn"), "relationship": (child.get("knytningsform") or {}).get("beskrivelse"), "basis": child.get("grunnlag"), "since": child.get("dato")},
                availability="available", method="konsernstruktur_verbatim_v1", evidence_ids=[group_evidence_id], seen_at=now,
            )
        builder.set_availability("group_structure", "available" if children else "not_applicable", "found" if children else "group_endpoint_returned_no_relationships", checked_at=now)
    else:
        builder.set_availability("group_structure", group_state, group_reason, checked_at=now)

    locations = records.get("locations", {})
    if locations.get("status") == "available":
        loc_evidence_id = builder.add_evidence(
            source_url=locations["source_url"], source_class="official_subunits", access_policy=NLOD,
            retrieved_at=locations.get("retrieved_at", now), http_status=200, content_sha256=locations.get("content_sha256"),
        )
        subunits = (locations.get("value") or {}).get("locations", [])
        for subunit in subunits:
            builder.add_claim(
                field="locations", subkey=subunit.get("organisation_number"),
                value={"name": subunit.get("name"), "address": subunit.get("address"), "industry": subunit.get("industry"), "employees": subunit.get("employees")},
                availability="available", method="registry_subunits_verbatim_v1", evidence_ids=[loc_evidence_id], seen_at=now,
            )
        builder.set_availability("locations", "available" if subunits else "not_available", "found" if subunits else "no_registered_subunits", checked_at=now)
    else:
        state, reason = _map_official_state(locations.get("status", "source_error"))
        builder.set_availability("locations", state, reason, checked_at=now)

    return {"identity_ok": True, "entity": identity_source}
