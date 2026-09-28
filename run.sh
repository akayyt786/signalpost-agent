#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
uv sync --frozen
exec uv run python scripts/run_signalpost.py --input "${1:?batch file required}" --out "${2:-out}" "${@:3}"
