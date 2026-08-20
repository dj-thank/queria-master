from __future__ import annotations

"""Persistent, portable application settings and artifact resolution.

The canonical, enrichment and runtime databases have different roles.  This
module resolves them as one explicit bundle so GUI/CLI callers cannot silently
pair a canonical database with an index built from the runtime database.
"""

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping


SETTINGS_SCHEMA_VERSION = 1


class SettingsError(RuntimeError):
    """Saved application settings are invalid or cannot be persisted."""


@dataclass(frozen=True)
class AppSettings:
    schema_version: int = SETTINGS_SCHEMA_VERSION
    home: str | None = None
    canonical_database: str = "data/queria_master.duckdb"
    enrichment_database: str = "data/queria_enrichment.duckdb"
    runtime_database: str = "data/queria_runtime.duckdb"
    search_index: str = "data/search.sqlite"
    default_limit: int = 1000
    validate_index: bool = True

    def __post_init__(self) -> None:
        if self.schema_version != SETTINGS_SCHEMA_VERSION:
            raise SettingsError(
                f"設定schema_version={self.schema_version}は未対応です。"
                f"対応版={SETTINGS_SCHEMA_VERSION}"
            )
        if not 1 <= int(self.default_limit) <= 100_000:
            raise SettingsError("default_limit は1〜100000で指定してください。")
        for field_name in (
            "canonical_database",
            "enrichment_database",
            "runtime_database",
            "search_index",
        ):
            if not str(getattr(self, field_name)).strip():
                raise SettingsError(f"{field_name} は空にできません。")


@dataclass(frozen=True)
class ResolvedArtifacts:
    home: Path
    canonical_database: Path
    enrichment_database: Path
    runtime_database: Path
    search_index: Path
    default_limit: int
    validate_index: bool
    origins: Mapping[str, str]


def default_settings_path(home: Path) -> Path:
    return Path(home).expanduser() / "config" / "queria-settings.json"


def load_settings(path: Path) -> AppSettings:
    path = Path(path)
    if not path.is_file():
        return AppSettings()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SettingsError(f"設定ファイルを読めません: {path}") from exc
    if not isinstance(payload, dict):
        raise SettingsError("設定ファイルのルートはJSON objectである必要があります。")
    allowed = set(AppSettings.__dataclass_fields__)
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise SettingsError(f"未知の設定項目があります: {', '.join(unknown)}")
    try:
        return AppSettings(**payload)
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"設定値が不正です: {path}") from exc


def save_settings(path: Path, settings: AppSettings) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(asdict(settings), ensure_ascii=False, indent=2) + "\n"
    try:
        temporary.write_text(payload, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise SettingsError(f"設定ファイルを保存できません: {path}") from exc


def _resolved_path(value: str | Path, home: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = home / path
    return path.resolve()


def resolve_artifacts(
    settings: AppSettings,
    *,
    fallback_home: Path,
    environment: Mapping[str, str] | None = None,
    explicit: Mapping[str, str | Path | None] | None = None,
) -> ResolvedArtifacts:
    """Resolve one coherent artifact bundle with deterministic precedence.

    Precedence is explicit caller override, field-specific environment,
    persisted setting, then the setting default.  ``QUERIA_MASTER_HOME``
    overrides a saved home so a moved portable ZIP can be redirected without
    rewriting its settings file.
    """

    env = os.environ if environment is None else environment
    overrides = {} if explicit is None else explicit
    env_home = str(env.get("QUERIA_MASTER_HOME", "")).strip()
    if env_home:
        home = Path(env_home).expanduser().resolve()
        home_origin = "environment"
    elif settings.home and settings.home.strip():
        home = Path(settings.home).expanduser()
        if not home.is_absolute():
            home = Path(fallback_home).expanduser() / home
        home = home.resolve()
        home_origin = "settings"
    else:
        home = Path(fallback_home).expanduser().resolve()
        home_origin = "automatic"

    field_environment = {
        "canonical_database": "QUERIA_CANONICAL_DB",
        "enrichment_database": "QUERIA_ENRICHMENT_DB",
        "runtime_database": "QUERIA_RUNTIME_DB",
        "search_index": "QUERIA_SEARCH_INDEX",
    }
    values: dict[str, Path] = {}
    origins: dict[str, str] = {"home": home_origin}
    for field_name, env_name in field_environment.items():
        explicit_value = overrides.get(field_name)
        environment_value = str(env.get(env_name, "")).strip()
        if explicit_value not in (None, ""):
            raw_value = explicit_value
            origin = "explicit"
        elif environment_value:
            raw_value = environment_value
            origin = "environment"
        else:
            raw_value = getattr(settings, field_name)
            origin = "settings"
        values[field_name] = _resolved_path(raw_value, home)
        origins[field_name] = origin

    return ResolvedArtifacts(
        home=home,
        canonical_database=values["canonical_database"],
        enrichment_database=values["enrichment_database"],
        runtime_database=values["runtime_database"],
        search_index=values["search_index"],
        default_limit=int(settings.default_limit),
        validate_index=bool(settings.validate_index),
        origins=origins,
    )


__all__ = [
    "AppSettings",
    "ResolvedArtifacts",
    "SETTINGS_SCHEMA_VERSION",
    "SettingsError",
    "default_settings_path",
    "load_settings",
    "resolve_artifacts",
    "save_settings",
]
