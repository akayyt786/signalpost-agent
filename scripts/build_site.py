#!/usr/bin/env python3
"""Render the Signalpost verification site from a run's envelopes.jsonl - no build step, no
framework, native HTML/CSS/JS. Works offline from file:// and identically over GitHub Pages.

    uv run python scripts/build_site.py --envelopes out/envelopes.jsonl --report out/run-report.json --out out/site
"""
from __future__ import annotations

import argparse
import html
import json
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.envelope import FIELD_FAMILIES  # noqa: E402

EXTERNAL_FAMILIES = {"job_postings", "credentials_and_approvals", "external_references", "social_profiles"}
MAIN_FAMILIES = [family for family in FIELD_FAMILIES if family not in EXTERNAL_FAMILIES]
EXTERNAL_ORDER = ["job_postings", "credentials_and_approvals", "social_profiles", "external_references"]

FAMILY_LABELS = {
    "legal_identity": "LEGAL IDENTITY", "registered_address": "REGISTERED ADDRESS", "industry": "INDUSTRY",
    "employees": "EMPLOYEES", "status_flags": "STATUS", "annual_accounts": "ANNUAL ACCOUNTS",
    "roles": "ROLES", "group_structure": "GROUP STRUCTURE", "locations": "LOCATIONS",
    "official_website": "OFFICIAL WEBSITE", "site_description": "SITE DESCRIPTION",
    "social_profiles": "SOCIAL PROFILES", "contact_points": "CONTACT POINTS",
    "job_postings": "JOB POSTINGS", "public_activity": "PUBLIC ACTIVITY",
    "credentials_and_approvals": "CREDENTIALS / APPROVALS", "external_references": "EXTERNAL REFERENCES",
}


def family_label(field: str) -> str:
    """A hand-tuned label for the original families (e.g. the "/" in CREDENTIALS / APPROVALS),
    auto-derived for any connector added since (snake_case -> UPPER CASE WITH SPACES) so a new
    field family never renders as a raw lowercase Python identifier on the site."""
    return FAMILY_LABELS.get(field, field.replace("_", " ").upper())


def e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def load_envelopes(path: Path) -> list[dict[str, Any]]:
    envelopes = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            envelopes.append(json.loads(line))
    return envelopes


def _claim_display_value(claim: dict[str, Any]) -> str:
    value = claim.get("value")
    if isinstance(value, dict):
        return "; ".join(f"{k}: {v}" for k, v in value.items() if v not in (None, ""))
    if isinstance(value, (int, float)) and claim.get("unit"):
        return f"{value:,.0f} {claim['unit']}".replace(",", " ")
    return str(value) if value is not None else ""


def _evidence_by_id(envelope: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in envelope.get("evidence", [])}


def render_claim_row(claim: dict[str, Any], evidence_by_id: dict[str, dict[str, Any]]) -> str:
    availability = claim["availability"]
    field_label = family_label(claim["field"])
    subkey_suffix = f" · {e(claim['subkey'])}" if claim.get("subkey") else ""
    value_text = e(_claim_display_value(claim))
    period = claim.get("reporting_period")
    period_text = f"{e(period.get('from'))} → {e(period.get('to'))}" if period else ""
    evidence_ids = claim.get("evidence_ids") or []
    source_link = ""
    retrieved_at = ""
    proof_span = ""
    if evidence_ids and evidence_ids[0] in evidence_by_id:
        ev = evidence_by_id[evidence_ids[0]]
        url = ev.get("final_url") or ev.get("source_url") or ""
        if url:
            source_link = f'<a href="{e(url)}" target="_blank" rel="noopener">[SOURCE →]</a>'
        retrieved_at = e(ev.get("retrieved_at") or "")
        if ev.get("claim_span") and claim["field"] == "official_website":
            proof_span = f'<div class="proof-span">&ldquo;{e(ev["claim_span"])}&rdquo;</div>'
    meta_bits = " · ".join(bit for bit in (period_text, retrieved_at) if bit)
    return (
        '<div class="claim-row">'
        f'<div class="claim-field">{e(field_label)}{subkey_suffix}</div>'
        f'<div class="claim-value">{value_text}<span class="badge {e(availability)}">{e(availability)}</span></div>'
        f'<div class="claim-meta">{source_link}<br>{meta_bits}</div>'
        f"{proof_span}"
        "</div>"
    )


