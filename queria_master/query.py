from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any, Iterable, Sequence

from .pipeline import PipelineError
from .resources import DEFAULT_DB
from .search_index import DEFAULT_SEARCH_INDEX, SearchIndex, SearchIndexError
from .semantic_index import (
    DEFAULT_SEMANTIC_INDEX,
    SemanticIndex,
    SentenceTransformerProvider,
    SemanticIndexError,
    doc_ids_for_corporate_numbers,
    hydrate_semantic_hits,
)


READ_ONLY_PREFIXES = {
    "SELECT",
    "WITH",
    "DESCRIBE",
    "DESC",
    "SHOW",
    "EXPLAIN",
    "SUMMARIZE",
    "VALUES",
    "TABLE",
    "FROM",
}

FORBIDDEN_KEYWORDS = {
    "INSERT",
    "UPDATE",
    "DELETE",
    "CREATE",
    "DROP",
    "ALTER",
    "COPY",
    "ATTACH",
    "DETACH",
    "INSTALL",
    "LOAD",
    "CALL",
    "EXPORT",
    "IMPORT",
    "SET",
    "RESET",
    "VACUUM",
    "CHECKPOINT",
    "BEGIN",
    "COMMIT",
    "ROLLBACK",
    "TRUNCATE",
    "MERGE",
    "REPLACE",
    "GRANT",
    "REVOKE",
}


def _duckdb_module():
    try:
        import duckdb  # type: ignore
    except ImportError as exc:
        raise PipelineError("duckdb がありません。先にセットアップを実行してください。") from exc
    return duckdb


def _open_database(path: Path = DEFAULT_DB, *, read_only: bool = True):
    if not path.is_file():
        raise PipelineError(f"DB がありません: {path}\n先に refresh を実行してください。")
    return _duckdb_module().connect(str(path), read_only=read_only)


