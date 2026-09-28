# 100-company smoke test

Run live, from a cold state (`rm -rf state out`), against the real Brønnøysund registry, NAV job
feed, DIBK register and Wikidata, using the documented commands from `README.md`:

```bash
curl -sSL -o signalpost-company-universe-2025.jsonl.gz https://builderr.ai/signalpost-company-universe-2025.jsonl.gz
uv run python select_entry_batch.py --universe signalpost-company-universe-2025.jsonl.gz --count 100 --output smoke-companies.jsonl --seed 20260823
./run.sh smoke-companies.jsonl out/smoke
uv run python scripts/build_site.py --envelopes out/smoke/envelopes.jsonl --report out/smoke/run-report.json --out out/smoke/site
uv run python scripts/score_local.py --envelopes out/smoke/envelopes.jsonl --report out/smoke/run-report.json --site out/smoke/site
```

Universe SHA-256: `1c89710e5b01f8617e86d09fbdff4a52f2f8dbbba297e74f7164b5984f5a0384` (matches the
organiser-declared hash).

## Result

| | |
|---|---|
| Input rows | 100 |
| Unique companies | 100 |
| Emitted envelopes | 100 (validation: `passed: true`) |
| Cold-start companies | 100/100 (0 incremental — first run against empty state) |
| Requests | 554 (well inside `--max-requests 6000`) |
| Runtime | 187s (well inside `--time-limit 2400`) |
| Third-party cost | $0.00 (no LLM key set) |
| Budget exhausted / degradations | none |

Full evidence: [`docs/evidence/smoke-100/`](evidence/smoke-100/) — `input-batch.jsonl` (the exact
100 organisation numbers), `envelopes.jsonl` (all 100 terminal envelopes, unmodified), `run-report.json`,
`score-local.json`.

## Local score

```
precision_and_evidence: 30/30  (evidence_completeness 1.0, zero wrong-company publications)
synthesis:               12/12 (100% non-empty summaries, 100% gap-list self-consistency)
ux:                        8/8 (all five structural checks pass)
measurable_points:       50/50
qualification_gates:     terminal_batch_contract, official_identity_complete,
                          zero_wrong_company_publications, evidence_complete,
                          refresh_idempotent — all pass
```

Recall/coverage cannot be scored locally (it requires the organiser's hidden pooled-evidence
collection); the self-measured coverage numbers are in `score-local.json`. On this particular
random 100-company draw, `official_website` coverage was 0% — checked and expected, not a bug: 62
of the 100 had no website/corporate-email/cross-reference candidate at all (consistent with the
measured ~74% of the population having neither), 18 had an unreachable candidate, 11 had a
candidate whose page evidence was too weak to pass the identity gate, 8 were blocked by the
single-token-name-requires-organisation-number guard, and 1 was a genuine `conflict` (a different
valid organisation number on the page). Zero candidates reached `verified`/`corroborated` in this
particular draw — see `docs/website-precision-audit-report.md` for the separate 2,000-company,
website-having-only audit that measured the identity gate's actual precision (142/142 = 1.000) and
publish rate.

## Live site

`.github/workflows/pages.yml` regenerates this same 100-company smoke run inside GitHub Actions on
every push to `main` that touches the agent code, and publishes it to GitHub Pages:
**https://akayyt786.github.io/signalpost-agent/**
