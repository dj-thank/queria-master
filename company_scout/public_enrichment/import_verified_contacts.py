#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Import verified public contacts into a local public-enrichment SQLite database."""
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def clean(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in {"none", "null", "nan"} else text


def normalize_header(value: Any) -> str:
    return re.sub(
        r"[\s\u3000_\-–—・:：()（）\[\]【】/\\]+",
        "",
        unicodedata.normalize("NFKC", clean(value)).lower(),
    )


def normalize_name(value: Any) -> str:
    text = unicodedata.normalize("NFKC", clean(value)).lower()
    for token in (
        "株式会社", "有限会社", "合同会社", "一般社団法人", "一般財団法人",
        "公益社団法人", "公益財団法人", "(株)", "㈱", "(有)", "㈲",
        "inc.", "inc", "co.,ltd.",
    ):
        text = text.replace(token, "")
    return re.sub(r"[\s\u3000・･\.．,，'’\"“”\-‐–—_()（）\[\]【】/\\]+", "", text)


def normalize_address(value: Any) -> str:
    text = unicodedata.normalize("NFKC", clean(value)).lower()
    for key, digit in {
        "〇":"0", "一":"1", "二":"2", "三":"3", "四":"4",
        "五":"5", "六":"6", "七":"7", "八":"8", "九":"9",
    }.items():
        text = text.replace(key, digit)
    text = text.replace("丁目", "-").replace("番地", "-").replace("番", "-").replace("号", "")
    text = re.sub(r"[\s\u3000・･\.．,，'’\"“”()（）\[\]【】/\\]+", "", text)
    return re.sub(r"[-‐‑‒–—―ー]+", "-", text).strip("-")


def normalize_security_code(value: Any) -> str:
    text = re.sub(r"[^0-9A-Z]", "", unicodedata.normalize("NFKC", clean(value)).upper())
    return text[:4] if len(text) >= 4 else ""


def find_key(fields, aliases):
    normalized = {normalize_header(field): field for field in fields if field is not None}
    return next((normalized[normalize_header(alias)] for alias in aliases if normalize_header(alias) in normalized), None)


def get_value(row, aliases) -> str:
    key = find_key(row.keys(), aliases)
    return clean(row.get(key, "")) if key else ""


def ensure_schema(con: sqlite3.Connection) -> None:
    if not con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='companies'"
    ).fetchone():
        raise RuntimeError("companies table not found; run prepare first")
    con.execute(
        """CREATE TABLE IF NOT EXISTS site_contacts(
        source_id TEXT PRIMARY KEY REFERENCES companies(source_id) ON DELETE CASCADE,
        phone TEXT,
        evidence_url TEXT,
        evidence_text TEXT,
        confidence REAL,
        fetched_at TEXT,
        raw_json TEXT
        )"""
    )
    additions = {
        "website_url":"TEXT", "phone_type":"TEXT", "phone_purpose":"TEXT",
        "is_representative":"INTEGER", "alternate_phone":"TEXT",
        "alternate_phone_purpose":"TEXT", "postal_code":"TEXT",
        "head_office_address":"TEXT", "registered_address":"TEXT",
        "source_type":"TEXT", "verified_at":"TEXT", "public_company_name":"TEXT",
        "former_company_name":"TEXT", "public_contact_id":"TEXT",
        "source_file":"TEXT", "match_method":"TEXT",
    }
    existing = {row[1] for row in con.execute("PRAGMA table_info(site_contacts)")}
    for column, sql_type in additions.items():
        if column not in existing:
            con.execute(f'ALTER TABLE site_contacts ADD COLUMN "{column}" {sql_type}')
    con.execute(
        """CREATE TABLE IF NOT EXISTS verified_contact_import_audit(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        public_contact_id TEXT,
        source_id TEXT,
        company_name TEXT,
        status TEXT,
        match_method TEXT,
        reason TEXT,
        source_file TEXT,
        imported_at TEXT,
        raw_json TEXT
        )"""
    )
    con.commit()


def company_indexes(con: sqlite3.Connection):
    columns = {row[1] for row in con.execute("PRAGMA table_info(companies)")}
    if not {"source_id", "company_name", "address"}.issubset(columns):
        raise RuntimeError("companies requires source_id, company_name and address")
    security_expr = "security_code" if "security_code" in columns else "'' AS security_code"
    by_id = {}
    by_security_name = defaultdict(list)
    by_name_address = defaultdict(list)
    for row in con.execute(
        f"SELECT source_id,company_name,address,{security_expr} FROM companies"
    ):
        by_id[clean(row["source_id"])] = row
        name_key = normalize_name(row["company_name"])
        address_key = normalize_address(row["address"])
        security = normalize_security_code(row["security_code"])
        if security and name_key:
            by_security_name[(security, name_key)].append(row)
        if name_key and address_key:
            by_name_address[(name_key, address_key)].append(row)
    return by_id, by_security_name, by_name_address


