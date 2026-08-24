#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公的情報で確認した公式URLから電話番号候補を低速・同一ドメイン限定で抽出する。"""
from __future__ import annotations

import argparse
import csv
import html
import ipaddress
import os
import socket
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urldefrag
from urllib.robotparser import RobotFileParser

import requests

USER_AGENT = "Public-Company-Enricher/1.1 (+official contact discovery; low rate)"
MAX_HTML_BYTES = 2_000_000
MAX_ROBOTS_BYTES = 512_000
MAX_REDIRECTS = 5
PHONE_RE = re.compile(r"(?<!\d)(?:\+81[-\s]?(?:\(0\))?\d{1,4}|0\d{1,4})[-‐‑‒–—―ー\s()]?\d{1,4}[-‐‑‒–—―ー\s]?\d{3,4}(?!\d)")
LINK_HINTS = ["company", "corporate", "about", "profile", "contact", "outline", "overview", "会社", "企業", "概要", "お問い合わせ", "連絡先"]
FAX_HINTS = ["fax", "ファックス", "ｆａｘ"]
REPRESENTATIVE_HINTS = ["代表電話", "大代表", "本社代表", "代表 tel", "代表電話番号"]
HEAD_OFFICE_HINTS = ["本社電話", "本社 tel", "本社連絡先"]
CONTACT_HINTS = ["tel", "電話", "お電話", "お問い合わせ", "問合せ", "連絡先", "contact"]
RECRUIT_HINTS = ["採用", "求人", "応募"]
SUPPORT_HINTS = ["サポート", "ヘルプ", "カスタマーセンター"]
MEDIA_IR_HINTS = ["広報", "報道", "ir", "株主"]
PRIVACY_HINTS = ["個人情報", "苦情"]
BRANCH_HINTS = ["支店", "営業所", "店舗", "事業所"]
PURPOSE_LIMIT_HINTS = RECRUIT_HINTS + SUPPORT_HINTS + MEDIA_IR_HINTS + PRIVACY_HINTS + BRANCH_HINTS


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_phone(value: str) -> str:
    s = value.replace("+81", "0")
    digits = re.sub(r"\D", "", s)
    if 10 <= len(digits) <= 11 and digits.startswith("0"):
        return digits
    return ""


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.tel_links: list[tuple[str, str]] = []
        self._href = ""
        self._link_text: list[str] = []
        self.text_parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k.lower(): (v or "") for k, v in attrs}
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self._skip += 1
        if tag.lower() == "a":
            self._href = attr.get("href", "")
            self._link_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"} and self._skip:
            self._skip -= 1
        if tag.lower() == "a" and self._href:
            label = " ".join(self._link_text).strip()
            self.links.append((self._href, label))
            if self._href.lower().startswith("tel:"):
                self.tel_links.append((self._href[4:], label))
            self._href = ""
            self._link_text = []

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        text = re.sub(r"\s+", " ", data).strip()
        if not text:
            return
        self.text_parts.append(text)
        if self._href:
            self._link_text.append(text)


def same_host(url: str, host: str) -> bool:
    parsed_host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    return parsed_host == host.removeprefix("www.")


def canonical_url(base: str, href: str) -> str:
    href = html.unescape(href.strip())
    if not href or href.startswith(("#", "mailto:", "javascript:", "data:")):
        return ""
    return urldefrag(urljoin(base, href))[0]


def is_public_http_url(url: str) -> bool:
    """Reject non-web schemes and destinations that can reach local/private networks."""
    try:
        parsed = urlparse(url)
        if parsed.scheme.lower() not in {"http", "https"}:
            return False
        if parsed.username or parsed.password:
            return False
        host = (parsed.hostname or "").strip(".").lower()
        if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            return False
        if parsed.port not in {None, 80, 443}:
            return False
        infos = socket.getaddrinfo(
            host,
            parsed.port or (443 if parsed.scheme.lower() == "https" else 80),
            type=socket.SOCK_STREAM,
        )
        addresses = {info[4][0].split("%", 1)[0] for info in infos}
        if not addresses:
            return False
        return all(ipaddress.ip_address(address).is_global for address in addresses)
    except (OSError, ValueError):
        return False


