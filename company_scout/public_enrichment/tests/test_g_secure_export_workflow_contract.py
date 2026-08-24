from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "g-contact-secure-export.yml"


class GSecureExportWorkflowContractTests(unittest.TestCase):
    def test_export_is_manual_encrypted_and_plaintext_free(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("name: G contact secure export", text)
        self.assertIn("workflow_dispatch:", text)
        self.assertNotIn("pull_request:", text)
        self.assertIn("actions: read", text)
        self.assertIn("secrets.G_CONTACT_ARTIFACT_KEY", text)
        self.assertIn("run-id: ${{ inputs.source_run_id }}", text)
        self.assertIn("g-contact-batch.tar.gz.enc", text)
        self.assertIn("rsa_padding_mode:oaep", text)
        self.assertIn("rsa_oaep_md:sha256", text)
        self.assertIn("rsa_mgf1_md:sha256", text)
        self.assertIn("g-contact-export.tar.gz.enc", text)
        self.assertIn("g-contact-export-key.rsa", text)
        self.assertIn('"plaintext_uploaded": false', text)
        self.assertIn("PROFILE_FILE_COUNT", text)
        self.assertIn("g_ses_priority_profiles.jsonl", text)
        self.assertIn('"ses_priority_profiles_included"', text)
        self.assertIn('"plaintext_file_count"', text)
        self.assertIn("rm -rf plaintext inbound", text)
        self.assertIn("retention-days: 1", text)
        self.assertNotIn("path: plaintext", text)
        self.assertNotIn("path: export/g-contact-export.tar.gz\n", text)
        self.assertNotIn("path: export/passphrase", text)
        self.assertNotIn("path: export/recipient-public.pem", text)


if __name__ == "__main__":
    unittest.main()
