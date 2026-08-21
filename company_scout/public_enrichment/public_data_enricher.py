#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""企業データへ公的・公式公開データを監査可能な形で統合する。

外部依存は requests のみ。Excelの読込には標準ライブラリでXLSX/XMLを解析する。
入力元列は変更せず、公開値・出典・照合品質を別列としてCSV/SQLiteへ保持する。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import itertools
import json
import math
import re
import sqlite3
import sys
import unicodedata
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

APP_VERSION = "1.1.0"
DEFAULT_DB = Path("output/company_public_data.sqlite3")
SOURCE_HEADERS = [
    "SOURCE_ID", "企業名", "本店所在地", "証券コード", "従業員数",
    "daibunruiCode", "daibunruiName", "chubunruiCode", "chubunruiName",
    "syoubunruiCode", "syoubunruiName", "jsicDetailedClass", "saibunruiName",
]


PUBLIC_COLUMNS = [
    "公開_法人番号", "公開_法人番号一致コード", "公開_法人番号一致信頼度", "公開_法人番号一致元",
    "公開_法人番号採用状態", "公開_法人名", "公開_法人名カナ", "公開_法人名英字", "公開_郵便番号",
    "公開_登記住所", "公開_法人状態", "公開_登記閉鎖日", "公開_代表者名称", "公開_代表者役職",
    "公開_資本金円", "公開_従業員数", "公開_設立年月日", "公開_WebサイトURL", "公開_電話番号",
    "公開_電話番号根拠URL", "公開_事業概要", "公開_事業種目", "公開_最新決算期",
    "公開_最新売上円", "公開_最新売上種別", "公開_最新純利益円", "公開_平均年齢",
    "公開_平均年収円", "公開_コアキーワード", "公開_ランキング業種コード", "公開_ランキング業種名",
    "公開_売上業種内順位", "公開_売上業種内母数", "公開_純利益業種内順位", "公開_純利益業種内母数",
    "公開_主要出典", "公開_データ品質", "公開_最終更新日", "公開_要確認理由", "公開_法人番号候補一覧",
]

AMOUNT_UNITS = {
    "円": 1,
    "千円": 1_000,
    "万円": 10_000,
    "十万円": 100_000,
    "百万円": 1_000_000,
    "千万円": 10_000_000,
    "億円": 100_000_000,
    "十億円": 1_000_000_000,
    "百億円": 10_000_000_000,
    "千億円": 100_000_000_000,
    "兆円": 1_000_000_000_000,
}

