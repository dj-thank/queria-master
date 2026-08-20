from pathlib import Path
import tempfile
import unittest

from queria_master.enrichment import initialize_database, seed_enrichment
from queria_master.runtime import build_runtime_database, runtime_summary


try:
    import duckdb
except ImportError:  # pragma: no cover
    duckdb = None


@unittest.skipIf(duckdb is None, "duckdb is not installed")
class RuntimeDatabaseTests(unittest.TestCase):
    def test_build_runtime_is_single_file_and_contains_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical = root / "canonical.duckdb"
            enrichment = root / "enrichment.duckdb"
            runtime = root / "runtime.duckdb"

            con = duckdb.connect(str(canonical))
            try:
                con.execute("CREATE SCHEMA core")
                con.execute("CREATE SCHEMA meta")
                con.execute(
                    """
                    CREATE TABLE core.companies AS SELECT * FROM (VALUES
                        ('1000000000001', 'A社', '東京都', '渋谷区', '東京都渋谷区1-1', '1500000', 'https://a.example.jp', 'G|G39', 'ソフトウェア'),
                        ('1000000000002', 'B社', '大阪府', '大阪市', '大阪府大阪市2-2', '5300000', NULL, 'E|E09', '食品製造')
                    ) AS t(corporate_number, company_name, prefecture_name, city_name,
                           full_address, post_code, company_url, jsic_codes_raw, business_summary)
                    """
                )
                con.execute(
                    "CREATE TABLE meta.refresh_log AS SELECT 'refresh-test' AS refresh_id, 'all-public' AS scope"
                )
            finally:
                con.close()

            initialize_database(canonical, enrichment)
            seed_enrichment(canonical, enrichment_path=enrichment)
            stats = build_runtime_database(canonical, enrichment, runtime, threads=1, memory_limit="1GB")
            self.assertEqual(stats["company_count"], 2)
            self.assertEqual(stats["profile"]["row_count"], 2)
            self.assertTrue(stats["generation_id"])
            self.assertTrue(runtime.is_file())

            summary = runtime_summary(runtime)
            self.assertEqual(summary["counts"]["companies"], 2)
            self.assertEqual(summary["counts"]["search_profiles"], 2)
            self.assertEqual(summary["manifest"]["generation_id"], stats["generation_id"])

            check = duckdb.connect(str(runtime), read_only=True)
            try:
                self.assertEqual(
                    check.execute(
                        "SELECT resolved_address FROM search.company_documents "
                        "WHERE corporate_number = '1000000000001'"
                    ).fetchone()[0],
                    "東京都渋谷区1-1",
                )
                self.assertEqual(
                    check.execute("SELECT count(*) FROM enrichment.enrichment_state").fetchone()[0],
                    10,
                )
                effective_url, official_url = check.execute(
                    "SELECT effective_company_url, official_url FROM search.company_documents "
                    "WHERE corporate_number = '1000000000001'"
                ).fetchone()
                self.assertEqual(effective_url, "https://a.example.jp")
                self.assertIsNone(official_url)
            finally:
                check.close()


if __name__ == "__main__":
    unittest.main()
