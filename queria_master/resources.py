from __future__ import annotations

import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = PACKAGE_ROOT.parent
SOURCE_LAYOUT = (SOURCE_ROOT / "sql" / "remote").is_dir() and (SOURCE_ROOT / "reference").is_dir()
ASSET_ROOT = SOURCE_ROOT if SOURCE_LAYOUT else PACKAGE_ROOT / "assets"

# In a source ZIP, keep data beside the project. When installed as a package,
# use the current directory unless QUERIA_MASTER_HOME explicitly overrides it.
_default_home = SOURCE_ROOT if SOURCE_LAYOUT else Path.cwd()
PROJECT_ROOT = Path(os.environ.get("QUERIA_MASTER_HOME", str(_default_home))).expanduser().resolve()
SQL_ROOT = ASSET_ROOT / "sql"
REFERENCE_ROOT = ASSET_ROOT / "reference"
DEFAULT_DB = PROJECT_ROOT / "data" / "queria_master.duckdb"
DEFAULT_CACHE = PROJECT_ROOT / "cache"

SCOPES = {
    "info-communications": SQL_ROOT / "remote" / "info_communications.sql",
    "gbizinfo-companies": SQL_ROOT / "remote" / "gbizinfo_companies.sql",
    "all-corporations": SQL_ROOT / "remote" / "all_corporations.sql",
}

ALL_PUBLIC_SCOPE = "all-public"
SCOPE_ALIASES = {"all": ALL_PUBLIC_SCOPE}

