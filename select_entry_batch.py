#!/usr/bin/env python3
"""Select a reproducible entry batch from the public Signalpost universe.

Default mode picks one random --count-sized batch (used for local smoke tests). --splits mode
partitions the universe into non-overlapping named sets (e.g. development/validation/final) with
no shared organisation number *and* no shared registered website host between sets - two
companies on the same domain (e.g. a group and its subsidiary) always land in the same set, so a
strategy cannot be tuned on one company and silently validated on its sibling.
"""
from __future__ import annotations

import argparse
import gzip
import json
import random
from pathlib import Path
from typing import Any


def _load_universe(path: Path) -> list[dict[str, Any]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _registered_host(website: str) -> str | None:
    website = (website or "").strip().lower()
    if not website:
        return None
    website = website.removeprefix("https://").removeprefix("http://").removeprefix("www.")
    host = website.split("/")[0].split(":")[0]
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else (host or None)


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def _parse_splits(spec: str) -> list[tuple[str, int]]:
    splits = []
    for part in spec.split(","):
        name, _, count = part.partition(":")
        splits.append((name.strip(), int(count)))
    return splits


def write_splits(rows: list[dict[str, Any]], splits: list[tuple[str, int]], *, seed: int, output_prefix: Path) -> dict[str, Any]:
    rng = random.Random(seed)
    shuffled = rows[:]
    rng.shuffle(shuffled)

    groups: dict[str, list[dict[str, Any]]] = {}
    for row in shuffled:
        host = _registered_host(row.get("website")) or f"__singleton__{row['organisation_number']}"
        groups.setdefault(host, []).append(row)
    group_list = list(groups.values())  # preserves shuffled order via first-seen insertion

    assignment: dict[str, list[dict[str, Any]]] = {name: [] for name, _ in splits}
    targets = dict(splits)
    cursor = 0
    for name, _target in splits:
        while cursor < len(group_list) and len(assignment[name]) < targets[name]:
            assignment[name].extend(group_list[cursor])
            cursor += 1

    manifest = {"seed": seed, "splits": {}}
    for name, target in splits:
        chosen = assignment[name][:target] if len(assignment[name]) > target else assignment[name]
        _write(Path(f"{output_prefix}-{name}.jsonl"), chosen)
        manifest["splits"][name] = {"requested": target, "written": len(chosen)}
    all_orgs = [row["organisation_number"] for rows_ in assignment.values() for row in rows_]
    manifest["no_organisation_overlap"] = len(all_orgs) == len(set(all_orgs))
    all_hosts = [
        _registered_host(row.get("website")) for rows_ in assignment.values() for row in rows_ if row.get("website")
    ]
    host_to_splits: dict[str, set[str]] = {}
    for name, rows_ in assignment.items():
        for row in rows_:
            host = _registered_host(row.get("website"))
            if host:
                host_to_splits.setdefault(host, set()).add(name)
    manifest["no_host_overlap"] = all(len(split_names) == 1 for split_names in host_to_splits.values())
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", required=True, help="Public .jsonl or .jsonl.gz universe")
    parser.add_argument("--output", help="Output JSONL manifest (single-batch mode)")
    parser.add_argument("--count", type=int, default=100, help="Number of companies for a local test batch")
    parser.add_argument("--seed", type=int, default=20260823)
    parser.add_argument("--splits", help="Non-overlapping named splits, e.g. development:600,validation:200,final:200")
    parser.add_argument("--output-prefix", help="Prefix for split output files (required with --splits)")
    parser.add_argument("--manifest", help="Where to write the split manifest JSON (with --splits)")
    args = parser.parse_args()

    rows = _load_universe(Path(args.universe))

    if args.splits:
        if not args.output_prefix:
            raise SystemExit("--output-prefix is required with --splits")
        splits = _parse_splits(args.splits)
        if sum(count for _, count in splits) > len(rows):
            raise SystemExit(f"Requested {sum(c for _, c in splits)} total; universe contains {len(rows)}")
        manifest = write_splits(rows, splits, seed=args.seed, output_prefix=Path(args.output_prefix))
        if args.manifest:
            Path(args.manifest).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(json.dumps(manifest, indent=2))
        return

    if not args.output:
        raise SystemExit("--output is required in single-batch mode")
    if args.count < 1:
        raise SystemExit("The local test batch must contain at least one company")
    if args.count > len(rows):
        raise SystemExit(f"Requested {args.count}; universe contains {len(rows)}")
    chosen = random.Random(args.seed).sample(rows, args.count)
    _write(Path(args.output), chosen)
    print(f"Wrote {len(chosen):,} companies to {args.output}")


if __name__ == "__main__":
    main()
