To: submit@builderr.ai
Subject: Signalpost submission — Arman Katia

Hi,

Signalpost entry submission.

**Entrant:** Arman Katia (akaykatia9@gmail.com), solo.

**Repository:** https://github.com/akayyt786/signalpost-agent
**Commit:** `663c16ed713b62ccd657f7353591c94fb49e115d` (tag `v1`)

**Run command:**
```
./run.sh <batch-file> <out-dir>
```
`batch-file` accepts `.txt` (one organisation number per line), `.jsonl`, `.json`, or `.csv`
(any optionally `.gz`). Install step: `uv sync --frozen` (Python 3.12+, `run.sh` runs this itself).

**100-company smoke test:** `docs/smoke-test-report.md` (full raw evidence in
`docs/evidence/smoke-100/`) — 100/100 envelopes emitted from a cold start, 654 requests, 191s,
$0 cost, zero degradations, zero wrong-company publications. Local score 50/50 measurable points,
all five qualification gates pass.

**Website-identity precision audit:** `docs/website-precision-audit-report.md` — 2,000 companies
audited, all 142 published claims hand-checked, precision 142/142 = 1.000. Now a nightly CI
regression gate (`.github/workflows/precision-audit.yml`).

**Live verification site:** https://akayyt786.github.io/signalpost-agent/ (regenerated live inside
GitHub Actions from a fresh 100-company run on every push touching agent code).

**Data sources (5 connectors, 18 field families):** official Brønnøysund registry foundation
(identity, address, industry, employees, status, annual accounts, roles, group structure,
locations), a proof-gated website identity layer (official website, description, social profiles,
contact points, public activity — published only on organisation-number or exact-name-plus-address
proof), NAV's job-vacancy feed, DIBK's central-approval register + Wikidata external references,
and the EU's TED public-procurement notice service (contract awards this organisation won, matched
by exact organisation number). Five additional sources were investigated and rejected on a
live-verification bar (personal-credential walls or no exact-entity attribution possible) — see
`CRAWLERS.md` for the full ledger.

**Models / APIs / licences:**
- Brønnøysund company registry (Norway) — NLOD 2.0.
- NAV public job-vacancy feed — public token fetched at run time (no personal credential),
  terms at `https://arbeidsplassen.nav.no/vilkar-api`.
- DIBK central-approval register — public register.
- Wikidata — CC0.
- EU TED public-procurement notice service — freely reusable for commercial or non-commercial
  purposes, no auth.
- No LLM is called by default; declared third-party cost is **$0.00**. If `SIGNALPOST_LLM_API_KEY`
  is supplied, an optional grounded-synthesis rewrite may run against `SIGNALPOST_LLM_MODEL`
  (default `gpt-4.1-mini`), budget-capped at `--llm-max-cost-usd` (default $3.00/run), and its
  output is only used if a validator confirms every number, date, name and URL it contains is
  verified present in the claim set.

Full source ledger with exact endpoints: `CRAWLERS.md`. What was deliberately not implemented and
why (LinkedIn/Meta/Glassdoor/Indeed and others): same file, "Deliberately not implemented" section.

**Expected cost per official run:** $0.00 (LLM disabled by default).

Contact: akaykatia9@gmail.com

Thanks,
Arman