def render_zone(title: str, families: list[str], claims: list[dict[str, Any]], evidence_by_id: dict[str, dict[str, Any]], *, external: bool = False) -> str:
    rows = [render_claim_row(c, evidence_by_id) for c in claims if c["field"] in families and c["availability"] == "available"]
    if not rows:
        return ""
    css_class = "zone external" if external else "zone"
    return f'<section class="{css_class}"><h2>{e(title)}</h2>{"".join(rows)}</section>'


def render_not_found(availability: dict[str, dict[str, Any]]) -> str:
    gaps = [(field, entry) for field, entry in availability.items() if entry["state"] in ("not_available", "blocked", "ambiguous", "failed")]
    if not gaps:
        return ""
    rows = "".join(
        f'<div class="row"><span>{e(family_label(field))}</span>'
        f'<span class="reason">{e(entry["state"])} · {e(entry["reason"])}</span></div>'
        for field, entry in sorted(gaps)
    )
    return f'<section class="zone"><h2>NOT FOUND</h2><div class="not-found-ledger">{rows}</div></section>'


def render_changes(changes: list[dict[str, Any]]) -> str:
    if not changes:
        return ""
    rows = []
    for change in changes:
        field_label = family_label(change["field"])
        change_type = change["change_type"]
        if change_type == "new_value":
            body = f'<span class="new">{e(change.get("new_value"))}</span> (new)'
        elif change_type == "changed_value":
            body = f'<span class="old">{e(change.get("old_value"))}</span> → <span class="new">{e(change.get("new_value"))}</span>'
        elif change_type == "removed_value":
            body = f'<span class="old">{e(change.get("old_value"))}</span> (removed)'
        else:
            body = f'<span class="old">{e(change.get("old_value"))}</span> (source unavailable this run)'
        rows.append(f'<div class="change-row"><span class="delta {e(change_type)}">{e(change_type.upper())}</span> {e(field_label)}: {body}</div>')
    return f'<section class="zone"><h2>WHAT CHANGED</h2>{"".join(rows)}</section>'


def status_badge(availability_claims: list[dict[str, Any]]) -> str:
    status_claim = next((c for c in availability_claims if c["field"] == "status_flags" and c["availability"] == "available"), None)
    if not status_claim:
        return '<span class="status-badge">STATUS UNKNOWN</span>'
    value = status_claim["value"] or {}
    if value.get("bankrupt"):
        return '<span class="status-badge risk">BANKRUPT</span>'
    if value.get("liquidating"):
        return '<span class="status-badge risk">UNDER LIQUIDATION</span>'
    if value.get("deregistered_since_snapshot"):
        return '<span class="status-badge risk">DEREGISTERED</span>'
    return '<span class="status-badge">ACTIVE</span>'


def render_company_page(envelope: dict[str, Any]) -> str:
    org = envelope["organisation_number"]
    claims = envelope.get("claims", [])
    availability = envelope.get("availability", {})
    evidence_by_id = _evidence_by_id(envelope)
    identity_claim = next((c for c in claims if c["field"] == "legal_identity" and c["availability"] == "available"), None)
    name = (identity_claim["value"] or {}).get("name") if identity_claim else f"ORGANISATION {org}"

    summary = envelope.get("summary") or {}
    summary_html = (
        f'<div class="summary-panel">{e(summary.get("text") or "No summary available.")}'
        f'<div class="not-found">{e(summary.get("not_found_sentence") or "")}</div>'
        f'<div class="generator">GENERATOR: {e(summary.get("generator") or "n/a")} · REFRESH: {e(summary.get("refresh_baseline") or "n/a")}</div>'
        "</div>"
    )

    main_zone = render_zone("RECORD", MAIN_FAMILIES, claims, evidence_by_id)
    external_zone = render_zone("EXTERNAL SIGNAL", EXTERNAL_ORDER, claims, evidence_by_id, external=True)
    changes_html = render_changes(envelope.get("changes") or [])
    not_found_html = render_not_found(availability)

    errors = envelope.get("errors") or []
    errors_html = ""
    if errors:
        rows = "".join(f'<div class="claim-row error-row">{e(err.get("stage"))}: {e(err.get("message"))}</div>' for err in errors)
        errors_html = f'<section class="zone"><h2>ERRORS</h2>{rows}</section>'

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{e(name)} // {e(org)} — SIGNALPOST</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="../style.css">
</head>
<body>
<main>
<a class="back-link" href="../index.html">← BACK TO INDEX</a>
<div class="dossier">
  <span class="unit">[ UNIT: {e(org)} ]</span>
  <span class="name">{e(name)}</span>
  {status_badge(claims)}
