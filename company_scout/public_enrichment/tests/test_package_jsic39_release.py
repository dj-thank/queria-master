from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "package_jsic39_release.py"
SPEC = importlib.util.spec_from_file_location("package_jsic39_release", MODULE_PATH)
assert SPEC and SPEC.loader
package_jsic39_release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package_jsic39_release)


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Jsic39ReleasePackagingTests(unittest.TestCase):
    def _inputs(self, root: Path) -> dict[str, Path]:
        contacts = root / "input" / "contacts.csv"
        _write_csv(
            contacts,
            ["法人番号", "会社名", "電話番号候補"],
            [
                {"法人番号": "1234567890123", "会社名": "甲社", "電話番号候補": "03-1111-2222"},
                {"法人番号": "2345678901234", "会社名": "乙社", "電話番号候補": ""},
                {"法人番号": "3456789012345", "会社名": "丙社", "電話番号候補": "06-3333-4444"},
            ],
        )
        collection_summary = root / "input" / "collection_summary.json"
        collection_summary.write_text(
            json.dumps({"processed_for_phone": 3, "companies_with_phone_candidates": 2}),
            encoding="utf-8",
        )
        export_summary = root / "input" / "export_summary.json"
        export_summary.write_text(
            json.dumps({"companies": 3, "companies_with_public_url": 3}),
            encoding="utf-8",
        )
        evidence = root / "evidence" / "shard-0"
        _write_csv(
            evidence / "manifest.csv",
            ["法人番号", "公式URL"],
            [{"法人番号": "1234567890123", "公式URL": "https://example.jp/"}],
        )
        _write_csv(
            evidence / "phones.csv",
            ["法人番号", "電話番号候補", "根拠URL"],
            [
                {
                    "法人番号": "1234567890123",
                    "電話番号候補": "03-1111-2222",
                    "根拠URL": "https://example.jp/contact",
                }
            ],
        )
        (evidence / "prepare_summary.json").write_text('{"companies": 1}', encoding="utf-8")
        return {
            "contacts": contacts,
            "collection_summary": collection_summary,
            "export_summary": export_summary,
            "evidence_root": root / "evidence",
        }

    def _build(self, inputs: dict[str, Path], output: Path) -> dict[str, object]:
        return package_jsic39_release.build_release(
            contacts=inputs["contacts"],
            collection_summary_path=inputs["collection_summary"],
            export_summary_path=inputs["export_summary"],
            evidence_root=inputs["evidence_root"],
            output_dir=output,
            rows_per_part=2,
            batch_start=0,
            batch_size=10,
            shard_count=1,
            release_tag="jsic39-test",
            verified_reference=None,
            verified_readme=None,
        )

    @mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": "1704067200"})
    def test_build_is_reproducible_and_auditable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = self._inputs(root)
            first = root / "release-1"
            second = root / "release-2"

            manifest = self._build(inputs, first)
            self._build(inputs, second)

            first_files = {path.name: path.read_bytes() for path in sorted(first.iterdir())}
            second_files = {path.name: path.read_bytes() for path in sorted(second.iterdir())}
            self.assertEqual(first_files, second_files)
            self.assertEqual(manifest["generated_at"], "2024-01-01T00:00:00+00:00")
            self.assertEqual(manifest["contacts_rows"], 3)

            checksum_lines = (first / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines()
            checksums = dict(line.split("  ", 1)[::-1] for line in checksum_lines)
            for name, checksum in checksums.items():
                self.assertEqual(checksum, _sha256(first / name))

            with zipfile.ZipFile(first / "jsic39_public_contacts_complete.zip") as archive:
                member = archive.infolist()[0]
                self.assertEqual(member.date_time, package_jsic39_release.ZIP_TIMESTAMP)
                self.assertEqual(member.filename, "jsic39_public_contacts.csv")

    def test_rejects_stale_output_and_prohibited_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = self._inputs(root)
            stale_output = root / "release"
            stale_output.mkdir()
            (stale_output / "stale.txt").write_text("old", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "must be empty"):
                self._build(inputs, stale_output)

            (inputs["evidence_root"] / "shard-0" / "prepare_summary.json").write_text(
                '{"source": "fumadata.com"}', encoding="utf-8"
            )
            with self.assertRaisesRegex(RuntimeError, "prohibited"):
                self._build(inputs, root / "clean-release")

    def test_rejects_unsafe_release_tag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = self._inputs(root)
            with self.assertRaisesRegex(ValueError, "release_tag"):
                package_jsic39_release.build_release(
                    contacts=inputs["contacts"],
                    collection_summary_path=inputs["collection_summary"],
                    export_summary_path=inputs["export_summary"],
                    evidence_root=inputs["evidence_root"],
                    output_dir=root / "release",
                    rows_per_part=2,
                    batch_start=0,
                    batch_size=10,
                    shard_count=1,
                    release_tag="../../unsafe",
                    verified_reference=None,
                    verified_readme=None,
                )


if __name__ == "__main__":
    unittest.main()
