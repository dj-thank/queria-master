from __future__ import annotations

import csv
import sys
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import enrich_g_contact_targets as enrichment


def test_enrich_targets_joins_release_runtime_fields_without_changing_state(tmp_path: Path) -> None:
    database = tmp_path / "runtime.duckdb"
    con = duckdb.connect(str(database))
    con.execute("CREATE SCHEMA core")
    con.execute("CREATE SCHEMA meta")
    con.execute("CREATE TABLE meta.dataset_manifest(dataset_key VARCHAR, value VARCHAR)")
    con.executemany(
        "INSERT INTO meta.dataset_manifest VALUES (?,?)",
        [
            ("dataset_key", "G37_41_FUMA"),
            ("generation", "g-v0.10.0-test"),
            ("scope", "G,37,38,39,40,41 and all descendants"),
        ],
    )
    con.execute(
        "CREATE TABLE core.g_companies("
        "entity_key VARCHAR, name VARCHAR, prefecture VARCHAR, city VARCHAR, employees BIGINT, capital BIGINT, industry_code VARCHAR)"
    )
    con.executemany(
        "INSERT INTO core.g_companies VALUES (?,?,?,?,?,?,?)",
        [
            ("1000000000001", "Alpha株式会社", "東京都", "千代田区", 120, 50000000, "G|39|G39|391"),
            ("1000000000002", "Beta株式会社", "大阪府", "大阪市", 25, None, "G"),
        ],
    )
    con.close()
    source = tmp_path / "phone_targets.csv"
    with source.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["entity_key", "corporate_number", "website", "state", "last_completed_at", "last_error"],
        )
        writer.writeheader()
        writer.writerows([
            {"entity_key": "1000000000001", "corporate_number": "1000000000001", "website": "https://alpha.example/", "state": "pending_official_site"},
            {"entity_key": "1000000000002", "corporate_number": "1000000000002", "website": "", "state": "website_missing"},
        ])
    output = tmp_path / "enriched.csv"

    result = enrichment.enrich_targets(source, database, output)

    with output.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert result == {"rows": 2, "matched": 2, "missing": 0, "output": str(output)}
    assert rows[0]["company_name"] == "Alpha株式会社"
    assert rows[0]["employee_number"] == "120"
    assert rows[0]["capital_stock"] == "50000000"
    assert rows[0]["scope_label"] == "G37-G41"
    assert rows[0]["dataset_generation"] == "g-v0.10.0-test"
    assert rows[0]["jsic_major_codes"] == "G"
    assert rows[0]["jsic_middle_codes"] == "39"
    assert rows[1]["jsic_middle_codes"] == ""
    assert rows[0]["state"] == "pending_official_site"
    assert rows[1]["state"] == "website_missing"


def test_enrich_targets_rejects_runtime_without_g_scope_manifest(tmp_path: Path) -> None:
    database = tmp_path / "wrong.duckdb"
    con = duckdb.connect(str(database))
    con.execute("CREATE SCHEMA core")
    con.execute("CREATE SCHEMA meta")
    con.execute("CREATE TABLE meta.dataset_manifest(dataset_key VARCHAR, value VARCHAR)")
    con.executemany(
        "INSERT INTO meta.dataset_manifest VALUES (?,?)",
        [("dataset_key", "OTHER"), ("generation", "wrong"), ("scope", "H")],
    )
    con.execute(
        "CREATE TABLE core.g_companies("
        "entity_key VARCHAR, name VARCHAR, prefecture VARCHAR, city VARCHAR, employees BIGINT, capital BIGINT, industry_code VARCHAR)"
    )
    con.close()
    source = tmp_path / "targets.csv"
    with source.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["entity_key", "corporate_number", "website", "state", "last_completed_at", "last_error"],
        )
        writer.writeheader()

    with pytest.raises(ValueError, match="G37_41_FUMA"):
        enrichment.enrich_targets(source, database, tmp_path / "out.csv")
