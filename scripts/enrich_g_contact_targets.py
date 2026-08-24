#!/usr/bin/env python3
"""Join the public G phone-target ledger with its release runtime metadata."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import duckdb


REQUIRED_TARGET_HEADERS = {
    "entity_key",
    "corporate_number",
    "website",
    "state",
    "last_completed_at",
    "last_error",
}
ENRICHED_HEADERS = [
    "company_name",
    "prefecture_name",
    "city_name",
    "employee_number",
    "capital_stock",
]


def enrich_targets(source: Path, database: Path, output: Path) -> dict[str, object]:
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        source_headers = list(reader.fieldnames or [])
        missing_headers = sorted(REQUIRED_TARGET_HEADERS.difference(source_headers))
        if missing_headers:
            raise ValueError(f"Target ledger lacks required headers: {missing_headers}")
        rows = list(reader)

    con = duckdb.connect(str(database), read_only=True)
    try:
        metadata_rows = con.execute(
            """
            SELECT entity_key, name, prefecture, city, employees, capital
            FROM core.g_companies
            """
        ).fetchall()
    finally:
        con.close()
    metadata = {
        str(entity_key): {
            "company_name": name,
            "prefecture_name": prefecture,
            "city_name": city,
            "employee_number": employees,
            "capital_stock": capital,
        }
        for entity_key, name, prefecture, city, employees, capital in metadata_rows
    }

    matched = 0
    for row in rows:
        values = metadata.get(str(row["entity_key"]))
        if values is None:
            for header in ENRICHED_HEADERS:
                row.setdefault(header, "")
            continue
        matched += 1
        row.update({header: "" if values[header] is None else values[header] for header in ENRICHED_HEADERS})

    fieldnames = [*source_headers, *[header for header in ENRICHED_HEADERS if header not in source_headers]]
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return {"rows": len(rows), "matched": matched, "missing": len(rows) - matched, "output": str(output)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--targets", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args()
    result = enrich_targets(args.targets, args.database, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.require_complete and result["missing"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
