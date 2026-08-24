from __future__ import annotations

"""Bridge the desktop public-data SQLite spool into canonical enrichment.

The SQLite database remains a staging artifact.  Search, CLI, CSV, and Tauri
continue to read the evidence DuckDB -> runtime -> generation-matched index
path only.
"""

import json
import math
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlsplit

from .enrichment import DEFAULT_ENRICHMENT_DB, EnrichmentError, import_enrichment_records
from .publish import publish_runtime_bundle
from .resources import DEFAULT_DB
from .runtime import DEFAULT_RUNTIME_DB
from .search_index import DEFAULT_SEARCH_INDEX
from .website_discovery import validate_public_website_url


REQUIRED_COLUMNS = {
    "corporate_matches": {"source_id", "corporate_number", "status", "confidence", "source_name", "matched_at"},
    "public_master": {
        "corporate_number",
        "website_url",
        "address",
        "postal_code",
        "source_org",
        "source_file",
        "acquired_at",
        "updated_at",
    },
    "site_contacts": {
        "source_id",
        "corporate_number",
        "website_url",
        "phone",
        "evidence_url",
        "evidence_text",
        "confidence",
        "fetched_at",
        "source_file",
    },
    "source_audit": {"source_file", "sha256"},
}
MAX_STAGING_BYTES = 8 * 1024**3
MAX_ACCEPTED_ROWS_PER_TABLE = 1_000_000
MAX_STAGING_PAYLOAD_CHARS = 256 * 1024**2
FIELD_LIMITS = {
    "corporate_matches": {
        "source_id": 1_024,
        "corporate_number": 64,
        "status": 64,
        "source_name": 2_048,
        "matched_at": 128,
    },
    "public_master": {
        "corporate_number": 64,
        "website_url": 8_192,
        "address": 32_768,
        "postal_code": 128,
        "source_org": 2_048,
        "source_file": 1_024,
        "acquired_at": 128,
        "updated_at": 128,
    },
    "site_contacts": {
        "source_id": 1_024,
        "corporate_number": 64,
        "website_url": 8_192,
        "phone": 256,
        "evidence_url": 8_192,
        "evidence_text": 100_000,
        "fetched_at": 128,
        "source_file": 1_024,
    },
    "source_audit": {"source_file": 1_024, "sha256": 128},
}


class PublicEnrichmentBridgeError(EnrichmentError):
    """The staging database is incompatible or could not be published."""


def _file_set_bytes(path: Path) -> int:
    total = 0
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            total += candidate.stat().st_size
        except FileNotFoundError:
            continue
    return total


def _validate_staging_bounds(con: sqlite3.Connection) -> None:
    payload_chars = 0
    for table, columns in FIELD_LIMITS.items():
        total_expression = " + ".join(
            f"length(coalesce(CAST(\"{column}\" AS TEXT), ''))" for column in columns
        )
        max_expressions = ", ".join(
            f"coalesce(max(length(CAST(\"{column}\" AS TEXT))), 0)" for column in columns
        )
        row = con.execute(
            f'SELECT coalesce(sum({total_expression}), 0), {max_expressions} FROM "{table}"'
        ).fetchone()
        if row is None:
            continue
        payload_chars += int(row[0] or 0)
        for index, (column, limit) in enumerate(columns.items(), 1):
            actual = int(row[index] or 0)
            if actual > limit:
                raise PublicEnrichmentBridgeError(
                    f"staging DBの{table}.{column}が長さ上限を超えています: {actual} > {limit}"
                )
        if payload_chars > MAX_STAGING_PAYLOAD_CHARS:
            raise PublicEnrichmentBridgeError(
                "staging DBの使用対象TEXT payloadが上限を超えています: "
                f"{payload_chars} > {MAX_STAGING_PAYLOAD_CHARS}"
            )
    for table in ("corporate_matches", "site_contacts"):
        invalid = int(
            con.execute(
                f"""
                SELECT count(*) FROM "{table}"
                WHERE confidence IS NULL
                   OR typeof(confidence) NOT IN ('integer', 'real')
                   OR confidence < 0 OR confidence > 1
                   OR confidence != confidence
                """
            ).fetchone()[0]
        )
        if invalid:
            raise PublicEnrichmentBridgeError(
                f"staging DBの{table}.confidenceに0〜1の有限数でない値があります: {invalid}件"
            )


