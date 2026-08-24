from __future__ import annotations

import csv
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "jsic39_collection.py"
SPEC = importlib.util.spec_from_file_location("jsic39_collection", MODULE_PATH)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class Jsic39CollectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.companies = self.root / "companies.csv"
        fields = [
            "corporate_number",
            "company_name",
            "prefecture_name",
            "city_name",
            "jsic_major_codes",
            "jsic_middle_codes",
            "employee_number",
            "capital_stock",
            "representative_name",
            "company_url",
            "business_summary",
        ]
        rows = [
            {
                "corporate_number": "1000000000001",
                "company_name": "Alpha Systems",
                "prefecture_name": "東京都",
                "city_name": "千代田区",
                "jsic_major_codes": "G",
                "jsic_middle_codes": "39",
                "employee_number": "100",
                "capital_stock": "10000000",
                "representative_name": "A",
                "company_url": "https://alpha.example/",
                "business_summary": "software",
            },
            {
                "corporate_number": "1000000000002",
                "company_name": "Beta Systems",
                "prefecture_name": "大阪府",
                "city_name": "大阪市",
                "jsic_major_codes": "G",
                "jsic_middle_codes": "39",
                "employee_number": "50",
                "capital_stock": "20000000",
                "representative_name": "B",
                "company_url": "beta.example",
                "business_summary": "services",
            },
            {
                "corporate_number": "1000000000003",
                "company_name": "Gamma Systems",
                "prefecture_name": "福岡県",
                "city_name": "福岡市",
                "jsic_major_codes": "G",
                "jsic_middle_codes": "39",
                "employee_number": "10",
                "capital_stock": "30000000",
                "representative_name": "C",
                "company_url": "",
                "business_summary": "consulting",
            },
        ]
        with self.companies.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def manifest_row(self, corporate_number: str) -> dict[str, str]:
        websites = {
            "1000000000001": "https://alpha.example/",
            "1000000000002": "https://beta.example",
        }
        return {
            "法人番号": corporate_number,
            "公式サイトURL": websites[corporate_number],
            "スコープ": "",
            "データ世代": "",
            "正本照合": "",
        }

    def write_manifest(self, path: Path, *corporate_numbers: str) -> None:
        module.write_csv(
            path,
            ["法人番号", "公式サイトURL", "スコープ", "データ世代", "正本照合"],
            [self.manifest_row(number) for number in corporate_numbers],
        )

    def test_prepare_shard_rejects_invalid_ranges(self) -> None:
        with self.assertRaisesRegex(ValueError, "offset"):
            module.prepare_shard(
                companies_csv=self.companies,
                database=self.root / "negative.sqlite3",
                manifest=self.root / "negative.csv",
                offset=-1,
                limit=1,
                summary=None,
            )
        with self.assertRaisesRegex(ValueError, "limit"):
            module.prepare_shard(
                companies_csv=self.companies,
                database=self.root / "zero.sqlite3",
                manifest=self.root / "zero.csv",
                offset=0,
                limit=0,
                summary=None,
            )

    def test_prepare_shard_prioritizes_and_builds_compatible_db(self) -> None:
        database = self.root / "shard.sqlite3"
        manifest = self.root / "manifest.csv"
        summary = self.root / "summary.json"
        result = module.prepare_shard(
            companies_csv=self.companies,
            database=database,
            manifest=manifest,
            offset=1,
            limit=1,
            summary=summary,
        )
        self.assertEqual(result["companies_with_web"], 2)
        self.assertEqual(result["selected"], 1)
        self.assertEqual(result["integrity"], "ok")
        connection = sqlite3.connect(database)
        try:
            row = connection.execute(
                "SELECT c.company_name,m.corporate_number,p.website_url "
                "FROM companies c JOIN corporate_matches m USING(source_id) "
                "JOIN public_master p USING(corporate_number)"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(row, ("Beta Systems", "1000000000002", "https://beta.example"))
        manifest_rows = module.read_csv(manifest)
        self.assertEqual(manifest_rows[0]["法人番号"], "1000000000002")
        self.assertEqual(json.loads(summary.read_text(encoding="utf-8"))["integrity"], "ok")

    def test_merge_preserves_multiple_candidates_and_progress_states(self) -> None:
        manifest = self.root / "manifest.csv"
        self.write_manifest(manifest, "1000000000001", "1000000000002")
        phones = self.root / "phones.csv"
        module.write_csv(
            phones,
            [
                "法人番号",
                "候補順位",
                "電話番号",
                "電話種別候補",
                "根拠URL",
                "根拠テキスト",
                "抽出方法",
                "信頼度",
                "取得日時",
            ],
            [
                {
                    "法人番号": "1000000000001",
                    "候補順位": "1",
                    "電話番号": "03-1234-5678",
                    "電話種別候補": "代表電話",
                    "根拠URL": "https://alpha.example/company",
                    "根拠テキスト": "会社概要 代表電話",
                    "抽出方法": "text",
                    "信頼度": "0.95",
                    "取得日時": "2026-08-21T00:00:00Z",
                },
                {
                    "法人番号": "1000000000001",
                    "候補順位": "2",
                    "電話番号": "0120-000-001",
                    "電話種別候補": "問い合わせ電話",
                    "根拠URL": "https://alpha.example/contact",
                    "根拠テキスト": "サービスのお問い合わせ",
                    "抽出方法": "text",
                    "信頼度": "0.75",
                    "取得日時": "2026-08-21T00:00:01Z",
                },
            ],
        )
        output = self.root / "output.csv"
        summary = self.root / "merge.json"
        result = module.merge_batches(
            all_companies_csv=self.companies,
            manifests=[str(manifest)],
            phone_files=[str(phones)],
            legacy_manifest_completion=True,
            output=output,
            summary=summary,
        )
        self.assertEqual(result["companies"], 3)
        self.assertEqual(result["companies_with_web"], 2)
        self.assertEqual(result["processed_for_phone"], 2)
        self.assertEqual(result["companies_with_phone_candidates"], 1)
        self.assertEqual(result["phone_candidates_total"], 2)
        rows = {row["法人番号"]: row for row in module.read_csv(output)}
        alpha = rows["1000000000001"]
        self.assertEqual(alpha["収集状態"], "phone_candidate_found")
        self.assertEqual(alpha["電話番号数字"], "0312345678")
        self.assertEqual(alpha["電話種別候補"], "代表電話")
        self.assertEqual(alpha["電話候補件数"], "2")
        payload = json.loads(alpha["電話候補一覧JSON"])
        self.assertEqual([item["phone_digits"] for item in payload], ["0312345678", "0120000001"])
        self.assertEqual(rows["1000000000002"]["収集状態"], "processed_no_phone")
        self.assertEqual(rows["1000000000003"]["収集状態"], "website_missing")

    def test_merge_uses_completed_progress_instead_of_manifest_as_processing_proof(self) -> None:
        manifest = self.root / "manifest.csv"
        self.write_manifest(manifest, "1000000000001", "1000000000002")
        progress = self.root / "progress.jsonl"
        progress.write_text(
            json.dumps({
                "schema_version": 1,
                "corporate_number": "1000000000001",
                "official_site_url": "https://alpha.example/",
                "state": "phone_candidate_found",
                "candidates": [{
                    "phone": "0312345678",
                    "candidate_type": "代表電話",
                    "url": "https://alpha.example/company",
                    "context": "会社概要 代表電話",
                    "source": "text",
                    "score": 0.95,
                }],
                "completed_at": "2026-08-24T00:00:00Z",
            }, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        output = self.root / "output.csv"
        summary = self.root / "merge.json"

        result = module.merge_batches(
            all_companies_csv=self.companies,
            manifests=[str(manifest)],
            phone_files=[],
            progress_files=[str(progress)],
            scope_label="G37-G41",
            output=output,
            summary=summary,
        )

        self.assertEqual(result["targeted_for_phone"], 2)
        self.assertEqual(result["processed_for_phone"], 1)
        self.assertEqual(result["scope"], "G37-G41")
        rows = {row["法人番号"]: row for row in module.read_csv(output)}
        self.assertEqual(rows["1000000000001"]["収集状態"], "phone_candidate_found")
        self.assertEqual(rows["1000000000002"]["収集状態"], "website_pending")

    def test_merge_reports_fax_only_separately_from_voice_candidates(self) -> None:
        manifest = self.root / "manifest.csv"
        self.write_manifest(manifest, "1000000000001")
        progress = self.root / "progress.jsonl"
        progress.write_text(
            json.dumps({
                "schema_version": 1,
                "corporate_number": "1000000000001",
                "official_site_url": "https://alpha.example/",
                "state": "fax_only",
                "candidates": [{
                    "phone": "0312345678",
                    "candidate_type": "FAX",
                    "url": "https://alpha.example/company",
                    "context": "FAX 03-1234-5678",
                    "source": "text",
                    "score": 0.0,
                }],
                "completed_at": "2026-08-24T00:00:00Z",
            }) + "\n",
            encoding="utf-8",
        )
        output = self.root / "output.csv"
        result = module.merge_batches(
            all_companies_csv=self.companies,
            manifests=[str(manifest)],
            phone_files=[],
            progress_files=[str(progress)],
            output=output,
            summary=self.root / "summary.json",
        )

        rows = {row["法人番号"]: row for row in module.read_csv(output)}
        self.assertEqual(rows["1000000000001"]["収集状態"], "fax_only")
        self.assertEqual(result["fax_only_companies"], 1)
        self.assertEqual(result["companies_with_voice_candidates"], 0)

    def test_merge_fails_closed_when_requested_progress_artifact_is_missing(self) -> None:
        manifest = self.root / "manifest.csv"
        self.write_manifest(manifest, "1000000000001")

        with self.assertRaisesRegex(FileNotFoundError, "progress"):
            module.merge_batches(
                all_companies_csv=self.companies,
                manifests=[str(manifest)],
                phone_files=[],
                progress_files=[str(self.root / "missing-progress-*.jsonl")],
                output=self.root / "output.csv",
                summary=self.root / "summary.json",
            )

    def test_merge_requires_progress_unless_legacy_mode_is_explicit(self) -> None:
        manifest = self.root / "manifest.csv"
        self.write_manifest(manifest, "1000000000001")

        with self.assertRaisesRegex(ValueError, "progress.*required"):
            module.merge_batches(
                all_companies_csv=self.companies,
                manifests=[str(manifest)],
                phone_files=[],
                output=self.root / "output.csv",
                summary=self.root / "summary.json",
            )

    def test_merge_rejects_progress_from_another_dataset_generation(self) -> None:
        manifest = self.root / "manifest.csv"
        self.write_manifest(manifest, "1000000000001")
        progress = self.root / "progress.jsonl"
        progress.write_text(
            json.dumps({
                "schema_version": 1,
                "corporate_number": "1000000000001",
                "official_site_url": "https://alpha.example/",
                "dataset_generation": "stale-generation",
                "state": "no_phone_found",
                "candidates": [],
                "completed_at": "2026-08-24T00:00:00Z",
            }) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "dataset_generation mismatch"):
            module.merge_batches(
                all_companies_csv=self.companies,
                manifests=[str(manifest)],
                phone_files=[],
                progress_files=[str(progress)],
                output=self.root / "output.csv",
                summary=self.root / "summary.json",
            )


if __name__ == "__main__":
    unittest.main()