def safe_get_text(
    session: requests.Session,
    url: str,
    *,
    expected_host: str,
    timeout: float,
    max_bytes: int,
) -> tuple[str, int, dict[str, str], str] | None:
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        if not is_public_http_url(current) or not same_host(current, expected_host):
            return None
        try:
            response = session.get(current, timeout=timeout, allow_redirects=False, stream=True)
        except requests.RequestException:
            return None
        try:
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location") or ""
                next_url = canonical_url(current, location)
                if not next_url:
                    return None
                current = next_url
                continue
            length = response.headers.get("Content-Length")
            if length and int(length) > max_bytes:
                return None
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    return None
                chunks.append(chunk)
            encoding = response.encoding or "utf-8"
            text = b"".join(chunks).decode(encoding, errors="replace")
            return current, response.status_code, dict(response.headers), text
        except (ValueError, LookupError):
            return None
        finally:
            response.close()
    return None


def load_robot_policy(
    session: requests.Session,
    base_url: str,
    timeout: float,
) -> tuple[RobotFileParser, str | None]:
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").lower()
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    parser = RobotFileParser()
    parser.set_url(robots_url)
    result = safe_get_text(
        session,
        robots_url,
        expected_host=host,
        timeout=timeout,
        max_bytes=MAX_ROBOTS_BYTES,
    )
    if result is None:
        parser.parse(["User-agent: *", "Disallow: /"])
        return parser, "robots_unavailable"
    _url, status, _headers, text = result
    if status == 200:
        parser.parse(text.splitlines())
    elif status == 404:
        parser.parse([])
    else:
        parser.parse(["User-agent: *", "Disallow: /"])
        return parser, f"robots_http_{status}"
    return parser, None


def build_robot(session: requests.Session, base_url: str, timeout: float) -> RobotFileParser:
    parser, _reason = load_robot_policy(session, base_url, timeout)
    return parser


def score_candidate(phone: str, context: str, source: str) -> float:
    """Score visible, purpose-labelled evidence above unlabeled tel links."""
    normalized_context = re.sub(r"\s+", " ", context).strip().lower()
    score = 0.40
    if source == "text":
        score += 0.10
    elif source == "tel" and normalized_context:
        score += 0.04
    if any(hint in normalized_context for hint in REPRESENTATIVE_HINTS):
        score += 0.28
    elif any(hint in normalized_context for hint in HEAD_OFFICE_HINTS):
        score += 0.24
    elif any(hint in normalized_context for hint in CONTACT_HINTS):
        score += 0.18
    if any(hint in normalized_context for hint in FAX_HINTS):
        score -= 0.75
    if any(hint in normalized_context for hint in PURPOSE_LIMIT_HINTS):
        score -= 0.18
    if not normalized_context or normalized_context in {"tel link", "tel:リンク", "電話する", "call"}:
        score -= 0.10
    if phone.startswith(("050", "0570")):
        score -= 0.04
    if phone.startswith(("070", "080", "090")) and not any(
        hint in normalized_context for hint in REPRESENTATIVE_HINTS
    ):
        score -= 0.08
    return max(0.0, min(1.0, score))


def classify_candidate(context: str) -> str:
    normalized_context = re.sub(r"\s+", " ", context).strip().lower()
    if any(hint in normalized_context for hint in FAX_HINTS):
        return "FAX"
    if any(hint in normalized_context for hint in REPRESENTATIVE_HINTS):
        return "代表電話"
    if any(hint in normalized_context for hint in HEAD_OFFICE_HINTS):
        return "本社電話"
    if any(hint in normalized_context for hint in RECRUIT_HINTS):
        return "採用窓口"
    if any(hint in normalized_context for hint in SUPPORT_HINTS):
        return "サポート窓口"
    if any(hint in normalized_context for hint in MEDIA_IR_HINTS):
        return "広報・IR窓口"
    if any(hint in normalized_context for hint in PRIVACY_HINTS):
        return "個人情報・相談窓口"
    if any(hint in normalized_context for hint in BRANCH_HINTS):
        return "支店・事業所"
    if any(hint in normalized_context for hint in CONTACT_HINTS):
        return "問い合わせ電話"
    return "未分類"


