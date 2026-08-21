#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Match a caller-local company CSV against a streamed public corporate index."""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import shutil
import subprocess
import unicodedata
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, TextIO

CSV_ENCODING = "utf-8-sig"
NAME_ALIASES = ["企業名", "会社名", "法人名", "商号又は名称", "company_name", "name"]
ADDRESS_ALIASES = ["所在地", "本店所在地", "住所", "full_address", "address"]
SOURCE_ID_ALIASES = ["SOURCE_ID", "LOCAL_SOURCE_ID", "source_id", "id"]
MAX_REVIEW_CANDIDATES = 10


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
    """Normalize notation while preserving the legal entity type."""
    text = unicodedata.normalize("NFKC", clean(value)).lower()
    replacements = {
        "(株)": "株式会社",
        "(有)": "有限会社",
        "(同)": "合同会社",
        "(資)": "合資会社",
        "(名)": "合名会社",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    return re.sub(
        r"[\s\u3000・･\.．,，'’\"“”\-‐–—_()（）\[\]【】/\\]+",
        "",
        text,
    )


def normalize_address(value: Any) -> str:
    text = unicodedata.normalize("NFKC", clean(value)).lower()
    text = re.sub(r"〒?\s*\d{3}[-‐‑‒–—―ー]?\d{4}", "", text)
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
    return re.sub(r"[-‐‑‒–—―ー]+", "", text)


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
        source_ids: set[str] = set()
        for index, row in enumerate(reader, start=1):
            source_id = clean(row.get(source_key, "")) if source_key else ""
            company_name = clean(row.get(name_key, ""))
            address = clean(row.get(address_key, ""))
            if not source_id:
                source_id = f"row-{index:08d}"
            if source_id in source_ids:
                raise ValueError(f"duplicate source ID: {source_id}")
            source_ids.add(source_id)
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


@contextmanager
def open_public_index(path: Path) -> Iterator[TextIO]:
    """Open plain TSV/CSV or Zstandard-compressed text without loading it into memory."""
    lower_name = path.name.lower()
    if not lower_name.endswith(".zst"):
        with path.open("r", encoding=CSV_ENCODING, newline="") as handle:
            yield handle
        return

    try:
        import zstandard  # type: ignore
    except ImportError:
        executable = shutil.which("zstd")
        if not executable:
            raise RuntimeError(
                "Zstandard index requires the Python zstandard package or a zstd executable"
            )
        process = subprocess.Popen(
            [executable, "-dc", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.stdout is None:
            process.kill()
            raise RuntimeError("failed to open zstd output stream")
        text = io.TextIOWrapper(process.stdout, encoding=CSV_ENCODING, newline="")
        try:
            yield text
        finally:
            text.close()
            stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
            return_code = process.wait()
            if return_code != 0:
                raise RuntimeError(f"zstd decompression failed: {stderr.strip()}")
        return

    with path.open("rb") as compressed:
        decompressor = zstandard.ZstdDecompressor()
        with decompressor.stream_reader(compressed) as stream:
            text = io.TextIOWrapper(stream, encoding=CSV_ENCODING, newline="")
            try:
                yield text
            finally:
                text.detach()


def public_delimiter(path: Path) -> str:
    name = path.name.lower()
    return "\t" if ".tsv" in name else ","


def _candidate_score(target_address: str, public_address: str) -> tuple[str, int]:
    if target_address and public_address and target_address == public_address:
        return "name_address_exact", 100
    if target_address and public_address and (
        target_address.startswith(public_address) or public_address.startswith(target_address)
    ):
        return "name_address_prefix", 80
    return "name_only", 20


def _candidate_sort_key(candidate: dict[str, Any]) -> tuple[int, str]:
    return (-int(candidate["match_score"]), clean(candidate["corporate_number"]))


def _public_candidate(row: dict[str, str], method: str, score: int) -> dict[str, Any]:
    return {
        "corporate_number": clean(row.get("corporate_number")),
        "public_company_name": clean(row.get("company_name")),
        "company_name_kana": clean(row.get("company_name_kana")),
        "post_code": clean(row.get("post_code")),
        "prefecture_name": clean(row.get("prefecture_name")),
        "city_name": clean(row.get("city_name")),
        "street_number": clean(row.get("street_number")),
        "public_address": clean(row.get("full_address")),
        "company_url": clean(row.get("company_url")),
        "representative_name": clean(row.get("representative_name")),
        "employee_number": clean(row.get("employee_number")),
        "capital_stock": clean(row.get("capital_stock")),
        "business_summary": clean(row.get("business_summary")),
        "jsic_middle_codes": clean(row.get("jsic_middle_codes")),
        "nta_update_date": clean(row.get("nta_update_date")),
        "match_method": method,
        "match_score": score,
    }


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
    targets_by_name: dict[str, list[dict[str, str]]] = defaultdict(list)
    for target in targets:
        targets_by_name[target["name_norm"]].append(target)

    candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    public_rows = 0
    matching_name_rows = 0
    with open_public_index(public_index) as handle:
        reader = csv.DictReader(handle, delimiter=public_delimiter(public_index))
        required = {"corporate_number", "company_name", "full_address"}
        missing = sorted(required - set(reader.fieldnames or []))
        if missing:
            raise ValueError("public index is missing columns: " + ", ".join(missing))
        for public_rows, row in enumerate(reader, start=1):
            name_norm = normalize_name(row.get("company_name"))
            matched_targets = targets_by_name.get(name_norm)
            if not matched_targets:
                continue
            matching_name_rows += 1
            public_address_norm = normalize_address(row.get("full_address"))
            for target in matched_targets:
                method, score = _candidate_score(target["address_norm"], public_address_norm)
                candidate = _public_candidate(row, method, score)
                bucket = candidates[target["source_id"]]
                bucket.append(candidate)
                bucket.sort(key=_candidate_sort_key)
                if len(bucket) > MAX_REVIEW_CANDIDATES:
                    del bucket[MAX_REVIEW_CANDIDATES:]
            if public_rows % 1_000_000 == 0:
                print(
                    json.dumps(
                        {
                            "public_rows_scanned": public_rows,
                            "public_rows_with_target_name": matching_name_rows,
                        },
                        ensure_ascii=False,
                    )
                )

    output_fields = [
        "source_id",
        "company_name",
        "address",
        "corporate_number",
        "public_company_name",
        "company_name_kana",
        "post_code",
        "prefecture_name",
        "city_name",
        "street_number",
        "public_address",
        "company_url",
        "representative_name",
        "employee_number",
        "capital_stock",
        "business_summary",
        "jsic_middle_codes",
        "nta_update_date",
        "match_method",
        "match_score",
        "same_score_candidates",
        "status",
    ]
    result_rows: list[dict[str, Any]] = []
    review_rows: list[dict[str, Any]] = []
    counts: dict[str, int] = defaultdict(int)

    for target in targets:
        bucket = sorted(candidates.get(target["source_id"], []), key=_candidate_sort_key)
        if not bucket:
            best: dict[str, Any] = {}
            same_score = 0
            status = "unmatched"
        else:
            best = bucket[0]
            best_score = int(best["match_score"])
            same_score = sum(int(candidate["match_score"]) == best_score for candidate in bucket)
            if best["match_method"] == "name_address_exact" and same_score == 1:
                status = "accepted"
            elif (
                accept_prefix
                and best["match_method"] == "name_address_prefix"
                and same_score == 1
            ):
                status = "accepted_prefix"
            else:
                status = "review"
        counts[status] += 1
        result_rows.append(
            {
                "source_id": target["source_id"],
                "company_name": target["company_name"],
                "address": target["address"],
                **best,
                "same_score_candidates": same_score,
                "status": status,
            }
        )
        if status == "review":
            for rank, candidate in enumerate(bucket, start=1):
                review_rows.append(
                    {
                        "source_id": target["source_id"],
                        "company_name": target["company_name"],
                        "address": target["address"],
                        **candidate,
                        "same_score_candidates": same_score,
                        "candidate_rank": rank,
                    }
                )

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding=CSV_ENCODING, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=output_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(result_rows)

    review_fields = output_fields[:-1] + ["candidate_rank"]
    review_output.parent.mkdir(parents=True, exist_ok=True)
    with review_output.open("w", encoding=CSV_ENCODING, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=review_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(review_rows)

    result = {
        "targets": len(targets),
        "public_rows_scanned": public_rows,
        "public_rows_with_target_name": matching_name_rows,
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
