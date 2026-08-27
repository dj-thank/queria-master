from __future__ import annotations

import hashlib
import json
import os
import stat
import zipfile
from pathlib import Path

import duckdb
import pytest

import queria_master.gbiz_archive as gbiz_archive
from queria_master.gbiz_archive import (
    ArchiveLimits,
    ArchiveValidationError,
    StagingDatabaseError,
    import_archive_to_staging,
    iter_normalized_batches,
)


def _write_zip(
    path: Path,
    members: dict[str, bytes],
    *,
    compression: int = zipfile.ZIP_DEFLATED,
) -> None:
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)


def _json_bytes(records: list[dict[str, object]]) -> bytes:
    return json.dumps(records, ensure_ascii=False).encode("utf-8")


def _company(number: int, industry: str = "G", **extra: object) -> dict[str, object]:
    record: dict[str, object] = {
        "corporate_number": f"{number:013d}",
        "name": f"法人{number}",
        "industry": [industry],
    }
    record.update(extra)
    return record


def test_import_creates_provenance_rich_staging_without_promoting_data(tmp_path: Path) -> None:
    archive_path = tmp_path / "Hojinjoho_20260818.zip"
    first_member = _json_bytes(
        [
            _company(
                1_000_000_000_001,
                name="情報通信株式会社",
                location="東京都千代田区",
                capital_stock=10_000_000,
                subsidy=[
                    {
                        "title": "支援事業",
                        "amount": 100,
                        "meta-data": {"key_field": "subsidy-1"},
                    }
                ],
                workplace_info={"base_infos": {"average_age": 40.5}},
                finance={
                    "accounting_standards": "Japan GAAP",
                    "meta-data": {"key_field": "finance-1"},
                    "management_index": [{"period": "2025"}, {"period": "2026"}],
                },
            )
        ]
    )
    second_member = _json_bytes([_company(1_000_000_000_002, industry="E")])
    _write_zip(
        archive_path,
        {
            "data/Hojinjoho_01.json": first_member,
            "data/Hojinjoho_02.json": second_member,
        },
    )
    expected_archive_sha = hashlib.sha256(archive_path.read_bytes()).hexdigest()

    staging = tmp_path / "historical-staging.duckdb"
    result = import_archive_to_staging(archive_path, staging, batch_size=1)

    assert result.staging_database == staging.resolve()
    assert result.source_sha256 == expected_archive_sha
    assert result.source_records == 2
    assert result.imported_records == 1
    assert result.activity_records == 5
    assert result.json_member_count == 2
    assert result.json_uncompressed_bytes == len(first_member) + len(second_member)

    connection = duckdb.connect(str(staging), read_only=True)
    try:
        run = connection.execute(
            """
            SELECT source_path, source_filename, source_sha256, target_industry,
                   source_record_count, imported_record_count, activity_record_count,
                   importer_version
            FROM gbiz_archive.import_runs
            """
        ).fetchone()
        assert run == (
            str(archive_path.resolve()),
            archive_path.name,
            expected_archive_sha,
            "G",
            2,
            1,
            5,
            "1",
        )
        companies = connection.execute(
            """
            SELECT corporate_number, name, capital_stock, industry_codes_json,
                   normalized_record_sha256, normalized_json
            FROM gbiz_archive.companies
            """
        ).fetchall()
        assert len(companies) == 1
        assert companies[0][:4] == (
            "1000000000001",
            "情報通信株式会社",
            "10000000",
            '["G"]',
        )
        assert companies[0][4] == hashlib.sha256(companies[0][5].encode("utf-8")).hexdigest()
        assert connection.execute(
            """
            SELECT activity_type, source_key
            FROM gbiz_archive.activities
            ORDER BY activity_type, activity_index
            """
        ).fetchall() == [
            ("finance.context", "finance-1"),
            ("finance.management_index", "finance-1"),
            ("finance.management_index", "finance-1"),
            ("subsidy", "subsidy-1"),
            ("workplace_info", None),
        ]
        members = connection.execute(
            """
            SELECT member_name, member_sha256
            FROM gbiz_archive.archive_members
            ORDER BY member_index
            """
        ).fetchall()
        assert members == [
            ("data/Hojinjoho_01.json", hashlib.sha256(first_member).hexdigest()),
            ("data/Hojinjoho_02.json", hashlib.sha256(second_member).hexdigest()),
        ]
    finally:
        connection.close()


