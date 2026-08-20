from __future__ import annotations

from pathlib import Path

from queria_master.app_config import load_settings
from queria_master.cli import _parse_args, main
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


def test_explicit_database_path_overrides_command_default():
    explicit = Path("custom.duckdb")

    assert _parse_args(["--db", str(explicit), "daemon"]).db == explicit


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
