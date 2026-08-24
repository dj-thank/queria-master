from __future__ import annotations

import argparse
import shutil
import stat
import zipfile
from pathlib import Path

try:
    from .verify_package import ROOT, package_files as release_files, sha256
except ImportError:  # Direct script execution.
    from verify_package import ROOT, package_files as release_files, sha256


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
