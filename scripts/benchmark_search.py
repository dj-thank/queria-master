from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from queria_master.search_index import DEFAULT_SEARCH_INDEX, SearchIndex


DEFAULT_DATABASE = ROOT / "data" / "queria_runtime.duckdb"


CASES: tuple[tuple[str, dict[str, Any]], ...] = (
    (
        "keyword_software_fast",
        {"keyword": "ソフトウェア", "limit": 1000, "fast": True},
    ),
    (
        "keyword_common_fast",
        {"keyword": "株式会社", "limit": 100, "fast": True},
    ),
    (
        "tokyo_major_g_fast",
        {"prefecture": "東京都", "industry_majors": ("G",), "limit": 1000, "fast": True},
    ),
    (
        "tokyo_middle_39_fast",
        {"prefecture": "東京都", "industry_middles": ("39",), "limit": 1000, "fast": True},
    ),
    (
        "tokyo_fast",
        {"prefecture": "東京都", "limit": 1000, "fast": True},
    ),
    (
        "keyword_software_stable_order",
        {"keyword": "ソフトウェア", "limit": 1000, "fast": False},
    ),
)


def _measure(index: SearchIndex, params: dict[str, Any], warmups: int, runs: int) -> dict[str, Any]:
    for _ in range(warmups):
        index.search(**params)
    durations: list[float] = []
    row_count = 0
    for _ in range(runs):
        started = time.perf_counter()
        rows = index.search(**params)
        durations.append((time.perf_counter() - started) * 1000.0)
        row_count = len(rows)
    return {
        "rows": row_count,
        "runs_ms": [round(value, 3) for value in durations],
        "min_ms": round(min(durations), 3),
        "p50_ms": round(statistics.median(durations), 3),
        "max_ms": round(max(durations), 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="全量SQLite/FTS検索索引の再現可能な性能計測")
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--search-index", type=Path, default=DEFAULT_SEARCH_INDEX)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--json", action="store_true", help="JSONのみを出力")
    args = parser.parse_args()

    with SearchIndex(args.search_index, database_path=args.db) as index:
        result = {
            "database": str(args.db.resolve()),
            "search_index": str(args.search_index.resolve()),
            "search_index_bytes": args.search_index.stat().st_size,
            "index_metadata": index.metadata,
            "warmups": args.warmups,
            "runs": args.runs,
            "cases": {
                name: _measure(index, params, args.warmups, args.runs)
                for name, params in CASES
            },
        }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"search_index_bytes={result['search_index_bytes']}")
        for name, measurement in result["cases"].items():
            print(
                f"{name}: rows={measurement['rows']} p50={measurement['p50_ms']:.3f}ms "
                f"min={measurement['min_ms']:.3f}ms max={measurement['max_ms']:.3f}ms"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
