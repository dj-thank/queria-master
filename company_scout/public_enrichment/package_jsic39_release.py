#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Package a public JSIC 39 contact batch into auditable split ZIP assets."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

CSV_ENCODING = "utf-8-sig"
PROHIBITED_MARKERS = (
    "fumadata.com",
    "fuma_id",
    "/_next/data/",
    "buildid",
)
RELEASE_TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def now_iso() -> str:
    source_date_epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if source_date_epoch is not None:
        try:
            instant = datetime.fromtimestamp(int(source_date_epoch), timezone.utc)
        except (OverflowError, ValueError) as exc:
            raise RuntimeError("SOURCE_DATE_EPOCH must be a valid Unix timestamp") from exc
        return instant.replace(microsecond=0).isoformat()
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def count_csv_rows(path: Path) -> int:
    with path.open("r", encoding=CSV_ENCODING, newline="") as handle:
        return sum(1 for _ in csv.DictReader(handle))


def assert_public_safe(path: Path) -> None:
    text = path.read_text(encoding=CSV_ENCODING, errors="ignore").lower()
    hits = [marker for marker in PROHIBITED_MARKERS if marker in text]
    if hits:
        raise RuntimeError(f"prohibited source-specific markers in {path}: {hits}")


def write_zip(zip_path: Path, files: Iterable[tuple[Path, str]]) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for source, archive_name in files:
            if source.is_symlink() or not source.is_file():
                raise RuntimeError(f"release input must be a regular file: {source}")
            normalized_name = PurePosixPath(archive_name)
            if normalized_name.is_absolute() or ".." in normalized_name.parts:
                raise RuntimeError(f"unsafe ZIP member name: {archive_name}")
            info = zipfile.ZipInfo(normalized_name.as_posix(), date_time=ZIP_TIMESTAMP)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(
                info,
                source.read_bytes(),
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            )


