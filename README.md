# Signalpost agent

An evidence-backed company-profile agent for the [Signalpost competition](https://builderr.ai)
(hosted on Unstop): give it a Norwegian organisation number, get back a JSON envelope of claims,
each one tied to a dated, hashed, URL-linked source — never a fact about the wrong company.

## Run it

```bash
./run.sh <batch-file> <out-dir>
```

`batch-file` is a list of Norwegian organisation numbers — `.txt` (one per line), `.jsonl`, `.json`,
or `.csv` (any of these optionally `.gz`), using any of the field names
`organisation_number` / `organisasjonsnummer` / `orgnr` / `org_number` / `organizationNumber`.
`out-dir` defaults to `out/`. The script itself is `uv run python scripts/run_signalpost.py`; `run.sh`
just runs `uv sync --frozen` first and forwards every extra argument.

```bash
# 100-company smoke test
curl -LO https://builderr.ai/signalpost-company-universe-2025.jsonl.gz
uv run python select_entry_batch.py \
  --universe signalpost-company-universe-2025.jsonl.gz --count 100 --output smoke-companies.jsonl
./run.sh smoke-companies.jsonl out/smoke
uv run python scripts/build_site.py --envelopes out/smoke/envelopes.jsonl --report out/smoke/run-report.json --out out/smoke/site
uv run python scripts/score_local.py --envelopes out/smoke/envelopes.jsonl --report out/smoke/run-report.json --site out/smoke/site
```

Install step: `uv sync --frozen` (Python 3.12+, dependencies pinned in `uv.lock`).

Test suite: `uv run --with pytest pytest -q` — 101 tests, 3 subtests, all pure/offline (no live
network calls; live behaviour is verified separately and documented in the git history).

## What it does

For each organisation number, in order:

1. **Identity foundation** (`src/norway_company_agent/foundation.py`) — legal identity, registered
   address, industry (NACE), employee count, status flags, and annual accounts copied verbatim from
   the official Brønnøysund registry (bulk CSV, refreshed against the live API only for companies the
   daily update feed says changed since the snapshot), plus active roles, group structure and
   registered subunits/locations.
2. **Website identity gate** (`src/norway_company_agent/site_resolver.py`) — resolves a candidate
   company website (registered `hjemmeside` → corporate-domain email → DIBK/Wikidata/NAV
   cross-reference → a DNS-verified name-guess as a last resort), then **only publishes a website
   claim when the organisation number itself is found on a fetched page** (`verified`) or the exact
   legal name plus the registered address/phone are corroborated (`corroborated`). A different valid
   organisation number on the page (accountant, franchisor, parent) is a `conflict` and publishes
   nothing; anything weaker is `ambiguous` and also publishes nothing. Single-token legal names never
   reach `corroborated` — they require the organisation number. Only a `verified` site unlocks its
   description, outbound social links, on-site contact details, and on-site news/press pages.
3. **Job postings** (`src/norway_company_agent/connectors/nav_jobs.py`) — from NAV's public job-ad
   feed, re-fetching each ad's detail at run time so only currently `ACTIVE`, unexpired ads for the
   exact `employer.orgnr` are published; `contactList` is never published.
4. **Credentials and external references** (`src/norway_company_agent/connectors/registries.py`) —
   DIBK central-approval status and Wikidata's orgnr-linked external references (Wikipedia, logo,
   social handles), both already orgnr-keyed so there is no identity risk.
5. **Synthesis** (`src/norway_company_agent/summarize.py`) — a deterministic summary built only from
   published claims, always shipped. An optional grounded LLM rewrite runs only when
   `SIGNALPOST_LLM_API_KEY` is set, and its output is discarded unless a validator confirms every
   number, date, name and URL it uses appears verbatim in the claim set.

Every one of the 17 field families (`legal_identity`, `registered_address`, `industry`, `employees`,
`status_flags`, `annual_accounts`, `roles`, `group_structure`, `locations`, `official_website`,
`site_description`, `social_profiles`, `contact_points`, `job_postings`, `public_activity`,
`credentials_and_approvals`, `external_references`) appears in every envelope's `availability` map
with one of `available`, `not_available`, `blocked`, `not_applicable`, `ambiguous`, `failed` and a
machine-readable reason — a checked source with nothing to report is never confused with a source
never reached, and absence is never rendered as zero.

Envelope shape: see `OUTPUT_CONTRACT.md` for a minimal example and
`src/norway_company_agent/envelope.py` for the full Pydantic model.

## Refresh, not re-discovery

