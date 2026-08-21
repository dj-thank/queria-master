from __future__ import annotations

import csv
import json
import os
import re
import shutil
import sqlite3
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .resources import PROJECT_ROOT
from .runtime import DEFAULT_RUNTIME_DB


SEARCH_INDEX_VERSION = "8"
DEFAULT_SEARCH_INDEX = PROJECT_ROOT / "data" / "search.sqlite"
_BATCH_SIZE = 20_000
MAX_SEARCH_KEYWORD_LENGTH = 256
_UNSAFE_QUERY = re.compile(r"[\"'*:()\-]|\b(?:AND|OR|NOT|NEAR)\b", re.IGNORECASE)
_URL_QUERY = re.compile(r"^https?://", re.IGNORECASE)
_PHONE_QUERY = re.compile(r"^\+?[0-9][0-9\s().\-]*[0-9]$")
_JSIC_TOKEN = re.compile(r"^([A-T])(?:([0-9]{2})[0-9]{0,3})?$", re.IGNORECASE)

SEARCH_RESULT_COLUMNS = (
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
    "corporate_kind_code",
)


class SearchIndexError(RuntimeError):
    """検索索引が利用できない、または原本DBと一致しない場合の例外。"""


class SearchQueryError(SearchIndexError):
    """検索条件が索引の安全な問い合わせ上限を超えている。"""


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _sql_path(value: Path) -> str:
    return "'" + str(value.resolve()).replace("'", "''") + "'"


def _normalise_text(value: Any) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    return " ".join(text.split())


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _trigrams(value: str) -> list[str]:
    return [value[index : index + 3] for index in range(len(value) - 2)]


def validate_search_keyword(keyword: Any) -> str:
    """Return the normalized keyword or reject input that would force a full scan."""
    raw_value = "" if keyword is None else str(keyword).strip()
    value = _normalise_text(raw_value)
    if "\x00" in raw_value or "\x00" in value:
        raise SearchQueryError("キーワードにNUL文字は使用できません。")
    if max(len(raw_value), len(value)) > MAX_SEARCH_KEYWORD_LENGTH:
        raise SearchQueryError(
            f"キーワードは{MAX_SEARCH_KEYWORD_LENGTH}文字以内で指定してください。"
        )
    return value


