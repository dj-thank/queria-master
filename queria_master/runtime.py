from __future__ import annotations

"""Build the single-file, read-mostly runtime database used by the app.

The canonical database and the evidence database remain the update sources.
This module materialises both into one DuckDB file so interactive reads do not
need an ATTACH or a cross-database join.  The output is built beside the final
file and atomically promoted only after the local schemas, views, indexes and
row-count checks pass.
"""

import json
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .enrichment import DEFAULT_ENRICHMENT_DB, SCHEMA_SQL, initialize_enrichment_schema
from .resources import DEFAULT_DB, PROJECT_ROOT


DEFAULT_RUNTIME_DB = PROJECT_ROOT / "data" / "queria_runtime.duckdb"
RUNTIME_SCHEMA_VERSION = "2"


class RuntimeBuildError(RuntimeError):
    """Runtime database could not be built or verified."""


def _duckdb():
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - project runtime dependency.
        raise RuntimeBuildError("duckdb がありません。セットアップを先に実行してください。") from exc
    return duckdb


def _quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _sql_string(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _local_table_names(con: Any, database_name: str) -> list[tuple[str, str]]:
    rows = con.execute(
        f"""
        SELECT schema_name, table_name
        FROM duckdb_tables()
        WHERE database_name = ?
          AND NOT internal
          AND NOT temporary
          AND schema_name NOT IN ('information_schema', 'pg_catalog', 'main')
        ORDER BY schema_name, table_name
        """,
        [database_name],
    ).fetchall()
    return [(str(schema), str(table)) for schema, table in rows]


def _copy_base_tables(con: Any, source_database: str) -> list[dict[str, Any]]:
    copied: list[dict[str, Any]] = []
    for schema, table in _local_table_names(con, source_database):
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {_quote_identifier(schema)}")
        target = f"{_quote_identifier(schema)}.{_quote_identifier(table)}"
        source = f"{_quote_identifier(source_database)}.{_quote_identifier(schema)}.{_quote_identifier(table)}"
        con.execute(f"CREATE TABLE {target} AS SELECT * FROM {source}")
        row_count = int(con.execute(f"SELECT count(*) FROM {target}").fetchone()[0])
        copied.append({"schema": schema, "table": table, "row_count": row_count})
    return copied


def _copy_enrichment_tables(con: Any, source_database: str) -> list[dict[str, Any]]:
    """Copy evidence tables into their constrained local definitions."""

    # CTAS deliberately drops primary/unique constraints.  The evidence
    # writer depends on those constraints for idempotent ON CONFLICT upserts,
    # so create the maintained schema first and then copy rows by column name.
    con.execute(SCHEMA_SQL)
    copied: list[dict[str, Any]] = []
    for schema, table in _local_table_names(con, source_database):
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {_quote_identifier(schema)}")
        target = f"{_quote_identifier(schema)}.{_quote_identifier(table)}"
        source = f"{_quote_identifier(source_database)}.{_quote_identifier(schema)}.{_quote_identifier(table)}"
        con.execute(f"INSERT INTO {target} BY NAME SELECT * FROM {source}")
        row_count = int(con.execute(f"SELECT count(*) FROM {target}").fetchone()[0])
        copied.append({"schema": schema, "table": table, "row_count": row_count})
    return copied


def _copy_indexes(con: Any, source_database: str) -> list[str]:
    """Recreate source indexes after CTAS removed their definitions."""

    try:
        rows = con.execute(
            f"""
            SELECT sql
            FROM duckdb_indexes()
            WHERE database_name = ?
              AND schema_name NOT IN ('information_schema', 'pg_catalog', 'main')
              AND sql IS NOT NULL
            ORDER BY schema_name, table_name, index_name
            """,
            [source_database],
        ).fetchall()
    except Exception:
        # Older DuckDB versions do not expose attached duckdb_indexes().
        return []
    created: list[str] = []
    for (sql,) in rows:
        statement = str(sql).strip()
        if not statement:
            continue
        con.execute(statement)
        created.append(statement)
    return created


def _copy_views(con: Any, source_database: str) -> list[str]:
    """Copy user views with local references instead of pointing at sources."""

    try:
        rows = con.execute(
            f"""
            SELECT schema_name, view_name, sql
            FROM duckdb_views()
            WHERE database_name = ?
              AND schema_name NOT IN ('information_schema', 'pg_catalog', 'main', 'temp')
              AND sql IS NOT NULL
            ORDER BY schema_name, view_name
            """,
            [source_database],
        ).fetchall()
    except Exception:
        return []
    pending = [row for row in rows if str(row[2]).strip().upper().startswith("CREATE VIEW ")]
    created: list[str] = []
    # Views can depend on another view (for example source_counts depends on
    # source_records).  Retry unresolved definitions until their dependencies
    # exist, while still failing closed if a definition is genuinely invalid.
    while pending:
        next_pending = []
        progress = False
        last_error: Exception | None = None
        for schema, view, sql in pending:
            statement = str(sql).strip()
            statement = "CREATE OR REPLACE " + statement[len("CREATE ") :]
            try:
                con.execute(statement)
            except Exception as exc:
                next_pending.append((schema, view, sql))
                last_error = exc
            else:
                created.append(f"{schema}.{view}")
                progress = True
        if not progress:
            if last_error is not None:
                raise last_error
            break
        pending = next_pending
    return created


def _materialize_search_profile(con: Any) -> dict[str, Any]:
    """Create a one-row-per-company read model with resolved enrichment fields."""

    con.execute("CREATE SCHEMA IF NOT EXISTS search")
    con.execute(
        """
        CREATE OR REPLACE TABLE search.company_documents AS
        WITH contact_values AS (
            SELECT
                corporate_number,
                max(value_normalized) FILTER (WHERE contact_type = 'phone' AND status IN ('found', 'verified')) AS phone,
                max(value_normalized) FILTER (WHERE contact_type = 'email' AND status IN ('found', 'verified')) AS email,
                max(value_normalized) FILTER (WHERE contact_type = 'form_url' AND status IN ('found', 'verified')) AS inquiry_form_url,
                max(sales_eligibility) FILTER (WHERE contact_type = 'phone' AND status IN ('found', 'verified')) AS phone_sales_eligibility,
                max(sales_eligibility) FILTER (WHERE contact_type = 'email' AND status IN ('found', 'verified')) AS email_sales_eligibility
            FROM enrichment.company_contact_points
            WHERE status IN ('found', 'verified')
              AND sales_eligibility = 'allowed'
            GROUP BY corporate_number
        ), websites AS (
            SELECT corporate_number, normalized_url AS official_url
            FROM enrichment.company_websites
            WHERE status = 'verified'
              AND website_role = 'official_homepage'
            QUALIFY row_number() OVER (
                PARTITION BY corporate_number
                ORDER BY confidence DESC NULLS LAST, checked_at DESC NULLS LAST, first_seen_at DESC
            ) = 1
        ), locations AS (
            SELECT corporate_number, address_normalized AS resolved_address,
                   postal_code AS resolved_postal_code,
                   prefecture_name AS resolved_prefecture_name,
                   city_name AS resolved_city_name
            FROM enrichment.company_locations
            WHERE status IN ('found', 'verified')
            QUALIFY row_number() OVER (
                PARTITION BY corporate_number
                ORDER BY confidence DESC NULLS LAST, observed_at DESC
            ) = 1
        )
        SELECT
            c.*,
            coalesce(l.resolved_address, c.full_address) AS resolved_address,
            coalesce(l.resolved_postal_code, c.post_code) AS resolved_postal_code,
            coalesce(l.resolved_prefecture_name, c.prefecture_name) AS resolved_prefecture_name,
            coalesce(l.resolved_city_name, c.city_name) AS resolved_city_name,
            coalesce(w.official_url, c.company_url) AS effective_company_url,
            w.official_url,
            a.phone,
            a.email,
            a.inquiry_form_url,
            a.phone_sales_eligibility,
            a.email_sales_eligibility,
            coalesce(cov.website_state, 'pending') AS enrichment_website_state,
            coalesce(cov.phone_state, 'pending') AS enrichment_phone_state,
            coalesce(cov.email_state, 'pending') AS enrichment_email_state,
            coalesce(cov.form_state, 'pending') AS enrichment_form_state,
            coalesce(cov.location_state, 'pending') AS enrichment_location_state,
            cov.sales_state AS enrichment_sales_state
        FROM core.companies c
        LEFT JOIN contact_values a USING (corporate_number)
        LEFT JOIN websites w USING (corporate_number)
        LEFT JOIN locations l USING (corporate_number)
        LEFT JOIN crm.v_enrichment_coverage cov USING (corporate_number)
        """
    )
    con.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_search_company_documents_corporate_number
            ON search.company_documents(corporate_number)
        """
    )
    con.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_search_company_documents_prefecture
            ON search.company_documents(resolved_prefecture_name)
        """
    )
    con.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_search_company_documents_city
            ON search.company_documents(resolved_city_name)
        """
    )
    return {
        "row_count": int(con.execute("SELECT count(*) FROM search.company_documents").fetchone()[0]),
        "phone_count": int(
            con.execute("SELECT count(*) FROM search.company_documents WHERE phone IS NOT NULL").fetchone()[0]
        ),
        "email_count": int(
            con.execute("SELECT count(*) FROM search.company_documents WHERE email IS NOT NULL").fetchone()[0]
        ),
        "website_count": int(
            con.execute(
                "SELECT count(*) FROM search.company_documents WHERE effective_company_url IS NOT NULL"
            ).fetchone()[0]
        ),
    }


def _manifest(con: Any, canonical_path: Path, enrichment_path: Path, copied: list[dict[str, Any]]) -> dict[str, Any]:
    company_count = int(con.execute("SELECT count(*) FROM core.companies").fetchone()[0])
    state_count = int(con.execute("SELECT count(*) FROM enrichment.enrichment_state").fetchone()[0])
    return {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "generation_id": str(uuid.uuid4()),
        "built_at": datetime.now(timezone.utc).isoformat(),
        "canonical_database": str(canonical_path),
        "canonical_bytes": canonical_path.stat().st_size,
        "enrichment_database": str(enrichment_path),
        "enrichment_bytes": enrichment_path.stat().st_size,
        "company_count": company_count,
        "enrichment_state_count": state_count,
        "copied_tables": copied,
    }


def build_runtime_database(
    database_path: Path = DEFAULT_DB,
    enrichment_path: Path = DEFAULT_ENRICHMENT_DB,
    output_path: Path = DEFAULT_RUNTIME_DB,
    *,
    threads: int = 4,
    memory_limit: str = "8GB",
) -> dict[str, Any]:
    """Build the one-file operational DB without changing either input DB."""

    canonical_path = Path(database_path).resolve()
    enrichment_path = Path(enrichment_path).resolve()
    output_path = Path(output_path).resolve()
    if not canonical_path.is_file():
        raise RuntimeBuildError(f"canonical DB がありません: {canonical_path}")
    if not enrichment_path.is_file():
        raise RuntimeBuildError(f"enrichment DB がありません: {enrichment_path}")
    if canonical_path == output_path or enrichment_path == output_path:
        raise RuntimeBuildError("入力DBと出力DBは別ファイルにしてください。")
    if threads < 1:
        raise RuntimeBuildError("threads は1以上です。")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    building_path = output_path.with_suffix(output_path.suffix + ".building")
    if building_path.exists():
        if building_path.is_file():
            building_path.unlink()
        else:
            shutil.rmtree(building_path)

    duckdb = _duckdb()
    con = duckdb.connect(str(building_path), read_only=False)
    attached: list[str] = []
    copied: list[dict[str, Any]] = []
    temp_dir: Path | None = None
    try:
        con.execute(f"PRAGMA threads={int(threads)}")
        con.execute(f"SET memory_limit={_sql_string(memory_limit)}")
        con.execute("SET preserve_insertion_order=false")
        temp_dir = Path(tempfile.mkdtemp(prefix="queria-runtime-", dir=str(output_path.parent)))
        con.execute(f"SET temp_directory={_sql_string(temp_dir)}")
        con.execute(f"ATTACH {_sql_string(canonical_path)} AS canonical (READ_ONLY)")
        attached.append("canonical")
        con.execute(f"ATTACH {_sql_string(enrichment_path)} AS enrichment_src (READ_ONLY)")
        attached.append("enrichment_src")

        copied.extend(_copy_base_tables(con, "canonical"))
        copied.extend(_copy_enrichment_tables(con, "enrichment_src"))
        # _copy_enrichment_tables already creates the evidence-layer indexes
        # together with its constrained schema.
        source_indexes = _copy_indexes(con, "canonical")
        source_views = _copy_views(con, "canonical")
        # The companion's crm views intentionally point at its canonical
        # attachment.  They are rebuilt below against the local core tables;
        # copying those definitions would reintroduce the cross-database join.

        initialize_enrichment_schema(con, company_relation="core.companies")
        profile = _materialize_search_profile(con)
        con.execute("CREATE SCHEMA IF NOT EXISTS meta")
        manifest = _manifest(con, canonical_path, enrichment_path, copied)
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS meta.runtime_manifest (
                schema_version VARCHAR PRIMARY KEY,
                built_at TIMESTAMPTZ NOT NULL,
                manifest_json VARCHAR NOT NULL
            )
            """
        )
        con.execute(
            """
            INSERT INTO meta.runtime_manifest(schema_version, built_at, manifest_json)
            VALUES (?, ?, ?)
            ON CONFLICT (schema_version) DO UPDATE SET
                built_at = excluded.built_at,
                manifest_json = excluded.manifest_json
            """,
            [RUNTIME_SCHEMA_VERSION, manifest["built_at"], json.dumps(manifest, ensure_ascii=False)],
        )

        # Runtime checks are intentionally strict: a partial copy must never
        # be promoted as the operational database.
        company_count = int(con.execute("SELECT count(*) FROM core.companies").fetchone()[0])
        profile_count = int(con.execute("SELECT count(*) FROM search.company_documents").fetchone()[0])
        if company_count < 1 or profile_count != company_count:
            raise RuntimeBuildError(
                f"ランタイム法人件数が不一致です: core={company_count}, search={profile_count}"
            )
        con.execute("CHECKPOINT")
        for alias in reversed(attached):
            con.execute(f"DETACH {_quote_identifier(alias)}")
        attached.clear()
        con.close()
        con = None
        os.replace(building_path, output_path)
        return {
            "runtime_database": str(output_path),
            "bytes": output_path.stat().st_size,
            "company_count": company_count,
            "profile": profile,
            "copied_tables": len(copied),
            "copied_indexes": len(source_indexes),
            "copied_views": len(source_views),
            "schema_version": RUNTIME_SCHEMA_VERSION,
            "generation_id": manifest["generation_id"],
        }
    except Exception as exc:
        if con is not None:
            try:
                for alias in reversed(attached):
                    con.execute(f"DETACH {_quote_identifier(alias)}")
            except Exception:
                pass
            con.close()
        if "failed to pin block" in str(exc).lower():
            raise RuntimeBuildError(
                "統合DBの生成でDuckDBメモリ上限に達しました。"
                " --memory-limit 8GB 以上、または --threads 2 以下で再実行してください。"
            ) from exc
        raise
    finally:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)


