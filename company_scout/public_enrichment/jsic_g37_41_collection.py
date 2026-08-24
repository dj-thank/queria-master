#!/usr/bin/env python3
"""Release-generation-bound G37-G41 adapter for the official-site phone pipeline.

The upstream collector is intentionally source-neutral and currently named
``jsic39_collection``.  This adapter feeds it the complete G37-G41 target
set, excludes FUMA-only rows from the corporate-number-required crawl, and
keeps the generated manifest tied to the G dataset.
"""

from __future__ import annotations

import argparse
import csv
import json
import tempfile
from pathlib import Path

from jsic39_collection import prepare_shard


REQUIRED_SCOPE_HEADERS = {
    "scope_label",
    "dataset_generation",
    "jsic_major_codes",
    "jsic_middle_codes",
}
ALLOWED_MIDDLE_CODES = {"", "37", "38", "39", "40", "41"}


def make_target_csv(source: Path) -> Path:
    with source.open("r", encoding="utf-8-sig", newline="") as source_handle:
        reader = csv.DictReader(source_handle)
        source_headers = set(reader.fieldnames or [])
        missing = sorted(REQUIRED_SCOPE_HEADERS.difference(source_headers))
        if missing:
            raise ValueError(f"G scope headers are missing: {missing}")
        rows = list(reader)

    generations: set[str] = set()
    for index, row in enumerate(rows, start=2):
        scope_label = (row.get("scope_label") or "").strip()
        generation = (row.get("dataset_generation") or "").strip()
        major = (row.get("jsic_major_codes") or "").strip()
        middle = (row.get("jsic_middle_codes") or "").strip()
        if scope_label != "G37-G41" or not generation or major != "G" or middle not in ALLOWED_MIDDLE_CODES:
            raise ValueError(f"G scope binding is invalid at row {index}")
        generations.add(generation)
    if len(generations) != 1:
        raise ValueError(f"G scope must use exactly one dataset generation: {sorted(generations)}")

    handle = tempfile.NamedTemporaryFile(prefix="g37_41_targets_", suffix=".csv", delete=False, mode="w", encoding="utf-8-sig", newline="")
    path = Path(handle.name)
    with handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "corporate_number", "company_name", "prefecture_name", "city_name",
            "scope_label", "dataset_generation", "jsic_major_codes", "jsic_middle_codes",
            "employee_number", "capital_stock", "company_url",
        ])
        writer.writeheader()
        for row in rows:
            corporate_number = (row.get("corporate_number") or "").strip()
            website = (row.get("website") or "").strip()
            state = (row.get("state") or "").strip()
            if (
                state == "pending_official_site"
                and len(corporate_number) == 13
                and corporate_number.isdigit()
                and website
            ):
                writer.writerow({
                    "corporate_number": corporate_number,
                    "company_name": row.get("company_name") or row.get("entity_key") or corporate_number,
                    "prefecture_name": row.get("prefecture_name") or "",
                    "city_name": row.get("city_name") or "",
                    "scope_label": row["scope_label"],
                    "dataset_generation": row["dataset_generation"],
                    "jsic_major_codes": row["jsic_major_codes"],
                    "jsic_middle_codes": row["jsic_middle_codes"],
                    "employee_number": row.get("employee_number") or "",
                    "capital_stock": row.get("capital_stock") or "",
                    "company_url": website,
                })
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare resumable G37-G41 official-site phone shards")
    parser.add_argument("--targets", type=Path, default=Path("data/phone_targets_g37_41.csv"))
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args()
    target_csv = make_target_csv(args.targets)
    try:
        result = prepare_shard(companies_csv=target_csv, database=args.database, manifest=args.manifest, offset=args.offset, limit=args.limit, summary=args.summary)
        result["scope"] = "G37-G41"
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        target_csv.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
