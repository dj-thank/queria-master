from __future__ import annotations

"""Read-only capability and artifact health report for GUI/CLI surfaces."""

from pathlib import Path
from typing import Any

from .app_config import ResolvedArtifacts
from .runtime import RUNTIME_SCHEMA_VERSION, _canonical_source_identity, runtime_summary
from .search_index import SEARCH_INDEX_VERSION, SearchIndex


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _source_identity(path: Path, query: str) -> str | None:
    try:
        import duckdb

        con = duckdb.connect(str(path), read_only=True)
        try:
            catalog = _quote_identifier(str(con.execute("SELECT current_catalog()").fetchone()[0]))
            qualified_query = query
            for schema in ("meta", "enrichment"):
                qualified_query = qualified_query.replace(
                    f"{schema}.", f"{catalog}.{schema}."
                )
            row = con.execute(qualified_query).fetchone()
        finally:
            con.close()
    except Exception:
        return None
    if row is None or row[0] is None:
        return None
    return _canonical_source_identity(row[0])


def inspect_application(artifacts: ResolvedArtifacts) -> dict[str, Any]:
    files = {
        "canonical_database": artifacts.canonical_database,
        "enrichment_database": artifacts.enrichment_database,
        "runtime_database": artifacts.runtime_database,
        "search_index": artifacts.search_index,
    }
    file_report = {
        name: {
            "path": str(path),
            "present": path.is_file(),
            "bytes": path.stat().st_size if path.is_file() else 0,
            "origin": artifacts.origins.get(name, "unknown"),
        }
        for name, path in files.items()
    }
    errors: list[str] = []
    runtime: dict[str, Any] | None = None
    index_metadata: dict[str, str] | None = None
    try:
        runtime = runtime_summary(artifacts.runtime_database)
    except Exception as exc:
        errors.append(f"runtime: {exc}")
    try:
        with SearchIndex(
            artifacts.search_index,
            database_path=artifacts.runtime_database,
            validate_database=artifacts.validate_index,
        ) as index:
            index_metadata = dict(index.metadata)
    except Exception as exc:
        errors.append(f"search_index: {exc}")

    counts = {} if runtime is None else dict(runtime.get("counts") or {})
    runtime_manifest = {} if runtime is None else dict(runtime.get("manifest") or {})
    runtime_schema = str(runtime_manifest.get("schema_version") or "")
    if runtime is not None and runtime_schema != RUNTIME_SCHEMA_VERSION:
        errors.append(
            f"runtime schema_version mismatch: expected={RUNTIME_SCHEMA_VERSION}, actual={runtime_schema or 'missing'}"
        )
    index_schema = "" if index_metadata is None else str(index_metadata.get("index_version") or "")
    if index_metadata is not None and index_schema != SEARCH_INDEX_VERSION:
        errors.append(
            f"search index_version mismatch: expected={SEARCH_INDEX_VERSION}, actual={index_schema or 'missing'}"
        )
    for artifact_name, manifest_key in (
        ("canonical_database", "canonical_bytes"),
        ("enrichment_database", "enrichment_bytes"),
    ):
        expected_bytes = runtime_manifest.get(manifest_key)
        actual = file_report[artifact_name]
        if expected_bytes is not None and (
            not actual["present"] or int(expected_bytes) != int(actual["bytes"])
        ):
            errors.append(f"runtime source mismatch: {artifact_name}")
    expected_refresh_id = str(runtime_manifest.get("canonical_refresh_id") or "")
    if expected_refresh_id:
        current_refresh_id = _source_identity(
            artifacts.canonical_database,
            "SELECT refresh_id FROM meta.refresh_log ORDER BY rowid DESC LIMIT 1",
        )
        if current_refresh_id != expected_refresh_id:
            errors.append("runtime source mismatch: canonical refresh_id")
    expected_enrichment_revision = str(runtime_manifest.get("enrichment_revision") or "")
    if expected_enrichment_revision:
        current_enrichment_revision = _source_identity(
            artifacts.enrichment_database,
            "SELECT initialized_at FROM enrichment.schema_meta "
            "WHERE schema_name = 'enrichment' LIMIT 1",
        )
        if current_enrichment_revision != expected_enrichment_revision:
            errors.append("runtime source mismatch: enrichment revision")
    runtime_generation = str(runtime_manifest.get("generation_id") or "")
    index_generation = "" if index_metadata is None else str(index_metadata.get("runtime_generation_id") or "")
    generation_match = bool(runtime_generation and index_generation and runtime_generation == index_generation)
    if runtime_generation or index_generation:
        if not generation_match:
            errors.append("runtime/index generation_id mismatch")

    search_ready = runtime is not None and index_metadata is not None and not errors
    capabilities = {
        "keyword_search": {
            "enabled": search_ready,
            "reason": "ready" if search_ready else "runtime/index pair is not healthy",
        },
        "canonical_refresh": {
            "enabled": file_report["canonical_database"]["present"],
            "reason": "canonical DB present" if file_report["canonical_database"]["present"] else "canonical DB missing",
        },
        "enrichment_update": {
            "enabled": file_report["canonical_database"]["present"]
            and file_report["enrichment_database"]["present"],
            "reason": "source DBs present"
            if file_report["canonical_database"]["present"] and file_report["enrichment_database"]["present"]
            else "canonical or enrichment DB missing",
        },
        "verified_company_contacts": {
            "enabled": int(counts.get("resolved_contacts", 0) or 0) > 0,
            "reason": f"{int(counts.get('resolved_contacts', 0) or 0):,} allowed resolved contact rows",
        },
        "establishment_contacts": {
            "enabled": int(counts.get("establishments", 0) or 0) > 0,
            "reason": f"{int(counts.get('establishments', 0) or 0):,} scoped establishment rows",
        },
    }
    return {
        "overall_status": "passed" if search_ready else "failed",
        "home": str(artifacts.home),
        "files": file_report,
        "runtime": runtime,
        "search_index_metadata": index_metadata,
        "generation": {
            "runtime": runtime_generation,
            "search_index": index_generation,
            "match": generation_match,
        },
        "capabilities": capabilities,
        "errors": errors,
    }


__all__ = ["inspect_application"]
