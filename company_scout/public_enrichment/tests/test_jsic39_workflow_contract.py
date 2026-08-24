from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "jsic39-contact-collection.yml"


class Jsic39WorkflowContractTests(unittest.TestCase):
    def test_public_repo_artifacts_are_encrypted_and_fail_closed(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("name: JSIC 39 official contact collection", text)
        self.assertIn("workflow_dispatch:", text)
        self.assertIn("pull_request:", text)
        self.assertIn("secrets.G_CONTACT_ARTIFACT_KEY", text)
        self.assertGreaterEqual(text.count("${#G_CONTACT_ARTIFACT_KEY} >= 32"), 6)
        self.assertEqual(text.count("uses: actions/upload-artifact@v4"), 3)
        self.assertGreaterEqual(text.count("openssl enc -aes-256-cbc -pbkdf2 -salt"), 3)
        self.assertGreaterEqual(text.count("openssl enc -d -aes-256-cbc -pbkdf2"), 3)
        self.assertIn("collection/jsic39-public-index.tar.gz.enc", text)
        self.assertIn("jsic39-phone-${{ matrix.shard }}.tar.gz.enc", text)
        self.assertIn("output/jsic39-contact-batch.tar.gz.enc", text)
        self.assertIn("Expected 8 encrypted JSIC39 shard artifacts.", text)
        self.assertIn('test -e "$WORK/progress.jsonl"', text)
        self.assertIn('[[ "$START_OFFSET" =~ ^[0-9]+$ ]]', text)
        self.assertIn('[[ "$BATCH_SIZE" =~ ^[0-9]+$ ]]', text)
        self.assertIn('[[ "$MAX_PAGES" =~ ^[0-9]+$ ]]', text)
        self.assertNotIn("path: collection/*", text)
        self.assertNotIn("path: company_scout/public_enrichment/work/shard-${{ matrix.shard }}/*", text)
        self.assertNotIn("path: company_scout/public_enrichment/output/*", text)
        self.assertNotIn('test -s "$WORK/progress.jsonl"', text)


if __name__ == "__main__":
    unittest.main()