def candidate_sort_key(candidate: dict[str, Any]) -> tuple[float, int, int, int, int]:
    """Rank by evidence quality; visible/representative context wins close ties."""
    context = str(candidate.get("context") or "").lower()
    phone = str(candidate.get("phone") or "")
    representative = int(any(hint in context for hint in REPRESENTATIVE_HINTS))
    visible = int(candidate.get("source") == "text")
    fixed = int(not phone.startswith(("050", "070", "080", "090", "0570", "0120", "0800")))
    return (
        float(candidate.get("score") or 0.0),
        representative,
        visible,
        fixed,
        len(context),
    )


def extract_candidates(url: str, content: str) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    parser = PageParser()
    try:
        parser.feed(content)
    except Exception:
        pass
    text = " | ".join(parser.text_parts)
    candidates_by_phone: dict[str, dict[str, Any]] = {}

    def add_candidate(phone: str, context: str, source: str) -> None:
        if not phone:
            return
        candidate = {
            "phone": phone,
            "score": score_candidate(phone, context, source),
            "context": context[:240],
            "url": url,
            "source": source,
            "candidate_type": classify_candidate(context),
        }
        current = candidates_by_phone.get(phone)
        if current is None or candidate_sort_key(candidate) > candidate_sort_key(current):
            candidates_by_phone[phone] = candidate

    # Visible text is stronger evidence than a tel: href that can be hidden,
    # duplicated, or attached to a purpose-specific floating button.
    for match in PHONE_RE.finditer(text):
        phone = normalize_phone(match.group(0))
        if not phone:
            continue
        start = max(0, match.start() - 90)
        end = min(len(text), match.end() + 90)
        add_candidate(phone, text[start:end], "text")

    for raw, label in parser.tel_links:
        phone = normalize_phone(raw)
        add_candidate(phone, label or "tel:リンク", "tel")

    return list(candidates_by_phone.values()), parser.links


def _crawl_site(
    session: requests.Session,
    website: str,
    max_pages: int,
    timeout: float,
    sleep_s: float,
    robot: RobotFileParser | None = None,
) -> dict[str, Any]:
    if not re.match(r"^https?://", website, re.I):
        website = "https://" + website.lstrip("/")
    parsed = urlparse(website)
    host = (parsed.hostname or "").lower()
    if not host or not is_public_http_url(website):
        return {"candidates": [], "pages_fetched": 0, "fetch_failures": 0, "policy_skips": 1}
    robot = robot or build_robot(session, website, timeout)
    queue = [website]
    visited: set[str] = set()
    candidates_by_phone: dict[str, dict[str, Any]] = {}
    pages_fetched = 0
    fetch_failures = 0
    policy_skips = 0
    while queue and len(visited) < max_pages:
        url = queue.pop(0)
        if url in visited or not same_host(url, host):
            continue
        visited.add(url)
        if not robot.can_fetch(USER_AGENT, url):
            policy_skips += 1
            continue
        result = safe_get_text(
            session,
            url,
            expected_host=host,
            timeout=timeout,
            max_bytes=MAX_HTML_BYTES,
        )
        if result is None:
            fetch_failures += 1
            continue
        final_url, status, headers, text = result
        content_type = headers.get("Content-Type") or headers.get("content-type") or ""
        if status != 200 or "text/html" not in content_type.lower():
            fetch_failures += 1
            continue
        pages_fetched += 1
        candidates, links = extract_candidates(final_url, text)
        for candidate in candidates:
            phone = str(candidate["phone"])
            current = candidates_by_phone.get(phone)
            if current is None or candidate_sort_key(candidate) > candidate_sort_key(current):
                candidates_by_phone[phone] = candidate
        scored_links: list[tuple[int, str]] = []
        for href, label in links:
            candidate_url = canonical_url(final_url, href)
            if not candidate_url or not same_host(candidate_url, host) or candidate_url in visited:
                continue
            clue = (candidate_url + " " + label).lower()
            score = sum(1 for hint in LINK_HINTS if hint.lower() in clue)
            if score:
                scored_links.append((score, candidate_url))
        for _score, candidate_url in sorted(scored_links, key=lambda item: (-item[0], len(item[1])))[:max_pages]:
            if candidate_url not in queue:
                queue.append(candidate_url)
        time.sleep(max(0.0, sleep_s))
    return {
        "candidates": sorted(candidates_by_phone.values(), key=candidate_sort_key, reverse=True),
        "pages_fetched": pages_fetched,
        "fetch_failures": fetch_failures,
        "policy_skips": policy_skips,
    }


