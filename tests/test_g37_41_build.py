from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_g37_41_fuma as builder


def test_taxonomy_aliases_and_ancestors():
    rows = [
        {"code": "G", "name": "情報通信業", "level": "major", "parent_code": ""},
        {"code": "39", "name": "情報サービス業", "level": "middle", "parent_code": "G"},
        {"code": "391", "name": "ソフトウェア業", "level": "small", "parent_code": "39"},
        {"code": "3911", "name": "受託開発ソフトウェア業", "level": "detail", "parent_code": "391"},
    ]
    by_code, aliases = builder.canonical_taxonomy(rows)
    assert aliases["G39"] == "39"
    assert aliases["G3911"] == "3911"
    assert builder.ancestors("3911", by_code) == ["G", "39", "391", "3911"]
    assert builder.aliases_for("3911", by_code) == ["G", "39", "G39", "391", "G391", "3911", "G3911"]


def test_phone_is_not_labeled_representative():
    assert builder.normalize_phone("03-1234-5678") == "0312345678"
    assert builder.phone_parts("代表 03-1234-5678 / FAX 03-1234-5679") == ["0312345678", "0312345679"]
    assert builder.normalize_http_url("HTTPS://EXAMPLE.COM/contact#top") == "https://example.com/contact"
    assert builder.normalize_http_url("file:///etc/passwd") is None


def test_public_establishment_contacts_keep_scope_and_evidence():
    con = builder.duckdb.connect()
    con.execute("CREATE SCHEMA mhlw")
    con.execute("CREATE SCHEMA meta")
    con.execute("CREATE TABLE scoped_g_numbers(corporate_number VARCHAR)")
    con.execute("INSERT INTO scoped_g_numbers VALUES ('1234567890123')")
    for table in ["kaigo_establishment", "shougai_establishment"]:
        con.execute(
            f"CREATE TABLE mhlw.{table}(corporate_number VARCHAR, establishment_number VARCHAR, "
            "name VARCHAR, phone VARCHAR, url VARCHAR)"
        )
    con.execute(
        "INSERT INTO mhlw.kaigo_establishment VALUES "
        "('1234567890123','E-1','テスト事業所','03-1234-5678','https://example.com/office'),"
        "('9999999999999','E-2','対象外','06-1234-5678','https://outside.example')"
    )
    con.execute("CREATE TABLE meta.refresh_log(completed_at TIMESTAMP)")
    con.execute("INSERT INTO meta.refresh_log VALUES (TIMESTAMP '2026-08-20 00:00:00')")

    contacts, stats = builder.load_public_establishment_contacts(con)
    assert list(contacts) == ["1234567890123"]
    assert stats["phone_company_rows"] == 1
    assert stats["website_company_rows"] == 1

    companies = [{
        "entity_key": "1234567890123",
        "corporate_number": "1234567890123",
        "phone": None,
        "phone_type": None,
        "phone_source_url": None,
        "phone_confidence": None,
        "phone_evidence_text": None,
        "phone_observed_at": None,
        "phone_status": "no_phone_source",
    }]
    phones: list[dict] = []
    websites: list[dict] = []
    applied = builder.apply_public_establishment_contacts(companies, contacts, phones, websites)
    assert applied == {"promoted_primary_phone_rows": 1}
    assert companies[0]["phone"] == "0312345678"
    assert companies[0]["phone_type"] == "establishment"
    assert "代表電話ではありません" in companies[0]["phone_evidence_text"]
    assert phones[0]["phone_type"] == "establishment"
    assert websites[0]["url_type"] == "establishment"


def test_corporate_match_normalization_preserves_entity_type_and_normalizes_address():
    assert builder.normalize_match_name("（株）テスト・ラボ") == builder.normalize_match_name(
        "株式会社テストラボ"
    )
    assert builder.normalize_match_name("有限会社テスト") != builder.normalize_match_name(
        "株式会社テスト"
    )
    assert builder.normalize_match_address("東京都千代田区丸の内一丁目2番3号") == builder.normalize_match_address(
        "東京都千代田区丸の内1-2-3"
    )


