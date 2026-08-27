from __future__ import annotations

from pathlib import Path

import duckdb

from queria_master.app_config import AppSettings, resolve_artifacts
from queria_master import health, runtime


def test_health_reports_generation_and_capability_liveness(monkeypatch, tmp_path: Path):
    for name in (
        "queria_master.duckdb",
        "queria_enrichment.duckdb",
        "queria_runtime.duckdb",
        "search.sqlite",
    ):
        path = tmp_path / "data" / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"x")
    artifacts = resolve_artifacts(AppSettings(home=str(tmp_path)), fallback_home=tmp_path, environment={})

    monkeypatch.setattr(
        health,
        "runtime_summary",
        lambda path: {
            "counts": {
                "companies": 10,
                "contact_points": 9,
                "resolved_contacts": 0,
                "establishments": 7,
            },
            "manifest": {
                "schema_version": health.RUNTIME_SCHEMA_VERSION,
                "generation_id": "generation-1",
            },
        },
    )

    class FakeIndex:
        metadata = {
            "index_version": health.SEARCH_INDEX_VERSION,
            "row_count": "10",
            "runtime_generation_id": "generation-1",
        }

        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(health, "SearchIndex", FakeIndex)

    report = health.inspect_application(artifacts)

    assert report["overall_status"] == "passed"
    assert report["generation"]["match"] is True
    assert report["capabilities"]["keyword_search"]["enabled"] is True
    assert report["capabilities"]["verified_company_contacts"]["enabled"] is False
    assert report["capabilities"]["establishment_contacts"]["enabled"] is True


def test_source_identity_qualifies_catalog_when_filename_matches_schema(tmp_path: Path):
    enrichment = tmp_path / "enrichment.duckdb"
    con = duckdb.connect(str(enrichment))
    try:
        con.execute("CREATE SCHEMA enrichment")
        con.execute(
            """
            CREATE TABLE "enrichment".enrichment.schema_meta(
                schema_name VARCHAR,
                initialized_at VARCHAR
            )
            """
        )
        con.execute(
            "INSERT INTO \"enrichment\".enrichment.schema_meta "
            "VALUES ('enrichment', 'revision-1')"
        )
    finally:
        con.close()

    assert health._source_identity(
        enrichment,
        "SELECT initialized_at FROM enrichment.schema_meta "
        "WHERE schema_name = 'enrichment' LIMIT 1",
    ) == "revision-1"


def test_enrichment_revision_is_stable_across_duckdb_timezones(
    monkeypatch, tmp_path: Path
):
    source = tmp_path / "source.duckdb"
    con = duckdb.connect(str(source))
    try:
        con.execute("CREATE SCHEMA enrichment")
        con.execute(
            """
            CREATE TABLE enrichment.schema_meta(
                schema_name VARCHAR,
                initialized_at TIMESTAMPTZ
            )
            """
        )
        con.execute(
            """
            INSERT INTO enrichment.schema_meta
            VALUES ('enrichment', TIMESTAMPTZ '2026-08-24 12:34:56+09:00')
            """
        )
    finally:
        con.close()

    manifest_con = duckdb.connect(str(source), read_only=True)
    try:
        manifest_con.execute("SET TimeZone='America/Los_Angeles'")
        manifest_identity = runtime._optional_source_identity(
            manifest_con,
            "SELECT initialized_at FROM enrichment.schema_meta "
            "WHERE schema_name = 'enrichment' LIMIT 1",
        )
    finally:
        manifest_con.close()

    real_connect = duckdb.connect

    def connect_in_tokyo(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connection.execute("SET TimeZone='Asia/Tokyo'")
        return connection

    monkeypatch.setattr(duckdb, "connect", connect_in_tokyo)
    health_identity = health._source_identity(
        source,
        "SELECT initialized_at FROM enrichment.schema_meta "
        "WHERE schema_name = 'enrichment' LIMIT 1",
    )

    assert manifest_identity == "2026-08-24T03:34:56+00:00"
    assert health_identity == manifest_identity
