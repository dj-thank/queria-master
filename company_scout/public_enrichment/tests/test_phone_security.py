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


if __name__ == "__main__":
    unittest.main()
