from __future__ import annotations

"""Official-site discovery contracts.

Discovery consumes search-result metadata and emits unverified candidates.  It
never downloads the candidate site and never extracts contacts.  Verification
and site extraction are deliberately separate stages in ``enrichment_worker``.
"""

import ipaddress
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

from .enrichment import (
    DEFAULT_DB,
    DEFAULT_ENRICHMENT_DB,
    EnrichmentError,
    _local_sql,
    _require_corporate_number,
    import_enrichment_records,
    normalize_url,
)


VERIFICATION_METHODS = frozenset(
    {
        "manual_identity_review",
        "signed_provider_assertion",
        "accepted_public_record_same_host",
    }
)
MAX_DISCOVERY_FILE_BYTES = 256 * 1024**2
MAX_DISCOVERY_LINE_CHARS = 2 * 1024**2
MAX_HITS_PER_COMPANY = 100


class OfficialSiteDiscoveryProvider(Protocol):
    """Provider boundary for a search engine adapter.

    Implementations may call a licensed search API.  They return metadata
    only; fetching result URLs is outside this protocol.
    """

    name: str

    def search(self, identity: "CompanyIdentity", *, limit: int = 10) -> Sequence["SearchHit"]: ...


@dataclass(frozen=True)
class CompanyIdentity:
    corporate_number: str
    company_name: str
    prefecture_name: str | None = None
    city_name: str | None = None

    def __post_init__(self) -> None:
        _require_corporate_number(self.corporate_number)
        if not self.company_name.strip() or len(self.company_name) > 1_000:
            raise EnrichmentError("会社名は1〜1000文字で指定してください。")
        if any(
            value is not None and len(str(value)) > 256
            for value in (self.prefecture_name, self.city_name)
        ):
            raise EnrichmentError("所在地フィールドが長すぎます。")


@dataclass(frozen=True)
class SearchHit:
    url: str
    rank: int
    query: str
    title: str | None = None
    snippet: str | None = None
    confidence: float | None = None
    observed_at: str | None = None


def _observed_at(value: str | None) -> str:
    if value:
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise EnrichmentError(f"observed_atがISO-8601ではありません: {value}") from exc
        return value
    return datetime.now(timezone.utc).isoformat()


def validate_public_website_url(value: Any) -> str:
    """Return a normalized public Web URL suitable for later extraction.

    Search results remain untrusted candidates.  This stricter check is used
    only at the verification boundary so a reviewer cannot accidentally make
    a loopback/private literal, credential-bearing URL, or unusual service
    port claimable by the extraction worker.
    """

    text = str(value or "").strip()
    if text.startswith("//"):
        text = "https:" + text
    parts = urlsplit(text)
    try:
        port = parts.port
    except ValueError as exc:
        raise EnrichmentError(f"URLのポートが不正です: {value}") from exc
    if parts.username is not None or parts.password is not None:
        raise EnrichmentError("認証情報を含むURLは公式サイトとして検証できません。")
    if port not in {None, 80, 443}:
        raise EnrichmentError("公式サイトURLのポートは80または443だけを許可します。")

    host = (parts.hostname or "").strip(".").casefold()
    if host == "localhost" or host.endswith(
        (".localhost", ".local", ".internal", ".home", ".lan", ".onion")
    ):
        raise EnrichmentError("ローカルネットワーク名は公式サイトとして検証できません。")
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise EnrichmentError("公開範囲外のIPアドレスは公式サイトとして検証できません。")
    return normalize_url(text)


