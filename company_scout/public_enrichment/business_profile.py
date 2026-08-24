#!/usr/bin/env python3
"""Bounded official-site evidence extraction and deterministic sales priority.

The module records lexical observations from pages that the existing safe
crawler already fetched.  It never turns those observations into confirmed
corporate relationships or confirmed phone numbers.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urldefrag, urljoin, urlparse


PROFILE_SCHEMA_VERSION = 1
SCORE_FORMULA_VERSION = "it-subsidiary-ses-v1"
MAX_EXCERPT_CHARS = 240

SIGNAL_PATTERNS: dict[str, tuple[str, ...]] = {
    "it_subsidiary": (
        r"情報システム(?:子会社|会社)",
        r"(?:IT|ＩＴ|システム)子会社",
        r"事業子会社",
    ),
    "user_system_it": (
        r"ユーザー系(?:SIer|ＳＩｅｒ|システムインテグレーター)?",
        r"グループ(?:IT|ＩＴ|情報システム)",
        r"情報システム部門",
    ),
    "parent_group": (
        r"(?:100|１００)[%％](?:子会社|出資)",
        r"完全子会社",
        r"全額出資",
        r"株主(?:構成)?",
        r"グループ会社",
        r"連結子会社",
    ),
    "ses": (
        r"(?<![A-Za-z])SES(?![A-Za-z])",
        r"システムエンジニアリングサービス",
        r"技術者派遣",
        r"労働者派遣(?:事業)?",
        r"エンジニア(?:を|の)提供",
    ),
    "onsite_development": (
        r"客先常駐",
        r"(?:お客様|顧客)先(?:に|で)常駐",
        r"人材常駐",
        r"オンサイト(?:運用|支援|開発)",
    ),
    "si": (
        r"システムインテグレーション",
        r"(?<![A-Za-z])SIer(?![A-Za-z])",
        r"システム(?:の)?(?:設計|構築)",
    ),
    "contract_development": (
        r"受託開発",
        r"請負(?:開発|契約)",
        r"ソフトウェア(?:の)?開発",
        r"システム(?:の)?開発",
    ),
    "it_operations": (
        r"運用[・･]?保守",
        r"保守[・･]?運用",
        r"IT運用",
        r"システム運用",
        r"ヘルプデスク",
    ),
    "recruitment": (
        r"採用情報",
        r"募集要項",
        r"エンジニア採用",
    ),
}

PROFILE_DIMENSIONS = tuple((*SIGNAL_PATTERNS.keys(), "contact_form"))
CONTACT_LINK_HINTS = ("contact", "inquiry", "お問い合わせ", "お問合せ", "ご相談")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _excerpt(text: str, start: int, end: int, limit: int) -> str:
    before = max(0, start - 90)
    after = min(len(text), end + 120)
    return clean_text(text[before:after])[:limit]


def _evidence(signal: str, url: str, excerpt: str, observed_at: str) -> dict[str, Any]:
    return {
        "signal": signal,
        "status": "observed_text",
        "evidence_url": url,
        "excerpt": excerpt,
        "excerpt_sha256": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
        "observed_at": observed_at,
    }


def empty_business_profile() -> dict[str, Any]:
    return {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "facts": [],
        "unknowns": list(PROFILE_DIMENSIONS),
    }


def extract_business_profile_evidence(
    url: str,
    visible_text: str,
    links: Iterable[tuple[str, str]] = (),
    *,
    observed_at: str | None = None,
    max_excerpt_chars: int = MAX_EXCERPT_CHARS,
) -> dict[str, Any]:
    """Extract bounded lexical evidence from one already-fetched HTML page."""
    observed_at = observed_at or now_iso()
    text = clean_text(visible_text)
    facts: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    for signal, patterns in SIGNAL_PATTERNS.items():
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            excerpt = _excerpt(text, match.start(), match.end(), max_excerpt_chars)
            key = (signal, url, excerpt)
            if key not in seen:
                seen.add(key)
                facts.append(_evidence(signal, url, excerpt, observed_at))
            break

    base_host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    for href, label in links:
        clue = clean_text(f"{href} {label}").lower()
        if not any(hint.lower() in clue for hint in CONTACT_LINK_HINTS):
            continue
        target = urldefrag(urljoin(url, str(href or "")))[0]
        parsed = urlparse(target)
        target_host = (parsed.hostname or "").lower().removeprefix("www.")
        if parsed.scheme.lower() not in {"http", "https"} or target_host != base_host:
            continue
        excerpt = clean_text(label)[:max_excerpt_chars] or "お問い合わせフォーム"
        key = ("contact_form", target, excerpt)
        if key not in seen:
            seen.add(key)
            facts.append(_evidence("contact_form", target, excerpt, observed_at))

    facts.sort(key=lambda item: (item["signal"], item["evidence_url"], item["excerpt_sha256"]))
    present = {str(item["signal"]) for item in facts}
    return {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "facts": facts,
        "unknowns": [dimension for dimension in PROFILE_DIMENSIONS if dimension not in present],
    }


def merge_business_profiles(profiles: Iterable[dict[str, Any]]) -> dict[str, Any]:
    facts: dict[tuple[str, str, str], dict[str, Any]] = {}
    for profile in profiles:
        for fact in profile.get("facts") or []:
            key = (
                str(fact.get("signal") or ""),
                str(fact.get("evidence_url") or ""),
                str(fact.get("excerpt_sha256") or ""),
            )
            if all(key):
                facts[key] = dict(fact)
    ordered = [facts[key] for key in sorted(facts)]
    present = {str(item["signal"]) for item in ordered}
    return {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "facts": ordered,
        "unknowns": [dimension for dimension in PROFILE_DIMENSIONS if dimension not in present],
    }


def validate_profile_evidence_host(profile: dict[str, Any], official_site_url: str) -> bool:
    official_host = (urlparse(official_site_url).hostname or "").lower().removeprefix("www.")
    if not official_host:
        return False
    for fact in profile.get("facts") or []:
        evidence_host = (
            urlparse(str(fact.get("evidence_url") or "")).hostname or ""
        ).lower().removeprefix("www.")
        if evidence_host != official_host:
            return False
        if not re.fullmatch(r"[a-f0-9]{64}", str(fact.get("excerpt_sha256") or "")):
            return False
        if len(str(fact.get("excerpt") or "")) > MAX_EXCERPT_CHARS:
            return False
    return True


def score_business_profile(
    profile: dict[str, Any] | None,
    candidates: Iterable[dict[str, Any]] = (),
    *,
    industry_code: str = "",
    company_name: str = "",
    collection_state: str = "",
    has_official_site: bool = True,
) -> dict[str, Any]:
    profile = profile or empty_business_profile()
    signals = {str(item.get("signal") or "") for item in profile.get("facts") or []}
    reasons: list[str] = []
    negative_controls: list[str] = []

    it_score = 0
    if signals.intersection({"it_subsidiary", "user_system_it"}):
        it_score += 40
        reasons.append("official_it_subsidiary_or_user_system_signal:+40")
    if "parent_group" in signals:
        it_score += 25
        reasons.append("official_parent_or_group_wording:+25")

    weak_only = False
    if it_score == 0 and re.search(r"(?:^|\D)(?:37|38|39|40|41)(?:\D|$)", industry_code):
        it_score += 10
        weak_only = True
        reasons.append("industry_weak_signal:+10")
        negative_controls.append("industry_only_no_business_text")
    if it_score == 0 and re.search(r"(?:システム|情報|IT|ＩＴ|デジタル)", company_name, re.IGNORECASE):
        it_score += 5
        weak_only = True
        reasons.append("company_name_weak_signal:+5")
        negative_controls.append("company_name_only_not_business_fact")
    it_score = min(65, it_score)

    ses_score = 0
    if signals.intersection({"ses", "onsite_development"}):
        ses_score += 45
        reasons.append("official_ses_or_onsite_signal:+45")
    if signals.intersection({"si", "contract_development", "it_operations"}):
        ses_score += 20
        reasons.append("official_si_contract_or_operations_signal:+20")
    elif "recruitment" in signals:
        ses_score += 5
        reasons.append("recruitment_only_signal:+5")
        negative_controls.append("recruitment_only_not_ses_fact")
    ses_score = min(65, ses_score)

    candidate_list = list(candidates)
    voice = [item for item in candidate_list if str(item.get("candidate_type") or "") != "FAX"]
    contact_score = 0
    if voice:
        preferred = {"代表電話", "本社電話", "問い合わせ電話"}
        contact_score += 20 if any(str(item.get("candidate_type") or "") in preferred for item in voice) else 12
        reasons.append("official_site_voice_phone_candidate:+20" if contact_score == 20 else "official_site_voice_phone_candidate:+12")
        negative_controls.append("phone_candidate_unconfirmed")
    elif candidate_list:
        negative_controls.append("fax_only")
    if "contact_form" in signals:
        contact_score += 10
        reasons.append("official_same_host_contact_form:+10")
    elif has_official_site:
        contact_score += 5
        reasons.append("official_site_available:+5")
    contact_score = min(30, contact_score)

    strong_business = bool(
        signals.intersection(
            {
                "it_subsidiary",
                "user_system_it",
                "ses",
                "onsite_development",
                "si",
                "contract_development",
                "it_operations",
            }
        )
    )
    total = min(100, it_score + ses_score + contact_score)
    if collection_state == "blocked_by_policy":
        tier = "blocked"
    elif strong_business and contact_score >= 10 and total >= 60:
        tier = "A"
    elif strong_business:
        tier = "B"
    elif weak_only:
        tier = "C"
    else:
        tier = "unknown"
    if "parent_group" not in signals:
        negative_controls.append("parent_relation_not_proven")
    if not strong_business:
        negative_controls.append("no_official_business_fit_evidence")

    return {
        "formula_version": SCORE_FORMULA_VERSION,
        "it_subsidiary_score": it_score,
        "ses_sales_score": ses_score,
        "contactability_score": contact_score,
        "priority_score": total,
        "tier": tier,
        "reasons": sorted(set(reasons)),
        "negative_controls": sorted(set(negative_controls)),
    }
