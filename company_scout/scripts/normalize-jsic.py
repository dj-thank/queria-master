#!/usr/bin/env python3
"""Normalize an e-Stat Japan Standard Industrial Classification CSV for CompanyScout.

Usage:
  python scripts/normalize-jsic.py input.csv output.csv

Output columns:
  code,name,level,parent_code,revision,source_url
"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

REVISION = "2023-07"
SOURCE_URL = "https://www.e-stat.go.jp/classifications/terms/10"
CODE_RE = re.compile(r"^(?:[A-T]|\d{2}|\d{3}|\d{4})$")

CODE_HEADERS = {
    "分類コード", "コード", "産業分類コード", "classificationcode", "code",
}
NAME_HEADERS = {
    "項目名", "分類項目名", "分類名", "名称", "name", "classificationname",
}


def clean(s: str) -> str:
    return (s or "").replace("\ufeff", "").strip()


def norm_header(s: str) -> str:
    return re.sub(r"[\s_\-／/（）()]+", "", clean(s)).lower()


def read_rows(path: Path) -> list[list[str]]:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "cp932", "shift_jis"):
        try:
            with path.open("r", encoding=encoding, newline="") as f:
                return [[clean(v) for v in row] for row in csv.reader(f)]
        except UnicodeDecodeError as exc:
            last_error = exc
    raise RuntimeError(f"CSV encoding could not be decoded: {last_error}")


def detect_columns(rows: list[list[str]]) -> tuple[int | None, int | None, int]:
    for row_index, row in enumerate(rows[:20]):
        normalized = [norm_header(v) for v in row]
        code_idx = next((i for i, h in enumerate(normalized) if h in CODE_HEADERS or "分類コード" in h), None)
        name_idx = next((i for i, h in enumerate(normalized) if h in NAME_HEADERS or "分類項目名" in h), None)
        if code_idx is not None and name_idx is not None:
            return code_idx, name_idx, row_index + 1
    return None, None, 0


def fallback_code_name(row: list[str]) -> tuple[str | None, str | None]:
    for i, value in enumerate(row):
        code = clean(value)
        if CODE_RE.fullmatch(code):
            for candidate in row[i + 1 :]:
                name = clean(candidate)
                if name and not CODE_RE.fullmatch(name) and not name.isdigit():
                    return code, name
            return code, None
    return None, None


def level_for(code: str) -> int:
    if re.fullmatch(r"[A-T]", code):
        return 1
    return len(code)  # 2=>中分類, 3=>小分類, 4=>細分類


def normalize(rows: list[list[str]]) -> list[dict[str, str | int]]:
    code_idx, name_idx, start = detect_columns(rows)
    seen: set[str] = set()
    parents: dict[int, str] = {}
    output: list[dict[str, str | int]] = []

    for row in rows[start:]:
        if not row:
            continue
        if code_idx is not None and name_idx is not None:
            code = clean(row[code_idx]) if code_idx < len(row) else ""
            name = clean(row[name_idx]) if name_idx < len(row) else ""
        else:
            c, n = fallback_code_name(row)
            code, name = c or "", n or ""

        if not CODE_RE.fullmatch(code) or not name or code in seen:
            continue

        level = level_for(code)
        parent = "" if level == 1 else parents.get(level - 1, "")
        output.append({
            "code": code,
            "name": name,
            "level": level,
            "parent_code": parent,
            "revision": REVISION,
            "source_url": SOURCE_URL,
        })
        seen.add(code)
        parents[level] = code
        for deeper in range(level + 1, 5):
            parents.pop(deeper, None)

    return output


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: normalize-jsic.py INPUT.csv OUTPUT.csv", file=sys.stderr)
        return 2
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    rows = read_rows(src)
    normalized = normalize(rows)
    if not normalized:
        raise RuntimeError("No JSIC classification rows were detected. Check the e-Stat CSV format.")
    dst.parent.mkdir(parents=True, exist_ok=True)
    with dst.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["code", "name", "level", "parent_code", "revision", "source_url"])
        writer.writeheader()
        writer.writerows(normalized)
    print(f"Normalized {len(normalized):,} JSIC rows -> {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
