from __future__ import annotations

import csv
import gzip
import io
import json
from pathlib import Path
from typing import Any

ORG_NUMBER_KEYS = ("organisation_number", "organisasjonsnummer", "orgnr", "org_number", "organizationNumber")
_WEIGHTS = (3, 2, 7, 6, 5, 4, 3, 2)


def normalize_org_digits(value: Any) -> str:
    return "".join(character for character in str(value or "") if character.isdigit())


def valid_check_digit(digits: str) -> bool:
    """Norwegian organisation-number mod-11 check digit (see plan glossary: last digit is a
    mod-11 check digit). A remainder of 10 has no valid check digit and is always rejected."""
    if len(digits) != 9:
        return False
    total = sum(int(digit) * weight for digit, weight in zip(digits[:8], _WEIGHTS))
    remainder = total % 11
    expected = 0 if remainder == 0 else 11 - remainder
    if expected == 10:
        return False
    return expected == int(digits[8])


def _extract_org_value(item: Any) -> Any:
    if isinstance(item, dict):
        for key in ORG_NUMBER_KEYS:
            if item.get(key) not in (None, ""):
                return item[key]
        return None
    return item


def _read_text(path: Path) -> tuple[str, str]:
    """Return (text, logical_suffix). `.jsonl.gz` / `.txt.gz` / `.json.gz` are transparently decompressed."""
    if path.suffix.lower() == ".gz":
        logical_suffix = Path(path.stem).suffix.lower()
        with gzip.open(path, "rt", encoding="utf-8-sig") as handle:
            return handle.read(), logical_suffix
    return path.read_text(encoding="utf-8-sig"), path.suffix.lower()


def read_batch(path: str | Path) -> list[dict[str, Any]]:
    """Read a batch of organisation numbers from .txt, .jsonl, .json or .csv (optionally .gz).

    Returns one row per input line/item, **in input order, with duplicates preserved** (a
    duplicated organisation number is resolved once by the caller and reused, never re-fetched,
    but every input row still gets its own terminal envelope). Never raises on a single malformed
    row: an unparseable or check-digit-invalid row is returned with `valid: False` so the caller
    can still emit a `failed` envelope for it instead of dropping the row or crashing the batch.
    """
    source = Path(path)
    text, suffix = _read_text(source)
    items: list[Any]
    if suffix == ".json":
        body = json.loads(text)
        items = body if isinstance(body, list) else body.get("organisation_numbers", [])
    elif suffix == ".jsonl":
        items = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                items.append(line.strip())
    elif suffix == ".csv":
        items = list(csv.DictReader(io.StringIO(text)))
    else:
        items = [line.strip() for line in text.splitlines() if line.strip()]

    rows: list[dict[str, Any]] = []
    for item in items:
        raw_value = _extract_org_value(item)
        digits = normalize_org_digits(raw_value)
        if len(digits) != 9:
            rows.append({"raw": item, "organisation_number": None, "valid": False, "error": f"expected 9 digits, got {len(digits)} from {raw_value!r}"})
        elif not valid_check_digit(digits):
            rows.append({"raw": item, "organisation_number": digits, "valid": False, "error": "invalid mod-11 check digit"})
        else:
            rows.append({"raw": item, "organisation_number": digits, "valid": True, "error": None})
    return rows
