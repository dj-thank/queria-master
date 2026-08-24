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

    def test_explicit_robots_denial_is_not_reported_as_processed_no_phone(self) -> None:
        def fake_get(_session, url: str, **_kwargs):
            if url.endswith("/robots.txt"):
                return url, 200, {"Content-Type": "text/plain"}, "User-agent: *\nDisallow: /"
            raise AssertionError("a denied site must not be fetched")

        with patch.object(phone, "is_public_http_url", return_value=True), patch.object(
            phone, "safe_get_text", side_effect=fake_get
        ):
            result = phone.discover_site_result(object(), "https://example.com/", 4, 20, 0)

        self.assertEqual(result["state"], "blocked_by_policy")
        self.assertEqual(result["reason"], "robots_disallow")
        self.assertEqual(result["pages_fetched"], 0)

    def test_unavailable_robots_policy_requires_review_instead_of_crawling(self) -> None:
        with patch.object(phone, "is_public_http_url", return_value=True), patch.object(
            phone, "safe_get_text", return_value=None
        ):
            result = phone.discover_site_result(object(), "https://example.com/", 4, 20, 0)

        self.assertEqual(result["state"], "needs_review")
        self.assertEqual(result["reason"], "robots_unavailable")
        self.assertEqual(result["pages_fetched"], 0)

    def test_failed_page_fetch_is_not_reported_as_processed_no_phone(self) -> None:
        def fake_get(_session, url: str, **_kwargs):
            if url.endswith("/robots.txt"):
                return url, 404, {"Content-Type": "text/plain"}, ""
            return None

        with patch.object(phone, "is_public_http_url", return_value=True), patch.object(
            phone, "safe_get_text", side_effect=fake_get
        ):
            result = phone.discover_site_result(object(), "https://example.com/", 4, 20, 0)

        self.assertEqual(result["state"], "needs_review")
        self.assertEqual(result["reason"], "fetch_failed")
        self.assertEqual(result["pages_fetched"], 0)

    def test_successful_html_without_phone_is_processed_no_phone(self) -> None:
        def fake_get(_session, url: str, **_kwargs):
            if url.endswith("/robots.txt"):
                return url, 404, {"Content-Type": "text/plain"}, ""
            return url, 200, {"Content-Type": "text/html; charset=utf-8"}, "<html><body>会社概要</body></html>"

        with patch.object(phone, "is_public_http_url", return_value=True), patch.object(
            phone, "safe_get_text", side_effect=fake_get
        ):
            result = phone.discover_site_result(object(), "https://example.com/", 4, 20, 0)

        self.assertEqual(result["state"], "processed_no_phone")
        self.assertIsNone(result["reason"])
        self.assertEqual(result["pages_fetched"], 1)

    def test_fax_only_page_is_not_counted_as_voice_phone_success(self) -> None:
        def fake_get(_session, url: str, **_kwargs):
            if url.endswith("/robots.txt"):
                return url, 404, {"Content-Type": "text/plain"}, ""
            return url, 200, {"Content-Type": "text/html; charset=utf-8"}, "<html><body>FAX 03-1234-5678</body></html>"

        with patch.object(phone, "is_public_http_url", return_value=True), patch.object(
            phone, "safe_get_text", side_effect=fake_get
        ):
            result = phone.discover_site_result(object(), "https://example.com/", 4, 20, 0)

        self.assertEqual(result["state"], "fax_only")
        self.assertEqual(result["reason"], "fax_only")
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["candidate_type"], "FAX")


if __name__ == "__main__":
    unittest.main()
