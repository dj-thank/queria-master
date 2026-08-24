#!/usr/bin/env python3
"""Create deterministic IT-subsidiary/SES priority targets and JSON exports."""
from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlsplit, urlunsplit

from business_profile import (
    SCORE_FORMULA_VERSION,
    bound_profile_facts,
    empty_business_profile,
    official_site_binding,
    score_business_profile,
    target_binding_sha256,
    validate_profile_evidence_host,
)


CSV_ENCODING = "utf-8-sig"
SCHEMA_VERSION = "1.0"
SEED_FORMULA_VERSION = "it-subsidiary-ses-seed-v1"
PHONE_TYPE_MAP = {
    "代表電話": "representative",
    "本社電話": "head_office",
    "問い合わせ電話": "inquiry",
    "採用窓口": "recruiting",
    "サポート窓口": "support",
    "広報・IR窓口": "ir",
    "個人情報・相談窓口": "privacy",
    "支店・事業所": "branch",
    "FAX": "fax",
    "未分類": "unclassified",
}
BUSINESS_SIGNAL_STRENGTH = {
    "it_subsidiary": 100,
    "user_system_it": 100,
    "parent_group": 90,
    "ses": 100,
    "onsite_development": 100,
    "si": 80,
    "contract_development": 80,
    "it_operations": 80,
    "recruitment": 40,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in {"none", "null", "nan"} else text


def as_int(value: Any) -> int | None:
    digits = re.sub(r"[^0-9-]", "", clean(value))
    if not digits:
        return None
    try:
        return max(0, int(digits))
    except ValueError:
        return None


def normalize_url(value: Any) -> str:
    url = clean(value)
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url.lstrip("/")
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return ""
    try:
        host = parsed.hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return ""
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    path = quote(parsed.path or "/", safe="/%:@-._~!$&'()*+,;=")
    query = quote(parsed.query, safe="=&?/%:+,;@-._~!$'()*")
    fragment = quote(parsed.fragment, safe="-._~!$&'()*+,;=:@/?")
    return urlunsplit((parsed.scheme.lower(), netloc, path, query, fragment))


def normalize_phone(value: Any) -> str:
    text = clean(value).replace("+81", "0")
    digits = re.sub(r"\D", "", text)
    return digits if 10 <= len(digits) <= 11 and digits.startswith("0") else ""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding=CSV_ENCODING, newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def write_csv(path: Path, fieldnames: list[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding=CSV_ENCODING, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def seed_priority(row: dict[str, str]) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    middle = clean(row.get("jsic_middle_codes"))
    if middle == "39":
        score += 20
        reasons.append("jsic39:+20")
    elif middle in {"37", "38", "40", "41"}:
        score += 5
        reasons.append("g37_41:+5")
    name = clean(row.get("company_name"))
    if re.search(r"(?:システム|ソリューション|テクノロジ|デジタル|情報|IT|ＩＴ)", name, re.IGNORECASE):
        score += 15
        reasons.append("company_name_weak_it_signal:+15")
    employees = as_int(row.get("employee_number")) or 0
    if 50 <= employees <= 999:
        score += 10
        reasons.append("sales_size_50_999:+10")
    elif 1 <= employees < 50:
        score += 4
        reasons.append("sales_size_1_49:+4")
    elif 1000 <= employees <= 4999:
        score += 5
        reasons.append("sales_size_1000_4999:+5")
    if clean(row.get("state")) == "pending_official_site":
        score += 5
        reasons.append("phone_pending_official_site:+5")
    return score, reasons


def prioritize_targets(source: Path, output: Path, summary: Path) -> dict[str, Any]:
    fieldnames, rows = read_csv(source)
    required = {"entity_key", "corporate_number", "company_name", "state", "dataset_generation"}
    missing = sorted(required.difference(fieldnames))
    if missing:
        raise ValueError(f"Priority input lacks required headers: {missing}")
    scored: list[tuple[int, int, list[str], dict[str, str]]] = []
    for index, row in enumerate(rows, start=1):
        score, reasons = seed_priority(row)
        scored.append((score, index, reasons, row))
    scored.sort(key=lambda item: (-item[0], item[1]))
    output_rows: list[dict[str, Any]] = []
    reason_counts: Counter[str] = Counter()
    for rank, (score, original, reasons, row) in enumerate(scored, start=1):
        reason_counts.update(reasons)
        output_rows.append({
            **row,
            "ses_priority_seed_rank": rank,
            "ses_priority_seed_score": score,
            "ses_priority_seed_reasons": "|".join(reasons),
            "ses_priority_original_rank": original,
            "ses_priority_seed_formula": SEED_FORMULA_VERSION,
        })
    extra = [
        "ses_priority_seed_rank",
        "ses_priority_seed_score",
        "ses_priority_seed_reasons",
        "ses_priority_original_rank",
        "ses_priority_seed_formula",
    ]
    write_csv(output, [*fieldnames, *extra], output_rows)
    result = {
        "schema_version": 1,
        "formula_version": SEED_FORMULA_VERSION,
        "rows": len(rows),
        "input_sha256": file_sha256(source),
        "output_sha256": file_sha256(output),
        "reason_counts": dict(sorted(reason_counts.items())),
        "promotion_authorized": False,
        "output": str(output),
    }
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def _expand(patterns: Iterable[str]) -> list[Path]:
    paths: dict[Path, Path] = {}
    for pattern in patterns:
        matches = [Path(item) for item in glob.glob(pattern, recursive=True)]
        if not matches and Path(pattern).is_file():
            matches = [Path(pattern)]
        for path in matches:
            paths[path.resolve()] = path
    return [paths[key] for key in sorted(paths, key=str)]


def load_progress(patterns: Iterable[str]) -> tuple[dict[str, dict[str, Any]], list[Path]]:
    by_company: dict[str, dict[str, Any]] = {}
    owner: dict[str, Path] = {}
    paths = _expand(patterns)
    for path in paths:
        latest_in_file: dict[str, dict[str, Any]] = {}
        with path.open("r", encoding="utf-8") as handle:
            for line_number, raw in enumerate(handle, start=1):
                if not raw.strip():
                    continue
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Invalid progress JSONL: {path}:{line_number}") from exc
                corporate_number = clean(record.get("corporate_number"))
                if not re.fullmatch(r"[0-9]{13}", corporate_number):
                    raise ValueError(f"Invalid progress corporate number: {path}:{line_number}")
                latest_in_file[corporate_number] = record
        for corporate_number, record in latest_in_file.items():
            if corporate_number in by_company:
                raise ValueError(
                    f"Duplicate company across progress shards: {corporate_number} "
                    f"({owner[corporate_number]} and {path})"
                )
            by_company[corporate_number] = record
            owner[corporate_number] = path
    return by_company, paths


def load_manifests(patterns: Iterable[str]) -> tuple[dict[str, dict[str, str]], list[Path]]:
    by_company: dict[str, dict[str, str]] = {}
    paths = _expand(patterns)
    if not paths:
        raise FileNotFoundError("No manifest files were found")
    for path in paths:
        _fields, rows = read_csv(path)
        for row in rows:
            corporate_number = clean(row.get("法人番号"))
            if not re.fullmatch(r"[0-9]{13}", corporate_number):
                raise ValueError(f"Invalid manifest corporate number: {path}")
            if corporate_number in by_company:
                raise ValueError(f"Duplicate company across manifests: {corporate_number}")
            by_company[corporate_number] = row
    return by_company, paths


def validate_progress_export_binding(
    target: dict[str, str],
    manifest: dict[str, str],
    progress: dict[str, Any],
) -> None:
    corporate_number = clean(target.get("corporate_number"))
    target_url = clean(target.get("website") or target.get("company_url"))
    bindings = {
        "corporate_number": (
            corporate_number,
            clean(manifest.get("法人番号")),
            clean(progress.get("corporate_number")),
        ),
        "official_site": (
            official_site_binding(target_url),
            official_site_binding(manifest.get("公式サイトURL")),
            official_site_binding(progress.get("official_site_url")),
        ),
        "scope_label": (
            clean(target.get("scope_label")),
            clean(manifest.get("スコープ")),
            clean(progress.get("scope_label")),
        ),
        "dataset_generation": (
            clean(target.get("dataset_generation")),
            clean(manifest.get("データ世代")),
            clean(progress.get("dataset_generation")),
        ),
        "runtime_binding_status": (
            clean(target.get("runtime_binding_status")),
            clean(manifest.get("正本照合")),
            clean(progress.get("runtime_binding_status")),
        ),
    }
    for field, values in bindings.items():
        if not values[0] or values[0] != values[1] or values[0] != values[2]:
            raise ValueError(f"Progress export {field} mismatch: {corporate_number}")
    if int(progress.get("schema_version") or 1) < 2:
        raise ValueError(f"Progress schema must be upgraded before profile export: {corporate_number}")
    expected_hash = target_binding_sha256(
        corporate_number=corporate_number,
        official_site_url=target_url,
        scope_label=target.get("scope_label"),
        dataset_generation=target.get("dataset_generation"),
        runtime_binding_status=target.get("runtime_binding_status"),
    )
    if clean(progress.get("target_binding_sha256")) != expected_hash:
        raise ValueError(f"Progress export target binding hash mismatch: {corporate_number}")
    target_host = official_site_binding(target_url)[0]
    for candidate in progress.get("candidates") or []:
        if not normalize_phone(candidate.get("phone")):
            raise ValueError(f"Invalid progress phone during export: {corporate_number}")
        evidence_url = normalize_url(candidate.get("url"))
        if not evidence_url or official_site_binding(evidence_url)[0] != target_host:
            raise ValueError(f"Progress candidate evidence host mismatch: {corporate_number}")
    profile = progress.get("business_profile")
    if not isinstance(profile, dict) or not validate_profile_evidence_host(profile, target_url):
        raise ValueError(f"Progress business profile binding mismatch: {corporate_number}")


def _evidence_copy(fact: dict[str, Any]) -> dict[str, Any]:
    excerpt = clean(fact.get("excerpt"))[:240]
    return {
        "signal": clean(fact.get("signal")),
        "status": "observed_text",
        "evidence_url": normalize_url(fact.get("evidence_url")),
        "excerpt": excerpt,
        "excerpt_sha256": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
        "observed_at": clean(fact.get("observed_at")) or now_iso(),
    }


def _record_from_target(row: dict[str, str], progress: dict[str, Any] | None) -> dict[str, Any]:
    progress = progress or {}
    website = normalize_url(row.get("website") or row.get("company_url"))
    profile = progress.get("business_profile") if isinstance(progress.get("business_profile"), dict) else empty_business_profile()
    facts = [
        copied
        for item in bound_profile_facts(profile.get("facts") or [])
        if (copied := _evidence_copy(item))["evidence_url"]
    ]
    target_host = official_site_binding(website)[0]
    if website and any(
        official_site_binding(fact.get("evidence_url"))[0] != target_host
        for fact in facts
    ):
        raise ValueError(f"Profile evidence host mismatch during export: {clean(row.get('corporate_number'))}")
    candidates = [dict(item) for item in progress.get("candidates") or []]
    state = clean(progress.get("state")) or clean(row.get("state")) or "not_processed"
    scores = score_business_profile(
        profile,
        candidates,
        industry_code=clean(row.get("jsic_middle_codes")),
        company_name=clean(row.get("company_name") or row.get("entity_key")),
        collection_state=state,
        has_official_site=bool(website),
    )
    contacts: list[dict[str, Any]] = []
    for candidate in candidates:
        phone_digits = normalize_phone(candidate.get("phone"))
        candidate_type = clean(candidate.get("candidate_type")) or "未分類"
        evidence_url = normalize_url(candidate.get("url"))
        if not phone_digits or not evidence_url:
            continue
        excluded = candidate_type == "FAX"
        contacts.append({
            "channel": "phone",
            "value": phone_digits,
            "type": PHONE_TYPE_MAP.get(candidate_type, "unclassified"),
            "evidence_url": evidence_url,
            "review_status": "excluded" if excluded else "candidate_needs_review",
            "source_kind": "official_site",
            "binding_status": "same_host",
        })
    for fact in facts:
        if fact["signal"] == "contact_form":
            contacts.append({
                "channel": "form",
                "value": None,
                "type": "form",
                "evidence_url": fact["evidence_url"],
                "review_status": "candidate_needs_review",
                "source_kind": "official_site",
                "binding_status": "same_host",
            })
    contacts.sort(key=lambda item: (item["channel"], item["type"], item["value"] or "", item["evidence_url"]))

    parent_facts = [item for item in facts if item["signal"] == "parent_group"]
    parent_candidates = [dict(item) for item in profile.get("parent_company_candidates") or []]
    unique_parent_names = sorted({clean(item.get("name")) for item in parent_candidates if clean(item.get("name"))})
    business = [
        {
            "signal": fact["signal"] if fact["signal"] in BUSINESS_SIGNAL_STRENGTH else "other",
            "fact_or_inference": "observed",
            "strength": BUSINESS_SIGNAL_STRENGTH.get(fact["signal"], 20),
            "evidence_url": fact["evidence_url"],
            "excerpt": fact["excerpt"],
        }
        for fact in facts
        if fact["signal"] != "contact_form"
    ]
    business.sort(key=lambda item: (item["signal"], item["evidence_url"], item["excerpt"]))
    unknowns = set(str(item) for item in profile.get("unknowns") or [])
    if parent_facts:
        if len(unique_parent_names) != 1:
            unknowns.add("parent_company_name_requires_review")
    else:
        unknowns.add("parent_company_relationship")
    if not contacts:
        unknowns.add("sales_contact")
    generated_at = clean(progress.get("completed_at")) or now_iso()
    entity_key = clean(row.get("entity_key")) or clean(row.get("corporate_number"))
    corporate = clean(row.get("corporate_number")) or None
    record = {
        "schema_version": SCHEMA_VERSION,
        "entity": {
            "entity_key": entity_key,
            "corporate_number": corporate,
            "company_name": clean(row.get("company_name")) or entity_key,
            "prefecture": clean(row.get("prefecture_name")) or None,
            "city": clean(row.get("city_name")) or None,
            "employees": as_int(row.get("employee_number")),
            "capital": as_int(row.get("capital_stock")),
            "industry_code": clean(row.get("jsic_middle_codes")) or None,
        },
        "official_site": {
            "url": website or None,
            "canonicality": (
                "observed" if facts or int(progress.get("pages_fetched") or 0) > 0
                else "blocked" if state == "blocked_by_policy"
                else "candidate" if website
                else "missing"
            ),
            "same_host_verified": bool(website and (not facts or validate_profile_evidence_host({"facts": facts}, website))),
            "evidence": facts,
        },
        "parent_company": {
            "name": unique_parent_names[0] if len(unique_parent_names) == 1 else None,
            "status": "candidate" if parent_facts else "unknown",
            "evidence": parent_facts,
        },
        "business_signals": business,
        "contact_evidence": contacts,
        "collection_state": state,
        "fit_scores": {key: value for key, value in scores.items() if key != "negative_controls"},
        "priority_tier": scores["tier"],
        "negative_controls": scores["negative_controls"],
        "unknowns": sorted(unknowns),
        "provenance": {
            "dataset_generation": clean(row.get("dataset_generation")) or None,
            "generated_at": generated_at,
            "profile_schema_version": int(profile.get("schema_version") or 1),
            "progress_schema_version": int(progress.get("schema_version") or 1),
            "algorithm_version": SCORE_FORMULA_VERSION,
            "promotion_authorized": False,
        },
    }
    return record


def _csv_row(rank: int, record: dict[str, Any]) -> dict[str, Any]:
    phones = [item for item in record["contact_evidence"] if item["channel"] == "phone" and item["review_status"] != "excluded"]
    forms = [item for item in record["contact_evidence"] if item["channel"] == "form"]
    business = record["business_signals"]
    evidence_urls = sorted({item["evidence_url"] for item in business})
    return {
        "priority_rank": rank,
        "priority_tier": record["priority_tier"],
        "priority_score": record["fit_scores"]["priority_score"],
        "it_subsidiary_score": record["fit_scores"]["it_subsidiary_score"],
        "ses_sales_score": record["fit_scores"]["ses_sales_score"],
        "contactability_score": record["fit_scores"]["contactability_score"],
        "corporate_number": record["entity"]["corporate_number"],
        "entity_key": record["entity"]["entity_key"],
        "company_name": record["entity"]["company_name"],
        "prefecture": record["entity"]["prefecture"],
        "city": record["entity"]["city"],
        "employees": record["entity"]["employees"],
        "official_website": record["official_site"]["url"],
        "primary_phone_candidate": phones[0]["value"] if phones else "",
        "primary_phone_type": phones[0]["type"] if phones else "",
        "phone_evidence_url": phones[0]["evidence_url"] if phones else "",
        "contact_form_url": forms[0]["evidence_url"] if forms else "",
        "parent_company_status": record["parent_company"]["status"],
        "business_signals": "|".join(sorted({item["signal"] for item in business})),
        "business_evidence_summary": " / ".join(item["excerpt"] for item in business[:3])[:720],
        "business_evidence_urls": "|".join(evidence_urls),
        "contact_state": record["collection_state"],
        "review_status": "needs_review" if phones or business or forms else record["collection_state"],
        "negative_controls": "|".join(record["negative_controls"]),
        "unknowns": "|".join(record["unknowns"]),
        "dataset_generation": record["provenance"]["dataset_generation"],
        "promotion_authorized": "false",
    }


CSV_FIELDS = [
    "priority_rank", "priority_tier", "priority_score", "it_subsidiary_score", "ses_sales_score",
    "contactability_score", "corporate_number", "entity_key", "company_name", "prefecture", "city",
    "employees", "official_website", "primary_phone_candidate", "primary_phone_type", "phone_evidence_url",
    "contact_form_url", "parent_company_status", "business_signals", "business_evidence_summary",
    "business_evidence_urls", "contact_state", "review_status", "negative_controls", "unknowns",
    "dataset_generation", "promotion_authorized",
]


def export_priority(
    *,
    targets: Path,
    manifest_patterns: list[str],
    progress_patterns: list[str],
    schema: Path,
    jsonl_output: Path,
    csv_output: Path,
    summary_output: Path,
) -> dict[str, Any]:
    _fields, rows = read_csv(targets)
    progress, progress_files = load_progress(progress_patterns)
    manifests, manifest_files = load_manifests(manifest_patterns)
    if set(progress) != set(manifests):
        missing_progress = sorted(set(manifests).difference(progress))
        outside_manifest = sorted(set(progress).difference(manifests))
        raise ValueError(
            f"Progress/manifest company mismatch: missing_progress={missing_progress[:5]}, "
            f"outside_manifest={outside_manifest[:5]}"
        )
    target_by_corporate: dict[str, dict[str, str]] = {}
    for row in rows:
        corporate_number = clean(row.get("corporate_number"))
        if not corporate_number:
            continue
        if corporate_number in target_by_corporate:
            raise ValueError(f"Duplicate corporate number in export targets: {corporate_number}")
        target_by_corporate[corporate_number] = row
    for corporate_number, progress_record in progress.items():
        target = target_by_corporate.get(corporate_number)
        if target is None:
            raise ValueError(f"Progress company is outside export targets: {corporate_number}")
        validate_progress_export_binding(target, manifests[corporate_number], progress_record)
    schema_object = json.loads(schema.read_text(encoding="utf-8"))
    from jsonschema import Draft202012Validator, FormatChecker

    validator = Draft202012Validator(schema_object, format_checker=FormatChecker())
    records: list[dict[str, Any]] = []
    seen_entities: set[str] = set()
    for row in rows:
        entity_key = clean(row.get("entity_key")) or clean(row.get("corporate_number"))
        if not entity_key or entity_key in seen_entities:
            raise ValueError(f"Duplicate or missing entity_key in targets: {entity_key!r}")
        seen_entities.add(entity_key)
        corporate = clean(row.get("corporate_number"))
        record = _record_from_target(row, progress.get(corporate))
        errors = sorted(validator.iter_errors(record), key=lambda item: list(item.path))
        if errors:
            raise ValueError(f"Schema validation failed for {entity_key}: {errors[0].message}")
        records.append(record)
    tier_order = {"A": 0, "B": 1, "C": 2, "blocked": 3, "unknown": 4}
    records.sort(key=lambda item: (
        tier_order[item["priority_tier"]],
        -int(item["fit_scores"]["priority_score"]),
        -int(item["fit_scores"]["ses_sales_score"]),
        -int(item["fit_scores"]["it_subsidiary_score"]),
        -(item["entity"]["employees"] or 0),
        item["entity"]["company_name"],
        item["entity"]["entity_key"],
    ))
    jsonl_output.parent.mkdir(parents=True, exist_ok=True)
    with jsonl_output.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    write_csv(csv_output, CSV_FIELDS, [_csv_row(index, record) for index, record in enumerate(records, start=1)])

    tiers = Counter(record["priority_tier"] for record in records)
    states = Counter(record["collection_state"] for record in records)
    signals = Counter(signal["signal"] for record in records for signal in record["business_signals"])
    result = {
        "schema_version": 1,
        "record_schema": SCHEMA_VERSION,
        "algorithm_version": SCORE_FORMULA_VERSION,
        "rows": len(records),
        "unique_entity_keys": len(seen_entities),
        "progress_records": len(progress),
        "profiles_with_evidence": sum(bool(record["business_signals"]) for record in records),
        "companies_with_voice_phone_candidates": sum(
            any(item["channel"] == "phone" and item["review_status"] != "excluded" for item in record["contact_evidence"])
            for record in records
        ),
        "priority_tiers": dict(sorted(tiers.items())),
        "collection_states": dict(sorted(states.items())),
        "business_signals": dict(sorted(signals.items())),
        "promotion_authorized": False,
        "input_target_sha256": file_sha256(targets),
        "jsonl_sha256": file_sha256(jsonl_output),
        "csv_sha256": file_sha256(csv_output),
        "progress_files": [str(path) for path in progress_files],
        "manifest_files": [str(path) for path in manifest_files],
        "outputs": {"jsonl": str(jsonl_output), "csv": str(csv_output)},
    }
    summary_output.parent.mkdir(parents=True, exist_ok=True)
    summary_output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="情シス子会社・SES営業優先JSONワークフロー")
    commands = parser.add_subparsers(dest="command", required=True)
    prioritize = commands.add_parser("prioritize-targets")
    prioritize.add_argument("--input", type=Path, required=True)
    prioritize.add_argument("--output", type=Path, required=True)
    prioritize.add_argument("--summary", type=Path, required=True)
    export = commands.add_parser("export")
    export.add_argument("--targets", type=Path, required=True)
    export.add_argument("--manifest", action="append", required=True)
    export.add_argument("--progress", action="append", required=True)
    export.add_argument("--schema", type=Path, default=Path("schemas/it-subsidiary-ses-priority-v1.schema.json"))
    export.add_argument("--jsonl", type=Path, required=True)
    export.add_argument("--csv", type=Path, required=True)
    export.add_argument("--summary", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "prioritize-targets":
        result = prioritize_targets(args.input, args.output, args.summary)
    else:
        result = export_priority(
            targets=args.targets,
            manifest_patterns=args.manifest,
            progress_patterns=args.progress,
            schema=args.schema,
            jsonl_output=args.jsonl,
            csv_output=args.csv,
            summary_output=args.summary,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