def candidate_records(
    identity: CompanyIdentity,
    hits: Iterable[SearchHit],
    *,
    provider: str,
) -> list[dict[str, Any]]:
    """Convert search hits into evidence-backed, review-only candidates."""

    provider = provider.strip().casefold()
    if not provider or len(provider) > 128 or any(char.isspace() for char in provider):
        raise EnrichmentError("providerは128文字以内の空白を含まない識別子で指定してください。")
    selected: dict[str, SearchHit] = {}
    for hit_index, hit in enumerate(hits, 1):
        if hit_index > MAX_HITS_PER_COMPANY:
            raise EnrichmentError(f"1法人の検索結果は{MAX_HITS_PER_COMPANY}件までです。")
        if hit.rank < 1:
            raise EnrichmentError("検索順位は1以上です。")
        if not hit.query.strip() or len(hit.query) > 2_000:
            raise EnrichmentError("検索クエリは1〜2000文字で指定してください。")
        if len(hit.url) > 8_192:
            raise EnrichmentError("検索結果URLが長すぎます。")
        if hit.title is not None and len(hit.title) > 2_000:
            raise EnrichmentError("検索結果titleが長すぎます。")
        if hit.snippet is not None and len(hit.snippet) > 16_000:
            raise EnrichmentError("検索結果snippetが長すぎます。")
        normalized = normalize_url(hit.url)
        previous = selected.get(normalized)
        if previous is None or (hit.rank, -(hit.confidence or 0.0)) < (
            previous.rank,
            -(previous.confidence or 0.0),
        ):
            selected[normalized] = hit

    records: list[dict[str, Any]] = []
    for normalized, hit in sorted(selected.items(), key=lambda item: (item[1].rank, item[0])):
        confidence = hit.confidence
        if confidence is not None and not 0.0 <= confidence <= 1.0:
            raise EnrichmentError("confidenceは0〜1です。")
        metadata = {
            "provider": provider,
            "query": hit.query,
            "rank": hit.rank,
            "title": hit.title,
            "snippet": hit.snippet,
        }
        records.append(
            {
                "kind": "website",
                "corporate_number": identity.corporate_number,
                "url": normalized,
                "website_role": "official_candidate",
                "discovery_method": "web_search",
                "status": "needs_review",
                "confidence": confidence,
                "source_key": f"web_search:{provider}",
                "source_url": normalized,
                "retrieved_at": _observed_at(hit.observed_at),
                "extractor_version": "website-discovery-v1",
                "policy_status": "review_required",
                "evidence_status": "candidate",
                "title": hit.title,
                "notes": json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                "pipeline_source_key": "official_site",
            }
        )
    return records


def iter_discovery_jsonl(path: Path) -> Iterator[tuple[CompanyIdentity, str, list[SearchHit]]]:
    """Read provider search output without performing any network activity."""

    path = Path(path)
    if not path.is_file():
        raise EnrichmentError(f"discovery JSONLがありません: {path}")
    if path.stat().st_size > MAX_DISCOVERY_FILE_BYTES:
        raise EnrichmentError(
            f"discovery JSONLが上限を超えています: {path.stat().st_size} > {MAX_DISCOVERY_FILE_BYTES}"
        )
    with path.open("r", encoding="utf-8") as handle:
        line_number = 0
        while line := handle.readline(MAX_DISCOVERY_LINE_CHARS + 1):
            line_number += 1
            if len(line) > MAX_DISCOVERY_LINE_CHARS:
                raise EnrichmentError(
                    f"discovery JSONL {line_number}行目が長さ上限を超えています。"
                )
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                if not isinstance(value, Mapping):
                    raise TypeError("各行はJSON objectである必要があります")
                raw_hits = value.get("hits", [])
                if not isinstance(raw_hits, list):
                    raise TypeError("hitsはJSON arrayである必要があります")
                if len(raw_hits) > MAX_HITS_PER_COMPANY:
                    raise TypeError(f"hitsは{MAX_HITS_PER_COMPANY}件までです")
                if any(not isinstance(item, Mapping) for item in raw_hits):
                    raise TypeError("hitsの各要素はJSON objectである必要があります")
                identity = CompanyIdentity(
                    corporate_number=str(value["corporate_number"]),
                    company_name=str(value["company_name"]),
                    prefecture_name=value.get("prefecture_name"),
                    city_name=value.get("city_name"),
                )
                provider = str(value["provider"])
                hits = [
                    SearchHit(
                        url=str(item["url"]),
                        rank=int(item["rank"]),
                        query=str(item["query"]),
                        title=item.get("title"),
                        snippet=item.get("snippet"),
                        confidence=(None if item.get("confidence") is None else float(item["confidence"])),
                        observed_at=item.get("observed_at"),
                    )
                    for item in raw_hits
                ]
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise EnrichmentError(f"discovery JSONL {line_number}行目が不正です: {exc}") from exc
            yield identity, provider, hits


