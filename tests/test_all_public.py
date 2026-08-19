from pathlib import Path
import tempfile
import unittest

from queria_master.pipeline import build_all_public_database
from queria_master.query import search_companies
from queria_master.resources import PUBLIC_TABLES, normalize_scope


try:
    import duckdb
except ImportError:  # pragma: no cover - the project runtime installs DuckDB.
    duckdb = None


@unittest.skipIf(duckdb is None, "duckdb is not installed")
class AllPublicDatabaseTests(unittest.TestCase):
    def _write_parquet(self, con, path: Path, query: str) -> None:
        escaped = str(path.resolve()).replace("'", "''")
        con.execute(f"COPY ({query}) TO '{escaped}' (FORMAT PARQUET)")

    def test_union_dedup_activity_tables_and_candidate_view(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = duckdb.connect()
            try:
                paths: dict[str, Path] = {}
                source.execute(
                    """
                    CREATE TABLE nta AS
                    SELECT * FROM (VALUES
                        ('1000000000001', '旧社名', DATE '2025-01-01', 1),
                        ('1000000000001', '新社名', DATE '2026-01-01', 2),
                        ('1000000000002', '住所だけ法人', DATE '2026-02-01', 1)
                    ) AS t(corporate_number, name, update_date, seq)
                    """
                )
                source.execute(
                    """
                    CREATE TABLE company AS
                    SELECT * FROM (VALUES
                        ('1000000000001', '新社名', 'G:情報通信業-40:インターネット附随サービス業-401:'),
                        ('1000000000003', 'クラウドSaaS株式会社', 'E:製造業-09:食料品製造業-091:')
                    ) AS t(corporate_number, name, business_items)
                    """
                )
                fact_queries = {
                    "gbizinfo_subsidy": "SELECT '1000000000001' AS corporate_number, '補助金' AS title",
                    "gbizinfo_procurement": "SELECT '1000000000001' AS corporate_number, '調達' AS title",
                    "gbizinfo_patent": "SELECT '1000000000003' AS corporate_number, '特許' AS title",
                    "gbizinfo_certification": "SELECT '1000000000002' AS corporate_number, '認定' AS title",
                    "gbizinfo_commendation": "SELECT '1000000000002' AS corporate_number, '表彰' AS title",
                }
                expanded_queries = {
                    "edinet_business_results": "SELECT '1000000000001' AS corporate_number, 2025 AS fiscal_year, 100 AS net_sales",
                    "edinet_companies": "SELECT '1000000000001' AS corporate_number, 'E00001' AS edinet_code",
                    "edinet_documents": "SELECT '1000000000001' AS corporate_number, 'D00001' AS doc_id",
                    "edinet_funds": "SELECT 'F00001' AS fund_code, 'ファンド' AS fund_name",
                    "edinet_financial_facts": "SELECT '1000000000001' AS corporate_number, 'jppfs_cor:NetSales' AS element_id, 100 AS value",
                    "mhlw_josei_katsuyaku": "SELECT '1000000000001' AS corporate_number, '女性活躍' AS program",
                    "mhlw_kaigo": "SELECT '1000000000002' AS corporate_number, '介護事業所' AS establishment_name",
                    "mhlw_shougai": "SELECT '1000000000003' AS corporate_number, '障害福祉' AS establishment_name",
                    "mhlw_ndb_health_checkup": "SELECT 2023 AS fiscal_year, 'BMI' AS test_item, '01' AS prefecture_code, 1 AS count",
                    "p_portal_procurement_award": "SELECT '1000000000001' AS corporate_number, '政府調達' AS title",
                    "metro_tokyo_care_service": "SELECT '1000000000002' AS corporate_number, '都内介護' AS name",
                    "metro_tokyo_cultural_property": "SELECT '1000000000003' AS corporate_number, '文化財' AS name",
                    "metro_tokyo_event": "SELECT '1000000000001' AS corporate_number, 'イベント' AS name",
                    "metro_tokyo_food_business": "SELECT '1000000000002' AS corporate_number, '食品営業' AS name",
                    "metro_tokyo_public_facility": "SELECT '1000000000003' AS corporate_number, '公共施設' AS name",
                    "metro_tokyo_support_system": "SELECT '支援制度' AS system_org, '制度' AS name",
                    "metro_tokyo_tourism": "SELECT '1000000000001' AS corporate_number, '観光施設' AS name",
                }
                source_queries = {
                    "houjin_bangou": "SELECT * FROM nta",
                    "gbizinfo_company": "SELECT * FROM company",
                    **fact_queries,
                    **expanded_queries,
                }
                self.assertEqual(set(source_queries), set(PUBLIC_TABLES))
                for table_key, query in source_queries.items():
                    path = root / f"{table_key}.parquet"
                    self._write_parquet(source, path, query)
                    paths[table_key] = path
            finally:
                source.close()

            database = root / "data" / "all-public.duckdb"
            company_count, total_bytes, manifest_sha, stats = build_all_public_database(
                paths,
                database,
                started_at="2026-08-19T00:00:00+00:00",
                source_metadata={"captured_at": "2026-08-19T00:00:00+00:00"},
            )
            self.assertEqual(company_count, 3)
            self.assertGreater(total_bytes, 0)
            self.assertEqual(len(stats), len(PUBLIC_TABLES))
            self.assertTrue(manifest_sha)

            con = duckdb.connect(str(database), read_only=True)
            try:
                self.assertEqual(con.execute("SELECT count(*) FROM core.companies").fetchone()[0], 3)
                self.assertEqual(
                    con.execute(
                        "SELECT company_name FROM core.companies WHERE corporate_number = '1000000000001'"
                    ).fetchone()[0],
                    "新社名",
                )
                self.assertEqual(
                    con.execute("SELECT count(*) FROM core.v_info_communications_strict").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    con.execute(
                        "SELECT count(*) FROM core.v_info_communications_candidates WHERE corporate_number = '1000000000003'"
                    ).fetchone()[0],
                    1,
                )
                all_codes = con.execute(
                    "SELECT jsic_codes_all_raw FROM core.companies WHERE corporate_number = '1000000000003'"
                ).fetchone()[0]
                self.assertEqual(all_codes, "E|E09|E09091")
                self.assertEqual(
                    con.execute(
                        "SELECT count(*) FROM core.company_industries WHERE corporate_number = '1000000000003' AND jsic_middle_code = '09' AND jsic_level = 'middle'"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    con.execute(
                        "SELECT count(*) FROM core.company_category_index "
                        "WHERE corporate_number = '1000000000003' AND jsic_middle_code = '09'"
                    ).fetchone()[0],
                    2,
                )
                self.assertEqual(
                    con.execute(
                        "SELECT company_count FROM core.v_category_summary WHERE jsic_middle_code = '09' AND jsic_level = 'middle'"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    con.execute(
                        "SELECT subsidy_record_count FROM core.v_company_activity WHERE corporate_number = '1000000000001'"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(con.execute("SELECT count(*) FROM gbizinfo.patents").fetchone()[0], 1)
                self.assertEqual(
                    con.execute("SELECT row_count FROM meta.dataset_row_counts WHERE local_table = 'subsidies'").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    con.execute(
                        "SELECT status FROM meta.coverage_boundary WHERE scope_key = 'gbizinfo_financial_detail'"
                    ).fetchone()[0],
                    "summary_only",
                )
                self.assertEqual(
                    con.execute(
                        "SELECT source_record_count FROM core.v_company_source_counts "
                        "WHERE source_key = 'p_portal_procurement_award' AND corporate_number = '1000000000001'"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    con.execute("SELECT count(*) FROM mhlw.ndb_health_checkup").fetchone()[0],
                    1,
                )
                quality = con.execute("SELECT with_business_items, with_jsic_codes FROM core.v_data_quality").fetchone()
                self.assertEqual(tuple(quality), (2, 2))
            finally:
                con.close()

            result_path = root / "manufacturing.json"
            self.assertEqual(
                search_companies(
                    db_path=database,
                    industry_majors=("e",),
                    industry_middles=("09",),
                    limit=10,
                    out=result_path,
                ),
                1,
            )
            self.assertIn("クラウド", result_path.read_text(encoding="utf-8"))

    def test_all_alias_is_canonical(self):
        self.assertEqual(normalize_scope("all"), "all-public")


if __name__ == "__main__":
    unittest.main()