def _quoted_fts_phrase(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _contact_fts_query(keyword: str) -> str | None:
    """Build a column-scoped FTS query for punctuation-heavy contact values."""
    value = _normalise_text(keyword)
    if len(value) < 3:
        return None
    phrase = _quoted_fts_phrase(value)
    if _URL_QUERY.match(value):
        return f"{{company_url inquiry_form_url}} : {phrase}"
    if _PHONE_QUERY.fullmatch(value) and len(re.sub(r"\D", "", value)) >= 6:
        return f"phone : {phrase}"
    return None


def _fts_query(keyword: str) -> str | None:
    value = _normalise_text(keyword)
    if len(value) < 3 or len(value) > MAX_SEARCH_KEYWORD_LENGTH or _UNSAFE_QUERY.search(value):
        return None
    grams = list(dict.fromkeys(_trigrams(value)))
    if not grams:
        return None
    # detail=full enables an exact phrase over the trigram tokens.  This is
    # slightly larger than detail=none, but avoids a second full-text LIKE
    # verification scan for common terms such as 株式会社.
    return _quoted_fts_phrase(value)


def _parse_jsic_categories(
    raw_codes: Any,
    major_codes: Any = None,
    middle_codes: Any = None,
) -> list[tuple[str | None, str | None]]:
    """Parse complete JSIC tokens without treating substrings as codes."""
    parsed: list[tuple[str | None, str | None]] = []
    for raw_code in str(raw_codes or "").split("|"):
        match = _JSIC_TOKEN.fullmatch(raw_code.strip())
        if match:
            parsed.append((match.group(1).upper(), match.group(2)))
    if not parsed:
        parsed.extend(
            (code, None)
            for value in str(major_codes or "").split("|")
            if (code := value.strip().upper()) and re.fullmatch(r"[A-T]", code)
        )
        parsed.extend(
            (None, code)
            for value in str(middle_codes or "").split("|")
            if (code := value.strip()) and re.fullmatch(r"[0-9]{2}", code)
        )
    return list(dict.fromkeys(parsed))


def _numeric(value: Any) -> int | float | None:
    if value is None:
        return None
    try:
        integer = int(value)
        if float(value) == integer:
            return integer
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _refresh_info(con: Any) -> tuple[str, str]:
    try:
        row = con.execute(
            "SELECT refresh_id, scope FROM meta.refresh_log ORDER BY completed_at DESC NULLS LAST LIMIT 1"
        ).fetchone()
    except Exception:
        row = con.execute("SELECT refresh_id, scope FROM meta.refresh_log LIMIT 1").fetchone()
    if not row:
        return "", ""
    return str(row[0] or ""), str(row[1] or "")


def _runtime_generation_id(con: Any) -> str:
    try:
        row = con.execute(
            "SELECT manifest_json FROM meta.runtime_manifest ORDER BY built_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            return ""
        payload = json.loads(str(row[0]))
        return str(payload.get("generation_id") or "") if isinstance(payload, dict) else ""
    except Exception:
        return ""


def _company_select(con: Any) -> tuple[str, list[str]]:
    source_relation = "core.companies"
    try:
        con.execute("DESCRIBE search.company_documents").fetchall()
        source_relation = "search.company_documents"
    except Exception:
        pass
    columns = {str(row[0]) for row in con.execute(f"DESCRIBE {source_relation}").fetchall()}

    def pick(*names: str, fallback: str = "NULL") -> str:
        for name in names:
            if name in columns:
                return f"c.{_quote_identifier(name)}"
        return fallback

    address = pick("resolved_address", "full_address")
    if address == "NULL":
        address = f"concat_ws('', {pick('prefecture_name')}, {pick('city_name')}, {pick('street_number')})"
    return (
        "SELECT "
        f"c.{_quote_identifier('corporate_number')}, "
        f"{pick('company_name')}, "
        f"{address}, "
        f"{pick('resolved_prefecture_name', 'prefecture_name')}, "
        f"{pick('resolved_city_name', 'city_name')}, "
        f"{pick('jsic_major_codes_all', 'jsic_major_code')}, "
        f"{pick('jsic_middle_codes_all', 'jsic_middle_codes')}, "
        f"{pick('employee_number')}, "
        f"{pick('capital_stock')}, "
        f"{pick('representative_name')}, "
        f"{pick('effective_company_url', 'company_url')}, "
        f"{pick('business_summary')}, "
        f"{pick('business_items_raw')}, "
        f"{pick('jsic_codes_all_raw')}, "
        f"{pick('phone')}, "
        f"{pick('email')}, "
        f"{pick('inquiry_form_url')}, "
        f"{pick('corporate_kind_code')} "
        # The index has its own monotonically increasing doc_id.  Avoiding a
        # 5.8-million-row sort keeps the extraction cursor streaming and
        # materially reduces peak DuckDB memory during index construction.
        f"FROM {source_relation} c"
    ), [
        "corporate_number",
        "company_name",
        "full_address",
        "prefecture_name",
        "city_name",
        "jsic_major_codes",
        "jsic_middle_codes",
        "employee_number",
        "capital_stock",
        "representative_name",
        "company_url",
        "business_summary",
        "business_items_raw",
        "jsic_codes_all_raw",
        "phone",
        "email",
        "inquiry_form_url",
        "corporate_kind_code",
    ]


def _configure_sqlite(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=FILE;
        PRAGMA cache_size=-262144;
        PRAGMA mmap_size=4294967296;
        PRAGMA page_size=32768;
        """
    )


def build_search_index(
    database_path: Path = DEFAULT_RUNTIME_DB,
    output_path: Path = DEFAULT_SEARCH_INDEX,
    *,
    batch_size: int = _BATCH_SIZE,
) -> dict[str, Any]:
    """DuckDBの法人マスターから、再開可能な読み取り用SQLite FTS索引を構築する。"""
    if batch_size < 1:
        raise SearchIndexError("batch_size は1以上で指定してください。")
    database_path = Path(database_path).resolve()
    output_path = Path(output_path).resolve()
    if not database_path.is_file():
        raise SearchIndexError(f"DuckDBがありません: {database_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - runtime dependency.
        raise SearchIndexError("duckdb がありません。") from exc

    temp_dir = Path(tempfile.mkdtemp(prefix="queria-search-index-", dir=str(output_path.parent)))
    temp_path = temp_dir / output_path.name
    db_con = duckdb.connect(str(database_path), read_only=True)
    sqlite_con: sqlite3.Connection | None = None
    try:
        db_con.execute("PRAGMA threads=4")
        db_con.execute("PRAGMA memory_limit='1GB'")
        db_con.execute(f"SET temp_directory={_sql_path(temp_dir)}")
        refresh_id, scope = _refresh_info(db_con)
        runtime_generation_id = _runtime_generation_id(db_con)
        select_sql, _ = _company_select(db_con)
        # The Python DuckDB fetch API needs pyarrow for true record-batch
        # streaming.  Use DuckDB's streaming CSV writer instead so the base
        # package stays stdlib-only and peak Python memory is bounded even
        # when the source has millions of rows.
        csv_path = temp_dir / "company-export.csv"
        db_con.execute(
            f"COPY ({select_sql}) TO {_sql_path(csv_path)} "
            "(FORMAT CSV, HEADER FALSE, QUOTE '\"', ESCAPE '\"')"
        )
        db_con.close()
        db_con = None

        sqlite_con = sqlite3.connect(str(temp_path))
        _configure_sqlite(sqlite_con)
        sqlite_con.executescript(
            """
            CREATE TABLE index_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE company_docs (
                doc_id INTEGER PRIMARY KEY,
                corporate_number TEXT NOT NULL UNIQUE,
                company_name TEXT,
                full_address TEXT,
                prefecture_name TEXT,
                city_name TEXT,
                jsic_major_codes TEXT,
                jsic_middle_codes TEXT,
                employee_number INTEGER,
                capital_stock REAL,
                representative_name TEXT,
                company_url TEXT,
                business_summary TEXT,
                business_items_raw TEXT,
                phone TEXT,
                email TEXT,
                inquiry_form_url TEXT
                ,corporate_kind_code TEXT
            );
            CREATE TABLE company_categories (
                doc_id INTEGER NOT NULL,
                major_code TEXT,
                middle_code TEXT,
                prefecture_name TEXT
            );
            CREATE VIRTUAL TABLE company_fts USING fts5(
                company_name,
                full_address,
                business_summary,
                business_items_raw,
                company_url,
                phone,
                email,
                inquiry_form_url,
                content='',
                tokenize='trigram',
                detail='full'
            );
            """
        )

        doc_sql = """
            INSERT INTO company_docs(
                doc_id, corporate_number, company_name, full_address,
                prefecture_name, city_name, jsic_major_codes, jsic_middle_codes,
                employee_number, capital_stock, representative_name, company_url,
                business_summary, business_items_raw, phone, email, inquiry_form_url,
                corporate_kind_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        fts_sql = """
            INSERT INTO company_fts(rowid, company_name, full_address,
                                    business_summary, business_items_raw, company_url,
                                    phone, email, inquiry_form_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        category_sql = """
            INSERT INTO company_categories(doc_id, major_code, middle_code, prefecture_name)
            VALUES (?, ?, ?, ?)
        """
        doc_id = 0
        with csv_path.open("r", encoding="utf-8", newline="") as source:
            reader = csv.reader(source)
            while rows := [row for _, row in zip(range(batch_size), reader)]:
                docs = []
                fts_rows = []
                category_rows = []
                for row in rows:
                    if len(row) != 18:
                        raise SearchIndexError(f"法人エクスポートの列数が不正です: {len(row)}")
                    values = [value if value != "" else None for value in row]
                    doc_id += 1
                    corporate_number = str(values[0])
                    searchable = [
                        _normalise_text(value)
                        for value in (
                            values[1], values[2], values[11], values[12], values[10],
                            values[14], values[15], values[16],
                        )
                    ]
                    docs.append(
                        (
                            doc_id,
                            corporate_number,
                            None if values[1] is None else str(values[1]),
                            None if values[2] is None else str(values[2]),
                            None if values[3] is None else str(values[3]),
                            None if values[4] is None else str(values[4]),
                            None if values[5] is None else str(values[5]),
                            None if values[6] is None else str(values[6]),
                            _numeric(values[7]),
                            _numeric(values[8]),
                            None if values[9] is None else str(values[9]),
                            None if values[10] is None else str(values[10]),
                            None if values[11] is None else str(values[11]),
                            None if values[12] is None else str(values[12]),
                            None if values[14] is None else str(values[14]),
                            None if values[15] is None else str(values[15]),
                            None if values[16] is None else str(values[16]),
                            None if values[17] is None else str(values[17]),
                        )
                    )
                    fts_rows.append((doc_id, *searchable))
                    parsed_categories = _parse_jsic_categories(
                        values[13],
                        values[5],
                        values[6],
                    )
                    category_rows.extend(
                        (doc_id, major, middle, values[3])
                        for major, middle in dict.fromkeys(parsed_categories)
                    )
                with sqlite_con:
                    sqlite_con.executemany(doc_sql, docs)
                    sqlite_con.executemany(fts_sql, fts_rows)
                    sqlite_con.executemany(category_sql, category_rows)

        sqlite_con.executescript(
            """
            CREATE INDEX idx_company_docs_corporate_number ON company_docs(corporate_number);
            CREATE INDEX idx_company_docs_prefecture ON company_docs(prefecture_name);
            CREATE INDEX idx_company_docs_city ON company_docs(city_name);
            CREATE INDEX idx_company_docs_company_name ON company_docs(company_name COLLATE NOCASE);
            CREATE INDEX idx_company_docs_corporate_kind ON company_docs(corporate_kind_code, doc_id);
            CREATE INDEX idx_company_categories_major ON company_categories(major_code, prefecture_name, doc_id);
            CREATE INDEX idx_company_categories_middle ON company_categories(middle_code, prefecture_name, doc_id);
            CREATE INDEX idx_company_categories_doc ON company_categories(doc_id);
            ANALYZE;
            PRAGMA optimize;
            """
        )
        metadata = {
            "index_version": SEARCH_INDEX_VERSION,
            "refresh_id": refresh_id,
            "scope": scope,
            "row_count": str(doc_id),
            "source_database": str(database_path),
            "source_database_bytes": str(database_path.stat().st_size),
            "source_database_mtime_ns": str(database_path.stat().st_mtime_ns),
            "tokenizer": "trigram",
            "detail": "full",
            "fields": json.dumps(
                [
                    "company_name", "full_address", "business_summary", "business_items_raw",
                    "company_url", "phone", "email", "inquiry_form_url",
                ],
                ensure_ascii=False,
            ),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if runtime_generation_id:
            metadata["runtime_generation_id"] = runtime_generation_id
        sqlite_con.executemany(
            "INSERT INTO index_metadata(key, value) VALUES (?, ?)",
            list(metadata.items()),
        )
        sqlite_con.commit()
        sqlite_con.close()
        sqlite_con = None
        os.replace(temp_path, output_path)
        return {
            "index_path": str(output_path),
            "row_count": doc_id,
            "refresh_id": refresh_id,
            "scope": scope,
            "bytes": output_path.stat().st_size,
            "tokenizer": "trigram",
            "detail": "full",
        }
    finally:
        if sqlite_con is not None:
            sqlite_con.close()
        if db_con is not None:
            db_con.close()
        shutil.rmtree(temp_dir, ignore_errors=True)


class SearchIndex:
    def __init__(
        self,
        index_path: Path = DEFAULT_SEARCH_INDEX,
        *,
        database_path: Path | None = None,
        validate_database: bool = True,
        check_same_thread: bool = True,
    ):
        self.index_path = Path(index_path).resolve()
        if not self.index_path.is_file():
            raise SearchIndexError(f"検索索引がありません: {self.index_path}")
        uri = f"file:{self.index_path.as_posix()}?mode=ro&immutable=1"
        self._con = sqlite3.connect(uri, uri=True, check_same_thread=check_same_thread)
        self._con.row_factory = sqlite3.Row
        self.metadata = dict(self._con.execute("SELECT key, value FROM index_metadata"))
        if self.metadata.get("index_version") != SEARCH_INDEX_VERSION:
            self.close()
            raise SearchIndexError("検索索引のバージョンが現在のコードと一致しません。")
        if database_path is not None and validate_database:
            self._validate_database(Path(database_path))

    def _validate_database(self, database_path: Path) -> None:
        # A release/runtime database and its immutable search index are built
        # as one refresh unit.  Prefer a metadata/stat check here: opening a
        # 31GB DuckDB only to read refresh_log made every short CLI query pay a
        # multi-second startup penalty.  Older indexes without these fields
        # retain the conservative DuckDB fallback below.
        expected_generation = self.metadata.get("runtime_generation_id")
        if expected_generation:
            try:
                import duckdb

                con = duckdb.connect(str(database_path), read_only=True)
                try:
                    actual_generation = _runtime_generation_id(con)
                finally:
                    con.close()
            except Exception as exc:
                self.close()
                raise SearchIndexError(f"検索用Runtime DBを検証できません: {database_path}") from exc
            if not actual_generation:
                self.close()
                raise SearchIndexError("Runtime DBにgeneration_idがありません。build-runtimeを再実行してください。")
            if actual_generation != expected_generation:
                self.close()
                raise SearchIndexError(
                    "Runtime DBと検索索引のgeneration_idが一致しません。"
                    "同じ更新で生成したファイルを選択してください。"
                )
            try:
                expected_bytes = self.metadata.get("source_database_bytes")
                if expected_bytes is not None and int(expected_bytes) != database_path.stat().st_size:
                    self.close()
                    raise SearchIndexError("検索索引の原本Runtime DBサイズが一致しません。")
            except SearchIndexError:
                raise
            except (OSError, TypeError, ValueError) as exc:
                self.close()
                raise SearchIndexError(f"Runtime DBのサイズを検証できません: {database_path}") from exc
            return
        try:
            database_stat = database_path.stat()
            expected_bytes = self.metadata.get("source_database_bytes")
            expected_mtime_ns = self.metadata.get("source_database_mtime_ns")
            expected_source = self.metadata.get("source_database")
            same_source_path = False
            if expected_source:
                try:
                    same_source_path = Path(expected_source).resolve() == database_path.resolve()
                except OSError:
                    same_source_path = False
            if expected_bytes is not None and int(expected_bytes) != database_stat.st_size:
                self.close()
                raise SearchIndexError("検索索引の原本DBサイズが一致しません。")
            # mtime is useful when the DB is refreshed in place, but it is not
            # stable after a ZIP is extracted to another machine or folder.
            # A relocated release therefore uses the immutable size/metadata
            # contract and avoids rejecting a valid bundle on filesystem time.
            if same_source_path and expected_mtime_ns is not None and int(expected_mtime_ns) != database_stat.st_mtime_ns:
                self.close()
                raise SearchIndexError("検索索引の原本DB更新時刻が一致しません。")
            if expected_bytes is not None and (expected_mtime_ns is None or not same_source_path):
                return
        except SearchIndexError:
            raise
        except (OSError, TypeError, ValueError):
            pass
        try:
            import duckdb

            con = duckdb.connect(str(database_path), read_only=True)
            try:
                refresh_id, _ = _refresh_info(con)
            finally:
                con.close()
        except Exception as exc:
            self.close()
            raise SearchIndexError(f"原本DBとの一致確認に失敗しました: {database_path}") from exc
        if refresh_id and self.metadata.get("refresh_id") and refresh_id != self.metadata["refresh_id"]:
            self.close()
            raise SearchIndexError("検索索引が原本DBより古いです。build-search-indexを再実行してください。")

    @property
    def row_count(self) -> int:
        return int(self.metadata.get("row_count", "0"))

    def close(self) -> None:
        if getattr(self, "_con", None) is not None:
            self._con.close()
            self._con = None

    def __enter__(self) -> "SearchIndex":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()

    def search(
        self,
        keyword: str | None = None,
        *,
        prefecture: str | None = None,
        city: str | None = None,
        industry_majors: Iterable[str] = (),
        industry_middles: Iterable[str] = (),
        corporate_kinds: Iterable[str] = (),
        min_employees: int | None = None,
        max_employees: int | None = None,
        min_capital: int | None = None,
        max_capital: int | None = None,
        has_web: bool = False,
        limit: int = 100,
        fast: bool = False,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100_000:
            raise SearchIndexError("limit は1〜100000の範囲で指定してください。")
        keyword_value = "" if keyword is None else str(keyword).strip()
        normalized_keyword = validate_search_keyword(keyword_value)
        fts = (
            _contact_fts_query(normalized_keyword) or _fts_query(normalized_keyword)
            if normalized_keyword
            else None
        )
        short_prefix_search = False
        where: list[str] = []
        params: list[Any] = []
        majors = [str(code).strip().upper() for code in industry_majors if str(code).strip()]
        middles = [str(code).strip() for code in industry_middles if str(code).strip()]
        kinds = [str(code).strip() for code in corporate_kinds if str(code).strip()]
        # A single category code can be driven directly from its covering index.
        # This avoids scanning all companies in a prefecture before applying the
        # category predicate.  For multiple codes we retain EXISTS semantics to
        # avoid duplicate companies.
        lead_category = None
        if fts is None and fast:
            if len(majors) == 1:
                lead_category = "major"
            elif len(middles) == 1:
                lead_category = "middle"
        if fts is not None:
            where.append("company_fts MATCH ?")
            params.append(fts)
        if fts is None and keyword_value:
            if fast and (len(normalized_keyword) < 3 or _UNSAFE_QUERY.search(normalized_keyword)):
                # SQLite's trigram tokenizer cannot answer sub-three-character
                # or operator-like terms efficiently.  In the speed-priority
                # path, use the indexed company-name prefix instead of a
                # 5.8-million-row substring scan.  Full substring semantics
                # remain available without --fast.
                where.append("d.company_name LIKE ? ESCAPE '\\'")
                params.append(f"{_escape_like(normalized_keyword)}%")
                short_prefix_search = True
            else:
                like = f"%{_escape_like(keyword_value)}%"
                where.append(
                    "(d.company_name LIKE ? ESCAPE '\\' OR d.full_address LIKE ? ESCAPE '\\' "
                    "OR d.business_summary LIKE ? ESCAPE '\\' OR d.business_items_raw LIKE ? ESCAPE '\\' "
                    "OR d.company_url LIKE ? ESCAPE '\\' OR d.phone LIKE ? ESCAPE '\\' "
                    "OR d.email LIKE ? ESCAPE '\\' OR d.inquiry_form_url LIKE ? ESCAPE '\\')"
                )
                params.extend([like] * 8)

        if majors:
            placeholders = ", ".join("?" for _ in majors)
            if lead_category == "major":
                # A company can have both a major-only row and one or more
                # middle-category rows.  Drive the fast path from a distinct
                # document-id relation so LIMIT and exported rows represent
                # unique companies rather than category rows.
                where.append(
                    "d.doc_id IN (SELECT DISTINCT cc.doc_id FROM company_categories AS cc "
                    f"WHERE cc.major_code IN ({placeholders})"
                    + (" AND cc.prefecture_name = ?" if prefecture else "")
                    + ")"
                )
                params.extend(majors)
                if prefecture:
                    params.append(prefecture)
            else:
                category_prefecture = " AND cc.prefecture_name = ?" if prefecture else ""
                where.append(
                    "EXISTS (SELECT 1 FROM company_categories cc "
                    f"WHERE cc.doc_id = d.doc_id AND cc.major_code IN ({placeholders}){category_prefecture})"
                )
                params.extend(majors)
                if prefecture:
                    params.append(prefecture)
        if middles:
            placeholders = ", ".join("?" for _ in middles)
            if lead_category == "middle":
                where.append(
                    "d.doc_id IN (SELECT DISTINCT cc.doc_id FROM company_categories AS cc "
                    f"WHERE cc.middle_code IN ({placeholders})"
                    + (" AND cc.prefecture_name = ?" if prefecture else "")
                    + ")"
                )
                params.extend(middles)
                if prefecture:
                    params.append(prefecture)
            else:
                category_prefecture = " AND cc.prefecture_name = ?" if prefecture else ""
                where.append(
                    "EXISTS (SELECT 1 FROM company_categories cc "
                    f"WHERE cc.doc_id = d.doc_id AND cc.middle_code IN ({placeholders}){category_prefecture})"
                )
                params.extend(middles)
                if prefecture:
                    params.append(prefecture)
        if prefecture and lead_category is None:
            where.append("d.prefecture_name = ?")
            params.append(prefecture)
        if city:
            where.append("d.city_name LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(city)}%")
        if kinds:
            placeholders = ", ".join("?" for _ in kinds)
            where.append(f"d.corporate_kind_code IN ({placeholders})")
            params.extend(kinds)
        if min_employees is not None:
            where.append("d.employee_number >= ?")
            params.append(min_employees)
        if max_employees is not None:
            where.append("d.employee_number <= ?")
            params.append(max_employees)
        if min_capital is not None:
            where.append("d.capital_stock >= ?")
            params.append(min_capital)
        if max_capital is not None:
            where.append("d.capital_stock <= ?")
            params.append(max_capital)
        if has_web:
            where.append("d.company_url IS NOT NULL AND trim(d.company_url) <> ''")
        if not where:
            where.append("1 = 1")

        order_by = (
            ""
            if fast and (fts is not None or short_prefix_search)
            else "ORDER BY d.doc_id"
            if fast
            else "ORDER BY d.employee_number IS NULL, d.employee_number DESC, "
            "d.capital_stock IS NULL, d.capital_stock DESC, d.company_name"
        )
        from_clause = "company_docs AS d"
        if fts is not None:
            from_clause += " JOIN company_fts ON company_fts.rowid = d.doc_id"
        sql = f"""
            SELECT
                d.corporate_number,
                d.company_name,
                d.prefecture_name,
                d.city_name,
                d.jsic_major_codes,
                d.jsic_middle_codes,
                d.employee_number,
                d.capital_stock,
                d.representative_name,
                d.company_url,
                d.business_summary,
                d.phone,
                d.email,
                d.inquiry_form_url
                ,d.corporate_kind_code
            FROM {from_clause}
            WHERE {' AND '.join(where)}
            {order_by}
            LIMIT ?
        """
        params.append(limit)
        rows = self._con.execute(sql, params).fetchall()
        return [dict(row) for row in rows]


__all__ = [
    "DEFAULT_SEARCH_INDEX",
    "MAX_SEARCH_KEYWORD_LENGTH",
    "SEARCH_INDEX_VERSION",
    "SEARCH_RESULT_COLUMNS",
    "SearchIndex",
    "SearchIndexError",
    "SearchQueryError",
    "build_search_index",
    "validate_search_keyword",
]
