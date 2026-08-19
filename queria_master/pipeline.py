from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from .resources import (
    ALL_PUBLIC_SCOPE,
    DEFAULT_CACHE,
    DEFAULT_DB,
    PROJECT_ROOT,
    PUBLIC_TABLES,
    REFERENCE_ROOT,
    load_public_sql,
    load_scope_sql,
    normalize_scope,
)


class PipelineError(RuntimeError):
    pass


@dataclass(frozen=True)
class RefreshResult:
    database_path: Path
    parquet_path: Path | None
    scope: str
    row_count: int
    parquet_bytes: int
    parquet_sha256: str
    artifact_paths: tuple[Path, ...] = ()


PROJECT_VERSION = "0.7.0"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _duckdb_module():
    try:
        import duckdb  # type: ignore
    except ImportError as exc:
        raise PipelineError(
            "duckdb がありません。bootstrap.ps1 / bootstrap.sh を先に実行してください。"
        ) from exc
    return duckdb


def find_queria_executable() -> Path:
    executable_name = "queria.exe" if os.name == "nt" else "queria"
    candidates = [Path(sys.executable).resolve().parent / executable_name]
    located = shutil.which("queria")
    if located:
        candidates.append(Path(located))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise PipelineError(
        "Queria CLI が見つかりません。requirements.txt をインストールしてください。"
    )