def _required_timestamp(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise PublicEnrichmentBridgeError(f"staging DBの{field_name}が空です。")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PublicEnrichmentBridgeError(
            f"staging DBの{field_name}がISO-8601ではありません: {text}"
        ) from exc
    if parsed.tzinfo is None:
        raise PublicEnrichmentBridgeError(
            f"staging DBの{field_name}にはtimezoneが必要です: {text}"
        )
    return text


def _confidence(value: Any, field_name: str) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError) as exc:
        raise PublicEnrichmentBridgeError(f"staging DBの{field_name}が数値ではありません。") from exc
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise PublicEnrichmentBridgeError(f"staging DBの{field_name}は0〜1の有限数が必要です。")
    return confidence


def _same_site_host(website_url: Any, evidence_url: Any) -> bool:
    def host(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            parsed = urlsplit(text if "://" in text else f"https://{text}")
            return (parsed.hostname or "").encode("idna").decode("ascii").casefold().removeprefix("www.")
        except (UnicodeError, ValueError):
            return ""

    website_host = host(website_url)
    return bool(website_host and website_host == host(evidence_url))


def _open_staging(path: Path) -> sqlite3.Connection:
    path = Path(path).resolve()
    if not path.is_file():
        raise PublicEnrichmentBridgeError(f"公開情報staging DBがありません: {path}")
    staging_bytes = _file_set_bytes(path)
    if staging_bytes > MAX_STAGING_BYTES:
        raise PublicEnrichmentBridgeError(
            f"公開情報staging DB+WALが上限を超えています: {staging_bytes} > {MAX_STAGING_BYTES}"
        )
    encoded_path = quote(path.as_posix(), safe="/:")
    con = sqlite3.connect(f"file:{encoded_path}?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=5000")
        con.execute("PRAGMA query_only=ON")
        con.execute("PRAGMA trusted_schema=OFF")
        con.execute("BEGIN")
        for table, required in REQUIRED_COLUMNS.items():
            actual = {str(row[1]) for row in con.execute(f'PRAGMA table_info("{table}")')}
            missing = required - actual
            if missing:
                raise PublicEnrichmentBridgeError(
                    f"staging DBの{table}に必要な列がありません: {sorted(missing)}"
                )
        _validate_staging_bounds(con)
        return con
    except Exception:
        con.close()
        raise


def _known_corporate_numbers(database_path: Path, values: Iterable[str]) -> set[str]:
    numbers = sorted({str(value) for value in values if value})
    if not numbers:
        return set()
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover
        raise PublicEnrichmentBridgeError("duckdb がありません。") from exc
    con = duckdb.connect(str(Path(database_path).resolve()), read_only=True)
    known: set[str] = set()
    try:
        for start in range(0, len(numbers), 1_000):
            chunk = numbers[start : start + 1_000]
            placeholders = ",".join("?" for _ in chunk)
            known.update(
                str(row[0])
                for row in con.execute(
                    f"SELECT corporate_number FROM core.companies WHERE corporate_number IN ({placeholders})",
                    chunk,
                ).fetchall()
            )
    finally:
        con.close()
    return known


def staging_records(staging_path: Path, database_path: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Convert accepted staging rows while retaining their provenance."""

    con = _open_staging(staging_path)
    try:
        audit_columns = {
            str(row[1]) for row in con.execute('PRAGMA table_info("source_audit")')
        }
        audit_time_sql = "imported_at" if "imported_at" in audit_columns else "NULL"
        source_audits: dict[str, tuple[str, str | None]] = {}
        for row in con.execute(
            f"SELECT source_file, sha256, {audit_time_sql} AS imported_at "
            "FROM source_audit ORDER BY rowid"
        ).fetchall():
            source_file = str(row["source_file"] or "").strip()
            source_sha256 = str(row["sha256"] or "").strip().casefold()
            if source_file:
                # public_master/site_contacts contain the latest upserted row;
                # only the latest audit row for the same source filename can
                # describe it in the v1 staging schema. A malformed latest row
                # invalidates the earlier hash instead of silently falling back.
                if re.fullmatch(r"[0-9a-f]{64}", source_sha256):
                    source_audits[source_file] = (
                        source_sha256,
                        str(row["imported_at"] or "").strip() or None,
                    )
                else:
                    source_audits.pop(source_file, None)
        public_rows = con.execute(
            """
            WITH ranked_matches AS (
                SELECT *, row_number() OVER (
                    PARTITION BY corporate_number
                    ORDER BY confidence DESC, matched_at DESC, source_name, source_id
                ) AS match_rank
                FROM corporate_matches
                WHERE status = 'accepted' AND corporate_number <> ''
            )
            SELECT DISTINCT
                p.corporate_number, p.website_url, p.address, p.postal_code,
                p.source_org, p.source_file, p.acquired_at, p.updated_at,
                m.confidence AS match_confidence, m.source_name AS match_source,
                m.matched_at
            FROM public_master p
            JOIN ranked_matches m USING (corporate_number)
            WHERE m.match_rank = 1
            ORDER BY p.corporate_number, p.source_file, p.website_url, p.address
            LIMIT ?
            """,
            [MAX_ACCEPTED_ROWS_PER_TABLE + 1],
        ).fetchall()
        contact_rows = con.execute(
            """
            WITH ranked_matches AS (
                SELECT *, row_number() OVER (
                    PARTITION BY source_id, corporate_number
                    ORDER BY confidence DESC, matched_at DESC, source_name
                ) AS match_rank
                FROM corporate_matches
                WHERE status = 'accepted' AND corporate_number <> ''
            )
            SELECT DISTINCT
                s.corporate_number, s.website_url, s.phone, s.evidence_url,
                s.evidence_text, s.confidence, s.fetched_at, s.source_file,
                m.confidence AS match_confidence, m.source_name AS match_source,
                m.matched_at
            FROM site_contacts s
            JOIN ranked_matches m
              ON m.source_id = s.source_id
             AND m.corporate_number = s.corporate_number
             AND m.match_rank = 1
            ORDER BY s.corporate_number, s.source_id, s.source_file
            LIMIT ?
            """,
            [MAX_ACCEPTED_ROWS_PER_TABLE + 1],
        ).fetchall()
    finally:
        con.close()

    if len(public_rows) > MAX_ACCEPTED_ROWS_PER_TABLE or len(contact_rows) > MAX_ACCEPTED_ROWS_PER_TABLE:
        raise PublicEnrichmentBridgeError(
            f"accepted staging rowsが上限{MAX_ACCEPTED_ROWS_PER_TABLE:,}件/表を超えています。"
        )
    referenced_files = {
        str(row["source_file"] or "").strip() for row in [*public_rows, *contact_rows]
    }
    missing_hashes = sorted(source for source in referenced_files if source not in source_audits)
    if missing_hashes:
        raise PublicEnrichmentBridgeError(
            f"source_audit SHA-256がないstaging sourceです（先頭）: {missing_hashes[:5]}"
        )

    all_numbers = [str(row["corporate_number"]) for row in [*public_rows, *contact_rows]]
    known = _known_corporate_numbers(database_path, all_numbers)
    records: list[dict[str, Any]] = []
    candidates: set[tuple[str, str]] = set()
    verified_sites: set[tuple[str, str]] = set()
    rejected_contact_rows = 0
    verified_contact_rows = 0
    for row in public_rows:
        corporate_number = str(row["corporate_number"])
        if corporate_number not in known:
            continue
        source_file = str(row["source_file"] or "").strip()
        source_sha256, audit_imported_at = source_audits[source_file]
        observed_at = _required_timestamp(
            row["updated_at"] or row["acquired_at"] or audit_imported_at,
            "public_master.updated_at/acquired_at/source_audit.imported_at",
        )
        match_confidence = _confidence(
            row["match_confidence"], "corporate_matches.confidence"
        )
        matched_at = _required_timestamp(
            row["matched_at"], "corporate_matches.matched_at"
        )
        provenance = {
            "source_org": row["source_org"],
            "source_file": source_file,
            "match_source": row["match_source"],
            "match_confidence": match_confidence,
            "matched_at": matched_at,
            "source_sha256": source_sha256,
        }
        if row["website_url"]:
            key = (corporate_number, str(row["website_url"]))
            if key not in candidates:
                candidates.add(key)
                records.append(
                    {
                        "kind": "website",
                        "corporate_number": corporate_number,
                        "url": row["website_url"],
                        "website_role": "official_candidate",
                        "discovery_method": "accepted_public_master",
                        "status": "needs_review",
                        "confidence": match_confidence,
                        "source_key": "public_enrichment_staging",
                        "source_url": row["website_url"],
                        "retrieved_at": observed_at,
                        "policy_status": "review_required",
                        "evidence_status": "candidate",
                        "notes": json.dumps(provenance, ensure_ascii=False, separators=(",", ":")),
                    }
                )
        if row["address"]:
            source_locator = row["website_url"] or (
                "staging://public-enrichment/" + quote(source_file, safe="")
            )
            records.append(
                {
                    "kind": "location",
                    "corporate_number": corporate_number,
                    "address_raw": row["address"],
                    "postal_code": row["postal_code"],
                    "location_type": "head_office",
                    "status": "found",
                    "confidence": match_confidence,
                    "source_key": "public_enrichment_staging",
                    "source_url": source_locator,
                    "retrieved_at": observed_at,
                    "policy_status": "allowed",
                    "evidence_status": "found",
                    "notes": json.dumps(provenance, ensure_ascii=False, separators=(",", ":")),
                }
            )

    for row in contact_rows:
        corporate_number = str(row["corporate_number"])
        if corporate_number not in known:
            continue
        source_file = str(row["source_file"] or "").strip()
        source_sha256, _audit_imported_at = source_audits[source_file]
        fetched_at = _required_timestamp(
            row["fetched_at"], "site_contacts.fetched_at"
        )
        contact_confidence = _confidence(
            row["confidence"], "site_contacts.confidence"
        )
        match_confidence = _confidence(
            row["match_confidence"], "corporate_matches.confidence"
        )
        matched_at = _required_timestamp(
            row["matched_at"], "corporate_matches.matched_at"
        )
        if not _same_site_host(row["website_url"], row["evidence_url"]):
            rejected_contact_rows += 1
            continue
        try:
            verified_url = validate_public_website_url(row["website_url"])
            evidence_url = validate_public_website_url(row["evidence_url"])
        except EnrichmentError:
            rejected_contact_rows += 1
            continue
        if not _same_site_host(verified_url, evidence_url):
            rejected_contact_rows += 1
            continue
        verified_contact_rows += 1
        provenance = {
            "source_file": source_file,
            "match_source": row["match_source"],
            "match_confidence": match_confidence,
            "matched_at": matched_at,
            "evidence_text": row["evidence_text"],
            "source_sha256": source_sha256,
        }
        if row["website_url"]:
            key = (corporate_number, verified_url)
            if key not in verified_sites:
                verified_sites.add(key)
                records.append(
                    {
                        "kind": "website",
                        "corporate_number": corporate_number,
                        "url": verified_url,
                        "website_role": "official_homepage",
                        "discovery_method": "accepted_match_same_host_evidence",
                        "status": "verified",
                        "confidence": contact_confidence,
                        "source_key": "public_enrichment_staging",
                        "source_url": evidence_url,
                        "retrieved_at": fetched_at,
                        "policy_status": "allowed",
                        "evidence_status": "verified",
                        "notes": json.dumps(provenance, ensure_ascii=False, separators=(",", ":")),
                    }
                )
        if row["phone"]:
            records.append(
                {
                    "kind": "contact",
                    "corporate_number": corporate_number,
                    "contact_type": "phone",
                    "value": row["phone"],
                    "scope": "company",
                    "publicness": "public_page",
                    "status": "verified",
                    "confidence": contact_confidence,
                    "verification_status": "accepted_match_same_official_host",
                    "sales_eligibility": "review",
                    "source_key": "public_enrichment_staging",
                    "source_url": evidence_url,
                    "retrieved_at": fetched_at,
                    "policy_status": "allowed",
                    "evidence_status": "verified",
                    "notes": json.dumps(provenance, ensure_ascii=False, separators=(",", ":")),
                }
            )
    return records, {
        "accepted_public_rows": len(public_rows),
        "accepted_contact_rows": len(contact_rows),
        "verified_contact_rows": verified_contact_rows,
        "rejected_contact_rows": rejected_contact_rows,
        "known_corporate_numbers": len(known),
        "skipped_unknown_corporate_numbers": len(set(all_numbers) - known),
    }


def integrate_public_enrichment(
    staging_path: Path,
    database_path: Path = DEFAULT_DB,
    *,
    enrichment_path: Path = DEFAULT_ENRICHMENT_DB,
    runtime_path: Path = DEFAULT_RUNTIME_DB,
    search_index_path: Path = DEFAULT_SEARCH_INDEX,
    publish: bool = True,
    threads: int = 4,
    memory_limit: str = "8GB",
) -> dict[str, Any]:
    records, staging_stats = staging_records(staging_path, database_path)
    imported = import_enrichment_records(
        database_path,
        records,
        enrichment_path=enrichment_path,
        batch_size=max(1, min(1_000, len(records))),
        _allow_verified_websites=True,
    )
    result: dict[str, Any] = {"staging": staging_stats, "records": len(records), "imported": imported}
    if publish:
        result["published"] = publish_runtime_bundle(
            database_path,
            enrichment_path=enrichment_path,
            runtime_path=runtime_path,
            search_index_path=search_index_path,
            threads=threads,
            memory_limit=memory_limit,
        )
    else:
        result["published"] = None
    return result


__all__ = [
    "PublicEnrichmentBridgeError",
    "integrate_public_enrichment",
    "staging_records",
]
