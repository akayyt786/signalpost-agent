from __future__ import annotations

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .connector_registry import field_families as _connector_field_families
from .evidence import utc_now

# Fixed field-family vocabulary. Used as the coverage join key everywhere (claims, availability,
# the local scorer, and the site's per-company coverage strip). Derived from connector_registry.py
# - adding a connector there extends this automatically. Never remove an entry a shipped connector
# still populates without updating AVAILABILITY_REASONS and every producer/consumer in the same change.
FIELD_FAMILIES: tuple[str, ...] = _connector_field_families()

AvailabilityState = Literal["available", "not_available", "blocked", "not_applicable", "ambiguous", "failed"]


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    source_url: str
    final_url: str | None = None
    source_class: str
    access_policy: str
    http_status: int | None = None
    retrieved_at: str
    content_sha256: str | None = None
    claim_span: str | None = None
    locator: str | None = None


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_id: str
    field: str
    subkey: str | None = None
    value: Any = None
    unit: str | None = None
    reporting_period: dict[str, str] | None = None
    availability: AvailabilityState
    confidence: float = 1.0
    method: str
    evidence_ids: list[str] = Field(default_factory=list)
    first_seen: str
    last_seen: str


class AvailabilityEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: AvailabilityState
    reason: str
    checked_at: str


class RunInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    started_at: str
    completed_at: str | None = None
    terminal_status: Literal["completed", "failed", "partial"] | None = None
    agent_version: str
    code_commit: str


