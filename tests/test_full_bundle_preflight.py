from __future__ import annotations

from pathlib import Path

import pytest

from scripts import build_full_app_bundle


def _report(runtime_generation: str, index_generation: str, *, passed: bool = True):
    return {
        "overall_status": "passed" if passed else "failed",
        "gates": {"runtime_present_and_aligned": passed},
        "runtime": {"manifest": {"generation_id": runtime_generation}},
        "search_index": {"runtime_generation_id": index_generation},
    }


def test_preflight_requires_matching_runtime_and_index_generation(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        build_full_app_bundle,
        "audit_database",
        lambda *args, **kwargs: _report("generation-1", "generation-1"),
    )

    report = build_full_app_bundle._preflight_data(tmp_path)

    assert report["overall_status"] == "passed"


def test_preflight_rejects_generation_mismatch(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        build_full_app_bundle,
        "audit_database",
        lambda *args, **kwargs: _report("generation-1", "generation-2"),
    )

    with pytest.raises(SystemExit, match="generation_id"):
        build_full_app_bundle._preflight_data(tmp_path)
