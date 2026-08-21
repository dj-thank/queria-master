from __future__ import annotations

import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import official_site_phone_enricher as phone


class PhoneSecurityTests(unittest.TestCase):
    def test_rejects_local_and_private_destinations(self) -> None:
        self.assertFalse(phone.is_public_http_url("http://localhost/"))
        self.assertFalse(phone.is_public_http_url("file:///etc/passwd"))
        with patch.object(socket, "getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]):
            self.assertFalse(phone.is_public_http_url("http://example.test/"))
        with patch.object(socket, "getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.2", 443))]):
            self.assertFalse(phone.is_public_http_url("https://example.test/"))

    def test_accepts_public_https_destination(self) -> None:
        with patch.object(socket, "getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]):
            self.assertTrue(phone.is_public_http_url("https://example.com/contact"))

    def test_candidate_scoring_penalizes_fax(self) -> None:
        tel = phone.score_candidate("0312345678", "代表 TEL", "text")
        fax = phone.score_candidate("0312345678", "FAX", "text")
        self.assertGreater(tel, fax)

    def test_visible_contact_number_beats_unlabelled_hidden_tel_link(self) -> None:
        html = """
        <html><body>
          <a href="tel:0487574535" aria-hidden="true"></a>
          <p>お電話によるお問い合わせは 0800-080-9696（無料通話）まで</p>
        </body></html>
        """
        candidates, _links = phone.extract_candidates("https://example.com/", html)
        winner = max(candidates, key=phone.candidate_sort_key)
        self.assertEqual(winner["phone"], "08000809696")
        self.assertEqual(winner["source"], "text")
        self.assertIn("お問い合わせ", winner["context"])

    def test_visible_context_is_retained_when_tel_href_has_same_number(self) -> None:
        html = """
        <html><body>
          <p>会社概要　代表電話 <a href="tel:0312345678">03-1234-5678</a></p>
        </body></html>
        """
        candidates, _links = phone.extract_candidates("https://example.com/company", html)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["phone"], "0312345678")
        self.assertEqual(candidates[0]["source"], "text")
        self.assertIn("代表電話", candidates[0]["context"])


if __name__ == "__main__":
    unittest.main()
