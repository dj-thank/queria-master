from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import jsic_g37_41_collection as collection
import export_g_contact_targets as exporter


class G3741CollectionTests(unittest.TestCase):
    def test_target_adapter_selects_only_pending_official_sites(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "phone_targets_g37_41.csv"
            with source.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "entity_key", "corporate_number", "company_name", "prefecture_name", "city_name",
                        "employee_number", "capital_stock", "website", "state", "last_completed_at", "last_error",
                    ],
                )
                writer.writeheader()
                writer.writerows(
                    [
                        {"entity_key": "1000000000001", "corporate_number": "1000000000001", "company_name": "優先株式会社", "prefecture_name": "東京都", "city_name": "千代田区", "employee_number": "120", "capital_stock": "50000000", "website": "https://pending.example/", "state": "pending_official_site"},
                        {"entity_key": "1000000000002", "corporate_number": "1000000000002", "website": "https://known.example/", "state": "fuma_phone"},
                        {"entity_key": "1000000000003", "corporate_number": "1000000000003", "website": "https://establishment.example/", "state": "establishment"},
                        {"entity_key": "fuma:4", "corporate_number": "", "website": "https://unmatched.example/", "state": "pending_official_site"},
                    ]
                )

            target_csv = collection.make_target_csv(source)
            try:
                with target_csv.open("r", encoding="utf-8-sig", newline="") as handle:
                    rows = list(csv.DictReader(handle))
            finally:
                target_csv.unlink(missing_ok=True)

            self.assertEqual([row["corporate_number"] for row in rows], ["1000000000001"])
            self.assertEqual(rows[0]["company_url"], "https://pending.example/")
            self.assertEqual(rows[0]["company_name"], "優先株式会社")
            self.assertEqual(rows[0]["employee_number"], "120")
            self.assertEqual(rows[0]["capital_stock"], "50000000")
            self.assertEqual(rows[0]["jsic_major_codes"], "G")

            exported = Path(temp) / "exported.csv"
            result = exporter.export_targets(source, exported)
            self.assertEqual(result["scope"], "G37-G41")
            self.assertEqual(result["rows"], 1)
            self.assertTrue(exported.is_file())


if __name__ == "__main__":
    unittest.main()
