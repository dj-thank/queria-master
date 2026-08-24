from __future__ import annotations

"""A bounded, restartable official-site enrichment worker.

The worker is intentionally one writer: it claims a batch, fetches one page at
a time, and sends importer-ready records through the single-writer importer.
Run several workers only with separate processes that share the same lease
protocol; do not open the DuckDB companion for direct writes elsewhere.
"""

import time
from pathlib import Path
from typing import Any

from .enrichment import (
    DEFAULT_DB,
    DEFAULT_ENRICHMENT_DB,
    EnrichmentError,
    claim_enrichment_tasks,
    complete_enrichment_task,
    import_enrichment_records,
)
from .enrichment_extract import DEFAULT_USER_AGENT, fetch_and_extract_page


def _canonical_urls(database_path: Path, corporate_numbers: list[str]) -> dict[str, str | None]:
    if not corporate_numbers:
        return {}
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover
        raise EnrichmentError("duckdb がありません。") from exc
    if not Path(database_path).is_file():
        raise EnrichmentError(f"canonical DuckDBがありません: {database_path}")
    con = duckdb.connect(str(Path(database_path).resolve()), read_only=True)
    try:
        placeholders = ", ".join("?" for _ in corporate_numbers)
        rows = con.execute(
            f"SELECT corporate_number, company_url FROM core.companies WHERE corporate_number IN ({placeholders})",
            corporate_numbers,
        ).fetchall()
        return {str(number): (str(url).strip() if url else None) for number, url in rows}
    finally:
        con.close()


def run_enrichment_worker(
    database_path: Path = DEFAULT_DB,
    *,
    enrichment_path: Path = DEFAULT_ENRICHMENT_DB,
    worker_id: str,
    field_name: str | None = None,
    source_key: str | None = None,
    batch_size: int = 20,
    max_tasks: int = 100,
    lease_seconds: int = 900,
    timeout: float = 15.0,
    max_bytes: int = 2_000_000,
    interval_seconds: float = 0.25,
    respect_robots: bool = True,
    user_agent: str = DEFAULT_USER_AGENT,
    only_with_url: bool = False,
) -> dict[str, int]:
    """Process at most ``max_tasks`` tasks and return auditable counters."""

    if max_tasks < 1:
        raise EnrichmentError("max_tasksは1以上です。")
    if interval_seconds < 0:
        raise EnrichmentError("interval_secondsは0以上です。")
    counts = {"claimed": 0, "found": 0, "not_found": 0, "blocked": 0, "not_applicable": 0, "failed": 0}
    remaining = max_tasks
    while remaining > 0:
        claimed = claim_enrichment_tasks(
            database_path,
            enrichment_path=enrichment_path,
            worker_id=worker_id,
            field_name=field_name,
            source_key=source_key,
            batch_size=min(batch_size, remaining),
            lease_seconds=lease_seconds,
            require_url=only_with_url,
        )
        if not claimed:
            break
        counts["claimed"] += len(claimed)
        remaining -= len(claimed)
        urls = _canonical_urls(database_path, [str(task["corporate_number"]) for task in claimed])
        for index, task in enumerate(claimed):
            corporate_number = str(task["corporate_number"])
            task_field = str(task["field_name"])
            task_source = str(task["source_key"])
            page_url = urls.get(corporate_number)
            try:
                if task_field == "location":
                    complete_enrichment_task(
                        database_path,
                        enrichment_path=enrichment_path,
                        corporate_number=corporate_number,
                        field_name=task_field,
                        source_key=task_source,
                        state="not_applicable",
                        worker_id=worker_id,
                        error="canonical company address is the authoritative location layer",
                    )
                    counts["not_applicable"] += 1
                    continue
                if not page_url:
                    complete_enrichment_task(
                        database_path,
                        enrichment_path=enrichment_path,
                        corporate_number=corporate_number,
                        field_name=task_field,
                        source_key=task_source,
                        state="not_found_after_policy",
                        worker_id=worker_id,
                        error="canonical company_url is empty; URL discovery adapter is required",
                    )
                    counts["not_found"] += 1
                    continue

                records = fetch_and_extract_page(
                    corporate_number,
                    page_url,
                    user_agent=user_agent,
                    timeout=timeout,
                    max_bytes=max_bytes,
                    respect_robots=respect_robots,
                    source_key=task_source,
                )
                if not records:
                    records = [
                        {
                            "kind": "state",
                            "corporate_number": corporate_number,
                            "field_name": task_field,
                            "source_key": task_source,
                            "source_url": page_url,
                            "state": "not_found_after_policy",
                            "error": "no explicit fact was found",
                            "policy_code": "official_page_no_fact",
                        }
                    ]
                elif not any(
                    (
                        record.get("kind") == "website" and task_field == "website"
                    )
                    or (
                        record.get("kind") == "contact" and record.get("contact_type") == task_field
                    )
                    or (
                        record.get("kind") == "state" and record.get("field_name") == task_field
                    )
                    for record in records
                ):
                    # A successful page fetch is still a completed negative
                    # result for the requested field; keep that distinction.
                    records.append(
                        {
                            "kind": "state",
                            "corporate_number": corporate_number,
                            "field_name": task_field,
                            "source_key": task_source,
                            "source_url": page_url,
                            "state": "not_found_after_policy",
                            "error": "official page contained no explicit value for this field",
                            "policy_code": "official_page_no_fact",
                        }
                    )
                import_enrichment_records(
                    database_path,
                    records,
                    enrichment_path=enrichment_path,
                    batch_size=max(1, len(records)),
                )
                if any(
                    record.get("kind") == "contact" and record.get("contact_type") == task_field
                    or record.get("kind") == "website" and task_field == "website"
                    for record in records
                ):
                    counts["found"] += 1
                elif any(record.get("state") == "blocked_by_policy" for record in records):
                    counts["blocked"] += 1
                else:
                    counts["not_found"] += 1
            except Exception as exc:
                counts["failed"] += 1
                try:
                    complete_enrichment_task(
                        database_path,
                        enrichment_path=enrichment_path,
                        corporate_number=corporate_number,
                        field_name=task_field,
                        source_key=task_source,
                        state="failed",
                        worker_id=worker_id,
                        error=str(exc),
                    )
                except Exception:
                    # Preserve the original failure; an expired lease can be
                    # recovered by the next worker invocation.
                    pass
            finally:
                if interval_seconds and index < len(claimed) - 1:
                    time.sleep(interval_seconds)
    return counts


__all__ = ["run_enrichment_worker"]