# These are the public Queria tables included in the all-public snapshot.
# Each table is exported without column projection so newly added source
# columns remain available in the local DB.  ``join_column`` is the stable
# corporate identifier when the source has one; None means an adjacent public
# dataset is retained as a raw relation but is not forced into the company
# master.
PUBLIC_TABLES = {
    "houjin_bangou": {
        "dataset": "houjin_bangou",
        "source_table": "houjin_bangou.main.mart_houjin_bangou",
        "sql_path": SQL_ROOT / "remote" / "public_houjin_bangou.sql",
        "schema": "raw",
        "table": "houjin_bangou",
        "join_column": "corporate_number",
        "role": "法人番号・商号・所在地・法人種別",
    },
    "gbizinfo_company": {
        "dataset": "gbizinfo",
        "source_table": "gbizinfo.main.mart_gbizinfo_company",
        "sql_path": SQL_ROOT / "remote" / "public_gbizinfo_company.sql",
        "schema": "gbizinfo",
        "table": "company_summary",
        "join_column": "corporate_number",
        "role": "法人サマリー・業種・財務/職場の最新指標",
    },
    "gbizinfo_subsidy": {
        "dataset": "gbizinfo",
        "source_table": "gbizinfo.main.mart_gbizinfo_subsidy",
        "sql_path": SQL_ROOT / "remote" / "public_gbizinfo_subsidy.sql",
        "schema": "gbizinfo",
        "table": "subsidies",
        "join_column": "corporate_number",
        "role": "補助金明細",
    },
    "gbizinfo_procurement": {
        "dataset": "gbizinfo",
        "source_table": "gbizinfo.main.mart_gbizinfo_procurement",
        "sql_path": SQL_ROOT / "remote" / "public_gbizinfo_procurement.sql",
        "schema": "gbizinfo",
        "table": "procurements",
        "join_column": "corporate_number",
        "role": "調達明細",
    },
    "gbizinfo_patent": {
        "dataset": "gbizinfo",
        "source_table": "gbizinfo.main.mart_gbizinfo_patent",
        "sql_path": SQL_ROOT / "remote" / "public_gbizinfo_patent.sql",
        "schema": "gbizinfo",
        "table": "patents",
        "join_column": "corporate_number",
        "role": "特許・意匠・商標明細",
    },
    "gbizinfo_certification": {
        "dataset": "gbizinfo",
        "source_table": "gbizinfo.main.mart_gbizinfo_certification",
        "sql_path": SQL_ROOT / "remote" / "public_gbizinfo_certification.sql",
        "schema": "gbizinfo",
        "table": "certifications",
        "join_column": "corporate_number",
        "role": "届出・認定明細",
    },
    "gbizinfo_commendation": {
        "dataset": "gbizinfo",
        "source_table": "gbizinfo.main.mart_gbizinfo_commendation",
        "sql_path": SQL_ROOT / "remote" / "public_gbizinfo_commendation.sql",
        "schema": "gbizinfo",
        "table": "commendations",
        "join_column": "corporate_number",
        "role": "表彰明細",
    },
    "edinet_business_results": {
        "dataset": "edinet",
        "source_table": "edinet.main.mart_business_results",
        "sql_path": SQL_ROOT / "remote" / "public_edinet_business_results.sql",
        "schema": "edinet",
        "table": "business_results",
        "join_column": "corporate_number",
        "role": "EDINET提出会社の主要財務指標",
    },
    "edinet_companies": {
        "dataset": "edinet",
        "source_table": "edinet.main.mart_companies",
        "sql_path": SQL_ROOT / "remote" / "public_edinet_companies.sql",
        "schema": "edinet",
        "table": "companies",
        "join_column": "corporate_number",
        "role": "EDINET提出会社マスター",
    },
    "edinet_documents": {
        "dataset": "edinet",
        "source_table": "edinet.main.mart_documents",
        "sql_path": SQL_ROOT / "remote" / "public_edinet_documents.sql",
        "schema": "edinet",
        "table": "documents",
        "join_column": "corporate_number",
        "role": "EDINET提出書類メタデータ",
    },
    "edinet_funds": {
        "dataset": "edinet",
        "source_table": "edinet.main.mart_funds",
        "sql_path": SQL_ROOT / "remote" / "public_edinet_funds.sql",
        "schema": "edinet",
        "table": "funds",
        "join_column": None,
        "role": "EDINETファンドマスター（法人番号結合対象外）",
    },
    "edinet_financial_facts": {
        "dataset": "edinet",
        "source_table": "edinet.main.stg_financial_facts",
        "sql_path": SQL_ROOT / "remote" / "public_edinet_financial_facts.sql",
        "schema": "edinet",
        "table": "financial_facts",
        "join_column": "corporate_number",
        "role": "EDINET財務ファクト（縦持ち・提出時点）",
    },
    "mhlw_josei_katsuyaku": {
        "dataset": "mhlw",
        "source_table": "mhlw.josei_katsuyaku.company",
        "sql_path": SQL_ROOT / "remote" / "public_mhlw_josei_katsuyaku.sql",
        "schema": "mhlw",
        "table": "josei_katsuyaku_company",
        "join_column": "corporate_number",
        "role": "女性活躍・職場情報",
    },
    "mhlw_kaigo": {
        "dataset": "mhlw",
        "source_table": "mhlw.kaigo.establishment",
        "sql_path": SQL_ROOT / "remote" / "public_mhlw_kaigo.sql",
        "schema": "mhlw",
        "table": "kaigo_establishment",
        "join_column": "corporate_number",
        "role": "介護サービス事業所",
    },
    "mhlw_shougai": {
        "dataset": "mhlw",
        "source_table": "mhlw.shougai.establishment",
        "sql_path": SQL_ROOT / "remote" / "public_mhlw_shougai.sql",
        "schema": "mhlw",
        "table": "shougai_establishment",
        "join_column": "corporate_number",
        "role": "障害福祉サービス事業所",
    },
    "mhlw_ndb_health_checkup": {
        "dataset": "mhlw",
        "source_table": "mhlw.ndb.health_checkup",
        "sql_path": SQL_ROOT / "remote" / "public_mhlw_ndb_health_checkup.sql",
        "schema": "mhlw",
        "table": "ndb_health_checkup",
        "join_column": None,
        "role": "NDB特定健診の地域統計（法人番号結合対象外）",
    },
    "p_portal_procurement_award": {
        "dataset": "p_portal",
        "source_table": "p_portal.main.procurement_award",
        "sql_path": SQL_ROOT / "remote" / "public_p_portal_procurement_award.sql",
        "schema": "p_portal",
        "table": "procurement_award",
        "join_column": "corporate_number",
        "role": "政府電子調達落札実績",
    },
    "metro_tokyo_care_service": {
        "dataset": "metro_tokyo",
        "source_table": "metro_tokyo.ods.care_service",
        "sql_path": SQL_ROOT / "remote" / "public_metro_tokyo_care_service.sql",
        "schema": "metro_tokyo",
        "table": "care_service",
        "join_column": "corporate_number",
        "role": "東京都オープンデータ介護サービス事業所",
    },
    "metro_tokyo_cultural_property": {
        "dataset": "metro_tokyo",
        "source_table": "metro_tokyo.ods.cultural_property",
        "sql_path": SQL_ROOT / "remote" / "public_metro_tokyo_cultural_property.sql",
        "schema": "metro_tokyo",
        "table": "cultural_property",
        "join_column": "corporate_number",
        "role": "東京都オープンデータ文化財",
    },
    "metro_tokyo_event": {
        "dataset": "metro_tokyo",
        "source_table": "metro_tokyo.ods.event",
        "sql_path": SQL_ROOT / "remote" / "public_metro_tokyo_event.sql",
        "schema": "metro_tokyo",
        "table": "event",
        "join_column": "corporate_number",
        "role": "東京都オープンデータイベント",
    },
    "metro_tokyo_food_business": {
        "dataset": "metro_tokyo",
        "source_table": "metro_tokyo.ods.food_business",
        "sql_path": SQL_ROOT / "remote" / "public_metro_tokyo_food_business.sql",
        "schema": "metro_tokyo",
        "table": "food_business",
        "join_column": "corporate_number",
        "role": "東京都オープンデータ食品営業許可・届出",
    },
    "metro_tokyo_public_facility": {
        "dataset": "metro_tokyo",
        "source_table": "metro_tokyo.ods.public_facility",
        "sql_path": SQL_ROOT / "remote" / "public_metro_tokyo_public_facility.sql",
        "schema": "metro_tokyo",
        "table": "public_facility",
        "join_column": "corporate_number",
        "role": "東京都オープンデータ公共施設",
    },
    "metro_tokyo_support_system": {
        "dataset": "metro_tokyo",
        "source_table": "metro_tokyo.ods.support_system",
        "sql_path": SQL_ROOT / "remote" / "public_metro_tokyo_support_system.sql",
        "schema": "metro_tokyo",
        "table": "support_system",
        "join_column": None,
        "role": "東京都オープンデータ支援制度",
    },
    "metro_tokyo_tourism": {
        "dataset": "metro_tokyo",
        "source_table": "metro_tokyo.ods.tourism",
        "sql_path": SQL_ROOT / "remote" / "public_metro_tokyo_tourism.sql",
        "schema": "metro_tokyo",
        "table": "tourism",
        "join_column": "corporate_number",
        "role": "東京都オープンデータ観光施設",
    },
}


def normalize_scope(scope: str) -> str:
    """Return the canonical refresh scope name."""
    return SCOPE_ALIASES.get(scope, scope)


def public_scope_choices() -> tuple[str, ...]:
    return tuple(sorted((*SCOPES, ALL_PUBLIC_SCOPE, *SCOPE_ALIASES)))


def load_scope_sql(scope: str) -> str:
    try:
        path = SCOPES[scope]
    except KeyError as exc:
        raise ValueError(f"Unknown scope: {scope}") from exc
    if not path.is_file():
        raise FileNotFoundError(f"Bundled SQL is missing: {path}")
    return path.read_text(encoding="utf-8").strip()


def load_public_sql(table_key: str) -> str:
    try:
        path = PUBLIC_TABLES[table_key]["sql_path"]
    except KeyError as exc:
        raise ValueError(f"Unknown public table: {table_key}") from exc
    if not path.is_file():
        raise FileNotFoundError(f"Bundled SQL is missing: {path}")
    return path.read_text(encoding="utf-8").strip()