def runtime_summary(runtime_path: Path = DEFAULT_RUNTIME_DB) -> dict[str, Any]:
    """Read-only health and coverage summary for the operational DB."""

    runtime_path = Path(runtime_path).resolve()
    if not runtime_path.is_file():
        raise RuntimeBuildError(f"ランタイムDBがありません: {runtime_path}")
    con = _duckdb().connect(str(runtime_path), read_only=True)
    try:
        def optional_count(relation: str) -> int:
            try:
                return int(con.execute(f"SELECT count(*) FROM {relation}").fetchone()[0])
            except Exception:
                return 0

        counts = {
            "companies": optional_count("core.companies"),
            "search_profiles": optional_count("search.company_documents"),
            "enrichment_state": optional_count("enrichment.enrichment_state"),
            "evidence_documents": optional_count("enrichment.evidence_documents"),
            "contact_points": optional_count("enrichment.company_contact_points"),
            "establishments": optional_count("enrichment.company_establishments"),
            "websites": optional_count("enrichment.company_websites"),
            "locations": optional_count("enrichment.company_locations"),
        }
        manifest_row = con.execute(
            "SELECT manifest_json FROM meta.runtime_manifest ORDER BY built_at DESC LIMIT 1"
        ).fetchone()
        return {
            "runtime_database": str(runtime_path),
            "bytes": runtime_path.stat().st_size,
            "counts": counts,
            "manifest": None if manifest_row is None else json.loads(str(manifest_row[0])),
        }
    finally:
        con.close()


__all__ = [
    "DEFAULT_RUNTIME_DB",
    "RUNTIME_SCHEMA_VERSION",
    "RuntimeBuildError",
    "build_runtime_database",
    "runtime_summary",
]
