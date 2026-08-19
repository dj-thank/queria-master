from __future__ import annotations

"""Policy-aware, deterministic extraction from an official HTML page.

This module deliberately extracts only values present in the fetched page.  It
does not guess addresses, synthesize email addresses, probe SMTP, or bypass
robots.txt.  Every returned record carries the page URL, retrieval time, and a
content digest so the importer can retain an auditable evidence trail.
"""

import hashlib
import json
import re
import urllib.error
import urllib.request
import urllib.robotparser
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Iterable, Mapping
from urllib.parse import urljoin, urlsplit

from .enrichment import (
    EnrichmentError,
    _as_timestamp,
    _require_corporate_number,
    _now,
    normalize_contact,
    normalize_url,
)


EXTRACTOR_VERSION = "html-contact-v1"
DEFAULT_USER_AGENT = "queria-master-enrichment/0.7 (+public-data-contact-research)"
_EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?:\+81|0)[0-9０-９\s\-‐‑‒–—−ー()（）]{7,}[0-9０-９]")
_CONTACT_HINT_RE = re.compile(
    r"(?:contact|inquiry|inquire|support|toiawase|form|お問い合わせ|問い合わせ|コンタクト|窓口)",
    re.IGNORECASE,
)
_GENERIC_MAILBOXES = {
    "info",
    "contact",
    "inquiry",
    "support",
    "sales",
    "hello",
    "office",
    "admin",
    "代表",
    "総務",
}


@dataclass(frozen=True)
class FetchedPage:
    requested_url: str
    final_url: str | None
    html: str | None
    http_status: int | None
    content_type: str | None
    robots_status: str
    retrieved_at: str
    error: str | None = None


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.forms: list[str] = []
        self.jsonld_parts: list[str] = []
        self._anchor_href: str | None = None
        self._anchor_text: list[str] = []
        self._script_type: str | None = None
        self._script_text: list[str] = []
        self._form_action: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.casefold(): value or "" for key, value in attrs}
        tag = tag.casefold()
        if tag == "a":
            self._anchor_href = attributes.get("href", "")
            self._anchor_text = []
        elif tag == "script":
            self._script_type = attributes.get("type", "").casefold()
            self._script_text = []
        elif tag == "form":
            self._form_action = attributes.get("action", "")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "a" and self._anchor_href is not None:
            self.links.append((self._anchor_href, " ".join(self._anchor_text).strip()))
            self._anchor_href = None
            self._anchor_text = []
        elif tag == "script":
            if self._script_type in {"application/ld+json", "application/json+ld"}:
                self.jsonld_parts.append("".join(self._script_text))
            self._script_type = None
            self._script_text = []
        elif tag == "form" and self._form_action:
            self.forms.append(self._form_action)
            self._form_action = None

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.text_parts.append(data)
        if self._anchor_href is not None:
            self._anchor_text.append(data)
        if self._script_type:
            self._script_text.append(data)


def _fullwidth_to_ascii(value: str) -> str:
    translation = str.maketrans("０１２３４５６７８９＋（）", "0123456789+()")
    return value.translate(translation).replace("ー", "-").replace("−", "-")


def _jsonld_values(value: Any, keys: set[str]) -> Iterable[tuple[str, Any]]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in keys:
                yield str(key).casefold(), item
            yield from _jsonld_values(item, keys)
    elif isinstance(value, list):
        for item in value:
            yield from _jsonld_values(item, keys)


def _parse_jsonld(parts: Iterable[str]) -> tuple[list[str], list[str], list[str]]:
    emails: list[str] = []
    phones: list[str] = []
    urls: list[str] = []
    for text in parts:
        try:
            value = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            continue
        for key, item in _jsonld_values(value, {"email", "telephone", "url", "sameas"}):
            values = item if isinstance(item, list) else [item]
            for candidate in values:
                if not isinstance(candidate, (str, int, float)):
                    continue
                candidate_text = str(candidate).strip()
                if key == "email":
                    emails.append(candidate_text)
                elif key == "telephone":
                    phones.append(candidate_text)
                else:
                    urls.append(candidate_text)
    return emails, phones, urls