def discover_candidates_for_site(
    session: requests.Session,
    website: str,
    max_pages: int,
    timeout: float,
    sleep_s: float,
    robot: RobotFileParser | None = None,
) -> list[dict[str, Any]]:
    return list(_crawl_site(session, website, max_pages, timeout, sleep_s, robot=robot)["candidates"])


def discover_for_site(
    session: requests.Session,
    website: str,
    max_pages: int,
    timeout: float,
    sleep_s: float,
) -> dict[str, Any] | None:
    """Backward-compatible helper returning only the strongest candidate."""
    candidates = discover_candidates_for_site(session, website, max_pages, timeout, sleep_s)
    return candidates[0] if candidates else None


def load_targets(db: Path, limit: int) -> list[sqlite3.Row]:
    connection = sqlite3.connect(db)
    connection.row_factory = sqlite3.Row
    sql = """SELECT c.source_id,c.company_name,m.corporate_number,p.website_url
             FROM companies c JOIN corporate_matches m ON m.source_id=c.source_id AND m.status='accepted'
             JOIN public_master p ON p.corporate_number=m.corporate_number
             WHERE TRIM(COALESCE(p.website_url,''))<>'' ORDER BY c.source_row"""
    rows = connection.execute(sql).fetchall()
    connection.close()
    return rows[:limit] if limit > 0 else rows


OUTPUT_FIELDS = [
    "SOURCE_ID",
    "企業名",
    "法人番号",
    "公式サイトURL",
    "候補順位",
    "電話番号",
    "電話種別候補",
    "根拠URL",
    "根拠テキスト",
    "抽出方法",
    "信頼度",
    "取得日時",
]
TERMINAL_STATES = {
    "phone_candidate_found",
    "fax_only",
    "processed_no_phone",
    "blocked_by_policy",
    "needs_review",
}


def _row_value(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def official_site_binding(value: Any) -> tuple[str, str, str]:
    url = str(value or "").strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url.lstrip("/")
    parsed = urlparse(url)
    return (
        (parsed.hostname or "").lower().removeprefix("www."),
        parsed.path.rstrip("/") or "/",
        parsed.query,
    )


def validate_progress_bindings(targets: list[Any], progress_by_company: dict[str, dict[str, Any]]) -> None:
    for row in targets:
        corporate_number = str(_row_value(row, "corporate_number") or "").strip()
        record = progress_by_company.get(corporate_number)
        if record is None:
            continue
        target_url = str(_row_value(row, "website_url") or "")
        record_url = str(record.get("official_site_url") or "")
        if not record_url or official_site_binding(record_url) != official_site_binding(target_url):
            raise ValueError(f"official site mismatch in progress: {corporate_number}")
        target_host = official_site_binding(target_url)[0]
        for candidate in record.get("candidates") or []:
            evidence_url = str(candidate.get("url") or "")
            if not evidence_url or official_site_binding(evidence_url)[0] != target_host:
                raise ValueError(f"candidate evidence host mismatch in progress: {corporate_number}")
            if not normalize_phone(str(candidate.get("phone") or "")):
                raise ValueError(f"invalid phone candidate in progress: {corporate_number}")


def load_progress(path: Path) -> tuple[dict[str, dict[str, Any]], int]:
    """Load the latest record per company, tolerating one truncated tail line."""
    if not path.is_file():
        return {}, 0
    lines = path.read_text(encoding="utf-8").splitlines()
    latest: dict[str, dict[str, Any]] = {}
    ignored_tail_lines = 0
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            if index == len(lines) - 1:
                ignored_tail_lines += 1
                continue
            raise ValueError(f"Invalid progress JSONL at {path}:{index + 1}")
        corporate_number = str(record.get("corporate_number") or "").strip()
        state = str(record.get("state") or "").strip()
        if not corporate_number or state not in TERMINAL_STATES:
            raise ValueError(f"Invalid progress record at {path}:{index + 1}")
        latest[corporate_number] = record
    return latest, ignored_tail_lines


def append_progress(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    with path.open("ab+") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell():
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) != b"\n":
                handle.seek(0, os.SEEK_END)
                handle.write(b"\n")
        handle.seek(0, os.SEEK_END)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def truncate_invalid_progress_tail(path: Path) -> None:
    data = path.read_bytes()
    last_newline = data.rfind(b"\n")
    valid = data[: last_newline + 1] if last_newline >= 0 else b""
    with path.open("r+b") as handle:
        handle.seek(0)
        handle.write(valid)
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())


