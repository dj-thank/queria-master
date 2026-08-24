from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import business_profile
import ses_priority_json as priority


SCHEMA = ROOT / "schemas" / "it-subsidiary-ses-priority-v1.schema.json"


def write_targets(path: Path) -> None:
    fields = [
        "entity_key", "corporate_number", "website", "state", "company_name",
        "prefecture_name", "city_name", "employee_number", "capital_stock",
        "scope_label", "dataset_generation", "jsic_major_codes", "jsic_middle_codes",
        "runtime_binding_status",
    ]
    rows = [
        {
            "entity_key": "1000000000001", "corporate_number": "1000000000001",
            "website": "https://alpha.example/", "state": "pending_official_site",
            "company_name": "Alpha株式会社", "prefecture_name": "東京都", "city_name": "千代田区",
            "employee_number": "8000", "capital_stock": "100000000", "scope_label": "G37-G41",
            "dataset_generation": "g-test", "jsic_major_codes": "G", "jsic_middle_codes": "37",
            "runtime_binding_status": "matched",
        },
        {
            "entity_key": "1000000000002", "corporate_number": "1000000000002",
            "website": "https://beta.example/", "state": "pending_official_site",
            "company_name": "Betaシステム株式会社", "prefecture_name": "大阪府", "city_name": "大阪市",
            "employee_number": "120", "capital_stock": "50000000", "scope_label": "G37-G41",
            "dataset_generation": "g-test", "jsic_major_codes": "G", "jsic_middle_codes": "39",
            "runtime_binding_status": "matched",
        },
        {
            "entity_key": "1000000000003", "corporate_number": "1000000000003",
            "website": "", "state": "website_missing", "company_name": "Gamma株式会社",
            "prefecture_name": "福岡県", "city_name": "福岡市", "employee_number": "20",
            "capital_stock": "10000000", "scope_label": "G37-G41", "dataset_generation": "g-test",
            "jsic_major_codes": "G", "jsic_middle_codes": "", "runtime_binding_status": "matched",
        },
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_prioritize_targets_prefers_mid_size_jsic39_it_signal(tmp_path: Path) -> None:
    source = tmp_path / "targets.csv"
    output = tmp_path / "prioritized.csv"
    summary = tmp_path / "summary.json"
    write_targets(source)

    result = priority.prioritize_targets(source, output, summary)

    _fields, rows = priority.read_csv(output)
    assert result["rows"] == 3
    assert rows[0]["corporate_number"] == "1000000000002"
    assert rows[0]["ses_priority_seed_formula"] == priority.SEED_FORMULA_VERSION
    assert "company_name_weak_it_signal" in rows[0]["ses_priority_seed_reasons"]
    assert result["input_sha256"] != result["output_sha256"]
    assert json.loads(summary.read_text(encoding="utf-8"))["promotion_authorized"] is False


def test_export_builds_schema_valid_candidate_only_priority_records(tmp_path: Path) -> None:
    targets = tmp_path / "targets.csv"
    write_targets(targets)
    page_profile = business_profile.extract_business_profile_evidence(
        "https://beta.example/company",
        "完全子会社の情報システム子会社としてSES、客先常駐、受託開発、運用保守を提供",
        [("/contact", "お問い合わせ")],
        observed_at="2026-08-25T00:00:00+00:00",
    )
    progress = tmp_path / "progress.jsonl"
    progress.write_text(
        json.dumps({
            "schema_version": 2,
            "corporate_number": "1000000000002",
            "official_site_url": "https://beta.example/",
            "dataset_generation": "g-test",
            "state": "phone_candidate_found",
            "pages_fetched": 2,
            "candidates": [{
                "phone": "0612345678", "candidate_type": "代表電話",
                "url": "https://beta.example/company", "context": "代表電話",
                "source": "text", "score": 0.78,
            }],
            "business_profile": page_profile,
            "completed_at": "2026-08-25T00:00:00+00:00",
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    jsonl = tmp_path / "priority.jsonl"
    csv_output = tmp_path / "priority.csv"
    summary = tmp_path / "priority-summary.json"

    result = priority.export_priority(
        targets=targets,
        progress_patterns=[str(progress)],
        schema=SCHEMA,
        jsonl_output=jsonl,
        csv_output=csv_output,
        summary_output=summary,
    )

    records = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
    assert result["rows"] == 3
    assert result["profiles_with_evidence"] == 1
    assert records[0]["entity"]["corporate_number"] == "1000000000002"
    assert records[0]["priority_tier"] == "A"
    assert records[0]["provenance"]["promotion_authorized"] is False
    phone_item = next(item for item in records[0]["contact_evidence"] if item["channel"] == "phone")
    assert phone_item["review_status"] == "candidate_needs_review"
    assert records[0]["parent_company"]["status"] == "candidate"
    assert records[-1]["official_site"]["canonicality"] == "missing"
    _fields, csv_rows = priority.read_csv(csv_output)
    assert csv_rows[0]["priority_tier"] == "A"
    assert csv_rows[0]["promotion_authorized"] == "false"


def test_export_rejects_duplicate_company_across_progress_shards(tmp_path: Path) -> None:
    targets = tmp_path / "targets.csv"
    write_targets(targets)
    record = {
        "schema_version": 2,
        "corporate_number": "1000000000001",
        "official_site_url": "https://alpha.example/",
        "state": "processed_no_phone",
        "candidates": [],
        "business_profile": business_profile.empty_business_profile(),
    }
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    payload = json.dumps(record) + "\n"
    first.write_text(payload, encoding="utf-8")
    second.write_text(payload, encoding="utf-8")

    with pytest.raises(ValueError, match="Duplicate company across progress shards"):
        priority.export_priority(
            targets=targets,
            progress_patterns=[str(first), str(second)],
            schema=SCHEMA,
            jsonl_output=tmp_path / "out.jsonl",
            csv_output=tmp_path / "out.csv",
            summary_output=tmp_path / "out-summary.json",
        )


def test_export_rejects_cross_host_business_evidence(tmp_path: Path) -> None:
    targets = tmp_path / "targets.csv"
    write_targets(targets)
    bad_profile = business_profile.extract_business_profile_evidence(
        "https://wrong.example/",
        "SES",
    )
    progress = tmp_path / "bad.jsonl"
    progress.write_text(json.dumps({
        "schema_version": 2,
        "corporate_number": "1000000000001",
        "official_site_url": "https://alpha.example/",
        "state": "processed_no_phone",
        "candidates": [],
        "business_profile": bad_profile,
    }) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Profile evidence host mismatch"):
        priority.export_priority(
            targets=targets,
            progress_patterns=[str(progress)],
            schema=SCHEMA,
            jsonl_output=tmp_path / "out.jsonl",
            csv_output=tmp_path / "out.csv",
            summary_output=tmp_path / "out-summary.json",
        )
