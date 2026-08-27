from __future__ import annotations

import socket
import sqlite3
from pathlib import Path

import duckdb
import pytest

from queria_master import enrichment_extract, enrichment_worker
from queria_master.enrichment import (
    EnrichmentError,
    claim_enrichment_tasks,
    export_sales_ready_accounts,
    import_enrichment_records,
    initialize_database,
    review_contact,
    seed_enrichment,
)
from queria_master.public_enrichment_bridge import (
    PublicEnrichmentBridgeError,
    integrate_public_enrichment,
)
from queria_master.publish import publish_runtime_bundle
from queria_master.search_index import SearchIndex
from queria_master.website_discovery import (
    CompanyIdentity,
    SearchHit,
    candidate_records,
    validate_public_website_url,
    verify_website_candidate,
)


def _canonical(path: Path) -> None:
    con = duckdb.connect(str(path))
    try:
        con.execute("CREATE SCHEMA core")
        con.execute("CREATE SCHEMA meta")
        con.execute(
            """
            CREATE TABLE core.companies AS SELECT * FROM (VALUES
                ('1000000000001', '発見株式会社', '東京都', '港区', '東京都港区1-1',
                 '1050000', NULL::VARCHAR, 'G|G39', 'ソフトウェア'),
                ('1000000000002', 'レビュー株式会社', '大阪府', '大阪市', '大阪府大阪市2-2',
                 '5300000', NULL::VARCHAR, 'G|G39', '情報サービス')
            ) AS t(corporate_number, company_name, prefecture_name, city_name,
                   full_address, post_code, company_url, jsic_codes_raw, business_summary)
            """
        )
        con.execute("CREATE TABLE meta.refresh_log AS SELECT 'fixture' refresh_id, 'test' AS \"scope\"")
    finally:
        con.close()


def test_candidate_verify_extract_and_publish_are_distinct_stages(tmp_path: Path, monkeypatch) -> None:
    canonical = tmp_path / "canonical.duckdb"
    enrichment = tmp_path / "enrichment.duckdb"
    runtime = tmp_path / "runtime.duckdb"
    index = tmp_path / "search.sqlite"
    _canonical(canonical)
    initialize_database(canonical, enrichment)
    seed_enrichment(canonical, enrichment_path=enrichment)

    records = candidate_records(
        CompanyIdentity("1000000000001", "発見株式会社", "東京都", "港区"),
        [
            SearchHit("https://found.example.jp", 2, "発見株式会社 公式", confidence=0.7),
            SearchHit("https://found.example.jp/", 1, "発見株式会社", confidence=0.8),
        ],
        provider="fixture",
    )
    assert len(records) == 1
    assert records[0]["website_role"] == "official_candidate"
    assert records[0]["status"] == "needs_review"
    import_enrichment_records(canonical, records, enrichment_path=enrichment)

    before_runtime = tmp_path / "before.duckdb"
    from queria_master.runtime import build_runtime_database

    build_runtime_database(canonical, enrichment, before_runtime, threads=1, memory_limit="1GB")
    con = duckdb.connect(str(before_runtime), read_only=True)
    try:
        assert con.execute(
            "SELECT effective_company_url FROM search.company_documents WHERE corporate_number='1000000000001'"
        ).fetchone()[0] is None
    finally:
        con.close()

    verify_website_candidate(
        canonical,
        enrichment_path=enrichment,
        corporate_number="1000000000001",
        url="https://found.example.jp/",
        verification_method="manual_identity_review",
        reviewer="qa",
        identity_evidence="fixture legal name and registered address match",
    )

    calls: list[str] = []

    def fake_fetch(corporate_number: str, page_url: str, **kwargs):
        calls.append(page_url)
        return [
            {
                "kind": "contact",
                "corporate_number": corporate_number,
                "contact_type": "phone",
                "value": "03-1234-5678",
                "status": "verified",
                "source_key": kwargs["source_key"],
                "source_url": page_url,
                "confidence": 0.95,
                "verification_status": "fixture_page",
                "sales_eligibility": "review",
            }
        ]

    monkeypatch.setattr(enrichment_worker, "fetch_and_extract_page", fake_fetch)
    result = enrichment_worker.run_enrichment_worker(
        canonical,
        enrichment_path=enrichment,
        worker_id="fixture-worker",
        batch_size=10,
        max_tasks=10,
        interval_seconds=0,
    )
    assert result["claimed"] == 1
    assert result["found"] == 1
    assert calls == ["https://found.example.jp/"]

    review_contact(
        canonical,
        enrichment_path=enrichment,
        corporate_number="1000000000001",
        contact_type="phone",
        value="03-1234-5678",
        decision="allowed",
        reviewer="qa",
        reason="fixture official-page evidence reviewed",
    )

    receipt = publish_runtime_bundle(
        canonical,
        enrichment_path=enrichment,
        runtime_path=runtime,
        search_index_path=index,
        threads=1,
        memory_limit="1GB",
        batch_size=10,
    )
    assert receipt["generation_id"]
    con = duckdb.connect(str(runtime), read_only=True)
    try:
        website, phone = con.execute(
            "SELECT effective_company_url, phone FROM search.company_documents WHERE corporate_number='1000000000001'"
        ).fetchone()
        assert website == "https://found.example.jp/"
        assert phone == "0312345678"
    finally:
        con.close()
    with SearchIndex(index, database_path=runtime) as search:
        assert search.metadata["runtime_generation_id"] == receipt["generation_id"]
    sqlite = sqlite3.connect(index)
    try:
        assert sqlite.execute(
            "SELECT company_url, phone FROM company_docs WHERE corporate_number='1000000000001'"
        ).fetchone() == ("https://found.example.jp/", "0312345678")
    finally:
        sqlite.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://service.internal/",
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "https://user:secret@example.jp/",
        "https://example.jp:8443/",
    ],
)
def test_verified_website_url_rejects_non_public_destinations(url: str) -> None:
    with pytest.raises(EnrichmentError):
        validate_public_website_url(url)