def write_candidate_output(
    output: Path,
    targets: list[Any],
    progress_by_company: dict[str, dict[str, Any]],
) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        for row in targets:
            corporate_number = str(_row_value(row, "corporate_number") or "").strip()
            record = progress_by_company.get(corporate_number) or {}
            candidates = list(record.get("candidates") or [])
            for rank, candidate in enumerate(candidates, start=1):
                writer.writerow(
                    {
                        "SOURCE_ID": _row_value(row, "source_id"),
                        "企業名": _row_value(row, "company_name"),
                        "法人番号": corporate_number,
                        "公式サイトURL": _row_value(row, "website_url"),
                        "候補順位": rank,
                        "電話番号": candidate.get("phone"),
                        "電話種別候補": candidate.get("candidate_type"),
                        "根拠URL": candidate.get("url"),
                        "根拠テキスト": candidate.get("context"),
                        "抽出方法": candidate.get("source"),
                        "信頼度": f"{float(candidate.get('score') or 0):.2f}",
                        "取得日時": record.get("completed_at"),
                    }
                )
                written += 1
    return written


def discover_site_result(
    session: requests.Session,
    website: str,
    max_pages: int,
    timeout: float,
    sleep_s: float,
) -> dict[str, Any]:
    if not re.match(r"^https?://", website, re.I):
        website = "https://" + website.lstrip("/")
    parsed = urlparse(website)
    host = (parsed.hostname or "").lower()
    if not host or not is_public_http_url(website):
        return {
            "state": "blocked_by_policy",
            "pages_fetched": 0,
            "reason": "unsafe_or_unresolvable_url",
            "candidates": [],
        }
    robot, policy_reason = load_robot_policy(session, website, timeout)
    if policy_reason:
        return {
            "state": "needs_review",
            "pages_fetched": 0,
            "reason": policy_reason,
            "candidates": [],
        }
    if not robot.can_fetch(USER_AGENT, website):
        return {
            "state": "blocked_by_policy",
            "pages_fetched": 0,
            "reason": "robots_disallow",
            "candidates": [],
        }
    crawl = _crawl_site(
        session,
        website,
        max_pages,
        timeout,
        sleep_s,
        robot=robot,
    )
    candidates = list(crawl["candidates"])
    voice_candidates = [candidate for candidate in candidates if candidate.get("candidate_type") != "FAX"]
    pages_fetched = int(crawl["pages_fetched"])
    if voice_candidates:
        state = "phone_candidate_found"
        reason = None
    elif candidates:
        state = "fax_only"
        reason = "fax_only"
    elif pages_fetched:
        state = "processed_no_phone"
        reason = None
    elif int(crawl["policy_skips"]):
        state = "blocked_by_policy"
        reason = "robots_disallow"
    else:
        state = "needs_review"
        reason = "fetch_failed"
    return {
        "state": state,
        "pages_fetched": pages_fetched,
        "reason": reason,
        "candidates": candidates,
    }