def test_iterator_is_write_free_and_yields_bounded_batches(tmp_path: Path) -> None:
    archive_path = tmp_path / "archive.zip"
    _write_zip(
        archive_path,
        {
            "Hojinjoho.json": _json_bytes(
                [_company(i, padding="あ" * 60) for i in range(1, 6)]
            )
        },
    )
    expected_sha = hashlib.sha256(archive_path.read_bytes()).hexdigest()

    batches = list(
        iter_normalized_batches(
            archive_path,
            batch_size=100,
            target_industry="ALL",
            limits=ArchiveLimits(
                max_normalized_record_bytes=600,
                max_batch_normalized_bytes=700,
            ),
        )
    )

    assert [len(batch) for batch in batches] == [2, 2, 1]
    assert all(sum(record.normalized_bytes for record in batch) <= 700 for batch in batches)
    records = [record for batch in batches for record in batch]
    assert all(record.source_archive_sha256 == expected_sha for record in records)
    assert [record.source_record_index for record in records] == [1, 2, 3, 4, 5]
    assert list(tmp_path.glob("*.duckdb")) == []


def test_iterator_validates_late_json_failure_before_yielding(tmp_path: Path) -> None:
    archive_path = tmp_path / "late-invalid.zip"
    valid_record = _json_bytes([_company(1)])
    _write_zip(
        archive_path,
        {
            "Hojinjoho_01.json": valid_record,
            "Hojinjoho_02.json": valid_record + b" trailing",
        },
    )

    batches = iter_normalized_batches(archive_path, batch_size=1)
    with pytest.raises(ArchiveValidationError, match="trailing data"):
        next(batches)


@pytest.mark.parametrize(
    "member_name",
    ["../escape.json", "/absolute.json", "C:/drive.json", r"safe\..\escape.json"],
)
def test_zip_slip_paths_are_rejected_without_creating_staging(
    tmp_path: Path,
    member_name: str,
) -> None:
    archive_path = tmp_path / "unsafe.zip"
    _write_zip(archive_path, {member_name: _json_bytes([_company(1)])})
    staging = tmp_path / "stage.duckdb"

    with pytest.raises(ArchiveValidationError, match="unsafe ZIP member path"):
        import_archive_to_staging(archive_path, staging)

    assert not staging.exists()


def test_zip_symlinks_are_rejected(tmp_path: Path) -> None:
    archive_path = tmp_path / "symlink.zip"
    info = zipfile.ZipInfo("Hojinjoho.json")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(info, "target.json")

    with pytest.raises(ArchiveValidationError, match="symbolic links"):
        import_archive_to_staging(archive_path, tmp_path / "stage.duckdb")


def test_member_size_and_compression_ratio_limits_reject_zip_bombs(tmp_path: Path) -> None:
    archive_path = tmp_path / "bomb.zip"
    payload = _json_bytes([_company(1, padding="A" * 20_000)])
    _write_zip(archive_path, {"Hojinjoho.json": payload})

    with pytest.raises(ArchiveValidationError, match="too large"):
        import_archive_to_staging(
            archive_path,
            tmp_path / "size-stage.duckdb",
            limits=ArchiveLimits(max_member_uncompressed_bytes=100),
        )
    with pytest.raises(ArchiveValidationError, match="compression ratio"):
        import_archive_to_staging(
            archive_path,
            tmp_path / "ratio-stage.duckdb",
            limits=ArchiveLimits(max_compression_ratio=2),
        )
    assert not (tmp_path / "size-stage.duckdb").exists()
    assert not (tmp_path / "ratio-stage.duckdb").exists()


