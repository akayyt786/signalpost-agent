# Source ledger

Every network source this agent talks to, exactly as implemented. No source outside this list is
called. See `docs/norway-sources.md` for the wider Brønnøysund endpoint reference and `README.md`
for what was deliberately left out and why.

## Bulk pulls (one request each, replace thousands of per-company calls)

| Source | Endpoint | Licence | Module |
|---|---|---|---|
| Entity bulk snapshot | `https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv` | NLOD 2.0 | `sourcepacks.py` (`EntityPack`), cached by ETag/Last-Modified |
| Update feed | `https://data.brreg.no/enhetsregisteret/api/oppdateringer/enheter` | NLOD 2.0 | `sourcepacks.py` (`UpdatePack`) — organisation numbers changed since the bulk snapshot, so only those get a live re-check |
| Wikidata SPARQL | `https://query.wikidata.org/sparql` (property `P2333` = Norwegian organisation number) | CC0 | `sourcepacks.py` (`ReferencePack`) |
| DIBK central approval register | `https://sgregister.dibk.no/api/enterprises` | Public register | `sourcepacks.py` (`ReferencePack`) |

## Per-company live calls

| Source | Endpoint | Licence | Cost | Module |
|---|---|---|---|---|
| Entity live re-check | `https://data.brreg.no/enhetsregisteret/api/enheter/{org}` | NLOD 2.0 | only companies in the update feed, or absent from the bulk snapshot | `foundation.py` |
| Annual accounts | `https://data.brreg.no/regnskapsregisteret/regnskap/{org}` | NLOD 2.0 | 1/company | `official.py` — every numeric value copied verbatim, never derived or defaulted to zero |
| Roles | `https://data.brreg.no/enhetsregisteret/api/enheter/{org}/roller` | NLOD 2.0 | 1/company | `official.py` — active roles only; dates of birth discarded |
| Group structure | `https://data.brreg.no/enhetsregisteret/api/konsernstruktur/{org}` | NLOD 2.0 | 1/company (skippable under budget pressure) | `official.py` |
| Subunits/locations | `https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={org}` | NLOD 2.0 | 1/company | `official.py` |
| Company website | resolved candidate (see below) | company-owned page, `robots.txt` honoured | homepage + up to 4 subpages, capped to 1 under budget pressure | `site_resolver.py`, `website.py` |

## External, organisation-number-keyed

| Source | Endpoint | Access | Terms | Module |
|---|---|---|---|---|
| NAV job vacancy feed | `https://pam-stilling-feed.nav.no/api/v1/feed` (index build), `/api/v1/feedentry/{uuid}` (live re-fetch) | Bearer JWT fetched fresh at run time from `https://pam-stilling-feed.nav.no/api/publicToken` — **no personal credential**, fully reproducible by the evaluator | `https://arbeidsplassen.nav.no/vilkar-api` | `connectors/nav_jobs.py`, `scripts/build_nav_index.py` |

The feed is a strictly forward, append-only changelog with no backward or date-jump pagination
(confirmed against its OpenAPI spec). `scripts/build_nav_index.py` is a separately-run, resumable
catch-up crawler (own `--pages`/`--time-limit` budget, cursor persisted in
`data/nav-jobs-cursor.json`) that maintains the shipped `data/nav-jobs-index.jsonl.gz`
(`orgnr → [{ad_uuid, title, published, expires, updated, municipality}]`) with upsert-or-delete
semantics — an ad whose feed status is no longer `ACTIVE` is removed from the index, matching NAV's
own documented consumer pseudocode. At run time, `run_signalpost.py` only ever loads this
already-built index and **re-fetches each candidate ad's detail live** before publishing anything,
so a published `job_postings` claim always reflects the ad's status *at request time*, not merely at
index-build time — an ad that went inactive since the index was last rebuilt is caught immediately.
Never publishes `contactList`; only ads whose re-fetched `employer.orgnr` equals the requested
organisation number and whose `expires` is in the future are published.

## Website candidate resolution order (`site_resolver.py::build_candidates`)

1. `hjemmeside` (registered website) from the bulk entity record.
2. The corporate-domain part of the registered `epostadresse`, excluding a freemail denylist (gmail,
   hotmail, outlook, icloud, yahoo, live, msn, protonmail, and Norwegian consumer ISPs).
3. `www` from a matched DIBK record, `homepage` from a matched NAV job ad, or `website` from a
   matched Wikidata record — all already organisation-number-keyed.
4. A DNS-verified name-guess (`<slug>.no`), attempted only when nothing above produced a candidate.

A candidate is only ever *fetched*; it never publishes a claim without passing the identity gate
described in `README.md` (`verified` / `corroborated` / `conflict` / `ambiguous`).

## Deliberately not implemented

LinkedIn, Meta/Facebook, Glassdoor, Indeed, proff.no/purehelp.no scraping — each has terms or
`robots.txt` that prohibit the collection method this agent would otherwise use. Arbeidstilsynet's
Bemanningsforetaksregisteret endpoint (`data.arbeidstilsynet.no/bemanningsforetaksregisteret2/api`)
returned `403 Forbidden` on a plain GET when tried live; Finanstilsynet's public API
(`api.finanstilsynet.no/registry/v1/`) serves only an HTML docs shell with no discoverable JSON
shape. Both are out of scope rather than guessed at. Mattilsynet's Smilefjes rating feed has no live
distribution host (`hotell.difi.no` does not resolve; `smilefjes.mattilsynet.no/api/sok` returns
404) and is treated as dead.
