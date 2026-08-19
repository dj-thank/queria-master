from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def build_exe(
    output_dir: Path,
    *,
    clean: bool = True,
    mode: str = "cli",
    bundle: str = "onefile",
    windowed: bool | None = None,
) -> Path:
    if sys.platform != "win32":
        raise SystemExit("Windows EXE build requires a Windows Python runtime.")
    if mode not in {"cli", "desktop"}:
        raise ValueError(f"unsupported EXE mode: {mode}")
    if bundle not in {"onefile", "onedir"}:
        raise ValueError(f"unsupported PyInstaller bundle: {bundle}")
    if windowed is None:
        windowed = mode == "desktop"
    output_dir = output_dir.resolve()
    work_dir = ROOT / "build" / "pyinstaller"
    if clean:
        shutil.rmtree(work_dir, ignore_errors=True)
        for spec_path in ROOT.glob("queria-master*.spec"):
            spec_path.unlink()
    output_dir.mkdir(parents=True, exist_ok=True)
    executable_stem = "queria-master" if mode == "cli" else "queria-master-desktop"
    executable_name = f"{executable_stem}.exe"
    entrypoint = ROOT / ("exe_entrypoint.py" if mode == "cli" else "desktop_entrypoint.py")
    if clean and bundle == "onedir":
        shutil.rmtree(output_dir / executable_stem, ignore_errors=True)
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        f"--{bundle}",
        "--name",
        executable_stem,
        "--paths",
        str(ROOT),
        "--distpath",
        str(output_dir),
        "--workpath",
        str(work_dir),
        "--specpath",
        str(ROOT / "build"),
        "--collect-data",
        "queria_master",
        "--collect-all",
        "duckdb",
        "--collect-all",
        "pytz",
        str(entrypoint),
    ]
    if windowed:
        command.insert(6, "--windowed")
    subprocess.run(command, cwd=ROOT, check=True)
    executable = output_dir / executable_name if bundle == "onefile" else output_dir / executable_stem / executable_name
    if not executable.is_file():
        raise SystemExit(f"PyInstaller did not produce {executable}")
    report = {
        "executable": str(executable),
        "bytes": executable.stat().st_size,
        "sha256": sha256(executable),
        "python": sys.version,
        "mode": mode,
        "bundle": bundle,
        "windowed": windowed,
        "project_root": str(ROOT),
        "data_policy": "external: set QUERIA_MASTER_HOME or keep data beside the executable",
    }
    report_path = executable.parent / f"{executable_stem}.exe.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return executable


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the Queria master CLI or resident desktop app as a Windows EXE")
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    parser.add_argument("--mode", choices=("cli", "desktop"), default="cli")
    parser.add_argument("--bundle", choices=("onefile", "onedir"), default="onefile")
    parser.add_argument(
        "--console",
        action="store_true",
        help="desktopモードでもコンソール付きブートローダーを使う（実行制御ポリシーの診断・互換用）",
    )
    parser.add_argument("--no-clean", action="store_true")
    args = parser.parse_args()
    executable = build_exe(
        args.out,
        clean=not args.no_clean,
        mode=args.mode,
        bundle=args.bundle,
        windowed=False if args.console else None,
    )
    print(f"Built {executable} ({executable.stat().st_size:,} bytes, sha256={sha256(executable)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
