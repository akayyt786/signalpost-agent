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
| EU TED public-procurement notices | `https://api.ted.europa.eu/v3/notices/search` (`query: winner-identifier=<org>`) | No auth, no key, officially documented and schema-validated (an invalid field name returns a 400 listing all ~1,830 valid fields) | `https://ted.europa.eu/en/legal-notice` — EU procurement notices are freely reusable for commercial or non-commercial purposes; TED/SIMAP metadata is CC0 | `connectors/procurement.py` |

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

**TED `public_contracts`**: publishes a claim only when the requested organisation number appears
*verbatim* in the notice's structured `winner-identifier` array — never a fuzzy name match. It
deliberately never publishes a winner *name* extracted from TED's `winner-name` field: on
multi-winner framework-agreement notices, `winner-name` and `winner-identifier` are parallel arrays
that are **not guaranteed to be the same length** (observed live: one real notice had 10
identifiers but only 4 names), so there is no reliable way to know which name corresponds to which
identifier — the org-number match alone is the only claim precision allows; the company's own name
already comes from Brønnøysund. Coverage caveat, stated honestly: TED only receives notices above
the EU procurement value thresholds Norwegian buyers must forward to it; Doffin (Norway's national
portal) additionally publishes below-threshold awards TED never sees, but Doffin's only live JSON
API is an undocumented internal endpoint of its own single-page app (reachable with zero auth, but
never publicly documented for third-party use, unlike this endpoint's officially-documented,
schema-validated surface) — not used here on that basis.

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

Investigated for the beast-mode coverage push and rejected, each live-verified rather than assumed:

- **Doffin webclient JSON API** (`api.doffin.no/webclient/api/v2/...`) — live, zero-auth, and does
  carry a structured winner organisation number, but it is the Norwegian procurement portal's own
  internal single-page-app backend, discovered by extracting base URLs from its production JS
  bundle, not a publicly documented integration point. Doffin's *actual* documented developer
  portal (`dof-notices-prod-api.developer.azure-api.net`) requires account signup + a subscription
  key — evidence the intended public access path is the gated one. TED's Search API v3 (above)
  gives the same signal through an officially documented, schema-validated, zero-credential path
  and is used instead.
- **Altinn** (`platform.altinn.no/register`, `/authorization`) — every data endpoint (organisation
  lookup, roles/authorized-signatories) returns `401 missing subscription key`; the only genuinely
  public endpoint is Altinn's own catalog of government digital services, which is not company
  data. Reaching real data requires Maskinporten client registration tied to an organisation
  identity — a business credential, not an anonymous public path.
- **Patentstyret** (trademarks/patents/designs) — a real, NLOD-2.0-licensed, orgnr-capable API
  exists (`developer.patentstyret.no`), but its entire API/product catalog returns empty to an
  unauthenticated caller; every real endpoint requires free account signup + a self-service
  subscription key. Free is not the bar; reproducible-without-a-personal-credential is.
- **Skatteetaten** (tax administration) — all external APIs require Maskinporten business OAuth2
  (confirmed on the agency's own security docs page: "All Skatteetaten external APIs use
  Maskinporten... for machine-to-machine authentication"). The one candidate that would have been a
  genuinely new signal (Restanser — tax-debt/enforcement by orgnr) is behind that same wall; even
  MVA/VAT-registration status is already sourced from Brønnøysund's own bulk field, not duplicated.
- **SSB (Statistics Norway) StatBank API** — live, CC-BY-4.0, no auth, but every business-statistics
  table is dimensioned by municipality × industry × employee-size bucket and returns an aggregate
  count, never an organisation number or any per-entity identifier. Every company sharing a bucket
  gets the identical value — fails exact-entity attribution outright, not a licensing or auth issue.
- **Kartverket / Geonorge** (`ws.geonorge.no`, `www.kartverket.no`) — every host in this domain
  family was unreachable from this environment across five independent methods (curl, raw TCP,
  TLS handshake, an internal fetch proxy, a real browser tab), while a control check to
  `data.brreg.no` and `google.com` succeeded in the same session, isolating the failure to this
  address range specifically. Moot regardless: the one orgnr-bearing dataset found in Kartverket's
  own documentation (business-property-owner records) is separately gated behind an application
  process ("Søknad-API-tilgang"), and the general Adresse/Eiendom APIs carry no organisation-number
  field even by documentation — they are geocoding utilities, not a claim source.
