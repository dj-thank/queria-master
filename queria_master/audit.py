from __future__ import annotations

"""Read-only integrity, coverage and search-latency audit for a release."""

import json
import statistics
import time
from pathlib import Path
from typing import Any

from .resources import DEFAULT_DB, PROJECT_ROOT
from .search_index import DEFAULT_SEARCH_INDEX, SearchIndex, SearchIndexError
from .runtime import DEFAULT_RUNTIME_DB


DEFAULT_AUDIT_OUTPUT = PROJECT_ROOT.parent / "outputs" / "QUALITY_AUDIT.json"


class AuditError(RuntimeError):
    """Audit could not be completed."""


def _duckdb():
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover
        raise AuditError("duckdb がありません。") from exc
    return duckdb


def _count(con: Any, sql: str) -> int:
    return int(con.execute(sql).fetchone()[0])


def _canonical_report(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise AuditError(f"canonical DB がありません: {path}")
    con = _duckdb().connect(str(path), read_only=True)
    try:
        companies = _count(con, "SELECT count(*) FROM core.companies")
        duplicate_rows = _count(
            con,
            "SELECT count(*) - count(DISTINCT corporate_number) FROM core.companies",
        )
        refresh = con.execute(
            """
            SELECT refresh_id, scope, row_count, completed_at, parquet_sha256
            FROM meta.refresh_log
            ORDER BY completed_at DESC NULLS LAST
            LIMIT 1
            """
        ).fetchone()
        coverage = {}
        for column, key in (
            ("full_address", "full_address"),
            ("company_url", "company_url"),
            ("business_summary", "business_summary"),
            ("representative_name", "representative_name"),
            ("employee_number", "employee_number"),
            ("capital_stock", "capital_stock"),
            ("jsic_codes_all_raw", "jsic_codes"),
        ):
            coverage[key] = _count(
                con,
                f"SELECT count(*) FROM core.companies WHERE {column} IS NOT NULL AND trim(CAST({column} AS VARCHAR)) <> ''",
            )
        source_registry = con.execute(
            """
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE license_name IS NULL OR trim(license_name) = '') AS missing_license,
                   count(*) FILTER (WHERE attribution IS NULL OR trim(attribution) = '') AS missing_attribution
            FROM meta.source_registry
            """
        ).fetchone()
        boundary_rows = con.execute(
            "SELECT status, count(*) FROM meta.coverage_boundary GROUP BY status ORDER BY status"
        ).fetchall()
        return {
            "path": str(path),
            "bytes": path.stat().st_size,
            "company_count": companies,
            "duplicate_corporate_number_rows": duplicate_rows,
            "coverage_counts": coverage,
            "latest_refresh": None if refresh is None else dict(
                zip(("refresh_id", "scope", "row_count", "completed_at", "parquet_sha256"), refresh)
            ),
            "source_registry": None if source_registry is None else dict(
                zip(("total", "missing_license", "missing_attribution"), source_registry)
            ),
            "coverage_boundary": [{"status": row[0], "count": int(row[1])} for row in boundary_rows],
        }
    finally:
        con.close()


def _optional_db_counts(path: Path, tables: tuple[tuple[str, str], ...]) -> dict[str, Any]:
    if not path.is_file():
        return {"path": str(path), "present": False, "counts": {}}
    con = _duckdb().connect(str(path), read_only=True)
    try:
        counts: dict[str, int] = {}
        for label, table in tables:
            counts[label] = _count(con, f"SELECT count(*) FROM {table}")
        return {"path": str(path), "present": True, "bytes": path.stat().st_size, "counts": counts}
    finally:
        con.close()


def _search_report(path: Path, canonical_path: Path, expected_rows: int) -> dict[str, Any]:
    if not path.is_file():
        return {"path": str(path), "present": False, "status": "missing"}
    try:
        with SearchIndex(path, database_path=canonical_path) as index:
            timings: list[float] = []
            for _ in range(3):
                started = time.perf_counter()
                index.search("株式会社", fast=True, limit=10)
                timings.append((time.perf_counter() - started) * 1000)
            row_count = index.row_count
            status = "passed" if row_count == expected_rows else "failed"
            return {
                "path": str(path),
                "present": True,
                "status": status,
                "row_count": row_count,
                "expected_company_count": expected_rows,
                "refresh_id": index.metadata.get("refresh_id", ""),
                "runtime_generation_id": index.metadata.get("runtime_generation_id", ""),
                "keyword_search_ms": {
                    "median": round(statistics.median(timings), 3),
                    "min": round(min(timings), 3),
                    "max": round(max(timings), 3),
                    "samples": [round(value, 3) for value in timings],
                },
            }
    except SearchIndexError as exc:
        return {"path": str(path), "present": True, "status": "stale_or_invalid", "error": str(exc)}


def _runtime_report(path: Path, expected_rows: int) -> dict[str, Any]:
    if not path.is_file():
        return {"path": str(path), "present": False, "status": "missing"}
    try:
        con = _duckdb().connect(str(path), read_only=True)
        try:
            companies = _count(con, "SELECT count(*) FROM core.companies")
            profiles = _count(con, "SELECT count(*) FROM search.company_documents")
            state_rows = _count(con, "SELECT count(*) FROM enrichment.enrichment_state")
            manifest = con.execute(
                "SELECT manifest_json FROM meta.runtime_manifest ORDER BY built_at DESC LIMIT 1"
            ).fetchone()
            status = "passed" if companies == expected_rows and profiles == expected_rows else "failed"
            return {
                "path": str(path),
                "present": True,
                "status": status,
                "company_count": companies,
                "search_profile_count": profiles,
                "enrichment_state_count": state_rows,
                "manifest": None if manifest is None else json.loads(str(manifest[0])),
            }
        finally:
            con.close()
    except Exception as exc:
        return {"path": str(path), "present": True, "status": "invalid", "error": str(exc)}


def audit_database(
    database_path: Path = DEFAULT_DB,
    *,
    search_index_path: Path = DEFAULT_SEARCH_INDEX,
    enrichment_path: Path | None = None,
    runtime_path: Path | None = DEFAULT_RUNTIME_DB,
) -> dict[str, Any]:
    """Return a JSON-serialisable audit report without mutating any DB."""

    database_path = Path(database_path).resolve()
    canonical = _canonical_report(database_path)
    expected_rows = int(canonical["company_count"])
    runtime_database_path = None if runtime_path is None else Path(runtime_path).resolve()
    # The release search index is built from the integrated runtime database,
    # not from the canonical refresh input.  Validate the same artifact pair
    # used by search/daemon; fall back to canonical only when runtime auditing
    # is explicitly disabled.
    search_database_path = runtime_database_path or database_path
    search = _search_report(Path(search_index_path).resolve(), search_database_path, expected_rows)
    enrichment = None
    if enrichment_path is not None:
        enrichment = _optional_db_counts(
            Path(enrichment_path).resolve(),
            (
                ("state_rows", "enrichment.enrichment_state"),
                ("evidence_documents", "enrichment.evidence_documents"),
                ("websites", "enrichment.company_websites"),
                ("contact_points", "enrichment.company_contact_points"),
                ("establishments", "enrichment.company_establishments"),
                ("locations", "enrichment.company_locations"),
                ("suppressions", "compliance.suppressions"),
            ),
        )
    runtime = None if runtime_database_path is None else _runtime_report(runtime_database_path, expected_rows)
    gates = {
        "canonical_has_companies": expected_rows > 0,
        "corporate_numbers_unique": canonical["duplicate_corporate_number_rows"] == 0,
        "search_index_present_and_fresh": search.get("status") == "passed",
        "runtime_present_and_aligned": runtime is None or runtime.get("status") == "passed",
    }
    return {
        "audit_version": "1",
        "audited_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "canonical": canonical,
        "search_index": search,
        "enrichment": enrichment,
        "runtime": runtime,
        "gates": gates,
        "overall_status": "passed" if all(gates.values()) else "failed",
    }


__all__ = ["AuditError", "DEFAULT_AUDIT_OUTPUT", "audit_database"]
