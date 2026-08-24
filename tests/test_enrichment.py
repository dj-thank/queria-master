from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from queria_master.enrichment import (
    EnrichmentError,
    _WriterLock,
    claim_enrichment_tasks,
    complete_enrichment_task,
    export_sales_ready_accounts,
    import_enrichment_records,
    initialize_database,
    review_contact,
    seed_enrichment,
)
from queria_master import enrichment_worker
from queria_master.website_discovery import (
    CompanyIdentity,
    SearchHit,
    candidate_records,
    verify_website_candidate,
)


def _make_canonical(path: Path) -> list[tuple[object, ...]]:
    con = duckdb.connect(str(path))
    try:
        con.execute("CREATE SCHEMA core")
        con.execute(
            """
            CREATE TABLE core.companies(
                corporate_number VARCHAR PRIMARY KEY,
                company_name VARCHAR,
                prefecture_name VARCHAR,
                city_name VARCHAR,
                full_address VARCHAR,
                jsic_codes_raw VARCHAR,
                business_summary VARCHAR,
                company_url VARCHAR
            )
            """
        )
        con.executemany(
            "INSERT INTO core.companies VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "1234567890123",
                    "株式会社テスト一",
                    "東京都",
                    "千代田区",
                    "東京都千代田区1-1",
                    "G37",
                    "ソフトウェア開発",
                    "https://example.jp/",
                ),
                (
                    "9876543210987",
                    "株式会社テスト二",
                    "大阪府",
                    "大阪市",
                    "大阪府大阪市2-2",
                    "G39",
                    "情報サービス",
                    None,
                ),
            ],
        )
        con.execute(
            "CREATE TABLE core.company_industries(corporate_number VARCHAR, jsic_major_code VARCHAR)"
        )
        con.executemany(
            "INSERT INTO core.company_industries VALUES (?, ?)",
            [("1234567890123", "G"), ("9876543210987", "E")],
        )
        return [tuple(row) for row in con.execute("SELECT * FROM core.companies ORDER BY corporate_number").fetchall()]
    finally:
        con.close()


