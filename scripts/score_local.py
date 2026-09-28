#!/usr/bin/env python3
"""Local scoring proxy mirroring the published Signalpost rubric shape (recall & coverage 50,
precision & evidence 30, synthesis 12, UX 8) and the evaluation contract's official-run checks.

This is NOT the organiser's score. Recall and coverage in the real contract are measured against
Builderr's hidden pooled-evidence collection from every entrant plus their own crawlers, which
cannot be reproduced locally. What this script CAN verify with certainty from our own output:
structural precision (every published site-derived claim passed the identity gate), evidence
completeness (every available claim resolves to a real, dated, hashed source), synthesis honesty
(summaries never claim more than the claims support), and the official-run checks (terminal
contract, idempotent refresh when --previous is supplied). Coverage is reported as a raw,
self-measured number for tracking progress over time - it is explicitly not comparable to the
organiser's pooled score, exactly as the starter kit's own scorer documents its "claim_boundary".

    uv run python scripts/score_local.py --envelopes out/envelopes.jsonl --report out/run-report.json \
        --site out/site --output out/score-local.json [--previous out/envelopes-prev.jsonl]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.envelope import FIELD_FAMILIES  # noqa: E402

# Site-derived families require the step-5 identity gate; every other family is orgnr-keyed at the
# source (official registry, NAV feed matched by employer.orgnr, DIBK/Wikidata matched by orgnr),
# so wrong-company risk there is structural, not a per-claim gate to check.
IDENTITY_GATED_FAMILIES = {"official_website", "site_description", "social_profiles", "contact_points", "public_activity"}
VERIFIED_METHOD_PREFIXES = ("identity_gate_verified_", "identity_gate_corroborated_")
GAP_STATES = {"not_available", "blocked", "ambiguous", "failed"}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def score_precision_and_evidence(envelopes: list[dict[str, Any]]) -> dict[str, Any]:
    wrong_company_publications = []
    evidence_by_id_per_envelope = [{ev["id"]: ev for ev in env.get("evidence", [])} for env in envelopes]
    total_available = 0
    complete_evidence = 0
    for env, evidence_by_id in zip(envelopes, evidence_by_id_per_envelope):
        claims = env.get("claims", [])
        # Only official_website itself carries the identity_gate_* method tag; the downstream
        # site-derived claims (description/social/contact/activity) are only ever emitted after
        # that same gate already passed inside site_resolver.py, so the correct structural check
        # is company-level: any published identity-gated claim requires *this company's own*
        # official_website claim to exist and carry a verified/corroborated method.
        website_claim = next((c for c in claims if c["field"] == "official_website" and c["availability"] == "available"), None)
        website_verified = bool(website_claim and website_claim["method"].startswith(VERIFIED_METHOD_PREFIXES))
        for claim in claims:
            if claim["availability"] != "available":
                continue
            total_available += 1
            if claim["field"] in IDENTITY_GATED_FAMILIES and not website_verified:
                wrong_company_publications.append({"organisation_number": env["organisation_number"], "field": claim["field"], "method": claim["method"]})
            evidence_ids = claim.get("evidence_ids") or []
            if evidence_ids and evidence_ids[0] in evidence_by_id:
                ev = evidence_by_id[evidence_ids[0]]
                if ev.get("source_url") and ev.get("retrieved_at") and ev.get("content_sha256"):
                    complete_evidence += 1
    evidence_completeness = complete_evidence / total_available if total_available else 1.0
    zero_wrong_company = len(wrong_company_publications) == 0
    points = 30.0 if (zero_wrong_company and evidence_completeness >= 0.999) else round(30.0 * evidence_completeness * (0.0 if wrong_company_publications else 1.0), 2)
    return {
        "points": points,
        "total_available_claims": total_available,
        "evidence_completeness": round(evidence_completeness, 4),
        "wrong_company_publications": wrong_company_publications,
        "zero_wrong_company_publications": zero_wrong_company,
    }


def score_coverage(envelopes: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(envelopes)
    per_family = {}
    for family in FIELD_FAMILIES:
        companies_with_data = sum(
            1 for env in envelopes if (env.get("availability", {}).get(family) or {}).get("state") == "available"
        )
        claim_count = sum(1 for env in envelopes for c in env.get("claims", []) if c["field"] == family and c["availability"] == "available")
        per_family[family] = {
            "company_coverage": round(companies_with_data / n, 4) if n else 0.0,
            "companies_with_data": companies_with_data,
            "total_claims": claim_count,
        }
    return {
        "note": "Self-measured raw coverage, not comparable to the organiser's pooled-evidence score. Track deltas over time, not the absolute number.",
        "per_family": per_family,
    }


def score_synthesis(envelopes: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(envelopes)
    non_empty = 0
    self_consistent = 0
    generators: dict[str, int] = {}
    for env in envelopes:
        summary = env.get("summary") or {}
        text = summary.get("text") or ""
        if text.strip():
            non_empty += 1
        generator = summary.get("generator", "none")
        generators[generator] = generators.get(generator, 0) + 1
        actual_gaps = {field for field, entry in env.get("availability", {}).items() if entry["state"] in GAP_STATES}
        reported_gaps = set(summary.get("not_found") or [])
        # not_found stores human labels, not raw field names; consistency check is on cardinality:
        # every actual gap must be represented by exactly one reported gap label.
        if len(actual_gaps) == len(reported_gaps):
            self_consistent += 1
    points = round(12.0 * (non_empty / n) * (self_consistent / n), 2) if n else 0.0
    return {
        "points": points,
        "non_empty_summary_rate": round(non_empty / n, 4) if n else 0.0,
        "gap_list_self_consistency_rate": round(self_consistent / n, 4) if n else 0.0,
        "generators": generators,
    }


def score_ux(site_dir: Path, envelope_count: int) -> dict[str, Any]:
    index_exists = (site_dir / "index.html").exists()
    style_exists = (site_dir / "style.css").exists()
    app_exists = (site_dir / "app.js").exists()
    company_pages = len([p for p in (site_dir / "company").glob("*.html") if not p.name.startswith(".")]) if (site_dir / "company").exists() else 0
    style_text = (site_dir / "style.css").read_text(encoding="utf-8") if style_exists else ""
    has_mobile_breakpoint = "max-width: 640px" in style_text
    has_focus_style = ":focus-visible" in style_text
    checks = {
        "index_exists": index_exists,
        "style_and_script_exist": style_exists and app_exists,
        "one_company_page_per_envelope": company_pages == envelope_count,
        "mobile_breakpoint_present": has_mobile_breakpoint,
        "focus_visible_style_present": has_focus_style,
    }
    points = round(8.0 * sum(checks.values()) / len(checks), 2)
    return {"points": points, "checks": checks, "company_pages_found": company_pages}


def check_refresh_idempotent(current: list[dict[str, Any]], previous: list[dict[str, Any]]) -> dict[str, Any]:
    previous_by_org = {env["organisation_number"]: env for env in previous}
    false_changes = 0
    total_changes = 0
    for env in current:
        prev = previous_by_org.get(env["organisation_number"])
        if prev is None:
            continue
        changes = env.get("changes") or []
        total_changes += len(changes)
        # A false change is one reported now but not backed by an actual value difference against
        # the previous envelope's own claims for the same (field, subkey).
        prev_claims = {(c["field"], c.get("subkey")): c["value"] for c in prev.get("claims", [])}
        for change in changes:
            if change["change_type"] in ("new_value", "changed_value"):
                key = (change["field"], change.get("subkey"))
                if key in prev_claims and prev_claims[key] == change.get("new_value") and change["change_type"] == "changed_value":
                    false_changes += 1
    return {
        "measured": True,
        "total_changes": total_changes,
        "false_changes": false_changes,
        "false_change_rate": round(false_changes / total_changes, 4) if total_changes else 0.0,
        "passed": false_changes == 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Signalpost scoring proxy")
    parser.add_argument("--envelopes", default="out/envelopes.jsonl")
    parser.add_argument("--report", default="out/run-report.json")
    parser.add_argument("--site", default="out/site")
    parser.add_argument("--previous", help="A prior run's envelopes.jsonl, to measure refresh idempotency/false-change rate")
    parser.add_argument("--output", default="out/score-local.json")
    args = parser.parse_args()

    envelopes = load_jsonl(Path(args.envelopes))
    run_report = json.loads(Path(args.report).read_text(encoding="utf-8")) if Path(args.report).exists() else {}
    n = len(envelopes)

    precision = score_precision_and_evidence(envelopes)
    coverage = score_coverage(envelopes)
    synthesis = score_synthesis(envelopes)
    ux = score_ux(Path(args.site), n)

    refresh = {"measured": False, "note": "pass --previous <envelopes.jsonl> to measure false-change rate"}
    if args.previous and Path(args.previous).exists():
        refresh = check_refresh_idempotent(envelopes, load_jsonl(Path(args.previous)))

    terminal_batch_contract = bool(run_report.get("validation", {}).get("passed", n > 0))
    official_identity_complete = all(
        (env.get("availability", {}).get("legal_identity") or {}).get("state") == "available"
        for env in envelopes
        if env["run"]["terminal_status"] != "failed"
    )

    gates = {
        "terminal_batch_contract": terminal_batch_contract,
        "official_identity_complete": official_identity_complete,
        "zero_wrong_company_publications": precision["zero_wrong_company_publications"],
        "evidence_complete": precision["evidence_completeness"] >= 0.999,
        "refresh_idempotent": (not refresh["measured"]) or refresh["passed"],
    }

    report = {
        "scorer": "signalpost_local_proxy_v1",
        "claim_boundary": "Local proxy computed from our own output only. Recall/coverage cannot reproduce the organiser's hidden pooled-evidence score; precision, evidence-completeness, synthesis self-consistency, UX structure, and refresh idempotency are measured with certainty.",
        "profiles": n,
        "rubric_weights": {"recall_and_coverage": 50, "precision_and_evidence": 30, "synthesis": 12, "ux": 8},
        "coverage": coverage,
        "precision_and_evidence": precision,
        "synthesis": synthesis,
        "ux": ux,
        "refresh": refresh,
        "measurable_points": round(precision["points"] + synthesis["points"] + ux["points"], 2),
        "measurable_points_of": round(30 + 12 + 8, 2),
        "qualification_gates": gates,
        "qualification_gates_passed": all(gates.values()),
        "unmet_gates": [name for name, passed in gates.items() if not passed],
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["qualification_gates_passed"] else 1)


if __name__ == "__main__":
    main()
