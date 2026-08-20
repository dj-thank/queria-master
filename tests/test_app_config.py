from __future__ import annotations

from pathlib import Path

from queria_master.app_config import (
    AppSettings,
    default_settings_path,
    load_settings,
    resolve_artifacts,
    save_settings,
)


def test_settings_round_trip_is_utf8_and_atomic(tmp_path: Path):
    path = tmp_path / "config" / "queria-settings.json"
    settings = AppSettings(
        home="D:/QueriaPortable",
        canonical_database="data/master.duckdb",
        enrichment_database="data/enrichment.duckdb",
        runtime_database="data/runtime.duckdb",
        search_index="data/search.sqlite",
        default_limit=250,
        validate_index=False,
    )

    save_settings(path, settings)

    assert load_settings(path) == settings
    assert not path.with_suffix(path.suffix + ".tmp").exists()


def test_missing_settings_return_safe_portable_defaults(tmp_path: Path):
    settings = load_settings(tmp_path / "missing.json")

    assert settings == AppSettings()
    assert settings.runtime_database == "data/queria_runtime.duckdb"
    assert settings.search_index == "data/search.sqlite"


def test_resolution_precedence_is_explicit_then_environment_then_saved(tmp_path: Path):
    fallback_home = tmp_path / "fallback"
    configured_home = tmp_path / "configured"
    environment_home = tmp_path / "environment"
    explicit_index = tmp_path / "explicit" / "search.sqlite"
    settings = AppSettings(
        home=str(configured_home),
        runtime_database="saved/runtime.duckdb",
        search_index="saved/search.sqlite",
    )

    resolved = resolve_artifacts(
        settings,
        fallback_home=fallback_home,
        environment={
            "QUERIA_MASTER_HOME": str(environment_home),
            "QUERIA_RUNTIME_DB": "env/runtime.duckdb",
            "QUERIA_SEARCH_INDEX": "env/search.sqlite",
        },
        explicit={"search_index": explicit_index},
    )

    assert resolved.home == environment_home.resolve()
    assert resolved.runtime_database == (environment_home / "env/runtime.duckdb").resolve()
    assert resolved.search_index == explicit_index.resolve()
    assert resolved.origins["home"] == "environment"
    assert resolved.origins["runtime_database"] == "environment"
    assert resolved.origins["search_index"] == "explicit"


def test_saved_relative_paths_are_anchored_to_saved_home(tmp_path: Path):
    fallback_home = tmp_path / "fallback"
    configured_home = tmp_path / "portable"
    settings = AppSettings(home=str(configured_home))

    resolved = resolve_artifacts(settings, fallback_home=fallback_home, environment={})

    assert resolved.canonical_database == (configured_home / "data/queria_master.duckdb").resolve()
    assert resolved.enrichment_database == (configured_home / "data/queria_enrichment.duckdb").resolve()
    assert resolved.runtime_database == (configured_home / "data/queria_runtime.duckdb").resolve()
    assert resolved.search_index == (configured_home / "data/search.sqlite").resolve()
    assert default_settings_path(configured_home) == configured_home / "config/queria-settings.json"
