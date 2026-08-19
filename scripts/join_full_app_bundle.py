from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path


def join_bundle(metadata_path: Path, output_path: Path) -> None:
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    parts = metadata.get("parts")
    if not isinstance(parts, list) or not parts:
        raise SystemExit("parts metadata が空です。")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    partial = output_path.with_name(output_path.name + ".part")
    digest = hashlib.sha256()
    total = 0
    with partial.open("wb") as destination:
        for index, item in enumerate(parts, start=1):
            if not isinstance(item, dict):
                raise SystemExit(f"不正なpart metadata: {index}")
            part_path = metadata_path.parent / str(item["name"])
            if not part_path.is_file():
                raise SystemExit(f"partがありません: {part_path}")
            expected_size = int(item["bytes"])
            expected_digest = str(item["sha256"])
            part_digest = hashlib.sha256()
            size = 0
            with part_path.open("rb") as source:
                while block := source.read(4 * 1024 * 1024):
                    destination.write(block)
                    digest.update(block)
                    part_digest.update(block)
                    size += len(block)
            if size != expected_size or part_digest.hexdigest() != expected_digest:
                partial.unlink(missing_ok=True)
                raise SystemExit(f"part検証失敗: {part_path}")
            total += size
            print(f"joined {index}/{len(parts)}: {size:,} bytes")
    actual_digest = digest.hexdigest()
    if total != int(metadata["bytes"]) or actual_digest != str(metadata["sha256"]):
        partial.unlink(missing_ok=True)
        raise SystemExit("結合後のサイズまたはSHA-256が一致しません。")
    os.replace(partial, output_path)
    print(f"output={output_path.resolve()}")
    print(f"bytes={total}")
    print(f"sha256={actual_digest}")


def main() -> int:
    parser = argparse.ArgumentParser(description="分割した全量アプリZIPを結合して検証")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    join_bundle(args.manifest.resolve(), args.out.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
