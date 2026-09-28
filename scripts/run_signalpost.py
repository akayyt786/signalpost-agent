#!/usr/bin/env python3
"""Signalpost batch driver: Norwegian organisation numbers in, one terminal envelope per input row out.

    uv run python scripts/run_signalpost.py --input <batch file> --out out/

See README.md for the full command and OUTPUT_CONTRACT.md-successor (the Envelope model in
src/norway_company_agent/envelope.py) for the exact output shape.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.envelope import FIELD_FAMILIES, EnvelopeBuilder  # noqa: E402
from norway_company_agent.evidence import utc_now  # noqa: E402
from norway_company_agent.foundation import populate_foundation  # noqa: E402
from norway_company_agent.input_batch import read_batch  # noqa: E402
from norway_company_agent.sourcepacks import build_entity_pack, build_reference_pack, build_update_pack  # noqa: E402

AGENT_VERSION = "1.0.0"


def _code_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=5).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _invalid_row_envelope(row: dict[str, Any], *, run_id: str, started_at: str, code_commit: str) -> dict[str, Any]:
    org_placeholder = row.get("organisation_number") or str(row.get("raw"))[:64]
    builder = EnvelopeBuilder(org_placeholder, run_id=run_id, started_at=started_at, agent_version=AGENT_VERSION, code_commit=code_commit)
    builder.add_error(stage="input", message=row.get("error") or "invalid input row")
    for family in FIELD_FAMILIES:
        builder.set_availability(family, "failed", "invalid_input")
    return builder.build(completed_at=utc_now(), terminal_status="failed")


class Budget:
    def __init__(self, *, max_requests: int, time_limit_s: float) -> None:
        self.max_requests = max_requests
        self.time_limit_s = time_limit_s
        self.started = time.monotonic()
        self._requests = 0
        self._lock = threading.Lock()

    def spend(self, n: int) -> None:
        with self._lock:
            self._requests += n

    @property
    def requests_spent(self) -> int:
        with self._lock:
            return self._requests

    def exhausted(self) -> bool:
        if time.monotonic() - self.started >= self.time_limit_s:
            return True
        return self.requests_spent >= self.max_requests


def process_company(
    org: str,
    *,
    entity_pack,
    update_pack,
    run_id: str,
    started_at: str,
    code_commit: str,
    budget: Budget,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Returns (envelope, metrics). metrics feeds the run report's request/byte/latency accounting."""
    company_started = time.monotonic()
    builder = EnvelopeBuilder(org, run_id=run_id, started_at=started_at, agent_version=AGENT_VERSION, code_commit=code_commit)
    if budget.exhausted():
        for family in FIELD_FAMILIES:
            builder.set_availability(family, "not_available", "budget_exhausted")
        envelope = builder.build(completed_at=utc_now(), terminal_status="partial")
        return envelope, {"requests": 0, "bytes": 0, "runtime_ms": 0, "budget_exhausted": True}

    outcome = populate_foundation(builder, org, entity_pack, update_pack)
    # Extension point: steps 5-7/9/10 (website identity gate, NAV jobs, registries/references,
    # refresh diff, synthesis) attach more claims to `builder` here once implemented, gated on
    # `outcome["identity_ok"]` so a failed identity never reaches an external-lookup layer.
    terminal_status = "completed" if outcome["identity_ok"] else "failed"
    envelope = builder.build(completed_at=utc_now(), terminal_status=terminal_status)
    budget.spend(envelope["operations"]["requests"])
    metrics = {
        "requests": envelope["operations"]["requests"],
        "bytes": envelope["operations"]["bytes"],
        "runtime_ms": int((time.monotonic() - company_started) * 1000),
        "budget_exhausted": False,
    }
    return envelope, metrics


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    tmp.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Signalpost: Norwegian company-research batch driver")
    parser.add_argument("--input", required=True, help="Batch file: .txt, .jsonl, .json or .csv (optionally .gz)")
    parser.add_argument("--out", default="out", help="Output directory")
    parser.add_argument("--cache", default="cache", help="Bulk-download cache directory")
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--max-requests", type=int, default=6000)
    parser.add_argument("--time-limit", type=float, default=2400.0, help="Seconds before remaining companies are marked budget_exhausted")
    parser.add_argument("--checkpoint-every", type=int, default=25)
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args()

    started_at = utc_now()
    run_id = args.run_id or f"local-{int(time.time())}"
    code_commit = _code_commit()
    out_dir = Path(args.out)
    cache_dir = Path(args.cache)

    rows = read_batch(args.input)
    valid_orgs_in_order = [row["organisation_number"] for row in rows if row["valid"]]
    unique_orgs = list(dict.fromkeys(valid_orgs_in_order))  # de-duplicate, preserve first-seen order

    print(f"[run_signalpost] {len(rows)} input rows, {len(unique_orgs)} unique valid organisation numbers", file=sys.stderr)

    entity_pack = build_entity_pack(unique_orgs, cache_dir)
    update_pack = build_update_pack(entity_pack.snapshot)
    print(f"[run_signalpost] entity pack: {len(entity_pack.index)}/{len(unique_orgs)} found in bulk, {update_pack.pages_fetched} update pages, {len(update_pack.changed)} changed since snapshot", file=sys.stderr)

    budget = Budget(max_requests=args.max_requests, time_limit_s=args.time_limit)
    computed: dict[str, dict[str, Any]] = {}
    all_metrics: list[dict[str, Any]] = []
    degradations: list[str] = []

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(process_company, org, entity_pack=entity_pack, update_pack=update_pack, run_id=run_id, started_at=started_at, code_commit=code_commit, budget=budget): org
            for org in unique_orgs
        }
        completed_count = 0
        for future in as_completed(futures):
            org = futures[future]
            envelope, metrics = future.result()
            computed[org] = envelope
            all_metrics.append(metrics)
            if metrics.get("budget_exhausted") and "budget_exhausted" not in degradations:
                degradations.append("budget_exhausted")
            completed_count += 1
            if completed_count % args.checkpoint_every == 0:
                print(f"[run_signalpost] {completed_count}/{len(unique_orgs)} companies processed, {budget.requests_spent} requests spent", file=sys.stderr)

    envelopes: list[dict[str, Any]] = []
    for row in rows:
        if row["valid"]:
            envelopes.append(computed[row["organisation_number"]])
        else:
            envelopes.append(_invalid_row_envelope(row, run_id=run_id, started_at=started_at, code_commit=code_commit))

    write_jsonl(out_dir / "envelopes.jsonl", envelopes)

    completed_at = utc_now()
    runtimes = sorted(m["runtime_ms"] for m in all_metrics if m["runtime_ms"])
    p50 = runtimes[len(runtimes) // 2] if runtimes else None
    p95 = runtimes[min(len(runtimes) - 1, int(len(runtimes) * 0.95))] if runtimes else None
    report = {
        "run_id": run_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "input_rows": len(rows),
        "unique_companies": len(unique_orgs),
        "emitted_envelopes": len(envelopes),
        "registry_snapshot": {
            "url": entity_pack.snapshot.get("url"),
            "etag": entity_pack.snapshot.get("etag"),
            "last_modified": entity_pack.snapshot.get("last_modified"),
            "downloaded_at": entity_pack.snapshot.get("downloaded_at"),
            "sha256": entity_pack.snapshot.get("sha256"),
        },
        "operations": {
            "requests": sum(m["requests"] for m in all_metrics),
            "bytes": sum(m["bytes"] for m in all_metrics),
            "p50_ms": p50,
            "p95_ms": p95,
            "third_party_cost_usd": 0.0,
        },
        "budget_exhausted": bool(degradations),
        "degradations": degradations,
        "validation": {
            "passed": len(envelopes) == len(rows),
            "expected_count": len(rows),
            "emitted_count": len(envelopes),
        },
    }
    (out_dir / "run-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["validation"]["passed"] else 1)


if __name__ == "__main__":
    main()