def _run_queria(
    args: list[str],
    *,
    capture_output: bool = False,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    executable = find_queria_executable()
    env = os.environ.copy()
    env.setdefault("QUERIA_NO_TELEMETRY", "1")
    try:
        completed = subprocess.run(
            [str(executable), *args],
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=capture_output,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise PipelineError(f"Queria CLI がタイムアウトしました: {' '.join(args[:2])}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        hint = (
            "\n匿名アクセスのレート制限なら、Queria CLI で `queria login` を一度実行してください。"
        )
        raise PipelineError(
            f"Queria CLI に失敗しました (exit={completed.returncode})。\n{detail}{hint}"
        )
    return completed


def _capture_json(args: list[str]) -> Any:
    try:
        completed = _run_queria([*args, "--format", "json"], capture_output=True, timeout=180)
        return json.loads(completed.stdout)
    except Exception as exc:  # Metadata failure must not discard a completed data export.
        return {"error": str(exc), "command": args}


def _client_version() -> str:
    try:
        return importlib.metadata.version("queria")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _sha256(path: Path, block_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(block_size):
            digest.update(block)
    return digest.hexdigest()


def _sql_string(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def export_remote(scope: str, output_path: Path) -> None:
    sql = load_scope_sql(scope)
    first = sql.lstrip().split(None, 1)[0].upper()
    if first not in {"SELECT", "WITH"}:
        raise PipelineError("リモート SQL は SELECT / WITH で始まる必要があります。")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Queria decides the output format from the final extension. Keep .parquet last.
    partial = output_path.with_name(f"{output_path.stem}.partial{output_path.suffix}")
    partial.unlink(missing_ok=True)
    print(f"Queria から公開データを抽出しています: scope={scope}")
    _run_queria(["sql", sql, "--out", str(partial)], timeout=None)
    if not partial.is_file() or partial.stat().st_size == 0:
        raise PipelineError("Queria の出力 Parquet が作成されませんでした。")
    os.replace(partial, output_path)


def export_public_tables(output_dir: Path) -> dict[str, Path]:
    """Export every public Queria table used by the all-public scope.

    Each table is written independently so a failed activity-table export
    cannot be mistaken for a complete snapshot.  The caller only promotes
    the directory after every table has passed the non-empty-file check and
    the local DuckDB build has completed.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    exported: dict[str, Path] = {}
    for table_key in PUBLIC_TABLES:
        sql = load_public_sql(table_key)
        first = sql.lstrip().split(None, 1)[0].upper()
        if first not in {"SELECT", "WITH"}:
            raise PipelineError(f"公開テーブル SQL が SELECT / WITH ではありません: {table_key}")
        output_path = output_dir / f"{table_key}.parquet"
        partial = output_path.with_name(f"{output_path.stem}.partial{output_path.suffix}")
        partial.unlink(missing_ok=True)
        print(f"Queria から公開テーブルを抽出しています: table={table_key}")
        _run_queria(["sql", sql, "--out", str(partial)], timeout=None)
        if not partial.is_file() or partial.stat().st_size == 0:
            raise PipelineError(f"Queria の出力 Parquet が作成されませんでした: {table_key}")
        os.replace(partial, output_path)
        exported[table_key] = output_path
    return exported


def _artifact_manifest(paths: Mapping[str, Path]) -> tuple[int, str, list[dict[str, Any]]]:
    """Return total bytes, a stable manifest digest, and per-file statistics."""
    digest = hashlib.sha256()
    total_bytes = 0
    records: list[dict[str, Any]] = []
    for name, path in sorted(paths.items()):
        size = path.stat().st_size
        sha = _sha256(path)
        total_bytes += size
        record = {"name": name, "bytes": size, "sha256": sha}
        records.append(record)
        digest.update(json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        digest.update(b"\n")
    return total_bytes, digest.hexdigest(), records


def _load_source_registry() -> list[dict[str, Any]]:
    path = REFERENCE_ROOT / "sources.json"
    return json.loads(path.read_text(encoding="utf-8"))["sources"]


def build_local_database(
    parquet_path: Path,
    database_path: Path,
    *,
    scope: str,
    started_at: str,
    source_metadata: dict[str, Any],
) -> tuple[int, str]:
    duckdb = _duckdb_module()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    temp_db = database_path.with_name(database_path.name + ".building")
    temp_wal = Path(str(temp_db) + ".wal")
    temp_db.unlink(missing_ok=True)
    temp_wal.unlink(missing_ok=True)

    parquet_sha = _sha256(parquet_path)
    parquet_bytes = parquet_path.stat().st_size
    refresh_id = str(uuid4())
    completed_at = _utc_now()

    con = duckdb.connect(str(temp_db))
    build_succeeded = False
    try:
        con.execute("PRAGMA threads = 4")
        con.execute("CREATE SCHEMA core")
        con.execute("CREATE SCHEMA meta")
        con.execute(
            f"CREATE TABLE core.companies AS SELECT * FROM read_parquet({_sql_string(parquet_path.resolve())})"
        )
        required_columns = {
            "corporate_number",
            "company_name",
            "prefecture_code",
            "prefecture_name",
            "city_name",
            "representative_name",
            "capital_stock",
            "employee_number",
            "business_summary",
            "jsic_codes_raw",
            "company_url",
        }
        actual_columns = {
            str(row[0])
            for row in con.execute("DESCRIBE core.companies").fetchall()
        }
        missing_columns = sorted(required_columns - actual_columns)
        if missing_columns:
            raise PipelineError(
                "Queria 出力のスキーマが想定と異なります。欠落列: "
                + ", ".join(missing_columns)
            )

        row_count = int(con.execute("SELECT count(*) FROM core.companies").fetchone()[0])
        if row_count <= 0:
            raise PipelineError("抽出結果が 0 件でした。業種コード仕様またはデータ更新を確認してください。")

        con.execute(
            """
            CREATE TABLE core.company_industries AS
            SELECT
                c.corporate_number,
                trim(code) AS jsic_code,
                substring(trim(code), 1, 1) AS jsic_major_code,
                CASE
                    WHEN length(trim(code)) >= 3 THEN substring(trim(code), 2, 2)
                    ELSE NULL
                END AS jsic_middle_code
            FROM core.companies c
            CROSS JOIN UNNEST(string_split(coalesce(c.jsic_codes_raw, ''), '|')) AS u(code)
            WHERE trim(code) <> ''
            """
        )
        _create_company_category_index(con)

        con.execute(
            """
            CREATE TABLE meta.jsic_info_communications (
                major_code VARCHAR,
                major_name VARCHAR,
                middle_code VARCHAR,
                middle_name VARCHAR
            )
            """
        )
        con.executemany(
            "INSERT INTO meta.jsic_info_communications VALUES (?, ?, ?, ?)",
            [
                ("G", "情報通信業", "37", "通信業"),
                ("G", "情報通信業", "38", "放送業"),
                ("G", "情報通信業", "39", "情報サービス業"),
                ("G", "情報通信業", "40", "インターネット附随サービス業"),
                ("G", "情報通信業", "41", "映像・音声・文字情報制作業"),
            ],
        )

        con.execute(
            """
            CREATE VIEW core.v_info_communications AS
            SELECT DISTINCT c.*
            FROM core.companies c
            JOIN core.company_industries i USING (corporate_number)
            WHERE i.jsic_major_code = 'G'
              AND (i.jsic_middle_code IN ('37', '38', '39', '40', '41') OR i.jsic_middle_code IS NULL)
            """
        )
        con.execute(
            """
            CREATE VIEW core.v_data_quality AS
            SELECT
                count(*) AS company_count,
                count_if(company_name IS NOT NULL AND trim(company_name) <> '') AS with_name,
                count_if(prefecture_name IS NOT NULL) AS with_prefecture,
                count_if(representative_name IS NOT NULL) AS with_representative,
                count_if(capital_stock IS NOT NULL) AS with_capital,
                count_if(employee_number IS NOT NULL) AS with_employee_number,
                count_if(company_url IS NOT NULL AND trim(company_url) <> '') AS with_company_url,
                count_if(business_summary IS NOT NULL AND trim(business_summary) <> '') AS with_business_summary,
                count_if(jsic_codes_raw IS NOT NULL AND trim(jsic_codes_raw) <> '') AS with_jsic_codes
            FROM core.companies
            """
        )

        con.execute(
            """
            CREATE TABLE meta.source_registry (
                source_name VARCHAR,
                queria_dataset VARCHAR,
                table_name VARCHAR,
                role VARCHAR,
                source_url VARCHAR,
                license_name VARCHAR,
                attribution VARCHAR
            )
            """
        )
        registry_rows = [
            (
                item["source_name"],
                item.get("queria_dataset"),
                item.get("table_name"),
                item.get("role"),
                item.get("source_url"),
                item.get("license_name"),
                item.get("attribution"),
            )
            for item in _load_source_registry()
        ]
        con.executemany("INSERT INTO meta.source_registry VALUES (?, ?, ?, ?, ?, ?, ?)", registry_rows)

        con.execute(
            """
            CREATE TABLE meta.source_metadata (
                dataset_name VARCHAR,
                metadata_json JSON
            )
            """
        )
        con.executemany(
            "INSERT INTO meta.source_metadata VALUES (?, ?)",
            [
                ("gbizinfo", json.dumps(source_metadata.get("gbizinfo"), ensure_ascii=False)),
                ("houjin_bangou", json.dumps(source_metadata.get("houjin_bangou"), ensure_ascii=False)),
                ("gbizinfo_columns", json.dumps(source_metadata.get("gbizinfo_columns"), ensure_ascii=False)),
                ("houjin_bangou_columns", json.dumps(source_metadata.get("houjin_bangou_columns"), ensure_ascii=False)),
            ],
        )

        con.execute(
            """
            CREATE TABLE meta.refresh_log (
                refresh_id VARCHAR,
                scope VARCHAR,
                started_at TIMESTAMPTZ,
                completed_at TIMESTAMPTZ,
                row_count BIGINT,
                parquet_bytes BIGINT,
                parquet_sha256 VARCHAR,
                queria_client_version VARCHAR,
                project_version VARCHAR,
                artifact_count INTEGER,
                artifact_manifest_json JSON
            )
            """
        )
        con.execute(
            "INSERT INTO meta.refresh_log VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                refresh_id,
                scope,
                started_at,
                completed_at,
                row_count,
                parquet_bytes,
                parquet_sha,
                _client_version(),
                PROJECT_VERSION,
                1,
                json.dumps(
                    [{"name": parquet_path.name, "bytes": parquet_bytes, "sha256": parquet_sha}],
                    ensure_ascii=False,
                ),
            ],
        )

        con.execute("CREATE INDEX idx_companies_corporate_number ON core.companies(corporate_number)")
        con.execute("CREATE INDEX idx_companies_prefecture_code ON core.companies(prefecture_code)")
        con.execute("CREATE INDEX idx_industries_number ON core.company_industries(corporate_number)")
        con.execute("CREATE INDEX idx_industries_major ON core.company_industries(jsic_major_code)")
        con.execute("CREATE INDEX idx_industries_middle ON core.company_industries(jsic_middle_code)")
        con.execute(
            "UPDATE meta.refresh_log SET completed_at = ? WHERE refresh_id = ?",
            [_utc_now(), refresh_id],
        )
        con.execute("CHECKPOINT")
        build_succeeded = True
    finally:
        con.close()
        if not build_succeeded:
            temp_db.unlink(missing_ok=True)
            temp_wal.unlink(missing_ok=True)

    try:
        os.replace(temp_db, database_path)
    except PermissionError as exc:
        raise PipelineError(
            f"DB を置換できません。{database_path} を開いている DuckDB/BI ツールを閉じて再実行してください。"
        ) from exc
    temp_wal.unlink(missing_ok=True)
    return row_count, parquet_sha


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _source_expr(alias: str, columns: set[str], name: str, null_type: str = "VARCHAR") -> str:
    if name in columns:
        return f"{alias}.{_quote_identifier(name)}"
    return f"CAST(NULL AS {null_type})"


def _coalesce_expr(*expressions: str) -> str:
    present = [expression for expression in expressions if expression]
    if not present:
        return "CAST(NULL AS VARCHAR)"
    if len(present) == 1:
        return present[0]
    return "coalesce(" + ", ".join(present) + ")"


def _describe_columns(con: Any, relation: str) -> set[str]:
    return {str(row[0]) for row in con.execute(f"DESCRIBE {relation}").fetchall()}


def _require_columns(table_key: str, columns: set[str], required: set[str]) -> None:
    missing = sorted(required - columns)
    if missing:
        raise PipelineError(
            f"公開テーブル {table_key} のスキーマが想定と異なります。欠落列: {', '.join(missing)}"
        )


def _info_communications_code_expr(business_items: str) -> str:
    return f"""
        concat_ws('|',
            CASE WHEN regexp_matches(coalesce({business_items}, ''), '(^|[|\\-])G:') THEN 'G' END,
            CASE WHEN regexp_matches(coalesce({business_items}, ''), '(^|-)37:') THEN 'G37' END,
            CASE WHEN regexp_matches(coalesce({business_items}, ''), '(^|-)38:') THEN 'G38' END,
            CASE WHEN regexp_matches(coalesce({business_items}, ''), '(^|-)39:') THEN 'G39' END,
            CASE WHEN regexp_matches(coalesce({business_items}, ''), '(^|-)40:') THEN 'G40' END,
            CASE WHEN regexp_matches(coalesce({business_items}, ''), '(^|-)41:') THEN 'G41' END
        )
    """


def _create_jsic_reference(con: Any) -> None:
    con.execute(
        """
        CREATE TABLE meta.jsic_info_communications (
            major_code VARCHAR,
            major_name VARCHAR,
            middle_code VARCHAR,
            middle_name VARCHAR
        )
        """
    )
    con.executemany(
        "INSERT INTO meta.jsic_info_communications VALUES (?, ?, ?, ?)",
        [
            ("G", "情報通信業", "37", "通信業"),
            ("G", "情報通信業", "38", "放送業"),
            ("G", "情報通信業", "39", "情報サービス業"),
            ("G", "情報通信業", "40", "インターネット附随サービス業"),
            ("G", "情報通信業", "41", "映像・音声・文字情報制作業"),
        ],
    )


def _create_company_category_index(con: Any) -> None:
    """Materialize the small company/category relation used by fast filters."""
    con.execute(
        """
        CREATE TABLE core.company_category_index AS
        SELECT DISTINCT
            i.corporate_number,
            i.jsic_code,
            i.jsic_major_code,
            i.jsic_middle_code,
            c.prefecture_code,
            c.prefecture_name,
            c.city_name
        FROM core.company_industries i
        JOIN core.companies c USING (corporate_number)
        """
    )
    con.execute(
        "CREATE INDEX idx_category_number ON core.company_category_index(corporate_number)"
    )
    con.execute(
        "CREATE INDEX idx_category_major_prefecture "
        "ON core.company_category_index(jsic_major_code, prefecture_name, corporate_number)"
    )
    con.execute(
        "CREATE INDEX idx_category_middle_prefecture "
        "ON core.company_category_index(jsic_middle_code, prefecture_name, corporate_number)"
    )


def build_all_public_database(
    table_paths: Mapping[str, Path],
    database_path: Path,
    *,
    started_at: str,
    source_metadata: dict[str, Any],
) -> tuple[int, int, str, list[dict[str, Any]]]:
    """Build a complete local DB from all public Parquet tables.

    The source tables are kept locally in ``raw``/``gbizinfo`` without a
    projection.  ``core.companies`` is a deduplicated union of the current
    NTA catalog and gBizINFO's company summary, so gBizINFO-only corporations
    are not silently discarded.  Activity tables remain one-row-per-record
    instead of being lossy aggregates.
    """
    duckdb = _duckdb_module()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    temp_db = database_path.with_name(database_path.name + ".building")
    temp_wal = Path(str(temp_db) + ".wal")
    temp_db.unlink(missing_ok=True)
    temp_wal.unlink(missing_ok=True)

    missing_files = sorted(set(PUBLIC_TABLES) - set(table_paths))
    if missing_files:
        raise PipelineError("全公開スコープの出力が不足しています: " + ", ".join(missing_files))
    for table_key, path in table_paths.items():
        if table_key not in PUBLIC_TABLES:
            raise PipelineError(f"未知の公開テーブルです: {table_key}")
        if not path.is_file() or path.stat().st_size == 0:
            raise PipelineError(f"公開テーブル Parquet がありません: {path}")

    total_bytes, manifest_sha, manifest_records = _artifact_manifest(table_paths)
    refresh_id = str(uuid4())
    completed_at = _utc_now()
    con = duckdb.connect(str(temp_db))
    build_succeeded = False
    try:
        con.execute("PRAGMA threads = 4")
        schemas = {
            "raw",
            "core",
            "meta",
            *(str(spec["schema"]) for spec in PUBLIC_TABLES.values()),
        }
        for schema in sorted(schemas):
            con.execute(f"CREATE SCHEMA {_quote_identifier(schema)}")

        local_relations: dict[str, str] = {}
        local_columns: dict[str, set[str]] = {}
        local_stats: list[dict[str, Any]] = []
        for table_key, spec in PUBLIC_TABLES.items():
            relation = f"{spec['schema']}.{spec['table']}"
            path = table_paths[table_key]
            con.execute(
                f"CREATE TABLE {relation} AS SELECT * FROM read_parquet({_sql_string(path.resolve())})"
            )
            columns = _describe_columns(con, relation)
            required = set()
            join_column = spec.get("join_column")
            if join_column:
                required.add(str(join_column))
            if table_key == "houjin_bangou":
                required.add("name")
            if table_key == "gbizinfo_company":
                required.add("name")
            _require_columns(table_key, columns, required)
            row_count = int(con.execute(f"SELECT count(*) FROM {relation}").fetchone()[0])
            if row_count <= 0:
                raise PipelineError(f"全公開スコープの基幹テーブルが 0 件です: {table_key}")
            local_relations[table_key] = relation
            local_columns[table_key] = columns
            local_stats.append(
                {
                    **next(record for record in manifest_records if record["name"] == table_key),
                    "source_table": spec["source_table"],
                    "local_schema": spec["schema"],
                    "local_table": spec["table"],
                    "row_count": row_count,
                }
            )

        con.execute(
            """
            CREATE TABLE meta.public_table_catalog (
                table_key VARCHAR,
                dataset VARCHAR,
                source_table VARCHAR,
                local_schema VARCHAR,
                local_table VARCHAR,
                join_column VARCHAR,
                role VARCHAR
            )
            """
        )
        con.executemany(
            "INSERT INTO meta.public_table_catalog VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    table_key,
                    spec["dataset"],
                    spec["source_table"],
                    spec["schema"],
                    spec["table"],
                    spec.get("join_column"),
                    spec.get("role"),
                )
                for table_key, spec in PUBLIC_TABLES.items()
            ],
        )

        nta_columns = local_columns["houjin_bangou"]
        company_columns = local_columns["gbizinfo_company"]
        h_corporate_number = _source_expr("h", nta_columns, "corporate_number")
        g_corporate_number = _source_expr("g", company_columns, "corporate_number")
        h_name = _source_expr("h", nta_columns, "name")
        g_name = _source_expr("g", company_columns, "name")
        business_items = _source_expr("g", company_columns, "business_items")
        jsic_codes = _info_communications_code_expr(business_items)

        nta_order: list[str] = []
        for column in ("update_date", "seq"):
            if column in nta_columns:
                nta_order.append(f"h.{_quote_identifier(column)} DESC NULLS LAST")
        if not nta_order:
            nta_order.append(f"{h_corporate_number}")
        gbiz_order: list[str] = []
        if "latest_fiscal_year" in company_columns:
            gbiz_order.append('g."latest_fiscal_year" DESC NULLS LAST')
        gbiz_order.append(g_corporate_number)

        h_update_date = _source_expr("h", nta_columns, "update_date", "DATE")
        h_corporate_kind = _source_expr("h", nta_columns, "kind")
        h_post_code = _source_expr("h", nta_columns, "post_code")
        h_prefecture_code = _source_expr("h", nta_columns, "prefecture_code")
        h_prefecture_name = _source_expr("h", nta_columns, "prefecture_name")
        h_city_code = _source_expr("h", nta_columns, "city_code")
        h_city_name = _source_expr("h", nta_columns, "city_name")
        h_street_number = _source_expr("h", nta_columns, "street_number")
        h_lg_code = _source_expr("h", nta_columns, "lg_code")
        h_name_en = _source_expr("h", nta_columns, "name_en")
        h_furigana = _source_expr("h", nta_columns, "furigana")

        g_fields = {
            "representative_name": ("VARCHAR", "representative_name"),
            "capital_stock": ("BIGINT", "capital_stock"),
            "employee_number": ("INTEGER", "employee_number"),
            "date_of_establishment": ("DATE", "date_of_establishment"),
            "founding_year": ("INTEGER", "founding_year"),
            "business_summary": ("VARCHAR", "business_summary"),
            "company_url": ("VARCHAR", "company_url"),
            "subsidy_count": ("BIGINT", "subsidy_count"),
            "subsidy_total_amount": ("HUGEINT", "subsidy_total_amount"),
            "procurement_count": ("BIGINT", "procurement_count"),
            "procurement_total_award": ("HUGEINT", "procurement_total_award"),
            "latest_fiscal_year": ("INTEGER", "latest_fiscal_year"),
            "latest_net_sales": ("BIGINT", "latest_net_sales"),
            "latest_ordinary_income": ("BIGINT", "latest_ordinary_income"),
            "latest_net_income": ("BIGINT", "latest_net_income"),
            "latest_total_assets": ("BIGINT", "latest_total_assets"),
            "latest_net_assets": ("BIGINT", "latest_net_assets"),
            "avg_age": ("DOUBLE", "avg_age"),
            "avg_monthly_overtime": ("DOUBLE", "avg_monthly_overtime"),
            "female_ratio": ("DOUBLE", "female_ratio"),
        }
        g_field_expr = {
            name: _source_expr("g", company_columns, source_name, null_type)
            for name, (null_type, source_name) in g_fields.items()
        }
        joined_sql = f"""
            WITH nta_ranked AS (
                SELECT
                    h.*,
                    row_number() OVER (
                        PARTITION BY {h_corporate_number}
                        ORDER BY {', '.join(nta_order)}
                    ) AS __queria_dedupe_rn
                FROM raw.houjin_bangou h
            ),
            nta AS (
                SELECT * EXCLUDE (__queria_dedupe_rn)
                FROM nta_ranked
                WHERE __queria_dedupe_rn = 1
            ),
            gbiz_ranked AS (
                SELECT
                    g.*,
                    row_number() OVER (
                        PARTITION BY {g_corporate_number}
                        ORDER BY {', '.join(gbiz_order)}
                    ) AS __queria_dedupe_rn
                FROM gbizinfo.company_summary g
            ),
            gbiz AS (
                SELECT * EXCLUDE (__queria_dedupe_rn)
                FROM gbiz_ranked
                WHERE __queria_dedupe_rn = 1
            )
            SELECT
                coalesce(CAST(h.corporate_number AS VARCHAR), CAST(g.corporate_number AS VARCHAR)) AS corporate_number,
                coalesce({h_name}, {g_name}) AS company_name,
                {h_name_en} AS company_name_en,
                {h_furigana} AS company_name_kana,
                {g_name} AS gbizinfo_company_name,
                {h_corporate_kind} AS corporate_kind_code,
                {h_post_code} AS post_code,
                {h_prefecture_code} AS prefecture_code,
                {h_prefecture_name} AS prefecture_name,
                {h_city_code} AS city_code,
                {h_city_name} AS city_name,
                {h_street_number} AS street_number,
                concat_ws('', {h_prefecture_name}, {h_city_name}, {h_street_number}) AS full_address,
                {h_lg_code} AS lg_code,
                {g_field_expr['representative_name']} AS representative_name,
                {g_field_expr['capital_stock']} AS capital_stock,
                {g_field_expr['employee_number']} AS employee_number,
                {g_field_expr['date_of_establishment']} AS date_of_establishment,
                {g_field_expr['founding_year']} AS founding_year,
                {g_field_expr['business_summary']} AS business_summary,
                {jsic_codes} AS jsic_codes_raw,
                {business_items} AS business_items_raw,
                {g_field_expr['company_url']} AS company_url,
                {g_field_expr['subsidy_count']} AS subsidy_count,
                {g_field_expr['subsidy_total_amount']} AS subsidy_total_amount,
                {g_field_expr['procurement_count']} AS procurement_count,
                {g_field_expr['procurement_total_award']} AS procurement_total_award,
                {g_field_expr['latest_fiscal_year']} AS latest_fiscal_year,
                {g_field_expr['latest_net_sales']} AS latest_net_sales,
                {g_field_expr['latest_ordinary_income']} AS latest_ordinary_income,
                {g_field_expr['latest_net_income']} AS latest_net_income,
                {g_field_expr['latest_total_assets']} AS latest_total_assets,
                {g_field_expr['latest_net_assets']} AS latest_net_assets,
                {g_field_expr['avg_age']} AS avg_age,
                {g_field_expr['avg_monthly_overtime']} AS avg_monthly_overtime,
                {g_field_expr['female_ratio']} AS female_ratio,
                {h_update_date} AS nta_update_date,
                (h.corporate_number IS NOT NULL) AS source_has_nta,
                (g.corporate_number IS NOT NULL) AS source_has_gbizinfo
            FROM nta h
            FULL OUTER JOIN gbiz g
              ON CAST(h.corporate_number AS VARCHAR) = CAST(g.corporate_number AS VARCHAR)
        """
        con.execute(
            f"""
            CREATE TABLE core.companies AS
            SELECT
                joined.*,
                CASE WHEN regexp_matches(coalesce(joined.jsic_codes_raw, ''), '(^|[|])G([|]|$)')
                     THEN 'G' END AS jsic_major_code,
                CASE WHEN regexp_matches(coalesce(joined.jsic_codes_raw, ''), '(^|[|])G([|]|$)')
                     THEN '情報通信業' END AS jsic_major_name,
                concat_ws('|',
                    CASE WHEN regexp_matches(coalesce(joined.jsic_codes_raw, ''), '(^|[|])G37([|]|$)') THEN '37' END,
                    CASE WHEN regexp_matches(coalesce(joined.jsic_codes_raw, ''), '(^|[|])G38([|]|$)') THEN '38' END,
                    CASE WHEN regexp_matches(coalesce(joined.jsic_codes_raw, ''), '(^|[|])G39([|]|$)') THEN '39' END,
                    CASE WHEN regexp_matches(coalesce(joined.jsic_codes_raw, ''), '(^|[|])G40([|]|$)') THEN '40' END,
                    CASE WHEN regexp_matches(coalesce(joined.jsic_codes_raw, ''), '(^|[|])G41([|]|$)') THEN '41' END
                ) AS jsic_middle_codes,
                current_timestamp AS extracted_at
            FROM ({joined_sql}) joined
            """
        )
        company_count = int(con.execute("SELECT count(*) FROM core.companies").fetchone()[0])
        if company_count <= 0:
            raise PipelineError("全公開スコープの統合結果が 0 件です。公開データを確認してください。")

        con.execute(
            """
            CREATE TABLE core.company_industries AS
            WITH paths AS (
                SELECT
                    c.corporate_number,
                    trim(path_value) AS business_path_raw
                FROM core.companies c
                CROSS JOIN UNNEST(string_split(coalesce(c.business_items_raw, ''), '|')) AS u(path_value)
                WHERE trim(path_value) <> ''
            ), parsed AS (
                SELECT
                    corporate_number,
                    business_path_raw,
                    NULLIF(regexp_extract(business_path_raw, '^([A-T]):', 1), '') AS jsic_major_code,
                    NULLIF(regexp_extract(business_path_raw, '^([A-T]):(.*?)(?:-[0-9]{2}:|$)', 2), '') AS jsic_major_name,
                    NULLIF(regexp_extract(business_path_raw, '-([0-9]{2}):', 1), '') AS jsic_middle_code,
                    NULLIF(regexp_extract(business_path_raw, '-[0-9]{2}:(.*?)(?:-[0-9]{3}:|$)', 1), '') AS jsic_middle_name,
                    NULLIF(regexp_extract(business_path_raw, '-([0-9]{3}):', 1), '') AS jsic_small_code,
                    NULLIF(regexp_extract(business_path_raw, '-[0-9]{3}:(.*)$', 1), '') AS jsic_small_name
                FROM paths
            ), levels AS (
                SELECT
                    corporate_number,
                    jsic_major_code AS jsic_code,
                    jsic_major_code,
                    CAST(NULL AS VARCHAR) AS jsic_middle_code,
                    CAST(NULL AS VARCHAR) AS jsic_small_code,
                    jsic_major_name,
                    CAST(NULL AS VARCHAR) AS jsic_middle_name,
                    CAST(NULL AS VARCHAR) AS jsic_small_name,
                    'major' AS jsic_level,
                    business_path_raw
                FROM parsed
                WHERE jsic_major_code IS NOT NULL
                UNION ALL
                SELECT
                    corporate_number,
                    jsic_major_code || jsic_middle_code AS jsic_code,
                    jsic_major_code,
                    jsic_middle_code,
                    CAST(NULL AS VARCHAR) AS jsic_small_code,
                    jsic_major_name,
                    jsic_middle_name,
                    CAST(NULL AS VARCHAR) AS jsic_small_name,
                    'middle' AS jsic_level,
                    business_path_raw
                FROM parsed
                WHERE jsic_major_code IS NOT NULL AND jsic_middle_code IS NOT NULL
                UNION ALL
                SELECT
                    corporate_number,
                    jsic_major_code || jsic_middle_code || jsic_small_code AS jsic_code,
                    jsic_major_code,
                    jsic_middle_code,
                    jsic_small_code,
                    jsic_major_name,
                    jsic_middle_name,
                    jsic_small_name,
                    'small' AS jsic_level,
                    business_path_raw
                FROM parsed
                WHERE jsic_major_code IS NOT NULL
                  AND jsic_middle_code IS NOT NULL
                  AND jsic_small_code IS NOT NULL
            )
            SELECT DISTINCT * FROM levels
            """
        )
        _create_company_category_index(con)
        con.execute("ALTER TABLE core.companies ADD COLUMN jsic_codes_all_raw VARCHAR")
        con.execute("ALTER TABLE core.companies ADD COLUMN jsic_major_codes_all VARCHAR")
        con.execute("ALTER TABLE core.companies ADD COLUMN jsic_middle_codes_all VARCHAR")
        con.execute(
            """
            UPDATE core.companies AS c
            SET
                jsic_codes_all_raw = categories.jsic_codes_all_raw,
                jsic_major_codes_all = categories.jsic_major_codes_all,
                jsic_middle_codes_all = categories.jsic_middle_codes_all
            FROM (
                SELECT
                    corporate_number,
                    string_agg(DISTINCT jsic_code, '|' ORDER BY jsic_code) AS jsic_codes_all_raw,
                    string_agg(DISTINCT jsic_major_code, '|' ORDER BY jsic_major_code) AS jsic_major_codes_all,
                    string_agg(DISTINCT jsic_middle_code, '|' ORDER BY jsic_middle_code)
                        FILTER (WHERE jsic_middle_code IS NOT NULL) AS jsic_middle_codes_all
                FROM core.company_industries
                GROUP BY corporate_number
            ) AS categories
            WHERE c.corporate_number = categories.corporate_number
            """
        )
        _create_jsic_reference(con)

        con.execute(
            """
            CREATE VIEW core.v_category_summary AS
            SELECT
                jsic_level,
                jsic_major_code,
                jsic_major_name,
                jsic_middle_code,
                jsic_middle_name,
                jsic_small_code,
                jsic_small_name,
                count(DISTINCT corporate_number) AS company_count
            FROM core.company_industries
            GROUP BY 1, 2, 3, 4, 5, 6, 7
            ORDER BY jsic_level, jsic_major_code, jsic_middle_code, jsic_small_code
            """
        )

        con.execute(
            """
            CREATE VIEW core.v_info_communications_strict AS
            SELECT DISTINCT c.*
            FROM core.companies c
            JOIN core.company_industries i USING (corporate_number)
            WHERE i.jsic_major_code = 'G'
            """
        )
        con.execute("CREATE VIEW core.v_info_communications AS SELECT * FROM core.v_info_communications_strict")

        candidate_text = "lower(concat_ws(' ', c.company_name, c.business_summary, c.business_items_raw))"
        candidate_match = " OR ".join(
            f"candidate_text ILIKE '%{token.replace('%', '%%')}%'"
            for token in (
                "saas",
                "ソフトウェア",
                "情報システム",
                "クラウド",
                "アプリ",
                "ウェブ",
                "web",
                "ネットワーク",
                "データセンター",
                "動画制作",
                "映像制作",
                "ゲーム",
                "通信",
            )
        )
        candidate_keywords = "concat_ws('|', " + ", ".join(
            f"CASE WHEN candidate_text ILIKE '%{token.replace('%', '%%')}%' THEN '{token}' END"
            for token in (
                "SaaS",
                "ソフトウェア",
                "情報システム",
                "クラウド",
                "アプリ",
                "ウェブ",
                "Web",
                "ネットワーク",
                "データセンター",
                "動画制作",
                "映像制作",
                "ゲーム",
                "通信",
            )
        ) + ")"
        con.execute(
            f"""
            CREATE VIEW core.v_info_communications_candidates AS
            WITH candidates AS (
                SELECT c.*, {candidate_text} AS candidate_text
                FROM core.companies c
                WHERE NOT EXISTS (
                    SELECT 1 FROM core.v_info_communications_strict s
                    WHERE s.corporate_number = c.corporate_number
                )
            )
            SELECT
                candidates.* EXCLUDE (candidate_text),
                {candidate_keywords} AS candidate_matched_keywords,
                'keyword' AS candidate_method
            FROM candidates
            WHERE {candidate_match}
            """
        )

        joinable_public_keys: list[str] = []
        source_record_selects: list[str] = []
        for table_key, spec in PUBLIC_TABLES.items():
            join_column = spec.get("join_column")
            if not join_column or join_column not in local_columns[table_key]:
                continue
            relation = local_relations[table_key]
            quoted_column = _quote_identifier(str(join_column))
            safe_key = "".join(
                character if character.isalnum() or character == "_" else "_"
                for character in table_key
            )
            index_name = f"idx_public_{safe_key}_{join_column}"
            con.execute(
                f"CREATE INDEX {_quote_identifier(index_name)} "
                f"ON {relation} ({quoted_column})"
            )
            joinable_public_keys.append(table_key)
            source_record_selects.append(
                "SELECT "
                f"'{table_key}' AS source_key, "
                f"'{spec['source_table']}' AS source_table, "
                f"CAST({quoted_column} AS VARCHAR) AS corporate_number "
                f"FROM {relation} "
                f"WHERE {quoted_column} IS NOT NULL "
                f"AND trim(CAST({quoted_column} AS VARCHAR)) <> ''"
            )

        if source_record_selects:
            con.execute(
                """
                CREATE VIEW core.v_company_source_records AS
                """
                + " UNION ALL ".join(source_record_selects)
            )
            con.execute(
                """
                CREATE VIEW core.v_company_source_counts AS
                SELECT
                    r.source_key,
                    r.source_table,
                    r.corporate_number,
                    count(*) AS source_record_count,
                    (c.corporate_number IS NOT NULL) AS matched_to_company
                FROM core.v_company_source_records r
                LEFT JOIN core.companies c USING (corporate_number)
                GROUP BY 1, 2, 3, 5
                """
            )
        else:
            con.execute(
                """
                CREATE VIEW core.v_company_source_records AS
                SELECT
                    CAST(NULL AS VARCHAR) AS source_key,
                    CAST(NULL AS VARCHAR) AS source_table,
                    CAST(NULL AS VARCHAR) AS corporate_number
                WHERE FALSE
                """
            )
            con.execute(
                """
                CREATE VIEW core.v_company_source_counts AS
                SELECT
                    CAST(NULL AS VARCHAR) AS source_key,
                    CAST(NULL AS VARCHAR) AS source_table,
                    CAST(NULL AS VARCHAR) AS corporate_number,
                    CAST(NULL AS BIGINT) AS source_record_count,
                    CAST(NULL AS BOOLEAN) AS matched_to_company
                WHERE FALSE
                """
            )

        fact_keys = [
            "gbizinfo_subsidy",
            "gbizinfo_procurement",
            "gbizinfo_patent",
            "gbizinfo_certification",
            "gbizinfo_commendation",
        ]
        activity_selects: list[str] = []
        for fact_key in fact_keys:
            spec = PUBLIC_TABLES[fact_key]
            relation = f"{spec['schema']}.{spec['table']}"
            activity_type = fact_key.removeprefix("gbizinfo_")
            activity_selects.append(
                f"SELECT CAST(corporate_number AS VARCHAR) AS corporate_number, '{activity_type}' AS activity_type FROM {relation}"
            )
        con.execute(
            f"""
            CREATE VIEW core.v_company_activity AS
            WITH activity AS (
                {' UNION ALL '.join(activity_selects)}
            ), counts AS (
                SELECT
                    corporate_number,
                    count(*) FILTER (WHERE activity_type = 'subsidy') AS subsidy_record_count,
                    count(*) FILTER (WHERE activity_type = 'procurement') AS procurement_record_count,
                    count(*) FILTER (WHERE activity_type = 'patent') AS patent_record_count,
                    count(*) FILTER (WHERE activity_type = 'certification') AS certification_record_count,
                    count(*) FILTER (WHERE activity_type = 'commendation') AS commendation_record_count
                FROM activity
                GROUP BY corporate_number
            )
            SELECT
                c.corporate_number,
                coalesce(counts.subsidy_record_count, 0) AS subsidy_record_count,
                coalesce(counts.procurement_record_count, 0) AS procurement_record_count,
                coalesce(counts.patent_record_count, 0) AS patent_record_count,
                coalesce(counts.certification_record_count, 0) AS certification_record_count,
                coalesce(counts.commendation_record_count, 0) AS commendation_record_count
            FROM core.companies c
            LEFT JOIN counts USING (corporate_number)
            """
        )
        con.execute(
            """
            CREATE VIEW core.v_info_communications_with_activity AS
            SELECT s.*, a.* EXCLUDE (corporate_number)
            FROM core.v_info_communications_strict s
            LEFT JOIN core.v_company_activity a USING (corporate_number)
            """
        )
        con.execute(
            """
            CREATE VIEW core.v_data_quality AS
            SELECT
                count(*) AS company_count,
                count_if(source_has_nta) AS with_nta,
                count_if(source_has_gbizinfo) AS with_gbizinfo,
                count_if(company_name IS NOT NULL AND trim(company_name) <> '') AS with_name,
                count_if(prefecture_name IS NOT NULL) AS with_prefecture,
                count_if(representative_name IS NOT NULL) AS with_representative,
                count_if(capital_stock IS NOT NULL) AS with_capital,
                count_if(employee_number IS NOT NULL) AS with_employee_number,
                count_if(company_url IS NOT NULL AND trim(company_url) <> '') AS with_company_url,
                count_if(business_summary IS NOT NULL AND trim(business_summary) <> '') AS with_business_summary,
                count_if(business_items_raw IS NOT NULL AND trim(business_items_raw) <> '') AS with_business_items,
                count_if(jsic_codes_all_raw IS NOT NULL AND trim(jsic_codes_all_raw) <> '') AS with_jsic_codes,
                count_if(jsic_major_code = 'G') AS strict_info_communications
            FROM core.companies
            """
        )

        con.execute(
            """
            CREATE TABLE meta.source_registry (
                source_name VARCHAR,
                queria_dataset VARCHAR,
                table_name VARCHAR,
                role VARCHAR,
                source_url VARCHAR,
                license_name VARCHAR,
                attribution VARCHAR
            )
            """
        )
        registry_rows = [
            (
                item["source_name"],
                item.get("queria_dataset"),
                item.get("table_name"),
                item.get("role"),
                item.get("source_url"),
                item.get("license_name"),
                item.get("attribution"),
            )
            for item in _load_source_registry()
        ]
        con.executemany("INSERT INTO meta.source_registry VALUES (?, ?, ?, ?, ?, ?, ?)", registry_rows)

        con.execute(
            """
            CREATE TABLE meta.coverage_boundary (
                scope_key VARCHAR,
                item_name VARCHAR,
                status VARCHAR,
                detail VARCHAR,
                source_url VARCHAR
            )
            """
        )
        con.executemany(
            "INSERT INTO meta.coverage_boundary VALUES (?, ?, ?, ?, ?)",
            [
                (
                    "company_master",
                    "国税庁法人番号＋gBizINFO法人サマリー",
                    "complete_source_snapshot",
                    "現行のQueria公開テーブルを法人番号で統合した全件マスタ。NTAのみ・gBizINFOのみの法人も保持。",
                    "https://www.houjin-bangou.nta.go.jp/download/zenken/",
                ),
                (
                    "gbizinfo_activity",
                    "Queriaが現在公開するgBizINFO活動5テーブル",
                    "complete_source_snapshot",
                    "補助金・調達・特許等・届出認定・表彰を明細粒度で保持。",
                    "https://info.gbiz.go.jp/hojin/DownloadTop",
                ),
                (
                    "gbizinfo_financial_detail",
                    "gBizINFO財務情報の生明細",
                    "summary_only",
                    "現行Queriaカタログでは生明細テーブルを公開していないため、company_summaryのlatest_*指標のみ収録。",
                    "https://info.gbiz.go.jp/hojin/DownloadTop",
                ),
                (
                    "gbizinfo_workplace_detail",
                    "gBizINFO職場情報の生明細",
                    "summary_only",
                    "現行Queriaカタログでは生明細テーブルを公開していないため、company_summaryのavg_age等の最新指標のみ収録。",
                    "https://info.gbiz.go.jp/hojin/DownloadTop",
                ),
                (
                    "edinet",
                    "EDINET会社・提出書類・財務ファクト",
                    "complete_source_snapshot",
                    "Queriaが現在公開するEDINETの会社マスター、提出書類、主要業績、財務ファクト、ファンドマスターを現行スナップショットとして保持。EDINET全期間・全書類の完全複製ではなく、Queria公開範囲に限定。",
                    "https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/download/ESE140206.pdf",
                ),
                (
                    "mhlw",
                    "厚生労働省の法人関連・地域統計",
                    "complete_source_snapshot",
                    "女性活躍企業、介護事業所、障害福祉事業所、NDB特定健診統計をQueria公開範囲で保持。法人番号を持つテーブルはsource_countsで法人マスターへ結合でき、NDB統計は地域集計のまま保持。",
                    "https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/0000177182.html",
                ),
                (
                    "p_portal",
                    "調達ポータル落札実績",
                    "complete_source_snapshot",
                    "政府電子調達システムの落札実績をQueria公開範囲で明細保持。法人番号で法人マスターに結合可能。",
                    "https://www.p-portal.go.jp/pps-web-biz/UAB02/OAB0201",
                ),
                (
                    "metro_tokyo",
                    "東京都オープンデータの法人関連テーブル",
                    "selected_source_snapshot",
                    "東京都ODSから介護、文化財、イベント、食品営業、公共施設、支援制度、観光の選定テーブルを収録。東京都の全カタログ・全自治体データの完全収録ではない。",
                    "https://www.digital.go.jp/resources/open_data/municipal-standard-data-set-test",
                ),
                (
                    "model_person_profiles",
                    "モデル・個人プロフィール情報",
                    "not_in_scope",
                    "現行の公開法人データセットに対応する公式モデル・個人プロフィール源は含めていない。",
                    "https://docs.queria.io/en/architecture/",
                ),
            ],
        )

        con.execute("CREATE TABLE meta.source_metadata (dataset_name VARCHAR, metadata_json JSON)")
        con.executemany(
            "INSERT INTO meta.source_metadata VALUES (?, ?)",
            [
                (name, json.dumps(value, ensure_ascii=False))
                for name, value in source_metadata.items()
            ],
        )
        con.execute(
            """
            CREATE TABLE meta.dataset_row_counts (
                source_table VARCHAR,
                local_schema VARCHAR,
                local_table VARCHAR,
                row_count BIGINT,
                parquet_bytes BIGINT,
                parquet_sha256 VARCHAR
            )
            """
        )
        con.executemany(
            "INSERT INTO meta.dataset_row_counts VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    record["source_table"],
                    record["local_schema"],
                    record["local_table"],
                    record["row_count"],
                    record["bytes"],
                    record["sha256"],
                )
                for record in local_stats
            ],
        )
        con.execute(
            """
            CREATE TABLE meta.refresh_log (
                refresh_id VARCHAR,
                scope VARCHAR,
                started_at TIMESTAMPTZ,
                completed_at TIMESTAMPTZ,
                row_count BIGINT,
                parquet_bytes BIGINT,
                parquet_sha256 VARCHAR,
                queria_client_version VARCHAR,
                project_version VARCHAR,
                artifact_count INTEGER,
                artifact_manifest_json JSON
            )
            """
        )
        con.execute(
            "INSERT INTO meta.refresh_log VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                refresh_id,
                ALL_PUBLIC_SCOPE,
                started_at,
                completed_at,
                company_count,
                total_bytes,
                manifest_sha,
                _client_version(),
                PROJECT_VERSION,
                len(manifest_records),
                json.dumps(local_stats, ensure_ascii=False),
            ],
        )

        con.execute("CREATE INDEX idx_companies_corporate_number ON core.companies(corporate_number)")
        con.execute("CREATE INDEX idx_companies_prefecture_code ON core.companies(prefecture_code)")
        con.execute("CREATE INDEX idx_industries_number ON core.company_industries(corporate_number)")
        con.execute("CREATE INDEX idx_industries_major ON core.company_industries(jsic_major_code)")
        con.execute("CREATE INDEX idx_industries_middle ON core.company_industries(jsic_middle_code)")
        con.execute("CHECKPOINT")
        build_succeeded = True
    finally:
        con.close()
        if not build_succeeded:
            temp_db.unlink(missing_ok=True)
            temp_wal.unlink(missing_ok=True)

    try:
        os.replace(temp_db, database_path)
    except PermissionError as exc:
        raise PipelineError(
            f"DB を置換できません。{database_path} を開いている DuckDB/BI ツールを閉じて再実行してください。"
        ) from exc
    temp_wal.unlink(missing_ok=True)
    return company_count, total_bytes, manifest_sha, local_stats


def collect_source_metadata(*, all_public: bool = False) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "captured_at": _utc_now(),
        "gbizinfo": _capture_json(["info", "gbizinfo"]),
        "houjin_bangou": _capture_json(["info", "houjin_bangou"]),
        "gbizinfo_columns": _capture_json(["columns", "gbizinfo", "mart_gbizinfo_company"]),
        "houjin_bangou_columns": _capture_json(["columns", "houjin_bangou", "mart_houjin_bangou"]),
    }
    if all_public:
        metadata["public_tables"] = {
            table_key: {
                "dataset": spec["dataset"],
                "source_table": spec["source_table"],
                "columns": _capture_json(
                    ["columns", spec["dataset"], spec["source_table"].split(".")[-1]]
                ),
            }
            for table_key, spec in PUBLIC_TABLES.items()
        }
    return metadata


def _promote_public_cache(staged_paths: Mapping[str, Path], cache_dir: Path) -> tuple[Path, ...]:
    """Promote a complete table set to a new cache directory."""
    latest_dir = cache_dir / f"{ALL_PUBLIC_SCOPE}-latest"
    next_dir = cache_dir / f".{ALL_PUBLIC_SCOPE}-latest-{uuid4().hex}.partial"
    next_dir.mkdir(parents=True, exist_ok=False)
    try:
        promoted: list[Path] = []
        for table_key, path in staged_paths.items():
            target = next_dir / path.name
            os.replace(path, target)
            promoted.append(target)
        if latest_dir.exists():
            shutil.rmtree(latest_dir)
        os.replace(next_dir, latest_dir)
        return tuple(latest_dir / path.name for path in promoted)
    except Exception:
        shutil.rmtree(next_dir, ignore_errors=True)
        raise


def _refresh_all_public(
    *,
    database_path: Path,
    cache_dir: Path,
    keep_cache: bool,
) -> RefreshResult:
    started_at = _utc_now()
    cache_dir.mkdir(parents=True, exist_ok=True)
    run_dir = cache_dir / f"run-{int(time.time())}-{os.getpid()}-{uuid4().hex[:8]}"
    run_dir.mkdir(parents=True, exist_ok=False)
    try:
        staged_paths = export_public_tables(run_dir)
        source_metadata = collect_source_metadata(all_public=True)
        metadata_path = run_dir / "source_metadata.json"
        metadata_path.write_text(
            json.dumps(source_metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        row_count, parquet_bytes, parquet_sha, _local_stats = build_all_public_database(
            staged_paths,
            database_path,
            started_at=started_at,
            source_metadata=source_metadata,
        )
        promoted_paths: tuple[Path, ...] = ()
        if keep_cache:
            promoted_paths = _promote_public_cache(staged_paths, cache_dir)
        final_metadata = database_path.parent / "source_metadata.json"
        final_metadata.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(metadata_path, final_metadata)
        return RefreshResult(
            database_path=database_path,
            parquet_path=cache_dir / f"{ALL_PUBLIC_SCOPE}-latest" if keep_cache else None,
            scope=ALL_PUBLIC_SCOPE,
            row_count=row_count,
            parquet_bytes=parquet_bytes,
            parquet_sha256=parquet_sha,
            artifact_paths=promoted_paths,
        )
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def refresh(
    *,
    scope: str,
    database_path: Path = DEFAULT_DB,
    cache_dir: Path = DEFAULT_CACHE,
    keep_cache: bool = True,
) -> RefreshResult:
    scope = normalize_scope(scope)
    if scope == ALL_PUBLIC_SCOPE:
        return _refresh_all_public(
            database_path=database_path,
            cache_dir=cache_dir,
            keep_cache=keep_cache,
        )
    started_at = _utc_now()
    cache_dir.mkdir(parents=True, exist_ok=True)
    run_dir = cache_dir / f"run-{int(time.time())}-{os.getpid()}"
    run_dir.mkdir(parents=True, exist_ok=False)
    staged_parquet = run_dir / f"{scope}.parquet"
    latest_parquet = cache_dir / f"{scope}-latest.parquet"

    try:
        export_remote(scope, staged_parquet)
        source_metadata = collect_source_metadata()
        metadata_path = run_dir / "source_metadata.json"
        metadata_path.write_text(
            json.dumps(source_metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        parquet_bytes = staged_parquet.stat().st_size
        row_count, parquet_sha = build_local_database(
            staged_parquet,
            database_path,
            scope=scope,
            started_at=started_at,
            source_metadata=source_metadata,
        )
        if keep_cache:
            os.replace(staged_parquet, latest_parquet)
        else:
            staged_parquet.unlink(missing_ok=True)
        final_metadata = database_path.parent / "source_metadata.json"
        shutil.copy2(metadata_path, final_metadata)
        return RefreshResult(
            database_path=database_path,
            parquet_path=latest_parquet if keep_cache else None,
            scope=scope,
            row_count=row_count,
            parquet_bytes=parquet_bytes,
            parquet_sha256=parquet_sha,
        )
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def online_probe() -> dict[str, Any]:
    return _capture_json(["info", "gbizinfo"])


def version_report() -> dict[str, str]:
    report = {"python": sys.version.split()[0], "project": PROJECT_VERSION}
    for package in ("duckdb", "queria"):
        try:
            report[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            report[package] = "not-installed"
    try:
        report["queria_executable"] = str(find_queria_executable())
    except PipelineError as exc:
        report["queria_executable"] = f"missing: {exc}"
    return report
