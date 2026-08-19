from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Iterable, TextIO

from .search_index import DEFAULT_SEARCH_INDEX, SEARCH_RESULT_COLUMNS, SearchIndex
from .resources import DEFAULT_DB


class ResidentSearchSession:
    """Keep one immutable SQLite FTS connection alive for interactive work."""

    def __init__(
        self,
        *,
        database_path: Path = DEFAULT_DB,
        search_index: Path = DEFAULT_SEARCH_INDEX,
        validate_database: bool = True,
    ) -> None:
        self.database_path = Path(database_path).resolve()
        self.search_index_path = Path(search_index).resolve()
        self.index = SearchIndex(
            self.search_index_path,
            database_path=self.database_path,
            validate_database=validate_database,
            check_same_thread=False,
        )

    @property
    def metadata(self) -> dict[str, str]:
        return self.index.metadata

    def search(self, request: dict[str, Any]) -> list[dict[str, Any]]:
        industry_majors = _as_strings(request.get("industry_majors"))
        industry_middles = _as_strings(request.get("industry_middles"))
        limit = int(request.get("limit", 1000))
        return self.index.search(
            request.get("keyword"),
            prefecture=request.get("prefecture"),
            city=request.get("city"),
            industry_majors=industry_majors,
            industry_middles=industry_middles,
            min_employees=_as_int(request.get("min_employees")),
            max_employees=_as_int(request.get("max_employees")),
            min_capital=_as_int(request.get("min_capital")),
            max_capital=_as_int(request.get("max_capital")),
            has_web=bool(request.get("has_web", False)),
            limit=limit,
            fast=True,
        )

    def close(self) -> None:
        self.index.close()


def _as_strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


def _as_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _compact_response(rows: list[dict[str, Any]], elapsed_ms: float) -> dict[str, Any]:
    """Return columns once and rows as arrays to reduce resident IPC overhead."""
    values = [[row.get(column) for column in SEARCH_RESULT_COLUMNS] for row in rows]
    return {
        "ok": True,
        "columns": list(SEARCH_RESULT_COLUMNS),
        "rows": values,
        "count": len(values),
        "elapsed_ms": round(elapsed_ms, 3),
    }


def run_jsonl_protocol(
    *,
    database_path: Path = DEFAULT_DB,
    search_index: Path = DEFAULT_SEARCH_INDEX,
    input_stream: TextIO = sys.stdin,
    output_stream: TextIO = sys.stdout,
    validate_database: bool = True,
) -> int:
    """Serve newline-delimited JSON without reopening the database per query."""
    session = ResidentSearchSession(
        database_path=database_path,
        search_index=search_index,
        validate_database=validate_database,
    )
    try:
        for line in input_stream:
            if not line.strip():
                continue
            started = time.perf_counter()
            try:
                request = json.loads(line)
                if request.get("op", "search") == "ping":
                    response: dict[str, Any] = {
                        "ok": True,
                        "op": "pong",
                        "metadata": session.metadata,
                    }
                elif request.get("op", "search") == "shutdown":
                    response = {"ok": True, "op": "shutdown"}
                    output_stream.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
                    output_stream.flush()
                    return 0
                else:
                    rows = session.search(request)
                    response = _compact_response(rows, (time.perf_counter() - started) * 1000.0)
            except Exception as exc:  # protocol boundary: keep the resident process alive
                response = {"ok": False, "error": str(exc)}
            output_stream.write(json.dumps(response, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")
            output_stream.flush()
    finally:
        session.close()
    return 0


__all__ = ["ResidentSearchSession", "run_jsonl_protocol"]
