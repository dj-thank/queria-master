#!/usr/bin/env python3
"""Export the release G phone-target ledger into the shared collector schema."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
from pathlib import Path

from jsic_g37_41_collection import make_target_csv


def export_targets(source: Path, output: Path) -> dict[str, object]:
    temporary = make_target_csv(source)
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    with output.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = sum(1 for _ in csv.DictReader(handle))
    return {"scope": "G37-G41", "rows": rows, "output": str(output)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--targets", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = export_targets(args.targets, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

