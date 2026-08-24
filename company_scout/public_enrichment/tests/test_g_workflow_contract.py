from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "g-contact-collection.yml"


class GContactWorkflowContractTests(unittest.TestCase):
    def test_workflow_is_manual_resumable_and_progress_strict(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("name: G37-G41 official contact collection", text)
        self.assertIn("workflow_dispatch:", text)
        self.assertNotIn("pull_request:", text)
        self.assertIn("prior_run_id:", text)
        self.assertIn("actions: read", text)
        self.assertIn("phone_targets_g37_41.csv", text)
        self.assertIn("queria_runtime_g_fuma.duckdb", text)
        self.assertIn("enrich_g_contact_targets.py", text)
        self.assertIn("ses_priority_json.py prioritize-targets", text)
        self.assertIn("phone_targets_g37_41_prioritized.csv", text)
        self.assertIn("g_ses_priority_seed_summary.json", text)
        self.assertIn("jsic_g37_41_collection.py", text)
        self.assertIn("gh run download", text)
        self.assertIn('PRIOR_ARTIFACT_COUNT="$(gh api', text)
        self.assertIn('if [[ "$PRIOR_ARTIFACT_COUNT" == "1" ]]', text)
        self.assertIn('elif [[ "$PRIOR_ARTIFACT_COUNT" == "0" ]]', text)
        self.assertIn("Prior artifact for shard ${SHARD} is absent; starting this shard fresh.", text)
        self.assertIn("cmp --silent", text)
        self.assertIn("--progress", text)
        self.assertIn("--retry-missing-profile", text)
        self.assertIn("ses_priority_json.py export", text)
        self.assertIn("--manifest '../../batches/decrypted/**/manifest.csv'", text)
        self.assertIn("it-subsidiary-ses-priority-v1.schema.json", text)
        self.assertIn("g_ses_priority_profiles.jsonl", text)
        self.assertIn("g_ses_priority_profiles.csv", text)
        self.assertIn("g_ses_priority_summary.json", text)
        self.assertIn("--scope-label G37-G41", text)
        self.assertNotIn("--legacy-manifest-completion", text)
        self.assertIn("retention-days: 90", text)
        self.assertIn("secrets.G_CONTACT_ARTIFACT_KEY", text)
        self.assertIn("openssl enc -aes-256-cbc -pbkdf2 -salt", text)
        self.assertIn("openssl enc -d -aes-256-cbc -pbkdf2", text)
        self.assertIn("g-contact-targets.tar.gz.enc", text)
        self.assertIn("g-contact-batch.tar.gz.enc", text)
        self.assertNotIn("path: company_scout/public_enrichment/output/*", text)
        self.assertNotIn("path: collection/phone_targets_g37_41_enriched.csv", text)
        self.assertNotIn("path: company_scout/public_enrichment/output/g_ses_priority_profiles", text)
        self.assertIn('[[ "$START_OFFSET" =~ ^[0-9]+$ ]]', text)
        self.assertIn('[[ "$PRIOR_RUN_ID" =~ ^[0-9]+$ ]]', text)
        self.assertIn("BATCH_SIZE <= 550", text)
        self.assertIn('test -e "$WORK/progress.jsonl"', text)
        self.assertNotIn('test -s "$WORK/progress.jsonl"', text)


if __name__ == "__main__":
    unittest.main()