def resolve_company(row, indexes):
    by_id, by_security_name, by_name_address = indexes
    source_id = get_value(row, ["SOURCE_ID", "source_id"])
    if source_id:
        target = by_id.get(source_id)
        return target, "SOURCE_ID", "" if target else "unknown SOURCE_ID"

    company_name = get_value(row, ["照合企業名", "企業名", "会社名", "法人名"])
    name_key = normalize_name(company_name)
    security = normalize_security_code(get_value(row, ["証券コード", "security_code"]))
    if security and name_key:
        candidates = by_security_name.get((security, name_key), [])
        if len(candidates) == 1:
            return candidates[0], "証券コード＋企業名", ""
        if len(candidates) > 1:
            return None, "証券コード＋企業名", "multiple candidates"

    address = get_value(row, ["照合所在地", "本店所在地", "所在地", "住所"])
    address_key = normalize_address(address)
    if name_key and address_key:
        candidates = by_name_address.get((name_key, address_key), [])
        if len(candidates) == 1:
            return candidates[0], "企業名＋所在地完全一致", ""
        if len(candidates) > 1:
            return None, "企業名＋所在地完全一致", "multiple candidates"

    return None, "未照合", "company-name-only matching is not accepted"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("output/company_public_data.sqlite3"))
    parser.add_argument("--contacts", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("output/csv/verified_contacts_reflected.csv"))
    parser.add_argument("--replace-source", action="store_true")
    args = parser.parse_args()

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    ensure_schema(con)
    indexes = company_indexes(con)
    with args.contacts.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    if args.replace_source:
        con.execute("DELETE FROM site_contacts WHERE source_file=?", (args.contacts.name,))
        con.execute("DELETE FROM verified_contact_import_audit WHERE source_file=?", (args.contacts.name,))

    accepted = review = invalid = 0
    methods = defaultdict(int)
    reflected = []
    imported_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    for row in rows:
        phone = get_value(row, ["電話番号", "代表電話"])
        evidence_url = get_value(row, ["根拠URL", "出典URL"])
        target, method, reason = resolve_company(row, indexes)
        if len(re.sub(r"\D", "", phone)) not in {10, 11} or not evidence_url.startswith("https://"):
            status = "invalid"
            invalid += 1
        elif target is None:
            status = "review"
            review += 1
        else:
            status = "accepted"
            accepted += 1
            methods[method] += 1
            values = {
                "source_id": target["source_id"],
                "phone": phone,
                "evidence_url": evidence_url,
                "evidence_text": get_value(row, ["根拠概要", "根拠テキスト", "根拠ページ"]),
                "confidence": float(get_value(row, ["信頼度"]) or 0.95),
                "fetched_at": get_value(row, ["確認日", "取得日時"]) or imported_at,
                "raw_json": json.dumps(row, ensure_ascii=False, separators=(",", ":")),
                "website_url": get_value(row, ["公式サイトURL", "WebサイトURL"]),
                "phone_type": get_value(row, ["電話種別"]),
                "phone_purpose": get_value(row, ["電話用途"]),
                "is_representative": 1 if get_value(row, ["代表電話フラグ"]) in {"1", "true", "True"} else 0,
                "alternate_phone": get_value(row, ["補助電話番号"]),
                "alternate_phone_purpose": get_value(row, ["補助電話用途"]),
                "postal_code": get_value(row, ["郵便番号"]),
                "head_office_address": get_value(row, ["本社所在地"]),
                "registered_address": get_value(row, ["登記・本店所在地", "登記住所"]),
                "source_type": get_value(row, ["出典区分"]),
                "verified_at": get_value(row, ["確認日"]) or imported_at,
                "public_company_name": get_value(row, ["公開法人名"]),
                "former_company_name": get_value(row, ["旧法人名"]),
                "public_contact_id": get_value(row, ["PUBLIC_CONTACT_ID"]),
                "source_file": args.contacts.name,
                "match_method": method,
            }
            columns = list(values)
            placeholders = ",".join("?" for _ in columns)
            updates = ",".join(
                f'"{column}"=excluded."{column}"'
                for column in columns if column != "source_id"
            )
            quoted_columns = ",".join(f'"{column}"' for column in columns)
            con.execute(
                f"INSERT INTO site_contacts({quoted_columns}) VALUES({placeholders}) "
                f"ON CONFLICT(source_id) DO UPDATE SET {updates}",
                [values[column] for column in columns],
            )
            reflected.append([
                target["source_id"], target["company_name"], target["address"], phone,
                get_value(row, ["電話種別"]), get_value(row, ["電話用途"]),
                evidence_url, method,
            ])

        con.execute(
            """INSERT INTO verified_contact_import_audit(
            public_contact_id,source_id,company_name,status,match_method,reason,
            source_file,imported_at,raw_json) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                get_value(row, ["PUBLIC_CONTACT_ID"]),
                target["source_id"] if target else "",
                get_value(row, ["照合企業名"]), status, method, reason,
                args.contacts.name, imported_at,
                json.dumps(row, ensure_ascii=False, separators=(",", ":")),
            ),
        )

    con.commit()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "SOURCE_ID", "企業名", "本店所在地", "電話番号", "電話種別",
            "電話用途", "根拠URL", "照合方法",
        ])
        writer.writerows(reflected)

    result = {
        "rows_read": len(rows),
        "accepted": accepted,
        "review": review,
        "invalid": invalid,
        "match_methods": dict(methods),
        "integrity": con.execute("PRAGMA integrity_check").fetchone()[0],
        "output": str(args.output),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    con.close()
    return 0 if invalid == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
