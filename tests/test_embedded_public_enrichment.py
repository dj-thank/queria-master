from __future__ import annotations

from pathlib import Path

import duckdb

from queria_master.enrichment import (
    export_establishment_contacts,
    initialize_database,
    sync_embedded_public_enrichment,
)


def _create_canonical(path: Path) -> None:
    con = duckdb.connect(str(path))
    try:
        con.execute("CREATE SCHEMA core")
        con.execute("CREATE SCHEMA mhlw")
        con.execute(
            """
            CREATE TABLE core.companies AS SELECT * FROM (VALUES
                ('1000000000001', 'A社', '東京都', '新宿区', '東京都新宿区1', NULL, NULL, NULL),
                ('1000000000002', 'B社', '大阪府', '大阪市', '大阪府大阪市2', NULL, NULL, NULL)
            ) AS t(corporate_number, company_name, prefecture_name, city_name,
                   full_address, company_url, jsic_codes_raw, business_summary)
            """
        )
        con.execute(
            """
            CREATE TABLE mhlw.kaigo_establishment AS SELECT * FROM (VALUES
                ('K-1', '訪問介護', 'A新宿事業所', '東京都新宿区1', '03-1234-5678', '03-1234-0000',
                 '1000000000001', 'A社', 'https://a.example.jp/facility'),
                ('K-2', '通所介護', 'A中野事業所', '東京都中野区2', '03-9999-8888', NULL,
                 '1000000000001', 'A社', NULL),
                ('K-X', '訪問介護', '番号不明', '東京都', '03-0000-0000', NULL,
                 'invalid', '不明', NULL)
            ) AS t(establishment_number, service_type, name, address, phone, fax,
                   corporate_number, corporate_name, url)
            """
        )
        con.execute(
            """
            CREATE TABLE mhlw.shougai_establishment AS SELECT * FROM (VALUES
                ('S-1', '就労支援', 'B大阪事業所', '大阪市', '北区3', '06-1111-2222', NULL,
                 'https://b.example.jp', '1000000000002', 'B社')
            ) AS t(establishment_number, service_type, name, address_city, address_detail,
                   phone, fax, url, corporate_number, corporate_name)
            """
        )
    finally:
        con.close()

def test_sync_embedded_public_establishments_is_scoped_and_idempotent(tmp_path: Path):
    canonical = tmp_path / "canonical.duckdb"
    enrichment = tmp_path / "evidence.duckdb"
    _create_canonical(canonical)
    initialize_database(canonical, enrichment)

    first = sync_embedded_public_enrichment(canonical, enrichment)
    second = sync_embedded_public_enrichment(canonical, enrichment)

    assert first["establishment_records"] == 3
    assert first["companies_with_establishment_phone"] == 2
    assert first["companies_with_establishment_url"] == 2
    assert second["establishment_records"] == 3

    con = duckdb.connect(str(enrichment), read_only=True)
    try:
        rows = con.execute(
            """
            SELECT corporate_number, establishment_name, phone_normalized, url, contact_scope
            FROM enrichment.company_establishments
            ORDER BY corporate_number, establishment_name
            """
        ).fetchall()
        assert rows == [
            ("1000000000001", "A中野事業所", "0399998888", None, "establishment"),
            ("1000000000001", "A新宿事業所", "0312345678", "https://a.example.jp/facility", "establishment"),
            ("1000000000002", "B大阪事業所", "0611112222", "https://b.example.jp", "establishment"),
        ]
        assert con.execute("SELECT count(*) FROM enrichment.evidence_documents").fetchone()[0] == 3
        assert con.execute(
            "SELECT count(*) FROM enrichment.enrichment_state "
            "WHERE field_name = 'establishment_phone' AND state = 'found'"
        ).fetchone()[0] == 2
        assert con.execute(
            "SELECT establishment_count, establishment_phone_count, establishment_url_count "
            "FROM crm.v_company_establishment_summary WHERE corporate_number = '1000000000001'"
        ).fetchone() == (2, 2, 1)
    finally:
        con.close()

    columns, exported = export_establishment_contacts(
        canonical,
        enrichment_path=enrichment,
        prefecture="東京都",
        max_rows=10,
    )
    records = [dict(zip(columns, row)) for row in exported]
    assert len(records) == 2
    assert {record["establishment_phone"] for record in records} == {"0312345678", "0399998888"}
    assert {record["contact_scope"] for record in records} == {"establishment"}
