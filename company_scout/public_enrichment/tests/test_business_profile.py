from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import business_profile as profile
import official_site_phone_enricher as phone


def signals(result: dict) -> set[str]:
    return {item["signal"] for item in result["facts"]}


def test_extracts_bounded_observations_and_same_host_contact_form() -> None:
    result = profile.extract_business_profile_evidence(
        "https://alpha.example/company",
        "当社は親会社が100％出資する情報システム子会社です。SES・客先常駐・受託開発・運用保守を提供します。",
        [
            ("/contact/", "お問い合わせ"),
            ("https://outside.example/contact", "外部問い合わせ"),
        ],
        observed_at="2026-08-25T00:00:00+00:00",
    )

    assert {
        "it_subsidiary",
        "parent_group",
        "ses",
        "onsite_development",
        "contract_development",
        "it_operations",
        "contact_form",
    }.issubset(signals(result))
    assert all(len(item["excerpt"]) <= profile.MAX_EXCERPT_CHARS for item in result["facts"])
    assert all(len(item["excerpt_sha256"]) == 64 for item in result["facts"])
    contact_urls = [item["evidence_url"] for item in result["facts"] if item["signal"] == "contact_form"]
    assert contact_urls == ["https://alpha.example/contact/"]


def test_unknowns_are_explicit_and_missing_is_not_false() -> None:
    result = profile.extract_business_profile_evidence(
        "https://alpha.example/",
        "会社概要 所在地 東京都",
    )
    assert result["facts"] == []
    assert set(result["unknowns"]) == set(profile.PROFILE_DIMENSIONS)


def test_profile_merge_is_deterministic_and_deduplicates() -> None:
    page = profile.extract_business_profile_evidence(
        "https://alpha.example/about",
        "受託開発とシステム運用を提供",
        observed_at="2026-08-25T00:00:00+00:00",
    )
    merged = profile.merge_business_profiles([page, page])
    assert len(merged["facts"]) == len(page["facts"])
    assert merged == profile.merge_business_profiles([page])


def test_score_prioritizes_evidence_backed_it_subsidiary_ses_and_contact() -> None:
    result = profile.extract_business_profile_evidence(
        "https://alpha.example/",
        "完全子会社の情報システム子会社としてSES、客先常駐、受託開発、運用保守を提供",
        [("/contact", "お問い合わせ")],
    )
    assert "ses" in signals(result)
    scored = profile.score_business_profile(
        result,
        [{"candidate_type": "代表電話", "phone": "0312345678"}],
        industry_code="39",
        company_name="Alphaシステム株式会社",
        collection_state="phone_candidate_found",
    )
    assert scored["it_subsidiary_score"] == 65
    assert scored["ses_sales_score"] == 65
    assert scored["contactability_score"] == 30
    assert scored["priority_score"] == 100
    assert scored["tier"] == "A"
    assert "phone_candidate_unconfirmed" in scored["negative_controls"]


def test_fax_and_name_or_industry_only_never_become_strong_business_evidence() -> None:
    scored = profile.score_business_profile(
        profile.empty_business_profile(),
        [{"candidate_type": "FAX", "phone": "0312345678"}],
        industry_code="G|39",
        company_name="Alphaシステム株式会社",
        collection_state="fax_only",
    )
    assert scored["tier"] == "C"
    assert scored["contactability_score"] == 5
    assert "fax_only" in scored["negative_controls"]
    assert "industry_only_no_business_text" in scored["negative_controls"]


def test_blocked_collection_cannot_be_promoted() -> None:
    result = profile.extract_business_profile_evidence(
        "https://alpha.example/",
        "SESと受託開発",
    )
    scored = profile.score_business_profile(result, collection_state="blocked_by_policy")
    assert scored["tier"] == "blocked"


def test_profile_host_validation_rejects_cross_host_and_oversized_excerpt() -> None:
    result = profile.extract_business_profile_evidence(
        "https://alpha.example/about",
        "受託開発",
    )
    assert profile.validate_profile_evidence_host(result, "https://alpha.example/")
    result["facts"][0]["evidence_url"] = "https://wrong.example/about"
    assert not profile.validate_profile_evidence_host(result, "https://alpha.example/")


def test_progress_binding_rejects_cross_host_profile_evidence() -> None:
    targets = [{
        "corporate_number": "1000000000001",
        "website_url": "https://alpha.example/",
    }]
    progress = {
        "1000000000001": {
            "official_site_url": "https://alpha.example/",
            "state": "processed_no_phone",
            "candidates": [],
            "business_profile": {
                "schema_version": 1,
                "facts": [{
                    "signal": "ses",
                    "status": "observed_text",
                    "evidence_url": "https://wrong.example/",
                    "excerpt": "SES",
                    "excerpt_sha256": "a" * 64,
                    "observed_at": "2026-08-25T00:00:00+00:00",
                }],
                "unknowns": [],
            },
        }
    }
    with pytest.raises(ValueError, match="business profile evidence host mismatch"):
        phone.validate_progress_bindings(targets, progress)


def test_retry_missing_profile_upgrades_v1_progress(tmp_path: Path) -> None:
    targets = [{
        "source_id": "alpha",
        "company_name": "Alpha",
        "corporate_number": "1000000000001",
        "website_url": "https://alpha.example/",
    }]
    progress_path = tmp_path / "progress.jsonl"
    progress_path.write_text(
        json.dumps({
            "schema_version": 1,
            "corporate_number": "1000000000001",
            "official_site_url": "https://alpha.example/",
            "state": "processed_no_phone",
            "candidates": [],
            "completed_at": "2026-08-24T00:00:00Z",
        }) + "\n",
        encoding="utf-8",
    )

    def discoverer(*_args):
        return {
            "state": "processed_no_phone",
            "pages_fetched": 1,
            "reason": None,
            "candidates": [],
            "business_profile": profile.extract_business_profile_evidence(
                "https://alpha.example/",
                "受託開発と運用保守",
            ),
        }

    result = phone.collect_targets(
        targets,
        session=object(),
        output=tmp_path / "phones.csv",
        progress=progress_path,
        max_pages=4,
        max_candidates=5,
        timeout=20,
        sleep_s=0,
        resume=True,
        retry_missing_profile=True,
        discoverer=discoverer,
    )
    latest, ignored = phone.load_progress(progress_path)
    assert result["retried_this_run"] == 1
    assert result["profiles_with_evidence"] == 1
    assert ignored == 0
    assert latest["1000000000001"]["schema_version"] == 2
    assert latest["1000000000001"]["business_profile"]["facts"]
