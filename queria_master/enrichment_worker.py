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
    _local_sql,
    claim_enrichment_tasks,
    complete_enrichment_task,
    import_enrichment_records,
)
from .enrichment_extract import DEFAULT_USER_AGENT, fetch_and_extract_page


def _verified_urls(enrichment_path: Path, corporate_numbers: list[str]) -> dict[str, str | None]:
    if not corporate_numbers:
        return {}
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover
        raise EnrichmentError("duckdb がありません。") from exc
    if not Path(enrichment_path).is_file():
        raise EnrichmentError(f"enrichment DuckDBがありません: {enrichment_path}")
    con = duckdb.connect(str(Path(enrichment_path).resolve()), read_only=True)
    try:
        placeholders = ", ".join("?" for _ in corporate_numbers)
        rows = con.execute(
            _local_sql(
                con,
            f"""
            SELECT corporate_number, normalized_url
            FROM enrichment.company_websites
            WHERE corporate_number IN ({placeholders})
              AND website_role = 'official_homepage'
              AND status = 'verified'
            QUALIFY row_number() OVER (
                PARTITION BY corporate_number
                ORDER BY confidence DESC NULLS LAST, checked_at DESC NULLS LAST,
                         first_seen_at DESC, normalized_url
            ) = 1
            """,
            ),
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
    """Extract all contact fields once from each verified official site.

    Web-search discovery and official-site verification are intentionally not
    performed here.  They feed ``official_candidate`` and verified
    ``official_homepage`` records through their own adapters.
    """

    if max_tasks < 1:
        raise EnrichmentError("max_tasksは1以上です。")
    if interval_seconds < 0:
        raise EnrichmentError("interval_secondsは0以上です。")
    if field_name not in (None, "contact_extraction"):
        raise EnrichmentError(
            "collect-enrichmentはcontact_extraction専用です。"
            " Web検索発見と公式サイト検証は別コマンドを使用してください。"
        )
    counts = {"claimed": 0, "found": 0, "not_found": 0, "blocked": 0, "not_applicable": 0, "failed": 0}
    remaining = max_tasks
    while remaining > 0:
        claimed = claim_enrichment_tasks(
            database_path,
            enrichment_path=enrichment_path,
            worker_id=worker_id,
            field_name="contact_extraction",
            source_key=source_key,
            batch_size=min(batch_size, remaining),
            lease_seconds=lease_seconds,
            require_url=only_with_url,
        )
        if not claimed:
            break
        counts["claimed"] += len(claimed)
        remaining -= len(claimed)
        urls = _verified_urls(enrichment_path, [str(task["corporate_number"]) for task in claimed])
        for index, task in enumerate(claimed):
            corporate_number = str(task["corporate_number"])
            task_field = str(task["field_name"])
            task_source = str(task["source_key"])
            lease_token = str(task["lease_token"])
            page_url = urls.get(corporate_number)
            try:
                if not page_url:
                    complete_enrichment_task(
                        database_path,
                        enrichment_path=enrichment_path,
                        corporate_number=corporate_number,
                        field_name=task_field,
                        source_key=task_source,
                        state="waiting_for_dependency",
                        worker_id=worker_id,
                        lease_token=lease_token,
                        error="verified official_homepage is required before extraction",
                    )
                    counts["failed"] += 1
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
                fetch_state = next(
                    (record for record in records if record.get("kind") == "state"),
                    None,
                )
                if fetch_state is not None and not any(record.get("kind") == "website" for record in records):
                    terminal = str(fetch_state.get("state") or "needs_review")
                    fetch_state["field_name"] = "contact_extraction"
                    fetch_state["source_key"] = task_source
                    fetch_state["lease_owner"] = worker_id
                    fetch_state["lease_token"] = lease_token
                    import_enrichment_records(
                        database_path,
                        records,
                        enrichment_path=enrichment_path,
                        batch_size=max(1, len(records)),
                    )
                    if terminal == "blocked_by_policy":
                        counts["blocked"] += 1
                    else:
                        counts["failed"] += 1
                    continue

                # Preserve the already-verified official-homepage status.  The
                # HTML parser only establishes that a page was fetched; it is
                # not an identity verifier and must not downgrade the fact.
                for record in records:
                    if record.get("kind") == "website" and record.get("website_role") == "official_homepage":
                        record["status"] = "verified"
                        record["discovery_method"] = "verified_site_extract"

                contact_types = {
                    str(record.get("contact_type"))
                    for record in records
                    if record.get("kind") == "contact"
                }
                for contact_type in ("phone", "email", "form_url"):
                    if contact_type not in contact_types:
                        records.append(
                            {
                                "kind": "state",
                                "corporate_number": corporate_number,
                                "field_name": contact_type,
                                "source_key": task_source,
                                "source_url": page_url,
                                "state": "not_found_after_policy",
                                "error": "verified page contained no explicit value for this field",
                                "policy_code": "official_page_no_fact",
                            }
                        )
                records.append(
                    {
                        "kind": "state",
                        "corporate_number": corporate_number,
                        "field_name": "contact_extraction",
                        "source_key": task_source,
                        "source_url": page_url,
                        "state": "found" if contact_types else "not_found_after_policy",
                        "error": None if contact_types else "no explicit contact fact was found",
                        "policy_code": "official_page_extracted",
                        "lease_owner": worker_id,
                        "lease_token": lease_token,
                    }
                )
                import_enrichment_records(
                    database_path,
                    records,
                    enrichment_path=enrichment_path,
                    batch_size=max(1, len(records)),
                    _allow_verified_websites=True,
                )
                if contact_types:
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
                        lease_token=lease_token,
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
