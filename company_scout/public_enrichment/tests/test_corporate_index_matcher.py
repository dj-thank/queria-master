from __future__ import annotations

import csv
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "corporate_index_matcher.py"
SPEC = importlib.util.spec_from_file_location("corporate_index_matcher", MODULE_PATH)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class CorporateIndexMatcherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.targets = self.root / "targets.csv"
        with self.targets.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["SOURCE_ID", "企業名", "本店所在地"],
            )
            writer.writeheader()
            writer.writerows(
                [
                    {
                        "SOURCE_ID": "exact",
                        "企業名": "株式会社ＡＢＣ",
                        "本店所在地": "東京都千代田区丸の内1丁目1番1号",
                    },
                    {
                        "SOURCE_ID": "prefix",
                        "企業名": "合同会社ベータ",
                        "本店所在地": "大阪府大阪市北区梅田1丁目1番1号 グランドビル",
                    },
                    {
                        "SOURCE_ID": "duplicate",
                        "企業名": "株式会社同名",
                        "本店所在地": "東京都新宿区1丁目1番1号",
                    },
                    {
                        "SOURCE_ID": "missing",
                        "企業名": "株式会社未登録",
                        "本店所在地": "福岡県福岡市1丁目1番1号",
                    },
                ]
            )
        self.index = self.root / "public-index.tsv"
        fields = [
            "corporate_number",
            "company_name",
            "company_name_kana",
            "post_code",
            "prefecture_name",
            "city_name",
            "street_number",
            "full_address",
            "company_url",
            "representative_name",
            "employee_number",
            "capital_stock",
            "business_summary",
            "jsic_middle_codes",
            "nta_update_date",
        ]
        with self.index.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t")
            writer.writeheader()
            writer.writerows(
                [
                    {
                        "corporate_number": "1000000000001",
                        "company_name": "株式会社ABC",
                        "company_name_kana": "エービーシー",
                        "post_code": "1000005",
                        "prefecture_name": "東京都",
                        "city_name": "千代田区丸の内",
                        "street_number": "1-1-1",
                        "full_address": "東京都千代田区丸の内1-1-1",
                        "company_url": "https://abc.example/",
                        "representative_name": "A",
                        "employee_number": "100",
                        "capital_stock": "10000000",
                        "business_summary": "software",
                        "jsic_middle_codes": "39",
                        "nta_update_date": "2026-07-31",
                    },
                    {
                        "corporate_number": "1000000000002",
                        "company_name": "合同会社ベータ",
                        "company_name_kana": "ベータ",
                        "post_code": "5300001",
                        "prefecture_name": "大阪府",
                        "city_name": "大阪市北区梅田",
                        "street_number": "1-1-1",
                        "full_address": "大阪府大阪市北区梅田1-1-1",
                        "company_url": "https://beta.example/",
                        "representative_name": "B",
                        "employee_number": "10",
                        "capital_stock": "1000000",
                        "business_summary": "services",
                        "jsic_middle_codes": "39",
                        "nta_update_date": "2026-07-31",
                    },
                    {
                        "corporate_number": "1000000000003",
                        "company_name": "株式会社同名",
                        "full_address": "東京都新宿区1-1-1",
                    },
                    {
                        "corporate_number": "1000000000004",
                        "company_name": "株式会社同名",
                        "full_address": "東京都新宿区1-1-1",
                    },
                ]
            )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _run(self, *, accept_prefix: bool = False):
        output = self.root / ("matches-prefix.csv" if accept_prefix else "matches.csv")
        review = self.root / ("review-prefix.csv" if accept_prefix else "review.csv")
        summary = self.root / ("summary-prefix.json" if accept_prefix else "summary.json")
        result = module.match_index(
            targets_csv=self.targets,
            public_index=self.index,
            output=output,
            review_output=review,
            summary_output=summary,
            accept_prefix=accept_prefix,
        )
        with output.open(encoding="utf-8-sig", newline="") as handle:
            rows = {row["source_id"]: row for row in csv.DictReader(handle)}
        return result, rows, review, summary

    def test_only_unique_exact_match_is_accepted_by_default(self) -> None:
        result, rows, review, summary = self._run()
        self.assertEqual(result["targets"], 4)
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(result["review"], 2)
        self.assertEqual(result["unmatched"], 1)
        self.assertEqual(rows["exact"]["status"], "accepted")
        self.assertEqual(rows["exact"]["corporate_number"], "1000000000001")
        self.assertEqual(rows["exact"]["company_url"], "https://abc.example/")
        self.assertEqual(rows["prefix"]["status"], "review")
        self.assertEqual(rows["prefix"]["match_method"], "name_address_prefix")
        self.assertEqual(rows["duplicate"]["status"], "review")
        self.assertEqual(rows["duplicate"]["same_score_candidates"], "2")
        self.assertEqual(rows["missing"]["status"], "unmatched")
        self.assertGreater(sum(1 for _ in review.open(encoding="utf-8-sig")), 1)
        self.assertEqual(json.loads(summary.read_text(encoding="utf-8"))["accepted"], 1)

    def test_prefix_requires_explicit_opt_in(self) -> None:
        result, rows, _review, _summary = self._run(accept_prefix=True)
        self.assertEqual(result["accepted_prefix"], 1)
        self.assertEqual(rows["prefix"]["status"], "accepted_prefix")
        self.assertEqual(rows["duplicate"]["status"], "review")


if __name__ == "__main__":
    unittest.main()
