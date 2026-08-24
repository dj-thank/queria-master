from __future__ import annotations

import json
from pathlib import Path

from queria_master.app_config import load_settings
from queria_master.cli import _parse_args, main
from queria_master.gbiz_archive import ImportResult
from queria_master.resources import DEFAULT_DB
from queria_master.runtime import DEFAULT_RUNTIME_DB


def test_search_commands_default_to_the_runtime_database():
    assert _parse_args(["daemon"]).db == DEFAULT_RUNTIME_DB
    assert _parse_args(["search"]).db == DEFAULT_RUNTIME_DB
    assert _parse_args(["summary"]).db == DEFAULT_RUNTIME_DB
    assert _parse_args(["sql", "--query", "SELECT 1"]).db == DEFAULT_RUNTIME_DB


def test_build_commands_keep_the_canonical_database_default():
    assert _parse_args(["refresh"]).db == DEFAULT_DB
    assert _parse_args(["build-runtime"]).db == DEFAULT_DB
    assert _parse_args(["publish-runtime"]).db == DEFAULT_DB
    assert _parse_args(
        ["integrate-public-enrichment", "--staging-db", "stage.sqlite3"]
    ).db == DEFAULT_DB
    assert _parse_args(
        ["import-website-discovery", "--file", "discovery.jsonl"]
    ).db == DEFAULT_DB
    archive = _parse_args(
        [
            "import-gbiz-archive",
            "--archive",
            "hojinjoho.zip",
            "--staging-db",
            "history.duckdb",
        ]
    )
    assert archive.db == DEFAULT_DB
    assert archive.target_industry == "G"


def test_explicit_database_path_overrides_command_default():
    explicit = Path("custom.duckdb")

    assert _parse_args(["--db", str(explicit), "daemon"]).db == explicit


def test_archive_cli_uses_only_explicit_archive_and_staging_paths(monkeypatch, capsys):
    calls = []

    def fake_import(archive, staging, *, target_industry, batch_size):
        calls.append((archive, staging, target_industry, batch_size))
        return ImportResult(
            staging_database=staging,
            import_id="test-import",
            source_sha256="0" * 64,
            source_records=2,
            imported_records=1,
            activity_records=3,
            json_member_count=1,
            json_uncompressed_bytes=123,
        )

    monkeypatch.setattr("queria_master.cli.import_archive_to_staging", fake_import)
    result = main(
        [
            "--db",
            "canonical-must-not-be-used.duckdb",
            "import-gbiz-archive",
            "--archive",
            "history.zip",
            "--staging-db",
            "history-stage.duckdb",
            "--target-industry",
            "ALL",
            "--batch-size",
            "25",
        ]
    )

    assert result == 0
    assert calls == [(Path("history.zip"), Path("history-stage.duckdb"), "ALL", 25)]
    payload = json.loads(capsys.readouterr().out)
    assert payload["staging_database"] == "history-stage.duckdb"
    assert payload["imported_records"] == 1


def test_configure_persists_settings_for_cli_and_desktop(tmp_path: Path, capsys):
    settings_path = tmp_path / "config" / "settings.json"
    portable_home = tmp_path / "portable"

    result = main(
        [
            "--settings",
            str(settings_path),
            "configure",
            "--home",
            str(portable_home),
            "--runtime-db",
            "data/runtime-v9.duckdb",
            "--search-index",
            "data/search-v9.sqlite",
            "--default-limit",
            "500",
        ]
    )

    assert result == 0
    saved = load_settings(settings_path)
    assert saved.home == str(portable_home)
    assert saved.runtime_database == "data/runtime-v9.duckdb"
    assert saved.search_index == "data/search-v9.sqlite"
    assert saved.default_limit == 500
    assert '"saved": true' in capsys.readouterr().out
