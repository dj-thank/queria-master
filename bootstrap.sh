#!/usr/bin/env bash
set -euo pipefail

SCOPE="${1:-all-public}"
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [[ ! -x .venv/bin/python ]]; then
  echo "[1/5] Python 仮想環境を作成します"
  python3 -m venv .venv
fi

echo "[2/5] Queria CLI と DuckDB をインストール／更新します"
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install --upgrade -r requirements.txt

export QUERIA_NO_TELEMETRY=1
if [[ -f data/queria_master.duckdb && -d cache/all-public-latest ]]; then
  echo "[3/5] 同梱済みの全量データを使用します（再ダウンロードしません）"
else
  echo "[3/5] Queria 公開データを抽出し、DuckDB を構築します"
  .venv/bin/python -m queria_master refresh --scope "$SCOPE"
fi

if .venv/bin/python -m queria_master app-health >/dev/null 2>&1; then
  echo "[4/5] 同一generationのruntime/indexを使用します"
else
  echo "[4/5] enrichment、runtime、検索索引を構築します"
  .venv/bin/python -m queria_master init-enrichment
  .venv/bin/python -m queria_master publish-runtime
fi

echo "[5/5] ローカル DB を検証します"
.venv/bin/python -m queria_master doctor

echo "完成: data/queria_runtime.duckdb + data/search.sqlite"