def split_contacts_csv(source: Path, output_dir: Path, rows_per_part: int) -> list[dict[str, Any]]:
    if rows_per_part < 1:
        raise ValueError("rows_per_part must be >= 1")
    assets: list[dict[str, Any]] = []
    with source.open("r", encoding=CSV_ENCODING, newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        if not fieldnames:
            raise RuntimeError(f"CSV has no header: {source}")
        part_index = 0
        row_start = 1
        while True:
            rows = []
            for _ in range(rows_per_part):
                try:
                    rows.append(next(reader))
                except StopIteration:
                    break
            if not rows:
                break
            part_index += 1
            row_end = row_start + len(rows) - 1
            stem = f"jsic39_public_contacts_part-{part_index:04d}_rows-{row_start:06d}-{row_end:06d}"
            with tempfile.TemporaryDirectory() as temporary:
                csv_path = Path(temporary) / f"{stem}.csv"
                with csv_path.open("w", encoding=CSV_ENCODING, newline="") as output:
                    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
                    writer.writeheader()
                    writer.writerows(rows)
                zip_path = output_dir / f"{stem}.zip"
                write_zip(zip_path, [(csv_path, csv_path.name)])
            assets.append(
                {
                    "name": zip_path.name,
                    "kind": "contacts_part",
                    "rows": len(rows),
                    "row_start": row_start,
                    "row_end": row_end,
                    "bytes": zip_path.stat().st_size,
                    "sha256": sha256_file(zip_path),
                }
            )
            row_start = row_end + 1
    return assets


def package_complete_contacts(source: Path, output_dir: Path) -> dict[str, Any]:
    zip_path = output_dir / "jsic39_public_contacts_complete.zip"
    write_zip(zip_path, [(source, "jsic39_public_contacts.csv")])
    return {
        "name": zip_path.name,
        "kind": "contacts_complete",
        "rows": count_csv_rows(source),
        "bytes": zip_path.stat().st_size,
        "sha256": sha256_file(zip_path),
    }


def package_phone_subset(source: Path, output_dir: Path) -> dict[str, Any]:
    with source.open("r", encoding=CSV_ENCODING, newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = [row for row in reader if (row.get("電話番号候補") or "").strip()]
    with tempfile.TemporaryDirectory() as temporary:
        csv_path = Path(temporary) / "jsic39_phone_candidates_found.csv"
        with csv_path.open("w", encoding=CSV_ENCODING, newline="") as output:
            writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        zip_path = output_dir / "jsic39_phone_candidates_found.zip"
        write_zip(zip_path, [(csv_path, csv_path.name)])
    return {
        "name": zip_path.name,
        "kind": "phone_candidates",
        "rows": len(rows),
        "bytes": zip_path.stat().st_size,
        "sha256": sha256_file(zip_path),
    }


def package_evidence(evidence_root: Path, output_dir: Path, batch_start: int, batch_end: int) -> dict[str, Any]:
    allowed_names = {"manifest.csv", "phones.csv", "prepare_summary.json"}
    files: list[tuple[Path, str]] = []
    for path in sorted(evidence_root.rglob("*")):
        if path.is_file() and path.name in allowed_names:
            relative = path.relative_to(evidence_root)
            files.append((path, str(relative)))
    zip_path = output_dir / f"jsic39_phone_evidence_batch-{batch_start:06d}-{batch_end:06d}.zip"
    write_zip(zip_path, files)
    phone_rows = sum(count_csv_rows(path) for path, _ in files if path.name == "phones.csv")
    manifest_rows = sum(count_csv_rows(path) for path, _ in files if path.name == "manifest.csv")
    return {
        "name": zip_path.name,
        "kind": "evidence",
        "manifest_rows": manifest_rows,
        "phone_candidate_rows": phone_rows,
        "files": len(files),
        "bytes": zip_path.stat().st_size,
        "sha256": sha256_file(zip_path),
    }


def package_verified_reference(reference: Path | None, readme: Path | None, output_dir: Path) -> dict[str, Any] | None:
    if reference is None or not reference.exists():
        return None
    files: list[tuple[Path, str]] = [(reference, "verified_public_contacts.csv")]
    if readme and readme.exists():
        files.append((readme, "README.md"))
    zip_path = output_dir / "verified_public_contacts_reference.zip"
    write_zip(zip_path, files)
    return {
        "name": zip_path.name,
        "kind": "verified_reference",
        "rows": count_csv_rows(reference),
        "bytes": zip_path.stat().st_size,
        "sha256": sha256_file(zip_path),
    }


def write_release_readme(
    path: Path,
    *,
    release_tag: str,
    batch_start: int,
    batch_end: int,
    contacts_rows: int,
    export_summary: dict[str, Any],
    collection_summary: dict[str, Any],
    generated_at: str,
) -> None:
    path.write_text(
        "\n".join(
            [
                f"# JSIC 39 Public Contacts — {release_tag}",
                "",
                "日本標準産業分類・中分類39（情報サービス業）について、公開法人情報と企業公式サイトから構築した公開データです。",
                "",
                "## 収録内容",
                "",
                f"- 全体CSV行数: {contacts_rows:,}",
                f"- 公式サイト巡回対象順位: {batch_start:,}–{batch_end:,}",
                f"- 今回の巡回処理数: {collection_summary.get('processed_for_phone', 0):,}",
                f"- 電話候補を得た企業数: {collection_summary.get('companies_with_phone_candidates', 0):,}",
                f"- 電話候補総数: {collection_summary.get('phone_candidates_total', 0):,}",
                f"- 公開法人母集団: {export_summary.get('companies', contacts_rows):,}",
                f"- 公開URL確認企業: {export_summary.get('companies_with_public_url', 0):,}",
                "",
                "## ZIP構成",
                "",
                "- `jsic39_public_contacts_complete.zip`: 全行を1ファイルにまとめた版",
                "- `jsic39_public_contacts_part-*.zip`: 5,000行単位の分割版",
                "- `jsic39_phone_candidates_found.zip`: 電話候補が見つかった企業だけ",
                "- `jsic39_phone_evidence_batch-*.zip`: シャード別の対象・候補・根拠",
                "- `verified_public_contacts_reference.zip`: 人手確認済み公式連絡先リファレンス",
                "- `MANIFEST.json` / `SHA256SUMS.txt`: 件数・サイズ・ハッシュ",
                "",
                "## 注意",
                "",
                "電話番号は公式サイト上の候補です。代表、本社、問い合わせ、採用、サポート、広報・IR、支店、FAX等の用途候補と根拠URLを併記しています。利用前に根拠ページを確認してください。",
                "",
                "この公開物には、特定の民間企業データベース固有ID、非公開API情報、元の非公開企業リスト、Cookie、APIキーを含めていません。",
                "",
                f"生成日時: {generated_at}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def build_release(
    *,
    contacts: Path,
    collection_summary_path: Path,
    export_summary_path: Path,
    evidence_root: Path,
    output_dir: Path,
    rows_per_part: int,
    batch_start: int,
    batch_size: int,
    shard_count: int,
    release_tag: str,
    verified_reference: Path | None,
    verified_readme: Path | None,
) -> dict[str, Any]:
    if rows_per_part < 1:
        raise ValueError("rows_per_part must be >= 1")
    if batch_start < 0:
        raise ValueError("batch_start must be >= 0")
    if batch_size < 1 or shard_count < 1:
        raise ValueError("batch_size and shard_count must be >= 1")
    if RELEASE_TAG.fullmatch(release_tag) is None:
        raise ValueError("release_tag contains unsupported characters")
    required_files = (contacts, collection_summary_path, export_summary_path)
    missing = [str(path) for path in required_files if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"missing release inputs: {missing}")
    if not evidence_root.is_dir():
        raise FileNotFoundError(f"missing evidence directory: {evidence_root}")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"output directory must be empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    for path in required_files:
        assert_public_safe(path)
    for path in sorted(evidence_root.rglob("*")):
        if path.is_file() and path.name in {"manifest.csv", "phones.csv", "prepare_summary.json"}:
            assert_public_safe(path)
    if verified_reference and verified_reference.exists():
        assert_public_safe(verified_reference)
    if verified_readme and verified_readme.exists():
        assert_public_safe(verified_readme)

    generated_at = now_iso()
    collection_summary = read_json(collection_summary_path)
    export_summary = read_json(export_summary_path)
    contacts_rows = count_csv_rows(contacts)
    batch_end = batch_start + batch_size * shard_count - 1

    assets: list[dict[str, Any]] = []
    assets.append(package_complete_contacts(contacts, output_dir))
    assets.extend(split_contacts_csv(contacts, output_dir, rows_per_part))
    assets.append(package_phone_subset(contacts, output_dir))
    assets.append(package_evidence(evidence_root, output_dir, batch_start, batch_end))
    verified = package_verified_reference(verified_reference, verified_readme, output_dir)
    if verified:
        assets.append(verified)

    release_readme = output_dir / "README.md"
    write_release_readme(
        release_readme,
        release_tag=release_tag,
        batch_start=batch_start,
        batch_end=batch_end,
        contacts_rows=contacts_rows,
        export_summary=export_summary,
        collection_summary=collection_summary,
        generated_at=generated_at,
    )

    for source, name in (
        (collection_summary_path, "jsic39_collection_summary.json"),
        (export_summary_path, "export_summary.json"),
    ):
        destination = output_dir / name
        shutil.copy2(source, destination)
        assets.append(
            {
                "name": destination.name,
                "kind": "summary",
                "bytes": destination.stat().st_size,
                "sha256": sha256_file(destination),
            }
        )

    assets.append(
        {
            "name": release_readme.name,
            "kind": "documentation",
            "bytes": release_readme.stat().st_size,
            "sha256": sha256_file(release_readme),
        }
    )
    manifest = {
        "schema_version": 1,
        "release_tag": release_tag,
        "generated_at": generated_at,
        "jsic_middle_code": "39",
        "batch_start": batch_start,
        "batch_end": batch_end,
        "batch_size_per_shard": batch_size,
        "shard_count": shard_count,
        "contacts_rows": contacts_rows,
        "collection_summary": collection_summary,
        "export_summary": export_summary,
        "assets": assets,
    }
    manifest_path = output_dir / "MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    checksum_lines = []
    for path in sorted(output_dir.iterdir()):
        if path.is_file() and path.name not in {"SHA256SUMS.txt"}:
            checksum_lines.append(f"{sha256_file(path)}  {path.name}")
    checksums = output_dir / "SHA256SUMS.txt"
    checksums.write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Package public JSIC 39 contacts into split ZIP assets")
    parser.add_argument("--contacts", type=Path, required=True)
    parser.add_argument("--collection-summary", type=Path, required=True)
    parser.add_argument("--export-summary", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--rows-per-part", type=int, default=5000)
    parser.add_argument("--batch-start", type=int, required=True)
    parser.add_argument("--batch-size", type=int, required=True)
    parser.add_argument("--shard-count", type=int, default=8)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--verified-reference", type=Path)
    parser.add_argument("--verified-readme", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    manifest = build_release(
        contacts=args.contacts,
        collection_summary_path=args.collection_summary,
        export_summary_path=args.export_summary,
        evidence_root=args.evidence_root,
        output_dir=args.output_dir,
        rows_per_part=args.rows_per_part,
        batch_start=args.batch_start,
        batch_size=args.batch_size,
        shard_count=args.shard_count,
        release_tag=args.release_tag,
        verified_reference=args.verified_reference,
        verified_readme=args.verified_readme,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