Re-running the same batch reuses `state/signalpost.sqlite` (SQLite `claims`/`evidence`/`runs`
tables) and reports only genuine `new_value` / `changed_value` / `removed_value` / `deferred`
changes. A source that fails on a given run never produces `removed_value` for what it previously
found — the old claim is carried forward as `deferred` with the failure reason, so a transient
outage never reads as "this fact disappeared." A cold start (no prior state, the daily-container
case) always reports `changes: []` with `refresh.baseline: "cold_start"`, never a wall of new-value
changes for a company seen for the first time. Duplicate organisation numbers within one input batch
are computed once and reused for every duplicate row — never re-fetched.

## Budget-exhaustion degrade ladder

Under request or time pressure, `run_signalpost.py` degrades gracefully rather than dropping
company rows: as `Budget.pressure_level()` rises, it disables the weakest (DNS name-guess) website
candidate tier, then caps site-subpage crawl depth, then caps NAV job-detail re-fetches, then skips
`group_structure`, and only as a last resort skips the whole website layer for a company. The
**terminal batch contract always holds** — exactly one envelope per input row, in input order, even
at `--max-requests 300` (verified live; see git history for the run-report evidence).

## Verification site

```bash
uv run python scripts/build_site.py --envelopes out/envelopes.jsonl --report out/run-report.json --out out/site
open out/site/index.html   # or serve it — see .github/workflows/pages.yml for GitHub Pages
```

Static HTML/CSS/JS, no framework, no build step, works directly from `file://` or GitHub Pages —
`index.html` is a filterable/sortable table with a 17-cell "coverage barcode" per company (one tick
per field family, coloured by availability); `company/<orgnr>.html` is a full evidence dossier with
every claim's source link, retrieval time and proof span, a "what changed" diff, and a `NOT FOUND`
ledger naming every gap with its reason. A company whose envelope fails to parse still appears in
the index with a `[ PARSE_ERROR ]` badge — never silently dropped.

## Local scoring harness

```bash
uv run python scripts/score_local.py --envelopes out/envelopes.jsonl --report out/run-report.json --site out/site
```

Mirrors the published rubric shape (recall & coverage 50 · precision & evidence 30 · synthesis 12 ·
UX 8) and reports what can be verified with certainty from our own output alone: zero wrong-company
publications (structural check — every published site-derived claim requires its own company's
`official_website` claim to have passed the identity gate), evidence completeness, synthesis
self-consistency, UX structure, and (with `--previous <envelopes.jsonl>`) the refresh false-change
rate. It is explicit that recall/coverage against the organiser's hidden pooled-evidence collection
cannot be reproduced locally — that section is a self-measured coverage number, not a score.

`select_entry_batch.py --splits development:600,validation:200,final:200` freezes three
non-overlapping company sets from the public universe (same seed → same output; no organisation
number or website host is ever split across sets — a group and its subsidiary sharing a domain
always land together).

`scripts/audit_website_precision.py` is the website-identity precision audit: it draws a large,
deterministic, unique-host sample and runs the real identity gate against the live web, writing out
every published claim's evidence for hand review.

## Sources, licences, and what was deliberately left out

Official Brønnøysund registry data (NLOD 2.0), NAV's public job-ad feed (terms:
`https://arbeidsplassen.nav.no/vilkar-api`, public token fetched at run time — no personal
credential required), DIBK's central-approval register, and Wikidata (CC0). See `CRAWLERS.md` for
the full source ledger with exact endpoints and cadence, and `docs/norway-sources.md` for the wider
Brønnøysund endpoint map.

LinkedIn, Meta, Glassdoor, Indeed and similar platforms whose terms prohibit the collection method
used here are **not** implemented, by design — not stubbed, not quarantined behind a flag, simply
absent. `scripts/run_google_news_rss_connector.py` exists but ships **disabled by default**; it may
only be enabled after a hand-labelled precision check per-source, which has not yet been run.

## Declared models and cost

No LLM call is made unless `SIGNALPOST_LLM_API_KEY` is set — the agent runs, and qualifies, with a
declared third-party cost of **$0**. When a key is supplied, the optional synthesis rewrite uses
`SIGNALPOST_LLM_MODEL` (default `gpt-4.1-mini`) against the claim list only, guarded by
`--llm-max-cost-usd` (default 3.00 per run), and its output is used only if every number, date, name
and URL it contains is verified present in the claim set — otherwise the deterministic summary
ships. `summary.generator` records which one actually ran (`deterministic_v1` or `llm_grounded_v1`).
