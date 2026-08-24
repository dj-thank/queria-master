from __future__ import annotations

import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

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

    def test_pinned_adapter_connects_to_validated_ip_with_original_tls_name(self) -> None:
        adapter = phone.PublicPinnedHTTPAdapter(max_retries=0)
        pool_manager = Mock()
        connection = object()
        pool_manager.connection_from_host.return_value = connection
        adapter.poolmanager = pool_manager
        request = requests.Request("GET", "https://example.com/contact?q=1").prepare()

        with patch.object(socket, "getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        ]):
            result = adapter.get_connection_with_tls_context(request, True, proxies={})

        self.assertIs(result, connection)
        call = pool_manager.connection_from_host.call_args
        self.assertEqual(call.kwargs["host"], "93.184.216.34")
        self.assertEqual(call.kwargs["scheme"], "https")
        self.assertEqual(call.kwargs["port"], 443)
        self.assertEqual(call.kwargs["pool_kwargs"]["server_hostname"], "example.com")
        self.assertEqual(call.kwargs["pool_kwargs"]["assert_hostname"], "example.com")

    def test_pinned_adapter_rejects_dns_rebind_before_connection(self) -> None:
        adapter = phone.PublicPinnedHTTPAdapter(max_retries=0)
        adapter.poolmanager = Mock()
        request = requests.Request("GET", "https://example.test/").prepare()
        resolutions = [
            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
        ]

        with patch.object(socket, "getaddrinfo", side_effect=resolutions):
            self.assertTrue(phone.is_public_http_url(request.url))
            with self.assertRaises(requests.exceptions.InvalidURL):
                adapter.get_connection_with_tls_context(request, True, proxies={})

        adapter.poolmanager.connection_from_host.assert_not_called()

    def test_pinned_adapter_preserves_original_host_header_and_rejects_proxies(self) -> None:
        adapter = phone.PublicPinnedHTTPAdapter(max_retries=0)
        request = requests.Request("GET", "https://example.com:443/contact?q=1").prepare()
        response = object()

        with patch.object(requests.adapters.HTTPAdapter, "send", return_value=response):
            self.assertIs(adapter.send(request, proxies={}), response)
        self.assertEqual(request.headers["Host"], "example.com")
        self.assertEqual(request.path_url, "/contact?q=1")

        with self.assertRaises(requests.exceptions.ProxyError):
            adapter.send(request, proxies={"https": "http://proxy.example:8080"})

    def test_safe_session_disables_environment_and_mounts_pinned_adapter(self) -> None:
        session = phone.build_safe_session()
        try:
            self.assertFalse(session.trust_env)
            self.assertIsInstance(session.get_adapter("https://example.com/"), phone.PublicPinnedHTTPAdapter)
            self.assertIsInstance(session.get_adapter("http://example.com/"), phone.PublicPinnedHTTPAdapter)
        finally:
            session.close()

    def test_truncated_chunked_response_is_a_site_failure_not_a_process_failure(self) -> None:
        class TruncatedResponse:
            status_code = 200
            headers: dict[str, str] = {}
            encoding = "utf-8"

            def __init__(self) -> None:
                self.closed = False

            def iter_content(self, *, chunk_size: int):
                self.chunk_size = chunk_size
                raise requests.exceptions.ChunkedEncodingError("Response ended prematurely")

            def close(self) -> None:
                self.closed = True

        response = TruncatedResponse()
        session = phone.build_safe_session()
        try:
            with patch.object(phone, "is_public_http_url", return_value=True), patch.object(
                session, "get", return_value=response
            ):
                self.assertIsNone(
                    phone.safe_get_text(
                        session,
                        "https://example.com/company",
                        expected_host="example.com",
                        timeout=20,
                        max_bytes=phone.MAX_HTML_BYTES,
                    )
                )
        finally:
            session.close()
        self.assertTrue(response.closed)

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
