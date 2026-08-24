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
PHONE_TYPE_PRIORITY = {
    "代表電話": 0,
    "本社電話": 1,
    "問い合わせ電話": 2,
    "支店・事業所": 3,
    "サポート窓口": 4,
    "広報・IR窓口": 5,
    "採用窓口": 6,
    "個人情報・相談窓口": 7,
    "未分類": 8,
    "FAX": 9,
}


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
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
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
            local_source_id = source_id(row, local_index)
            corporate_number = clean(row.get("corporate_number"))
            company_name = clean(row.get("company_name")) or corporate_number
            address = clean(row.get("prefecture_name")) + clean(row.get("city_name"))
            website = normalize_url(row.get("company_url"))
            connection.execute(
                "INSERT INTO companies(source_id,source_row,company_name,address,security_code) VALUES(?,?,?,?,?)",
                (local_source_id, local_index + 1, company_name, address, ""),
            )
            connection.execute(
                "INSERT INTO corporate_matches(source_id,corporate_number,status) VALUES(?,?,?)",
                (local_source_id, corporate_number, "accepted"),
            )
            connection.execute(
                "INSERT OR REPLACE INTO public_master(corporate_number,website_url) VALUES(?,?)",
                (corporate_number, website),
            )
            manifest_rows.append(
                {
                    "SOURCE_ID": local_source_id,
                    "法人番号": corporate_number,
                    "企業名": company_name,
                    "都道府県": clean(row.get("prefecture_name")),
                    "市区町村": clean(row.get("city_name")),
                    "スコープ": clean(row.get("scope_label")),
                    "データ世代": clean(row.get("dataset_generation")),
                    "JSIC大分類": clean(row.get("jsic_major_codes")),
                    "JSIC中分類": clean(row.get("jsic_middle_codes")),
                    "従業員数": clean(row.get("employee_number")),
                    "資本金": clean(row.get("capital_stock")),
                    "公式サイトURL": website,
                    "優先順位": local_index + 1,
                }
            )
        connection.commit()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()

    manifest_fields = [
        "SOURCE_ID",
        "法人番号",
        "企業名",
        "都道府県",
        "市区町村",
        "スコープ",
        "データ世代",
        "JSIC大分類",
        "JSIC中分類",
        "従業員数",
        "資本金",
        "公式サイトURL",
        "優先順位",
    ]
    write_csv(manifest, manifest_fields, manifest_rows)
    result = {
        "companies_with_web": len(rows),
        "scope_labels": sorted({clean(row.get("scope_label")) for row in selected if clean(row.get("scope_label"))}),
        "dataset_generations": sorted({clean(row.get("dataset_generation")) for row in selected if clean(row.get("dataset_generation"))}),
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


def _read_progress_jsonl(patterns: list[str]) -> tuple[dict[str, dict[str, Any]], list[Path]]:
    """Return the latest durable completion record for each corporate number."""
    paths = _expand_patterns(patterns)
    latest: dict[str, dict[str, Any]] = {}
    for path in paths:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Invalid progress JSONL: {path}:{line_number}") from exc
                corporate_number = clean(record.get("corporate_number"))
                state = clean(record.get("state"))
                if not corporate_number or not state:
                    raise ValueError(f"Progress record lacks corporate_number/state: {path}:{line_number}")
                latest[corporate_number] = record
    return latest, paths


def _candidate_key(row: dict[str, str]) -> tuple[float, int, int, int]:
    confidence = float(clean(row.get("信頼度")) or 0)
    rank = as_int(row.get("候補順位")) or 999999
    phone_type = clean(row.get("電話種別候補")) or "未分類"
    evidence = int(bool(clean(row.get("根拠テキスト"))))
    return (
        confidence,
        -PHONE_TYPE_PRIORITY.get(phone_type, 8),
        evidence,
        -rank,
    )


def merge_batches(
    *,
    all_companies_csv: Path,
    manifests: list[str],
    phone_files: list[str],
    progress_files: list[str] | None = None,
    legacy_manifest_completion: bool = False,
    scope_label: str = "JSIC39",
    output: Path,
    summary: Path,
) -> dict[str, Any]:
    all_rows = read_csv(all_companies_csv)
    manifest_paths = _expand_patterns(manifests)
    phone_paths = _expand_patterns(phone_files)

    targeted: set[str] = set()
    for path in manifest_paths:
        for row in read_csv(path):
            corporate_number = clean(row.get("法人番号"))
            if corporate_number:
                targeted.add(corporate_number)

    requested_progress = list(progress_files or [])
    missing_progress_patterns = [
        pattern
        for pattern in requested_progress
        if not glob.glob(pattern, recursive=True) and not Path(pattern).is_file()
    ]
    if missing_progress_patterns:
        raise FileNotFoundError(f"Requested progress artifact was not found: {missing_progress_patterns}")
    progress_by_company, progress_paths = _read_progress_jsonl(requested_progress)
    if not progress_paths and not legacy_manifest_completion:
        raise ValueError("progress artifact is required unless legacy_manifest_completion is explicit")
    processed = set(progress_by_company) if progress_paths else set(targeted)

    candidates_by_company: dict[str, dict[str, dict[str, str]]] = {}
    for path in phone_paths:
        for row in read_csv(path):
            corporate_number = clean(row.get("法人番号"))
            phone_digits = re.sub(r"\D", "", clean(row.get("電話番号")))
            if not corporate_number or not phone_digits:
                continue
            company_candidates = candidates_by_company.setdefault(corporate_number, {})
            current = company_candidates.get(phone_digits)
            if current is None or _candidate_key(row) > _candidate_key(current):
                company_candidates[phone_digits] = row

    # The append-only progress log is the crash-safe source of truth. Rebuild
    # candidate rows from it so a process interruption between progress commit
    # and CSV export cannot lose successful evidence.
    for corporate_number, progress_record in progress_by_company.items():
        for rank, candidate in enumerate(progress_record.get("candidates") or [], start=1):
            phone_digits = re.sub(r"\D", "", clean(candidate.get("phone")))
            if not phone_digits:
                continue
            row = {
                "法人番号": corporate_number,
                "候補順位": str(rank),
                "電話番号": clean(candidate.get("phone")),
                "電話種別候補": clean(candidate.get("candidate_type")) or "未分類",
                "根拠URL": clean(candidate.get("url")),
                "根拠テキスト": clean(candidate.get("context")),
                "抽出方法": clean(candidate.get("source")),
                "信頼度": str(candidate.get("score") or 0),
                "取得日時": clean(progress_record.get("completed_at")),
            }
            company_candidates = candidates_by_company.setdefault(corporate_number, {})
            current = company_candidates.get(phone_digits)
            if current is None or _candidate_key(row) > _candidate_key(current):
                company_candidates[phone_digits] = row

    output_rows: list[dict[str, Any]] = []
    website_count = processed_count = companies_with_phone = candidate_total = 0
    companies_with_voice = fax_only_companies = voice_candidate_total = 0
    for row in all_rows:
        corporate_number = clean(row.get("corporate_number"))
        website = normalize_url(row.get("company_url"))
        candidates = sorted(
            candidates_by_company.get(corporate_number, {}).values(),
            key=_candidate_key,
            reverse=True,
        )
        best = candidates[0] if candidates else {}
        voice_candidates = [candidate for candidate in candidates if clean(candidate.get("電話種別候補")) != "FAX"]
        if website:
            website_count += 1
        if corporate_number in processed:
            processed_count += 1
        if candidates:
            companies_with_phone += 1
            candidate_total += len(candidates)
        if voice_candidates:
            companies_with_voice += 1
            voice_candidate_total += len(voice_candidates)
        elif candidates:
            fax_only_companies += 1
        if voice_candidates:
            status = "phone_candidate_found"
        elif candidates:
            status = "fax_only"
        elif corporate_number in processed:
            progress_state = clean(progress_by_company.get(corporate_number, {}).get("state"))
            status = progress_state if progress_state and progress_state != "phone_candidate_found" else "processed_no_phone"
        elif website:
            status = "website_pending"
        else:
            status = "website_missing"

        candidate_payload = [
            {
                "rank": index,
                "phone": clean(candidate.get("電話番号")),
                "phone_digits": re.sub(r"\D", "", clean(candidate.get("電話番号"))),
                "candidate_type": clean(candidate.get("電話種別候補")) or "未分類",
                "evidence_url": clean(candidate.get("根拠URL")),
                "evidence_text": clean(candidate.get("根拠テキスト")),
                "extraction_method": clean(candidate.get("抽出方法")),
                "confidence": clean(candidate.get("信頼度")),
                "fetched_at": clean(candidate.get("取得日時")),
            }
            for index, candidate in enumerate(candidates, start=1)
        ]
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
                "電話番号候補": clean(best.get("電話番号")),
                "電話番号数字": re.sub(r"\D", "", clean(best.get("電話番号"))),
                "電話種別候補": clean(best.get("電話種別候補")),
                "電話根拠URL": clean(best.get("根拠URL")),
                "電話根拠テキスト": clean(best.get("根拠テキスト")),
                "電話信頼度": clean(best.get("信頼度")),
                "電話取得日時": clean(best.get("取得日時")),
                "電話候補件数": len(candidates),
                "電話候補一覧JSON": json.dumps(candidate_payload, ensure_ascii=False, separators=(",", ":")),
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
        "電話種別候補",
        "電話根拠URL",
        "電話根拠テキスト",
        "電話信頼度",
        "電話取得日時",
        "電話候補件数",
        "電話候補一覧JSON",
        "収集状態",
    ]
    write_csv(output, fields, output_rows)
    result = {
        "scope": scope_label,
        "companies": len(all_rows),
        "companies_with_web": website_count,
        "targeted_for_phone": len(targeted),
        "processed_for_phone": processed_count,
        "companies_with_phone_candidates": companies_with_phone,
        "phone_candidates_total": candidate_total,
        "companies_with_voice_candidates": companies_with_voice,
        "voice_phone_candidates_total": voice_candidate_total,
        "fax_only_companies": fax_only_companies,
        "manifest_files": [str(path) for path in manifest_paths],
        "phone_files": [str(path) for path in phone_paths],
        "progress_files": [str(path) for path in progress_paths],
        "legacy_manifest_completion": legacy_manifest_completion,
        "output": str(output),
    }
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="JSIC 39 public contact collection helpers")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare-shard")
    prepare.add_argument("--companies", type=Path, required=True)
    prepare.add_argument("--db", type=Path, required=True)
    prepare.add_argument("--manifest", type=Path, required=True)
    prepare.add_argument("--summary", type=Path)
    prepare.add_argument("--offset", type=int, default=0)
    prepare.add_argument("--limit", type=int, default=100)

    merge = subparsers.add_parser("merge")
    merge.add_argument("--all-companies", type=Path, required=True)
    merge.add_argument("--manifest", action="append", default=[], required=True)
    merge.add_argument("--phones", action="append", default=[], required=True)
    merge.add_argument("--progress", action="append", default=[])
    merge.add_argument(
        "--legacy-manifest-completion",
        action="store_true",
        help="旧成果物のみ: manifestを処理済み証拠として扱う",
    )
    merge.add_argument("--scope-label", default="JSIC39")
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
            progress_files=args.progress,
            legacy_manifest_completion=args.legacy_manifest_completion,
            scope_label=args.scope_label,
            output=args.output,
            summary=args.summary,
        )
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
