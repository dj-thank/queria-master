from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path


DEFAULT_CHUNK_SIZE = 1800 * 1024 * 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(4 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def split_bundle(input_path: Path, output_dir: Path, chunk_size: int) -> Path:
    if not input_path.is_file():
        raise SystemExit(f"入力ZIPがありません: {input_path}")
    if chunk_size <= 0 or chunk_size >= 2 * 1024 * 1024 * 1024:
        raise SystemExit("chunk_size は0より大きく2GiB未満にしてください。")
    output_dir.mkdir(parents=True, exist_ok=True)
    total_bytes = input_path.stat().st_size
    part_count = math.ceil(total_bytes / chunk_size)
    full_digest = hashlib.sha256()
    parts: list[dict[str, object]] = []
    with input_path.open("rb") as source:
        for index in range(part_count):
            name = f"{input_path.stem}.part-{index + 1:04d}-of-{part_count:04d}.bin"
            part_path = output_dir / name
            part_digest = hashlib.sha256()
            written = 0
            with part_path.open("wb") as destination:
                remaining = min(chunk_size, total_bytes - index * chunk_size)
                while remaining:
                    block = source.read(min(4 * 1024 * 1024, remaining))
                    if not block:
                        raise SystemExit("入力ZIPの読み込みが途中で終了しました。")
                    destination.write(block)
                    part_digest.update(block)
                    full_digest.update(block)
                    written += len(block)
                    remaining -= len(block)
            parts.append({"name": name, "bytes": written, "sha256": part_digest.hexdigest()})
            print(f"part {index + 1}/{part_count}: {written:,} bytes")
    metadata = {
        "format": "queria-full-app-parts-v1",
        "input_name": input_path.name,
        "bytes": total_bytes,
        "sha256": full_digest.hexdigest(),
        "chunk_size_bytes": chunk_size,
        "part_count": part_count,
        "parts": parts,
    }
    metadata_path = output_dir / f"{input_path.stem}.parts.json"
    partial = metadata_path.with_suffix(metadata_path.suffix + ".part")
    partial.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(partial, metadata_path)
    return metadata_path


def main() -> int:
    parser = argparse.ArgumentParser(description="GitHub Release向けに全量アプリZIPを分割")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--chunk-size-mib", type=int, default=1800)
    args = parser.parse_args()
    metadata = split_bundle(args.input.resolve(), args.out_dir.resolve(), args.chunk_size_mib * 1024 * 1024)
    print(f"metadata={metadata}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
