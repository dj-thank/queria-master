#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Match a caller-local company CSV against a public corporate-number Parquet index."""
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any

import duckdb

CSV_ENCODING = "utf-8-sig"
NAME_ALIASES = ["企業名", "会社名", "法人名", "商号又は名称", "company_name", "name"]
ADDRESS_ALIASES = ["所在地", "本店所在地", "住所", "full_address", "address"]
SOURCE_ID_ALIASES = ["SOURCE_ID", "LOCAL_SOURCE_ID", "source_id", "id"]
CORPORATE_DESIGNATORS = (
    "株式会社",
    "有限会社",
    "合同会社",
    "合資会社",
    "合名会社",
    "一般社団法人",
    "一般財団法人",
    "公益社団法人",
    "公益財団法人",
    "社会福祉法人",
    "医療法人",
    "学校法人",
    "宗教法人",
    "特定非営利活動法人",
    "独立行政法人",
    "地方独立行政法人",
    "国立大学法人",
)


def clean(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in {"none", "null", "nan"} else text


def normalize_header(value: Any) -> str:
    return re.sub(
        r"[\s\u3000_\-–—・:：()（）\[\]【】/\\]+",
        "",
        unicodedata.normalize("NFKC", clean(value)).lower(),
    )


def find_key(fields: list[str], aliases: list[str]) -> str | None:
    normalized = {normalize_header(field): field for field in fields}
    for alias in aliases:
        key = normalized.get(normalize_header(alias))
        if key:
            return key
    return None


def normalize_name(value: Any) -> str:
    text = unicodedata.normalize("NFKC", clean(value)).lower()
    for token in CORPORATE_DESIGNATORS:
        text = text.replace(token.lower(), "")
    text = text.replace("㈱", "").replace("(株)", "").replace("（株）", "")
    text = text.replace("㈲", "").replace("(有)", "").replace("（有）", "")
    return re.sub(r"[\s\u3000・･\.．,，'’\"“”\-‐–—_()（）\[\]【】/\\]+", "", text)


def normalize_address(value: Any) -> str:
    text = unicodedata.normalize("NFKC", clean(value)).lower()
    for source, target in {
        "〇": "0",
        "一": "1",
        "二": "2",
        "三": "3",
        "四": "4",
        "五": "5",
        "六": "6",
        "七": "7",
        "八": "8",
        "九": "9",
    }.items():
        text = text.replace(source, target)
    text = (
        text.replace("丁目", "-")
        .replace("番地", "-")
        .replace("番", "-")
        .replace("号", "")
        .replace("大字", "")
        .replace("字", "")
    )
    text = re.sub(r"[\s\u3000・･\.．,，'’\"“”()（）\[\]【】/\\]+", "", text)
    text = re.sub(r"[-‐‑‒–—―ー]+", "", text)
    return text


def read_targets(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding=CSV_ENCODING, newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        source_key = find_key(fields, SOURCE_ID_ALIASES)
        name_key = find_key(fields, NAME_ALIASES)
        address_key = find_key(fields, ADDRESS_ALIASES)
        if not name_key or not address_key:
            raise ValueError("input CSV requires company-name and address columns")
        rows: list[dict[str, str]] = []
        for index, row in enumerate(reader, start=1):
            source_id = clean(row.get(source_key, "")) if source_key else ""
            company_name = clean(row.get(name_key, ""))
            address = clean(row.get(address_key, ""))
            if not source_id:
                source_id = f"row-{index:08d}"
            if not company_name or not address:
                continue
            rows.append(
                {
                    "source_id": source_id,
                    "company_name": company_name,
                    "address": address,
                    "name_norm": normalize_name(company_name),
                    "address_norm": normalize_address(address),
                }
            )
    return rows


def _register_targets(connection: duckdb.DuckDBPyConnection, rows: list[dict[str, str]]) -> None:
    connection.execute(
        """
        CREATE TEMP TABLE targets(
            source_id VARCHAR,
            company_name VARCHAR,
            address VARCHAR,
            name_norm VARCHAR,
            address_norm VARCHAR
        )
        """
    )
    connection.executemany(
        "INSERT INTO targets VALUES(?,?,?,?,?)",
        [
            (
                row["source_id"],
                row["company_name"],
                row["address"],
                row["name_norm"],
                row["address_norm"],
            )
            for row in rows
        ],
    )


def _install_normalizers(connection: duckdb.DuckDBPyConnection) -> None:
    connection.create_function("py_norm_name", normalize_name, [str], str)
    connection.create_function("py_norm_address", normalize_address, [str], str)


def match_index(
    *,
    targets_csv: Path,
    public_index: Path,
    output: Path,
    review_output: Path,
    summary_output: Path,
    accept_prefix: bool = False,
) -> dict[str, Any]:
    targets = read_targets(targets_csv)
    connection = duckdb.connect()
    try:
        connection.execute("PRAGMA threads=4")
        _register_targets(connection, targets)
        _install_normalizers(connection)
        connection.execute(
            """
            CREATE TEMP TABLE candidate_matches AS
            WITH public_index AS (
                SELECT
                    corporate_number,
                    company_name AS public_company_name,
                    post_code,
                    prefecture_name,
                    city_name,
                    street_number,
                    full_address AS public_address,
                    nta_update_date,
                    py_norm_name(company_name) AS name_norm,
                    py_norm_address(full_address) AS address_norm
                FROM read_parquet(?)
            ),
            scored AS (
                SELECT
                    t.source_id,
                    t.company_name,
                    t.address,
                    p.corporate_number,
                    p.public_company_name,
                    p.post_code,
                    p.prefecture_name,
                    p.city_name,
                    p.street_number,
                    p.public_address,
                    p.nta_update_date,
                    CASE
                        WHEN t.address_norm = p.address_norm THEN 'name_address_exact'
                        WHEN starts_with(t.address_norm, p.address_norm)
                          OR starts_with(p.address_norm, t.address_norm)
                        THEN 'name_address_prefix'
                        ELSE 'name_only'
                    END AS match_method,
                    CASE
                        WHEN t.address_norm = p.address_norm THEN 100
                        WHEN starts_with(t.address_norm, p.address_norm)
                          OR starts_with(p.address_norm, t.address_norm)
                        THEN 80
                        ELSE 20
                    END AS match_score
                FROM targets t
                JOIN public_index p ON t.name_norm = p.name_norm
            )
            SELECT
                *,
                count(*) OVER(PARTITION BY source_id, match_score) AS same_score_candidates,
                row_number() OVER(
                    PARTITION BY source_id
                    ORDER BY match_score DESC, corporate_number
                ) AS candidate_rank
            FROM scored
            """,
            [str(public_index)],
        )
        records = connection.execute(
            """
            SELECT
                t.source_id,
                t.company_name,
                t.address,
                c.corporate_number,
                c.public_company_name,
                c.post_code,
                c.prefecture_name,
                c.city_name,
                c.street_number,
                c.public_address,
                c.nta_update_date,
                c.match_method,
                c.match_score,
                c.same_score_candidates,
                CASE
                    WHEN c.match_method = 'name_address_exact'
                     AND c.same_score_candidates = 1 THEN 'accepted'
                    WHEN ?
                     AND c.match_method = 'name_address_prefix'
                     AND c.same_score_candidates = 1 THEN 'accepted_prefix'
                    WHEN c.corporate_number IS NULL THEN 'unmatched'
                    ELSE 'review'
                END AS status
            FROM targets t
            LEFT JOIN candidate_matches c
              ON c.source_id=t.source_id AND c.candidate_rank=1
            ORDER BY t.source_id
            """,
            [accept_prefix],
        ).fetchall()
        columns = [column[0] for column in connection.description]

        review_rows = connection.execute(
            """
            SELECT
                source_id,
                company_name,
                address,
                corporate_number,
                public_company_name,
                post_code,
                public_address,
                nta_update_date,
                match_method,
                match_score,
                same_score_candidates,
                candidate_rank
            FROM candidate_matches
            WHERE candidate_rank <= 10
              AND NOT (
                  match_method = 'name_address_exact'
                  AND same_score_candidates = 1
              )
            ORDER BY source_id, match_score DESC, corporate_number
            """
        ).fetchall()
        review_columns = [column[0] for column in connection.description]
    finally:
        connection.close()

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding=CSV_ENCODING, newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        writer.writerows(records)
    review_output.parent.mkdir(parents=True, exist_ok=True)
    with review_output.open("w", encoding=CSV_ENCODING, newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(review_columns)
        writer.writerows(review_rows)

    status_index = columns.index("status")
    counts: dict[str, int] = {}
    for record in records:
        status = clean(record[status_index])
        counts[status] = counts.get(status, 0) + 1
    result = {
        "targets": len(targets),
        "accepted": counts.get("accepted", 0),
        "accepted_prefix": counts.get("accepted_prefix", 0),
        "review": counts.get("review", 0),
        "unmatched": counts.get("unmatched", 0),
        "review_candidates": len(review_rows),
        "output": str(output),
        "review_output": str(review_output),
    }
    summary_output.parent.mkdir(parents=True, exist_ok=True)
    summary_output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Match companies against a public corporate-number index")
    parser.add_argument("--targets", type=Path, required=True)
    parser.add_argument("--public-index", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--review-output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--accept-prefix", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    match_index(
        targets_csv=args.targets,
        public_index=args.public_index,
        output=args.output,
        review_output=args.review_output,
        summary_output=args.summary,
        accept_prefix=args.accept_prefix,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
