from __future__ import annotations

import argparse
import hashlib
import shutil
import stat
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from build_release import release_files, sha256, sync_assets
from queria_master.resources import PUBLIC_TABLES


DATA_FILES = (
    ROOT / "data" / "queria_master.duckdb",
    ROOT / "data" / "source_metadata.json",
    ROOT / "data" / "search.sqlite",
)
OPTIONAL_DATA_FILES = (
    ROOT / "data" / "queria_enrichment.duckdb",
    ROOT / "data" / "queria_runtime.duckdb",
)
DATA_DIR = ROOT / "cache" / "all-public-latest"
EXPECTED_PARQUETS = {f"{table_key}.parquet" for table_key in PUBLIC_TABLES}


def _full_data_files(*, include_parquet: bool = False) -> list[Path]:
    missing = [path for path in DATA_FILES if not path.is_file()]
    if include_parquet:
        missing.extend(DATA_DIR / name for name in sorted(EXPECTED_PARQUETS) if not (DATA_DIR / name).is_file())
    if missing:
        names = ", ".join(str(path.relative_to(ROOT)) for path in missing)
        raise SystemExit(
            "全量データが不足しています。先に `python -m queria_master refresh --scope all-public` を実行してください。"
            f" 不足: {names}"
        )
    optional = [path for path in OPTIONAL_DATA_FILES if path.is_file()]
    parquet = (DATA_DIR / name for name in sorted(EXPECTED_PARQUETS)) if include_parquet else ()
    return [*DATA_FILES, *optional, *parquet]


def _bundle_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _write_streamed(archive: zipfile.ZipFile, path: Path, arcname: str) -> None:
    info = zipfile.ZipInfo.from_file(path, arcname=arcname)
    # Parquet, DuckDB, and SQLite indexes already contain compressed or
    # page-oriented binary structures.  Deflating them
    # again makes a multi-gigabyte release unnecessarily slow and consumes
    # CPU without a useful size reduction, so store those binary artifacts as
    # is while keeping source text compressed.
    info.compress_type = (
        zipfile.ZIP_STORED if path.suffix.lower() in {".duckdb", ".parquet", ".sqlite"} else zipfile.ZIP_DEFLATED
    )
    if path.suffix == ".sh":
        info.external_attr = (stat.S_IFREG | 0o755) << 16
    with archive.open(info, mode="w", force_zip64=True) as destination:
        with path.open("rb") as source:
            shutil.copyfileobj(source, destination, length=4 * 1024 * 1024)


def build_zip(output: Path, *, include_parquet: bool = False) -> tuple[int, str, int]:
    sync_assets()
    source_files = release_files()
    data_files = _full_data_files(include_parquet=include_parquet)
    all_files = [*source_files, *data_files]

    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="queria-full-manifest-") as tmp:
        manifest = Path(tmp) / "FULL_BUNDLE_MANIFEST.sha256"
        lines = []
        for path in all_files:
            relative = (Path("queria-master") / path.relative_to(ROOT)).as_posix()
            lines.append(f"{_bundle_sha256(path)}  {relative}")
        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")

        with zipfile.ZipFile(
            output,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=1,
            allowZip64=True,
        ) as archive:
            for path in all_files:
                relative = Path("queria-master") / path.relative_to(ROOT)
                _write_streamed(archive, path, relative.as_posix())
            _write_streamed(archive, manifest, "queria-master/FULL_BUNDLE_MANIFEST.sha256")
    return len(all_files) + 1, _bundle_sha256(output), output.stat().st_size


def main() -> int:
    parser = argparse.ArgumentParser(description="検証済み全量データ付きQueria ZIPを作成")
    parser.add_argument("--out", type=Path, default=ROOT.parent / "queria-master-all-public.zip")
    parser.add_argument(
        "--include-parquet",
        action="store_true",
        help="DuckDBへ統合済みのParquetキャッシュも重複収録する（容量が大きい）",
    )
    args = parser.parse_args()
    count, digest, size = build_zip(args.out.resolve(), include_parquet=args.include_parquet)
    print(f"Built {args.out.resolve()} ({count} files, bytes={size}, sha256={digest})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
