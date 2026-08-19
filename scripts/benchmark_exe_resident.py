from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def _request(process: subprocess.Popen[str], request: dict[str, Any]) -> tuple[dict[str, Any], float]:
    assert process.stdin is not None
    assert process.stdout is not None
    started = time.perf_counter()
    process.stdin.write(json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n")
    process.stdin.flush()
    line = process.stdout.readline()
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if not line:
        stderr = process.stderr.read() if process.stderr is not None else ""
        raise RuntimeError(f"resident EXE exited unexpectedly: {stderr}")
    return json.loads(line), elapsed_ms


def _measure(process: subprocess.Popen[str], request: dict[str, Any], runs: int) -> dict[str, Any]:
    for _ in range(2):
        _request(process, request)
    samples: list[float] = []
    server_samples: list[float] = []
    rows = 0
    for _ in range(runs):
        response, elapsed_ms = _request(process, request)
        if not response.get("ok"):
            raise RuntimeError(response.get("error", "resident search failed"))
        samples.append(elapsed_ms)
        server_samples.append(float(response.get("elapsed_ms", elapsed_ms)))
        rows = int(response.get("count", 0))
    return {
        "rows": rows,
        "runs_ms": [round(value, 3) for value in samples],
        "p50_ms": round(statistics.median(samples), 3),
        "p95_ms": round(sorted(samples)[max(0, int(len(samples) * 0.95) - 1)], 3),
        "max_ms": round(max(samples), 3),
        "server_p50_ms": round(statistics.median(server_samples), 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure the resident JSONL EXE search path")
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--search-index", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args()

    command = [
        str(args.exe),
        "--db",
        str(args.db),
        "daemon",
        "--search-index",
        str(args.search_index),
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    started = time.perf_counter()
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=creationflags,
    )
    try:
        ping, ping_ms = _request(process, {"op": "ping"})
        startup_ms = (time.perf_counter() - started) * 1000.0
        if not ping.get("ok"):
            raise RuntimeError(ping.get("error", "resident ping failed"))
        cases = {}
        for rows in (10, 100, 1000):
            cases[str(rows)] = _measure(
                process,
                {"op": "search", "keyword": "ソフトウェア", "limit": rows},
                args.runs,
            )
        _request(process, {"op": "shutdown"})
        process.wait(timeout=10)
        print(
            json.dumps(
                {
                    "exe": str(args.exe.resolve()),
                    "startup_ms": round(startup_ms, 3),
                    "ping_ms": round(ping_ms, 3),
                    "cases": cases,
                    "exit_code": process.returncode,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if process.returncode == 0 else process.returncode
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
