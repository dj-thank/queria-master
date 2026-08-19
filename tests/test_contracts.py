from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PackageContractTests(unittest.TestCase):
    def test_required_files_exist(self):
        required = [
            "README.md",
            "requirements.txt",
            "bootstrap.ps1",
            "bootstrap.sh",
            "queria_master/cli.py",
            "queria_master/pipeline.py",
            "sql/remote/info_communications.sql",
            "reference/sources.json",
            "queria_master/assets/sql/remote/info_communications.sql",
            "queria_master/assets/reference/sources.json",
        ]
        for item in required:
            self.assertTrue((ROOT / item).is_file(), item)

    def test_remote_sql_is_read_only(self):
        forbidden = re.compile(r"\b(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|COPY|ATTACH|DETACH|INSTALL|LOAD)\b", re.I)
        for path in (ROOT / "sql" / "remote").glob("*.sql"):
            sql = path.read_text(encoding="utf-8").strip()
            self.assertIn(sql.split(None, 1)[0].upper(), {"SELECT", "WITH"}, path.name)
            self.assertIsNone(forbidden.search(sql), path.name)


    def test_bundled_assets_match_source_layout(self):
        pairs = [
            (ROOT / "sql" / "remote", ROOT / "queria_master" / "assets" / "sql" / "remote"),
            (ROOT / "reference", ROOT / "queria_master" / "assets" / "reference"),
        ]
        for source_root, bundled_root in pairs:
            source_files = {path.name: path.read_bytes() for path in source_root.glob("*") if path.is_file()}
            bundled_files = {path.name: path.read_bytes() for path in bundled_root.glob("*") if path.is_file()}
            self.assertEqual(source_files, bundled_files)

    def test_no_embedded_tokens(self):
        token_like = re.compile(r"\b(?:qk_|sk-)[A-Za-z0-9_-]{12,}|\b[A-Za-z0-9]{32}\b")
        skip = {".git", ".venv", "__pycache__", ".pytest_cache", "build", "dist"}
        for path in ROOT.rglob("*"):
            if (
                not path.is_file()
                or path.suffix.lower() in {".zip", ".duckdb", ".parquet", ".pyc"}
                or any(part in skip or part.endswith(".egg-info") for part in path.relative_to(ROOT).parts)
                or path.name == "MANIFEST.sha256"
                or (
                    path.relative_to(ROOT).parts[0] in {"data", "cache", "exports"}
                    and path.name not in {"README.md", ".gitkeep"}
                )
            ):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            self.assertIsNone(token_like.search(text), str(path.relative_to(ROOT)))


if __name__ == "__main__":
    unittest.main()
