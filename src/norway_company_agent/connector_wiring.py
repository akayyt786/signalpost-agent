"""Binds connector_registry.py's declarative specs to their real populate() implementations. Heavy
imports (requests, BeautifulSoup, etc., via the connector modules) live here rather than in
connector_registry.py so that module stays safely importable from envelope.py.

Every "simple" connector (uniform (builder, org, entity, reference_pack) -> None shape, no bespoke
budget-degradation logic of its own) is bound here. "Complex" connectors (foundation, website,
jobs - each with its own multi-level internal degradation) are still called directly by name in
run_signalpost.py; they don't need a wrapper because there is exactly one of each and their call
sites are already the natural place to express their bespoke behavior.

Add a source: write its populate(builder, org, entity, reference_pack) function, add a ConnectorSpec
to connector_registry.CONNECTORS with simple=True, bind it in SIMPLE_CONNECTOR_IMPLS below. That's
the whole integration - run_signalpost.py's simple-connector loop and the budget degrade ladder
both pick it up automatically.
"""
from __future__ import annotations

from typing import Any, Callable

from .connector_registry import ConnectorSpec, simple_connectors_by_tier
from .connectors.registries import populate_registries
from .envelope import EnvelopeBuilder
from .sourcepacks import ReferencePack

SimplePopulate = Callable[[EnvelopeBuilder, str, dict[str, Any], ReferencePack], None]

# registries.py's populate_registries() predates this registry and has the (builder, org,
# reference_pack) shape without `entity` - wrapped rather than changed, zero risk to its existing
# tests. New connectors should accept entity directly instead of needing a wrapper.
SIMPLE_CONNECTOR_IMPLS: dict[str, SimplePopulate] = {
    "registries": lambda builder, org, entity, reference_pack: populate_registries(builder, org, reference_pack),
}


def simple_connectors_in_drop_order() -> tuple[tuple[ConnectorSpec, SimplePopulate], ...]:
    """(spec, implementation) pairs, lowest-tier-first (dropped first under budget pressure)."""
    specs = simple_connectors_by_tier()
    missing = [spec.name for spec in specs if spec.name not in SIMPLE_CONNECTOR_IMPLS]
    if missing:
        raise RuntimeError(f"connector_registry declares simple connectors with no bound implementation: {missing}")
    return tuple((spec, SIMPLE_CONNECTOR_IMPLS[spec.name]) for spec in specs)