class Operations(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requests: int = 0
    bytes: int = 0
    runtime_ms: int = 0
    third_party_cost_usd: float = 0.0


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_id: str
    field: str
    subkey: str | None = None
    change_type: Literal["new_value", "changed_value", "removed_value", "deferred"]
    old_value: Any = None
    new_value: Any = None
    old_evidence_ids: list[str] = Field(default_factory=list)
    new_evidence_ids: list[str] = Field(default_factory=list)
    reason: str | None = None


class Error(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage: str
    message: str
    field: str | None = None


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    organisation_number: str
    run: RunInfo
    claims: list[Claim] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    availability: dict[str, AvailabilityEntry] = Field(default_factory=dict)
    changes: list[Change] = Field(default_factory=list)
    errors: list[Error] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    operations: Operations = Field(default_factory=Operations)


class EnvelopeBuilder:
    """Accumulates claims/evidence for one company and emits a validated Envelope.

    Every field family starts as `failed` / `not_processed_yet`: a safety net so a bug that skips
    a family is caught immediately (by `unset_families()`) rather than silently reading as
    "checked, nothing there". Every code path that touches a company MUST call `set_availability`
    for each family it is responsible for before `build()` is called.
    """

    def __init__(self, organisation_number: str, *, run_id: str, started_at: str, agent_version: str, code_commit: str) -> None:
        self.organisation_number = organisation_number
        self.run_id = run_id
        self.started_at = started_at
        self.agent_version = agent_version
        self.code_commit = code_commit
        self._claims: list[dict[str, Any]] = []
        self._evidence: list[dict[str, Any]] = []
        self._availability: dict[str, dict[str, Any]] = {
            family: {"state": "failed", "reason": "not_processed_yet", "checked_at": started_at} for family in FIELD_FAMILIES
        }
        self._changes: list[dict[str, Any]] = []
        self._errors: list[dict[str, Any]] = []
        self._summary: dict[str, Any] = {}
        self._operations = {"requests": 0, "bytes": 0, "runtime_ms": 0, "third_party_cost_usd": 0.0}

    def add_evidence(
        self,
        *,
        source_url: str,
        source_class: str,
        access_policy: str,
        retrieved_at: str | None = None,
        final_url: str | None = None,
        http_status: int | None = None,
        content_sha256: str | None = None,
        claim_span: str | None = None,
        locator: str | None = None,
    ) -> str:
        evidence_id = f"ev-{len(self._evidence) + 1}"
        self._evidence.append({
            "id": evidence_id,
            "source_url": source_url,
            "final_url": final_url,
            "source_class": source_class,
            "access_policy": access_policy,
            "http_status": http_status,
            "retrieved_at": retrieved_at or utc_now(),
            "content_sha256": content_sha256,
            "claim_span": claim_span,
            "locator": locator,
        })
        return evidence_id

    def add_claim(
        self,
        *,
        field: str,
        value: Any,
        availability: AvailabilityState,
        method: str,
        evidence_ids: list[str],
        subkey: str | None = None,
        unit: str | None = None,
        reporting_period: dict[str, str] | None = None,
        confidence: float = 1.0,
        seen_at: str | None = None,
    ) -> dict[str, Any]:
        if field not in FIELD_FAMILIES:
            raise ValueError(f"Unknown field family: {field!r}")
        now = seen_at or utc_now()
        claim_key = f"{self.organisation_number}|{field}|{subkey or ''}"
        claim_id = hashlib.sha256(claim_key.encode()).hexdigest()[:16]
        claim = {
            "claim_id": claim_id,
            "field": field,
            "subkey": subkey,
            "value": value,
            "unit": unit,
            "reporting_period": reporting_period,
            "availability": availability,
            "confidence": confidence,
            "method": method,
            "evidence_ids": evidence_ids,
            "first_seen": now,
            "last_seen": now,
        }
        self._claims.append(claim)
        return claim

    def set_availability(self, field: str, state: AvailabilityState, reason: str, *, checked_at: str | None = None) -> None:
        if field not in FIELD_FAMILIES:
            raise ValueError(f"Unknown field family: {field!r}")
        self._availability[field] = {"state": state, "reason": reason, "checked_at": checked_at or utc_now()}

    def add_change(
        self,
        *,
        claim_id: str,
        field: str,
        change_type: Literal["new_value", "changed_value", "removed_value", "deferred"],
        subkey: str | None = None,
        old_value: Any = None,
        new_value: Any = None,
        old_evidence_ids: list[str] | None = None,
        new_evidence_ids: list[str] | None = None,
        reason: str | None = None,
    ) -> None:
        self._changes.append({
            "claim_id": claim_id,
            "field": field,
            "subkey": subkey,
            "change_type": change_type,
            "old_value": old_value,
            "new_value": new_value,
            "old_evidence_ids": old_evidence_ids or [],
            "new_evidence_ids": new_evidence_ids or [],
            "reason": reason,
        })

    def add_error(self, *, stage: str, message: str, field: str | None = None) -> None:
        self._errors.append({"stage": stage, "message": message, "field": field})

    def add_operations(self, *, requests: int = 0, bytes_: int = 0, runtime_ms: int = 0, cost_usd: float = 0.0) -> None:
        self._operations["requests"] += requests
        self._operations["bytes"] += bytes_
        self._operations["runtime_ms"] += runtime_ms
        self._operations["third_party_cost_usd"] += cost_usd

    def set_summary(self, summary: dict[str, Any]) -> None:
        self._summary = summary

    def unset_families(self) -> list[str]:
        """Field families still carrying the not_processed_yet sentinel. Must be empty before build()."""
        return [field for field, entry in self._availability.items() if entry["reason"] == "not_processed_yet"]

    @property
    def claims(self) -> list[dict[str, Any]]:
        """Read-only snapshot of claims accumulated so far, for refresh diffing before build()."""
        return list(self._claims)

    @property
    def availability(self) -> dict[str, dict[str, Any]]:
        """Read-only snapshot of per-family availability accumulated so far."""
        return dict(self._availability)

    @property
    def operations(self) -> dict[str, Any]:
        """Read-only snapshot of accumulated requests/bytes/cost so far, for budget tracking before build()."""
        return dict(self._operations)

    def build(self, *, completed_at: str, terminal_status: Literal["completed", "failed", "partial"]) -> dict[str, Any]:
        envelope = Envelope(
            organisation_number=self.organisation_number,
            run=RunInfo(
                run_id=self.run_id,
                started_at=self.started_at,
                completed_at=completed_at,
                terminal_status=terminal_status,
                agent_version=self.agent_version,
                code_commit=self.code_commit,
            ),
            claims=[Claim(**claim) for claim in self._claims],
            evidence=[Evidence(**item) for item in self._evidence],
            availability={family: AvailabilityEntry(**entry) for family, entry in self._availability.items()},
            changes=[Change(**item) for item in self._changes],
            errors=[Error(**item) for item in self._errors],
            summary=self._summary,
            operations=Operations(**self._operations),
        )
        return envelope.model_dump(mode="json")
