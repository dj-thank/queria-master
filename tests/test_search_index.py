from pathlib import Path
import os
import shutil
import tempfile
import time
import unittest

from queria_master.pipeline import PipelineError
from queria_master.query import search_companies
from queria_master.search_index import SearchIndex, SearchIndexError, build_search_index


try:
    import duckdb
except ImportError:  # pragma: no cover - the project runtime installs DuckDB.
    duckdb = None


@unittest.skipIf(duckdb is None, "duckdb is not installed")
class SearchIndexTests(unittest.TestCase):
    def test_trigram_index_returns_exact_japanese_match_and_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "companies.duckdb"
            index_path = root / "search.sqlite"
            con = duckdb.connect(str(database))
            try:
                con.execute("CREATE SCHEMA core")
                con.execute("CREATE SCHEMA meta")
                con.execute(
                    """
                    CREATE TABLE core.companies AS
                    SELECT * FROM (VALUES
                        ('1000000000001', 'ソフトウェア開発株式会社', '東京都', '渋谷区', 'G|H', '39|42', 'G|G39|H42421', 20, 1000000, 'https://software.example.jp', 'ソフトウェア開発', '301'),
                        ('1000000000002', '食品製造株式会社', '大阪府', '大阪市', 'E', '09', 'E|E09|noiseH42noise', 10, 500000, 'https://food.example.jp', '食品製造', '301'),
                        ('1000000000003', 'クラウドサービス株式会社', '東京都', '港区', 'G', '40', 'G|G40', 30, 2000000, 'https://cloud.example.jp', 'クラウドサービス', '101'),
                        ('1000000000004', 'ソフト開発ウェア株式会社', '東京都', '新宿区', 'G', '39', 'G|G39', 5, 100000, 'https://near.example.jp', 'ソフト開発ウェア', '301')
                    ) AS t(corporate_number, company_name, prefecture_name, city_name,
                           jsic_major_codes_all, jsic_middle_codes_all, jsic_codes_all_raw,
                           employee_number, capital_stock,
                           company_url, business_summary, corporate_kind_code)
                    """
                )
                con.execute(
                    "CREATE TABLE meta.refresh_log AS SELECT 'refresh-1' AS refresh_id, 'all-public' AS scope"
                )
                con.execute(
                    """
                    CREATE TABLE meta.runtime_manifest AS
                    SELECT '2' AS schema_version, current_timestamp AS built_at,
                           '{"generation_id":"generation-a"}' AS manifest_json
                    """
                )
                con.execute("CREATE SCHEMA search")
                con.execute(
                    """
                    CREATE TABLE search.company_documents AS
                    SELECT c.*,
                           concat_ws('', c.prefecture_name, c.city_name) AS resolved_address,
                           c.prefecture_name AS resolved_prefecture_name,
                           c.city_name AS resolved_city_name,
                           c.company_url AS effective_company_url,
                           CASE WHEN c.corporate_number = '1000000000001' THEN '03-1234-5678' END AS phone,
                           CASE WHEN c.corporate_number = '1000000000001' THEN 'info@example.jp' END AS email,
                           CASE WHEN c.corporate_number = '1000000000001' THEN 'https://example.jp/contact' END AS inquiry_form_url
                    FROM core.companies c
                    """
                )
            finally:
                con.close()

            stats = build_search_index(database, index_path)
            self.assertEqual(stats["row_count"], 4)

            with SearchIndex(index_path, database_path=database) as index:
                started = time.perf_counter()
                hits = index.search("ソフトウェア", limit=10)
                elapsed_ms = (time.perf_counter() - started) * 1000
                self.assertEqual([hit["corporate_number"] for hit in hits], ["1000000000001"])
                self.assertLess(elapsed_ms, 1000)
                self.assertEqual(index.metadata["refresh_id"], "refresh-1")
                self.assertEqual(index.metadata["runtime_generation_id"], "generation-a")

                regional = index.search("株式会社", prefecture="東京都", limit=10)
                self.assertEqual(
                    {hit["corporate_number"] for hit in regional},
                    {"1000000000001", "1000000000003", "1000000000004"},
                )
                category = index.search(None, prefecture="東京都", industry_majors=("G",), fast=True, limit=10)
                self.assertEqual(len(category), 3)
                self.assertEqual(
                    len({hit["corporate_number"] for hit in category}),
                    len(category),
                )
                self.assertEqual(
                    {hit["corporate_number"] for hit in category},
                    {"1000000000001", "1000000000003", "1000000000004"},
                )
                corporate_kind = index.search(None, corporate_kinds=("101",), fast=True, limit=10)
                self.assertEqual(
                    [hit["corporate_number"] for hit in corporate_kind],
                    ["1000000000003"],
                )
                middle_category = index.search(
                    None,
                    industry_middles=("39",),
                    fast=True,
                    limit=10,
                )
                self.assertEqual(len(middle_category), 2)
                self.assertEqual(
                    len({hit["corporate_number"] for hit in middle_category}),
                    len(middle_category),
                )
                contact_hits = index.search("info@example.jp", limit=10)
                self.assertEqual(contact_hits[0]["corporate_number"], "1000000000001")
                self.assertEqual(contact_hits[0]["email"], "info@example.jp")
                self.assertEqual(contact_hits[0]["phone"], "03-1234-5678")

                phone_hits = index.search("03-1234-5678", fast=True, limit=10)
                self.assertEqual(
                    [hit["corporate_number"] for hit in phone_hits],
                    ["1000000000001"],
                )
                url_hits = index.search("https://software.example.jp", fast=True, limit=10)
                self.assertEqual(
                    [hit["corporate_number"] for hit in url_hits],
                    ["1000000000001"],
                )
                inquiry_url_hits = index.search("https://example.jp/contact", fast=True, limit=10)
                self.assertEqual(
                    [hit["corporate_number"] for hit in inquiry_url_hits],
                    ["1000000000001"],
                )

                detailed_major = index.search(None, industry_majors=("H",), fast=True, limit=10)
                self.assertEqual(
                    [hit["corporate_number"] for hit in detailed_major],
                    ["1000000000001"],
                )
                detailed_middle = index.search(None, industry_middles=("42",), fast=True, limit=10)
                self.assertEqual(
                    [hit["corporate_number"] for hit in detailed_middle],
                    ["1000000000001"],
                )

                with self.assertRaisesRegex(SearchIndexError, "256文字以内"):
                    index.search("x" * 257, fast=True, limit=10)
                with self.assertRaisesRegex(SearchIndexError, "NUL文字"):
                    index.search("株式会社\x00東京", fast=True, limit=10)

                short_fast = index.search("ソ", fast=True, limit=10)
                self.assertEqual(
                    {hit["corporate_number"] for hit in short_fast},
                    {"1000000000001", "1000000000004"},
                )
                short_substring = index.search("フト", fast=False, limit=10)
                self.assertEqual(
                    {hit["corporate_number"] for hit in short_substring},
                    {"1000000000001", "1000000000004"},
                )

            relocated_database = root / "extracted" / "queria_runtime.duckdb"
            relocated_database.parent.mkdir()
            shutil.copyfile(database, relocated_database)
            os.utime(relocated_database, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
            with SearchIndex(index_path, database_path=relocated_database) as relocated_index:
                self.assertEqual(
                    relocated_index.search("ソフトウェア", limit=10)[0]["corporate_number"],
                    "1000000000001",
                )

            mismatched_database = root / "mismatched.duckdb"
            shutil.copyfile(database, mismatched_database)
            mismatch = duckdb.connect(str(mismatched_database))
            try:
                mismatch.execute(
                    "UPDATE meta.runtime_manifest "
                    "SET manifest_json = '{\"generation_id\":\"generation-b\"}'"
                )
            finally:
                mismatch.close()
            with self.assertRaisesRegex(Exception, "generation_id"):
                SearchIndex(index_path, database_path=mismatched_database)

            with self.assertRaisesRegex(PipelineError, "256文字以内"):
                search_companies(
                    db_path=root / "missing.duckdb",
                    keyword="x" * 257,
                    search_index=None,
                )


if __name__ == "__main__":
    unittest.main()
