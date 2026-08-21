#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gビズインフォ由来の公式URLから代表電話候補を低速・同一ドメイン限定で抽出する。"""
from __future__ import annotations

import argparse
import csv
import html
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

USER_AGENT = "Public-Company-Enricher/1.0 (+official contact discovery; low rate)"
PHONE_RE = re.compile(r"(?<!\d)(?:\+81[-\s]?(?:\(0\))?\d{1,4}|0\d{1,4})[-‐‑‒–—―ー\s()]?\d{1,4}[-‐‑‒–—―ー\s]?\d{3,4}(?!\d)")
LINK_HINTS = ["company", "corporate", "about", "profile", "contact", "outline", "overview", "会社", "企業", "概要", "お問い合わせ", "連絡先"]
FAX_HINTS = ["fax", "ファックス", "ｆａｘ"]
TEL_HINTS = ["tel", "電話", "代表", "お問い合わせ", "contact"]


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
        self.tel_links: list[str] = []
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
            if self._href.lower().startswith("tel:"):
                self.tel_links.append(self._href[4:])

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"} and self._skip:
            self._skip -= 1
        if tag.lower() == "a" and self._href:
            self.links.append((self._href, " ".join(self._link_text).strip()))
            self._href = ""
            self._link_text = []

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        t = re.sub(r"\s+", " ", data).strip()
        if not t:
            return
        self.text_parts.append(t)
        if self._href:
            self._link_text.append(t)


def same_host(url: str, host: str) -> bool:
    h = (urlparse(url).hostname or "").lower().removeprefix("www.")
    return h == host.removeprefix("www.")


def canonical_url(base: str, href: str) -> str:
    href = html.unescape(href.strip())
    if not href or href.startswith(("#", "mailto:", "javascript:", "data:")):
        return ""
    return urldefrag(urljoin(base, href))[0]


def build_robot(session: requests.Session, base_url: str, timeout: float) -> RobotFileParser:
    parsed = urlparse(base_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = RobotFileParser(); rp.set_url(robots_url)
    try:
        r = session.get(robots_url, timeout=timeout)
        if r.status_code == 200:
            rp.parse(r.text.splitlines())
        else:
            rp.parse([])
    except requests.RequestException:
        rp.parse([])
    return rp


def score_candidate(phone: str, context: str, source: str) -> float:
    c = context.lower()
    score = 0.45
    if source == "tel": score += 0.35
    if any(x in c for x in TEL_HINTS): score += 0.20
    if any(x in c for x in FAX_HINTS): score -= 0.55
    if "フリーダイヤル" in c or "0120" in phone or "0800" in phone: score -= 0.05
    if phone.startswith(("050", "0570")): score -= 0.08
    return max(0.0, min(1.0, score))


def extract_candidates(url: str, content: str) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    parser = PageParser()
    try:
        parser.feed(content)
    except Exception:
        pass
    text = " | ".join(parser.text_parts)
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in parser.tel_links:
        phone = normalize_phone(raw)
        if phone and phone not in seen:
            seen.add(phone)
            candidates.append({"phone": phone, "score": score_candidate(phone, "tel link", "tel"), "context": "tel:リンク", "url": url})
    for match in PHONE_RE.finditer(text):
        phone = normalize_phone(match.group(0))
        if not phone or phone in seen:
            continue
        start = max(0, match.start() - 90); end = min(len(text), match.end() + 90)
        context = text[start:end]
        seen.add(phone)
        candidates.append({"phone": phone, "score": score_candidate(phone, context, "text"), "context": context[:240], "url": url})
    return candidates, parser.links


def discover_for_site(session: requests.Session, website: str, max_pages: int, timeout: float, sleep_s: float) -> dict[str, Any] | None:
    if not re.match(r"^https?://", website, re.I):
        website = "https://" + website.lstrip("/")
    parsed = urlparse(website)
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    rp = build_robot(session, website, timeout)
    queue = [website]
    visited: set[str] = set()
    all_candidates: list[dict[str, Any]] = []
    while queue and len(visited) < max_pages:
        url = queue.pop(0)
        if url in visited or not same_host(url, host):
            continue
        if not rp.can_fetch(USER_AGENT, url):
            visited.add(url)
            continue
        try:
            r = session.get(url, timeout=timeout, allow_redirects=True)
            visited.add(url)
            if r.status_code != 200 or "text/html" not in (r.headers.get("Content-Type") or "").lower():
                continue
            if not same_host(r.url, host):
                continue
            candidates, links = extract_candidates(r.url, r.text)
            all_candidates.extend(candidates)
            scored_links: list[tuple[int, str]] = []
            for href, label in links:
                u = canonical_url(r.url, href)
                if not u or not same_host(u, host) or u in visited:
                    continue
                clue = (u + " " + label).lower()
                score = sum(1 for hint in LINK_HINTS if hint.lower() in clue)
                if score:
                    scored_links.append((score, u))
            for _score, u in sorted(scored_links, key=lambda x: (-x[0], len(x[1])))[:max_pages]:
                if u not in queue:
                    queue.append(u)
            time.sleep(sleep_s)
        except requests.RequestException:
            visited.add(url)
            continue
    if not all_candidates:
        return None
    def key(c: dict[str, Any]):
        p = c["phone"]
        fixed_bonus = 1 if not p.startswith(("050", "070", "080", "090", "0570", "0120", "0800")) else 0
        return (c["score"], fixed_bonus, -len(c["context"]))
    return max(all_candidates, key=key)


def load_targets(db: Path, limit: int) -> list[sqlite3.Row]:
    con = sqlite3.connect(db); con.row_factory = sqlite3.Row
    sql = """SELECT c.source_id,c.company_name,m.corporate_number,p.website_url
             FROM companies c JOIN corporate_matches m ON m.source_id=c.source_id AND m.status='accepted'
             JOIN public_master p ON p.corporate_number=m.corporate_number
             WHERE TRIM(COALESCE(p.website_url,''))<>'' ORDER BY c.source_row"""
    rows = con.execute(sql).fetchall(); con.close()
    return rows[:limit] if limit > 0 else rows


def main() -> int:
    ap = argparse.ArgumentParser(description="公式サイトから代表電話候補を取得")
    ap.add_argument("--db", type=Path, default=Path("output/company_public_data.sqlite3"))
    ap.add_argument("--output", type=Path, default=Path("input/公式サイト_電話番号.csv"))
    ap.add_argument("--max-pages", type=int, default=4)
    ap.add_argument("--sleep", type=float, default=1.0)
    ap.add_argument("--timeout", type=float, default=20)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    targets = load_targets(args.db, args.limit)
    session = requests.Session(); session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.7"})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fields = ["SOURCE_ID","企業名","法人番号","公式サイトURL","電話番号","根拠URL","根拠テキスト","信頼度","取得日時"]
    found = 0
    with args.output.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
        for i, row in enumerate(targets, 1):
            result = discover_for_site(session, row["website_url"], args.max_pages, args.timeout, args.sleep)
            if not result:
                continue
            w.writerow({
                "SOURCE_ID": row["source_id"], "企業名": row["company_name"], "法人番号": row["corporate_number"],
                "公式サイトURL": row["website_url"], "電話番号": result["phone"], "根拠URL": result["url"],
                "根拠テキスト": result["context"], "信頼度": f"{result['score']:.2f}", "取得日時": now_iso(),
            })
            found += 1
    print(json.dumps({"targets": len(targets), "phones_found": found, "output": str(args.output)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