</div>
{summary_html}
{external_zone}
{changes_html}
{main_zone}
{not_found_html}
{errors_html}
</main>
</body>
</html>
"""


def _coverage_barcode(availability: dict[str, dict[str, Any]]) -> str:
    ticks = []
    for family in FIELD_FAMILIES:
        state = (availability.get(family) or {}).get("state", "failed")
        ticks.append(f'<span class="tick {e(state)}" title="{e(family_label(family))}: {e(state)}"></span>')
    return f'<span class="barcode">{"".join(ticks)}</span>'


def _first_value(claims: list[dict[str, Any]], field: str, subkey: str | None = None) -> Any:
    claim = next((c for c in claims if c["field"] == field and c.get("subkey") == subkey and c["availability"] == "available"), None)
    return claim["value"] if claim else None


def render_index_row(envelope: dict[str, Any]) -> str:
    org = envelope["organisation_number"]
    claims = envelope.get("claims", [])
    availability = envelope.get("availability", {})
    identity = _first_value(claims, "legal_identity") or {}
    name = identity.get("name") or f"ORGANISATION {org}"
    address = _first_value(claims, "registered_address", "business_address") or {}
    municipality = address.get("municipality") or ""
    industry = _first_value(claims, "industry") or {}
    nace = industry.get("code") or ""
    employees = _first_value(claims, "employees")
    latest_record = None
    for claim in claims:
        if claim["field"] == "annual_accounts" and claim.get("reporting_period"):
            period_to = claim["reporting_period"].get("to") or ""
            if latest_record is None or period_to > latest_record:
                latest_record = period_to
    if envelope["run"]["terminal_status"] == "failed":
        search_blob = f"{org} {name}"
        row_class = ' class="error-row"'
        cells = (
            f'<td data-label="ORGNR" data-orgnr="{e(org)}" class="mono">{e(org)}</td>'
            f'<td data-label="NAME" class="name" colspan="6">[ PARSE_ERROR ] {e(name)}</td>'
        )
        return f'<tr{row_class} data-search="{e(search_blob.lower())}" data-orgnr="{e(org)}" data-name="{e(name.lower())}" data-municipality="" data-nace="" data-employees="" data-period="">{cells}</tr>'
    search_blob = " ".join(str(v) for v in (org, name, municipality, nace) if v).lower()
    return (
        f'<tr data-search="{e(search_blob)}" data-orgnr="{e(org)}" data-name="{e(name.lower())}" '
        f'data-municipality="{e(municipality.lower())}" data-nace="{e(nace)}" data-employees="{e(employees if employees is not None else "")}" data-period="{e(latest_record or "")}">'
        f'<td data-label="ORGNR" class="mono"><a href="company/{e(org)}.html">{e(org)}</a></td>'
        f'<td data-label="NAME" class="name"><a href="company/{e(org)}.html">{e(name)}</a></td>'
        f'<td data-label="MUNICIPALITY">{e(municipality)}</td>'
        f'<td data-label="NACE">{e(nace)}</td>'
        f'<td data-label="EMP">{e(employees if employees is not None else "—")}</td>'
        f'<td data-label="LATEST FILED">{e(latest_record or "—")}</td>'
        f'<td data-label="COVERAGE">{_coverage_barcode(availability)}</td>'
        "</tr>"
    )


def render_index(envelopes: list[dict[str, Any]], run_report: dict[str, Any]) -> str:
    verified_count = sum(
        1 for env in envelopes
        if (env.get("availability", {}).get("legal_identity") or {}).get("state") == "available"
    )
    rows_html = "".join(render_index_row(env) for env in envelopes)
    run_id = run_report.get("run_id", "n/a")
    generated_at = run_report.get("completed_at", "n/a")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>SIGNALPOST // COMPANY INTELLIGENCE TERMINAL</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="style.css">
</head>
<body>
<header class="bar">
  <div class="brand">SIGNALPOST <span class="sub">// COMPANY INTELLIGENCE TERMINAL</span></div>
  <dl class="run-meta">
    <div><dt>RUN</dt><dd>{e(run_id)}</dd></div>
    <div><dt>GENERATED</dt><dd>{e(generated_at)}</dd></div>
    <div><dt>STATUS</dt><dd>{verified_count} / {len(envelopes)} IDENTITY-VERIFIED</dd></div>
  </dl>
</header>
<main>
  <div class="controls">
    <input id="search" type="search" placeholder="FILTER BY NAME, ORGNR, MUNICIPALITY, NACE…" autocomplete="off">
  </div>
  <table id="companies">
    <thead>
      <tr>
        <th data-sort="orgnr">ORGNR</th>
        <th data-sort="name">NAME</th>
        <th data-sort="municipality">MUNICIPALITY</th>
        <th data-sort="nace">NACE</th>
        <th data-sort="employees">EMP</th>
        <th data-sort="period">LATEST FILED</th>
        <th>COVERAGE</th>
      </tr>
    </thead>
    <tbody>
{rows_html}
    </tbody>
  </table>
  <p id="empty-state" hidden>NO RESULTS FOR THIS FILTER</p>
</main>
<script src="app.js"></script>
</body>
</html>
"""


