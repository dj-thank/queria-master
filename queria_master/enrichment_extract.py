from __future__ import annotations

"""Policy-aware, deterministic extraction from an official HTML page.

This module deliberately extracts only values present in the fetched page.  It
does not guess addresses, synthesize email addresses, probe SMTP, or bypass
robots.txt.  Every returned record carries the page URL, retrieval time, and a
content digest so the importer can retain an auditable evidence trail.
"""

import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import urllib.robotparser
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote, urljoin, urlsplit

from .enrichment import (
    EnrichmentError,
    _as_timestamp,
    _require_corporate_number,
    _now,
    normalize_contact,
    normalize_url,
)
from .website_discovery import validate_public_website_url


EXTRACTOR_VERSION = "html-contact-v1"
DEFAULT_USER_AGENT = "queria-master-enrichment/0.7 (+public-data-contact-research)"
MAX_REDIRECTS = 5
MAX_DNS_ADDRESSES = 32
MAX_ROBOTS_BYTES = 512_000
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


class NetworkPolicyError(EnrichmentError):
    """A URL or resolved destination violates the outbound network policy."""


@dataclass(frozen=True)
class _HttpResult:
    final_url: str
    status: int
    headers: Mapping[str, str]
    body: bytes


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


def _policy_url(value: str) -> str:
    try:
        return validate_public_website_url(value)
    except EnrichmentError as exc:
        raise NetworkPolicyError(str(exc)) from exc


def _verified_host(value: str) -> str:
    host = (urlsplit(value).hostname or "").strip(".").casefold()
    return host.removeprefix("www.")