def test_verified_website_url_normalizes_public_host() -> None:
    assert validate_public_website_url("HTTPS://例.jp/path/#fragment") == "https://xn--fsq.jp/path"


def test_generic_importer_cannot_promote_privileged_facts(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical.duckdb"
    enrichment = tmp_path / "enrichment.duckdb"
    _canonical(canonical)
    initialize_database(canonical, enrichment)

    with pytest.raises(EnrichmentError, match="official_homepage"):
        import_enrichment_records(
            canonical,
            [
                {
                    "kind": "website",
                    "corporate_number": "1000000000001",
                    "url": "https://unreviewed.example.jp/",
                    "website_role": "official_homepage",
                    "status": "verified",
                    "source_key": "untrusted_jsonl",
                    "source_url": "https://unreviewed.example.jp/",
                }
            ],
            enrichment_path=enrichment,
        )
    with pytest.raises(EnrichmentError, match="review-contact"):
        import_enrichment_records(
            canonical,
            [
                {
                    "kind": "contact",
                    "corporate_number": "1000000000001",
                    "contact_type": "phone",
                    "value": "03-0000-0000",
                    "sales_eligibility": "allowed",
                    "source_key": "untrusted_jsonl",
                    "source_url": "https://unreviewed.example.jp/contact",
                }
            ],
            enrichment_path=enrichment,
        )
    check = duckdb.connect(str(enrichment), read_only=True)
    try:
        assert check.execute(
            "SELECT count(*) FROM enrichment.enrichment.company_websites"
        ).fetchone()[0] == 0
        assert check.execute(
            "SELECT count(*) FROM enrichment.enrichment.company_contact_points"
        ).fetchone()[0] == 0
    finally:
        check.close()


@pytest.mark.parametrize(
    ("reviewer", "identity_evidence", "message"),
    [
        ("", "legal identity matched", "reviewer"),
        ("qa", "", "identity_evidence"),
    ],
)
def test_website_verification_requires_auditable_identity_evidence(
    tmp_path: Path,
    reviewer: str,
    identity_evidence: str,
    message: str,
) -> None:
    canonical = tmp_path / "canonical.duckdb"
    enrichment = tmp_path / "enrichment.duckdb"
    _canonical(canonical)
    initialize_database(canonical, enrichment)
    import_enrichment_records(
        canonical,
        candidate_records(
            CompanyIdentity("1000000000001", "発見株式会社"),
            [SearchHit("https://candidate.example.jp/", 1, "発見株式会社 公式")],
            provider="fixture",
        ),
        enrichment_path=enrichment,
    )

    with pytest.raises(EnrichmentError, match=message):
        verify_website_candidate(
            canonical,
            enrichment_path=enrichment,
            corporate_number="1000000000001",
            url="https://candidate.example.jp/",
            verification_method="manual_identity_review",
            reviewer=reviewer,
            identity_evidence=identity_evidence,
        )


def test_fetch_resolution_rejects_mixed_public_private_dns(monkeypatch) -> None:
    def mixed_answers(*args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443)),
        ]

    monkeypatch.setattr(enrichment_extract.socket, "getaddrinfo", mixed_answers)
    with pytest.raises(enrichment_extract.NetworkPolicyError, match="公開範囲外"):
        enrichment_extract._resolve_public_targets("https://example.jp/")