REVENUE_CANDIDATES = [
    ("売上高", ["売上高"]),
    ("営業収益", ["営業収益"]),
    ("営業収入", ["営業収入"]),
    ("営業総収入", ["営業総収入"]),
    ("経常収益", ["経常収益"]),
    ("正味収入保険料", ["正味収入保険料"]),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_path(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    if s.lower() in {"none", "null", "nan"}:
        return ""
    return s


def normalize_header(value: Any) -> str:
    s = unicodedata.normalize("NFKC", clean_text(value)).lower()
    return re.sub(r"[\s\u3000_\-–—・:：()（）\[\]【】/\\]+", "", s)


def normalize_name(value: Any) -> str:
    s = unicodedata.normalize("NFKC", clean_text(value)).lower()
    replacements = {
        "株式会社": "", "有限会社": "", "合同会社": "", "合資会社": "", "合名会社": "",
        "一般社団法人": "", "一般財団法人": "", "公益社団法人": "", "公益財団法人": "",
        "特定非営利活動法人": "", "社会福祉法人": "", "学校法人": "", "医療法人": "",
        "(株)": "", "㈱": "", "(有)": "", "㈲": "", "inc.": "", "inc": "", "co.,ltd.": "",
    }
    for old, new in replacements.items():
        s = s.replace(old, new)
    s = re.sub(r"[\s\u3000・･\.．,，'’\"“”\-‐–—_()（）\[\]【】/\\]+", "", s)
    return s


def normalize_address(value: Any) -> str:
    s = unicodedata.normalize("NFKC", clean_text(value)).lower()
    kanji_digits = {"〇": "0", "一": "1", "二": "2", "三": "3", "四": "4", "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}
    for k, v in kanji_digits.items():
        s = s.replace(k, v)
    s = s.replace("丁目", "-").replace("番地", "-").replace("番", "-").replace("号", "")
    s = re.sub(r"[\s\u3000・･\.．,，'’\"“”()（）\[\]【】/\\]+", "", s)
    s = re.sub(r"[-‐‑‒–—―ー]+", "-", s).strip("-")
    return s


def normalize_corporate_number(value: Any) -> str:
    digits = re.sub(r"\D", "", clean_text(value))
    return digits if len(digits) == 13 else ""


def normalize_security_code(value: Any) -> str:
    code = re.sub(r"[^0-9A-Z]", "", unicodedata.normalize("NFKC", clean_text(value)).upper())
    return code[:4] if len(code) >= 4 else ""


def parse_number(value: Any) -> float | None:
    s = unicodedata.normalize("NFKC", clean_text(value))
    if not s or s in {"-", "―", "—"}:
        return None
    neg = False
    if s.startswith(("△", "▲", "-")) or (s.startswith("(") and s.endswith(")")):
        neg = True
    s = s.replace("△", "").replace("▲", "").strip("() ")
    s = re.sub(r"[^0-9.+-]", "", s)
    if not s or s in {"+", "-", "."}:
        return None
    try:
        n = float(s)
    except ValueError:
        return None
    return -abs(n) if neg else n


def amount_to_yen(value: Any, unit: Any = "") -> int | None:
    raw = clean_text(value)
    if not raw:
        return None
    unit_s = unicodedata.normalize("NFKC", clean_text(unit)).replace(" ", "")
    multiplier = None
    for u in sorted(AMOUNT_UNITS, key=len, reverse=True):
        if u in unit_s or u in raw:
            multiplier = AMOUNT_UNITS[u]
            break
    if multiplier is None:
        multiplier = 1 if re.fullmatch(r"[△▲\-()0-9,，.\s]+", raw) else None
    if multiplier is None:
        return None
    n = parse_number(raw)
    if n is None:
        return None
    return int(round(n * multiplier))


def parse_age(value: Any) -> float | None:
    n = parse_number(value)
    if n is None or not (0 <= n <= 100):
        return None
    return round(float(n), 2)


def parse_employee_count(value: Any) -> int | None:
    n = parse_number(value)
    if n is None or n < 0:
        return None
    return int(round(n))


def latest_date_key(text: Any) -> str:
    s = unicodedata.normalize("NFKC", clean_text(text))
    dates: list[str] = []
    for y, m, d in re.findall(r"(\d{4})[年/\-.](\d{1,2})[月/\-.](\d{1,2})日?", s):
        dates.append(f"{int(y):04d}-{int(m):02d}-{int(d):02d}")
    for y, m in re.findall(r"(\d{4})[年/\-.](\d{1,2})月?", s):
        dates.append(f"{int(y):04d}-{int(m):02d}-01")
    if not dates:
        years = re.findall(r"(?:19|20)\d{2}", s)
        dates.extend(f"{y}-01-01" for y in years)
    return max(dates) if dates else ""


def first_value(row: dict[str, str], aliases: Sequence[str], contains: bool = False) -> str:
    normalized = {normalize_header(k): clean_text(v) for k, v in row.items()}
    for alias in aliases:
        a = normalize_header(alias)
        if a in normalized and normalized[a] != "":
            return normalized[a]
    if contains:
        for alias in aliases:
            a = normalize_header(alias)
            for k, v in normalized.items():
                if a and a in k and v != "":
                    return v
    return ""


def find_header_key(fieldnames: Sequence[str], aliases: Sequence[str], contains: bool = False) -> str | None:
    normalized = {normalize_header(k): k for k in fieldnames if k is not None}
    for alias in aliases:
        a = normalize_header(alias)
        if a in normalized:
            return normalized[a]
    if contains:
        for alias in aliases:
            a = normalize_header(alias)
            for nk, orig in normalized.items():
                if a and a in nk:
                    return orig
    return None


def open_text_binary(stream: io.BufferedIOBase) -> io.TextIOWrapper:
    sample = stream.read(4)
    stream.seek(0)
    if sample.startswith(b"\xff\xfe") or sample.startswith(b"\xfe\xff"):
        return io.TextIOWrapper(stream, encoding="utf-16", newline="")
    data = stream.read(65536)
    stream.seek(0)
    for enc in ("utf-8-sig", "cp932"):
        try:
            data.decode(enc)
            return io.TextIOWrapper(stream, encoding=enc, newline="", errors="strict")
        except UnicodeDecodeError:
            continue
    return io.TextIOWrapper(stream, encoding="utf-8-sig", newline="", errors="replace")


def iter_csv_sources(path: Path) -> Iterator[tuple[str, io.TextIOBase]]:
    if path.suffix.lower() == ".zip":
        zf = zipfile.ZipFile(path)
        try:
            for info in zf.infolist():
                if info.is_dir() or not info.filename.lower().endswith(('.csv', '.txt')):
                    continue
                raw = zf.open(info, "r")
                text = open_text_binary(raw)
                yield f"{path.name}!{info.filename}", text
                text.close()
        finally:
            zf.close()
    else:
        raw = path.open("rb")
        text = open_text_binary(raw)
        try:
            yield path.name, text
        finally:
            text.close()


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def drop_schema(con: sqlite3.Connection) -> None:
    """Drop the local working schema. Intended only for an explicit prepare --replace."""
    for table in [
        "site_contacts", "edinet_metrics", "workplace_info", "financial_history",
        "public_master", "corporate_match_candidates", "corporate_matches",
        "derived_company", "companies", "source_audit", "metadata",
    ]:
        con.execute(f"DROP TABLE IF EXISTS {table}")
    con.commit()


def _table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}


def assert_schema_compatible(con: sqlite3.Connection) -> None:
    required = {
        "companies": {"source_id", "source_row_json", "jsic_detail_code"},
        "corporate_matches": {"source_id", "corporate_number", "status"},
        "public_master": {"corporate_number", "business_categories_json"},
        "financial_history": {"corporate_number", "fiscal_period", "source_file"},
    }
    for table, columns in required.items():
        actual = _table_columns(con, table)
        missing = columns - actual
        if missing:
            raise RuntimeError(
                f"incompatible database schema in {table}; missing {sorted(missing)}. "
                "Run prepare --replace with the original local input file or use a new --db path."
            )


def init_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS companies (
            source_id TEXT PRIMARY KEY,
            source_row INTEGER NOT NULL UNIQUE,
            company_name TEXT NOT NULL,
            address TEXT NOT NULL,
            employee_count_raw TEXT,
            security_code TEXT,
            jsic_large_code TEXT,
            jsic_large_name TEXT,
            jsic_middle_code TEXT,
            jsic_middle_name TEXT,
            jsic_small_code TEXT,
            jsic_small_name TEXT,
            jsic_detail_code TEXT,
            jsic_detail_name TEXT,
            source_row_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_companies_name_address ON companies(company_name, address);
        CREATE INDEX IF NOT EXISTS idx_companies_security ON companies(security_code);
        CREATE TABLE IF NOT EXISTS corporate_matches (
            source_id TEXT PRIMARY KEY REFERENCES companies(source_id) ON DELETE CASCADE,
            corporate_number TEXT,
            matched_name TEXT,
            matched_address TEXT,
            match_code TEXT,
            hit_count INTEGER,
            source_name TEXT,
            confidence REAL,
            status TEXT NOT NULL,
            reason TEXT,
            matched_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_matches_corp ON corporate_matches(corporate_number);
        CREATE TABLE IF NOT EXISTS corporate_match_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT NOT NULL REFERENCES companies(source_id) ON DELETE CASCADE,
            corporate_number TEXT,
            matched_name TEXT,
            matched_address TEXT,
            match_code TEXT,
            hit_count INTEGER,
            source_name TEXT,
            confidence REAL,
            status TEXT,
            reason TEXT,
            observed_at TEXT NOT NULL,
            UNIQUE(source_id, corporate_number, match_code, source_name)
        );
        CREATE INDEX IF NOT EXISTS idx_match_candidates_source ON corporate_match_candidates(source_id);
        CREATE TABLE IF NOT EXISTS public_master (
            corporate_number TEXT PRIMARY KEY,
            company_name TEXT,
            name_kana TEXT,
            name_en TEXT,
            postal_code TEXT,
            address TEXT,
            corporate_status TEXT,
            close_date TEXT,
            close_cause TEXT,
            representative_name TEXT,
            representative_position TEXT,
            capital_yen INTEGER,
            employees INTEGER,
            established_date TEXT,
            business_summary TEXT,
            website_url TEXT,
            business_categories_json TEXT,
            source_quality TEXT,
            source_org TEXT,
            acquired_at TEXT,
            updated_at TEXT,
            source_file TEXT,
            raw_json TEXT
        );
        CREATE TABLE IF NOT EXISTS financial_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            corporate_number TEXT NOT NULL,
            fiscal_period TEXT NOT NULL,
            fiscal_sort_key TEXT,
            accounting_standard TEXT,
            revenue_yen INTEGER,
            revenue_label TEXT,
            revenue_raw TEXT,
            net_income_yen INTEGER,
            net_income_raw TEXT,
            ordinary_income_yen INTEGER,
            capital_yen INTEGER,
            net_assets_yen INTEGER,
            total_assets_yen INTEGER,
            employees INTEGER,
            source_quality TEXT,
            source_org TEXT,
            acquired_at TEXT,
            updated_at TEXT,
            source_file TEXT,
            raw_json TEXT,
            UNIQUE(corporate_number, fiscal_period, source_file)
        );
        CREATE INDEX IF NOT EXISTS idx_fin_corp_period ON financial_history(corporate_number, fiscal_sort_key);
        CREATE TABLE IF NOT EXISTS workplace_info (
            corporate_number TEXT PRIMARY KEY,
            average_age REAL,
            average_tenure REAL,
            monthly_overtime REAL,
            source_quality TEXT,
            source_org TEXT,
            acquired_at TEXT,
            updated_at TEXT,
            source_file TEXT,
            raw_json TEXT
        );
        CREATE TABLE IF NOT EXISTS edinet_metrics (
            source_id TEXT PRIMARY KEY REFERENCES companies(source_id) ON DELETE CASCADE,
            security_code TEXT,
            edinet_code TEXT,
            doc_id TEXT,
            submit_datetime TEXT,
            period_end TEXT,
            average_age REAL,
            average_salary_yen INTEGER,
            source_url TEXT,
            source_file TEXT,
            imported_at TEXT,
            raw_json TEXT
        );
        CREATE TABLE IF NOT EXISTS site_contacts (
            source_id TEXT PRIMARY KEY REFERENCES companies(source_id) ON DELETE CASCADE,
            corporate_number TEXT,
            website_url TEXT,
            phone TEXT,
            evidence_url TEXT,
            evidence_text TEXT,
            confidence REAL,
            fetched_at TEXT,
            source_file TEXT,
            raw_json TEXT
        );
        CREATE TABLE IF NOT EXISTS derived_company (
            source_id TEXT PRIMARY KEY REFERENCES companies(source_id) ON DELETE CASCADE,
            keywords TEXT,
            industry_group_code TEXT,
            industry_group_name TEXT,
            latest_period TEXT,
            latest_revenue_yen INTEGER,
            latest_revenue_label TEXT,
            latest_net_income_yen INTEGER,
            revenue_rank INTEGER,
            revenue_count INTEGER,
            net_income_rank INTEGER,
            net_income_count INTEGER,
            derived_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS source_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file TEXT NOT NULL,
            source_type TEXT NOT NULL,
            sha256 TEXT,
            rows_read INTEGER NOT NULL,
            rows_accepted INTEGER NOT NULL,
            rows_review INTEGER NOT NULL,
            errors INTEGER NOT NULL,
            imported_at TEXT NOT NULL,
            notes TEXT
        );
        """
    )
    assert_schema_compatible(con)
    set_meta(con, "app_version", APP_VERSION)
    con.commit()


def set_meta(con: sqlite3.Connection, key: str, value: Any) -> None:
    con.execute(
        "INSERT INTO metadata(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, clean_text(value), now_iso()),
    )


def get_meta(con: sqlite3.Connection, key: str, default: str = "") -> str:
    row = con.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


# ---------------- XLSX parser ----------------
XLS_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def column_index(cell_ref: str) -> int:
    letters = re.match(r"[A-Z]+", cell_ref.upper())
    if not letters:
        return 0
    idx = 0
    for ch in letters.group(0):
        idx = idx * 26 + (ord(ch) - 64)
    return idx - 1


def load_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    values: list[str] = []
    with zf.open("xl/sharedStrings.xml") as f:
        for event, elem in ET.iterparse(f, events=("end",)):
            if elem.tag == f"{{{XLS_NS}}}si":
                values.append("".join(t.text or "" for t in elem.iter(f"{{{XLS_NS}}}t")))
                elem.clear()
    return values


def resolve_sheet_path(zf: zipfile.ZipFile, sheet_name: str) -> str:
    wb_root = ET.fromstring(zf.read("xl/workbook.xml"))
    sheets = wb_root.findall(f".//{{{XLS_NS}}}sheet")
    selected = None
    if sheet_name:
        selected = next((sheet for sheet in sheets if sheet.attrib.get("name") == sheet_name), None)
    elif sheets:
        selected = sheets[0]
    if selected is None:
        label = sheet_name or "先頭シート"
        raise ValueError(f"シートが見つかりません: {label}")
    rel_id = selected.attrib.get(f"{{{REL_NS}}}id")
    if not rel_id:
        raise ValueError("シートの関連IDがありません")
    rel_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    for rel in rel_root.findall(f"{{{PKG_REL_NS}}}Relationship"):
        if rel.attrib.get("Id") == rel_id:
            target = rel.attrib["Target"].lstrip("/")
            return target if target.startswith("xl/") else "xl/" + target
    raise ValueError(f"シート実体を解決できません: {sheet_name}")


def cell_value(cell: ET.Element, shared: list[str]) -> str:
    ctype = cell.attrib.get("t", "")
    if ctype == "inlineStr":
        return "".join(t.text or "" for t in cell.iter(f"{{{XLS_NS}}}t"))
    v = cell.find(f"{{{XLS_NS}}}v")
    if v is None or v.text is None:
        return ""
    if ctype == "s":
        try:
            return shared[int(v.text)]
        except (ValueError, IndexError):
            return ""
    if ctype == "b":
        return "TRUE" if v.text == "1" else "FALSE"
    return v.text


def iter_xlsx_rows(path: Path, sheet_name: str) -> Iterator[list[str]]:
    with zipfile.ZipFile(path) as zf:
        shared = load_shared_strings(zf)
        sheet_path = resolve_sheet_path(zf, sheet_name)
        with zf.open(sheet_path) as f:
            for event, elem in ET.iterparse(f, events=("end",)):
                if elem.tag != f"{{{XLS_NS}}}row":
                    continue
                vals: dict[int, str] = {}
                max_col = -1
                for c in elem.findall(f"{{{XLS_NS}}}c"):
                    idx = column_index(c.attrib.get("r", "A1"))
                    vals[idx] = cell_value(c, shared)
                    max_col = max(max_col, idx)
                yield [vals.get(i, "") for i in range(max_col + 1)]
                elem.clear()


def reset_data(con: sqlite3.Connection) -> None:
    """Remove all local input and enrichment data while retaining the schema."""
    for table in [
        "site_contacts", "edinet_metrics", "workplace_info", "financial_history",
        "public_master", "corporate_match_candidates", "corporate_matches",
        "derived_company", "companies", "source_audit",
    ]:
        con.execute(f"DELETE FROM {table}")
    con.commit()


def _header_index(headers: Sequence[str], aliases: Sequence[str], *, contains: bool = False) -> int | None:
    key = find_header_key(headers, aliases, contains=contains)
    return list(headers).index(key) if key is not None else None


def prepare_rows(
    con: sqlite3.Connection,
    headers: Sequence[str],
    rows: Iterable[Sequence[Any]],
    *,
    source_path: Path,
    source_format: str,
    replace: bool = False,
    sheet_name: str = "",
) -> dict[str, Any]:
    """Prepare a caller-owned CSV/XLSX without assuming a source-specific schema."""
    init_schema(con)
    if replace:
        reset_data(con)
    elif con.execute("SELECT COUNT(*) FROM companies").fetchone()[0]:
        raise RuntimeError("companies already contains data; pass --replace to rebuild")

    normalized_headers = [clean_text(x) for x in headers]
    if not any(normalized_headers):
        raise RuntimeError("input header row is empty")

    id_i = _header_index(normalized_headers, ["SOURCE_ID", "source_id", "ID", "id"])
    name_i = _header_index(normalized_headers, [
        "企業名", "会社名", "法人名", "商号又は名称", "商号または名称", "company_name", "name",
    ])
    address_i = _header_index(normalized_headers, [
        "本店所在地", "所在地", "住所", "登記住所", "address",
    ])
    if name_i is None or address_i is None:
        raise RuntimeError("company name and address columns are required")

    employee_i = _header_index(normalized_headers, ["従業員数", "employee_count", "employees"])
    security_i = _header_index(normalized_headers, ["証券コード", "security_code", "symbol"])
    large_code_i = _header_index(normalized_headers, ["daibunruiCode", "JSIC大分類コード"])
    large_name_i = _header_index(normalized_headers, ["daibunruiName", "JSIC大分類名"])
    middle_code_i = _header_index(normalized_headers, ["chubunruiCode", "JSIC中分類コード"])
    middle_name_i = _header_index(normalized_headers, ["chubunruiName", "JSIC中分類名"])
    small_code_i = _header_index(normalized_headers, ["syoubunruiCode", "JSIC小分類コード"])
    small_name_i = _header_index(normalized_headers, ["syoubunruiName", "JSIC小分類名"])
    detail_code_i = _header_index(normalized_headers, ["jsicDetailedClass", "JSIC細分類コード", "業種コード"])
    detail_name_i = _header_index(normalized_headers, ["saibunruiName", "JSIC細分類名", "業種名"])

    def at(row: Sequence[Any], idx: int | None) -> str:
        return clean_text(row[idx]) if idx is not None and idx < len(row) else ""

    inserted = duplicate = invalid = 0
    batch: list[tuple[Any, ...]] = []
    for source_row, raw_values in enumerate(rows, start=2):
        raw = [clean_text(x) for x in raw_values]
        raw += [""] * max(0, len(normalized_headers) - len(raw))
        raw = raw[:len(normalized_headers)]
        source_id = at(raw, id_i) if id_i is not None else f"row-{source_row - 1:08d}"
        name = at(raw, name_i)
        address = at(raw, address_i)
        if not source_id or not name or not address:
            invalid += 1
            continue
        batch.append((
            source_id, source_row, name, address, at(raw, employee_i),
            normalize_security_code(at(raw, security_i)),
            at(raw, large_code_i), at(raw, large_name_i),
            at(raw, middle_code_i), at(raw, middle_name_i),
            at(raw, small_code_i), at(raw, small_name_i),
            at(raw, detail_code_i), at(raw, detail_name_i),
            json.dumps(raw, ensure_ascii=False, separators=(",", ":")),
        ))
        if len(batch) >= 2000:
            inserted_now, duplicate_now = _insert_company_batch(con, batch)
            inserted += inserted_now
            duplicate += duplicate_now
            batch.clear()
    if batch:
        inserted_now, duplicate_now = _insert_company_batch(con, batch)
        inserted += inserted_now
        duplicate += duplicate_now

    set_meta(con, "source_path", str(source_path))
    set_meta(con, "source_sha256", sha256_path(source_path))
    set_meta(con, "source_format", source_format)
    set_meta(con, "source_sheet", sheet_name)
    set_meta(con, "source_headers_json", json.dumps(normalized_headers, ensure_ascii=False))
    set_meta(con, "prepared_at", now_iso())
    con.commit()
    return {
        "inserted": inserted,
        "duplicate": duplicate,
        "invalid": invalid,
        "headers": len(normalized_headers),
        "generated_source_ids": id_i is None,
        "source_format": source_format,
    }


def _insert_company_batch(con: sqlite3.Connection, batch: Sequence[tuple[Any, ...]]) -> tuple[int, int]:
    inserted = duplicate = 0
    for row in batch:
        try:
            con.execute(
                """INSERT INTO companies(
                    source_id,source_row,company_name,address,employee_count_raw,security_code,
                    jsic_large_code,jsic_large_name,jsic_middle_code,jsic_middle_name,
                    jsic_small_code,jsic_small_name,jsic_detail_code,jsic_detail_name,source_row_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                row,
            )
            inserted += 1
        except sqlite3.IntegrityError:
            duplicate += 1
    con.commit()
    return inserted, duplicate


def prepare_from_xlsx(
    con: sqlite3.Connection,
    xlsx: Path,
    sheet_name: str = "",
    replace: bool = False,
) -> dict[str, Any]:
    rows = iter_xlsx_rows(xlsx, sheet_name)
    try:
        headers = next(rows)
    except StopIteration as exc:
        raise RuntimeError("Excel sheet is empty") from exc
    return prepare_rows(
        con,
        headers,
        rows,
        source_path=xlsx,
        source_format="xlsx",
        replace=replace,
        sheet_name=sheet_name,
    )


def prepare_from_csv(con: sqlite3.Connection, csv_path: Path, replace: bool = False) -> dict[str, Any]:
    with csv_path.open("rb") as raw:
        text = open_text_binary(raw)
        reader = csv.reader(text)
        try:
            headers = next(reader)
        except StopIteration as exc:
            raise RuntimeError("CSV is empty") from exc
        return prepare_rows(
            con,
            headers,
            reader,
            source_path=csv_path,
            source_format="csv",
            replace=replace,
        )


def prepare_input(
    con: sqlite3.Connection,
    input_path: Path,
    *,
    sheet_name: str = "",
    replace: bool = False,
) -> dict[str, Any]:
    suffix = input_path.suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        return prepare_from_xlsx(con, input_path, sheet_name=sheet_name, replace=replace)
    if suffix in {".csv", ".txt"}:
        return prepare_from_csv(con, input_path, replace=replace)
    raise RuntimeError(f"unsupported input format: {input_path.suffix or '(none)'}")


# ---------------- Assignment ----------------
def make_assignment(con: sqlite3.Connection, output: Path, chunk_size: int = 0) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    rows = con.execute("SELECT source_id,company_name,address FROM companies ORDER BY source_row")
    count = 0
    chunks: list[str] = []
    if chunk_size > 0:
        chunk_dir = output.parent / "法人番号付与用_chunks"
        chunk_dir.mkdir(parents=True, exist_ok=True)
    writer = None
    fh = output.open("w", encoding="utf-8-sig", newline="")
    writer = csv.writer(fh)
    writer.writerow(["SOURCE_ID", "企業名", "本店所在地", "法人番号"])
    chunk_fh = None
    chunk_writer = None
    chunk_start = 1
    try:
        for r in rows:
            count += 1
            row = [r["source_id"], r["company_name"], r["address"], ""]
            writer.writerow(row)
            if chunk_size > 0:
                if chunk_writer is None or (count - 1) % chunk_size == 0:
                    if chunk_fh:
                        chunk_fh.close()
                    chunk_start = count
                    chunk_end = min(count + chunk_size - 1, con.execute("SELECT COUNT(*) FROM companies").fetchone()[0])
                    chunk_path = chunk_dir / f"法人番号付与用_{chunk_start:06d}-{chunk_end:06d}.csv"
                    chunks.append(str(chunk_path))
                    chunk_fh = chunk_path.open("w", encoding="utf-8-sig", newline="")
                    chunk_writer = csv.writer(chunk_fh)
                    chunk_writer.writerow(["SOURCE_ID", "企業名", "本店所在地", "法人番号"])
                chunk_writer.writerow(row)
    finally:
        fh.close()
        if chunk_fh:
            chunk_fh.close()
    return {"rows": count, "output": str(output), "chunks": len(chunks)}


# ---------------- Audit ----------------
def audit(con: sqlite3.Connection, source_file: str, source_type: str, file_hash: str, read: int, accepted: int, review: int, errors: int, notes: str = "") -> None:
    con.execute(
        "INSERT INTO source_audit(source_file,source_type,sha256,rows_read,rows_accepted,rows_review,errors,imported_at,notes) VALUES(?,?,?,?,?,?,?,?,?)",
        (source_file, source_type, file_hash, read, accepted, review, errors, now_iso(), notes),
    )
    con.commit()


def record_match_candidate(
    con: sqlite3.Connection,
    *,
    source_id: str,
    corporate_number: str,
    matched_name: str,
    matched_address: str,
    match_code: str,
    hit_count: int,
    source_name: str,
    confidence: float,
    status: str,
    reason: str,
) -> None:
    con.execute(
        """INSERT INTO corporate_match_candidates(
            source_id,corporate_number,matched_name,matched_address,match_code,hit_count,
            source_name,confidence,status,reason,observed_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(source_id,corporate_number,match_code,source_name) DO UPDATE SET
            matched_name=excluded.matched_name,matched_address=excluded.matched_address,
            hit_count=excluded.hit_count,confidence=excluded.confidence,status=excluded.status,
            reason=excluded.reason,observed_at=excluded.observed_at""",
        (
            source_id, corporate_number, matched_name, matched_address, match_code,
            hit_count, source_name, confidence, status, reason, now_iso(),
        ),
    )


def upsert_match(
    con: sqlite3.Connection,
    *,
    source_id: str,
    corporate_number: str,
    matched_name: str,
    matched_address: str,
    match_code: str,
    hit_count: int,
    source_name: str,
    confidence: float,
    status: str,
    reason: str,
) -> bool:
    """Record a candidate and update the resolved match without hiding conflicts."""
    record_match_candidate(
        con,
        source_id=source_id,
        corporate_number=corporate_number,
        matched_name=matched_name,
        matched_address=matched_address,
        match_code=match_code,
        hit_count=hit_count,
        source_name=source_name,
        confidence=confidence,
        status=status,
        reason=reason,
    )
    existing = con.execute("SELECT * FROM corporate_matches WHERE source_id=?", (source_id,)).fetchone()

    if existing and existing["status"] == "accepted":
        if status != "accepted":
            return False
        if existing["corporate_number"] != corporate_number:
            old_number = clean_text(existing["corporate_number"])
            con.execute(
                """UPDATE corporate_matches SET corporate_number='',matched_name='',matched_address='',
                match_code='CONFLICT',hit_count=?,source_name=?,confidence=0.0,status='review',
                reason=?,matched_at=? WHERE source_id=?""",
                (
                    max(int(existing["hit_count"] or 1), hit_count, 2),
                    " / ".join(dict.fromkeys(x for x in [clean_text(existing["source_name"]), source_name] if x)),
                    f"conflicting accepted corporate numbers: {old_number} vs {corporate_number}",
                    now_iso(),
                    source_id,
                ),
            )
            return True

    if existing and existing["status"] == "review" and status == "review":
        if float(existing["confidence"] or 0.0) >= confidence:
            return False

    con.execute(
        """INSERT INTO corporate_matches(
            source_id,corporate_number,matched_name,matched_address,match_code,hit_count,
            source_name,confidence,status,reason,matched_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(source_id) DO UPDATE SET
            corporate_number=excluded.corporate_number,matched_name=excluded.matched_name,
            matched_address=excluded.matched_address,match_code=excluded.match_code,
            hit_count=excluded.hit_count,source_name=excluded.source_name,
            confidence=excluded.confidence,status=excluded.status,reason=excluded.reason,
            matched_at=excluded.matched_at""",
        (
            source_id, corporate_number, matched_name, matched_address, match_code,
            hit_count, source_name, confidence, status, reason, now_iso(),
        ),
    )
    return True


# ---------------- Classification ----------------
def classify_rows(first_row: list[str], second_row: list[str] | None = None) -> str:
    hs = {normalize_header(x) for x in first_row}
    joined = "|".join(hs)
    if "sourceid" in hs and ("結果コード" in joined or "マッチコード" in joined or "法人番号" in joined):
        if any(normalize_header(x) in hs for x in ["結果コード", "一致コード", "マッチコード"]):
            return "numbering"
    if "sourceid" in hs and any(x in joined for x in ["平均年収", "平均年間給与", "書類管理番号", "edinetコード"]):
        return "edinet"
    if "sourceid" in hs and any(x in joined for x in ["根拠url", "電話番号", "公式サイトurl"]):
        return "site_phone"
    if "法人番号" in joined and any(x in joined for x in ["従業員の平均年齢", "平均継続勤務年数", "月平均所定外労働時間"]):
        return "gbiz_workplace"
    if "法人番号" in joined and any(x in joined for x in ["事業年度", "売上高", "営業収益", "純利益", "総資産"]):
        return "gbiz_financial"
    if "法人番号" in joined and any(x in joined for x in ["事業概要", "法人代表者", "企業ホームページ", "webサイトurl", "設立年月日", "資本金"]):
        return "gbiz_basic"
    # NTA bulk CSV has no header: column 2 is a 13-digit corporate number and column 7 is name.
    if len(first_row) >= 16 and normalize_corporate_number(first_row[1] if len(first_row) > 1 else ""):
        return "nta_bulk"
    if second_row and len(second_row) >= 16 and normalize_corporate_number(second_row[1] if len(second_row) > 1 else ""):
        return "nta_bulk_with_header"
    return "unknown"


# ---------------- NTA bulk ----------------
def import_nta_stream(con: sqlite3.Connection, source_name: str, rows: Iterator[list[str]], first: list[str], has_header: bool = False) -> tuple[int,int,int,int]:
    targets: dict[tuple[str, str], list[str]] = defaultdict(list)
    for r in con.execute("SELECT source_id,company_name,address FROM companies"):
        targets[(normalize_name(r["company_name"]), normalize_address(r["address"]))].append(r["source_id"])
    read = accepted = review = errors = 0
    iterable = rows if has_header else itertools.chain([first], rows)
    for row in iterable:
        read += 1
        try:
            if len(row) < 16:
                errors += 1
                continue
            corp = normalize_corporate_number(row[1])
            name = clean_text(row[6])
            # 国税庁CSVの0始まり列: prefectureName=9, cityName=10, streetNumber=11
            # （sequenceNumber=0, corporateNumber=1, ... kind=8）
            prefecture = clean_text(row[9]) if len(row) > 9 else ""
            city = clean_text(row[10]) if len(row) > 10 else ""
            street = clean_text(row[11]) if len(row) > 11 else ""
            address = prefecture + city + street
            postal = clean_text(row[15]) if len(row) > 15 else ""
            close_date = clean_text(row[18]) if len(row) > 18 else ""
            close_cause = clean_text(row[19]) if len(row) > 19 else ""
            key = (normalize_name(name), normalize_address(address))
            source_ids = targets.get(key, [])
            if not source_ids:
                continue
            if len(source_ids) == 1 and corp:
                if upsert_match(con, source_id=source_ids[0], corporate_number=corp, matched_name=name, matched_address=address,
                                match_code="NTA_EXACT", hit_count=1, source_name=source_name, confidence=1.0,
                                status="accepted", reason="法人名＋住所の正規化完全一致"):
                    accepted += 1
                con.execute(
                    """INSERT INTO public_master(corporate_number,company_name,postal_code,address,corporate_status,close_date,close_cause,source_org,source_file,raw_json)
                    VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(corporate_number) DO UPDATE SET
                    company_name=COALESCE(NULLIF(excluded.company_name,''),public_master.company_name),
                    postal_code=COALESCE(NULLIF(excluded.postal_code,''),public_master.postal_code),
                    address=COALESCE(NULLIF(excluded.address,''),public_master.address),
                    corporate_status=COALESCE(NULLIF(excluded.corporate_status,''),public_master.corporate_status),
                    close_date=COALESCE(NULLIF(excluded.close_date,''),public_master.close_date),
                    close_cause=COALESCE(NULLIF(excluded.close_cause,''),public_master.close_cause),
                    source_org=COALESCE(NULLIF(excluded.source_org,''),public_master.source_org),
                    source_file=excluded.source_file""",
                    (corp, name, postal, address, "登記閉鎖" if close_date else "存続", close_date, close_cause,
                     "国税庁法人番号公表サイト", source_name, json.dumps(row, ensure_ascii=False)),
                )
            else:
                for fid in source_ids:
                    if upsert_match(con, source_id=fid, corporate_number=corp, matched_name=name, matched_address=address,
                                    match_code="NTA_EXACT_MULTI_TARGET", hit_count=len(source_ids), source_name=source_name,
                                    confidence=0.4, status="review", reason="同一名・同一住所の元データが複数"):
                        review += 1
            if read % 100000 == 0:
                con.commit()
        except Exception:
            errors += 1
    con.commit()
    return read, accepted, review, errors


# ---------------- Numbering result ----------------
def import_numbering(con: sqlite3.Connection, source_name: str, reader: csv.DictReader, accept_prefix: bool) -> tuple[int,int,int,int]:
    rows = list(reader)
    read = len(rows)
    fields = reader.fieldnames or []
    fid_key = find_header_key(fields, ["SOURCE_ID", "SOURCEID"])
    name_key = find_header_key(fields, ["企業名", "法人名", "商号または名称", "商号又は名称"])
    addr_key = find_header_key(fields, ["本店所在地", "所在地", "住所", "登記住所"])
    corp_key = find_header_key(fields, ["法人番号"])
    code_key = find_header_key(fields, ["結果コード", "一致コード", "マッチコード", "付与結果コード"], contains=True)
    hit_key = find_header_key(fields, ["ヒット件数", "候補件数", "該当件数"], contains=True)
    if not corp_key:
        raise ValueError("法人番号列を検出できません")
    groups: dict[str, list[dict[str,str]]] = defaultdict(list)
    by_key: dict[tuple[str,str], list[str]] = defaultdict(list)
    if not fid_key:
        for r in con.execute("SELECT source_id,company_name,address FROM companies"):
            by_key[(normalize_name(r["company_name"]), normalize_address(r["address"]))].append(r["source_id"])
    for row in rows:
        fid = clean_text(row.get(fid_key, "")) if fid_key else ""
        if not fid and name_key and addr_key:
            ids = by_key.get((normalize_name(row.get(name_key,"")), normalize_address(row.get(addr_key,""))), [])
            if len(ids) == 1:
                fid = ids[0]
        if fid:
            groups[fid].append(row)
    accepted = review = errors = 0
    for fid, grow in groups.items():
        corp_numbers = sorted({normalize_corporate_number(r.get(corp_key, "")) for r in grow if normalize_corporate_number(r.get(corp_key, ""))})
        code = clean_text(grow[0].get(code_key, "")).upper() if code_key else ""
        hit_count = int(parse_number(grow[0].get(hit_key, "")) or len(corp_numbers) or len(grow)) if hit_key else (len(corp_numbers) or len(grow))
        corp = corp_numbers[0] if len(corp_numbers) == 1 else ""
        name = clean_text(grow[0].get(name_key, "")) if name_key else ""
        addr = clean_text(grow[0].get(addr_key, "")) if addr_key else ""
        if code == "M00" and hit_count == 1 and corp:
            status, confidence, reason = "accepted", 1.0, "M00かつヒット1件"
        elif accept_prefix and code in {"M01", "M02"} and hit_count == 1 and corp:
            status, confidence, reason = "accepted", 0.85, f"{code}前方一致を明示許可"
        else:
            status, confidence, reason = "review", 0.60 if code in {"M01","M02"} and hit_count == 1 else 0.40, f"自動採用条件外: code={code or '不明'}, hit={hit_count}"
        try:
            changed = upsert_match(con, source_id=fid, corporate_number=corp, matched_name=name, matched_address=addr,
                                   match_code=code or "GBIZ_NUMBERING", hit_count=hit_count, source_name=source_name,
                                   confidence=confidence, status=status, reason=reason)
            if changed:
                if status == "accepted": accepted += 1
                else: review += 1
        except Exception:
            errors += 1
    con.commit()
    return read, accepted, review, errors


# ---------------- gBiz basic ----------------
def import_gbiz_basic(con: sqlite3.Connection, source_name: str, reader: csv.DictReader) -> tuple[int,int,int,int]:
    fields = reader.fieldnames or []
    corp_key = find_header_key(fields, ["法人番号"])
    if not corp_key:
        raise ValueError("法人番号列を検出できません")
    target_corp = {r[0] for r in con.execute("SELECT corporate_number FROM corporate_matches WHERE status='accepted' AND corporate_number<>''")}
    if not target_corp:
        raise RuntimeError("採用済み法人番号が0件です。先に法人番号付与結果または国税庁全件CSVを取り込んでください。")
    read = accepted = errors = 0
    for row in reader:
        read += 1
        try:
            corp = normalize_corporate_number(row.get(corp_key, ""))
            if not corp:
                errors += 1
                continue
            if target_corp and corp not in target_corp:
                continue
            name = first_value(row, ["商号または名称", "商号又は名称", "法人名", "名称"])
            kana = first_value(row, ["商号または名称（カナ）", "商号又は名称（カナ）", "法人名フリガナ", "法人名カナ"])
            name_en = first_value(row, ["商号または名称（英字）", "法人名英語"])
            postal = first_value(row, ["郵便番号"])
            address = first_value(row, ["登記住所", "本社所在地", "所在地"])
            close_date = first_value(row, ["登記記録の閉鎖等年月日", "閉鎖年月日"])
            close_cause = first_value(row, ["登記記録の閉鎖等の事由", "閉鎖事由"])
            rep_name = first_value(row, ["法人代表者名", "代表者名", "代表者氏名"])
            rep_pos = first_value(row, ["法人代表者役職", "代表者役職"])
            capital_raw = first_value(row, ["資本金", "資本金額"])
            capital_unit = first_value(row, ["資本金（単位）", "資本金単位"])
            employees = parse_employee_count(first_value(row, ["従業員数"]));
            established = first_value(row, ["設立年月日", "設立日"])
            summary = first_value(row, ["事業概要", "企業概要", "事業内容"])
            website = first_value(row, ["企業ホームページ", "WebサイトURL", "法人ホームページ", "URL"], contains=True)
            categories = []
            for key, value in row.items():
                nk = normalize_header(key)
                if any(x in nk for x in [normalize_header("事業種目"), normalize_header("業種"), normalize_header("事業分野")]):
                    v = clean_text(value)
                    if v and v not in categories:
                        categories.append(v)
            quality = first_value(row, ["データ品質"])
            source_org = first_value(row, ["出典元", "出典"])
            acquired = first_value(row, ["最終取得日", "取得日"])
            updated = first_value(row, ["最終更新日", "更新日"])
            raw_json = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
            con.execute(
                """INSERT INTO public_master(
                    corporate_number,company_name,name_kana,name_en,postal_code,address,corporate_status,close_date,close_cause,
                    representative_name,representative_position,capital_yen,employees,established_date,business_summary,website_url,
                    business_categories_json,source_quality,source_org,acquired_at,updated_at,source_file,raw_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(corporate_number) DO UPDATE SET
                    company_name=COALESCE(NULLIF(excluded.company_name,''),public_master.company_name),
                    name_kana=COALESCE(NULLIF(excluded.name_kana,''),public_master.name_kana),
                    name_en=COALESCE(NULLIF(excluded.name_en,''),public_master.name_en),
                    postal_code=COALESCE(NULLIF(excluded.postal_code,''),public_master.postal_code),
                    address=COALESCE(NULLIF(excluded.address,''),public_master.address),
                    corporate_status=COALESCE(NULLIF(excluded.corporate_status,''),public_master.corporate_status),
                    close_date=COALESCE(NULLIF(excluded.close_date,''),public_master.close_date),
                    close_cause=COALESCE(NULLIF(excluded.close_cause,''),public_master.close_cause),
                    representative_name=COALESCE(NULLIF(excluded.representative_name,''),public_master.representative_name),
                    representative_position=COALESCE(NULLIF(excluded.representative_position,''),public_master.representative_position),
                    capital_yen=COALESCE(excluded.capital_yen,public_master.capital_yen),
                    employees=COALESCE(excluded.employees,public_master.employees),
                    established_date=COALESCE(NULLIF(excluded.established_date,''),public_master.established_date),
                    business_summary=COALESCE(NULLIF(excluded.business_summary,''),public_master.business_summary),
                    website_url=COALESCE(NULLIF(excluded.website_url,''),public_master.website_url),
                    business_categories_json=COALESCE(NULLIF(excluded.business_categories_json,'[]'),public_master.business_categories_json),
                    source_quality=COALESCE(NULLIF(excluded.source_quality,''),public_master.source_quality),
                    source_org=COALESCE(NULLIF(excluded.source_org,''),public_master.source_org),
                    acquired_at=COALESCE(NULLIF(excluded.acquired_at,''),public_master.acquired_at),
                    updated_at=COALESCE(NULLIF(excluded.updated_at,''),public_master.updated_at),
                    source_file=excluded.source_file, raw_json=excluded.raw_json""",
                (corp,name,kana,name_en,postal,address,"登記閉鎖" if close_date else "存続",close_date,close_cause,
                 rep_name,rep_pos,amount_to_yen(capital_raw,capital_unit),employees,established,summary,website,
                 json.dumps(categories,ensure_ascii=False),quality,source_org,acquired,updated,source_name,raw_json),
            )
            accepted += 1
        except Exception:
            errors += 1
        if read % 10000 == 0:
            con.commit()
    con.commit()
    return read, accepted, 0, errors


# ---------------- gBiz financial ----------------
def pair_amount(row: dict[str,str], label_aliases: Sequence[str]) -> tuple[int|None,str,str]:
    raw = first_value(row, label_aliases)
    unit_aliases: list[str] = []
    for label in label_aliases:
        unit_aliases.extend([f"{label}（単位）", f"{label}(単位)", f"{label}単位"])
    unit = first_value(row, unit_aliases)
    return amount_to_yen(raw, unit), raw, unit


def import_gbiz_financial(con: sqlite3.Connection, source_name: str, reader: csv.DictReader) -> tuple[int,int,int,int]:
    fields = reader.fieldnames or []
    corp_key = find_header_key(fields, ["法人番号"])
    if not corp_key:
        raise ValueError("法人番号列を検出できません")
    target_corp = {r[0] for r in con.execute("SELECT corporate_number FROM corporate_matches WHERE status='accepted' AND corporate_number<>''")}
    if not target_corp:
        raise RuntimeError("採用済み法人番号が0件です。先に法人番号付与結果または国税庁全件CSVを取り込んでください。")
    read = accepted = errors = 0
    for row in reader:
        read += 1
        try:
            corp = normalize_corporate_number(row.get(corp_key, ""))
            if target_corp and corp not in target_corp:
                continue
            period = first_value(row, ["事業年度", "会計期間", "決算期"])
            if not corp or not period:
                errors += 1
                continue
            revenue_yen = None; revenue_label = ""; revenue_raw = ""
            for label, aliases in REVENUE_CANDIDATES:
                val, raw, _unit = pair_amount(row, aliases)
                if val is not None:
                    revenue_yen, revenue_label, revenue_raw = val, label, raw
                    break
            net_yen, net_raw, _ = pair_amount(row, ["当期純利益", "純利益", "親会社株主に帰属する当期純利益"])
            ordinary_yen, _, _ = pair_amount(row, ["経常利益"])
            capital_yen, _, _ = pair_amount(row, ["資本金"])
            net_assets_yen, _, _ = pair_amount(row, ["純資産額", "純資産"])
            total_assets_yen, _, _ = pair_amount(row, ["総資産額", "総資産"])
            employees = parse_employee_count(first_value(row, ["従業員数"]));
            quality = first_value(row, ["データ品質"])
            source_org = first_value(row, ["出典元", "出典"])
            acquired = first_value(row, ["最終取得日", "取得日"])
            updated = first_value(row, ["最終更新日", "更新日"])
            accounting = first_value(row, ["会計基準"])
            raw_json = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
            con.execute(
                """INSERT INTO financial_history(
                    corporate_number,fiscal_period,fiscal_sort_key,accounting_standard,revenue_yen,revenue_label,revenue_raw,
                    net_income_yen,net_income_raw,ordinary_income_yen,capital_yen,net_assets_yen,total_assets_yen,employees,
                    source_quality,source_org,acquired_at,updated_at,source_file,raw_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(corporate_number,fiscal_period,source_file) DO UPDATE SET
                    fiscal_sort_key=excluded.fiscal_sort_key, accounting_standard=excluded.accounting_standard,
                    revenue_yen=excluded.revenue_yen, revenue_label=excluded.revenue_label, revenue_raw=excluded.revenue_raw,
                    net_income_yen=excluded.net_income_yen, net_income_raw=excluded.net_income_raw,
                    ordinary_income_yen=excluded.ordinary_income_yen, capital_yen=excluded.capital_yen,
                    net_assets_yen=excluded.net_assets_yen, total_assets_yen=excluded.total_assets_yen,
                    employees=excluded.employees, source_quality=excluded.source_quality, source_org=excluded.source_org,
                    acquired_at=excluded.acquired_at, updated_at=excluded.updated_at, raw_json=excluded.raw_json""",
                (corp,period,latest_date_key(period),accounting,revenue_yen,revenue_label,revenue_raw,net_yen,net_raw,
                 ordinary_yen,capital_yen,net_assets_yen,total_assets_yen,employees,quality,source_org,acquired,updated,source_name,raw_json),
            )
            accepted += 1
        except Exception:
            errors += 1
        if read % 10000 == 0:
            con.commit()
    con.commit()
    return read, accepted, 0, errors


# ---------------- gBiz workplace ----------------
def import_gbiz_workplace(con: sqlite3.Connection, source_name: str, reader: csv.DictReader) -> tuple[int,int,int,int]:
    fields = reader.fieldnames or []
    corp_key = find_header_key(fields, ["法人番号"])
    if not corp_key:
        raise ValueError("法人番号列を検出できません")
    target_corp = {r[0] for r in con.execute("SELECT corporate_number FROM corporate_matches WHERE status='accepted' AND corporate_number<>''")}
    if not target_corp:
        raise RuntimeError("採用済み法人番号が0件です。先に法人番号付与結果または国税庁全件CSVを取り込んでください。")
    read = accepted = errors = 0
    for row in reader:
        read += 1
        try:
            corp = normalize_corporate_number(row.get(corp_key, ""))
            if target_corp and corp not in target_corp:
                continue
            if not corp:
                errors += 1
                continue
            age = parse_age(first_value(row, ["従業員の平均年齢", "平均年齢"]));
            tenure = parse_number(first_value(row, ["正社員の平均継続勤務年数", "平均継続勤務年数"]));
            overtime = parse_number(first_value(row, ["月平均所定外労働時間"]));
            quality = first_value(row, ["データ品質"])
            source_org = first_value(row, ["出典元", "出典"])
            acquired = first_value(row, ["最終取得日", "取得日"])
            updated = first_value(row, ["最終更新日", "更新日"])
            con.execute(
                """INSERT INTO workplace_info(corporate_number,average_age,average_tenure,monthly_overtime,source_quality,source_org,acquired_at,updated_at,source_file,raw_json)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(corporate_number) DO UPDATE SET
                average_age=COALESCE(excluded.average_age,workplace_info.average_age),
                average_tenure=COALESCE(excluded.average_tenure,workplace_info.average_tenure),
                monthly_overtime=COALESCE(excluded.monthly_overtime,workplace_info.monthly_overtime),
                source_quality=COALESCE(NULLIF(excluded.source_quality,''),workplace_info.source_quality),
                source_org=COALESCE(NULLIF(excluded.source_org,''),workplace_info.source_org),
                acquired_at=COALESCE(NULLIF(excluded.acquired_at,''),workplace_info.acquired_at),
                updated_at=COALESCE(NULLIF(excluded.updated_at,''),workplace_info.updated_at),
                source_file=excluded.source_file, raw_json=excluded.raw_json""",
                (corp,age,tenure,overtime,quality,source_org,acquired,updated,source_name,json.dumps(row,ensure_ascii=False,separators=(",", ":"))),
            )
            accepted += 1
        except Exception:
            errors += 1
    con.commit()
    return read, accepted, 0, errors


# ---------------- EDINET/site CSV ----------------
def import_edinet(con: sqlite3.Connection, source_name: str, reader: csv.DictReader) -> tuple[int,int,int,int]:
    fields = reader.fieldnames or []
    source_id_key = find_header_key(fields, ["SOURCE_ID", "SOURCEID"])
    security_key = find_header_key(fields, ["証券コード", "security_code"])
    read = accepted = review = errors = 0
    security_to_ids: dict[str, list[str]] = defaultdict(list)
    for row in con.execute("SELECT source_id,security_code FROM companies WHERE TRIM(COALESCE(security_code,''))<>''"):
        security_to_ids[normalize_security_code(row["security_code"])].append(row["source_id"])

    for row in reader:
        read += 1
        try:
            source_id = clean_text(row.get(source_id_key, "")) if source_id_key else ""
            security = normalize_security_code(row.get(security_key, "")) if security_key else ""
            if not source_id and security and len(security_to_ids.get(security, [])) == 1:
                source_id = security_to_ids[security][0]
            company = con.execute(
                "SELECT security_code FROM companies WHERE source_id=?", (source_id,)
            ).fetchone() if source_id else None
            if company is None:
                review += 1
                continue
            local_security = normalize_security_code(company["security_code"])
            if security and local_security and security != local_security:
                review += 1
                continue

            age = parse_age(first_value(row, ["平均年齢"]))
            salary = amount_to_yen(first_value(row, ["平均年収円", "平均年間給与", "平均年収"]), "円")
            if age is None and salary is None:
                review += 1
                continue
            con.execute(
                """INSERT INTO edinet_metrics(
                    source_id,security_code,edinet_code,doc_id,submit_datetime,period_end,
                    average_age,average_salary_yen,source_url,source_file,imported_at,raw_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET
                    security_code=excluded.security_code,edinet_code=excluded.edinet_code,
                    doc_id=excluded.doc_id,submit_datetime=excluded.submit_datetime,
                    period_end=excluded.period_end,average_age=COALESCE(excluded.average_age,edinet_metrics.average_age),
                    average_salary_yen=COALESCE(excluded.average_salary_yen,edinet_metrics.average_salary_yen),
                    source_url=excluded.source_url,source_file=excluded.source_file,
                    imported_at=excluded.imported_at,raw_json=excluded.raw_json""",
                (
                    source_id, security or local_security, first_value(row, ["EDINETコード"]),
                    first_value(row, ["書類管理番号", "docID"]), first_value(row, ["提出日時"]),
                    first_value(row, ["事業年度末", "期間末"]), age, salary,
                    first_value(row, ["出典URL", "ソースURL"]), source_name, now_iso(),
                    json.dumps(row, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            accepted += 1
        except Exception:
            errors += 1
    con.commit()
    return read, accepted, review, errors


def _normalized_host(value: str) -> str:
    if not value:
        return ""
    candidate = value if re.match(r"^https?://", value, re.I) else f"https://{value.lstrip('/')}"
    return (urlparse(candidate).hostname or "").lower().removeprefix("www.")


def normalize_phone_number(value: Any) -> str:
    text = unicodedata.normalize("NFKC", clean_text(value)).replace("+81", "0")
    digits = re.sub(r"\D", "", text)
    return digits if digits.startswith("0") and 10 <= len(digits) <= 11 else ""


def import_site_phone(con: sqlite3.Connection, source_name: str, reader: csv.DictReader) -> tuple[int,int,int,int]:
    fields = reader.fieldnames or []
    source_id_key = find_header_key(fields, ["SOURCE_ID", "SOURCEID"])
    read = accepted = review = errors = 0
    for row in reader:
        read += 1
        try:
            source_id = clean_text(row.get(source_id_key, "")) if source_id_key else ""
            target = con.execute(
                """SELECT m.corporate_number,p.website_url FROM corporate_matches m
                JOIN public_master p ON p.corporate_number=m.corporate_number
                WHERE m.source_id=? AND m.status='accepted'""",
                (source_id,),
            ).fetchone() if source_id else None
            if target is None:
                review += 1
                continue

            provided_corporate_number = normalize_corporate_number(first_value(row, ["法人番号"]))
            if provided_corporate_number and provided_corporate_number != target["corporate_number"]:
                review += 1
                continue
            phone = normalize_phone_number(first_value(row, ["電話番号", "代表電話"]))
            evidence_url = first_value(row, ["根拠URL"])
            official_url = clean_text(target["website_url"])
            if not phone or not evidence_url or _normalized_host(evidence_url) != _normalized_host(official_url):
                review += 1
                continue
            confidence = parse_number(first_value(row, ["信頼度"]))
            confidence = max(0.0, min(1.0, float(confidence))) if confidence is not None else 0.0
            con.execute(
                """INSERT INTO site_contacts(
                    source_id,corporate_number,website_url,phone,evidence_url,evidence_text,
                    confidence,fetched_at,source_file,raw_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET
                    corporate_number=excluded.corporate_number,website_url=excluded.website_url,
                    phone=excluded.phone,evidence_url=excluded.evidence_url,
                    evidence_text=excluded.evidence_text,confidence=excluded.confidence,
                    fetched_at=excluded.fetched_at,source_file=excluded.source_file,
                    raw_json=excluded.raw_json""",
                (
                    source_id, target["corporate_number"], official_url, phone, evidence_url,
                    first_value(row, ["根拠テキスト"]), confidence,
                    first_value(row, ["取得日時"]) or now_iso(), source_name,
                    json.dumps(row, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            accepted += 1
        except Exception:
            errors += 1
    con.commit()
    return read, accepted, review, errors


# ---------------- Generic import ----------------
def import_file(con: sqlite3.Connection, path: Path, accept_prefix: bool = False) -> list[dict[str,Any]]:
    results: list[dict[str,Any]] = []
    file_hash = sha256_path(path)
    for source_name, text in iter_csv_sources(path):
        reader0 = csv.reader(text)
        try:
            first = next(reader0)
        except StopIteration:
            continue
        try:
            second = next(reader0)
        except StopIteration:
            second = None
        kind = classify_rows(first, second)
        # Reconstruct stream from consumed rows.
        remaining = iter(reader0)
        try:
            if kind in {"nta_bulk", "nta_bulk_with_header"}:
                if kind == "nta_bulk":
                    seq = itertools.chain(([second] if second is not None else []), remaining)
                    counts = import_nta_stream(con, source_name, seq, first, has_header=False)
                else:
                    seq = itertools.chain(([second] if second is not None else []), remaining)
                    counts = import_nta_stream(con, source_name, seq, first, has_header=True)
            else:
                rows_iter = itertools.chain(([second] if second is not None else []), remaining)
                dict_iter = (dict(zip(first, row + [""] * max(0, len(first)-len(row)))) for row in rows_iter)
                # Small streaming adapter exposes fieldnames for import functions.
                class Adapter:
                    def __init__(self, fieldnames, iterator): self.fieldnames, self._it = fieldnames, iterator
                    def __iter__(self): return self
                    def __next__(self): return next(self._it)
                adapter = Adapter(first, dict_iter)
                if kind == "numbering": counts = import_numbering(con, source_name, adapter, accept_prefix)
                elif kind == "gbiz_basic": counts = import_gbiz_basic(con, source_name, adapter)
                elif kind == "gbiz_financial": counts = import_gbiz_financial(con, source_name, adapter)
                elif kind == "gbiz_workplace": counts = import_gbiz_workplace(con, source_name, adapter)
                elif kind == "edinet": counts = import_edinet(con, source_name, adapter)
                elif kind == "site_phone": counts = import_site_phone(con, source_name, adapter)
                else:
                    unknown_count = sum(1 for _ in adapter)
                    counts = (unknown_count, 0, 0, 0)
            audit(con, source_name, kind, file_hash, *counts, notes="自動判定")
            results.append({"source": source_name, "type": kind, "rows_read": counts[0], "accepted": counts[1], "review": counts[2], "errors": counts[3]})
        except Exception as exc:
            audit(con, source_name, kind, file_hash, 0, 0, 0, 1, notes=f"取込失敗: {type(exc).__name__}: {exc}")
            results.append({"source": source_name, "type": kind, "error": f"{type(exc).__name__}: {exc}"})
    return results


def peek_file_kind(path: Path) -> str:
    gen = iter_csv_sources(path)
    try:
        _name, text = next(gen)
        rr = csv.reader(text)
        first = next(rr, [])
        second = next(rr, None)
        return classify_rows(first, second)
    except Exception:
        return "unknown"
    finally:
        try:
            gen.close()
        except Exception:
            pass

def import_directory(con: sqlite3.Connection, input_dir: Path, accept_prefix: bool = False) -> list[dict[str,Any]]:
    if not input_dir.exists():
        return []
    paths = sorted(
        p for p in input_dir.iterdir()
        if p.is_file() and p.suffix.lower() in {".csv", ".txt", ".zip"}
        and p.name != "ここに公開データを入れる.txt"
    )
    priority = {"numbering":0,"nta_bulk":0,"nta_bulk_with_header":0,"gbiz_basic":1,"gbiz_financial":2,"gbiz_workplace":2,"edinet":3,"site_phone":3,"unknown":9}
    ordered = sorted(paths, key=lambda p: (priority.get(peek_file_kind(p),9), p.name))
    results=[]
    for p in ordered:
        results.extend(import_file(con,p,accept_prefix=accept_prefix))
    return results


# ---------------- Derived values ----------------
STOPWORDS = {
    "株式会社","有限会社","合同会社","事業","業務","サービス","提供","開発","販売","運営","関連","各種","及び","または","その他",
    "システム","ソフトウェア","情報","企業","会社","向け","など","する","こと","もの","利用","対応","支援","管理",
}


def derive_keywords(*texts: str, limit: int = 12) -> str:
    combined = " ".join(clean_text(x) for x in texts if clean_text(x))
    combined = unicodedata.normalize("NFKC", combined)
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9+.#/_-]{1,30}|[\u30A1-\u30FAー]{2,20}|[\u4E00-\u9FFF]{2,10}", combined)
    counter: Counter[str] = Counter()
    first_order: dict[str,int] = {}
    for i,t in enumerate(tokens):
        t=t.strip("_-/. ")
        if len(t)<2 or t in STOPWORDS or t.lower() in {x.lower() for x in STOPWORDS}:
            continue
        if t not in first_order: first_order[t]=i
        counter[t]+=1
    ranked=sorted(counter, key=lambda t:(-counter[t], first_order[t], -len(t)))[:limit]
    return " / ".join(ranked)


def derive(con: sqlite3.Connection) -> dict[str,int]:
    con.execute("DELETE FROM derived_company")
    accepted_rows = con.execute(
        """SELECT c.*,m.corporate_number,p.business_summary,p.business_categories_json,p.source_quality,
        w.average_age AS workplace_age,e.average_age AS edinet_age,e.average_salary_yen
        FROM companies c LEFT JOIN corporate_matches m ON m.source_id=c.source_id AND m.status='accepted'
        LEFT JOIN public_master p ON p.corporate_number=m.corporate_number AND m.status='accepted'
        LEFT JOIN workplace_info w ON w.corporate_number=m.corporate_number AND m.status='accepted'
        LEFT JOIN edinet_metrics e ON e.source_id=c.source_id ORDER BY c.source_row"""
    ).fetchall()
    financial_latest: dict[str,sqlite3.Row] = {}
    for row in con.execute("SELECT * FROM financial_history ORDER BY corporate_number, fiscal_sort_key DESC, fiscal_period DESC"):
        financial_latest.setdefault(row["corporate_number"], row)
    temp=[]
    for r in accepted_rows:
        categories=""
        try:
            categories=" / ".join(json.loads(r["business_categories_json"] or "[]"))
        except Exception:
            categories=clean_text(r["business_categories_json"])
        keywords=derive_keywords(r["business_summary"] or "",categories,r["jsic_detail_name"] or "",r["jsic_small_name"] or "",r["jsic_middle_name"] or "")
        group_code=r["jsic_detail_code"] or r["jsic_small_code"] or r["jsic_middle_code"] or ""
        group_name=r["jsic_detail_name"] or r["jsic_small_name"] or r["jsic_middle_name"] or ""
        fin=financial_latest.get(r["corporate_number"] or "")
        temp.append({
            "source_id":r["source_id"],"keywords":keywords,"code":group_code,"name":group_name,
            "period":fin["fiscal_period"] if fin else "","revenue":fin["revenue_yen"] if fin else None,
            "label":fin["revenue_label"] if fin else "","net":fin["net_income_yen"] if fin else None,
        })
    rev_groups: dict[str,list[int]] = defaultdict(list); net_groups: dict[str,list[int]]=defaultdict(list)
    for x in temp:
        if x["code"] and x["revenue"] is not None: rev_groups[x["code"]].append(x["revenue"])
        if x["code"] and x["net"] is not None: net_groups[x["code"]].append(x["net"])
    rev_rank={k:{v:i+1 for i,v in enumerate(sorted(set(vals),reverse=True))} for k,vals in rev_groups.items()}
    net_rank={k:{v:i+1 for i,v in enumerate(sorted(set(vals),reverse=True))} for k,vals in net_groups.items()}
    ts=now_iso()
    for x in temp:
        con.execute(
            """INSERT INTO derived_company(source_id,keywords,industry_group_code,industry_group_name,latest_period,latest_revenue_yen,
            latest_revenue_label,latest_net_income_yen,revenue_rank,revenue_count,net_income_rank,net_income_count,derived_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (x["source_id"],x["keywords"],x["code"],x["name"],x["period"],x["revenue"],x["label"],x["net"],
             rev_rank.get(x["code"],{}).get(x["revenue"]) if x["revenue"] is not None else None,
             len(rev_groups.get(x["code"],[])) if x["revenue"] is not None else None,
             net_rank.get(x["code"],{}).get(x["net"]) if x["net"] is not None else None,
             len(net_groups.get(x["code"],[])) if x["net"] is not None else None,ts),
        )
    con.commit()
    return {"companies":len(temp),"revenue_ranked":sum(len(v) for v in rev_groups.values()),"net_income_ranked":sum(len(v) for v in net_groups.values())}


# ---------------- Export ----------------
def csv_writer(path: Path):
    path.parent.mkdir(parents=True,exist_ok=True)
    fh=path.open("w",encoding="utf-8-sig",newline="")
    return fh,csv.writer(fh)


def integrated_query(con: sqlite3.Connection) -> Iterable[sqlite3.Row]:
    return con.execute(
        """SELECT c.*,m.corporate_number,m.match_code,m.confidence,m.source_name AS match_source,m.status AS match_status,m.reason AS match_reason,
        (SELECT GROUP_CONCAT(DISTINCT NULLIF(mc.corporate_number,'')) FROM corporate_match_candidates mc WHERE mc.source_id=c.source_id) AS candidate_numbers,
        p.company_name AS public_company_name,p.name_kana AS public_name_kana,p.name_en AS public_name_en,
        p.postal_code AS public_postal_code,p.address AS public_address,p.corporate_status AS public_corporate_status,
        p.close_date AS public_close_date,p.representative_name AS public_representative_name,
        p.representative_position AS public_representative_position,p.capital_yen AS public_capital_yen,
        p.employees AS public_employees,p.established_date AS public_established_date,p.business_summary AS public_business_summary,
        p.website_url AS public_website_url,p.business_categories_json AS public_business_categories_json,
        p.source_quality AS public_source_quality,p.source_org AS public_source_org,p.updated_at AS public_updated_at,
        w.average_age AS workplace_average_age,w.updated_at AS workplace_updated_at,w.source_quality AS workplace_quality,
        e.average_age AS edinet_average_age,e.average_salary_yen,e.source_url AS edinet_source_url,e.period_end AS edinet_period_end,
        s.phone,s.evidence_url,s.confidence AS phone_confidence,
        d.keywords,d.industry_group_code,d.industry_group_name,d.latest_period,d.latest_revenue_yen,d.latest_revenue_label,
        d.latest_net_income_yen,d.revenue_rank,d.revenue_count,d.net_income_rank,d.net_income_count
        FROM companies c LEFT JOIN corporate_matches m ON m.source_id=c.source_id
        LEFT JOIN public_master p ON p.corporate_number=m.corporate_number
        LEFT JOIN workplace_info w ON w.corporate_number=m.corporate_number
        LEFT JOIN edinet_metrics e ON e.source_id=c.source_id
        LEFT JOIN site_contacts s ON s.source_id=c.source_id
        LEFT JOIN derived_company d ON d.source_id=c.source_id
        ORDER BY c.source_row"""
    )


def review_reason(row: sqlite3.Row) -> str:
    reasons=[]
    if not row["corporate_number"]: reasons.append("法人番号未確定")
    elif row["match_status"] != "accepted": reasons.append(row["match_reason"] or "法人番号要確認")
    if row["phone"] and (row["phone_confidence"] or 0)<0.75: reasons.append("電話番号候補の信頼度が低い")
    return " / ".join(reasons)


def getv(row: sqlite3.Row, key: str) -> Any:
    try: return row[key]
    except (IndexError,KeyError): return None


def export_all(con: sqlite3.Connection, output_dir: Path) -> dict[str,int]:
    output_dir.mkdir(parents=True,exist_ok=True)
    headers=json.loads(get_meta(con,"source_headers_json",json.dumps(SOURCE_HEADERS,ensure_ascii=False)))
    integrated_path=output_dir/"companies_enriched.csv"
    details_path=output_dir/"public_company_details.csv"
    ranking_path=output_dir/"industry_rankings.csv"
    review_path=output_dir/"review_required.csv"
    audit_path=output_dir/"source_audit.csv"
    financial_path=output_dir/"financial_history.csv"

    rows=list(integrated_query(con))
    fh,w=csv_writer(integrated_path); w.writerow(headers+PUBLIC_COLUMNS)
    detail_headers=["SOURCE_ID","企業名","法人番号","一致コード","一致信頼度","一致元","採用状態","公開法人名","郵便番号","登記住所","法人状態","代表者","代表者役職","資本金円","従業員数","設立年月日","WebサイトURL","電話番号","事業概要","事業種目","最新決算期","最新売上円","最新売上種別","最新純利益円","平均年齢","平均年収円","コアキーワード","データ品質","最終更新日","要確認理由"]
    fhd,wd=csv_writer(details_path); wd.writerow(detail_headers)
    fhr,wr=csv_writer(ranking_path); wr.writerow(["SOURCE_ID","企業名","法人番号","業種コード","業種名","最新決算期","最新売上円","売上順位","売上母数","最新純利益円","純利益順位","純利益母数","定義"])
    fhq,wq=csv_writer(review_path); wq.writerow(["SOURCE_ID","企業名","本店所在地","法人番号候補一覧","一致コード","一致信頼度","一致元","理由"])
    count=review_count=0
    for r in rows:
        original=json.loads(r["source_row_json"])
        categories=""
        try: categories=" / ".join(json.loads(getv(r,"public_business_categories_json") or "[]"))
        except Exception: categories=clean_text(getv(r,"public_business_categories_json"))
        age=getv(r,"edinet_average_age") if getv(r,"edinet_average_age") is not None else getv(r,"workplace_average_age")
        sources=[]
        if getv(r,"match_source"): sources.append(getv(r,"match_source"))
        if getv(r,"public_source_org"): sources.append(getv(r,"public_source_org"))
        if getv(r,"edinet_source_url"): sources.append("EDINET")
        if getv(r,"evidence_url"): sources.append("公式サイト")
        sources=" / ".join(dict.fromkeys(clean_text(x) for x in sources if clean_text(x)))
        qualities=" / ".join(dict.fromkeys(x for x in [clean_text(getv(r,"public_source_quality")),clean_text(getv(r,"workplace_quality"))] if x))
        updated=max([x for x in [clean_text(getv(r,"public_updated_at")),clean_text(getv(r,"workplace_updated_at")),clean_text(getv(r,"edinet_period_end"))] if x],default="")
        reason=review_reason(r)
        public=[
            getv(r,"corporate_number"),getv(r,"match_code"),getv(r,"confidence"),getv(r,"match_source"),getv(r,"match_status"),
            getv(r,"public_company_name"),getv(r,"public_name_kana"),getv(r,"public_name_en"),getv(r,"public_postal_code"),getv(r,"public_address"),
            getv(r,"public_corporate_status"),getv(r,"public_close_date"),getv(r,"public_representative_name"),getv(r,"public_representative_position"),
            getv(r,"public_capital_yen"),getv(r,"public_employees"),getv(r,"public_established_date"),getv(r,"public_website_url"),
            getv(r,"phone"),getv(r,"evidence_url"),getv(r,"public_business_summary"),categories,getv(r,"latest_period"),getv(r,"latest_revenue_yen"),
            getv(r,"latest_revenue_label"),getv(r,"latest_net_income_yen"),age,getv(r,"average_salary_yen"),getv(r,"keywords"),
            getv(r,"industry_group_code"),getv(r,"industry_group_name"),getv(r,"revenue_rank"),getv(r,"revenue_count"),getv(r,"net_income_rank"),
            getv(r,"net_income_count"),sources,qualities,updated,reason,getv(r,"candidate_numbers") or "",
        ]
        w.writerow(original+public)
        wd.writerow([
            r["source_id"],r["company_name"],public[0],public[1],public[2],public[3],public[4],public[5],public[8],public[9],public[10],
            public[12],public[13],public[14],public[15],public[16],public[17],public[18],public[20],public[21],public[22],public[23],
            public[24],public[25],public[26],public[27],public[28],qualities,updated,reason
        ])
        wr.writerow([r["source_id"],r["company_name"],getv(r,"corporate_number"),getv(r,"industry_group_code"),getv(r,"industry_group_name"),getv(r,"latest_period"),getv(r,"latest_revenue_yen"),getv(r,"revenue_rank"),getv(r,"revenue_count"),getv(r,"latest_net_income_yen"),getv(r,"net_income_rank"),getv(r,"net_income_count"),"JSIC細分類優先・最新公開財務・降順密順位"])
        if reason:
            wq.writerow([r["source_id"],r["company_name"],r["address"],getv(r,"candidate_numbers") or getv(r,"corporate_number"),getv(r,"match_code"),getv(r,"confidence"),getv(r,"match_source"),reason])
            review_count+=1
        count+=1
    for x in (fh,fhd,fhr,fhq): x.close()

    fhf,wf=csv_writer(financial_path)
    fin_headers=["法人番号","企業名","SOURCE_ID","事業年度","年度比較キー","会計基準","売上円","売上種別","売上原文","純利益円","純利益原文","経常利益円","資本金円","純資産円","総資産円","従業員数","データ品質","出典元","最終取得日","最終更新日","取込元ファイル"]
    wf.writerow(fin_headers)
    for x in con.execute("""SELECT f.*,p.company_name,c.source_id FROM financial_history f
        LEFT JOIN public_master p ON p.corporate_number=f.corporate_number
        LEFT JOIN corporate_matches m ON m.corporate_number=f.corporate_number AND m.status='accepted'
        LEFT JOIN companies c ON c.source_id=m.source_id ORDER BY f.corporate_number,f.fiscal_sort_key"""):
        wf.writerow([x["corporate_number"],x["company_name"],x["source_id"],x["fiscal_period"],x["fiscal_sort_key"],x["accounting_standard"],x["revenue_yen"],x["revenue_label"],x["revenue_raw"],x["net_income_yen"],x["net_income_raw"],x["ordinary_income_yen"],x["capital_yen"],x["net_assets_yen"],x["total_assets_yen"],x["employees"],x["source_quality"],x["source_org"],x["acquired_at"],x["updated_at"],x["source_file"]])
    fhf.close()

    fha,wa=csv_writer(audit_path)
    audit_headers=["ID","取込元","種別","SHA256","読込件数","採用件数","要確認件数","エラー件数","取込日時","備考"]
    wa.writerow(audit_headers)
    for a in con.execute("SELECT * FROM source_audit ORDER BY id"):
        wa.writerow([a["id"],a["source_file"],a["source_type"],a["sha256"],a["rows_read"],a["rows_accepted"],a["rows_review"],a["errors"],a["imported_at"],a["notes"]])
    fha.close()

    summary={
        "generated_at":now_iso(),"companies":count,"accepted_corporate_numbers":con.execute("SELECT COUNT(*) FROM corporate_matches WHERE status='accepted'").fetchone()[0],
        "review_rows":review_count,"public_master":con.execute("SELECT COUNT(*) FROM public_master").fetchone()[0],
        "financial_history":con.execute("SELECT COUNT(*) FROM financial_history").fetchone()[0],"workplace_info":con.execute("SELECT COUNT(*) FROM workplace_info").fetchone()[0],
        "edinet_metrics":con.execute("SELECT COUNT(*) FROM edinet_metrics").fetchone()[0],"site_contacts":con.execute("SELECT COUNT(*) FROM site_contacts").fetchone()[0],
        "derived_company":con.execute("SELECT COUNT(*) FROM derived_company").fetchone()[0],
        "files":[p.name for p in [integrated_path,details_path,financial_path,ranking_path,review_path,audit_path]],
    }
    (output_dir/"integration_summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    return {"integrated_rows":count,"review_rows":review_count}


# ---------------- Status/CLI ----------------
def status(con: sqlite3.Connection) -> dict[str,Any]:
    tables=["companies","corporate_matches","corporate_match_candidates","public_master","financial_history","workplace_info","edinet_metrics","site_contacts","derived_company","source_audit"]
    result={t:con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    result["accepted_matches"]=con.execute("SELECT COUNT(*) FROM corporate_matches WHERE status='accepted'").fetchone()[0]
    result["review_matches"]=con.execute("SELECT COUNT(*) FROM corporate_matches WHERE status='review'").fetchone()[0]
    result["integrity"]=con.execute("PRAGMA integrity_check").fetchone()[0]
    return result


def build_parser() -> argparse.ArgumentParser:
    p=argparse.ArgumentParser(description="企業データへ公開データを統合")
    p.add_argument("--db",type=Path,default=DEFAULT_DB)
    sub=p.add_subparsers(dest="command",required=True)
    sp=sub.add_parser("prepare",help="CSV/XLSXからローカルSQLiteを作成")
    sp.add_argument("input",type=Path); sp.add_argument("--sheet",default=""); sp.add_argument("--replace",action="store_true")
    sm=sub.add_parser("make-assignment",help="法人番号付与用CSVを生成")
    sm.add_argument("--output",type=Path,default=Path("法人番号付与用.csv")); sm.add_argument("--chunk-size",type=int,default=10000)
    si=sub.add_parser("import",help="公開CSV/ZIPを取込")
    si.add_argument("--input-dir",type=Path,default=Path("input")); si.add_argument("--accept-prefix",action="store_true")
    sd=sub.add_parser("derive",help="キーワードとランキングを再計算")
    se=sub.add_parser("export",help="統合CSVを出力"); se.add_argument("--output-dir",type=Path,default=Path("output/csv"))
    sr=sub.add_parser("run-all",help="取込→派生→出力"); sr.add_argument("--input-dir",type=Path,default=Path("input")); sr.add_argument("--output-dir",type=Path,default=Path("output/csv")); sr.add_argument("--accept-prefix",action="store_true")
    sub.add_parser("status",help="件数と整合性を表示")
    return p


def main() -> int:
    args=build_parser().parse_args()
    con=connect(args.db)
    if args.command == "prepare" and args.replace:
        drop_schema(con)
    init_schema(con)
    try:
        if args.command=="prepare": result=prepare_input(con,args.input,sheet_name=args.sheet,replace=args.replace)
        elif args.command=="make-assignment": result=make_assignment(con,args.output,args.chunk_size)
        elif args.command=="import": result=import_directory(con,args.input_dir,args.accept_prefix)
        elif args.command=="derive": result=derive(con)
        elif args.command=="export": result=export_all(con,args.output_dir)
        elif args.command=="run-all":
            result={"imports":import_directory(con,args.input_dir,args.accept_prefix),"derive":derive(con),"export":export_all(con,args.output_dir)}
        elif args.command=="status": result=status(con)
        else: raise AssertionError(args.command)
        print(json.dumps(result,ensure_ascii=False,indent=2,default=str))
        return 0
    finally:
        con.close()

if __name__=="__main__":
    raise SystemExit(main())