def render_empty_index() -> str:
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>SIGNALPOST // COMPANY INTELLIGENCE TERMINAL</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="style.css">
</head>
<body>
<header class="bar"><div class="brand">SIGNALPOST <span class="sub">// COMPANY INTELLIGENCE TERMINAL</span></div></header>
<main>
  <p id="empty-state">NO COMPANIES LOADED // point --out at a run directory</p>
</main>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Render the Signalpost verification site")
    parser.add_argument("--envelopes", default="out/envelopes.jsonl")
    parser.add_argument("--report", default="out/run-report.json")
    parser.add_argument("--out", default="out/site")
    args = parser.parse_args()

    out_dir = Path(args.out)
    (out_dir / "company").mkdir(parents=True, exist_ok=True)
    assets_dir = Path(__file__).parent / "site_assets"
    shutil.copyfile(assets_dir / "style.css", out_dir / "style.css")
    shutil.copyfile(assets_dir / "app.js", out_dir / "app.js")

    envelopes_path = Path(args.envelopes)
    if not envelopes_path.exists():
        (out_dir / "index.html").write_text(render_empty_index(), encoding="utf-8")
        print(f"[build_site] no envelopes found at {envelopes_path}; wrote empty-state index")
        return

    envelopes = load_envelopes(envelopes_path)
    report_path = Path(args.report)
    run_report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}

    if not envelopes:
        (out_dir / "index.html").write_text(render_empty_index(), encoding="utf-8")
        print("[build_site] envelopes file was empty; wrote empty-state index")
        return

    for envelope in envelopes:
        org = envelope["organisation_number"]
        (out_dir / "company" / f"{org}.html").write_text(render_company_page(envelope), encoding="utf-8")

    (out_dir / "index.html").write_text(render_index(envelopes, run_report), encoding="utf-8")
    print(f"[build_site] wrote {len(envelopes)} company pages + index to {out_dir}")


if __name__ == "__main__":
    main()
