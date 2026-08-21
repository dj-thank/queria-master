from __future__ import annotations

import csv
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))

import public_data_enricher as pe


class PublicDataEnricherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "company.sqlite3"
        self.con = pe.connect(self.db)
        pe.init_schema(self.con)

    def tearDown(self) -> None:
        self.con.close()
        self.tmp.cleanup()

    def prepare(self, rows: list[list[str]] | None = None) -> None:
        rows = rows or [
            ["local-001", "例示株式会社", "東京都千代田区1-1-1", "1234", "3911", "受託開発ソフトウェア業"],
            ["local-002", "見本合同会社", "大阪府大阪市北区2-2-2", "", "4012", "ポータルサイト・サーバ運営業"],
        ]
        path = self.root / "companies.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["SOURCE_ID", "企業名", "本店所在地", "証券コード", "JSIC細分類コード", "JSIC細分類名"])
            writer.writerows(rows)
        result = pe.prepare_from_csv(self.con, path, replace=False)
        self.assertEqual(result["inserted"], len(rows))

    @staticmethod
    def reader(text: str) -> csv.DictReader:
        return csv.DictReader(io.StringIO(text.strip() + "\n"))

    def test_prepare_csv_generates_source_ids_when_missing(self) -> None:
        path = self.root / "companies.csv"
        path.write_text("企業名,所在地,任意列\n例示株式会社,東京都千代田区1-1-1,保持値\n", encoding="utf-8-sig")
        result = pe.prepare_from_csv(self.con, path)
        row = self.con.execute("SELECT * FROM companies").fetchone()
        self.assertTrue(result["generated_source_ids"])
        self.assertEqual(row["source_id"], "row-00000001")
        headers = json.loads(pe.get_meta(self.con, "source_headers_json"))
        original = json.loads(row["source_row_json"])
        self.assertEqual(dict(zip(headers, original))["任意列"], "保持値")

    def test_basic_import_refuses_unscoped_dump(self) -> None:
        self.prepare()
        reader = self.reader("""
法人番号,商号又は名称,所在地,事業概要
1234567890123,例示株式会社,東京都千代田区1-1-1,ソフトウェア開発
""")
        with self.assertRaises(RuntimeError):
            pe.import_gbiz_basic(self.con, "basic.csv", reader)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM public_master").fetchone()[0], 0)

    def test_numbering_scope_financial_and_export(self) -> None:
        self.prepare()
        numbering = self.reader("""
SOURCE_ID,企業名,本店所在地,法人番号,結果コード,ヒット件数
local-001,例示株式会社,東京都千代田区1-1-1,1234567890123,M00,1
local-002,見本合同会社,大阪府大阪市北区2-2-2,9999999999999,M01,1
""")
        stats = pe.import_numbering(self.con, "numbering.csv", numbering, accept_prefix=False)
        self.assertEqual(stats[1:3], (1, 1))

        basic = self.reader("""
法人番号,商号又は名称,郵便番号,所在地,法人代表者名,資本金,資本金（単位）,従業員数,企業ホームページ,事業概要,事業種目,データ品質,出典元,最終更新日
1234567890123,例示株式会社,1000001,東京都千代田区1-1-1,例示太郎,10,百万円,20,https://example.com,業務システム開発,ソフトウェア,公表,公的データ,2026-08-01
1111111111111,対象外株式会社,1000002,東京都千代田区9-9-9,対象外,999,百万円,999,https://outside.example,対象外,対象外,公表,公的データ,2026-08-01
""")
        stats = pe.import_gbiz_basic(self.con, "basic.csv", basic)
        self.assertEqual(stats[1], 1)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM public_master").fetchone()[0], 1)

        financial = self.reader("""
法人番号,事業年度,売上高,売上高（単位）,当期純利益,当期純利益（単位）,出典元
1234567890123,2025年4月1日-2026年3月31日,120,百万円,△5,百万円,公的データ
1111111111111,2025年4月1日-2026年3月31日,999,百万円,99,百万円,公的データ
""")
        stats = pe.import_gbiz_financial(self.con, "financial.csv", financial)
        self.assertEqual(stats[1], 1)
        fin = self.con.execute("SELECT * FROM financial_history").fetchone()
        self.assertEqual(fin["revenue_yen"], 120_000_000)
        self.assertEqual(fin["net_income_yen"], -5_000_000)

        workplace = self.reader("""
法人番号,従業員の平均年齢,出典元
1234567890123,41.2,公的データ
1111111111111,99,公的データ
""")
        stats = pe.import_gbiz_workplace(self.con, "workplace.csv", workplace)
        self.assertEqual(stats[1], 1)

        pe.derive(self.con)
        output = self.root / "csv"
        result = pe.export_all(self.con, output)
        self.assertEqual(result["integrated_rows"], 2)
        self.assertTrue((output / "companies_enriched.csv").exists())
        self.assertTrue((output / "review_required.csv").exists())
        with (output / "companies_enriched.csv").open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        accepted = next(row for row in rows if row["SOURCE_ID"] == "local-001")
        review = next(row for row in rows if row["SOURCE_ID"] == "local-002")
        self.assertEqual(accepted["公開_最新売上円"], "120000000")
        self.assertEqual(accepted["公開_平均年齢"], "41.2")
        self.assertEqual(review["公開_法人番号採用状態"], "review")
        self.assertEqual(review["公開_法人名"], "")

    def test_conflicting_accepted_matches_are_not_joinable(self) -> None:
        self.prepare(rows=[["local-001", "例示株式会社", "東京都千代田区1-1-1", "", "3911", "受託開発ソフトウェア業"]])
        first = self.reader("""
SOURCE_ID,企業名,本店所在地,法人番号,結果コード,ヒット件数
local-001,例示株式会社,東京都千代田区1-1-1,1234567890123,M00,1
""")
        second = self.reader("""
SOURCE_ID,企業名,本店所在地,法人番号,結果コード,ヒット件数
local-001,例示株式会社,東京都千代田区1-1-1,9999999999999,M00,1
""")
        pe.import_numbering(self.con, "first.csv", first, False)
        pe.import_numbering(self.con, "second.csv", second, False)
        match = self.con.execute("SELECT * FROM corporate_matches").fetchone()
        candidates = self.con.execute("SELECT corporate_number FROM corporate_match_candidates ORDER BY corporate_number").fetchall()
        self.assertEqual(match["status"], "review")
        self.assertEqual(match["corporate_number"], "")
        self.assertEqual([row[0] for row in candidates], ["1234567890123", "9999999999999"])

    def test_site_phone_requires_official_host_and_match(self) -> None:
        self.prepare(rows=[["local-001", "例示株式会社", "東京都千代田区1-1-1", "", "3911", "受託開発ソフトウェア業"]])
        pe.import_numbering(self.con, "numbering.csv", self.reader("""
SOURCE_ID,企業名,本店所在地,法人番号,結果コード,ヒット件数
local-001,例示株式会社,東京都千代田区1-1-1,1234567890123,M00,1
"""), False)
        pe.import_gbiz_basic(self.con, "basic.csv", self.reader("""
法人番号,商号又は名称,所在地,企業ホームページ,事業概要
1234567890123,例示株式会社,東京都千代田区1-1-1,https://example.com,ソフトウェア開発
"""))
        bad = pe.import_site_phone(self.con, "bad.csv", self.reader("""
SOURCE_ID,法人番号,電話番号,根拠URL,根拠テキスト,信頼度
local-001,1234567890123,03-1234-5678,https://other.example/contact,TEL,0.9
"""))
        self.assertEqual(bad[2], 1)
        good = pe.import_site_phone(self.con, "good.csv", self.reader("""
SOURCE_ID,法人番号,電話番号,根拠URL,根拠テキスト,信頼度
local-001,1234567890123,03-1234-5678,https://www.example.com/contact,代表TEL,0.9
"""))
        self.assertEqual(good[1], 1)
        self.assertEqual(self.con.execute("SELECT phone FROM site_contacts").fetchone()[0], "0312345678")

    def test_amount_and_date_normalization(self) -> None:
        self.assertEqual(pe.amount_to_yen("1.2兆円"), 1_200_000_000_000)
        self.assertEqual(pe.amount_to_yen("△100", "億円"), -10_000_000_000)
        self.assertEqual(pe.latest_date_key("2025年4月1日-2026年3月31日"), "2026-03-31")


if __name__ == "__main__":
    unittest.main()