def collect_targets(
    targets: list[Any],
    *,
    session: Any,
    output: Path,
    progress: Path,
    max_pages: int,
    max_candidates: int,
    timeout: float,
    sleep_s: float,
    resume: bool,
    retry_states: set[str] | None = None,
    discoverer: Any = discover_site_result,
) -> dict[str, Any]:
    retry_states = set(retry_states or ())
    unsupported_retry_states = retry_states.difference(TERMINAL_STATES)
    if unsupported_retry_states:
        raise ValueError(f"Unsupported retry states: {sorted(unsupported_retry_states)}")
    progress.parent.mkdir(parents=True, exist_ok=True)
    progress.touch(exist_ok=True)
    progress_by_company, ignored_tail_lines = load_progress(progress) if resume else ({}, 0)
    if ignored_tail_lines:
        truncate_invalid_progress_tail(progress)
    target_numbers = {
        str(_row_value(row, "corporate_number") or "").strip()
        for row in targets
        if str(_row_value(row, "corporate_number") or "").strip()
    }
    validate_progress_bindings(targets, progress_by_company)
    already_completed = sum(
        1
        for number in target_numbers
        if number in progress_by_company and progress_by_company[number]["state"] not in retry_states
    )
    attempted = 0
    retried = 0
    for row in targets:
        corporate_number = str(_row_value(row, "corporate_number") or "").strip()
        existing = progress_by_company.get(corporate_number)
        if not corporate_number or (resume and existing and existing["state"] not in retry_states):
            continue
        if existing:
            retried += 1
        result = discoverer(
            session,
            str(_row_value(row, "website_url") or ""),
            max_pages,
            timeout,
            sleep_s,
        )
        state = str(result.get("state") or "").strip()
        if state not in TERMINAL_STATES:
            raise ValueError(f"Unsupported collection state: {state}")
        candidates = sorted(
            list(result.get("candidates") or []),
            key=candidate_sort_key,
            reverse=True,
        )[:max_candidates]
        record = {
            "schema_version": 1,
            "source_id": _row_value(row, "source_id"),
            "company_name": _row_value(row, "company_name"),
            "corporate_number": corporate_number,
            "official_site_url": _row_value(row, "website_url"),
            "state": state,
            "pages_fetched": result.get("pages_fetched"),
            "reason": result.get("reason"),
            "candidates": candidates,
            "completed_at": now_iso(),
        }
        append_progress(progress, record)
        progress_by_company[corporate_number] = record
        attempted += 1

    candidates_written = write_candidate_output(output, targets, progress_by_company)
    state_counts: dict[str, int] = {}
    for corporate_number in target_numbers:
        record = progress_by_company.get(corporate_number)
        if not record:
            continue
        state = str(record["state"])
        state_counts[state] = state_counts.get(state, 0) + 1
    return {
        "targets": len(target_numbers),
        "already_completed": already_completed,
        "attempted_this_run": attempted,
        "retried_this_run": retried,
        "completed_total": sum(state_counts.values()),
        "states": state_counts,
        "companies_with_candidates": state_counts.get("phone_candidate_found", 0),
        "phone_candidates_written": candidates_written,
        "ignored_truncated_tail_lines": ignored_tail_lines,
        "output": str(output),
        "progress": str(progress),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="公式サイトから電話番号候補を取得")
    parser.add_argument("--db", type=Path, default=Path("output/company_public_data.sqlite3"))
    parser.add_argument("--output", type=Path, default=Path("input/公式サイト_電話番号.csv"))
    parser.add_argument("--max-pages", type=int, default=4)
    parser.add_argument("--max-candidates", type=int, default=1)
    parser.add_argument("--sleep", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--progress", type=Path, help="再開用のappend-only JSONL")
    parser.add_argument("--summary", type=Path, help="今回と累積の件数JSON")
    parser.add_argument("--restart", action="store_true", help="指定progressを破棄して最初から処理する")
    parser.add_argument(
        "--retry-state",
        action="append",
        default=[],
        choices=sorted(TERMINAL_STATES),
        help="指定状態だけを再試行する（複数指定可）",
    )
    parser.add_argument("--trust-env", action="store_true", help="requestsのプロキシ環境変数を利用する")
    args = parser.parse_args()
    if args.max_candidates < 1:
        parser.error("--max-candidates は1以上で指定してください")

    targets = load_targets(args.db, args.limit)
    session = requests.Session()
    session.trust_env = args.trust_env
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.7"})
    progress = args.progress or args.output.with_suffix(".progress.jsonl")
    if args.restart and progress.exists():
        progress.unlink()
    result = collect_targets(
        targets,
        session=session,
        output=args.output,
        progress=progress,
        max_pages=args.max_pages,
        max_candidates=args.max_candidates,
        timeout=args.timeout,
        sleep_s=args.sleep,
        resume=True,
        retry_states=set(args.retry_state),
    )
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