def _resolve_public_targets(url: str) -> tuple[str, list[tuple[int, str]]]:
    """Resolve once, reject mixed/private answers, and return pinned targets."""

    normalized = _policy_url(url)
    parts = urlsplit(normalized)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        try:
            infos = socket.getaddrinfo(
                host,
                port,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except OSError as exc:
            raise OSError(f"DNS解決に失敗しました: {host}") from exc
        targets: list[tuple[int, str]] = []
        for family, _socktype, _proto, _canonname, sockaddr in infos:
            if family not in {socket.AF_INET, socket.AF_INET6}:
                continue
            address_text = str(sockaddr[0]).split("%", 1)[0]
            try:
                address = ipaddress.ip_address(address_text)
            except ValueError as exc:
                raise NetworkPolicyError(f"DNS応答のIPアドレスが不正です: {address_text}") from exc
            if not address.is_global:
                raise NetworkPolicyError(
                    f"DNS応答に公開範囲外のIPアドレスが含まれます: {address}"
                )
            item = (family, str(address))
            if item not in targets:
                targets.append(item)
            if len(targets) > MAX_DNS_ADDRESSES:
                raise NetworkPolicyError("DNS応答のアドレス数が上限を超えました。")
        if not targets:
            raise NetworkPolicyError("DNS応答に接続可能な公開IPアドレスがありません。")
        return normalized, targets
    if not literal.is_global:
        raise NetworkPolicyError(f"公開範囲外のIPアドレスには接続できません: {literal}")
    family = socket.AF_INET6 if literal.version == 6 else socket.AF_INET
    return normalized, [(family, str(literal))]


def _pinned_connection(
    url: str,
    *,
    family: int,
    address: str,
    timeout: float,
) -> http.client.HTTPConnection:
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if parts.scheme == "https":
        connection: http.client.HTTPConnection = http.client.HTTPSConnection(
            host,
            port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
    else:
        connection = http.client.HTTPConnection(host, port, timeout=timeout)

    def create_connection(
        _destination: tuple[str, int],
        socket_timeout: float | object = timeout,
        source_address: tuple[str, int] | None = None,
    ) -> socket.socket:
        effective_timeout = timeout if not isinstance(socket_timeout, (int, float)) else socket_timeout
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.settimeout(float(effective_timeout))
            if source_address is not None:
                sock.bind(source_address)
            destination: tuple[Any, ...]
            if family == socket.AF_INET6:
                destination = (address, port, 0, 0)
            else:
                destination = (address, port)
            sock.connect(destination)
            return sock
        except Exception:
            sock.close()
            raise

    # Keep the logical hostname on the connection for Host, TLS SNI, and
    # certificate verification, but connect only to the address just checked.
    connection._create_connection = create_connection  # type: ignore[attr-defined]
    return connection


def _request_target(url: str) -> str:
    parts = urlsplit(url)
    path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    if parts.query:
        path += "?" + quote(parts.query, safe="=&?/:;+,%@!$'()*-._~")
    return path


def _request_once(
    url: str,
    *,
    targets: Sequence[tuple[int, str]],
    user_agent: str,
    accept: str,
    timeout: float,
    max_bytes: int,
) -> _HttpResult:
    last_error: Exception | None = None
    for family, address in targets:
        connection = _pinned_connection(
            url,
            family=family,
            address=address,
            timeout=timeout,
        )
        try:
            connection.request(
                "GET",
                _request_target(url),
                headers={"User-Agent": user_agent, "Accept": accept, "Connection": "close"},
            )
            response = connection.getresponse()
            content_length = response.getheader("Content-Length")
            if content_length is not None:
                try:
                    if int(content_length) > max_bytes:
                        raise EnrichmentError(f"response exceeded max_bytes={max_bytes}")
                except ValueError as exc:
                    raise EnrichmentError("Content-Lengthが不正です。") from exc
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise EnrichmentError(f"response exceeded max_bytes={max_bytes}")
            headers = {key.casefold(): value for key, value in response.getheaders()}
            return _HttpResult(url, int(response.status), headers, body)
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            last_error = exc
        finally:
            connection.close()
    if last_error is not None:
        raise OSError(f"公開サイトへ接続できません: {last_error}") from last_error
    raise OSError("公開サイトの接続先がありません。")


def _redirect_target(current_url: str, location: str, expected_host: str) -> str:
    if not location.strip():
        raise EnrichmentError("redirectにLocationがありません。")
    target = _policy_url(urljoin(current_url, location))
    if _verified_host(target) != expected_host:
        raise NetworkPolicyError("検証済み公式サイトと異なるhostへのredirectを拒否しました。")
    if urlsplit(current_url).scheme == "https" and urlsplit(target).scheme != "https":
        raise NetworkPolicyError("HTTPSからHTTPへのredirectを拒否しました。")
    return target


def _bounded_get(
    url: str,
    *,
    expected_host: str,
    user_agent: str,
    accept: str,
    timeout: float,
    max_bytes: int,
) -> _HttpResult:
    current = _policy_url(url)
    for redirect_count in range(MAX_REDIRECTS + 1):
        current, targets = _resolve_public_targets(current)
        if _verified_host(current) != expected_host:
            raise NetworkPolicyError("検証済み公式サイトと異なるhostへの接続を拒否しました。")
        result = _request_once(
            current,
            targets=targets,
            user_agent=user_agent,
            accept=accept,
            timeout=timeout,
            max_bytes=max_bytes,
        )
        if result.status not in {301, 302, 303, 307, 308}:
            return result
        if redirect_count >= MAX_REDIRECTS:
            raise EnrichmentError("redirect回数が上限を超えました。")
        current = _redirect_target(current, result.headers.get("location", ""), expected_host)
    raise AssertionError("redirect loop must return or raise")


def _robots_allowed(url: str, user_agent: str, timeout: float) -> tuple[bool, str]:
    parts = urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    parser = urllib.robotparser.RobotFileParser()
    parser.set_url(robots_url)
    try:
        result = _bounded_get(
            robots_url,
            expected_host=_verified_host(url),
            user_agent=user_agent,
            accept="text/plain,*/*;q=0.1",
            timeout=timeout,
            max_bytes=MAX_ROBOTS_BYTES,
        )
        if result.status == 404:
            parser.parse([])
        elif 200 <= result.status < 300:
            parser.parse(result.body.decode("utf-8", errors="replace").splitlines())
        else:
            return False, "unavailable"
        allowed = parser.can_fetch(user_agent, url)
        return allowed, "allowed" if allowed else "blocked"
    except NetworkPolicyError:
        raise
    except (EnrichmentError, OSError, ValueError):
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
    retrieved_at = _now()
    try:
        requested_url = _policy_url(page_url)
        expected_host = _verified_host(requested_url)
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
                    "robots.txt policy did not permit a fetch"
                    if robots_status == "blocked"
                    else "robots.txt unavailable",
                )
        else:
            robots_status = "not_checked"
        result = _bounded_get(
            requested_url,
            expected_host=expected_host,
            user_agent=user_agent,
            accept="text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
            timeout=timeout,
            max_bytes=max_bytes,
        )
        content_type = result.headers.get("content-type")
        if not 200 <= result.status < 300:
            return FetchedPage(
                requested_url,
                result.final_url,
                None,
                result.status,
                content_type,
                robots_status,
                retrieved_at,
                f"HTTP status {result.status}",
            )
        media_type = (content_type or "").split(";", 1)[0].strip().casefold()
        if media_type and media_type not in {"text/html", "application/xhtml+xml"}:
            return FetchedPage(
                requested_url,
                result.final_url,
                None,
                result.status,
                content_type,
                robots_status,
                retrieved_at,
                f"HTMLではないContent-Typeです: {media_type}",
            )
        charset_match = re.search(r"charset=([^;\s]+)", content_type or "", re.IGNORECASE)
        encoding = charset_match.group(1).strip(' \"\'') if charset_match else "utf-8"
        try:
            html = result.body.decode(encoding, errors="replace")
        except LookupError:
            html = result.body.decode("utf-8", errors="replace")
        return FetchedPage(
            requested_url,
            result.final_url,
            html,
            result.status,
            content_type,
            robots_status,
            retrieved_at,
        )
    except NetworkPolicyError as exc:
        return FetchedPage(
            str(page_url),
            None,
            None,
            None,
            None,
            "network_blocked",
            retrieved_at,
            str(exc),
        )
    except (EnrichmentError, OSError, ValueError) as exc:
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
                "state": "blocked_by_policy"
                if page.robots_status in {"blocked", "network_blocked"}
                else "needs_review",
                "error": page.error,
                "robots_status": page.robots_status,
                "policy_code": (
                    "robots_disallow"
                    if page.robots_status == "blocked"
                    else "network_destination_blocked"
                    if page.robots_status == "network_blocked"
                    else "fetch_failed"
                ),
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
    "NetworkPolicyError",
    "extract_contact_records",
    "fetch_and_extract_page",
    "fetch_official_page",
]
