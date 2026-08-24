#!/usr/bin/env python3
"""Join the public G phone-target ledger with its release runtime metadata."""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from urllib.parse import urlparse

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
    "scope_label",
    "dataset_generation",
    "jsic_major_codes",
    "jsic_middle_codes",
    "runtime_binding_status",
]


def middle_code(industry_code: object) -> str:
    for token in str(industry_code or "").split("|"):
        match = re.fullmatch(r"G?(37|38|39|40|41)", token.strip())
        if match:
            return match.group(1)
    return ""


def is_g_industry(industry_code: object) -> bool:
    return any(token.strip() == "G" for token in str(industry_code or "").split("|"))


def is_g37_41_scope(scope: object) -> bool:
    text = str(scope or "")
    has_g = re.search(r"(?<![A-Za-z0-9])G(?![A-Za-z0-9])", text) is not None
    divisions = set(re.findall(r"(?<!\d)(37|38|39|40|41)(?!\d)", text))
    return has_g and divisions == {"37", "38", "39", "40", "41"}


def normalized_url(value: object) -> tuple[str, str, str]:
    url = str(value or "").strip()
    if not url:
        return "", "", ""
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url.lstrip("/")
    parsed = urlparse(url)
    return (
        (parsed.hostname or "").lower().removeprefix("www."),
        parsed.path.rstrip("/") or "/",
        parsed.query,
    )


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
        manifest = dict(con.execute("SELECT dataset_key, value FROM meta.dataset_manifest").fetchall())
        if manifest.get("dataset_key") != "G37_41_FUMA":
            raise ValueError("Release runtime is not the G37_41_FUMA dataset")
        generation = str(manifest.get("generation") or "").strip()
        scope = str(manifest.get("scope") or "")
        if not generation or not is_g37_41_scope(scope):
            raise ValueError("Release runtime lacks the required G37-G41 scope manifest")
        metadata_rows = con.execute(
            """
            SELECT entity_key, corporate_number, name, prefecture, city, employees, capital,
                   industry_code, website, phone, phone_status
            FROM core.g_companies
            """
        ).fetchall()
    finally:
        con.close()
    metadata: dict[str, dict[str, object]] = {}
    for entity_key, corporate_number, name, prefecture, city, employees, capital, industry_code, website, phone, phone_status in metadata_rows:
        key = str(entity_key)
        if key in metadata:
            raise ValueError(f"Duplicate entity key in release runtime: {key}")
        if not is_g_industry(industry_code) or middle_code(industry_code) not in {"", "37", "38", "39", "40", "41"}:
            raise ValueError(f"Release runtime contains an entity outside G37-G41: {key}")
        metadata[key] = {
            "company_name": name,
            "prefecture_name": prefecture,
            "city_name": city,
            "employee_number": employees,
            "capital_stock": capital,
            "scope_label": "G37-G41",
            "dataset_generation": generation,
            "jsic_major_codes": "G",
            "jsic_middle_codes": middle_code(industry_code),
            "runtime_binding_status": "matched",
            "_corporate_number": corporate_number,
            "_website": website,
            "_phone": phone,
            "_phone_status": phone_status,
        }

    matched = 0
    seen_source_keys: set[str] = set()
    for row in rows:
        source_key = str(row["entity_key"])
        if source_key in seen_source_keys:
            raise ValueError(f"Duplicate entity key in target ledger: {source_key}")
        seen_source_keys.add(source_key)
        values = metadata.get(source_key)
        if values is None:
            for header in ENRICHED_HEADERS:
                row.setdefault(header, "")
            continue
        source_generation = str(row.get("dataset_generation") or "").strip()
        if source_generation and source_generation != generation:
            raise ValueError(f"Target generation differs from runtime: {row['entity_key']}")
        source_corporate_number = str(row.get("corporate_number") or "").strip()
        runtime_corporate_number = str(values["_corporate_number"] or "").strip()
        if source_corporate_number != runtime_corporate_number:
            raise ValueError(f"Target corporate number differs from runtime: {row['entity_key']}")
        if normalized_url(row.get("website")) != normalized_url(values["_website"]):
            raise ValueError(f"Target website differs from runtime: {row['entity_key']}")
        expected_state = (
            str(values["_phone_status"] or "")
            if values["_phone"]
            else "pending_official_site"
            if values["_website"] and values["_corporate_number"]
            else "fuma_only_blocked"
            if values["_website"]
            else "website_missing"
        )
        if str(row.get("state") or "").strip() != expected_state:
            raise ValueError(f"Target state differs from runtime: {row['entity_key']}")
        matched += 1
        row.update({header: "" if values[header] is None else values[header] for header in ENRICHED_HEADERS})

    fieldnames = [*source_headers, *[header for header in ENRICHED_HEADERS if header not in source_headers]]
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return {
        "rows": len(rows),
        "matched": matched,
        "missing": len(rows) - matched,
        "generation": generation,
        "scope": "G37-G41",
        "output": str(output),
    }


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