def test_companion_layer_is_resumable_and_does_not_change_canonical(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical.duckdb"
    enrichment = tmp_path / "enrichment.duckdb"
    before = _make_canonical(canonical)

    initialized = initialize_database(canonical, enrichment)
    assert initialized["enrichment_database"] == str(enrichment.resolve())
    seeded = seed_enrichment(canonical, enrichment_path=enrichment)
    assert seeded["companies"] == 2
    assert seeded["states"] == 8
    assert seeded["canonical_websites"] == 1
    reseeded = seed_enrichment(canonical, enrichment_path=enrichment)
    assert reseeded["canonical_websites"] == 1
    assert reseeded["canonical_locations"] == 2
    con = duckdb.connect(str(enrichment), read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM enrichment.enrichment.evidence_documents").fetchone()[0] == 3
    finally:
        con.close()

    records = [
        {
            "kind": "contact",
            "corporate_number": "1234567890123",
            "contact_type": "phone",
            "value": "03-1234-5678",
            "source_key": "official_site_html",
            "source_url": "https://example.jp/contact",
            "sales_eligibility": "review",
            "verification_status": "page_explicit",
        },
        {
            "kind": "contact",
            "corporate_number": "1234567890123",
            "contact_type": "email",
            "value": "info@example.jp",
            "source_key": "official_site_html",
            "source_url": "https://example.jp/contact",
            "sales_eligibility": "review",
            "verification_status": "page_explicit",
        },
        {
            "kind": "contact",
            "corporate_number": "1234567890123",
            "contact_type": "form_url",
            "value": "https://example.jp/inquiry",
            "source_key": "official_site_html",
            "source_url": "https://example.jp/contact",
            "sales_eligibility": "review",
        },
        {
            "kind": "location",
            "corporate_number": "1234567890123",
            "address_raw": "東京都 千代田区 1-1",
            "prefecture_name": "東京都",
            "city_name": "千代田区",
            "source_key": "official_site_html",
            "source_url": "https://example.jp/company",
        },
        {
            "kind": "contact",
            "corporate_number": "9876543210987",
            "contact_type": "phone",
            "value": "06-9999-0000",
            "source_key": "public_candidate",
            "source_url": "https://candidate.example.jp",
            "sales_eligibility": "review",
        },
    ]
    first_import = import_enrichment_records(canonical, records, enrichment_path=enrichment)
    second_import = import_enrichment_records(canonical, records[:2], enrichment_path=enrichment)
    assert first_import == {"contact": 4, "location": 1}
    assert second_import == {"contact": 2}
    for contact_type, value in (("phone", "03-1234-5678"), ("email", "info@example.jp")):
        review_contact(
            canonical,
            enrichment_path=enrichment,
            corporate_number="1234567890123",
            contact_type=contact_type,
            value=value,
            decision="allowed",
            reviewer="test",
            reason="fixture evidence reviewed",
        )
    # A fresh observation updates evidence/status but must not erase an
    # explicit, audited sales-eligibility decision for the same contact.
    import_enrichment_records(canonical, records[:2], enrichment_path=enrichment)

    columns, rows = export_sales_ready_accounts(canonical, enrichment_path=enrichment)
    assert len(rows) == 1
    account = dict(zip(columns, rows[0]))
    assert account["corporate_number"] == "1234567890123"
    assert account["phone"] == "0312345678"
    assert account["email"] == "info@example.jp"
    assert account["address"] == "東京都千代田区1-1"

    canonical_sql = str(canonical).replace("'", "''")
    con = duckdb.connect(str(enrichment), read_only=True)
    try:
        con.execute(f"ATTACH '{canonical_sql}' AS canonical (READ_ONLY)")
        assert con.execute("SELECT count(*) FROM enrichment.enrichment.company_contact_points").fetchone()[0] == 4
        assert con.execute("SELECT count(*) FROM enrichment.enrichment.evidence_documents").fetchone()[0] == 8
        assert con.execute(
            "SELECT sales_state FROM enrichment.crm.v_enrichment_coverage "
            "WHERE corporate_number = '9876543210987'"
        ).fetchone()[0] != "ready"
    finally:
        con.execute("DETACH canonical")
        con.close()

    suppression = {
        "kind": "suppression",
        "corporate_number": "1234567890123",
        "suppression_type": "email",
        "value": "info@example.jp",
        "reason": "user_request",
        "source": "test",
        "source_url": "https://example.jp/contact",
    }
    import_enrichment_records(canonical, [suppression], enrichment_path=enrichment)
    _columns, contact_suppressed_rows = export_sales_ready_accounts(canonical, enrichment_path=enrichment)
    assert len(contact_suppressed_rows) == 1
    contact_suppressed = dict(zip(_columns, contact_suppressed_rows[0]))
    assert contact_suppressed["phone"] == "0312345678"
    assert contact_suppressed["email"] is None

    corporate_suppression = {
        "kind": "suppression",
        "suppression_type": "corporate_number",
        "value": "1234567890123",
        "reason": "user_request",
        "source": "test",
        "source_url": "https://example.jp/contact",
    }
    import_enrichment_records(canonical, [corporate_suppression], enrichment_path=enrichment)
    _columns, blocked_rows = export_sales_ready_accounts(canonical, enrichment_path=enrichment)
    assert blocked_rows == []

    claimed = claim_enrichment_tasks(
        canonical,
        enrichment_path=enrichment,
        worker_id="worker-test",
        field_name="website_verification",
        source_key="official_site",
        batch_size=1,
    )
    assert len(claimed) == 1
    assert claimed[0]["corporate_number"] == "1234567890123"
    second_claim = claim_enrichment_tasks(
        canonical,
        enrichment_path=enrichment,
        worker_id="another-worker",
        field_name="website_verification",
        source_key="official_site",
        batch_size=1,
    )
    assert second_claim == []
    assert claim_enrichment_tasks(
        canonical,
        enrichment_path=enrichment,
        worker_id="third-worker",
        field_name="website_verification",
        source_key="official_site",
        batch_size=1,
    ) == []
    with pytest.raises(EnrichmentError, match="古いworker結果"):
        complete_enrichment_task(
            canonical,
            enrichment_path=enrichment,
            corporate_number="1234567890123",
            field_name="website_verification",
            source_key="official_site",
            state="needs_review",
            worker_id="worker-test",
            lease_token="wrong-claim-token",
        )
    complete_enrichment_task(
        canonical,
        enrichment_path=enrichment,
        corporate_number="1234567890123",
        field_name="website_verification",
        source_key="official_site",
        state="needs_review",
        worker_id="worker-test",
        lease_token=claimed[0]["lease_token"],
    )

    con = duckdb.connect(str(enrichment), read_only=True)
    try:
        state = con.execute(
            """
            SELECT state, attempt_count, lease_owner
                FROM enrichment.enrichment.enrichment_state
                WHERE corporate_number = '1234567890123'
              AND field_name = 'website_verification' AND source_key = 'official_site'
            """
        ).fetchone()
        assert state == ("needs_review", 1, None)
    finally:
        con.close()

    canonical_con = duckdb.connect(str(canonical), read_only=True)
    try:
        after = [tuple(row) for row in canonical_con.execute("SELECT * FROM core.companies ORDER BY corporate_number").fetchall()]
        assert after == before
        schemas = {row[0] for row in canonical_con.execute("SELECT schema_name FROM information_schema.schemata").fetchall()}
        assert "enrichment" not in schemas
    finally:
        canonical_con.close()


def test_companion_writer_lock_is_exclusive(tmp_path: Path) -> None:
    database = tmp_path / "enrichment.duckdb"
    first = _WriterLock(database, timeout_seconds=0.1)
    second = _WriterLock(database, timeout_seconds=0.1)
    first.acquire()
    try:
        try:
            second.acquire()
        except EnrichmentError:
            pass
        else:
            raise AssertionError("second writer unexpectedly acquired the lock")
    finally:
        first.release()


def test_seed_can_limit_tasks_to_industry_major(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical.duckdb"
    enrichment = tmp_path / "queria_enrichment.duckdb"
    _make_canonical(canonical)

    seeded = seed_enrichment(canonical, enrichment_path=enrichment, industry_major="g")

    assert seeded["industry_major"] == "G"
    assert seeded["companies"] == 1
    # Discovery and verification are separate gates; one verified-page fetch
    # then fans out phone/email/form extraction as a single leased task.
    assert seeded["states"] == 4
    assert seeded["canonical_websites"] == 1


def test_worker_marks_missing_url_without_guessing(tmp_path: Path, monkeypatch) -> None:
    canonical = tmp_path / "canonical.duckdb"
    enrichment = tmp_path / "queria_enrichment.duckdb"
    _make_canonical(canonical)
    seed_enrichment(canonical, enrichment_path=enrichment)

    fetched: list[str] = []

    def fake_fetch(corporate_number: str, page_url: str, **kwargs):
        fetched.append(page_url)
        return [
            {
                "kind": "contact",
                "corporate_number": corporate_number,
                "contact_type": "phone",
                "value": "03-1234-5678",
                "source_key": kwargs["source_key"],
                "source_url": page_url,
                "sales_eligibility": "review",
                "verification_status": "test_page",
            }
        ]

    # A search result is only a candidate.  An explicit verification step is
    # required before the extraction queue becomes claimable.
    import_enrichment_records(
        canonical,
        candidate_records(
            CompanyIdentity("1234567890123", "株式会社テスト一"),
            [SearchHit("https://example.jp/", 1, "株式会社テスト一 公式")],
            provider="fixture",
        ),
        enrichment_path=enrichment,
    )
    verify_website_candidate(
        canonical,
        enrichment_path=enrichment,
        corporate_number="1234567890123",
        url="https://example.jp/",
        verification_method="manual_identity_review",
        reviewer="test",
        identity_evidence="fixture page states the legal company name and address",
    )

    monkeypatch.setattr(enrichment_worker, "fetch_and_extract_page", fake_fetch)
    result = enrichment_worker.run_enrichment_worker(
        canonical,
        enrichment_path=enrichment,
        worker_id="worker-test",
        field_name="contact_extraction",
        source_key="official_site",
        batch_size=2,
        max_tasks=2,
        interval_seconds=0,
        respect_robots=False,
    )
    assert result["claimed"] == 1
    assert result["found"] == 1
    assert result["not_found"] == 0
    assert fetched == ["https://example.jp/"]

    con = duckdb.connect(str(enrichment), read_only=True)
    try:
        rows = con.execute(
            """
            SELECT corporate_number, state
            FROM enrichment.enrichment_state
            WHERE field_name = 'contact_extraction' AND source_key = 'official_site'
            ORDER BY corporate_number
            """
        ).fetchall()
        assert rows == [("1234567890123", "found"), ("9876543210987", "waiting_for_dependency")]
    finally:
        con.close()


def test_v6_migration_requires_a_review_and_preserves_the_latest_decision(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical.duckdb"
    enrichment = tmp_path / "enrichment.duckdb"
    _make_canonical(canonical)
    initialize_database(canonical, enrichment)
    import_enrichment_records(
        canonical,
        [
            {
                "kind": "contact",
                "corporate_number": "1234567890123",
                "contact_type": "phone",
                "value": "03-1111-2222",
                "status": "verified",
                "source_key": "legacy",
                "source_url": "https://example.jp/contact",
                "sales_eligibility": "review",
            },
            {
                "kind": "contact",
                "corporate_number": "1234567890123",
                "contact_type": "phone",
                "value": "03-3333-4444",
                "status": "verified",
                "source_key": "reviewed",
                "source_url": "https://example.jp/contact",
                "sales_eligibility": "review",
            },
        ],
        enrichment_path=enrichment,
    )
    review_contact(
        canonical,
        enrichment_path=enrichment,
        corporate_number="1234567890123",
        contact_type="phone",
        value="03-3333-4444",
        decision="allowed",
        reviewer="migration-test",
        reason="first reviewed decision",
    )
    review_contact(
        canonical,
        enrichment_path=enrichment,
        corporate_number="1234567890123",
        contact_type="phone",
        value="03-3333-4444",
        decision="not_allowed",
        reviewer="migration-test",
        reason="latest reviewed decision",
    )

    con = duckdb.connect(str(enrichment))
    try:
        # Simulate a pre-v6 database: one privileged value has no review, while
        # the reviewed value has drifted away from its latest audit decision.
        con.execute(
            """
            UPDATE enrichment.enrichment.company_contact_points
            SET sales_eligibility = CASE
                WHEN value_normalized = '0311112222' THEN 'allowed'
                ELSE 'review'
            END
            """
        )
        con.execute(
            """
            UPDATE enrichment.enrichment.schema_meta
            SET schema_version = '5'
            WHERE schema_name = 'enrichment'
            """
        )
    finally:
        con.close()

    initialize_database(canonical, enrichment)
    check = duckdb.connect(str(enrichment), read_only=True)
    try:
        assert check.execute(
            """
            SELECT value_normalized, sales_eligibility
            FROM enrichment.enrichment.company_contact_points
            ORDER BY value_normalized
            """
        ).fetchall() == [
            ("0311112222", "review"),
            ("0333334444", "not_allowed"),
        ]
        assert check.execute(
            "SELECT count(*) FROM enrichment.enrichment.contact_reviews"
        ).fetchone()[0] == 2
        assert check.execute(
            "SELECT count(*) FROM enrichment.crm.v_resolved_company_contacts"
        ).fetchone()[0] == 0
    finally:
        check.close()