def import_discovery_jsonl(
    database_path: Path = DEFAULT_DB,
    jsonl_path: Path | None = None,
    *,
    enrichment_path: Path = DEFAULT_ENRICHMENT_DB,
) -> dict[str, int]:
    if jsonl_path is None or not Path(jsonl_path).is_file():
        raise EnrichmentError(f"discovery JSONLがありません: {jsonl_path}")
    records = (
        record
        for identity, provider, hits in iter_discovery_jsonl(jsonl_path)
        for record in candidate_records(identity, hits, provider=provider)
    )
    return import_enrichment_records(database_path, records, enrichment_path=enrichment_path)


def verify_website_candidate(
    database_path: Path,
    *,
    enrichment_path: Path,
    corporate_number: str,
    url: str,
    verification_method: str,
    reviewer: str | None = None,
    identity_evidence: str | None = None,
    confidence: float = 1.0,
) -> dict[str, int]:
    """Promote an existing candidate after an explicit verification step."""

    corporate_number = _require_corporate_number(corporate_number)
    normalized = validate_public_website_url(url)
    verification_method = verification_method.strip()
    if verification_method not in VERIFICATION_METHODS:
        raise EnrichmentError(
            "verification_methodは定義済みの検証方法から選択してください: "
            + ", ".join(sorted(VERIFICATION_METHODS))
        )
    reviewer = str(reviewer or "").strip()
    if not reviewer or len(reviewer) > 128:
        raise EnrichmentError("reviewerは1〜128文字で指定してください。")
    identity_evidence = str(identity_evidence or "").strip()
    if not identity_evidence or len(identity_evidence) > 2_000:
        raise EnrichmentError("identity_evidenceは1〜2000文字で指定してください。")
    if not 0.0 <= confidence <= 1.0:
        raise EnrichmentError("confidenceは0〜1です。")
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover
        raise EnrichmentError("duckdb がありません。") from exc
    resolved_enrichment = Path(enrichment_path).resolve()
    if not resolved_enrichment.is_file():
        raise EnrichmentError(f"enrichment DuckDBがありません: {resolved_enrichment}")
    con = duckdb.connect(str(resolved_enrichment), read_only=True)
    try:
        candidates = con.execute(
            _local_sql(
                con,
                """
                SELECT normalized_url, url, source_evidence_id
                FROM enrichment.company_websites
                WHERE corporate_number = ?
                  AND website_role = 'official_candidate'
                  AND status IN ('found', 'needs_review', 'verified')
                """,
            ),
            [corporate_number],
        ).fetchall()
    finally:
        con.close()
    candidate_evidence_ids: set[str] = set()
    for stored_normalized, stored_url, source_evidence_id in candidates:
        for candidate in (stored_normalized, stored_url):
            try:
                if normalize_url(candidate) == normalized:
                    if source_evidence_id:
                        candidate_evidence_ids.add(str(source_evidence_id))
                    break
            except EnrichmentError:
                continue
    if not candidate_evidence_ids:
        raise EnrichmentError("検証対象のofficial_candidateがありません。先に発見結果を取り込んでください。")
    notes = {
        "verification_method": verification_method,
        "reviewer": reviewer,
        "identity_evidence": identity_evidence,
        "candidate_evidence_ids": sorted(candidate_evidence_ids),
    }
    return import_enrichment_records(
        database_path,
        [
            {
                "kind": "website",
                "corporate_number": corporate_number,
                "url": normalized,
                "website_role": "official_homepage",
                "discovery_method": verification_method,
                "status": "verified",
                "confidence": confidence,
                "source_key": "official_site_verification",
                "source_url": normalized,
                "policy_status": "allowed",
                "evidence_status": "verified",
                "extractor_version": "website-verification-v1",
                "notes": json.dumps(notes, ensure_ascii=False, separators=(",", ":")),
                "pipeline_source_key": "official_site",
            }
        ],
        enrichment_path=enrichment_path,
        _allow_verified_websites=True,
    )


__all__ = [
    "CompanyIdentity",
    "MAX_DISCOVERY_FILE_BYTES",
    "MAX_DISCOVERY_LINE_CHARS",
    "MAX_HITS_PER_COMPANY",
    "OfficialSiteDiscoveryProvider",
    "SearchHit",
    "VERIFICATION_METHODS",
    "candidate_records",
    "import_discovery_jsonl",
    "iter_discovery_jsonl",
    "validate_public_website_url",
    "verify_website_candidate",
]
