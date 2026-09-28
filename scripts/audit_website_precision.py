#!/usr/bin/env python3
"""Independent precision audit for the website-identity publication gate (step 5 / plan item:
"150-company hand-labelled precision=1.000 check").

Draws a large, deterministic sample of companies with a registry-declared website (unique hosts,
see sampling.deterministic_website_audit_sample), builds each one's identity_source straight from
the already-cached bulk registry snapshot (no live per-company API refresh - this audits the
publication *gate*, not registry freshness), and runs the real populate_website() against the real
web. For every claim the gate actually published (verified or corroborated), the evidence's
claim_span (the literal page excerpt the "verified" verdict was based on) is written to
--review-output so a human can read the exact quote and confirm it is a genuine self-referential
match, not a third-party mention (e.g. "our supplier's org.nr is ...").

    uv run python scripts/audit_website_precision.py --count 2000 --seed 20261010 \
        --review-output out/website-precision-audit.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.envelope import EnvelopeBuilder  # noqa: E402
from norway_company_agent.sampling import deterministic_website_audit_sample  # noqa: E402
from norway_company_agent.site_resolver import populate_website  # noqa: E402
from norway_company_agent.sourcepacks import BRREG_ENTITY_BULK_CSV, build_entity_pack, build_reference_pack, cached_download  # noqa: E402


def identity_source_from_row(row: dict[str, Any]) -> dict[str, Any]:
    """Same construction as foundation.populate_foundation's bulk-row branch, without a live
    per-company refresh - this audits the site_resolver/identity gate, not registry freshness."""
    raw = row.get("raw", {})
    return {
        "name": row["name"],
        "website": row.get("website"),
        "business_address": {
            "address": raw.get("forretningsadresse.adresse"),
            "postal_code": raw.get("forretningsadresse.postnummer"),
            "city": raw.get("forretningsadresse.poststed"),
            "municipality": raw.get("forretningsadresse.kommune"),
            "country": raw.get("forretningsadresse.land"),
        },
        "email": raw.get("epostadresse"),
        "phone": raw.get("telefon"),
    }


def audit_one(org: str, row: dict[str, Any], reference_pack) -> dict[str, Any]:
    builder = EnvelopeBuilder(org, run_id="audit", started_at="1970-01-01T00:00:00Z", agent_version="audit", code_commit="audit")
    identity_source = identity_source_from_row(row)
    populate_website(builder, org, identity_source, reference_pack)
    envelope = builder.build(completed_at="1970-01-01T00:00:00Z", terminal_status="completed")
    availability = envelope["availability"]["official_website"]
    claims = [c for c in envelope["claims"] if c["field"] == "official_website"]
    evidence_by_id = {e["id"]: e for e in envelope["evidence"]}
    result = {
        "organisation_number": org,
        "name": row["name"],
        "registry_website": row.get("website"),
        "state": availability["state"],
        "reason": availability["reason"],
    }
    if claims:
        claim = claims[0]
        evidence = evidence_by_id.get(claim["evidence_ids"][0], {})
        result["published_url"] = claim["value"]
        result["method"] = claim["method"]
        result["claim_span"] = evidence.get("claim_span")
        result["source_url"] = evidence.get("source_url")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", default="cache")
    parser.add_argument("--count", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=20261010)
    parser.add_argument("--workers", type=int, default=24)
    parser.add_argument("--review-output", default="out/website-precision-audit.json")
    args = parser.parse_args()

    snapshot = cached_download(BRREG_ENTITY_BULK_CSV, Path(args.cache), "enheter-bulk.csv.gz", timeout=900.0)
    selected, sample_meta = deterministic_website_audit_sample(
        snapshot["path"], args.count, excluded_organisation_numbers=set(), seed=args.seed,
    )
    print(f"[audit] sample: {sample_meta}", file=sys.stderr)
    entity_pack = build_entity_pack({row["organisation_number"] for row in selected}, Path(args.cache))
    reference_pack = build_reference_pack(Path(args.cache))

    # Re-key selected rows through entity_pack so identity_source_from_row sees the full "raw"
    # dict (business address, phone, email) that build_entity_pack's index carries and the audit
    # sample's minimal row does not.
    rows_by_org = {org: entity_pack.get(org) for org in (row["organisation_number"] for row in selected)}

    started = time.monotonic()
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(audit_one, org, row, reference_pack): org
            for org, row in rows_by_org.items() if row is not None
        }
        done = 0
        for future in as_completed(futures):
            results.append(future.result())
            done += 1
            if done % 200 == 0:
                print(f"[audit] {done}/{len(futures)} in {time.monotonic() - started:.0f}s", file=sys.stderr)

    published = [r for r in results if r["state"] == "available"]
    verified = [r for r in published if r["reason"] == "verified_by_organisation_number"]
    corroborated = [r for r in published if r["reason"] == "corroborated_by_name_and_address_or_phone"]
    reason_counts: dict[str, int] = {}
    for r in results:
        reason_counts[r["reason"]] = reason_counts.get(r["reason"], 0) + 1

    report = {
        "sample": sample_meta,
        "audited": len(results),
        "published": len(published),
        "verified": len(verified),
        "corroborated": len(corroborated),
        "reason_counts": reason_counts,
        "published_for_hand_review": published,
    }
    Path(args.review_output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.review_output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "published_for_hand_review"}, indent=2))
    print(f"[audit] wrote {len(published)} published claims for hand review to {args.review_output}", file=sys.stderr)


if __name__ == "__main__":
    main()
