from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import official_site_phone_enricher as phone


class PhoneProgressTests(unittest.TestCase):
    def test_resume_rejects_progress_bound_to_a_different_official_host(self) -> None:
        targets = [{
            "source_id": "alpha",
            "company_name": "Alpha",
            "corporate_number": "1000000000001",
            "website_url": "https://alpha.example/",
        }]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            progress = root / "progress.jsonl"
            progress.write_text(json.dumps({
                "schema_version": 1,
                "corporate_number": "1000000000001",
                "official_site_url": "https://wrong.example/",
                "state": "processed_no_phone",
                "candidates": [],
                "completed_at": "2026-08-24T00:00:00Z",
            }) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "official site mismatch"):
                phone.collect_targets(
                    targets,
                    session=object(),
                    output=root / "phones.csv",
                    progress=progress,
                    max_pages=4,
                    max_candidates=5,
                    timeout=20,
                    sleep_s=0,
                    resume=True,
                )

    def test_empty_shard_still_emits_resume_and_output_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "phones.csv"
            progress = root / "progress.jsonl"

            result = phone.collect_targets(
                [],
                session=object(),
                output=output,
                progress=progress,
                max_pages=4,
                max_candidates=5,
                timeout=20,
                sleep_s=0,
                resume=True,
            )

            self.assertEqual(result["targets"], 0)
            self.assertTrue(progress.is_file())
            self.assertEqual(progress.read_text(encoding="utf-8"), "")
            self.assertTrue(output.is_file())

    def test_collection_progress_is_resumable_and_is_the_candidate_source_of_truth(self) -> None:
        targets = [
            {
                "source_id": "alpha",
                "company_name": "Alpha Systems",
                "corporate_number": "1000000000001",
                "website_url": "https://alpha.example/",
            },
            {
                "source_id": "beta",
                "company_name": "Beta Systems",
                "corporate_number": "1000000000002",
                "website_url": "https://beta.example/",
            },
        ]
        calls: list[str] = []

        def discoverer(_session, website: str, _max_pages: int, _timeout: float, _sleep_s: float):
            calls.append(website)
            if "alpha" in website:
                return {
                    "state": "phone_candidate_found",
                    "pages_fetched": 1,
                    "reason": None,
                    "candidates": [{
                        "phone": "0312345678",
                        "candidate_type": "代表電話",
                        "url": "https://alpha.example/company",
                        "context": "会社概要 代表電話",
                        "source": "text",
                        "score": 0.95,
                    }],
                }
            return {"state": "processed_no_phone", "pages_fetched": 1, "reason": None, "candidates": []}

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "phones.csv"
            progress = root / "progress.jsonl"
            first = phone.collect_targets(
                targets,
                session=object(),
                output=output,
                progress=progress,
                max_pages=4,
                max_candidates=5,
                timeout=20,
                sleep_s=0,
                resume=True,
                discoverer=discoverer,
            )
            self.assertEqual(first["attempted_this_run"], 2)
            self.assertEqual(first["already_completed"], 0)
            self.assertEqual(first["states"], {"phone_candidate_found": 1, "processed_no_phone": 1})
            self.assertEqual(calls, ["https://alpha.example/", "https://beta.example/"])
            progress_rows = [json.loads(line) for line in progress.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(progress_rows), 2)
            with output.open("r", encoding="utf-8-sig", newline="") as handle:
                candidate_rows = list(csv.DictReader(handle))
            self.assertEqual(len(candidate_rows), 1)
            self.assertEqual(candidate_rows[0]["法人番号"], "1000000000001")
            self.assertEqual(candidate_rows[0]["電話番号"], "0312345678")

            def must_not_run(*_args, **_kwargs):
                raise AssertionError("completed targets must be skipped on resume")

            second = phone.collect_targets(
                targets,
                session=object(),
                output=output,
                progress=progress,
                max_pages=4,
                max_candidates=5,
                timeout=20,
                sleep_s=0,
                resume=True,
                discoverer=must_not_run,
            )
            self.assertEqual(second["attempted_this_run"], 0)
            self.assertEqual(second["already_completed"], 2)
            self.assertEqual(second["states"], {"phone_candidate_found": 1, "processed_no_phone": 1})

    def test_retry_state_reprocesses_only_explicitly_selected_failures(self) -> None:
        targets = [
            {"source_id": "alpha", "company_name": "Alpha", "corporate_number": "1000000000001", "website_url": "https://alpha.example/"},
            {"source_id": "beta", "company_name": "Beta", "corporate_number": "1000000000002", "website_url": "https://beta.example/"},
        ]
        calls: list[str] = []

        def discoverer(_session, website: str, _max_pages: int, _timeout: float, _sleep_s: float):
            calls.append(website)
            return {"state": "processed_no_phone", "pages_fetched": 1, "reason": None, "candidates": []}

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            progress = root / "progress.jsonl"
            progress.write_text(
                "\n".join([
                    json.dumps({"schema_version": 1, "corporate_number": "1000000000001", "official_site_url": "https://alpha.example/", "state": "needs_review", "candidates": [], "completed_at": "2026-08-24T00:00:00Z"}),
                    json.dumps({"schema_version": 1, "corporate_number": "1000000000002", "official_site_url": "https://beta.example/", "state": "processed_no_phone", "candidates": [], "completed_at": "2026-08-24T00:00:00Z"}),
                ]) + "\n",
                encoding="utf-8",
            )

            result = phone.collect_targets(
                targets,
                session=object(),
                output=root / "phones.csv",
                progress=progress,
                max_pages=4,
                max_candidates=5,
                timeout=20,
                sleep_s=0,
                resume=True,
                retry_states={"needs_review"},
                discoverer=discoverer,
            )

            self.assertEqual(calls, ["https://alpha.example/"])
            self.assertEqual(result["attempted_this_run"], 1)
            self.assertEqual(result["retried_this_run"], 1)
            self.assertEqual(result["states"], {"processed_no_phone": 2})

    def test_resume_repairs_a_truncated_tail_before_appending_new_progress(self) -> None:
        targets = [
            {"source_id": "alpha", "company_name": "Alpha", "corporate_number": "1000000000001", "website_url": "https://alpha.example/"},
            {"source_id": "beta", "company_name": "Beta", "corporate_number": "1000000000002", "website_url": "https://beta.example/"},
        ]

        def discoverer(_session, _website: str, _max_pages: int, _timeout: float, _sleep_s: float):
            return {"state": "processed_no_phone", "pages_fetched": 1, "reason": None, "candidates": []}

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            progress = root / "progress.jsonl"
            valid = json.dumps({
                "schema_version": 1,
                "corporate_number": "1000000000001",
                "official_site_url": "https://alpha.example/",
                "state": "processed_no_phone",
                "candidates": [],
                "completed_at": "2026-08-24T00:00:00Z",
            })
            progress.write_text(valid + "\n" + '{"schema_version":1', encoding="utf-8")

            result = phone.collect_targets(
                targets,
                session=object(),
                output=root / "phones.csv",
                progress=progress,
                max_pages=4,
                max_candidates=5,
                timeout=20,
                sleep_s=0,
                resume=True,
                discoverer=discoverer,
            )
            latest, ignored = phone.load_progress(progress)

            self.assertEqual(result["ignored_truncated_tail_lines"], 1)
            self.assertEqual(result["attempted_this_run"], 1)
            self.assertEqual(set(latest), {"1000000000001", "1000000000002"})
            self.assertEqual(ignored, 0)


if __name__ == "__main__":
    unittest.main()
