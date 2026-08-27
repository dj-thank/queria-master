import json
from pathlib import Path
import re
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PackageContractTests(unittest.TestCase):
    def test_required_files_exist(self):
        required = [
            "README.md",
            "QUICKSTART_JA.md",
            "requirements.txt",
            "requirements-dev.txt",
            "bootstrap.ps1",
            "bootstrap.sh",
            "refresh.ps1",
            "refresh.sh",
            "queria_master/cli.py",
            "queria_master/gbiz_archive.py",
            "queria_master/pipeline.py",
            "queria_master/public_enrichment_bridge.py",
            "queria_master/publish.py",
            "queria_master/website_discovery.py",
            "docs/ARCHITECTURE.md",
            "docs/GBIZ_ARCHIVE_IMPORT_JA.md",
            "docs/WEBSITE_DISCOVERY_EXTRACTION_ARCHITECTURE_JA.md",
            "docs/V090_OPERATIONAL_ARCHITECTURE_JA.md",
            "docs/ZIP_AUDIT_20260824.md",
            "tests/test_gbiz_archive.py",
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

    def test_release_builder_uses_the_verified_file_list(self):
        from scripts import build_release, verify_package

        self.assertIs(build_release.release_files, verify_package.package_files)
        relative_paths = {
            path.relative_to(ROOT).as_posix()
            for path in verify_package.package_files()
        }
        self.assertIn("queria_master/gbiz_archive.py", relative_paths)
        self.assertIn("queria_master/public_enrichment_bridge.py", relative_paths)
        self.assertIn("queria_master/publish.py", relative_paths)
        self.assertIn("queria_master/website_discovery.py", relative_paths)
        self.assertIn("tests/test_gbiz_archive.py", relative_paths)
        self.assertIn("docs/GBIZ_ARCHIVE_IMPORT_JA.md", relative_paths)
        self.assertFalse(
            any(
                "node_modules" in path.split("/") or "target" in path.split("/")
                for path in relative_paths
            )
        )
        self.assertFalse(any(path.startswith("pytest-") for path in relative_paths))

    def test_release_file_list_excludes_untracked_worktree_files(self):
        from scripts import verify_package

        with tempfile.NamedTemporaryFile(
            dir=ROOT,
            prefix="release-untracked-",
            suffix=".txt",
        ) as untracked:
            relative = Path(untracked.name).relative_to(ROOT)
            self.assertNotIn(
                relative,
                {path.relative_to(ROOT) for path in verify_package.package_files()},
            )

    def test_no_embedded_tokens(self):
        token_like = re.compile(
            r"\b(?:qk_|sk-)[A-Za-z0-9_-]{12,}|"
            r"\b(?=[A-Za-z0-9]{0,31}[A-Z])(?=[A-Za-z0-9]{0,31}[a-z])"
            r"(?=[A-Za-z0-9]{0,31}[0-9])[A-Za-z0-9]{32}\b"
        )
        skip = {
            ".git",
            ".venv",
            "node_modules",
            "target",
            "__pycache__",
            ".pytest_cache",
            "build",
            "dist",
            "work",
        }
        for path in ROOT.rglob("*"):
            if (
                not path.is_file()
                or path.suffix.lower() in {".zip", ".duckdb", ".sqlite", ".db", ".parquet", ".pyc", ".exe", ".dll"}
                or any(
                    part in skip or part.startswith(".test-tmp") or part.endswith(".egg-info")
                    for part in path.relative_to(ROOT).parts
                )
                or path.name == "MANIFEST.sha256"
                or (
                    path.relative_to(ROOT).parts[0] in {"data", "cache", "exports"}
                    and path.name not in {"README.md", ".gitkeep"}
                )
            ):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if path.name == "package-lock.json":
                lockfile = json.loads(text)

                def remove_integrity_hashes(value):
                    if isinstance(value, dict):
                        return {
                            key: remove_integrity_hashes(item)
                            for key, item in value.items()
                            if key != "integrity"
                        }
                    if isinstance(value, list):
                        return [remove_integrity_hashes(item) for item in value]
                    return value

                # SRI digests are intentionally high-entropy and otherwise look
                # exactly like credentials. Keep scanning every other lockfile
                # value, including resolved URLs and lifecycle metadata.
                text = json.dumps(remove_integrity_hashes(lockfile), ensure_ascii=False)
            self.assertIsNone(token_like.search(text), str(path.relative_to(ROOT)))


if __name__ == "__main__":
    unittest.main()
