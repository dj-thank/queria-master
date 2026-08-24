from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = {
    "README.md",
    "QUICKSTART_JA.md",
    "bootstrap.ps1",
    "bootstrap.sh",
    "refresh.ps1",
    "refresh.sh",
    "requirements.txt",
    "requirements-dev.txt",
    "queria_master/cli.py",
    "queria_master/gbiz_archive.py",
    "queria_master/public_enrichment_bridge.py",
    "queria_master/publish.py",
    "queria_master/website_discovery.py",
    "queria_master/pipeline.py",
    "queria_master/query.py",
    "tests/test_gbiz_archive.py",
    "tests/test_cli_defaults.py",
    "tests/test_website_pipeline.py",
    "docs/ARCHITECTURE.md",
    "docs/GBIZ_ARCHIVE_IMPORT_JA.md",
    "docs/V090_OPERATIONAL_ARCHITECTURE_JA.md",
    "docs/WEBSITE_DISCOVERY_EXTRACTION_ARCHITECTURE_JA.md",
    "docs/ZIP_AUDIT_20260824.md",
    "queria_master/assets/sql/remote/info_communications.sql",
    "sql/remote/info_communications.sql",
    "sql/remote/gbizinfo_companies.sql",
    "sql/remote/all_corporations.sql",
    "reference/sources.json",
}
FORBIDDEN_SQL = re.compile(r"\b(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|COPY|ATTACH|DETACH|INSTALL|LOAD)\b", re.I)
TOKEN_LIKE = re.compile(
    r"\b(?:qk_|sk-)[A-Za-z0-9_-]{12,}|"
    r"\b(?=[A-Za-z0-9]{0,31}[A-Z])(?=[A-Za-z0-9]{0,31}[a-z])"
    r"(?=[A-Za-z0-9]{0,31}[0-9])[A-Za-z0-9]{32}\b"
)
SKIP_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    "build",
    "dist",
    "node_modules",
    "target",
}
SKIP_FILES = {"MANIFEST.sha256"}
GENERATED_DIRS = {"data", "cache", "exports"}
KEEP_IN_GENERATED_DIRS = {"README.md", ".gitkeep"}


def _is_release_file(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    if not path.is_file() or path.name in SKIP_FILES:
        return False
    if (
        len(relative.parts) == 1
        and relative.name.startswith("pytest-")
        and relative.suffix in {".json", ".log"}
    ):
        # Historical local test captures contain machine-specific paths and
        # are not runtime source or current validation evidence.
        return False
    if any(part in SKIP_PARTS or part.endswith(".egg-info") for part in relative.parts):
        return False
    if relative.parts and relative.parts[0] in GENERATED_DIRS and path.name not in KEEP_IN_GENERATED_DIRS:
        return False
    return True


def package_files() -> list[Path]:
    git_marker = ROOT / ".git"
    if git_marker.exists():
        result = subprocess.run(
            ["git", "ls-files", "-z", "--cached"],
            cwd=ROOT,
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError("git ls-files failed while resolving release inputs")
        candidates = [
            ROOT / relative.decode("utf-8", errors="strict")
            for relative in result.stdout.split(b"\0")
            if relative
        ]
    else:
        # Exported source archives do not contain .git. In that environment
        # the archive itself is the trust boundary, so enumerate its files.
        candidates = list(ROOT.rglob("*"))
    return [path for path in sorted(candidates) if _is_release_file(path)]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _token_scan_text(path: Path, data: bytes) -> str:
    text = data.decode("utf-8", errors="ignore")
    if path.name != "package-lock.json":
        return text
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return text

    def strip_integrity(item: object) -> object:
        if isinstance(item, dict):
            return {
                key: strip_integrity(child)
                for key, child in item.items()
                if key != "integrity"
            }
        if isinstance(item, list):
            return [strip_integrity(child) for child in item]
        return item

    return json.dumps(strip_integrity(value), ensure_ascii=False, sort_keys=True)


def main() -> int:
    failures: list[str] = []
    release_relatives = {
        path.relative_to(ROOT).as_posix() for path in package_files()
    }
    for relative in sorted(REQUIRED):
        if not (ROOT / relative).is_file():
            failures.append(f"missing: {relative}")
        elif relative not in release_relatives:
            failures.append(f"required file is not tracked/releasable: {relative}")

    for sql_root in (ROOT / "sql" / "remote", ROOT / "queria_master" / "assets" / "sql" / "remote"):
        for path in sql_root.glob("*.sql"):
            sql = path.read_text(encoding="utf-8").strip()
            if sql.split(None, 1)[0].upper() not in {"SELECT", "WITH"}:
                failures.append(f"remote SQL does not start SELECT/WITH: {path.relative_to(ROOT)}")
            if FORBIDDEN_SQL.search(sql):
                failures.append(f"write keyword in remote SQL: {path.relative_to(ROOT)}")

    files = package_files()
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(ROOT).as_posix()
        data = path.read_bytes()
        digest.update(relative.encode("utf-8") + b"\0" + data)
        if path.suffix.lower() not in {".zip", ".duckdb", ".parquet", ".pyc"}:
            text = _token_scan_text(path, data)
            if TOKEN_LIKE.search(text):
                failures.append(f"credential-like token: {relative}")

    manifest = ROOT / "MANIFEST.sha256"
    if manifest.is_file():
        expected: dict[str, str] = {}
        for line in manifest.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            checksum, relative = line.split("  ", 1)
            expected[relative] = checksum
        actual = {path.relative_to(ROOT).as_posix(): sha256(path) for path in files}
        if expected != actual:
            missing = sorted(set(actual) - set(expected))
            stale = sorted(set(expected) - set(actual))
            changed = sorted(k for k in set(actual) & set(expected) if actual[k] != expected[k])
            failures.append(
                "manifest mismatch: "
                f"missing={missing[:5]}, stale={stale[:5]}, changed={changed[:5]}"
            )

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print(f"OK: {len(files)} release files, tree-sha256={digest.hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
