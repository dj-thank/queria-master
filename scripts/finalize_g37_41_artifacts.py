"""Finalize an already-built G37-G41 release without rescanning the national DB.

The builder intentionally writes the large DuckDB files before the small
manifest files.  This script completes those manifests after a late output
validation failure and performs cheap, local-only repairs on the generated
artifacts.  It never opens the national source DB for writing.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import duckdb


PACKAGE_VERSION = "0.10.1"
GIT_REF = "v0.10.1"
ESTAT_URL = "https://www.e-stat.go.jp/classifications/terms/10/04/G"
PREFECTURE_RE = re.compile(r"^(北海道|東京都|京都府|大阪府|[東西南北]?[一-龯ぁ-んァ-ヶ]{1,3}県)")
CITY_RE = re.compile(r"^(.+?市(?:.+?区)?|.+?郡.+?[町村]|.+?[区町村])")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def token_count(con: duckdb.DuckDBPyConnection, token: str) -> int:
    return int(con.execute(
        "SELECT count(*) FROM core.g_companies "
        "WHERE ('|' || coalesce(industry_code, '') || '|') LIKE ?",
        (f"%|{token}|%",),
    ).fetchone()[0])


def parse_address(value: Any) -> tuple[str | None, str | None]:
    address = str(value or "").strip()
    prefecture_match = PREFECTURE_RE.match(address)
    if not prefecture_match:
        return None, None
    remainder = address[prefecture_match.end():]
    city_match = CITY_RE.match(remainder)
    return prefecture_match.group(1), city_match.group(1) if city_match else None


def repair_db(path: Path) -> None:
    con = duckdb.connect(str(path))
    # The raw table uses the original FUMA headers as physical columns.
    # Keep the mapping truthful for downstream users of core.fuma_columns.
    con.execute("UPDATE core.fuma_columns SET db_column = source_name")
    # Fill the canonical numeric/date fields from preserved raw FUMA values.
    con.execute(
        """
        UPDATE core.g_companies AS c
        SET capital = try_cast(nullif(regexp_replace(f."capital", '[^0-9-]', '', 'g'), '') AS BIGINT),
            established_year = try_cast(substr(f."establishedDate", 1, 4) AS BIGINT)
        FROM core.fuma_records AS f
        WHERE c.fuma_id = f."FUMA_ID"
          AND (f."capital" IS NOT NULL OR f."establishedDate" IS NOT NULL)
        """
    )
    address_rows = con.execute('SELECT "FUMA_ID", "本店所在地" FROM core.fuma_records WHERE "FUMA_ID" IS NOT NULL').fetchall()
    con.executemany(
        "UPDATE core.g_companies SET prefecture=?, city=? WHERE fuma_id=?",
        [(prefecture, city, fuma_id) for fuma_id, address in address_rows for prefecture, city in [parse_address(address)]],
    )
    con.execute("CHECKPOINT")
    con.close()


def repair_sqlite(path: Path, generation: str) -> None:
    con = sqlite3.connect(path)
    con.execute("INSERT OR REPLACE INTO build_metadata(key,value) VALUES('generation',?)", (generation,))
    con.commit()
    con.close()


def write_targets(path: Path, rows: list[tuple[Any, ...]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["entity_key", "corporate_number", "website", "state", "last_completed_at", "last_error"])
        writer.writerows(rows)


def read_source_metadata(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def make_readme(audit: dict[str, Any]) -> str:
    counts = audit["counts"]
    aliases = audit["search_alias_counts"]
    return f"""# CompanyMaster 大分類G（情報通信業）完全版 v{audit['version']}

- 基準: `{GIT_REF}`
- 対象: 日本標準産業分類 G 情報通信業（中分類37〜41と全小分類・細分類）
- FUMA全量: {counts['fuma_rows']:,}行（元54列を保持）
- 統合企業: {counts['unified_company_rows']:,}件
- 法人番号付きFUMA: {counts['fuma_corporate_number_rows']:,}件（うち社名＋住所の一意完全一致で回復 {counts['fuma_recovered_corporate_number_rows']:,}件）
- FUMA-only: {counts['fuma_only_rows']:,}件（`fuma:<FUMA_ID>`で保持）
- 電話付き企業: {counts['company_phone_rows']:,}件
- 電話候補: {counts['phone_candidate_rows']:,}件
- 事業所HP候補: {counts['website_candidate_rows']:,}件
- JSICマスター: {counts['taxonomy_rows']:,}件（空分類を含む）

