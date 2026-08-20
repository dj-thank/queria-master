from __future__ import annotations

from pathlib import Path

from queria_master.app_config import AppSettings, resolve_artifacts
from queria_master import health


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
            "counts": {"companies": 10, "contact_points": 0, "establishments": 7},
            "manifest": {"generation_id": "generation-1"},
        },
    )

    class FakeIndex:
        metadata = {"row_count": "10", "runtime_generation_id": "generation-1"}

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
