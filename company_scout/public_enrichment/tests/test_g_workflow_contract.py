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
        self.assertIn("jsic_g37_41_collection.py", text)
        self.assertIn("gh run download", text)
        self.assertIn("cmp --silent", text)
        self.assertIn("--progress", text)
        self.assertIn("--scope-label G37-G41", text)
        self.assertNotIn("--legacy-manifest-completion", text)
        self.assertIn("retention-days: 90", text)
        self.assertIn('[[ "$START_OFFSET" =~ ^[0-9]+$ ]]', text)
        self.assertIn('[[ "$PRIOR_RUN_ID" =~ ^[0-9]+$ ]]', text)


if __name__ == "__main__":
    unittest.main()
