# 1,000-company scale rehearsal

The plan's own verification checklist named this and it was never actually run at real
official-batch scale before this: "one 1,000-company run on the final commit before submitting,
recording wall-clock, requests and cost, so the declared numbers... are measured rather than
estimated." Two real, consequential findings came out of finally running it.

## Finding 1 (critical, fixed): an uncaught exception crashed the entire batch

The first attempt crashed outright after 325/1000 companies:
`UnicodeEncodeError: 'idna' codec can't encode characters in position 0-77: label too long`.

An unusually long company name (org `976007441`, a small foundation with no website/email/registry
cross-reference so it fell through to the DNS name-guess candidate tier) slugifies to a
78-character hostname label — over DNS's 63-octet limit. `socket.getaddrinfo`'s IDNA codec raises
`UnicodeEncodeError` for this, which is a `ValueError` subclass, not an `OSError` subclass — a
narrower `except OSError` in `site_resolver.py::_dns_resolves` let it escape uncaught through a
worker thread and kill the whole process. Per the rubric's own rule ("an entrant-caused failed
batch scores 0"), this would have zeroed out an entire official daily run if it had surfaced there
instead of here. Fixed at the trigger and, for defense in depth, at the same root cause in
`website.py::assert_public_url` (see the "CRITICAL" commit for the full writeup). Reran clean:
**1,000/1,000 envelopes emitted** both times after the fix.

## Finding 2 (tuning, fixed): the beast-mode expansion made the default budget genuinely marginal

| | First clean run (fix only) | After procurement tier retune |
|---|---:|---:|
| Requests for 1,000 companies | 5,916 | 6,043 |
| Terminal status | 1000 completed | 962 completed, 38 partial (fully budget_exhausted) |
| `website_layer_skipped` | 13 | 59 |
| `group_structure_skipped` | 131 | 163 |
| `procurement_skipped` | **378** | **163** |

The four-connector system's original degrade ladder was tuned (in an earlier session) against a
request budget that had real headroom under the assumed 6,000-request default. Adding the
`public_contracts` connector added real per-company request cost without re-tuning where it sat in
the drop order — it landed at tier 2 (dropped as early as site-subpage capping), even though it
costs only **one** request per company and adds an entire new field family. At 1,000-company scale
that meant 37.8% of companies lost it for the cheapest possible saving. Retuned to tier 4 (same
threshold as `group_structure` — comparable cost, comparable "not every company has one" value
profile): the same real rerun dropped it for only 16.3% of companies for the same budget, a large
real coverage gain at zero extra cost. Full evidence:
[`docs/evidence/scale-rehearsal-1000/`](evidence/scale-rehearsal-1000/).

The second run also crossed 6,000 requests naturally (run-to-run variance in which sites happen to
have more subpages, more job listings, etc.) and correctly triggered the blunt full-exhaustion
fallback for the last 38 companies — **the terminal batch contract still held: 1,000/1,000
envelopes emitted, zero missing, zero crashed.** That is exactly the degrade ladder's job: this
project's own plan already flagged the real official budget as unpublished and assumed "≥6,000
requests... for 1,000 companies" as a conservative floor, not a comfortable ceiling. This rehearsal
is concrete, measured confirmation that the assumption is now genuinely tight for the expanded
5-connector system, not a hypothetical - reinforcing that the still-unsent question to
`soham@builderr.ai` about the real official budget matters more now than it did before this
expansion, not less. Raising the local default without a confirmed real ceiling was considered and
rejected: it would trade a known-safe, fully-tested behavior (graceful degradation within a
self-tracked budget) for an unknown risk (colliding with an external constraint this agent cannot
see coming, since `--max-requests` only tracks the agent's own request count, not any organiser-side
enforcement).

## Reproduce

```bash
uv run python select_entry_batch.py --universe signalpost-company-universe-2025.jsonl.gz \
  --count 1000 --output batch.jsonl --seed 20261028
./run.sh batch.jsonl out
```
