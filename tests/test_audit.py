from __future__ import annotations

from pathlib import Path

from queria_master import audit


def test_audit_validates_search_index_against_runtime_database(monkeypatch, tmp_path: Path):
    canonical = tmp_path / "queria_master.duckdb"
    runtime = tmp_path / "queria_runtime.duckdb"
    index = tmp_path / "search.sqlite"
    observed: dict[str, Path] = {}

    monkeypatch.setattr(
        audit,
        "_canonical_report",
        lambda path: {
            "company_count": 10,
            "duplicate_corporate_number_rows": 0,
        },
    )

    def fake_search_report(path: Path, database_path: Path, expected_rows: int):
        observed["database_path"] = database_path
        return {"path": str(path), "present": True, "status": "passed", "row_count": expected_rows}

    monkeypatch.setattr(audit, "_search_report", fake_search_report)
    monkeypatch.setattr(
        audit,
        "_runtime_report",
        lambda path, expected_rows: {
            "path": str(path),
            "present": True,
            "status": "passed",
            "company_count": expected_rows,
        },
    )

    report = audit.audit_database(
        canonical,
        search_index_path=index,
        runtime_path=runtime,
    )

    assert report["overall_status"] == "passed"
    assert observed["database_path"] == runtime.resolve()


def test_audit_uses_canonical_database_when_runtime_is_disabled(monkeypatch, tmp_path: Path):
    canonical = tmp_path / "queria_master.duckdb"
    index = tmp_path / "search.sqlite"
    observed: dict[str, Path] = {}

    monkeypatch.setattr(
        audit,
        "_canonical_report",
        lambda path: {
            "company_count": 10,
            "duplicate_corporate_number_rows": 0,
        },
    )

    def fake_search_report(path: Path, database_path: Path, expected_rows: int):
        observed["database_path"] = database_path
        return {"path": str(path), "present": True, "status": "passed", "row_count": expected_rows}

    monkeypatch.setattr(audit, "_search_report", fake_search_report)

    report = audit.audit_database(
        canonical,
        search_index_path=index,
        runtime_path=None,
    )

    assert report["overall_status"] == "passed"
    assert observed["database_path"] == canonical.resolve()