def test_redirect_policy_keeps_verified_host_and_https() -> None:
    assert enrichment_extract._redirect_target(
        "http://example.jp/start",
        "https://www.example.jp/next",
        "example.jp",
    ) == "https://www.example.jp/next"
    with pytest.raises(enrichment_extract.NetworkPolicyError, match="異なるhost"):
        enrichment_extract._redirect_target(
            "https://example.jp/start",
            "https://evil.example/next",
            "example.jp",
        )
    with pytest.raises(enrichment_extract.NetworkPolicyError, match="HTTPSからHTTP"):
        enrichment_extract._redirect_target(
            "https://example.jp/start",
            "http://example.jp/next",
            "example.jp",
        )


def test_fetch_rejects_private_literal_without_network() -> None:
    page = enrichment_extract.fetch_official_page(
        "http://127.0.0.1/",
        respect_robots=False,
    )
    assert page.html is None
    assert page.robots_status == "network_blocked"


def test_verifier_accepts_legacy_root_candidate_normalization(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical.duckdb"
    enrichment = tmp_path / "enrichment.duckdb"
    _canonical(canonical)
    initialize_database(canonical, enrichment)
    seed_enrichment(canonical, enrichment_path=enrichment)

    con = duckdb.connect(str(enrichment))
    try:
        con.execute(
            """
            INSERT INTO enrichment.enrichment.evidence_documents(
                evidence_id, corporate_number, source_key, source_url, retrieved_at,
                policy_status, evidence_status
            ) VALUES ('legacy-evidence', '1000000000001', 'legacy',
                      'https://legacy.example.jp/', current_timestamp,
                      'review_required', 'candidate')
            """
        )
        con.execute(
            """
            INSERT INTO enrichment.enrichment.company_websites(
                website_id, corporate_number, url, normalized_url, website_role,
                discovery_method, source_evidence_id, status, first_seen_at
            ) VALUES ('legacy-root', '1000000000001', 'https://legacy.example.jp/',
                      'https://legacy.example.jp', 'official_candidate', 'legacy',
                      'legacy-evidence', 'found', current_timestamp)
            """
        )
    finally:
        con.close()

    verify_website_candidate(
        canonical,
        enrichment_path=enrichment,
        corporate_number="1000000000001",
        url="https://legacy.example.jp/",
        verification_method="manual_identity_review",
        reviewer="qa",
        identity_evidence="legacy fixture legal identity match",
    )
    check = duckdb.connect(str(enrichment), read_only=True)
    try:
        assert check.execute(
            """
            SELECT count(*)
            FROM enrichment.enrichment.company_websites
            WHERE corporate_number = '1000000000001'
              AND normalized_url = 'https://legacy.example.jp/'
              AND website_role = 'official_homepage'
              AND status = 'verified'
            """
        ).fetchone()[0] == 1
    finally:
        check.close()

def test_public_sqlite_is_staging_and_only_accepted_matches_cross_bridge(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical.duckdb"
    enrichment = tmp_path / "enrichment.duckdb"
    staging = tmp_path / "public.sqlite3"
    _canonical(canonical)
    initialize_database(canonical, enrichment)
    seed_enrichment(canonical, enrichment_path=enrichment)

    con = sqlite3.connect(staging)
    try:
        con.executescript(
            """
            CREATE TABLE corporate_matches(
                source_id TEXT, corporate_number TEXT, status TEXT, confidence REAL,
                source_name TEXT, matched_at TEXT
            );
            CREATE TABLE public_master(
                corporate_number TEXT, website_url TEXT, address TEXT, postal_code TEXT,
                source_org TEXT, source_file TEXT, acquired_at TEXT, updated_at TEXT
            );
            CREATE TABLE site_contacts(
                source_id TEXT, corporate_number TEXT, website_url TEXT, phone TEXT, evidence_url TEXT,
                evidence_text TEXT, confidence REAL, fetched_at TEXT, source_file TEXT
            );
            CREATE TABLE source_audit(source_file TEXT, sha256 TEXT);
            """
        )
        con.executemany(
            "INSERT INTO corporate_matches VALUES(?,?,?,?,?,?)",
            [
                ("accepted-1", "1000000000001", "accepted", 1.0, "fixture", "2026-08-24T00:00:00Z"),
                ("accepted-bad", "1000000000001", "accepted", 1.0, "fixture", "2026-08-24T00:00:00Z"),
                ("review-2", "1000000000002", "review", 0.5, "fixture", "2026-08-24T00:00:00Z"),
            ],
        )
        con.executemany(
            "INSERT INTO public_master VALUES(?,?,?,?,?,?,?,?)",
            [
                ("1000000000001", "https://accepted.example.jp/", "東京都港区3-3", "1050003", "gov", "accepted.csv", "2026-08-24T00:00:00Z", "2026-08-24T00:00:00Z"),
                ("1000000000001", None, "東京都港区5-5", "1050005", "gov", "accepted.csv", "2026-08-24T01:00:00Z", "2026-08-24T01:00:00Z"),
                ("1000000000002", "https://review.example.jp/", "大阪府大阪市4-4", "5300004", "gov", "review.csv", "2026-08-24T00:00:00Z", "2026-08-24T00:00:00Z"),
            ],
        )
        con.executemany(
            "INSERT INTO site_contacts VALUES(?,?,?,?,?,?,?,?,?)",
            [
                ("accepted-1", "1000000000001", "https://accepted.example.jp/", "03-1111-2222", "https://accepted.example.jp/contact", "代表電話", 0.95, "2026-08-24T00:00:00Z", "accepted.csv"),
                ("accepted-bad", "1000000000001", "https://accepted.example.jp/", "03-9999-9999", "https://evil.example/contact", "不一致host", 0.99, "2026-08-24T00:00:00Z", "accepted.csv"),
                ("review-2", "1000000000002", "https://review.example.jp/", "06-1111-2222", "https://review.example.jp/contact", "代表電話", 0.8, "2026-08-24T00:00:00Z", "review.csv"),
            ],
        )
        con.executemany(
            "INSERT INTO source_audit VALUES(?,?)",
            [("accepted.csv", "a" * 64), ("review.csv", "b" * 64)],
        )
        con.commit()
    finally:
        con.close()

    result = integrate_public_enrichment(
        staging,
        canonical,
        enrichment_path=enrichment,
        publish=False,
    )
    assert result["staging"]["accepted_public_rows"] == 2
    assert result["staging"]["accepted_contact_rows"] == 2
    assert result["staging"]["verified_contact_rows"] == 1
    assert result["staging"]["rejected_contact_rows"] == 1
    assert result["published"] is None

    check = duckdb.connect(str(enrichment), read_only=True)
    try:
        rows = check.execute(
            "SELECT corporate_number, normalized_url, status FROM enrichment.enrichment.company_websites ORDER BY corporate_number, website_role"
        ).fetchall()
        assert all(row[0] == "1000000000001" for row in rows)
        assert ("1000000000001", "https://accepted.example.jp/", "verified") in rows
        phone = check.execute(
            "SELECT value_normalized, status, sales_eligibility FROM enrichment.enrichment.company_contact_points"
        ).fetchone()
        assert phone == ("0311112222", "verified", "review")
        notes = [row[0] or "" for row in check.execute(
            "SELECT notes FROM enrichment.enrichment.evidence_documents"
        ).fetchall()]
        assert any("a" * 64 in note for note in notes)
        source_urls = [row[0] for row in check.execute(
            "SELECT source_url FROM enrichment.enrichment.evidence_documents"
        ).fetchall()]
        assert "staging://public-enrichment/accepted.csv" in source_urls
    finally:
        check.close()

    review = review_contact(
        canonical,
        enrichment_path=enrichment,
        corporate_number="1000000000001",
        contact_type="phone",
        value="03-1111-2222",
        decision="allowed",
        reviewer="qa",
        reason="accepted corporate match and same-host evidence reviewed",
    )
    assert review["previous_sales_eligibility"] == "review"
    runtime = tmp_path / "bridge-runtime.duckdb"
    from queria_master.runtime import build_runtime_database

    build_runtime_database(canonical, enrichment, runtime, threads=1, memory_limit="1GB")
    runtime_check = duckdb.connect(str(runtime), read_only=True)
    try:
        assert runtime_check.execute(
            """
            SELECT phone FROM search.company_documents
            WHERE corporate_number = '1000000000001'
            """
        ).fetchone()[0] == "0311112222"
        assert runtime_check.execute(
            "SELECT count(*) FROM enrichment.contact_reviews"
        ).fetchone()[0] == 1
    finally:
        runtime_check.close()


def test_public_bridge_reads_committed_rows_from_an_active_wal(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical.duckdb"
    enrichment = tmp_path / "enrichment.duckdb"
    staging = tmp_path / "public.sqlite3"
    _canonical(canonical)
    initialize_database(canonical, enrichment)

    writer = sqlite3.connect(staging)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0].casefold() == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.executescript(
            """
            CREATE TABLE corporate_matches(
                source_id TEXT, corporate_number TEXT, status TEXT, confidence REAL,
                source_name TEXT, matched_at TEXT
            );
            CREATE TABLE public_master(
                corporate_number TEXT, website_url TEXT, address TEXT, postal_code TEXT,
                source_org TEXT, source_file TEXT, acquired_at TEXT, updated_at TEXT
            );
            CREATE TABLE site_contacts(
                source_id TEXT, corporate_number TEXT, website_url TEXT, phone TEXT,
                evidence_url TEXT, evidence_text TEXT, confidence REAL,
                fetched_at TEXT, source_file TEXT
            );
            CREATE TABLE source_audit(source_file TEXT, sha256 TEXT);
            INSERT INTO corporate_matches VALUES(
                'wal-accepted', '1000000000001', 'accepted', 1.0, 'wal-fixture',
                '2026-08-24T00:00:00Z'
            );
            INSERT INTO public_master VALUES(
                '1000000000001', NULL, '東京都港区WAL1-1', '1050001', 'gov',
                'wal-source.csv', '2026-08-24T00:00:00Z', '2026-08-24T00:00:00Z'
            );
            INSERT INTO source_audit VALUES(
                'wal-source.csv',
                'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc'
            );
            """
        )
        writer.commit()
        wal_path = Path(str(staging) + "-wal")
        assert wal_path.is_file()
        assert wal_path.stat().st_size > 0

        result = integrate_public_enrichment(
            staging,
            canonical,
            enrichment_path=enrichment,
            publish=False,
        )
        assert result["staging"]["accepted_public_rows"] == 1

        check = duckdb.connect(str(enrichment), read_only=True)
        try:
            assert check.execute(
                """
                SELECT address_raw
                FROM enrichment.enrichment.company_locations
                WHERE corporate_number = '1000000000001'
                """
            ).fetchone()[0] == "東京都港区WAL1-1"
        finally:
            check.close()
    finally:
        writer.close()


def test_public_bridge_rejects_a_malformed_latest_source_audit(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical.duckdb"
    enrichment = tmp_path / "enrichment.duckdb"
    staging = tmp_path / "public.sqlite3"
    _canonical(canonical)
    initialize_database(canonical, enrichment)
    con = sqlite3.connect(staging)
    try:
        con.executescript(
            """
            CREATE TABLE corporate_matches(
                source_id TEXT, corporate_number TEXT, status TEXT, confidence REAL,
                source_name TEXT, matched_at TEXT
            );
            CREATE TABLE public_master(
                corporate_number TEXT, website_url TEXT, address TEXT, postal_code TEXT,
                source_org TEXT, source_file TEXT, acquired_at TEXT, updated_at TEXT
            );
            CREATE TABLE site_contacts(
                source_id TEXT, corporate_number TEXT, website_url TEXT, phone TEXT,
                evidence_url TEXT, evidence_text TEXT, confidence REAL,
                fetched_at TEXT, source_file TEXT
            );
            CREATE TABLE source_audit(source_file TEXT, sha256 TEXT);
            INSERT INTO corporate_matches VALUES(
                'accepted-1', '1000000000001', 'accepted', 1.0, 'fixture',
                '2026-08-24T00:00:00Z'
            );
            INSERT INTO public_master VALUES(
                '1000000000001', NULL, '東京都港区', '1050000', 'gov',
                'source.csv', '2026-08-24T00:00:00Z', '2026-08-24T00:00:00Z'
            );
            INSERT INTO source_audit VALUES('source.csv', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
            INSERT INTO source_audit VALUES('source.csv', 'invalid-latest-hash');
            """
        )
        con.commit()
    finally:
        con.close()

    with pytest.raises(PublicEnrichmentBridgeError, match="SHA-256"):
        integrate_public_enrichment(
            staging,
            canonical,
            enrichment_path=enrichment,
            publish=False,
        )


def test_sales_and_runtime_use_the_same_deterministic_contact_resolver(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical.duckdb"
    enrichment = tmp_path / "enrichment.duckdb"
    runtime = tmp_path / "runtime.duckdb"
    _canonical(canonical)
    initialize_database(canonical, enrichment)
    seed_enrichment(canonical, enrichment_path=enrichment)
    import_enrichment_records(
        canonical,
        [
            {
                "kind": "contact",
                "corporate_number": "1000000000001",
                "contact_type": "phone",
                "value": "03-9999-9999",
                "status": "found",
                "confidence": 1.0,
                "sales_eligibility": "review",
                "source_key": "fixture",
                "source_url": "https://found.example.jp/contact",
                "retrieved_at": "2026-08-24T02:00:00Z",
            },
            {
                "kind": "contact",
                "corporate_number": "1000000000001",
                "contact_type": "phone",
                "value": "03-1111-1111",
                "status": "verified",
                "confidence": 0.2,
                "sales_eligibility": "review",
                "source_key": "fixture",
                "source_url": "https://verified.example.jp/contact",
                "retrieved_at": "2026-08-24T01:00:00Z",
            },
        ],
        enrichment_path=enrichment,
    )
    for value in ("03-9999-9999", "03-1111-1111"):
        review_contact(
            canonical,
            enrichment_path=enrichment,
            corporate_number="1000000000001",
            contact_type="phone",
            value=value,
            decision="allowed",
            reviewer="qa",
            reason="fixture resolver input reviewed",
        )
    columns, rows = export_sales_ready_accounts(canonical, enrichment_path=enrichment)
    sales = dict(zip(columns, rows[0]))
    assert sales["phone"] == "0311111111"

    from queria_master.runtime import build_runtime_database

    build_runtime_database(canonical, enrichment, runtime, threads=1, memory_limit="1GB")
    con = duckdb.connect(str(runtime), read_only=True)
    try:
        assert con.execute(
            "SELECT phone FROM search.company_documents WHERE corporate_number='1000000000001'"
        ).fetchone()[0] == sales["phone"]
    finally:
        con.close()


def test_publish_failure_keeps_the_previous_runtime_and_index(tmp_path: Path, monkeypatch) -> None:
    canonical = tmp_path / "canonical.duckdb"
    enrichment = tmp_path / "enrichment.duckdb"
    runtime = tmp_path / "runtime.duckdb"
    index = tmp_path / "search.sqlite"
    _canonical(canonical)
    initialize_database(canonical, enrichment)
    seed_enrichment(canonical, enrichment_path=enrichment)
    runtime.write_bytes(b"previous-runtime")
    index.write_bytes(b"previous-index")

    from queria_master import publish

    def fail_index(*args, **kwargs):
        raise RuntimeError("fixture index failure")

    monkeypatch.setattr(publish, "build_search_index", fail_index)
    with pytest.raises(RuntimeError, match="fixture index failure"):
        publish_runtime_bundle(
            canonical,
            enrichment_path=enrichment,
            runtime_path=runtime,
            search_index_path=index,
            threads=1,
            memory_limit="1GB",
        )
    assert runtime.read_bytes() == b"previous-runtime"
    assert index.read_bytes() == b"previous-index"


def test_publish_rejects_any_input_output_path_collision(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical.duckdb"
    enrichment = tmp_path / "enrichment.duckdb"
    index = tmp_path / "search.sqlite"
    _canonical(canonical)
    initialize_database(canonical, enrichment)
    original = canonical.read_bytes()

    from queria_master.publish import PublishError

    with pytest.raises(PublishError, match="すべて別ファイル"):
        publish_runtime_bundle(
            canonical,
            enrichment_path=enrichment,
            runtime_path=canonical,
            search_index_path=index,
        )
    assert canonical.read_bytes() == original


def test_publish_second_replace_failure_rolls_back_runtime(tmp_path: Path, monkeypatch) -> None:
    canonical = tmp_path / "canonical.duckdb"
    enrichment = tmp_path / "enrichment.duckdb"
    runtime = tmp_path / "runtime.duckdb"
    index = tmp_path / "search.sqlite"
    _canonical(canonical)
    initialize_database(canonical, enrichment)
    publish_runtime_bundle(
        canonical,
        enrichment_path=enrichment,
        runtime_path=runtime,
        search_index_path=index,
        threads=1,
        memory_limit="1GB",
        batch_size=10,
    )
    runtime_before = runtime.read_bytes()
    index_before = index.read_bytes()

    from queria_master import publish

    real_replace = publish.os.replace

    def fail_index_promotion(source, destination):
        if Path(destination) == index and Path(source) != index:
            raise OSError("fixture index promotion failure")
        return real_replace(source, destination)

    monkeypatch.setattr(publish.os, "replace", fail_index_promotion)
    with pytest.raises(publish.PublishError, match="rollback"):
        publish_runtime_bundle(
            canonical,
            enrichment_path=enrichment,
            runtime_path=runtime,
            search_index_path=index,
            threads=1,
            memory_limit="1GB",
            batch_size=10,
        )
    assert runtime.read_bytes() == runtime_before
    assert index.read_bytes() == index_before


def test_expired_worker_cannot_overwrite_a_reclaimed_lease(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical.duckdb"
    enrichment = tmp_path / "enrichment.duckdb"
    _canonical(canonical)
    initialize_database(canonical, enrichment)
    seed_enrichment(canonical, enrichment_path=enrichment)
    candidates = candidate_records(
        CompanyIdentity("1000000000001", "発見株式会社"),
        [SearchHit("https://lease.example.jp/", 1, "発見株式会社 公式")],
        provider="fixture",
    )
    import_enrichment_records(canonical, candidates, enrichment_path=enrichment)
    verify_website_candidate(
        canonical,
        enrichment_path=enrichment,
        corporate_number="1000000000001",
        url="https://lease.example.jp/",
        verification_method="manual_identity_review",
        reviewer="qa",
        identity_evidence="fixture legal identity match",
    )
    old_claim = claim_enrichment_tasks(
        canonical,
        enrichment_path=enrichment,
        worker_id="reused-worker-id",
        field_name="contact_extraction",
        batch_size=1,
    )
    assert len(old_claim) == 1
    con = duckdb.connect(str(enrichment))
    try:
        con.execute(
            """
            UPDATE enrichment.enrichment.enrichment_state
            SET lease_until = current_timestamp - INTERVAL 1 SECOND
            WHERE corporate_number = '1000000000001'
              AND field_name = 'contact_extraction'
            """
        )
    finally:
        con.close()
    new_claim = claim_enrichment_tasks(
        canonical,
        enrichment_path=enrichment,
        worker_id="reused-worker-id",
        field_name="contact_extraction",
        batch_size=1,
    )
    assert len(new_claim) == 1
    assert new_claim[0]["lease_token"] != old_claim[0]["lease_token"]

    with pytest.raises(EnrichmentError, match="古いworker結果"):
        import_enrichment_records(
            canonical,
            [
                {
                    "kind": "contact",
                    "corporate_number": "1000000000001",
                    "contact_type": "phone",
                    "value": "03-7777-7777",
                    "status": "verified",
                    "source_key": "official_site",
                    "source_url": "https://lease.example.jp/",
                    "sales_eligibility": "review",
                },
                {
                    "kind": "state",
                    "corporate_number": "1000000000001",
                    "field_name": "contact_extraction",
                    "source_key": "official_site",
                    "state": "found",
                    "lease_owner": "reused-worker-id",
                    "lease_token": old_claim[0]["lease_token"],
                },
            ],
            enrichment_path=enrichment,
        )
    check = duckdb.connect(str(enrichment), read_only=True)
    try:
        assert check.execute(
            "SELECT count(*) FROM enrichment.enrichment.company_contact_points"
        ).fetchone()[0] == 0
        assert check.execute(
            """
            SELECT state, lease_owner, lease_token
            FROM enrichment.enrichment.enrichment_state
            WHERE corporate_number = '1000000000001'
              AND field_name = 'contact_extraction'
            """
        ).fetchone() == (
            "leased",
            "reused-worker-id",
            new_claim[0]["lease_token"],
        )
    finally:
        check.close()