def _sql_string(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _strip_sql_comments(sql: str) -> str:
    """Remove -- and /* */ comments while preserving quoted SQL text."""
    out: list[str] = []
    index = 0
    state = "normal"
    while index < len(sql):
        char = sql[index]
        nxt = sql[index + 1] if index + 1 < len(sql) else ""
        if state == "normal":
            if char == "-" and nxt == "-":
                state = "line_comment"
                out.append(" ")
                index += 2
                continue
            if char == "/" and nxt == "*":
                state = "block_comment"
                out.append(" ")
                index += 2
                continue
            if char == "'":
                state = "single_quote"
            elif char == '"':
                state = "double_quote"
            out.append(char)
            index += 1
            continue
        if state == "line_comment":
            if char == "\n":
                out.append("\n")
                state = "normal"
            index += 1
            continue
        if state == "block_comment":
            if char == "*" and nxt == "/":
                state = "normal"
                out.append(" ")
                index += 2
                continue
            if char == "\n":
                out.append("\n")
            index += 1
            continue
        if state == "single_quote":
            out.append(char)
            if char == "'":
                if nxt == "'":
                    out.append(nxt)
                    index += 2
                    continue
                state = "normal"
            index += 1
            continue
        if state == "double_quote":
            out.append(char)
            if char == '"':
                if nxt == '"':
                    out.append(nxt)
                    index += 2
                    continue
                state = "normal"
            index += 1
            continue
    if state == "block_comment":
        raise PipelineError("SQL のブロックコメントが閉じられていません。")
    return "".join(out)


def _split_sql_statements(sql: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    index = 0
    state = "normal"
    while index < len(sql):
        char = sql[index]
        nxt = sql[index + 1] if index + 1 < len(sql) else ""
        if state == "normal":
            if char == "'":
                state = "single_quote"
            elif char == '"':
                state = "double_quote"
            elif char == ";":
                statement = "".join(current).strip()
                if statement:
                    statements.append(statement)
                current = []
                index += 1
                continue
            current.append(char)
            index += 1
            continue
        current.append(char)
        if state == "single_quote" and char == "'":
            if nxt == "'":
                current.append(nxt)
                index += 2
                continue
            state = "normal"
        elif state == "double_quote" and char == '"':
            if nxt == '"':
                current.append(nxt)
                index += 2
                continue
            state = "normal"
        index += 1
    statement = "".join(current).strip()
    if statement:
        statements.append(statement)
    return statements


def _mask_quoted_text(sql: str) -> str:
    out: list[str] = []
    index = 0
    state = "normal"
    while index < len(sql):
        char = sql[index]
        nxt = sql[index + 1] if index + 1 < len(sql) else ""
        if state == "normal":
            if char == "'":
                state = "single_quote"
                out.append(" ")
            elif char == '"':
                state = "double_quote"
                out.append(" ")
            else:
                out.append(char)
            index += 1
            continue
        out.append("\n" if char == "\n" else " ")
        if state == "single_quote" and char == "'":
            if nxt == "'":
                out.append(" ")
                index += 2
                continue
            state = "normal"
        elif state == "double_quote" and char == '"':
            if nxt == '"':
                out.append(" ")
                index += 2
                continue
            state = "normal"
        index += 1
    return "".join(out)


def normalize_read_only_sql(sql: str) -> str:
    without_comments = _strip_sql_comments(sql)
    statements = _split_sql_statements(without_comments)
    if not statements:
        raise PipelineError("SQL が空です。")
    if len(statements) != 1:
        raise PipelineError("SQL は 1 文だけ指定してください。")
    statement = statements[0].strip()
    masked = _mask_quoted_text(statement)
    first_match = re.match(r"\s*([A-Za-z]+)", masked)
    if first_match is None:
        raise PipelineError("SQL の先頭トークンを判定できません。")
    first = first_match.group(1).upper()
    if first not in READ_ONLY_PREFIXES:
        raise PipelineError(f"読み取り専用 SQL だけ実行できます。先頭トークン: {first}")
    forbidden = re.search(
        r"\b(" + "|".join(sorted(FORBIDDEN_KEYWORDS)) + r")\b",
        masked,
        flags=re.IGNORECASE,
    )
    if forbidden:
        raise PipelineError(f"読み取り専用 SQL では使えないキーワードです: {forbidden.group(1).upper()}")
    return statement


def validate_read_only(sql: str) -> None:
    normalize_read_only_sql(sql)


def _format_cell(value: Any, max_width: int = 48) -> str:
    if value is None:
        return "NULL"
    text = str(value).replace("\n", " ").replace("\r", " ")
    return text if len(text) <= max_width else text[: max_width - 1] + "…"


def print_table(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    if not columns:
        print("(no columns)")
        return
    rendered = [[_format_cell(value) for value in row] for row in rows]
    widths = [len(str(column)) for column in columns]
    for row in rendered:
        for index, value in enumerate(row):
            widths[index] = min(48, max(widths[index], len(value)))
    header = " | ".join(str(column).ljust(widths[i]) for i, column in enumerate(columns))
    rule = "-+-".join("-" * width for width in widths)
    print(header)
    print(rule)
    for row in rendered:
        print(" | ".join(value.ljust(widths[i]) for i, value in enumerate(row)))
    print(f"\n{len(rows)} row(s)")


def _write_rows(path: Path, columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    rows_list = list(rows)
    if suffix == ".csv":
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(columns)
            writer.writerows(rows_list)
    elif suffix == ".json":
        records = [dict(zip(columns, row)) for row in rows_list]
        path.write_text(json.dumps(records, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    elif suffix == ".jsonl":
        with path.open("w", encoding="utf-8") as handle:
            for row in rows_list:
                handle.write(json.dumps(dict(zip(columns, row)), ensure_ascii=False, default=str) + "\n")
    else:
        raise PipelineError("行出力は .csv / .json / .jsonl を指定してください。")


def search_companies(
    *,
    db_path: Path = DEFAULT_DB,
    keyword: str | None = None,
    prefecture: str | None = None,
    city: str | None = None,
    industry_majors: Sequence[str] = (),
    industry_middles: Sequence[str] = (),
    min_employees: int | None = None,
    max_employees: int | None = None,
    min_capital: int | None = None,
    max_capital: int | None = None,
    has_web: bool = False,
    limit: int = 100,
    out: Path | None = None,
    search_index: Path | None = DEFAULT_SEARCH_INDEX,
    fast: bool = False,
) -> int:
    if not 1 <= limit <= 100_000:
        raise PipelineError("--limit は 1〜100000 の範囲で指定してください。")
    normalized_majors = tuple(str(value).strip().upper() for value in industry_majors)
    invalid_majors = sorted({value for value in normalized_majors if not re.fullmatch(r"[A-T]", value)})
    if invalid_majors:
        raise PipelineError(f"大分類コードは A〜T の1文字です。不正値: {', '.join(invalid_majors)}")
    normalized_middles = tuple(str(value).strip() for value in industry_middles)
    invalid_middles = sorted({value for value in normalized_middles if not re.fullmatch(r"[0-9]{2}", value)})
    if invalid_middles:
        raise PipelineError(f"中分類コードは2桁の数字です。不正値: {', '.join(invalid_middles)}")

    # A keyword query is the expensive path in DuckDB because the legacy
    # implementation must scan several large text columns with ILIKE.  Use
    # the immutable SQLite FTS sidecar when it is present and compatible;
    # Parquet output still uses DuckDB so the existing export contract stays
    # unchanged.  A stale/missing sidecar falls back to the canonical DB.
    if (keyword or fast) and (out is None or out.suffix.lower() in {".csv", ".json", ".jsonl"}):
        index_path = Path(search_index) if search_index is not None else DEFAULT_SEARCH_INDEX
        if index_path.is_file():
            try:
                with SearchIndex(index_path, database_path=db_path) as index:
                    hits = index.search(
                        keyword,
                        prefecture=prefecture,
                        city=city,
                        industry_majors=normalized_majors,
                        industry_middles=normalized_middles,
                        min_employees=min_employees,
                        max_employees=max_employees,
                        min_capital=min_capital,
                        max_capital=max_capital,
                        has_web=has_web,
                        limit=limit,
                        fast=fast,
                    )
            except SearchIndexError:
                hits = None
            if hits is not None:
                columns = [
                    "corporate_number",
                    "company_name",
                    "prefecture_name",
                    "city_name",
                    "jsic_major_codes",
                    "jsic_middle_codes",
                    "employee_number",
                    "capital_stock",
                    "representative_name",
                    "company_url",
                    "business_summary",
                    "phone",
                    "email",
                    "inquiry_form_url",
                ]
                rows = [tuple(hit.get(column) for column in columns) for hit in hits]
                if out is None:
                    print_table(columns, rows)
                else:
                    _write_rows(out, columns, rows)
                    print(f"出力しました: {out} ({len(rows)} rows)")
                return len(rows)

    conditions: list[str] = ["1 = 1"]
    params: list[Any] = []
    if keyword:
        conditions.append(
            "(coalesce(c.company_name, '') ILIKE ? OR coalesce(c.business_summary, '') ILIKE ? OR coalesce(c.business_items_raw, '') ILIKE ? OR coalesce(c.company_url, '') ILIKE ?)"
        )
        token = f"%{keyword}%"
        params.extend([token, token, token, token])
    if prefecture:
        conditions.append("c.prefecture_name = ?")
        params.append(prefecture)
    if city:
        conditions.append("c.city_name ILIKE ?")
        params.append(f"%{city}%")
    if min_employees is not None:
        conditions.append("c.employee_number >= ?")
        params.append(min_employees)
    if max_employees is not None:
        conditions.append("c.employee_number <= ?")
        params.append(max_employees)
    if min_capital is not None:
        conditions.append("c.capital_stock >= ?")
        params.append(min_capital)
    if max_capital is not None:
        conditions.append("c.capital_stock <= ?")
        params.append(max_capital)
    if has_web:
        conditions.append("c.company_url IS NOT NULL AND trim(c.company_url) <> ''")
    if normalized_majors:
        placeholders = ", ".join("?" for _ in normalized_majors)
        conditions.append(
            f"c.corporate_number IN (SELECT DISTINCT i.corporate_number FROM core.company_category_index i WHERE i.jsic_major_code IN ({placeholders}))"
        )
        params.extend(normalized_majors)
    if normalized_middles:
        placeholders = ", ".join("?" for _ in normalized_middles)
        conditions.append(
            f"c.corporate_number IN (SELECT DISTINCT i.corporate_number FROM core.company_category_index i WHERE i.jsic_middle_code IN ({placeholders}))"
        )
        params.extend(normalized_middles)

    parquet_output = out is not None and out.suffix.lower() == ".parquet"
    con = _open_database(db_path, read_only=not parquet_output)
    try:
        company_columns = {
            str(row[0]) for row in con.execute("DESCRIBE core.companies").fetchall()
        }
        major_codes_column = (
            "c.jsic_major_codes_all" if "jsic_major_codes_all" in company_columns else "c.jsic_major_code"
        )
        middle_codes_column = (
            "c.jsic_middle_codes_all" if "jsic_middle_codes_all" in company_columns else "c.jsic_middle_codes"
        )
        sql = f"""
            SELECT
                c.corporate_number,
                c.company_name,
                c.prefecture_name,
                c.city_name,
                {major_codes_column} AS jsic_major_codes,
                {middle_codes_column} AS jsic_middle_codes,
                c.employee_number,
                c.capital_stock,
                c.representative_name,
                c.company_url,
                c.business_summary
            FROM core.companies c
            WHERE {' AND '.join(conditions)}
            ORDER BY c.employee_number DESC NULLS LAST, c.capital_stock DESC NULLS LAST, c.company_name
            LIMIT {limit}
        """
        cursor = con.execute(sql, params)
        columns = [item[0] for item in cursor.description]
        rows = cursor.fetchall()
        if out is None:
            print_table(columns, rows)
        elif out.suffix.lower() == ".parquet":
            con.execute("CREATE TEMP TABLE _search_result AS " + sql, params)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.unlink(missing_ok=True)
            con.execute(f"COPY _search_result TO {_sql_string(out.resolve())} (FORMAT PARQUET)")
            print(f"出力しました: {out} ({len(rows)} rows)")
        else:
            _write_rows(out, columns, rows)
            print(f"出力しました: {out} ({len(rows)} rows)")
        return len(rows)
    finally:
        con.close()


def semantic_search_companies(
    *,
    query_text: str,
    model_name: str | None = None,
    search_index: Path = DEFAULT_SEARCH_INDEX,
    semantic_index: Path = DEFAULT_SEMANTIC_INDEX,
    candidate_keyword: str | None = None,
    prefecture: str | None = None,
    city: str | None = None,
    industry_majors: Sequence[str] = (),
    industry_middles: Sequence[str] = (),
    has_web: bool = False,
    candidate_limit: int = 20_000,
    limit: int = 100,
    out: Path | None = None,
    device: str | None = None,
) -> int:
    """Run optional embedding search after an FTS candidate reduction."""

    if not query_text.strip():
        raise PipelineError("埋め込み検索文を指定してください。")
    if not 1 <= limit <= 100_000:
        raise PipelineError("--limit は1〜100000の範囲で指定してください。")
    if not 1 <= candidate_limit <= 100_000:
        raise PipelineError("--candidate-limit は1〜100000の範囲で指定してください。")
    try:
        with SemanticIndex(semantic_index, search_index_path=search_index) as vector_index:
            selected_model = model_name or str(vector_index.metadata.get("model_name", ""))
            if not selected_model:
                raise SemanticIndexError("モデル名が指定されていません。")
            provider = SentenceTransformerProvider(selected_model, device=device)
            candidate_doc_ids: list[int] | None = None
            if candidate_keyword:
                with SearchIndex(search_index) as index:
                    candidates = index.search(
                        candidate_keyword,
                        prefecture=prefecture,
                        city=city,
                        industry_majors=industry_majors,
                        industry_middles=industry_middles,
                        has_web=has_web,
                        limit=candidate_limit,
                        fast=True,
                    )
                candidate_doc_ids = doc_ids_for_corporate_numbers(
                    search_index,
                    [str(item["corporate_number"]) for item in candidates],
                )
            hits = vector_index.search(
                query_text,
                provider,
                top_k=limit,
                candidate_doc_ids=candidate_doc_ids,
            )
        records = hydrate_semantic_hits(search_index, hits)
    except SemanticIndexError as exc:
        raise PipelineError(str(exc)) from exc

    columns = [
        "corporate_number",
        "company_name",
        "prefecture_name",
        "city_name",
        "jsic_major_codes",
        "jsic_middle_codes",
        "employee_number",
        "capital_stock",
        "representative_name",
        "company_url",
        "business_summary",
        "semantic_score",
    ]
    rows = [tuple(record.get(column) for column in columns) for record in records]
    if out is None:
        print_table(columns, rows)
    else:
        _write_rows(out, columns, rows)
        print(f"出力しました: {out} ({len(rows)} rows)")
    return len(rows)


def show_summary(db_path: Path = DEFAULT_DB) -> None:
    con = _open_database(db_path)
    try:
        print("[収録件数・充足率]")
        cursor = con.execute("SELECT * FROM core.v_data_quality")
        print_table([item[0] for item in cursor.description], cursor.fetchall())

        print("\n[都道府県別 上位20]")
        cursor = con.execute(
            """
            SELECT coalesce(prefecture_name, '(不明)') AS prefecture_name, count(*) AS companies
            FROM core.companies GROUP BY 1 ORDER BY 2 DESC LIMIT 20
            """
        )
        print_table([item[0] for item in cursor.description], cursor.fetchall())

        industry_columns = {
            str(row[0]) for row in con.execute("DESCRIBE core.company_industries").fetchall()
        }
        if "jsic_level" in industry_columns:
            print("\n[大分類別 上位30 — 複数業種法人は重複計上]")
            cursor = con.execute(
                """
                SELECT
                    jsic_major_code,
                    max(jsic_major_name) AS jsic_major_name,
                    count(DISTINCT corporate_number) AS companies
                FROM core.company_industries
                WHERE jsic_level = 'major'
                GROUP BY 1 ORDER BY companies DESC, jsic_major_code LIMIT 30
                """
            )
            print_table([item[0] for item in cursor.description], cursor.fetchall())

            print("\n[中分類別 上位50 — 複数業種法人は重複計上]")
            cursor = con.execute(
                """
                SELECT
                    jsic_major_code,
                    jsic_middle_code,
                    max(jsic_major_name) AS jsic_major_name,
                    max(jsic_middle_name) AS jsic_middle_name,
                    count(DISTINCT corporate_number) AS companies
                FROM core.company_industries
                WHERE jsic_level = 'middle'
                GROUP BY 1, 2 ORDER BY companies DESC, jsic_major_code, jsic_middle_code LIMIT 50
                """
            )
            print_table([item[0] for item in cursor.description], cursor.fetchall())
        else:
            print("\n[情報通信業 中分類別 — 複数業種法人は重複計上]")
            cursor = con.execute(
                """
                SELECT
                    i.jsic_middle_code,
                    m.middle_name,
                    count(DISTINCT i.corporate_number) AS companies
                FROM core.company_industries i
                LEFT JOIN meta.jsic_info_communications m
                  ON i.jsic_middle_code = m.middle_code
                WHERE i.jsic_major_code = 'G'
                GROUP BY 1, 2 ORDER BY 1
                """
            )
            print_table([item[0] for item in cursor.description], cursor.fetchall())
    finally:
        con.close()


def run_local_sql(
    sql: str,
    *,
    db_path: Path = DEFAULT_DB,
    max_rows: int = 200,
    out: Path | None = None,
) -> None:
    statement = normalize_read_only_sql(sql)
    con = _open_database(db_path)
    try:
        if out is not None:
            if out.suffix.lower() not in {".csv", ".parquet", ".json", ".jsonl"}:
                raise PipelineError("--out は .csv / .parquet / .json / .jsonl を指定してください。")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.unlink(missing_ok=True)
            suffix = out.suffix.lower()
            if suffix == ".csv":
                options = "FORMAT CSV, HEADER TRUE"
            elif suffix == ".parquet":
                options = "FORMAT PARQUET"
            elif suffix == ".json":
                options = "FORMAT JSON, ARRAY TRUE"
            else:
                options = "FORMAT JSON, ARRAY FALSE"
            con.execute(f"COPY ({statement}) TO {_sql_string(out.resolve())} ({options})")
            print(f"出力しました: {out}")
            return
        cursor = con.execute(statement)
        if cursor.description is None:
            print("完了しました。")
            return
        columns = [item[0] for item in cursor.description]
        rows = cursor.fetchmany(max_rows)
        print_table(columns, rows)
        if len(rows) == max_rows:
            print(f"表示は先頭 {max_rows} 行までです。--out で全件を書き出せます。")
    finally:
        con.close()
