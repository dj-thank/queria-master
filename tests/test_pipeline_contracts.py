from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from queria_master.pipeline import export_remote, refresh


class PipelineContractTests(unittest.TestCase):
    def test_export_keeps_parquet_as_final_temp_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "companies.parquet"

            def fake_run(args, **_kwargs):
                out_index = args.index("--out") + 1
                partial = Path(args[out_index])
                self.assertEqual(partial.suffix, ".parquet")
                self.assertTrue(partial.name.endswith(".partial.parquet"))
                partial.write_bytes(b"PAR1-test")
                return None

            with patch("queria_master.pipeline._run_queria", side_effect=fake_run):
                export_remote("info-communications", output)
            self.assertEqual(output.read_bytes(), b"PAR1-test")

    def test_no_cache_keeps_correct_parquet_size_in_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "data" / "test.duckdb"
            cache = root / "cache"
            payload = b"x" * 123

            def fake_export(_scope, output):
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(payload)

            def fake_build(_parquet, database_path, **_kwargs):
                database_path.parent.mkdir(parents=True, exist_ok=True)
                database_path.write_bytes(b"db")
                return 7, "abc123"

            with (
                patch("queria_master.pipeline.export_remote", side_effect=fake_export),
                patch("queria_master.pipeline.collect_source_metadata", return_value={}),
                patch("queria_master.pipeline.build_local_database", side_effect=fake_build),
            ):
                result = refresh(
                    scope="info-communications",
                    database_path=database,
                    cache_dir=cache,
                    keep_cache=False,
                )

            self.assertEqual(result.row_count, 7)
            self.assertEqual(result.parquet_bytes, len(payload))
            self.assertIsNone(result.parquet_path)
            self.assertTrue(database.is_file())


if __name__ == "__main__":
    unittest.main()
