from __future__ import annotations

import argparse
import sys
from pathlib import Path

from verify_package import ROOT, package_files, sha256


DEFAULT_MANIFEST = ROOT / "MANIFEST.sha256"


def render_manifest() -> str:
    lines = [
        f"{sha256(path)}  {path.relative_to(ROOT).as_posix()}"
        for path in package_files()
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the deterministic release-file manifest")
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify MANIFEST.sha256 without modifying it",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="manifest path (default: repository MANIFEST.sha256)",
    )
    args = parser.parse_args()

    output = args.output.resolve()
    rendered = render_manifest()
    if args.check:
        if not output.is_file() or output.read_text(encoding="utf-8") != rendered:
            print(
                f"FAIL: {output} is stale; run {Path(__file__).name} to regenerate it",
                file=sys.stderr,
            )
            return 1
        print(f"OK: {output} matches {len(package_files())} release files")
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"Wrote {output} for {len(package_files())} release files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
