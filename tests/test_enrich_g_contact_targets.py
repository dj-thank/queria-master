from __future__ import annotations

import csv
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import enrich_g_contact_targets as enrichment


def test_enrich_targets_joins_release_runtime_fields_without_changing_state(tmp_path: Path) -> None:
    database = tmp_path / "runtime.duckdb"
    con = duckdb.connect(str(database))
    con.execute("CREATE SCHEMA core")
    con.execute(
        "CREATE TABLE core.g_companies("
        "entity_key VARCHAR, name VARCHAR, prefecture VARCHAR, city VARCHAR, employees BIGINT, capital BIGINT)"
    )
    con.executemany(
        "INSERT INTO core.g_companies VALUES (?,?,?,?,?,?)",
        [
            ("1000000000001", "Alpha株式会社", "東京都", "千代田区", 120, 50000000),
            ("1000000000002", "Beta株式会社", "大阪府", "大阪市", 25, None),
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
    assert rows[0]["state"] == "pending_official_site"
    assert rows[1]["state"] == "website_missing"
