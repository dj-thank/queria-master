"""Build a filtered Queria database for one JSIC middle-category code.

The source database is kept read-only.  Every table that has a
``corporate_number`` column is filtered to the companies in the requested
JSIC middle category; reference and metadata tables without that column are
copied as-is so the existing views and runtime builder remain compatible.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
from typing import Any

import duckdb


def quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def sql_string(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def relation(schema: str, table: str, database: str | None = None) -> str:
    prefix = f"{quote_identifier(database)}." if database else ""
    return f"{prefix}{quote_identifier(schema)}.{quote_identifier(table)}"


def base_tables(con: Any, database: str) -> list[tuple[str, str]]:
    rows = con.execute(
        """
        SELECT schema_name, table_name
        FROM duckdb_tables()
        WHERE database_name = ?
          AND NOT internal
          AND NOT temporary
          AND schema_name NOT IN ('information_schema', 'pg_catalog', 'main')
        ORDER BY schema_name, table_name
        """,
        [database],
    ).fetchall()
    return [(str(schema), str(table)) for schema, table in rows]


def has_column(con: Any, database: str, schema: str, table: str, column: str) -> bool:
    return bool(
        con.execute(
            """
            SELECT count(*)
            FROM duckdb_columns()
            WHERE database_name = ? AND schema_name = ? AND table_name = ?
              AND column_name = ?
            """,
            [database, schema, table, column],
        ).fetchone()[0]
    )


def source_views(con: Any, database: str) -> list[str]:
    rows = con.execute(
        """
        SELECT sql
        FROM duckdb_views()
        WHERE database_name = ?
          AND schema_name NOT IN ('information_schema', 'pg_catalog', 'main', 'temp')
          AND sql IS NOT NULL
        ORDER BY schema_name, view_name
        """,
        [database],
    ).fetchall()
    return [str(row[0]).strip() for row in rows if str(row[0]).strip()]


def build_filtered_database(
    source_path: Path,
    output_path: Path,
    jsic_code: str,
    *,
    threads: int = 4,
    memory_limit: str = "8GB",
) -> dict[str, Any]:
    source_path = source_path.resolve()
    output_path = output_path.resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    if source_path == output_path:
        raise ValueError("source and output databases must be different")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    building_path = output_path.with_suffix(output_path.suffix + ".building")
    for path in (building_path,):
        if path.is_file():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)

    con = duckdb.connect(str(building_path), read_only=False)
    try:
        con.execute(f"PRAGMA threads={int(threads)}")
        con.execute(f"SET memory_limit={sql_string(memory_limit)}")
        con.execute("SET preserve_insertion_order=false")
        con.execute(f"ATTACH {sql_string(source_path)} AS source (READ_ONLY)")
        con.execute(
            """
            CREATE TEMP TABLE target_corporate_numbers AS
            SELECT DISTINCT trim(CAST(corporate_number AS VARCHAR)) AS corporate_number
            FROM source.core.company_industries
            WHERE CASE WHEN length(?) = 1
                       THEN trim(CAST(jsic_major_code AS VARCHAR)) = ?
                       ELSE trim(CAST(jsic_middle_code AS VARCHAR)) = ?
                  END
              AND regexp_full_match(trim(CAST(corporate_number AS VARCHAR)), '[0-9]{13}')
            """,
            [jsic_code, jsic_code, jsic_code],
        )
        target_count = int(con.execute("SELECT count(*) FROM target_corporate_numbers").fetchone()[0])
        if target_count < 1:
            raise ValueError(f"no companies found for JSIC code {jsic_code}")

        copied: list[dict[str, Any]] = []
        for schema, table in base_tables(con, "source"):
            con.execute(f"CREATE SCHEMA IF NOT EXISTS {quote_identifier(schema)}")
            source_relation = relation(schema, table, "source")
            target_relation = relation(schema, table)
            filtered = has_column(con, "source", schema, table, "corporate_number")
            if filtered:
                predicate = (
                    "WHERE trim(CAST(corporate_number AS VARCHAR)) IN "
                    "(SELECT corporate_number FROM target_corporate_numbers)"
                )
            elif schema == "meta":
                # Keep classification/catalog metadata for diagnostics and UI
                # labels.  Non-company data tables get an empty compatible
                # schema so a scoped DB cannot silently expose out-of-scope
                # records.
                predicate = ""
            else:
                predicate = "WHERE FALSE"
            con.execute(f"CREATE TABLE {target_relation} AS SELECT * FROM {source_relation} {predicate}")
            row_count = int(con.execute(f"SELECT count(*) FROM {target_relation}").fetchone()[0])
            copied.append({"schema": schema, "table": table, "row_count": row_count, "filtered": filtered})

        # Recreate source views after all filtered tables exist.  Definitions
        # are unqualified in the canonical DB and therefore resolve locally.
        created_views: list[str] = []
        pending = list(source_views(con, "source"))
        while pending:
            next_pending: list[str] = []
            progress = False
            last_error: Exception | None = None
            for statement in pending:
                if not statement.upper().startswith("CREATE VIEW "):
                    next_pending.append(statement)
                    continue
                local_statement = "CREATE OR REPLACE " + statement[len("CREATE ") :]
                try:
                    con.execute(local_statement)
                except Exception as exc:
                    next_pending.append(statement)
                    last_error = exc
                else:
                    created_views.append(local_statement.split(" VIEW ", 1)[-1].split(" AS ", 1)[0].strip())
                    progress = True
            if not progress:
                if last_error:
                    raise last_error
                break
            pending = next_pending

        con.execute("CHECKPOINT")
        con.execute("DETACH source")
        con.close()
        con = None
        os.replace(building_path, output_path)
        return {
            "source_database": str(source_path),
            "output_database": str(output_path),
            "jsic_code": jsic_code,
            "target_companies": target_count,
            "copied_tables": copied,
            "created_views": len(created_views),
            "bytes": output_path.stat().st_size,
        }
    finally:
        if con is not None:
            try:
                con.execute("DETACH source")
            except Exception:
                pass
            con.close()
        if building_path.exists():
            if building_path.is_file():
                building_path.unlink()
            else:
                shutil.rmtree(building_path, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--jsic-code", default="39", help="JSIC大分類1文字または中分類2桁")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--memory-limit", default="8GB")
    args = parser.parse_args()
    result = build_filtered_database(
        args.source,
        args.output,
        str(args.jsic_code).strip().upper(),
        threads=args.threads,
        memory_limit=args.memory_limit,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