def test_activity_amplification_is_bounded(tmp_path: Path) -> None:
    archive_path = tmp_path / "many-activities.zip"
    _write_zip(
        archive_path,
        {
            "Hojinjoho.json": _json_bytes(
                [_company(1, subsidy=[{"title": "a"}, {"title": "b"}])]
            )
        },
    )

    with pytest.raises(ArchiveValidationError, match="normalized activities"):
        import_archive_to_staging(
            archive_path,
            tmp_path / "stage.duckdb",
            limits=ArchiveLimits(max_activities_per_record=1),
        )


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is not available")
def test_non_regular_archive_source_is_rejected_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "archive.zip"
    os.mkfifo(fifo)

    with pytest.raises(ArchiveValidationError, match="not a regular file"):
        list(iter_normalized_batches(fifo))


@pytest.mark.parametrize(
    "payload, message",
    [
        (b'{"corporate_number":"1000000000001"}', "top-level array"),
        (b'[{"corporate_number":"bad","industry":["G"]}]', "not 13 digits"),
        (b'[{"corporate_number":"1000000000001","industry":"G"}]', "array of strings"),
        (b'[{"corporate_number":"1000000000001","industry":["G"]}', "unterminated JSON"),
        (
            b'[{"corporate_number":"1000000000001","industry":["G"],"subsidy":{}}]',
            "must be an array",
        ),
        (
            (
                b'[{"corporate_number":"1000000000001",'
                b'"corporate_number":"1000000000002","industry":["G"]}]'
            ),
            "duplicate JSON object key",
        ),
        (
            b'[{"corporate_number":"1000000000001","industry":["G"],"capital_stock":NaN}]',
            "non-standard JSON number",
        ),
    ],
)
def test_invalid_json_or_record_schema_rolls_back_and_removes_staging(
    tmp_path: Path,
    payload: bytes,
    message: str,
) -> None:
    archive_path = tmp_path / "invalid.zip"
    _write_zip(archive_path, {"Hojinjoho.json": payload})
    staging = tmp_path / "stage.duckdb"

    with pytest.raises(ArchiveValidationError, match=message):
        import_archive_to_staging(archive_path, staging)

    assert not staging.exists()
    assert not Path(str(staging) + ".wal").exists()


def test_existing_database_is_never_opened_or_overwritten(tmp_path: Path) -> None:
    archive_path = tmp_path / "valid.zip"
    _write_zip(archive_path, {"Hojinjoho.json": _json_bytes([_company(1)])})
    canonical = tmp_path / "canonical.duckdb"
    connection = duckdb.connect(str(canonical))
    try:
        connection.execute("CREATE TABLE marker(value VARCHAR)")
        connection.execute("INSERT INTO marker VALUES ('preserve me')")
    finally:
        connection.close()

    before = hashlib.sha256(canonical.read_bytes()).hexdigest()
    with pytest.raises(StagingDatabaseError, match="already exists"):
        import_archive_to_staging(archive_path, canonical)
    after = hashlib.sha256(canonical.read_bytes()).hexdigest()

    assert after == before
    connection = duckdb.connect(str(canonical), read_only=True)
    try:
        assert connection.execute("SELECT value FROM marker").fetchone() == ("preserve me",)
    finally:
        connection.close()


def test_concurrent_target_creation_is_not_deleted_or_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_path = tmp_path / "valid.zip"
    _write_zip(archive_path, {"Hojinjoho.json": _json_bytes([_company(1)])})
    raced_target = tmp_path / "raced.duckdb"

    def create_competing_database(_source: Path, target: Path) -> None:
        connection = duckdb.connect(str(target))
        try:
            connection.execute("CREATE TABLE marker(value VARCHAR)")
            connection.execute("INSERT INTO marker VALUES ('other writer')")
        finally:
            connection.close()
        raise FileExistsError(str(target))

    monkeypatch.setattr(gbiz_archive.os, "link", create_competing_database)
    with pytest.raises(StagingDatabaseError, match="created concurrently"):
        import_archive_to_staging(archive_path, raced_target)

    connection = duckdb.connect(str(raced_target), read_only=True)
    try:
        assert connection.execute("SELECT value FROM marker").fetchone() == ("other writer",)
        assert connection.execute(
            "SELECT count(*) FROM information_schema.schemata WHERE schema_name = 'gbiz_archive'"
        ).fetchone() == (0,)
    finally:
        connection.close()
