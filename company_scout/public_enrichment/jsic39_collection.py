#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build and merge source-neutral JSIC middle-code 39 contact collection batches."""
from __future__ import annotations

import argparse
import csv
import glob
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

CSV_ENCODING = "utf-8-sig"


def clean(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in {"none", "null", "nan"} else text


def as_int(value: Any) -> int:
    text = re.sub(r"[^0-9-]", "", clean(value))
    try:
        return int(text) if text else 0
    except ValueError:
        return 0


def normalize_url(value: Any) -> str:
    text = clean(value)
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text.lstrip("/")
    parsed = urlparse(text)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ""
    return text


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding=CSV_ENCODING, newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, fieldnames: list[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding=CSV_ENCODING, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def source_id(row: dict[str, str], index: int) -> str:
    corporate_number = clean(row.get("corporate_number"))
    if re.fullmatch(r"\d{13}", corporate_number):
        return f"corp-{corporate_number}"
    return f"row-{index + 1:08d}"


def priority_key(row: dict[str, str]) -> tuple[int, int, str]:
    """Prefer larger employers, then higher capital, while remaining deterministic."""
    return (
        -as_int(row.get("employee_number")),
        -as_int(row.get("capital_stock")),
        clean(row.get("corporate_number")),
    )


def prepare_shard(
    *,
    companies_csv: Path,
    database: Path,
    manifest: Path,
    offset: int,
    limit: int,
    summary: Path | None,
) -> dict[str, Any]:
    if offset < 0 or limit < 1:
        raise ValueError("offset must be >= 0 and limit must be >= 1")

    rows = [row for row in read_csv(companies_csv) if normalize_url(row.get("company_url"))]
    rows.sort(key=priority_key)
    selected = rows[offset : offset + limit]

    database.parent.mkdir(parents=True, exist_ok=True)
    if database.exists():
        database.unlink()
    con = sqlite3.connect(database)
    try:
        con.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE companies(
                source_id TEXT PRIMARY KEY,
                source_row INTEGER NOT NULL,
                company_name TEXT NOT NULL,
                address TEXT,
                security_code TEXT
            );
            CREATE TABLE corporate_matches(
                source_id TEXT PRIMARY KEY REFERENCES companies(source_id) ON DELETE CASCADE,
                corporate_number TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE public_master(
                corporate_number TEXT PRIMARY KEY,
                website_url TEXT NOT NULL
            );
            """
        )
        manifest_rows: list[dict[str, Any]] = []
        for local_index, row in enumerate(selected, start=offset):
            sid = source_id(row, local_index)
            corporate_number = clean(row.get("corporate_number"))
            company_name = clean(row.get("company_name")) or corporate_number
            address = clean(row.get("prefecture_name")) + clean(row.get("city_name"))
            website = normalize_url(row.get("company_url"))
            con.execute(
                "INSERT INTO companies(source_id,source_row,company_name,address,security_code) VALUES(?,?,?,?,?)",
                (sid, local_index + 1, company_name, address, ""),
            )
            con.execute(
                "INSERT INTO corporate_matches(source_id,corporate_number,status) VALUES(?,?,?)",
                (sid, corporate_number, "accepted"),
            )
            con.execute(
                "INSERT OR REPLACE INTO public_master(corporate_number,website_url) VALUES(?,?)",
                (corporate_number, website),
            )
            manifest_rows.append(
                {
                    "SOURCE_ID": sid,
                    "法人番号": corporate_number,
                    "企業名": company_name,
                    "都道府県": clean(row.get("prefecture_name")),
                    "市区町村": clean(row.get("city_name")),
                    "従業員数": clean(row.get("employee_number")),
                    "資本金": clean(row.get("capital_stock")),
                    "公式サイトURL": website,
                    "優先順位": local_index + 1,
                }
            )
        con.commit()
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        con.close()

    manifest_fields = [
        "SOURCE_ID",
        "法人番号",
        "企業名",
        "都道府県",
        "市区町村",
        "従業員数",
        "資本金",
        "公式サイトURL",
        "優先順位",
    ]
    write_csv(manifest, manifest_fields, manifest_rows)
    result = {
        "companies_with_web": len(rows),
        "offset": offset,
        "limit": limit,
        "selected": len(selected),
        "database": str(database),
        "manifest": str(manifest),
        "integrity": integrity,
    }
    if summary:
        summary.parent.mkdir(parents=True, exist_ok=True)
        summary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def _expand_patterns(patterns: list[str]) -> list[Path]:
    paths: list[Path] = []
    seen: set[Path] = set()
    for pattern in patterns:
        matches = [Path(item) for item in glob.glob(pattern, recursive=True)]
        if not matches and Path(pattern).is_file():
            matches = [Path(pattern)]
        for path in matches:
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                paths.append(path)
    return sorted(paths)


def merge_batches(
    *,
    all_companies_csv: Path,
    manifests: list[str],
    phone_files: list[str],
    output: Path,
    summary: Path,
) -> dict[str, Any]:
    all_rows = read_csv(all_companies_csv)
    manifest_paths = _expand_patterns(manifests)
    phone_paths = _expand_patterns(phone_files)

    processed: set[str] = set()
    for path in manifest_paths:
        for row in read_csv(path):
            processed.add(clean(row.get("法人番号")))

    phones: dict[str, dict[str, str]] = {}
    for path in phone_paths:
        for row in read_csv(path):
            corporate_number = clean(row.get("法人番号"))
            if not corporate_number:
                continue
            current = phones.get(corporate_number)
            candidate_confidence = float(clean(row.get("信頼度")) or 0)
            current_confidence = float(clean(current.get("信頼度")) or 0) if current else -1
            if current is None or candidate_confidence > current_confidence:
                phones[corporate_number] = row

    output_rows: list[dict[str, Any]] = []
    website_count = phone_count = processed_count = 0
    for row in all_rows:
        corporate_number = clean(row.get("corporate_number"))
        website = normalize_url(row.get("company_url"))
        phone = phones.get(corporate_number, {})
        if website:
            website_count += 1
        if corporate_number in processed:
            processed_count += 1
        if clean(phone.get("電話番号")):
            phone_count += 1
        if clean(phone.get("電話番号")):
            status = "phone_candidate_found"
        elif corporate_number in processed:
            status = "processed_no_phone"
        elif website:
            status = "website_pending"
        else:
            status = "website_missing"
        output_rows.append(
            {
                "法人番号": corporate_number,
                "企業名": clean(row.get("company_name")),
                "都道府県": clean(row.get("prefecture_name")),
                "市区町村": clean(row.get("city_name")),
                "JSIC大分類": clean(row.get("jsic_major_codes")),
                "JSIC中分類": clean(row.get("jsic_middle_codes")),
                "従業員数": clean(row.get("employee_number")),
                "資本金": clean(row.get("capital_stock")),
                "代表者": clean(row.get("representative_name")),
                "公式サイトURL": website,
                "事業概要": clean(row.get("business_summary")),
                "電話番号候補": clean(phone.get("電話番号")),
                "電話番号数字": re.sub(r"\D", "", clean(phone.get("電話番号"))),
                "電話根拠URL": clean(phone.get("根拠URL")),
                "電話根拠テキスト": clean(phone.get("根拠テキスト")),
                "電話信頼度": clean(phone.get("信頼度")),
                "電話取得日時": clean(phone.get("取得日時")),
                "収集状態": status,
            }
        )

    fields = [
        "法人番号",
        "企業名",
        "都道府県",
        "市区町村",
        "JSIC大分類",
        "JSIC中分類",
        "従業員数",
        "資本金",
        "代表者",
        "公式サイトURL",
        "事業概要",
        "電話番号候補",
        "電話番号数字",
        "電話根拠URL",
        "電話根拠テキスト",
        "電話信頼度",
        "電話取得日時",
        "収集状態",
    ]
    write_csv(output, fields, output_rows)
    result = {
        "companies": len(all_rows),
        "companies_with_web": website_count,
        "processed_for_phone": processed_count,
        "phone_candidates_found": phone_count,
        "manifest_files": [str(path) for path in manifest_paths],
        "phone_files": [str(path) for path in phone_paths],
        "output": str(output),
    }
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="JSIC 39 public contact collection helpers")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare-shard")
    prepare.add_argument("--companies", type=Path, required=True)
    prepare.add_argument("--db", type=Path, required=True)
    prepare.add_argument("--manifest", type=Path, required=True)
    prepare.add_argument("--summary", type=Path)
    prepare.add_argument("--offset", type=int, default=0)
    prepare.add_argument("--limit", type=int, default=100)

    merge = sub.add_parser("merge")
    merge.add_argument("--all-companies", type=Path, required=True)
    merge.add_argument("--manifest", action="append", default=[], required=True)
    merge.add_argument("--phones", action="append", default=[], required=True)
    merge.add_argument("--output", type=Path, required=True)
    merge.add_argument("--summary", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "prepare-shard":
        prepare_shard(
            companies_csv=args.companies,
            database=args.db,
            manifest=args.manifest,
            offset=args.offset,
            limit=args.limit,
            summary=args.summary,
        )
        return 0
    if args.command == "merge":
        merge_batches(
            all_companies_csv=args.all_companies,
            manifests=args.manifest,
            phone_files=args.phones,
            output=args.output,
            summary=args.summary,
        )
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
