from __future__ import annotations

"""Read-only capability and artifact health report for GUI/CLI surfaces."""

from pathlib import Path
from typing import Any

from .app_config import ResolvedArtifacts
from .runtime import runtime_summary
from .search_index import SearchIndex


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
            "enabled": int(counts.get("contact_points", 0) or 0) > 0,
            "reason": f"{int(counts.get('contact_points', 0) or 0):,} contact rows",
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
