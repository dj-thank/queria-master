from __future__ import annotations

from pathlib import Path

from queria_master import desktop_app


def test_frozen_home_discovers_data_next_to_bundle(monkeypatch, tmp_path: Path):
    release_root = tmp_path / "release"
    bundle_dir = release_root / "queria-master-desktop"
    data_dir = release_root / "data"
    bundle_dir.mkdir(parents=True)
    data_dir.mkdir()
    (data_dir / "queria_runtime.duckdb").write_bytes(b"db")
    (data_dir / "search.sqlite").write_bytes(b"index")

    monkeypatch.setattr(desktop_app.sys, "frozen", True, raising=False)
    monkeypatch.setattr(desktop_app.sys, "executable", str(bundle_dir / "app.exe"))

    assert desktop_app._frozen_home() == release_root.resolve()
