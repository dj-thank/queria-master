#!/usr/bin/env bash
set -euo pipefail

cat \
  .patch_payload/contact_coverage_bundle.b64.part00 \
  .patch_payload/contact_coverage_bundle.b64.part01 \
  .patch_payload/contact_coverage_bundle.b64.part02 \
  .patch_payload/contact_coverage_bundle.b64.part03 \
  .patch_payload/contact_coverage_bundle.b64.part04 \
  .patch_payload/contact_coverage_bundle.b64.part05a \
  .patch_payload/contact_coverage_bundle.b64.part05b \
  .patch_payload/contact_coverage_bundle.b64.part05c \
  .patch_payload/contact_coverage_bundle.b64.part05d \
  .patch_payload/contact_coverage_bundle.b64.part06 \
  .patch_payload/contact_coverage_bundle.b64.part07 \
  > /tmp/contact-coverage.b64
base64 --decode /tmp/contact-coverage.b64 > /tmp/contact-coverage.tar.gz
echo "1f259a403e9f39f2b00e772e34d42ad1d5722a45d40c19e33b6b2b797633045e  /tmp/contact-coverage.tar.gz" | sha256sum --check -
tar -xzf /tmp/contact-coverage.tar.gz -C .
python scripts/run_contact_coverage_patch_once.py
python -m py_compile \
  company_scout/public_enrichment/structured_contact_extractor.py \
  company_scout/public_enrichment/official_site_discovery.py \
  company_scout/public_enrichment/official_site_phone_enricher.py \
  scripts/import_g_contact_artifact.py \
  scripts/g_contact_seed.py \
  scripts/enrich_g_contact_targets.py \
  scripts/build_g37_41_fuma.py

: "${G_CONTACT_ARTIFACT_KEY:?G_CONTACT_ARTIFACT_KEY is required}"
(( ${#G_CONTACT_ARTIFACT_KEY} >= 32 ))
umask 077
test -s inbound/g-contact-batch.tar.gz.enc
mkdir -p plaintext
openssl enc -d -aes-256-cbc -pbkdf2 \
  -pass env:G_CONTACT_ARTIFACT_KEY \
  -in inbound/g-contact-batch.tar.gz.enc |
  tar -C plaintext -xzf -
test -s plaintext/g_public_contacts.csv
test -s plaintext/g_collection_summary.json

OUTPUT_DIR="data/info_communications_contacts"
rm -rf "$OUTPUT_DIR/parts"
mkdir -p "$OUTPUT_DIR/parts"
python scripts/import_g_contact_artifact.py \
  --contacts plaintext/g_public_contacts.csv \
  --manual "$OUTPUT_DIR/manual_verified_official_contacts_20260821.csv" \
  --output "$OUTPUT_DIR/official_contact_candidates.csv" \
  --zip-dir "$OUTPUT_DIR/parts" \
  --summary "$OUTPUT_DIR/collection_summary.json" \
  --source-run-id "32759320358"
rm -rf plaintext inbound

python -m pip install \
  -r company_scout/public_enrichment/requirements.txt \
  "pytest>=8,<10" \
  "duckdb>=1.5.5,<2" \
  "openpyxl>=3.1,<4"
python -m pytest company_scout/public_enrichment/tests -q
python -m pytest tests/test_g_contact_target_enrichment.py -q

python - <<'PY'
import csv
import json
import zipfile
from pathlib import Path

root = Path("data/info_communications_contacts")
seed = root / "official_contact_candidates.csv"
summary = json.loads((root / "collection_summary.json").read_text(encoding="utf-8"))
with seed.open("r", encoding="utf-8-sig", newline="") as handle:
    reader = csv.DictReader(handle)
    headers = list(reader.fieldnames or [])
    rows = list(reader)
required = {
    "corporate_number", "official_website_url", "phone",
    "phone_type", "evidence_url", "confidence", "source_kind",
}
if not required.issubset(headers):
    raise SystemExit(f"public seed is missing fields: {sorted(required.difference(headers))}")
forbidden = ("fuma", "nokizal", "source_row_number", "raw_json")
if any(token in header.casefold() for header in headers for token in forbidden):
    raise SystemExit("source-platform-only field leaked into the public seed")
if not rows or int(summary.get("contact_candidates") or 0) != len(rows):
    raise SystemExit("public seed row count does not match its summary")
if int(summary.get("companies_with_voice_candidates") or 0) < 1:
    raise SystemExit("public seed contains no voice-phone companies")
zip_paths = sorted((root / "parts").glob("*.zip"))
if not zip_paths:
    raise SystemExit("middle-code ZIP parts were not generated")
for path in zip_paths:
    with zipfile.ZipFile(path) as archive:
        if len(archive.namelist()) != 1:
            raise SystemExit(f"unexpected ZIP layout: {path}")
print(json.dumps({
    "contact_candidates": len(rows),
    "companies_with_candidates": summary.get("companies_with_candidates"),
    "voice_companies": summary.get("companies_with_voice_candidates"),
    "manual_rows_matched": summary.get("manual_rows_matched"),
    "manual_rows_unmatched": summary.get("manual_rows_unmatched"),
    "zip_parts": len(zip_paths),
}, ensure_ascii=False))
PY

git diff --check
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add -A
git reset -- \
  .github/workflows \
  .patch_payload \
  MANIFEST.sha256 \
  scripts/apply_contact_coverage_patch_once.py \
  scripts/run_contact_coverage_patch_once.py \
  scripts/apply_contact_coverage_v3_once.sh
git diff --cached --check
git diff --cached --quiet && { echo "No implementation changes required"; exit 0; }
git commit -m "feat: expand information-communications contact coverage"
git push origin HEAD:feat/info-communications-contact-coverage
