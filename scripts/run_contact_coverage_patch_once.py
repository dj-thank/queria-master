#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
from pathlib import Path

PATCH_PATH = Path(__file__).with_name("apply_contact_coverage_patch_once.py")
spec = importlib.util.spec_from_file_location("contact_coverage_patch", PATCH_PATH)
if spec is None or spec.loader is None:
    raise SystemExit(f"could not load patch module: {PATCH_PATH}")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

updated = 0
for operation in module.OPERATIONS:
    if (
        operation.get("kind") == "replace_once"
        and operation.get("path") == "scripts/build_g37_41_fuma.py"
        and str(operation.get("old", "")).startswith("- 電話付き企業:")
    ):
        operation["old"] = str(operation["old"]).replace("\n", "\\n")
        operation["new"] = str(operation["new"]).replace("\n", "\\n")
        updated += 1

if updated != 1:
    raise SystemExit(f"expected one portable README operation, found {updated}")
module.main()


def replace_exact(path: str, old: str, new: str) -> None:
    target = Path(path)
    body = target.read_text(encoding="utf-8")
    count = body.count(old)
    if count != 1:
        raise SystemExit(f"{path}: compatibility replacement expected once, found {count}: {old[:100]!r}")
    target.write_text(body.replace(old, new, 1), encoding="utf-8", newline="\n")


# The reviewed contact file is wider than the previous crawl universe. A row
# absent from that universe is audited as unmatched rather than blocking every
# recovered candidate. Multiple matches remain a hard failure.
importer = Path("scripts/import_g_contact_artifact.py")
text = importer.read_text(encoding="utf-8")
replacements = [
    (
        "def _manual_match(\n"
        "    row: dict[str, str],\n"
        "    universe: dict[tuple[str, str], list[dict[str, str]]],\n"
        ") -> dict[str, str]:",
        "def _manual_match(\n"
        "    row: dict[str, str],\n"
        "    universe: dict[tuple[str, str], list[dict[str, str]]],\n"
        ") -> dict[str, str] | None:",
    ),
    (
        "    if len(matches) != 1:\n"
        "        raise ValueError(\n"
        "            f\"manual contact did not resolve one-to-one: {clean(row.get('照合企業名'))} \"\n"
        "            f\"({sorted(matches)})\"\n"
        "        )\n"
        "    return next(iter(matches.values()))",
        "    if not matches:\n"
        "        return None\n"
        "    if len(matches) != 1:\n"
        "        raise ValueError(\n"
        "            f\"manual contact did not resolve one-to-one: {clean(row.get('照合企業名'))} \"\n"
        "            f\"({sorted(matches)})\"\n"
        "        )\n"
        "    return next(iter(matches.values()))",
    ),
    (
        ") -> tuple[list[dict[str, str]], int]:\n"
        "    if path is None:\n"
        "        return [], 0",
        ") -> tuple[list[dict[str, str]], int, int]:\n"
        "    if path is None:\n"
        "        return [], 0, 0",
    ),
    (
        "    output: list[dict[str, str]] = []\n"
        "    matched = 0\n"
        "    for row in rows:\n"
        "        match = _manual_match(row, universe)\n"
        "        matched += 1",
        "    output: list[dict[str, str]] = []\n"
        "    matched = 0\n"
        "    unmatched = 0\n"
        "    for row in rows:\n"
        "        match = _manual_match(row, universe)\n"
        "        if match is None:\n"
        "            unmatched += 1\n"
        "            continue\n"
        "        matched += 1",
    ),
    (
        "    return output, matched\n\n\ndef _dedupe_key",
        "    return output, matched, unmatched\n\n\ndef _dedupe_key",
    ),
    (
        "    manual_rows, manual_matched = manual_candidates(\n",
        "    manual_rows, manual_matched, manual_unmatched = manual_candidates(\n",
    ),
    (
        "        \"manual_rows_matched\": manual_matched,\n",
        "        \"manual_rows_matched\": manual_matched,\n"
        "        \"manual_rows_unmatched\": manual_unmatched,\n",
    ),
]
for old, new in replacements:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"importer compatibility replacement expected once, found {count}: {old[:80]!r}")
    text = text.replace(old, new, 1)
importer.write_text(text, encoding="utf-8", newline="\n")

# Deep discovery compatibility and intended probing semantics.
replace_exact(
    "company_scout/public_enrichment/official_site_discovery.py",
    "        current = phone.canonical_url(queue.pop(0))\n",
    "        current = phone.canonical_url(candidate_url, queue.pop(0))\n",
)
replace_exact(
    "company_scout/public_enrichment/official_site_phone_enricher.py",
    "                for chunk in response.iter_content(chunk_size=64 * 1024):\n",
    "                try:\n"
    "                    content_iterator = response.iter_content(chunk_size=64 * 1024)\n"
    "                except TypeError:\n"
    "                    content_iterator = response.iter_content(64 * 1024)\n"
    "                for chunk in content_iterator:\n",
)
replace_exact(
    "company_scout/public_enrichment/official_site_phone_enricher.py",
    "    queue = [website]\n",
    "    queue = [website, *common_contact_urls(website)]\n",
)

# Existing tests must reflect the deliberate common-path probing behavior;
# contact pages are prioritized before company-profile paths. The synthetic
# example domain bypasses DNS only inside the test, preserving runtime SSRF
# protection unchanged.
replace_exact(
    "company_scout/public_enrichment/tests/test_phone_security.py",
    "        self.assertEqual(result[\"pages_fetched\"], 1)\n",
    "        self.assertEqual(result[\"pages_fetched\"], 4)\n",
)
replace_exact(
    "company_scout/public_enrichment/tests/test_structured_contact_extractor.py",
    "    assert rows[0] == \"https://example.jp/company/\"\n",
    "    assert rows[0] == \"https://example.jp/contact/\"\n",
)
replace_exact(
    "company_scout/public_enrichment/tests/test_official_site_phone_deep.py",
    "    monkeypatch.setattr(phone, \"safe_get_text\", fake_get)\n"
    "    monkeypatch.setattr(phone.time, \"sleep\", lambda _seconds: None)\n",
    "    monkeypatch.setattr(phone, \"safe_get_text\", fake_get)\n"
    "    monkeypatch.setattr(phone, \"is_public_http_url\", lambda _url: True)\n"
    "    monkeypatch.setattr(phone.time, \"sleep\", lambda _seconds: None)\n",
)
