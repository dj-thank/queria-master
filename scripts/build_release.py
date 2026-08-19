from __future__ import annotations

import argparse
import hashlib
import shutil
import stat
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {".git", ".venv", "__pycache__", ".pytest_cache", "build", "dist"}
SKIP_FILES = {"MANIFEST.sha256"}
GENERATED_DIRS = {"data", "cache", "exports"}
KEEP_IN_GENERATED_DIRS = {"README.md", ".gitkeep"}


def is_release_file(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    if not path.is_file() or path.name in SKIP_FILES:
        return False
    if any(part in SKIP_PARTS or part.endswith(".egg-info") for part in relative.parts):
        return False
    if relative.parts and relative.parts[0] in GENERATED_DIRS and path.name not in KEEP_IN_GENERATED_DIRS:
        return False
    return True


def release_files() -> list[Path]:
    return [path for path in sorted(ROOT.rglob("*")) if is_release_file(path)]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def sync_assets() -> None:
    pairs = [
        (ROOT / "sql" / "remote", ROOT / "queria_master" / "assets" / "sql" / "remote"),
        (ROOT / "reference", ROOT / "queria_master" / "assets" / "reference"),
    ]
    for source, target in pairs:
        target.mkdir(parents=True, exist_ok=True)
        for stale in target.iterdir():
            if stale.is_file() and not (source / stale.name).is_file():
                stale.unlink()
        for item in source.iterdir():
            if item.is_file():
                shutil.copy2(item, target / item.name)


def write_manifest(files: list[Path]) -> Path:
    manifest = ROOT / "MANIFEST.sha256"
    lines = [f"{sha256(path)}  {path.relative_to(ROOT).as_posix()}" for path in files]
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return manifest


def build_zip(output: Path) -> tuple[int, str]:
    sync_assets()
    files = release_files()
    manifest = write_manifest(files)
    files_with_manifest = [*files, manifest]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files_with_manifest:
            relative = Path("queria-master") / path.relative_to(ROOT)
            info = zipfile.ZipInfo.from_file(path, arcname=relative.as_posix())
            if path.suffix == ".sh":
                info.external_attr = (stat.S_IFREG | 0o755) << 16
            with path.open("rb") as handle:
                archive.writestr(info, handle.read(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return len(files_with_manifest), sha256(output)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=ROOT.parent / "queria-master.zip")
    args = parser.parse_args()
    count, digest = build_zip(args.out.resolve())
    print(f"Built {args.out.resolve()} ({count} files, sha256={digest})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
