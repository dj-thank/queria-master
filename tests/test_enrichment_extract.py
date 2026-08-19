from __future__ import annotations

from queria_master.enrichment_extract import extract_contact_records


def test_html_extractor_keeps_explicit_values_and_defaults_to_review() -> None:
    html = """
    <html><body>
      <script type="application/ld+json">
        {"@type":"Organization","url":"https://example.jp/",
         "telephone":"03-1234-5678","email":"info@example.jp"}
      </script>
      <p>お問い合わせ info@example.jp</p>
      <a href="tel:+81312345678">03-1234-5678</a>
      <a href="/contact">お問い合わせフォーム</a>
    </body></html>
    """
    records = extract_contact_records(html, "1234567890123", "https://example.jp/company")
    contacts = [record for record in records if record["kind"] == "contact"]
    websites = [record for record in records if record["kind"] == "website"]

    assert {record["contact_type"] for record in contacts} == {"email", "phone", "form_url"}
    assert {record["value"] for record in contacts if record["contact_type"] == "email"} == {"info@example.jp"}
    assert {record["value"] for record in contacts if record["contact_type"] == "phone"} == {"0312345678"}
    assert {record["value"] for record in contacts if record["contact_type"] == "form_url"} == {
        "https://example.jp/contact"
    }
    assert all(record["sales_eligibility"] == "review" for record in contacts)
    assert websites[0]["website_role"] == "official_homepage"
    assert all(record["source_url"] == "https://example.jp/company" for record in records)
    assert all(record["content_sha256"] for record in records)
