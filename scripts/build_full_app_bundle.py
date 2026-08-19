from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import stat
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_README = ROOT / "docs" / "FULL_APP_BUNDLE_README_JA.md"


def _sha256_bytes(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(4 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _iter_files(root: Path) -> list[Path]:
    if not root.is_dir():
        raise SystemExit(f"アプリディレクトリがありません: {root}")
    return [path for path in sorted(root.rglob("*")) if path.is_file()]


def _binary_entry(path: Path) -> bool:
    return path.suffix.lower() in {
        ".duckdb",
        ".sqlite",
        ".exe",
        ".dll",
        ".pyd",
        ".zip",
        ".parquet",
    }


def _write_entry(archive: zipfile.ZipFile, path: Path, arcname: str) -> tuple[str, int]:
    info = zipfile.ZipInfo.from_file(path, arcname=arcname)
    info.compress_type = zipfile.ZIP_STORED if _binary_entry(path) else zipfile.ZIP_DEFLATED
    if path.suffix.lower() == ".sh":
        info.external_attr = (stat.S_IFREG | 0o755) << 16
    digest = hashlib.sha256()
    size = 0
    with archive.open(info, mode="w", force_zip64=True) as destination:
        with path.open("rb") as source:
            while block := source.read(4 * 1024 * 1024):
                digest.update(block)
                size += len(block)
                destination.write(block)
    return digest.hexdigest(), size


def _required_data(data_dir: Path) -> list[Path]:
    names = (
        "queria_runtime.duckdb",
        "search.sqlite",
        "queria_master.duckdb",
        "queria_enrichment.duckdb",
        "source_metadata.json",
    )
    files = [data_dir / name for name in names]
    missing = [path for path in files if not path.is_file()]
    if missing:
        raise SystemExit("全量同梱に必要なデータがありません: " + ", ".join(str(path) for path in missing))
    return files


def build_bundle(
    *,
    output: Path,
    desktop_dir: Path,
    console_dir: Path,
    cli_dir: Path,
    data_dir: Path,
    readme: Path,
) -> tuple[int, str, int]:
    data_files = _required_data(data_dir)
    entries: list[tuple[Path, str]] = [(readme, "README_JA.md")]
    for folder_name, folder in (
        ("queria-master-desktop", desktop_dir),
        ("queria-master-desktop-console", console_dir),
        ("queria-master-cli", cli_dir),
    ):
        entries.extend((path, (Path(folder_name) / path.relative_to(folder)).as_posix()) for path in _iter_files(folder))
    entries.extend((path, (Path("data") / path.name).as_posix()) for path in data_files)

    total_bytes = readme.stat().st_size + sum(path.stat().st_size for path, _ in entries[1:])
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    free_bytes = shutil.disk_usage(output.parent).free
    if free_bytes < total_bytes + 1024**3:
        raise SystemExit(
            f"出力先の空き容量が不足しています。必要概算={total_bytes:,} bytes、空き={free_bytes:,} bytes"
        )
    partial = output.with_name(output.name + ".part")
    partial.unlink(missing_ok=True)
    manifest: list[str] = []
    with zipfile.ZipFile(
        partial,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=1,
        allowZip64=True,
    ) as archive:
        for path, arcname in entries:
            digest, size = _write_entry(archive, path, arcname)
            manifest.append(f"{digest}  {arcname}  {size}")
        manifest_bytes = ("\n".join(manifest) + "\n").encode("utf-8")
        archive.writestr(
            "FULL_APP_BUNDLE_MANIFEST.sha256",
            manifest_bytes,
            compress_type=zipfile.ZIP_DEFLATED,
            compresslevel=1,
        )
    os.replace(partial, output)
    return len(entries) + 1, _sha256_bytes(output), output.stat().st_size


def main() -> int:
    parser = argparse.ArgumentParser(description="EXEと全量DBを同梱したQueriaアプリZIPを作成")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--desktop-dir", type=Path, default=ROOT / "dist" / "queria-master-desktop")
    parser.add_argument("--console-dir", type=Path, default=ROOT / "dist" / "desktop-console" / "queria-master-desktop")
    parser.add_argument("--cli-dir", type=Path, default=ROOT / "dist" / "cli-onedir" / "queria-master")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--readme", type=Path, default=DEFAULT_README)
    args = parser.parse_args()
    count, digest, size = build_bundle(
        output=args.out,
        desktop_dir=args.desktop_dir,
        console_dir=args.console_dir,
        cli_dir=args.cli_dir,
        data_dir=args.data_dir,
        readme=args.readme,
    )
    print(f"Built {args.out.resolve()} ({count} files, bytes={size}, sha256={digest})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
