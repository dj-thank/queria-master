#!/usr/bin/env bash
set -euo pipefail
SCOPE="${1:-all-public}"
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
if [[ ! -x .venv/bin/python ]]; then
  echo "先に ./bootstrap.sh を実行してください。" >&2
  exit 1
fi
export QUERIA_NO_TELEMETRY=1
.venv/bin/python -m queria_master refresh --scope "$SCOPE"
.venv/bin/python -m queria_master init-enrichment
.venv/bin/python -m queria_master publish-runtime
