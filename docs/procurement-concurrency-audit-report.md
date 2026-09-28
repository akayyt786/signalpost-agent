# Public-contracts connector: concurrency and attribution audit

The website-identity gate got a 2,000-company hand-checked precision audit
(`docs/website-precision-audit-report.md`). The newest connector, `public_contracts` (EU TED),
had only 4 unit tests and one live company before this. Its per-claim attribution risk is
structurally different and much lower than the website gate's (TED's `winner-identifier=<org>`
query filter, not a heuristic name/address match, does the identity work) — but a real risk this
audit specifically targets is **concurrency correctness**: `process_company()` runs many companies
in parallel threads, and a bug there (e.g. shared mutable state) could in principle attribute one
company's contract award to a different company's envelope.

## Method

Ran 500 real companies (`select_entry_batch.py`, seed `20261010`) through the real, unmodified
`run_signalpost.py` pipeline with `--workers 20` — genuine concurrent load, not a synthetic test.
For every organisation number whose envelope published a `public_contracts` claim, independently
re-queried TED fresh (a separate, later HTTP call, outside the pipeline entirely) with
`winner-identifier=<org>` and confirmed the claimed `publication_number` is actually present in
that exact organisation's own live result set.

## Result

500/500 envelopes emitted (`validation.passed: true`). 3 companies published 4
`public_contracts` claims total. **All 4 independently re-verified correct**: every claimed
publication number is genuinely present in a fresh, separate TED query for that exact
organisation number — zero cross-contamination under real concurrent load. Full evidence:
[`docs/evidence/procurement-concurrency-audit/report.json`](evidence/procurement-concurrency-audit/report.json).

Real examples found: Ålesund kommune → project-manager contract (org `998058376`); Innherred
Anskaffelser → company health services (org `987403829`); Klepp kommune (plumbing framework
agreement) and Time Kommune (HVAC installation) both won by org `976700376`.

## Reproduce

```bash
uv run python select_entry_batch.py --universe signalpost-company-universe-2025.jsonl.gz \
  --count 500 --output batch.jsonl --seed 20261010
./run.sh batch.jsonl out --workers 20
# then re-query TED per org.number that published a public_contracts claim and diff
```