def _unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _common_fields(
    corporate_number: str,
    source_url: str,
    retrieved_at: str,
    content_sha256: str,
    source_key: str,
) -> dict[str, Any]:
    return {
        "corporate_number": corporate_number,
        "source_key": source_key,
        "source_url": source_url,
        "retrieved_at": retrieved_at,
        "content_type": "text/html",
        "content_sha256": content_sha256,
        "extractor_version": EXTRACTOR_VERSION,
        "robots_status": "not_checked",
        "policy_status": "review_required",
        "evidence_status": "found",
    }


def _record(
    common: Mapping[str, Any],
    *,
    kind: str,
    value: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    record = dict(common)
    record["kind"] = kind
    if value is not None:
        record["value"] = value
    record.update(fields)
    return record


def _is_generic_mailbox(value: str) -> bool:
    local = value.split("@", 1)[0].casefold()
    return local in _GENERIC_MAILBOXES or any(local.startswith(prefix + ".") for prefix in _GENERIC_MAILBOXES)


def extract_contact_records(
    html: str,
    corporate_number: str,
    page_url: str,
    *,
    retrieved_at: str | None = None,
    source_key: str = "official_site_html",
) -> list[dict[str, Any]]:
    """Extract explicit website/contact facts from one HTML document."""

    corporate_number = _require_corporate_number(corporate_number)
    page_url = normalize_url(page_url)
    retrieved_at = _as_timestamp(retrieved_at)
    if not isinstance(html, str):
        raise EnrichmentError("HTMLは文字列で指定してください。")
    parser = _PageParser()
    parser.feed(html)
    parser.close()
    text = " ".join(parser.text_parts)
    content_sha256 = hashlib.sha256(html.encode("utf-8")).hexdigest()
    common = _common_fields(corporate_number, page_url, retrieved_at, content_sha256, source_key)
    records: list[dict[str, Any]] = []

    page_path = urlsplit(page_url).path.casefold()
    page_role = "contact_page" if _CONTACT_HINT_RE.search(page_path) else "official_homepage"
    records.append(
        _record(
            common,
            kind="website",
            url=page_url,
            website_role=page_role,
            discovery_method="official_html_page",
            status="found",
            confidence=0.8,
        )
    )

    jsonld_emails, jsonld_phones, jsonld_urls = _parse_jsonld(parser.jsonld_parts)
    email_candidates = list(jsonld_emails)
    phone_candidates = list(jsonld_phones)
    url_candidates = list(jsonld_urls)
    for href, label in parser.links:
        if href.casefold().startswith("mailto:"):
            email_candidates.append(href)
        elif href.casefold().startswith("tel:"):
            phone_candidates.append(href[4:])
        if _CONTACT_HINT_RE.search(f"{href} {label}"):
            url_candidates.append(href)
    email_candidates.extend(_EMAIL_RE.findall(text))
    phone_candidates.extend(_PHONE_RE.findall(_fullwidth_to_ascii(text)))

    for candidate in _unique(email_candidates):
        try:
            normalized = normalize_contact("email", candidate)
        except EnrichmentError:
            continue
        records.append(
            _record(
                common,
                kind="contact",
                value=candidate,
                contact_type="email",
                scope="company" if _is_generic_mailbox(normalized) else "person_or_unknown",
                publicness="public_page",
                status="found",
                confidence=0.95 if candidate in jsonld_emails else 0.85,
                verification_status="unverified",
                sales_eligibility="review",
            )
        )

    for candidate in _unique(phone_candidates):
        try:
            normalized = normalize_contact("phone", _fullwidth_to_ascii(candidate))
        except EnrichmentError:
            continue
        records.append(
            _record(
                common,
                kind="contact",
                value=normalized,
                contact_type="phone",
                scope="company",
                publicness="public_page",
                status="found",
                confidence=0.95 if candidate in jsonld_phones else 0.8,
                verification_status="unverified",
                sales_eligibility="review",
            )
        )

    for candidate in [*url_candidates, *parser.forms]:
        try:
            normalized = normalize_contact("form_url", urljoin(page_url, candidate))
        except EnrichmentError:
            continue
        if not _CONTACT_HINT_RE.search(candidate) and candidate not in parser.forms:
            continue
        records.append(
            _record(
                common,
                kind="contact",
                value=normalized,
                contact_type="form_url",
                scope="company",
                publicness="public_page",
                status="found",
                confidence=0.85,
                verification_status="unverified",
                sales_eligibility="review",
            )
        )

    return _deduplicate_records(records)


def _deduplicate_records(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for record in records:
        key = (
            record.get("kind"),
            record.get("contact_type"),
            record.get("value") or record.get("url"),
            record.get("website_role"),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(record))
    return result


def _robots_allowed(url: str, user_agent: str, timeout: float) -> tuple[bool, str]:
    parts = urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    parser = urllib.robotparser.RobotFileParser()
    parser.set_url(robots_url)
    try:
        request = urllib.request.Request(robots_url, headers={"User-Agent": user_agent})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(512_000).decode("utf-8", errors="replace")
        parser.parse(body.splitlines())
        return parser.can_fetch(user_agent, url), "allowed" if parser.can_fetch(user_agent, url) else "blocked"
    except (OSError, urllib.error.URLError, ValueError):
        return False, "unavailable"


def fetch_official_page(
    page_url: str,
    *,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout: float = 15.0,
    max_bytes: int = 2_000_000,
    respect_robots: bool = True,
) -> FetchedPage:
    """Fetch one page with bounded I/O and robots.txt policy enforcement."""

    if timeout <= 0 or max_bytes < 1024:
        raise EnrichmentError("timeoutは正数、max_bytesは1024以上で指定してください。")
    requested_url = normalize_url(page_url)
    retrieved_at = _now()
    if respect_robots:
        allowed, robots_status = _robots_allowed(requested_url, user_agent, timeout)
        if not allowed:
            return FetchedPage(
                requested_url,
                None,
                None,
                None,
                None,
                "blocked" if robots_status == "blocked" else "unavailable",
                retrieved_at,
                "robots.txt policy did not permit a fetch" if robots_status == "blocked" else "robots.txt unavailable",
            )
    else:
        robots_status = "not_checked"
    request = urllib.request.Request(
        requested_url,
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(max_bytes + 1)
            final_url = normalize_url(response.geturl())
            content_type = response.headers.get("Content-Type")
            status = getattr(response, "status", None)
        if len(raw) > max_bytes:
            return FetchedPage(
                requested_url,
                final_url,
                None,
                status,
                content_type,
                robots_status,
                retrieved_at,
                f"response exceeded max_bytes={max_bytes}",
            )
        charset_match = re.search(r"charset=([^;\s]+)", content_type or "", re.IGNORECASE)
        encoding = charset_match.group(1).strip(' \"\'') if charset_match else "utf-8"
        try:
            html = raw.decode(encoding, errors="replace")
        except LookupError:
            html = raw.decode("utf-8", errors="replace")
        return FetchedPage(requested_url, final_url, html, status, content_type, robots_status, retrieved_at)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        return FetchedPage(requested_url, None, None, None, None, robots_status, retrieved_at, str(exc))


def fetch_and_extract_page(
    corporate_number: str,
    page_url: str,
    *,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout: float = 15.0,
    max_bytes: int = 2_000_000,
    respect_robots: bool = True,
    source_key: str = "official_site_html",
) -> list[dict[str, Any]]:
    """Fetch one allowed page and return importer-ready records."""

    page = fetch_official_page(
        page_url,
        user_agent=user_agent,
        timeout=timeout,
        max_bytes=max_bytes,
        respect_robots=respect_robots,
    )
    if page.html is None:
        return [
            {
                "kind": "state",
                "corporate_number": _require_corporate_number(corporate_number),
                "field_name": "website",
                "source_key": source_key,
                "source_url": page.requested_url,
                "retrieved_at": page.retrieved_at,
                "state": "blocked_by_policy" if page.robots_status == "blocked" else "needs_review",
                "error": page.error,
                "robots_status": page.robots_status,
                "policy_code": "robots_disallow" if page.robots_status == "blocked" else "fetch_failed",
                "extractor_version": EXTRACTOR_VERSION,
            }
        ]
    records = extract_contact_records(
        page.html,
        corporate_number,
        page.final_url or page.requested_url,
        retrieved_at=page.retrieved_at,
        source_key=source_key,
    )
    for record in records:
        record["robots_status"] = page.robots_status
        record["http_status"] = page.http_status
        record["policy_status"] = "allowed"
    return records


__all__ = [
    "DEFAULT_USER_AGENT",
    "EXTRACTOR_VERSION",
    "FetchedPage",
    "extract_contact_records",
    "fetch_and_extract_page",
    "fetch_official_page",
]