def test_recover_corporate_numbers_accepts_only_unique_exact_matches():
    con = builder.duckdb.connect()
    con.execute("CREATE SCHEMA core")
    con.execute(
        "CREATE TABLE core.companies("
        "corporate_number VARCHAR, company_name VARCHAR, full_address VARCHAR)"
    )
    con.executemany(
        "INSERT INTO core.companies VALUES (?,?,?)",
        [
            ("1234567890123", "株式会社テストラボ", "東京都千代田区丸の内1丁目2番3号"),
            ("9999999999999", "株式会社テストラボ", "大阪府大阪市北区1-1-1"),
        ],
    )
    records = [
        {
            "fuma_id": "fuma-1",
            "by_header": {
                "法人番号": None,
                "企業名": "(株)テスト・ラボ",
                "本店所在地": "東京都千代田区丸の内一丁目2番3号",
            },
        }
    ]
    matches, stats = builder.recover_corporate_numbers(con, records)
    assert matches == {"fuma-1": "1234567890123"}
    assert stats["accepted_one_to_one"] == 1

    records.append(
        {
            "fuma_id": "fuma-explicit",
            "by_header": {
                "法人番号": "1234567890123",
                "企業名": "別名称株式会社",
                "本店所在地": "東京都千代田区1-1-1",
            },
        }
    )
    matches, stats = builder.recover_corporate_numbers(con, records)
    assert matches == {}
    assert stats["rejected_existing_explicit_number"] == 1


def test_insert_batches_uses_multirow_values_without_changing_rows():
    con = builder.duckdb.connect()
    con.execute("CREATE TABLE sample(id INTEGER, value VARCHAR)")
    inserted = builder.insert_batches(
        con,
        "sample",
        ["id", "value"],
        [
            {"id": 0, "value": None},
            {"id": 1, "value": ""},
            {"id": 2, "value": "comma, quote \" and\nnewline"},
            *[{"id": index, "value": f"value-{index}"} for index in range(3, 7)],
        ],
        batch_size=3,
    )
    assert inserted == 7
    assert con.execute("SELECT * FROM sample ORDER BY id").fetchall() == [
        (0, None),
        (1, ""),
        (2, "comma, quote \" and\nnewline"),
        *[(index, f"value-{index}") for index in range(3, 7)],
    ]


def test_fuma_address_is_split_for_region_filters():
    assert builder.parse_japanese_address("東京都千代田区永田町2丁目11番1号") == ("東京都", "千代田区")
    assert builder.parse_japanese_address("神奈川県横浜市西区みなとみらい1丁目") == ("神奈川県", "横浜市西区")


def test_public_region_fields_override_fuma_full_address():
    by_code, aliases = builder.canonical_taxonomy([
        {"code": "G", "name": "情報通信業", "level": "major", "parent_code": ""},
    ])
    company, _, _ = builder.make_company(
        entity_key="1234567890123",
        fuma={"row_number": 2, "values": [], "by_header": {
            "本店所在地": "東京都千代田区永田町2丁目11番1号",
            "address": "東京都千代田区永田町2丁目11番1号",
            "法人番号": "1234567890123",
            "企業名": "テスト株式会社",
            "daibunruiCode": "G",
            "chubunruiCode": None,
            "syoubunruiCode": None,
            "jsicDetailedClass": None,
            "電話番号": None,
        }},
        public={"prefecture_name": "東京都", "city_name": "千代田区", "full_address": "東京都千代田区永田町2丁目11番1号", "company_name": "テスト株式会社", "corporate_number": "1234567890123"},
        fuma_id="fuma-test",
        fuma_row_number=2,
        by_code=by_code,
        aliases=aliases,
        industry=None,
    )
    assert company["prefecture"] == "東京都"
    assert company["city"] == "千代田区"
    assert company["address"] == "東京都千代田区永田町2丁目11番1号"
