from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "import_verified_contacts.py"
SPEC = importlib.util.spec_from_file_location("import_verified_contacts", MODULE_PATH)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class ImportVerifiedContactsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "companies.sqlite3"
        con = sqlite3.connect(self.db)
        con.execute(
            """CREATE TABLE companies(
            source_id TEXT PRIMARY KEY,
            source_row INTEGER,
            company_name TEXT,
            address TEXT,
            security_code TEXT,
            jsic_code TEXT,
            jsic_name TEXT,
            source_json TEXT
            )"""
        )
        con.executemany(
            "INSERT INTO companies VALUES(?,?,?,?,?,?,?,?)",
            [
                ("local-1", 1, "アルファ株式会社", "東京都千代田区1-1", "1234", "", "", "{}"),
                ("local-2", 2, "ベータ株式会社", "東京都港区2-2", "", "", "", "{}"),
                ("local-3", 3, "同名株式会社", "東京都新宿区3-3", "", "", "", "{}"),
                ("local-4", 4, "同名株式会社", "東京都渋谷区4-4", "", "", "", "{}"),
            ],
        )
        con.commit()
        con.close()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_unique_security_and_address_matches(self) -> None:
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        module.ensure_schema(con)
        indexes = module.company_indexes(con)
        security_match, _, _ = module.resolve_company(
            {"照合企業名": "株式会社アルファ", "証券コード": "1234"}, indexes
        )
        address_match, _, _ = module.resolve_company(
            {"照合企業名": "ベータ株式会社", "照合所在地": "東京都港区2丁目2番"}, indexes
        )
        self.assertEqual(security_match["source_id"], "local-1")
        self.assertEqual(address_match["source_id"], "local-2")
        con.close()

    def test_company_name_only_is_not_accepted(self) -> None:
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        module.ensure_schema(con)
        target, method, reason = module.resolve_company(
            {"照合企業名": "同名株式会社"}, module.company_indexes(con)
        )
        self.assertIsNone(target)
        self.assertEqual(method, "未照合")
        self.assertIn("company-name-only", reason)
        con.close()


if __name__ == "__main__":
    unittest.main()
