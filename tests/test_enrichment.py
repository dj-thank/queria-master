from __future__ import annotations

from pathlib import Path

import duckdb

from queria_master.enrichment import (
    EnrichmentError,
    _WriterLock,
    claim_enrichment_tasks,
    complete_enrichment_task,
    export_sales_ready_accounts,
    import_enrichment_records,
    initialize_database,
    seed_enrichment,
)
from queria_master import enrichment_worker


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
    assert seeded["states"] == 10
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
            "sales_eligibility": "allowed",
            "verification_status": "page_explicit",
        },
        {
            "kind": "contact",
            "corporate_number": "1234567890123",
            "contact_type": "email",
            "value": "info@example.jp",
            "source_key": "official_site_html",
            "source_url": "https://example.jp/contact",
            "sales_eligibility": "allowed",
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
    ]
    first_import = import_enrichment_records(canonical, records, enrichment_path=enrichment)
    second_import = import_enrichment_records(canonical, records[:2], enrichment_path=enrichment)
    assert first_import == {"contact": 3, "location": 1}
    assert second_import == {"contact": 2}

    columns, rows = export_sales_ready_accounts(canonical, enrichment_path=enrichment)
    assert len(rows) == 1
    account = dict(zip(columns, rows[0]))
    assert account["corporate_number"] == "1234567890123"
    assert account["phone"] == "0312345678"
    assert account["email"] == "info@example.jp"
    assert account["address"] == "東京都千代田区1-1"

    con = duckdb.connect(str(enrichment), read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM enrichment.enrichment.company_contact_points").fetchone()[0] == 3
        assert con.execute("SELECT count(*) FROM enrichment.enrichment.evidence_documents").fetchone()[0] == 7
    finally:
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
        field_name="phone",
        source_key="official_site",
        batch_size=1,
    )
    assert len(claimed) == 1
    assert claimed[0]["corporate_number"] == "1234567890123"
    second_claim = claim_enrichment_tasks(
        canonical,
        enrichment_path=enrichment,
        worker_id="another-worker",
        field_name="phone",
        source_key="official_site",
        batch_size=1,
    )
    assert len(second_claim) == 1
    assert second_claim[0]["corporate_number"] == "9876543210987"
    assert claim_enrichment_tasks(
        canonical,
        enrichment_path=enrichment,
        worker_id="third-worker",
        field_name="phone",
        source_key="official_site",
        batch_size=1,
    ) == []
    complete_enrichment_task(
        canonical,
        enrichment_path=enrichment,
        corporate_number="1234567890123",
        field_name="phone",
        source_key="official_site",
        state="needs_review",
        worker_id="worker-test",
    )

    con = duckdb.connect(str(enrichment), read_only=True)
    try:
        state = con.execute(
            """
            SELECT state, attempt_count, lease_owner
                FROM enrichment.enrichment.enrichment_state
                WHERE corporate_number = '1234567890123'
              AND field_name = 'phone' AND source_key = 'official_site'
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


def test_worker_marks_missing_url_without_guessing(tmp_path: Path, monkeypatch) -> None:
    canonical = tmp_path / "canonical.duckdb"
    enrichment = tmp_path / "queria_enrichment.duckdb"
    _make_canonical(canonical)
    seed_enrichment(canonical, enrichment_path=enrichment)

    def fake_fetch(corporate_number: str, page_url: str, **kwargs):
        return [
            {
                "kind": "contact",
                "corporate_number": corporate_number,
                "contact_type": "phone",
                "value": "03-1234-5678",
                "source_key": kwargs["source_key"],
                "source_url": page_url,
                "sales_eligibility": "allowed",
                "verification_status": "test_page",
            }
        ]

    monkeypatch.setattr(enrichment_worker, "fetch_and_extract_page", fake_fetch)
    result = enrichment_worker.run_enrichment_worker(
        canonical,
        enrichment_path=enrichment,
        worker_id="worker-test",
        field_name="phone",
        source_key="official_site",
        batch_size=2,
        max_tasks=2,
        interval_seconds=0,
        respect_robots=False,
    )
    assert result["claimed"] == 2
    assert result["found"] == 1
    assert result["not_found"] == 1

    con = duckdb.connect(str(enrichment), read_only=True)
    try:
        rows = con.execute(
            """
            SELECT corporate_number, state
            FROM enrichment.enrichment_state
            WHERE field_name = 'phone' AND source_key = 'official_site'
            ORDER BY corporate_number
            """
        ).fetchall()
        assert rows == [("1234567890123", "found"), ("9876543210987", "not_found_after_policy")]
    finally:
        con.close()
