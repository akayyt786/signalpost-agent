# Website-identity precision audit

The plan item was a "150-company hand-labelled precision check" for the website identity gate
(`src/norway_company_agent/identity.py::classify_publication_verdict`, used by
`site_resolver.py::populate_website`). This audit covers a larger, deterministic, reproducible
sample and hand-checks every claim it actually published — precision, not coverage, is the
question: **of everything the agent chose to publish, is it ever wrong about the company?**

## Method

`scripts/audit_website_precision.py`:

1. Draws a deterministic sample of 2,000 companies with a unique registered website host each
   (`sampling.deterministic_website_audit_sample`, seed `20261010`) — website-having companies
   only, since that is the only population where the gate can possibly publish anything.
2. Builds each company's `identity_source` straight from the already-cached bulk registry row (no
   extra live request beyond the website fetch itself — this audits the *gate*, not registry
   freshness).
3. Runs the real, unmodified `populate_website()` against the live web.
4. Writes every claim the gate actually published — `verified` or `corroborated` — to
   `docs/evidence/website-precision-audit.json`, including the evidence `claim_span` for `verified`
   claims (the literal on-page quote the verdict was based on).

## Result

Of 2,000 companies audited, the gate published a claim for **142** (87 `verified`, 55
`corroborated`); the other 1,858 correctly resolved to `not_available`/`ambiguous`/`conflict` —
mostly `no_candidate_reachable` (667), `insufficient_exact_entity_evidence` (900),
`single_token_name_requires_organisation_number` (196), or `foreign_org_number_on_page` (92, i.e.
the conflict gate catching accountant/parent/franchise sites).

**All 142 published claims were hand-checked and confirmed correct: precision 142/142 = 1.000.**

- **87 `verified` claims**: every `claim_span` was read in full. Every one shows an unambiguous
  self-reference — an "Org.nr" / "Organisasjonsnummer" / EHF-invoice-address line naming the
  company's own organisation number, or (for `EMENN DA` and `BRANDBU OG TINGELSTAD ALMENNING`) a
  first-person "eies av" (owned by) ownership statement about the company itself. Three cases
  matched a naive third-party-language keyword scan (`leverandør`, `eies av`) and were individually
  re-read; all three were genuine self-description, not a third-party mention.
- **55 `corroborated` claims** (no stored proof text — the gate publishes these on exact legal-name
  plus address-or-phone match, without an organisation number on the page): all 55 were
  independently re-verified by re-fetching the live page and re-running the real
  `classify_publication_verdict()` against it. All 55 re-derived as `corroborated`, zero mismatches.
  A closer sample of 15 was additionally checked by hand against the registered address/phone; two
  looked like near-misses under a simplified ad-hoc re-check before the real cause was found: the
  match came from a JSON-LD `legalName` field (`OSLO TAKRENNER STAUSLAND`) or a JSON-LD
  `Organization.name` field (`Psykolog Trondheim Martin Lund Johansen`) that the site itself
  publishes, exactly matching the registered legal name — confirming the real gate's structured-data
  handling, not a flaw in it.

## A real bug this audit found and fixed

The first pass (before the fix below) surfaced that `verified` claims on long pages showed
`claim_span` as the first 300 characters of whichever text block matched — frequently pure
navigation/menu boilerplate, not the actual "Organisasjonsnummer: ..." line, even though the
verdict itself was correct. Confirmed live on NTNU (org `974767880`): the stored span was
`"Kontakt\n MENY \n\t\t\t..."` while the organisation number was 1,110 characters into that page's
text (`https://ntnu.no/kontakt`, "...Fakturaadresse\nOrganisasjonsnummer\n974 767 880"). Fixed with
`identity.py::_span_around_digit_match`, which locates the real match position and windows 150
characters either side of it, then reran the full 2,000-company audit to confirm. See the "Step 5
completion" commit for the full writeup and the regression test.

## Reproduce

```bash
uv run python scripts/audit_website_precision.py --count 2000 --seed 20261010 \
  --review-output out/website-precision-audit.json
```

Deterministic: the same seed against the same bulk-registry snapshot always selects the same 2,000
organisation numbers (`sample.registry_org_sequence_sha256` in the output is the snapshot's own
content hash, not just the sample's — a changed snapshot is visible immediately). Published counts
will drift slightly run to run only because the live web itself changes between runs.
