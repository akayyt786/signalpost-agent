from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .envelope import FIELD_FAMILIES

SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
    claim_key TEXT PRIMARY KEY,
    organisation_number TEXT NOT NULL,
    field TEXT NOT NULL,
    subkey TEXT,
    value_json TEXT NOT NULL,
    availability TEXT NOT NULL,
    evidence_id TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claims_org ON claims(organisation_number);

CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    organisation_number TEXT NOT NULL,
    source_url TEXT,
    final_url TEXT,
    retrieved_at TEXT,
    content_sha256 TEXT,
    claim_span TEXT,
    http_status INTEGER
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT,
    completed_at TEXT,
    input_rows INTEGER,
    requests INTEGER,
    cost_usd REAL
);
"""

# A stale claim in one of these states never produces removed_value; the source did not give a
# clean answer this run, so the previous value is carried forward and reported `deferred`.
FAILURE_STATES = {"failed", "blocked"}


def open_store(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def _claim_key(org: str, field: str, subkey: str | None) -> str:
    return f"{org}|{field}|{subkey or ''}"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def load_previous_claims(conn: sqlite3.Connection, org: str) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "SELECT claim_key, field, subkey, value_json, availability, evidence_id, first_seen, last_seen "
        "FROM claims WHERE organisation_number = ?",
        (org,),
    ).fetchall()
    return {
        row[0]: {
            "field": row[1], "subkey": row[2], "value": json.loads(row[3]), "availability": row[4],
            "evidence_id": row[5], "first_seen": row[6], "last_seen": row[7],
        }
        for row in rows
    }


def diff_and_store(
    conn: sqlite3.Connection,
    org: str,
    current_claims: list[dict[str, Any]],
    availability: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Diff `current_claims` (this run's claims for `org`, from EnvelopeBuilder.claims before
    build()) against the stored state, upsert the store, and return
    `{"baseline": "cold_start" | "incremental", "changes": [...]}`.

    Cold start (no prior state for this org) inserts a baseline and reports zero changes - a first
    sighting is not a change. `families_failed` (derived from `availability`, any family whose
    state is `failed`/`blocked`) is carried forward as `deferred`, never `removed_value`: a source
    that did not answer cleanly this run must never look like a fact that disappeared. Every other
    family is diffed for real: a claim present before and absent now is a genuine `removed_value`.
    """
    previous = load_previous_claims(conn, org)
    cold_start = not previous
    families_failed = {family for family, entry in availability.items() if entry.get("state") in FAILURE_STATES}
    current_by_key = {_claim_key(org, claim["field"], claim.get("subkey")): claim for claim in current_claims}
    changes: list[dict[str, Any]] = []
    written_keys: set[str] = set()

    for key, claim in current_by_key.items():
        prev = previous.get(key)
        value_json = _canonical(claim["value"])
        evidence_id = (claim.get("evidence_ids") or [None])[0]
        if prev is None:
            conn.execute(
                "INSERT INTO claims (claim_key, organisation_number, field, subkey, value_json, availability, evidence_id, first_seen, last_seen) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (key, org, claim["field"], claim.get("subkey"), value_json, claim["availability"], evidence_id, claim["first_seen"], claim["last_seen"]),
            )
            if not cold_start:
                changes.append({
                    "claim_id": claim["claim_id"], "field": claim["field"], "subkey": claim.get("subkey"),
                    "change_type": "new_value", "old_value": None, "new_value": claim["value"],
                    "old_evidence_ids": [], "new_evidence_ids": claim.get("evidence_ids") or [],
                })
        else:
            if _canonical(prev["value"]) != value_json:
                conn.execute(
                    "UPDATE claims SET value_json=?, availability=?, evidence_id=?, last_seen=? WHERE claim_key=?",
                    (value_json, claim["availability"], evidence_id, claim["last_seen"], key),
                )
                changes.append({
                    "claim_id": claim["claim_id"], "field": claim["field"], "subkey": claim.get("subkey"),
                    "change_type": "changed_value", "old_value": prev["value"], "new_value": claim["value"],
                    "old_evidence_ids": [prev["evidence_id"]] if prev["evidence_id"] else [], "new_evidence_ids": claim.get("evidence_ids") or [],
                })
            else:
                conn.execute("UPDATE claims SET last_seen=? WHERE claim_key=?", (claim["last_seen"], key))
        written_keys.add(key)

    for key, prev in previous.items():
        if key in written_keys:
            continue
        if prev["field"] in families_failed:
            changes.append({
                "claim_id": None, "field": prev["field"], "subkey": prev.get("subkey"), "change_type": "deferred",
                "old_value": prev["value"], "new_value": prev["value"], "old_evidence_ids": [prev["evidence_id"]] if prev["evidence_id"] else [],
                "new_evidence_ids": [], "reason": "source_failed_this_run",
            })
            continue
        if prev["field"] not in FIELD_FAMILIES:
            continue  # defensive: ignore rows from a since-removed field family
        conn.execute("DELETE FROM claims WHERE claim_key=?", (key,))
        changes.append({
            "claim_id": None, "field": prev["field"], "subkey": prev.get("subkey"), "change_type": "removed_value",
            "old_value": prev["value"], "new_value": None, "old_evidence_ids": [prev["evidence_id"]] if prev["evidence_id"] else [], "new_evidence_ids": [],
        })

    conn.commit()
    return {"baseline": "cold_start" if cold_start else "incremental", "changes": changes}


def record_run(conn: sqlite3.Connection, *, run_id: str, started_at: str, completed_at: str, input_rows: int, requests: int, cost_usd: float) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO runs (run_id, started_at, completed_at, input_rows, requests, cost_usd) VALUES (?,?,?,?,?,?)",
        (run_id, started_at, completed_at, input_rows, requests, cost_usd),
    )
    conn.commit()