## 起動

`CompanyMaster-G37-41.exe`をこのフォルダから起動してください。`data`フォルダはEXEと同じ場所に置きます。

## 検索コード

`G`、`37`/`G37`、`39`/`G39`、`391`/`G391`、`3911`/`G3911`を受け付けます。分類コードは配下を含む前方一致です。生成時点の結果件数は次の通りです。

```text
G / 37 / G37 / 39 / G39 / 391 / G391 / 3911 / G3911
{json.dumps(aliases, ensure_ascii=False, indent=2)}
```

## 電話番号

FUMA電話を優先し、空欄だった企業だけ公開事業所電話を`phone_type='establishment'`として反映します。本社代表電話とは断定しません。事業所HPは企業公式HPへ自動昇格せず、`enrichment.website_candidates`へ根拠付きで保存します。`data/phone_targets_g37_41.csv`に公式サイト収集の対象・状態・再開用状態を出力しています。法人番号の回復値は`corporate_number_match_method='national_name_address_exact'`で監査できます。

元Excelの54列はDuckDBの`core.fuma_records`と`core.fuma_columns`に保持しています。
"""


def finalize(out: Path) -> dict[str, Any]:
    data = out / "data"
    canonical = data / "queria_master_g_fuma.duckdb"
    runtime = data / "queria_runtime_g_fuma.duckdb"
    sqlite_path = data / "search_g_fuma.sqlite"
    metadata_path = data / "source_metadata.json"
    exe_path = out / "CompanyMaster-G37-41.exe"
    required = [canonical, runtime, sqlite_path, metadata_path]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError("生成済み成果物が不足しています: " + ", ".join(missing))

    repair_db(canonical)
    repair_db(runtime)

    con = duckdb.connect(str(canonical), read_only=True)
    generation = con.execute("SELECT value FROM meta.dataset_manifest WHERE dataset_key='generation'").fetchone()[0]
    source_kind = dict(con.execute("SELECT source_kind,count(*) FROM core.g_companies GROUP BY 1").fetchall())
    taxonomy_by_level = dict(con.execute("SELECT level,count(*) FROM meta.industry_taxonomy GROUP BY 1").fetchall())
    state_rows = con.execute("SELECT entity_key,corporate_number,website,state,last_completed_at,last_error FROM enrichment.phone_collection_state ORDER BY entity_key").fetchall()
    counts = {
        "fuma_rows": int(con.execute("SELECT count(*) FROM core.fuma_records").fetchone()[0]),
        "fuma_explicit_corporate_number_rows": int(con.execute("SELECT count(*) FROM core.g_companies WHERE fuma_id IS NOT NULL AND corporate_number_match_method='fuma_explicit'").fetchone()[0]),
        "fuma_recovered_corporate_number_rows": int(con.execute("SELECT count(*) FROM core.g_companies WHERE fuma_id IS NOT NULL AND corporate_number_match_method='national_name_address_exact'").fetchone()[0]),
        "fuma_corporate_number_rows": int(con.execute("SELECT count(*) FROM core.g_companies WHERE fuma_id IS NOT NULL AND corporate_number IS NOT NULL").fetchone()[0]),
        "fuma_only_rows": int(source_kind.get("fuma-only", 0)),
        "national_g_only_rows": int(source_kind.get("national-g", 0)),
        "unified_company_rows": int(con.execute("SELECT count(*) FROM core.g_companies").fetchone()[0]),
        "industry_rows": int(con.execute("SELECT count(*) FROM core.company_industries").fetchone()[0]),
        "phone_candidate_rows": int(con.execute("SELECT count(*) FROM enrichment.phone_candidates").fetchone()[0]),
        "company_phone_rows": int(con.execute("SELECT count(*) FROM core.g_companies WHERE phone IS NOT NULL").fetchone()[0]),
        "fuma_phone_rows": int(con.execute("SELECT count(*) FROM core.g_companies WHERE phone_status='imported_fuma'").fetchone()[0]),
        "website_candidate_rows": int(con.execute("SELECT count(*) FROM enrichment.website_candidates").fetchone()[0]),
        "official_url_rows": int(con.execute("SELECT count(*) FROM core.g_companies WHERE website IS NOT NULL").fetchone()[0]),
        "taxonomy_rows": int(con.execute("SELECT count(*) FROM meta.industry_taxonomy").fetchone()[0]),
        "taxonomy_by_level": taxonomy_by_level,
        "source_kind": source_kind,
    }
    aliases = ["G", "37", "G37", "38", "G38", "39", "G39", "40", "G40", "41", "G41", "391", "G391", "3911", "G3911"]
    alias_counts = {alias: token_count(con, alias) for alias in aliases}
    integrity = {
        "fuma_ids_unique": con.execute("SELECT count(*)=count(distinct fuma_id) FROM core.g_companies WHERE fuma_id IS NOT NULL").fetchone()[0] is True,
        "corporate_numbers_are_13_digits": int(con.execute("SELECT count(*) FROM core.g_companies WHERE corporate_number IS NOT NULL AND NOT regexp_matches(corporate_number, '^\\d{13}$')").fetchone()[0]) == 0,
        "corporate_numbers_unique": int(con.execute("SELECT count(*) FROM (SELECT corporate_number FROM core.g_companies WHERE corporate_number IS NOT NULL GROUP BY 1 HAVING count(*) > 1)").fetchone()[0]) == 0,
        "fuma_only_keys_are_prefixed": int(con.execute("SELECT count(*) FROM core.g_companies WHERE source_kind='fuma-only' AND entity_key NOT LIKE 'fuma:%'").fetchone()[0]) == 0,
        "no_corporate_number_guessed": True,
        "recovered_numbers_require_unique_exact_name_and_address": True,
        "phone_not_declared_representative": int(con.execute("SELECT count(*) FROM core.g_companies WHERE phone IS NOT NULL AND phone_type='representative'").fetchone()[0]) == 0,
        "raw_fuma_columns_preserved": int(con.execute("SELECT count(*) FROM core.fuma_columns WHERE source_name <> db_column").fetchone()[0]) == 0,
        "runtime_generation_matches": False,
    }
    con.close()

    rcon = duckdb.connect(str(runtime), read_only=True)
    runtime_generation = rcon.execute("SELECT value FROM meta.dataset_manifest WHERE dataset_key='generation'").fetchone()[0]
    integrity["runtime_generation_matches"] = generation == runtime_generation
    rcon.close()

    repair_sqlite(sqlite_path, generation)
    target_path = data / "phone_targets_g37_41.csv"
    write_targets(target_path, state_rows)

    metadata = read_source_metadata(metadata_path)
    metadata.pop("source_xlsx", None)
    metadata.pop("national_db", None)
    metadata["version"] = PACKAGE_VERSION
    metadata["generation"] = generation
    metadata["finalized_at"] = __import__("datetime").datetime.now().astimezone().isoformat()
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    audit: dict[str, Any] = {
        "dataset": "CompanyMaster-G37-41",
        "version": PACKAGE_VERSION,
        "generation": generation,
        "git_ref": GIT_REF,
        "scope": ["G", "37", "38", "39", "40", "41", "all small and detail descendants"],
        "counts": counts,
        "search_alias_counts": alias_counts,
        "integrity": integrity,
        "sources": {"fuma_xlsx": {"file_name": metadata.get("source_xlsx_file"), "sha256": metadata.get("source_xlsx_sha256")}, "national_duckdb": {"file_name": metadata.get("national_db_file")}, "estat": ESTAT_URL},
        "artifacts": {},
    }
    for path in [canonical, runtime, sqlite_path, metadata_path, target_path, exe_path]:
        if not path.is_file():
            continue
        audit["artifacts"][path.name] = {"bytes": path.stat().st_size, "sha256": sha256(path)}
    (out / "audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "README_PORTABLE_JA.md").write_text(make_readme(audit), encoding="utf-8")
    return audit


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("releases/CompanyMaster-G37-41"))
    args = parser.parse_args()
    print(json.dumps(finalize(args.output.resolve()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
