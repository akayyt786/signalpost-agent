"""Declarative connector metadata: which field families a connector populates, and where it sits
in the budget-pressure drop order. Deliberately zero heavy imports (no requests/BeautifulSoup, no
importing the connector implementation modules) so this can sit *below* envelope.py in the import
graph - envelope.py needs FIELD_FAMILIES before any connector implementation is even importable
(EnvelopeBuilder.__init__ seeds every family with a not_processed_yet safety-net sentinel), and the
connector implementations import EnvelopeBuilder for type hints. Putting the real populate()
functions here would create envelope.py -> connector_registry.py -> foundation.py -> envelope.py.

The actual populate() callables are bound to these specs in connector_wiring.py (heavy imports
live there instead), imported only by the batch driver and anything that needs to actually run a
connector - tests, build_site.py and score_local.py only ever need the metadata below.

Adding a new source: write its populate() function, add one ConnectorSpec here, add one binding
line in connector_wiring.py. Nothing else changes - FIELD_FAMILIES, the coverage barcode on the
verification site, and (for "simple" connectors) the budget degrade ladder all derive from this
list automatically.
"""
from __future__ import annotations

from dataclasses import dataclass

# Budget-pressure tier for "simple" connectors only (see connector_wiring.SIMPLE_CONNECTOR_NAMES):
# the lowest tier is dropped first as Budget.pressure_level() rises, matching the same 1-5 scale
# and thresholds as the legacy connectors' own internal graduated degradation (run_signalpost.py).
# foundation/website/jobs are "complex" connectors with bespoke multi-level internal degradation
# (e.g. website drops name-guessing before subpages before itself) and are wired directly in
# run_signalpost.py rather than through the generic simple-connector loop; their tier here is
# documentation only (foundation is effectively tier 0 / never dropped short of full exhaustion).
TIER_NEVER_DROP = 0
TIER_LAST_RESORT = 5


@dataclass(frozen=True)
class ConnectorSpec:
    name: str
    field_families: tuple[str, ...]
    tier: int
    description: str
    simple: bool  # True: uniform (builder, org, entity, reference_pack) shape, driven generically.
    # False: bespoke signature/degradation, wired by hand in run_signalpost.py (foundation/website/jobs).


CONNECTORS: tuple[ConnectorSpec, ...] = (
    ConnectorSpec(
        "foundation",
        ("legal_identity", "registered_address", "industry", "employees", "status_flags",
         "annual_accounts", "roles", "group_structure", "locations"),
        tier=TIER_NEVER_DROP, simple=False,
        description="Brønnøysund bulk registry + live re-check for changed entities. Identity gate for every other connector.",
    ),
    ConnectorSpec(
        "website",
        ("official_website", "site_description", "social_profiles", "contact_points", "public_activity"),
        tier=TIER_LAST_RESORT, simple=False,
        description="Website identity gate (verified/corroborated/conflict/ambiguous). Own internal graduated degradation before being dropped entirely.",
    ),
    ConnectorSpec(
        "jobs",
        ("job_postings",),
        tier=3, simple=False,
        description="NAV job-ad feed, re-fetched live per ad. Own internal degradation (detail re-fetch cap) before being dropped.",
    ),
    ConnectorSpec(
        "registries",
        ("credentials_and_approvals", "external_references"),
        tier=TIER_NEVER_DROP, simple=True,
        description="DIBK central-approval register + Wikidata external references. Already-fetched data, zero new requests - never worth degrading for budget.",
    ),
)


def field_families() -> tuple[str, ...]:
    """All field families across every registered connector, in connector-declaration order with
    families within a connector kept in that connector's own order. This is the single source of
    truth for envelope.py::FIELD_FAMILIES - adding a connector here extends the schema everywhere
    that imports it (the envelope model, the site's coverage barcode, the local scorer)."""
    seen: dict[str, None] = {}
    for spec in CONNECTORS:
        for family in spec.field_families:
            seen[family] = None
    return tuple(seen)


def simple_connectors_by_tier() -> tuple[ConnectorSpec, ...]:
    """Simple connectors ordered lowest-tier-first (the order they get dropped under rising budget pressure)."""
    return tuple(sorted((spec for spec in CONNECTORS if spec.simple), key=lambda spec: spec.tier))
