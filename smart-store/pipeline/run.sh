#!/usr/bin/env bash
# One-command pipeline runner.
#
# Usage:
#   ./pipeline/run.sh                              # both stores, default API
#   ./pipeline/run.sh STORE_BLR_001                # one store
#   ./pipeline/run.sh STORE_BLR_001 ./data/Store\ 1
#
# Override the API target with PIPELINE_API_BASE.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

run_one() {
    local store="$1"
    local clip_dir="$2"
    echo "[run.sh] store=${store} clip_dir=${clip_dir}"
    python -m pipeline.run --store "$store" --clip-dir "$clip_dir"
}

if [[ $# -ge 2 ]]; then
    run_one "$1" "$2"
elif [[ $# -eq 1 ]]; then
    case "$1" in
        STORE_BLR_001) run_one "$1" "data/Store 1" ;;
        STORE_BLR_002) run_one "$1" "data/Store 2" ;;
        *) echo "Unknown store id: $1" >&2; exit 2 ;;
    esac
else
    [[ -d "data/Store 1" ]] && run_one "STORE_BLR_001" "data/Store 1"
    [[ -d "data/Store 2" ]] && run_one "STORE_BLR_002" "data/Store 2"
fi
