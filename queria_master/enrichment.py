from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence
from urllib.parse import urljoin, urlsplit, urlunsplit

from .resources import DEFAULT_DB, PROJECT_ROOT


ENRICHMENT_SCHEMA_VERSION = "6"
DEFAULT_ENRICHMENT_DB = PROJECT_ROOT / "data" / "queria_enrichment.duckdb"
ENRICHMENT_STATES = frozenset(
    {
        "pending",
        "leased",
        "found",
        "verified",
        "waiting_for_dependency",
        "not_found_after_policy",
        "not_applicable",
        "needs_review",
        "blocked_by_policy",
        "failed",
    }
)
ENRICHMENT_TASK_FIELDS = frozenset(
    {"website_discovery", "website_verification", "contact_extraction", "location"}
)
CONTACT_TYPES = frozenset({"phone", "email", "fax", "form_url"})
WEBSITE_ROLES = frozenset({"official_candidate", "official_homepage", "contact_page", "form_page"})
SALES_ELIGIBILITY = frozenset({"allowed", "review", "not_allowed"})


class EnrichmentError(RuntimeError):
    """Evidence-layer validation, migration, or database error."""


class _WriterLock:
    """Cross-process, crash-recoverable lock for the companion DuckDB writer."""

    def __init__(self, database_path: Path, *, timeout_seconds: float = 120.0) -> None:
        self.path = Path(database_path).resolve().with_suffix(
            Path(database_path).suffix + ".writer.lock"
        )
        self.timeout_seconds = timeout_seconds
        self._fd: int | None = None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == "nt":
            # ``os.kill(pid, 0)`` is not a reliable liveness probe on
            # Windows: it can fail for a live process, which would make a
            # second writer delete the first writer's still-open lock file.
            # Query the process exit code instead, and keep the lock when
            # Windows refuses the query so that we never steal a live lock.
            try:
                import ctypes
                from ctypes import wintypes

                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                process_query_limited_information = 0x1000
                still_active = 259
                kernel32.OpenProcess.argtypes = [
                    wintypes.DWORD,
                    wintypes.BOOL,
                    wintypes.DWORD,
                ]
                kernel32.OpenProcess.restype = wintypes.HANDLE
                kernel32.GetExitCodeProcess.argtypes = [
                    wintypes.HANDLE,
                    ctypes.POINTER(wintypes.DWORD),
                ]
                kernel32.GetExitCodeProcess.restype = wintypes.BOOL
                kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
                kernel32.CloseHandle.restype = wintypes.BOOL

                handle = kernel32.OpenProcess(
                    process_query_limited_information,
                    False,
                    pid,
                )
                if not handle:
                    # ERROR_INVALID_PARAMETER means that the PID no longer
                    # exists. Access/other query failures are treated as
                    # alive because deleting that lock would be unsafe.
                    return ctypes.get_last_error() != 87
                try:
                    exit_code = wintypes.DWORD()
                    if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                        return True
                    return exit_code.value == still_active
                finally:
                    kernel32.CloseHandle(handle)
            except (ImportError, OSError):
                return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def acquire(self) -> None:
        deadline = time.monotonic() + self.timeout_seconds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self._fd, f"pid={os.getpid()}\nstarted_at={_now()}\n".encode("utf-8"))
                return
            except FileExistsError:
                owner_pid: int | None = None
                try:
                    text = self.path.read_text(encoding="utf-8", errors="replace")
                    match = re.search(r"^pid=(\d+)$", text, re.MULTILINE)
                    if match:
                        owner_pid = int(match.group(1))
                except OSError:
                    pass
                if owner_pid is not None and not self._pid_alive(owner_pid):
                    try:
                        self.path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    owner = f"pid={owner_pid}" if owner_pid is not None else "owner=unknown"
                    raise EnrichmentError(
                        f"拡張DBのwriter lockを取得できません ({owner}): {self.path}"
                    )
                time.sleep(0.1)

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass


def _duckdb():
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - project runtime installs DuckDB.
        raise EnrichmentError("duckdb がありません。セットアップを先に実行してください。") from exc
    return duckdb


def _open_writer(database_path: Path) -> tuple[Any, _WriterLock]:
    lock = _WriterLock(database_path)
    lock.acquire()
    try:
        return _duckdb().connect(str(Path(database_path).resolve()), read_only=False), lock
    except Exception:
        lock.release()
        raise


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_timestamp(value: Any) -> str:
    if value in (None, ""):
        return _now()
    text = str(value).strip()
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EnrichmentError(f"日時がISO-8601ではありません: {value}") from exc
    return text


def _new_id() -> str:
    return str(uuid.uuid4())


def _require_corporate_number(value: Any) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{13}", text):
        raise EnrichmentError(f"法人番号は13桁数字で指定してください: {value}")
    return text


def normalize_url(value: Any, *, base_url: str | None = None) -> str:
    text = str(value or "").strip()
    if not text:
        raise EnrichmentError("URLが空です。")
    if text.startswith("//"):
        text = "https:" + text
    elif "://" not in text and base_url:
        text = urljoin(base_url, text)
    parts = urlsplit(text)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise EnrichmentError(f"http/https URLではありません: {value}")
    try:
        host = parts.hostname.encode("idna").decode("ascii").lower()
        port = parts.port
    except (UnicodeError, ValueError) as exc:
        raise EnrichmentError(f"URLのホスト名またはポートが不正です: {value}") from exc
    # urlunsplit requires bracketed IPv6 literals; IDNA hostnames and IPv4 do
    # not contain a colon.
    netloc = f"[{host}]" if ":" in host else host
    if port and not ((parts.scheme.lower() == "http" and port == 80) or (parts.scheme.lower() == "https" and port == 443)):
        netloc += f":{port}"
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), netloc, path, parts.query, ""))


def normalize_email(value: Any) -> str:
    text = str(value or "").strip().casefold()
    if text.lower().startswith("mailto:"):
        text = text[7:].split("?", 1)[0].strip()
    _display, address = parseaddr(text)
    if address != text or not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", text):
        raise EnrichmentError(f"メールアドレスの形式が不正です: {value}")
    return text


def normalize_phone(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise EnrichmentError("電話番号が空です。")
    compact = re.sub(r"[^0-9+]", "", text)
    if compact.startswith("+81"):
        compact = "0" + compact[3:]
    if not re.fullmatch(r"0\d{9,10}", compact):
        raise EnrichmentError(f"電話番号の形式が不正です: {value}")
    return compact


def normalize_contact(contact_type: str, value: Any) -> str:
    if contact_type == "email":
        return normalize_email(value)
    if contact_type == "phone" or contact_type == "fax":
        return normalize_phone(value)
    if contact_type == "form_url":
        return normalize_url(value)
    raise EnrichmentError(f"未知の連絡先種別です: {contact_type}")


def normalize_suppression_value(suppression_type: str, value: Any) -> str:
    if suppression_type == "email":
        return normalize_email(value)
    if suppression_type == "phone":
        return normalize_phone(value)
    if suppression_type == "domain":
        text = str(value or "").strip().casefold().lstrip(".")
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", text):
            raise EnrichmentError(f"ドメインの形式が不正です: {value}")
        return text
    if suppression_type == "corporate_number":
        return _require_corporate_number(value)
    raise EnrichmentError(f"未知の抑止種別です: {suppression_type}")


def hash_normalized(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS enrichment;
CREATE SCHEMA IF NOT EXISTS compliance;
CREATE SCHEMA IF NOT EXISTS crm;

CREATE TABLE IF NOT EXISTS enrichment.schema_meta (
    schema_name VARCHAR PRIMARY KEY,
    schema_version VARCHAR NOT NULL,
    initialized_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS enrichment.evidence_documents (
    evidence_id VARCHAR PRIMARY KEY,
    corporate_number VARCHAR,
    source_key VARCHAR NOT NULL,
    source_url VARCHAR,
    retrieved_at TIMESTAMPTZ NOT NULL,
    published_at TIMESTAMPTZ,
    http_status INTEGER,
    content_type VARCHAR,
    content_sha256 VARCHAR,
    extractor_version VARCHAR,
    robots_status VARCHAR,
    policy_status VARCHAR NOT NULL,
    evidence_status VARCHAR NOT NULL,
    title VARCHAR,
    notes VARCHAR
);

CREATE TABLE IF NOT EXISTS enrichment.company_websites (
    website_id VARCHAR PRIMARY KEY,
    corporate_number VARCHAR NOT NULL,
    url VARCHAR NOT NULL,
    normalized_url VARCHAR NOT NULL,
    website_role VARCHAR NOT NULL,
    discovery_method VARCHAR NOT NULL,
    source_evidence_id VARCHAR,
    status VARCHAR NOT NULL,
    confidence DOUBLE,
    robots_status VARCHAR,
    http_status INTEGER,
    canonical_url VARCHAR,
    first_seen_at TIMESTAMPTZ NOT NULL,
    checked_at TIMESTAMPTZ,
    last_error VARCHAR,
    UNIQUE (corporate_number, normalized_url, website_role)
);

CREATE TABLE IF NOT EXISTS enrichment.company_contact_points (
    contact_id VARCHAR PRIMARY KEY,
    corporate_number VARCHAR NOT NULL,
    contact_type VARCHAR NOT NULL,
    value_raw VARCHAR,
    value_normalized VARCHAR NOT NULL,
    scope VARCHAR NOT NULL,
    publicness VARCHAR NOT NULL,
    source_evidence_id VARCHAR,
    source_url VARCHAR,
    observed_at TIMESTAMPTZ NOT NULL,
    status VARCHAR NOT NULL,
    confidence DOUBLE,
    verification_status VARCHAR NOT NULL,
    sales_eligibility VARCHAR NOT NULL,
    last_error VARCHAR,
    UNIQUE (corporate_number, contact_type, value_normalized)
);

CREATE TABLE IF NOT EXISTS enrichment.contact_reviews (
    review_id VARCHAR PRIMARY KEY,
    contact_id VARCHAR NOT NULL,
    corporate_number VARCHAR NOT NULL,
    contact_type VARCHAR NOT NULL,
    value_normalized VARCHAR NOT NULL,
    previous_sales_eligibility VARCHAR NOT NULL,
    decision VARCHAR NOT NULL,
    reviewer VARCHAR NOT NULL,
    reason VARCHAR NOT NULL,
    reviewed_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS enrichment.company_locations (
    location_id VARCHAR PRIMARY KEY,
    corporate_number VARCHAR NOT NULL,
    location_type VARCHAR NOT NULL,
    address_raw VARCHAR,
    address_normalized VARCHAR NOT NULL,
    postal_code VARCHAR,
    prefecture_name VARCHAR,
    city_name VARCHAR,
    source_evidence_id VARCHAR,
    observed_at TIMESTAMPTZ NOT NULL,
    status VARCHAR NOT NULL,
    confidence DOUBLE,
    UNIQUE (corporate_number, location_type, address_normalized)
);

CREATE TABLE IF NOT EXISTS enrichment.company_establishments (
    establishment_id VARCHAR PRIMARY KEY,
    corporate_number VARCHAR NOT NULL,
    source_key VARCHAR NOT NULL,
    source_record_id VARCHAR NOT NULL,
    service_type VARCHAR,
    establishment_name VARCHAR,
    address VARCHAR,
    phone_raw VARCHAR,
    phone_normalized VARCHAR,
    fax_raw VARCHAR,
    url VARCHAR,
    contact_scope VARCHAR NOT NULL,
    source_evidence_id VARCHAR,
    observed_at TIMESTAMPTZ NOT NULL,
    status VARCHAR NOT NULL,
    confidence DOUBLE,
    UNIQUE (source_key, source_record_id, service_type, corporate_number)
);

CREATE TABLE IF NOT EXISTS enrichment.enrichment_state (
    corporate_number VARCHAR NOT NULL,
    field_name VARCHAR NOT NULL,
    source_key VARCHAR NOT NULL,
    state VARCHAR NOT NULL,
    attempt_count INTEGER NOT NULL,
    input_fingerprint VARCHAR,
    policy_code VARCHAR,
    worker_run_id VARCHAR,
    lease_owner VARCHAR,
    lease_token VARCHAR,
    lease_until TIMESTAMPTZ,
    next_attempt_at TIMESTAMPTZ,
    last_started_at TIMESTAMPTZ,
    last_completed_at TIMESTAMPTZ,
    last_error VARCHAR,
    last_evidence_id VARCHAR,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (corporate_number, field_name, source_key)
);

CREATE TABLE IF NOT EXISTS compliance.suppressions (
    suppression_id VARCHAR PRIMARY KEY,
    corporate_number VARCHAR,
    suppression_type VARCHAR NOT NULL,
    value_normalized VARCHAR NOT NULL,
    value_sha256 VARCHAR NOT NULL,
    reason VARCHAR NOT NULL,
    source VARCHAR NOT NULL,
    source_evidence_id VARCHAR,
    effective_from TIMESTAMPTZ NOT NULL,
    effective_to TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    notes VARCHAR,
    UNIQUE (corporate_number, suppression_type, value_sha256)
);

CREATE INDEX IF NOT EXISTS idx_enrichment_websites_corporate
    ON enrichment.company_websites(corporate_number, status);
CREATE INDEX IF NOT EXISTS idx_enrichment_contacts_corporate
    ON enrichment.company_contact_points(corporate_number, contact_type, status);
CREATE INDEX IF NOT EXISTS idx_enrichment_contacts_value
    ON enrichment.company_contact_points(contact_type, value_normalized);
CREATE INDEX IF NOT EXISTS idx_enrichment_contact_reviews_contact
    ON enrichment.contact_reviews(contact_id, reviewed_at);
CREATE INDEX IF NOT EXISTS idx_enrichment_establishment_company
    ON enrichment.company_establishments(corporate_number, status);
CREATE INDEX IF NOT EXISTS idx_enrichment_establishment_phone
    ON enrichment.company_establishments(phone_normalized);
CREATE INDEX IF NOT EXISTS idx_enrichment_state_queue
    ON enrichment.enrichment_state(state, next_attempt_at, lease_until);
CREATE INDEX IF NOT EXISTS idx_compliance_suppressions_corporate
    ON compliance.suppressions(corporate_number, suppression_type);
"""


VIEW_SQL_TEMPLATE = """
CREATE OR REPLACE VIEW crm.v_resolved_company_websites AS
SELECT
    corporate_number,
    normalized_url AS official_url,
    source_evidence_id,
    status,
    confidence,
    checked_at
FROM enrichment.company_websites
WHERE status = 'verified'
  AND website_role = 'official_homepage'
QUALIFY row_number() OVER (
    PARTITION BY corporate_number
    ORDER BY confidence DESC NULLS LAST,
             checked_at DESC NULLS LAST,
             first_seen_at DESC,
             normalized_url,
             website_id
) = 1;

CREATE OR REPLACE VIEW crm.v_resolved_company_contacts AS
SELECT
    p.corporate_number,
    p.contact_type,
    p.value_normalized,
    p.source_evidence_id,
    p.source_url,
    p.observed_at,
    p.status,
    p.confidence,
    p.verification_status,
    p.sales_eligibility
FROM enrichment.company_contact_points p
WHERE p.status IN ('found', 'verified')
  AND p.sales_eligibility = 'allowed'
  AND NOT EXISTS (
      SELECT 1
      FROM compliance.suppressions s
      WHERE (s.corporate_number = p.corporate_number OR s.corporate_number IS NULL)
        AND (
            (s.suppression_type = p.contact_type AND s.value_sha256 = sha256(p.value_normalized))
            OR (
                s.suppression_type = 'domain'
                AND p.contact_type = 'email'
                AND split_part(p.value_normalized, '@', 2) = s.value_normalized
            )
        )
        AND (s.effective_from IS NULL OR s.effective_from <= current_timestamp)
        AND (s.effective_to IS NULL OR s.effective_to > current_timestamp)
  )
QUALIFY row_number() OVER (
    PARTITION BY p.corporate_number, p.contact_type
    ORDER BY CASE WHEN p.status = 'verified' THEN 0 ELSE 1 END,
             p.confidence DESC NULLS LAST,
             p.observed_at DESC,
             p.value_normalized,
             p.contact_id
) = 1;

CREATE OR REPLACE VIEW crm.v_resolved_company_locations AS
SELECT
    corporate_number,
    address_normalized,
    postal_code,
    prefecture_name,
    city_name,
    source_evidence_id,
    status,
    confidence,
    observed_at
FROM enrichment.company_locations
WHERE status IN ('found', 'verified')
QUALIFY row_number() OVER (
    PARTITION BY corporate_number
    ORDER BY CASE WHEN status = 'verified' THEN 0 ELSE 1 END,
             confidence DESC NULLS LAST,
             observed_at DESC,
             address_normalized,
             location_id
) = 1;

CREATE OR REPLACE VIEW crm.v_enrichment_coverage AS
WITH latest_states AS (
    SELECT corporate_number, field_name, state, updated_at, last_completed_at
    FROM enrichment.enrichment_state
    QUALIFY row_number() OVER (
        PARTITION BY corporate_number, field_name
        ORDER BY updated_at DESC, last_completed_at DESC NULLS LAST
    ) = 1
), states AS (
    SELECT
        corporate_number,
        coalesce(
            max(CASE WHEN field_name = 'website_verification' THEN state END),
            max(CASE WHEN field_name = 'website_discovery' THEN state END),
            max(CASE WHEN field_name = 'website' THEN state END)
        ) AS website_state,
        max(CASE WHEN field_name = 'contact_extraction' THEN state END) AS contact_extraction_state,
        max(CASE WHEN field_name = 'phone' THEN state END) AS phone_state,
        max(CASE WHEN field_name = 'email' THEN state END) AS email_state,
        max(CASE WHEN field_name = 'form_url' THEN state END) AS form_state,
        max(CASE WHEN field_name = 'location' THEN state END) AS location_state,
        max(updated_at) AS last_enrichment_at
    FROM latest_states
    GROUP BY corporate_number
), contact_flags AS (
    SELECT
        corporate_number,
        max(CASE WHEN contact_type = 'phone' AND status IN ('found', 'verified') THEN 1 ELSE 0 END) AS has_phone,
        max(CASE WHEN contact_type = 'email' AND status IN ('found', 'verified') THEN 1 ELSE 0 END) AS has_email,
        max(CASE WHEN contact_type = 'form_url' AND status IN ('found', 'verified') THEN 1 ELSE 0 END) AS has_form
    FROM enrichment.company_contact_points
    GROUP BY corporate_number
), resolved_flags AS (
    SELECT
        corporate_number,
        max(CASE WHEN contact_type = 'phone' THEN 1 ELSE 0 END) AS has_allowed_phone,
        max(CASE WHEN contact_type = 'email' THEN 1 ELSE 0 END) AS has_allowed_email,
        max(CASE WHEN contact_type = 'form_url' THEN 1 ELSE 0 END) AS has_allowed_form
    FROM crm.v_resolved_company_contacts
    GROUP BY corporate_number
)
SELECT
    c.corporate_number,
    c.company_name,
    c.prefecture_name,
    c.city_name,
    c.company_url AS canonical_company_url,
    coalesce(s.website_state, 'pending') AS website_state,
    coalesce(s.contact_extraction_state, 'waiting_for_dependency') AS contact_extraction_state,
    coalesce(s.phone_state, s.contact_extraction_state, 'waiting_for_dependency') AS phone_state,
    coalesce(s.email_state, s.contact_extraction_state, 'waiting_for_dependency') AS email_state,
    coalesce(s.form_state, s.contact_extraction_state, 'waiting_for_dependency') AS form_state,
    coalesce(s.location_state, 'pending') AS location_state,
    coalesce(f.has_phone, 0) = 1 AS has_phone,
    coalesce(f.has_email, 0) = 1 AS has_email,
    coalesce(f.has_form, 0) = 1 AS has_form,
    s.last_enrichment_at,
    CASE
        WHEN EXISTS (
            SELECT 1 FROM compliance.suppressions x
            WHERE (
                x.suppression_type = 'corporate_number'
                AND (
                    x.corporate_number = c.corporate_number
                    OR x.value_normalized = c.corporate_number
                )
            )
              AND (x.effective_from IS NULL OR x.effective_from <= current_timestamp)
              AND (x.effective_to IS NULL OR x.effective_to > current_timestamp)
        ) THEN 'blocked_by_policy'
        WHEN coalesce(r.has_allowed_phone, 0) = 1
          OR coalesce(r.has_allowed_email, 0) = 1
          OR coalesce(r.has_allowed_form, 0) = 1 THEN 'ready'
        WHEN coalesce(s.website_state, 'pending') IN ('pending', 'leased')
          OR coalesce(s.contact_extraction_state, 'pending') IN ('pending', 'leased')
          OR coalesce(s.phone_state, s.contact_extraction_state, 'waiting_for_dependency') IN ('pending', 'leased')
          OR coalesce(s.email_state, s.contact_extraction_state, 'waiting_for_dependency') IN ('pending', 'leased')
          OR coalesce(s.form_state, s.contact_extraction_state, 'waiting_for_dependency') IN ('pending', 'leased') THEN 'pending'
        WHEN coalesce(s.website_state, '') = 'needs_review'
          OR coalesce(s.phone_state, s.contact_extraction_state, '') = 'needs_review'
          OR coalesce(s.email_state, s.contact_extraction_state, '') = 'needs_review'
          OR coalesce(s.form_state, s.contact_extraction_state, '') = 'needs_review' THEN 'needs_review'
        ELSE 'not_found_after_policy'
    END AS sales_state
FROM __COMPANY_RELATION__ c
LEFT JOIN states s USING (corporate_number)
LEFT JOIN contact_flags f USING (corporate_number)
LEFT JOIN resolved_flags r USING (corporate_number);

CREATE OR REPLACE VIEW crm.v_sales_ready_accounts AS
WITH allowed_contacts AS (
    SELECT
        corporate_number,
        max(CASE WHEN contact_type = 'phone' THEN value_normalized END) AS phone,
        max(CASE WHEN contact_type = 'email' THEN value_normalized END) AS email,
        max(CASE WHEN contact_type = 'form_url' THEN value_normalized END) AS inquiry_form_url
    FROM crm.v_resolved_company_contacts
    GROUP BY corporate_number
), websites AS (
    SELECT corporate_number, official_url
    FROM crm.v_resolved_company_websites
), locations AS (
    SELECT corporate_number, address_normalized, postal_code, prefecture_name, city_name
    FROM crm.v_resolved_company_locations
)
SELECT
    c.corporate_number,
    c.company_name,
    coalesce(l.prefecture_name, c.prefecture_name) AS prefecture_name,
    coalesce(l.city_name, c.city_name) AS city_name,
    coalesce(l.address_normalized, c.full_address) AS address,
    c.jsic_codes_raw,
    c.business_summary,
    w.official_url,
    a.phone,
    a.email,
    a.inquiry_form_url,
    'ready' AS sales_state
FROM __COMPANY_RELATION__ c
JOIN allowed_contacts a USING (corporate_number)
LEFT JOIN websites w USING (corporate_number)
LEFT JOIN locations l USING (corporate_number)
WHERE NOT EXISTS (
    SELECT 1 FROM compliance.suppressions s
    WHERE (
        s.suppression_type = 'corporate_number'
        AND (
            s.corporate_number = c.corporate_number
            OR s.value_normalized = c.corporate_number
        )
    )
      AND (s.effective_from IS NULL OR s.effective_from <= current_timestamp)
      AND (s.effective_to IS NULL OR s.effective_to > current_timestamp)
);

CREATE OR REPLACE VIEW crm.v_company_establishment_summary AS
SELECT
    corporate_number,
    count(*) AS establishment_count,
    count(*) FILTER (WHERE phone_normalized IS NOT NULL) AS establishment_phone_count,
    count(*) FILTER (WHERE url IS NOT NULL) AS establishment_url_count,
    min(phone_normalized) FILTER (WHERE phone_normalized IS NOT NULL) AS sample_establishment_phone,
    min(url) FILTER (WHERE url IS NOT NULL) AS sample_establishment_url,
    max(observed_at) AS last_observed_at
FROM enrichment.company_establishments
WHERE status IN ('found', 'verified')
GROUP BY corporate_number;
"""


def _view_sql(company_relation: str) -> str:
    return VIEW_SQL_TEMPLATE.replace("__COMPANY_RELATION__", company_relation)


def initialize_enrichment_schema(con: Any, *, company_relation: str = "core.companies") -> None:
    """Create the evidence layer and deterministic CRM views idempotently."""

    con.execute(_local_sql(con, SCHEMA_SQL))
    for column in (
        "input_fingerprint VARCHAR",
        "policy_code VARCHAR",
        "worker_run_id VARCHAR",
        "lease_token VARCHAR",
    ):
        con.execute(_local_sql(con, f"ALTER TABLE enrichment.enrichment_state ADD COLUMN IF NOT EXISTS {column}"))
    # Schema v6 makes contact_reviews the authority for privileged sales
    # decisions. Older databases could contain allowed/not_allowed values
    # written without an audit row. Preserve the latest genuine review and fail
    # closed to review when no decision can be proven.
    con.execute(
        _local_sql(
            con,
            """
            UPDATE enrichment.company_contact_points AS p
            SET sales_eligibility = coalesce(
                (
                    SELECT r.decision
                    FROM enrichment.contact_reviews AS r
                    WHERE r.contact_id = p.contact_id
                    ORDER BY r.reviewed_at DESC, r.review_id DESC
                    LIMIT 1
                ),
                'review'
            )
            WHERE p.sales_eligibility IN ('allowed', 'not_allowed')
               OR EXISTS (
                    SELECT 1
                    FROM enrichment.contact_reviews AS r
                    WHERE r.contact_id = p.contact_id
               )
            """,
        )
    )
    con.execute(
        _local_sql(
            con,
            """
        INSERT INTO enrichment.schema_meta(schema_name, schema_version, initialized_at)
        VALUES ('enrichment', ?, current_timestamp)
        ON CONFLICT (schema_name) DO UPDATE SET
            schema_version = excluded.schema_version,
            initialized_at = excluded.initialized_at
        """,
        ),
        [ENRICHMENT_SCHEMA_VERSION],
    )
    con.execute(_local_sql(con, _view_sql(company_relation)))


def _attach_canonical(con: Any, canonical_path: Path) -> None:
    canonical_path = Path(canonical_path).resolve()
    if not canonical_path.is_file():
        raise EnrichmentError(f"canonical DuckDBがありません: {canonical_path}")
    con.execute(f"ATTACH {_sql_string(canonical_path)} AS canonical (READ_ONLY)")


def initialize_database(
    database_path: Path = DEFAULT_DB,
    enrichment_path: Path = DEFAULT_ENRICHMENT_DB,
) -> dict[str, Any]:
    """Create a companion evidence DB without modifying the canonical DB."""

    database_path = Path(database_path).resolve()
    enrichment_path = Path(enrichment_path).resolve()
    if database_path == enrichment_path:
        raise EnrichmentError("canonical DBと拡張DBは別ファイルにしてください。")
    enrichment_path.parent.mkdir(parents=True, exist_ok=True)
    con, writer_lock = _open_writer(enrichment_path)
    attached = False
    try:
        _attach_canonical(con, database_path)
        attached = True
        initialize_enrichment_schema(con, company_relation="canonical.core.companies")
        con.execute("CHECKPOINT")
        return {
            "canonical_database": str(database_path),
            "enrichment_database": str(enrichment_path),
            "schema_version": ENRICHMENT_SCHEMA_VERSION,
            "schemas": ["enrichment", "compliance", "crm"],
        }
    finally:
        if attached:
            con.execute("DETACH canonical")
        con.close()
        writer_lock.release()


def sync_embedded_public_enrichment(
    database_path: Path = DEFAULT_DB,
    enrichment_path: Path = DEFAULT_ENRICHMENT_DB,
) -> dict[str, Any]:
    """Import corporate-number-linked public establishment contacts.

    MHLW care/disability rows describe establishments, not headquarters. They
    stay in a dedicated table with ``contact_scope='establishment'`` and are
    never promoted to the representative phone/email fields.
    """

    database_path = Path(database_path).resolve()
    enrichment_path = Path(enrichment_path).resolve()
    if database_path == enrichment_path:
        raise EnrichmentError("canonical DBと拡張DBは別ファイルにしてください。")
    enrichment_path.parent.mkdir(parents=True, exist_ok=True)
    con, writer_lock = _open_writer(enrichment_path)
    attached = False
    try:
        _attach_canonical(con, database_path)
        attached = True
        initialize_enrichment_schema(con, company_relation="canonical.core.companies")
        con.execute(
            """
            CREATE TEMP TABLE _embedded_public_rows(
                establishment_id VARCHAR,
                evidence_id VARCHAR,
                corporate_number VARCHAR,
                source_key VARCHAR,
                source_record_id VARCHAR,
                service_type VARCHAR,
                establishment_name VARCHAR,
                address VARCHAR,
                phone_raw VARCHAR,
                phone_normalized VARCHAR,
                fax_raw VARCHAR,
                url VARCHAR
            )
            """
        )
        sources = (
            {
                "schema": "mhlw",
                "table": "kaigo_establishment",
                "source_key": "mhlw.kaigo_establishment",
                "address": "NULLIF(trim(CAST(address AS VARCHAR)), '')",
            },
            {
                "schema": "mhlw",
                "table": "shougai_establishment",
                "source_key": "mhlw.shougai_establishment",
                "address": "NULLIF(trim(concat_ws('', CAST(address_city AS VARCHAR), CAST(address_detail AS VARCHAR))), '')",
            },
        )
        imported_sources: list[str] = []
        for source in sources:
            exists = con.execute(
                """
                SELECT count(*)
                FROM duckdb_tables()
                WHERE database_name = 'canonical' AND schema_name = ? AND table_name = ?
                """,
                [source["schema"], source["table"]],
            ).fetchone()[0]
            if not exists:
                continue
            relation = f"canonical.{_quote_identifier(source['schema'])}.{_quote_identifier(source['table'])}"
            source_key = _sql_string(source["source_key"])
            con.execute(
                f"""
                INSERT INTO _embedded_public_rows
                SELECT
                    md5({source_key} || '|' || trim(CAST(corporate_number AS VARCHAR)) || '|' ||
                        coalesce(CAST(establishment_number AS VARCHAR), '') || '|' ||
                        coalesce(CAST(service_type AS VARCHAR), '') || '|' ||
                        coalesce(CAST(phone AS VARCHAR), '') || '|' || coalesce(CAST(url AS VARCHAR), '')),
                    md5('evidence|' || {source_key} || '|' || trim(CAST(corporate_number AS VARCHAR)) || '|' ||
                        coalesce(CAST(establishment_number AS VARCHAR), '') || '|' ||
                        coalesce(CAST(service_type AS VARCHAR), '') || '|' ||
                        coalesce(CAST(phone AS VARCHAR), '') || '|' || coalesce(CAST(url AS VARCHAR), '')),
                    trim(CAST(corporate_number AS VARCHAR)),
                    {source_key},
                    coalesce(NULLIF(trim(CAST(establishment_number AS VARCHAR)), ''),
                             md5(coalesce(CAST(name AS VARCHAR), '') || '|' ||
                                 coalesce(CAST(phone AS VARCHAR), '') || '|' || coalesce(CAST(url AS VARCHAR), ''))),
                    NULLIF(trim(CAST(service_type AS VARCHAR)), ''),
                    NULLIF(trim(CAST(name AS VARCHAR)), ''),
                    {source['address']},
                    NULLIF(trim(CAST(phone AS VARCHAR)), ''),
                    NULLIF(regexp_replace(trim(CAST(phone AS VARCHAR)), '[^0-9+]', '', 'g'), ''),
                    NULLIF(trim(CAST(fax AS VARCHAR)), ''),
                    NULLIF(trim(CAST(url AS VARCHAR)), '')
                FROM {relation}
                WHERE regexp_full_match(trim(CAST(corporate_number AS VARCHAR)), '[0-9]{{13}}')
                  AND (NULLIF(trim(CAST(phone AS VARCHAR)), '') IS NOT NULL
                       OR NULLIF(trim(CAST(url AS VARCHAR)), '') IS NOT NULL)
                """
            )
            imported_sources.append(str(source["source_key"]))

        con.execute("BEGIN TRANSACTION")
        try:
            _execute_local(
                con,
                """
                INSERT INTO enrichment.evidence_documents(
                    evidence_id, corporate_number, source_key, source_url, retrieved_at,
                    extractor_version, policy_status, evidence_status, notes
                )
                SELECT evidence_id, corporate_number, source_key, NULL, current_timestamp,
                       'embedded-public-v1', 'not_checked', 'found',
                       'corporate-number-linked public establishment record'
                FROM _embedded_public_rows
                ON CONFLICT (evidence_id) DO UPDATE SET
                    retrieved_at = excluded.retrieved_at,
                    evidence_status = excluded.evidence_status,
                    notes = excluded.notes
                """,
            )
            _execute_local(
                con,
                """
                INSERT INTO enrichment.company_establishments(
                    establishment_id, corporate_number, source_key, source_record_id,
                    service_type, establishment_name, address, phone_raw, phone_normalized,
                    fax_raw, url, contact_scope, source_evidence_id, observed_at, status, confidence
                )
                SELECT establishment_id, corporate_number, source_key, source_record_id,
                       service_type, establishment_name, address, phone_raw, phone_normalized,
                       fax_raw, url, 'establishment', evidence_id, current_timestamp, 'found', 0.95
                FROM _embedded_public_rows
                ON CONFLICT (source_key, source_record_id, service_type, corporate_number) DO UPDATE SET
                    establishment_name = excluded.establishment_name,
                    address = excluded.address,
                    phone_raw = excluded.phone_raw,
                    phone_normalized = excluded.phone_normalized,
                    fax_raw = excluded.fax_raw,
                    url = excluded.url,
                    source_evidence_id = excluded.source_evidence_id,
                    observed_at = excluded.observed_at,
                    status = excluded.status,
                    confidence = excluded.confidence
                """,
            )
            for field_name, predicate in (
                ("establishment_phone", "phone_normalized IS NOT NULL"),
                ("establishment_url", "url IS NOT NULL"),
            ):
                _execute_local(
                    con,
                    f"""
                    INSERT INTO enrichment.enrichment_state(
                        corporate_number, field_name, source_key, state, attempt_count,
                        last_completed_at, last_evidence_id, updated_at
                    )
                    SELECT corporate_number, ?, source_key, 'found', 1,
                           current_timestamp, min(evidence_id), current_timestamp
                    FROM _embedded_public_rows
                    WHERE {predicate}
                    GROUP BY corporate_number, source_key
                    ON CONFLICT (corporate_number, field_name, source_key) DO UPDATE SET
                        state = 'found', last_completed_at = excluded.last_completed_at,
                        last_evidence_id = excluded.last_evidence_id, updated_at = excluded.updated_at
                    """,
                    [field_name],
                )
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        stats = _execute_local(
            con,
            """
            SELECT count(*),
                   count(DISTINCT corporate_number) FILTER (WHERE phone_normalized IS NOT NULL),
                   count(DISTINCT corporate_number) FILTER (WHERE url IS NOT NULL)
            FROM enrichment.company_establishments
            """,
        ).fetchone()
        con.execute("CHECKPOINT")
        return {
            "canonical_database": str(database_path),
            "enrichment_database": str(enrichment_path),
            "sources": imported_sources,
            "establishment_records": int(stats[0]),
            "companies_with_establishment_phone": int(stats[1]),
            "companies_with_establishment_url": int(stats[2]),
            "contact_scope": "establishment",
        }
    finally:
        if attached:
            con.execute("DETACH canonical")
        con.close()
        writer_lock.release()


def preserve_enrichment_layer(con: Any, previous_database_path: Path) -> dict[str, int]:
    """Copy evidence facts from a previous DB during an atomic canonical rebuild."""

    previous_database_path = Path(previous_database_path).resolve()
    if not previous_database_path.is_file():
        return {}
    attached = "previous_enrichment"
    con.execute(f"ATTACH {_sql_string(previous_database_path)} AS {attached} (READ_ONLY)")
    copied: dict[str, int] = {}
    tables = (
        ("enrichment", "evidence_documents"),
        ("enrichment", "company_websites"),
        ("enrichment", "company_contact_points"),
        ("enrichment", "company_locations"),
        ("enrichment", "company_establishments"),
        ("enrichment", "enrichment_state"),
        ("compliance", "suppressions"),
    )
    try:
        for schema, table in tables:
            target = f"{schema}.{table}"
            source = f"{attached}.{schema}.{table}"
            try:
                con.execute(f"INSERT INTO {target} SELECT * FROM {source}")
                copied[target] = int(con.execute(f"SELECT count(*) FROM {target}").fetchone()[0])
            except Exception as exc:
                if "does not exist" not in str(exc).lower() and "catalog" not in str(exc).lower():
                    raise EnrichmentError(f"既存の拡張層を移送できません: {source}") from exc
    finally:
        con.execute(f"DETACH {attached}")
    return copied


def _sql_string(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _local_catalog(con: Any) -> str:
    catalog = con.execute("SELECT current_catalog()").fetchone()[0]
    return _quote_identifier(str(catalog))


def _local_sql(con: Any, sql: str) -> str:
    """Qualify local schemas so a file named enrichment.duckdb is also safe."""

    catalog = _local_catalog(con)
    for schema in ("enrichment", "compliance", "crm"):
        sql = sql.replace(f"{schema}.", f"{catalog}.{schema}.")
    return sql


def _execute_local(con: Any, sql: str, *args: Any) -> Any:
    return con.execute(_local_sql(con, sql), *args)


def _seed_company_table(
    con: Any,
    limit: int | None,
    company_relation: str,
    *,
    industry_major: str | None = None,
) -> None:
    con.execute("DROP TABLE IF EXISTS _enrichment_seed_companies")
    limit_sql = "" if limit is None else f" LIMIT {int(limit)}"
    if industry_major:
        con.execute(
            f"""
            CREATE TEMP TABLE _enrichment_seed_companies AS
            SELECT DISTINCT c.corporate_number
            FROM {company_relation} c
            JOIN canonical.core.company_industries i USING (corporate_number)
            WHERE c.corporate_number IS NOT NULL
              AND upper(trim(i.jsic_major_code)) = ?
            {limit_sql}
            """,
            [industry_major],
        )
    else:
        con.execute(
            f"""
            CREATE TEMP TABLE _enrichment_seed_companies AS
            SELECT corporate_number
            FROM {company_relation}
            WHERE corporate_number IS NOT NULL
            {limit_sql}
            """
        )


def seed_enrichment(
    database_path: Path = DEFAULT_DB,
    *,
    enrichment_path: Path = DEFAULT_ENRICHMENT_DB,
    limit: int | None = None,
    source_key: str = "official_site",
    industry_major: str | None = None,
) -> dict[str, Any]:
    """Create resumable tasks for a company set and seed canonical URLs as evidence."""

    if limit is not None and limit < 1:
        raise EnrichmentError("limit は1以上で指定してください。")
    if not source_key.strip():
        raise EnrichmentError("source_key が空です。")
    if industry_major is not None:
        industry_major = industry_major.strip().upper()
        if not re.fullmatch(r"[A-T]", industry_major):
            raise EnrichmentError("industry_major はJSIC大分類の1文字（A〜T）で指定してください。")
    database_path = Path(database_path).resolve()
    enrichment_path = Path(enrichment_path).resolve()
    if database_path == enrichment_path:
        raise EnrichmentError("canonical DBと拡張DBは別ファイルにしてください。")
    enrichment_path.parent.mkdir(parents=True, exist_ok=True)
    con, writer_lock = _open_writer(enrichment_path)
    attached = False
    try:
        _attach_canonical(con, database_path)
        attached = True
        initialize_enrichment_schema(con, company_relation="canonical.core.companies")
        seed_source = "_enrichment_seed_companies"
        if limit is None and industry_major is None:
            # Stream the full canonical relation directly.  Materializing a
            # 5.8M-row company list before the five-way task expansion wastes
            # memory; bounded runs still use a temp subset for --limit.
            seed_source = "canonical.core.companies"
        else:
            _seed_company_table(
                con,
                limit,
                "canonical.core.companies",
                industry_major=industry_major,
            )
            seed_source = "_enrichment_seed_companies"
        con.execute("BEGIN TRANSACTION")
        con.execute(
            _local_sql(con, f"""
            INSERT INTO enrichment.enrichment_state(
                corporate_number, field_name, source_key, state, attempt_count, updated_at
            )
            SELECT
                s.corporate_number,
                f.field_name,
                ?,
                CASE
                    WHEN f.field_name = 'website_discovery'
                        AND nullif(trim(c.company_url), '') IS NOT NULL THEN 'found'
                    WHEN f.field_name = 'website_verification'
                        AND nullif(trim(c.company_url), '') IS NOT NULL THEN 'pending'
                    WHEN f.field_name IN ('website_verification', 'contact_extraction')
                        THEN 'waiting_for_dependency'
                    ELSE 'pending'
                END,
                0,
                current_timestamp
            FROM {seed_source} s
            JOIN canonical.core.companies c USING (corporate_number)
            CROSS JOIN (
                VALUES ('website_discovery'), ('website_verification'),
                       ('contact_extraction'), ('location')
            ) f(field_name)
            ON CONFLICT (corporate_number, field_name, source_key) DO NOTHING
            """),
            [source_key],
        )
        con.execute("DROP TABLE IF EXISTS _enrichment_seed_web")
        con.execute(
            f"""
            CREATE TEMP TABLE _enrichment_seed_web AS
            SELECT
                md5(s.corporate_number || '|' || lower(trim(c.company_url)) || '|gbizinfo.company_summary') AS evidence_id,
                md5(s.corporate_number || '|' || lower(trim(c.company_url))) AS website_id,
                s.corporate_number,
                trim(c.company_url) AS url,
                regexp_replace(lower(trim(c.company_url)), '/+$', '') AS normalized_url
            FROM {seed_source} s
            JOIN canonical.core.companies c USING (corporate_number)
            WHERE c.company_url IS NOT NULL
              AND trim(c.company_url) <> ''
            """
        )
        con.execute(
            _local_sql(con, """
            INSERT INTO enrichment.evidence_documents(
                evidence_id, corporate_number, source_key, source_url, retrieved_at,
                extractor_version, policy_status, evidence_status, notes
            )
            SELECT evidence_id, corporate_number, 'gbizinfo.company_summary', url, current_timestamp,
                   'seed-v1', 'not_checked', 'found', 'canonical company_url seed'
            FROM _enrichment_seed_web
            ON CONFLICT (evidence_id) DO UPDATE SET
                source_url = excluded.source_url,
                retrieved_at = excluded.retrieved_at,
                evidence_status = excluded.evidence_status,
                notes = excluded.notes
            """ )
        )
        con.execute(
            _local_sql(con, """
            INSERT INTO enrichment.company_websites(
                website_id, corporate_number, url, normalized_url, website_role,
                discovery_method, source_evidence_id, status, confidence,
                first_seen_at, checked_at
            )
            SELECT website_id, corporate_number, url, normalized_url, 'official_candidate',
                   'canonical_gbizinfo_url', evidence_id, 'found', 1.0,
                   current_timestamp, current_timestamp
            FROM _enrichment_seed_web
            ON CONFLICT (corporate_number, normalized_url, website_role) DO UPDATE SET
                url = excluded.url,
                source_evidence_id = excluded.source_evidence_id,
                status = excluded.status,
                confidence = excluded.confidence,
                checked_at = excluded.checked_at,
                last_error = NULL
            """ )
        )
        con.execute(
            _local_sql(con, """
            UPDATE enrichment.enrichment_state st
            SET state = 'found', last_evidence_id = w.evidence_id, last_completed_at = current_timestamp,
                updated_at = current_timestamp
            FROM _enrichment_seed_web w
            WHERE st.corporate_number = w.corporate_number
              AND st.field_name = 'website_discovery'
              AND st.source_key = ?
            """),
            [source_key],
        )
        con.execute("DROP TABLE IF EXISTS _enrichment_seed_loc")
        con.execute(
            f"""
            CREATE TEMP TABLE _enrichment_seed_loc AS
            SELECT
                md5(s.corporate_number || '|' || trim(c.full_address) || '|canonical_location') AS evidence_id,
                md5(s.corporate_number || '|' || trim(c.full_address)) AS location_id,
                s.corporate_number,
                trim(c.full_address) AS address_normalized,
                c.prefecture_name,
                c.city_name
            FROM {seed_source} s
            JOIN canonical.core.companies c USING (corporate_number)
            WHERE c.full_address IS NOT NULL
              AND trim(c.full_address) <> ''
            """
        )
        con.execute(
            _local_sql(con, """
            INSERT INTO enrichment.evidence_documents(
                evidence_id, corporate_number, source_key, source_url, retrieved_at,
                extractor_version, policy_status, evidence_status, notes
            )
            SELECT evidence_id, corporate_number, 'canonical.company_summary', NULL, current_timestamp,
                   'seed-v1', 'not_checked', 'found', 'canonical full_address seed'
            FROM _enrichment_seed_loc
            ON CONFLICT (evidence_id) DO UPDATE SET
                retrieved_at = excluded.retrieved_at,
                evidence_status = excluded.evidence_status,
                notes = excluded.notes
            """ )
        )
        con.execute(
            _local_sql(con, """
            INSERT INTO enrichment.company_locations(
                location_id, corporate_number, location_type, address_raw, address_normalized,
                prefecture_name, city_name, source_evidence_id, observed_at, status, confidence
            )
            SELECT location_id, corporate_number, 'head_office', address_normalized, address_normalized,
                   prefecture_name, city_name, evidence_id, current_timestamp, 'found', 1.0
            FROM _enrichment_seed_loc
            ON CONFLICT (corporate_number, location_type, address_normalized) DO UPDATE SET
                address_raw = excluded.address_raw,
                prefecture_name = excluded.prefecture_name,
                city_name = excluded.city_name,
                source_evidence_id = excluded.source_evidence_id,
                observed_at = excluded.observed_at,
                status = excluded.status,
                confidence = excluded.confidence
            """ )
        )
        con.execute(
            _local_sql(con, """
            UPDATE enrichment.enrichment_state st
            SET state = 'found', last_evidence_id = l.evidence_id, last_completed_at = current_timestamp,
                updated_at = current_timestamp
            FROM _enrichment_seed_loc l
            WHERE st.corporate_number = l.corporate_number
              AND st.field_name = 'location'
              AND st.source_key = ?
            """),
            [source_key],
        )
        con.execute("COMMIT")
        companies = int(con.execute(f"SELECT count(*) FROM {seed_source}").fetchone()[0])
        websites = int(con.execute("SELECT count(*) FROM _enrichment_seed_web").fetchone()[0])
        locations = int(con.execute("SELECT count(*) FROM _enrichment_seed_loc").fetchone()[0])
        states = int(
            con.execute(
                _local_sql(con, "SELECT count(*) FROM enrichment.enrichment_state WHERE source_key = ?"),
                [source_key],
            ).fetchone()[0]
        )
        return {
            "canonical_database": str(database_path),
            "enrichment_database": str(enrichment_path),
            "companies": companies,
            "canonical_websites": websites,
            "canonical_locations": locations,
            "states": states,
            "industry_major": industry_major,
        }
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        if attached:
            con.execute("DETACH canonical")
        con.close()
        writer_lock.release()


def _record_source(record: Mapping[str, Any]) -> tuple[str, str, str]:
    source_key = str(record.get("source_key") or record.get("source") or "official_site").strip()
    source_url = str(record.get("source_url") or record.get("url") or "").strip()
    retrieved_at = _as_timestamp(record.get("retrieved_at") or record.get("observed_at"))
    if not source_key:
        raise EnrichmentError("source_key が空です。")
    if not source_url and record.get("kind") != "state":
        raise EnrichmentError("証拠付きレコードにはsource_urlが必要です。")
    return source_key, source_url or None, retrieved_at


def _chunked(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _known_corporate_numbers(
    con: Any,
    records: Sequence[Mapping[str, Any]],
    company_relation: str,
) -> set[str]:
    numbers = sorted(
        {
            _require_corporate_number(record.get("corporate_number"))
            for record in records
            if str(record.get("kind") or "").strip() != "suppression"
            or record.get("corporate_number") not in (None, "")
        }
    )
    known: set[str] = set()
    for batch in _chunked(numbers, 500):
        placeholders = ", ".join("?" for _ in batch)
        known.update(
            str(row[0])
            for row in con.execute(
                f"SELECT corporate_number FROM {company_relation} WHERE corporate_number IN ({placeholders})",
                list(batch),
            ).fetchall()
        )
    missing = sorted(set(numbers) - known)
    if missing:
        raise EnrichmentError(f"core.companiesに存在しない法人番号です（先頭）: {missing[:5]}")
    return known


def _insert_evidence(
    con: Any,
    record: Mapping[str, Any],
    corporate_number: str | None,
) -> tuple[str, str | None, str]:
    source_key, source_url, retrieved_at = _record_source(record)
    stable_record = {
        str(key): value
        for key, value in record.items()
        if key not in {"retrieved_at", "observed_at", "published_at"}
    }
    stable_payload = json.dumps(stable_record, ensure_ascii=False, sort_keys=True, default=str)
    evidence_id = hashlib.sha256(
        f"evidence-v2|{corporate_number or ''}|{source_key}|{source_url or ''}|{stable_payload}".encode("utf-8")
    ).hexdigest()
    con.execute(
        _local_sql(
            con,
            """
        INSERT INTO enrichment.evidence_documents(
            evidence_id, corporate_number, source_key, source_url, retrieved_at,
            published_at, http_status, content_type, content_sha256, extractor_version,
            robots_status, policy_status, evidence_status, title, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (evidence_id) DO UPDATE SET
            corporate_number = excluded.corporate_number,
            source_url = excluded.source_url,
            retrieved_at = excluded.retrieved_at,
            published_at = excluded.published_at,
            http_status = excluded.http_status,
            content_type = excluded.content_type,
            content_sha256 = excluded.content_sha256,
            extractor_version = excluded.extractor_version,
            robots_status = excluded.robots_status,
            policy_status = excluded.policy_status,
            evidence_status = excluded.evidence_status,
            title = excluded.title,
            notes = excluded.notes
        """,
        ),
        [
            evidence_id,
            corporate_number,
            source_key,
            source_url,
            retrieved_at,
            record.get("published_at"),
            record.get("http_status"),
            record.get("content_type"),
            record.get("content_sha256"),
            record.get("extractor_version", "enrichment-v1"),
            record.get("robots_status", "not_checked"),
            record.get("policy_status", "not_checked"),
            record.get("evidence_status", "found"),
            record.get("title"),
            record.get("notes"),
        ],
    )
    return evidence_id, source_url, retrieved_at


def _state_for_record(record: Mapping[str, Any], default: str = "found") -> str:
    state = str(record.get("state") or record.get("status") or default).strip()
    if state not in ENRICHMENT_STATES:
        raise EnrichmentError(f"未知の状態です: {state}")
    return state


def _upsert_state(
    con: Any,
    corporate_number: str,
    field_name: str,
    source_key: str,
    state: str,
    evidence_id: str | None,
    error: str | None = None,
    *,
    input_fingerprint: str | None = None,
    policy_code: str | None = None,
    worker_run_id: str | None = None,
) -> None:
    leased = bool(
        _execute_local(
            con,
            """
            SELECT count(*) > 0
            FROM enrichment.enrichment_state
            WHERE corporate_number = ? AND field_name = ? AND source_key = ?
              AND state = 'leased'
            """,
            [corporate_number, field_name, source_key],
        ).fetchone()[0]
    )
    if leased:
        # A generic importer may add evidence while a worker is running, but
        # it must never steal or complete that worker's lease. Worker results
        # use the owner-checked CAS helper below.
        return
    con.execute(
        _local_sql(
            con,
            """
        INSERT INTO enrichment.enrichment_state(
            corporate_number, field_name, source_key, state, attempt_count,
            input_fingerprint, policy_code, worker_run_id,
            last_completed_at, last_error, last_evidence_id, updated_at
        ) VALUES (?, ?, ?, ?, 0, ?, ?, ?, current_timestamp, ?, ?, current_timestamp)
        ON CONFLICT (corporate_number, field_name, source_key) DO UPDATE SET
            state = excluded.state,
            input_fingerprint = coalesce(excluded.input_fingerprint, enrichment.enrichment_state.input_fingerprint),
            policy_code = coalesce(excluded.policy_code, enrichment.enrichment_state.policy_code),
            worker_run_id = coalesce(excluded.worker_run_id, enrichment.enrichment_state.worker_run_id),
            last_completed_at = excluded.last_completed_at,
            last_error = excluded.last_error,
            last_evidence_id = excluded.last_evidence_id,
            lease_owner = NULL,
            lease_token = NULL,
            lease_until = NULL,
            updated_at = excluded.updated_at
        """,
        ),
        [
            corporate_number,
            field_name,
            source_key,
            state,
            input_fingerprint,
            policy_code,
            worker_run_id,
            error,
            evidence_id,
        ],
    )


def _complete_leased_state(
    con: Any,
    corporate_number: str,
    field_name: str,
    source_key: str,
    state: str,
    lease_owner: str,
    lease_token: str,
    evidence_id: str | None,
    error: str | None,
    *,
    input_fingerprint: str | None = None,
    policy_code: str | None = None,
    worker_run_id: str | None = None,
) -> None:
    row = _execute_local(
        con,
        """
        UPDATE enrichment.enrichment_state
        SET state = ?, input_fingerprint = coalesce(?, input_fingerprint),
            policy_code = coalesce(?, policy_code),
            worker_run_id = coalesce(?, worker_run_id),
            last_completed_at = current_timestamp, last_error = ?,
            last_evidence_id = ?, lease_owner = NULL, lease_token = NULL,
            lease_until = NULL,
            updated_at = current_timestamp
        WHERE corporate_number = ? AND field_name = ? AND source_key = ?
          AND state = 'leased' AND lease_owner = ? AND lease_token = ?
        RETURNING corporate_number
        """,
        [
            state,
            input_fingerprint,
            policy_code,
            worker_run_id,
            error,
            evidence_id,
            corporate_number,
            field_name,
            source_key,
            lease_owner,
            lease_token,
        ],
    ).fetchone()
    if row is None:
        raise EnrichmentError(
            "タスクleaseの所有者が変わったため、古いworker結果を破棄しました。"
        )


def _ensure_pipeline_task(
    con: Any,
    corporate_number: str,
    field_name: str,
    state: str,
    evidence_id: str | None,
    *,
    input_fingerprint: str | None = None,
    source_key: str = "official_site",
) -> None:
    """Advance a staged website task without replaying completed work.

    A new input fingerprint requeues downstream work.  Re-importing the same
    candidate is idempotent, and an active lease is never stolen.
    """

    _execute_local(
        con,
        """
        INSERT INTO enrichment.enrichment_state(
            corporate_number, field_name, source_key, state, attempt_count,
            input_fingerprint, last_completed_at, last_evidence_id, updated_at
        ) VALUES (?, ?, ?, ?, 0, ?,
                  CASE WHEN ? IN ('found', 'verified') THEN current_timestamp ELSE NULL END,
                  ?, current_timestamp)
        ON CONFLICT (corporate_number, field_name, source_key) DO NOTHING
        """,
        [corporate_number, field_name, source_key, state, input_fingerprint, state, evidence_id],
    )
    _execute_local(
        con,
        """
        UPDATE enrichment.enrichment_state
        SET state = ?, input_fingerprint = ?, last_evidence_id = ?,
            last_error = NULL, next_attempt_at = NULL,
            last_completed_at = CASE
                WHEN ? IN ('found', 'verified') THEN current_timestamp
                ELSE last_completed_at
            END,
            updated_at = current_timestamp
        WHERE corporate_number = ? AND field_name = ? AND source_key = ?
          AND state <> 'leased'
          AND (
              state = 'waiting_for_dependency'
              OR input_fingerprint IS DISTINCT FROM ?
              OR (? IN ('found', 'verified') AND state <> ?)
          )
        """,
        [
            state,
            input_fingerprint,
            evidence_id,
            state,
            corporate_number,
            field_name,
            source_key,
            input_fingerprint,
            state,
            state,
        ],
    )


def _advance_website_pipeline(
    con: Any,
    corporate_number: str,
    *,
    role: str,
    status: str,
    normalized_url: str,
    evidence_id: str | None,
    pipeline_source_key: str = "official_site",
) -> None:
    fingerprint = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()
    if role == "official_candidate" and status in {"found", "needs_review", "verified"}:
        _ensure_pipeline_task(
            con,
            corporate_number,
            "website_discovery",
            "found",
            evidence_id,
            input_fingerprint=fingerprint,
            source_key=pipeline_source_key,
        )
        _ensure_pipeline_task(
            con,
            corporate_number,
            "website_verification",
            "pending",
            evidence_id,
            input_fingerprint=fingerprint,
            source_key=pipeline_source_key,
        )
    if role == "official_homepage" and status == "verified":
        _ensure_pipeline_task(
            con,
            corporate_number,
            "website_verification",
            "verified",
            evidence_id,
            input_fingerprint=fingerprint,
            source_key=pipeline_source_key,
        )
        _ensure_pipeline_task(
            con,
            corporate_number,
            "contact_extraction",
            "pending",
            evidence_id,
            input_fingerprint=fingerprint,
            source_key=pipeline_source_key,
        )


def import_enrichment_records(
    database_path: Path = DEFAULT_DB,
    records: Iterable[Mapping[str, Any]] = (),
    *,
    enrichment_path: Path = DEFAULT_ENRICHMENT_DB,
    batch_size: int = 1000,
    _allow_verified_websites: bool = False,
) -> dict[str, int]:
    """Import extractor output as latest facts while retaining evidence history.

    The generic/JSONL boundary cannot promote an official homepage or make a
    contact sales-ready.  Website verification is reserved for the explicit
    verifier and validated public-record bridge; contacts become ``allowed``
    only through :func:`review_contact` so every promotion has an audit row.
    """

    if batch_size < 1:
        raise EnrichmentError("batch_size は1以上で指定してください。")
    database_path = Path(database_path).resolve()
    enrichment_path = Path(enrichment_path).resolve()
    if database_path == enrichment_path:
        raise EnrichmentError("canonical DBと拡張DBは別ファイルにしてください。")
    enrichment_path.parent.mkdir(parents=True, exist_ok=True)
    con, writer_lock = _open_writer(enrichment_path)
    attached = False
    counts: dict[str, int] = {}
    buffered: list[Mapping[str, Any]] = []

    def process(batch: Sequence[Mapping[str, Any]]) -> None:
        if not batch:
            return
        _known_corporate_numbers(con, batch, "canonical.core.companies")
        con.execute("BEGIN TRANSACTION")
        try:
            for record in batch:
                kind = str(record.get("kind") or "").strip()
                corporate_number = (
                    _require_corporate_number(record.get("corporate_number"))
                    if record.get("corporate_number") not in (None, "")
                    else None
                )
                if kind != "suppression" and corporate_number is None:
                    raise EnrichmentError("法人番号が必要なレコードです。")
                if kind == "state":
                    source_key, _source_url, _retrieved_at = _record_source(record)
                    evidence_id = None
                    if record.get("source_url"):
                        evidence_id, _source_url, _retrieved_at = _insert_evidence(con, record, corporate_number)
                    field_name = str(record.get("field_name") or "").strip()
                    if not field_name:
                        raise EnrichmentError("stateレコードにはfield_nameが必要です。")
                    state = _state_for_record(record, default="needs_review")
                    lease_owner = str(record.get("lease_owner") or "").strip()
                    if lease_owner:
                        lease_token = str(record.get("lease_token") or "").strip()
                        if not lease_token:
                            raise EnrichmentError(
                                "worker完了stateにはclaimで返されたlease_tokenが必要です。"
                            )
                        _complete_leased_state(
                            con,
                            corporate_number,
                            field_name,
                            source_key,
                            state,
                            lease_owner,
                            lease_token,
                            evidence_id,
                            record.get("error"),
                            input_fingerprint=record.get("input_fingerprint"),
                            policy_code=record.get("policy_code"),
                            worker_run_id=record.get("worker_run_id"),
                        )
                    else:
                        _upsert_state(
                            con,
                            corporate_number,
                            field_name,
                            source_key,
                            state,
                            evidence_id,
                            record.get("error"),
                            input_fingerprint=record.get("input_fingerprint"),
                            policy_code=record.get("policy_code"),
                            worker_run_id=record.get("worker_run_id"),
                        )
                    counts["state"] = counts.get("state", 0) + 1
                    continue
                evidence_id, source_url, observed_at = _insert_evidence(con, record, corporate_number)
                if kind == "website":
                    url = normalize_url(record.get("url") or record.get("value"))
                    role = str(record.get("website_role") or "official_candidate")
                    if role not in WEBSITE_ROLES:
                        raise EnrichmentError(f"未知のwebsite_roleです: {role}")
                    status = _state_for_record(record)
                    if (
                        role == "official_homepage"
                        and status == "verified"
                        and not _allow_verified_websites
                    ):
                        raise EnrichmentError(
                            "generic importerからofficial_homepageをverifiedへ昇格できません。"
                            " verify-websiteまたは検証済みpublic bridgeを使用してください。"
                        )
                    _execute_local(
                        con,
                        """
                        INSERT INTO enrichment.company_websites(
                            website_id, corporate_number, url, normalized_url, website_role,
                            discovery_method, source_evidence_id, status, confidence,
                            robots_status, http_status, canonical_url, first_seen_at, checked_at, last_error
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (corporate_number, normalized_url, website_role) DO UPDATE SET
                            url = excluded.url, discovery_method = excluded.discovery_method,
                            source_evidence_id = excluded.source_evidence_id, status = excluded.status,
                            confidence = excluded.confidence, robots_status = excluded.robots_status,
                            http_status = excluded.http_status, canonical_url = excluded.canonical_url,
                            checked_at = excluded.checked_at, last_error = excluded.last_error
                        """,
                        [
                            _new_id(), corporate_number, url, url, role,
                            record.get("discovery_method", "extractor"), evidence_id, status,
                            record.get("confidence"), record.get("robots_status", "not_checked"),
                            record.get("http_status"), record.get("canonical_url"), observed_at,
                            observed_at, record.get("error"),
                        ],
                    )
                    source_key = str(record.get("source_key") or record.get("source") or "official_site")
                    _upsert_state(
                        con,
                        corporate_number,
                        "website",
                        source_key,
                        status,
                        evidence_id,
                        record.get("error"),
                        input_fingerprint=record.get("input_fingerprint"),
                        policy_code=record.get("policy_code"),
                        worker_run_id=record.get("worker_run_id"),
                    )
                    _advance_website_pipeline(
                        con,
                        corporate_number,
                        role=role,
                        status=status,
                        normalized_url=url,
                        evidence_id=evidence_id,
                        pipeline_source_key=str(record.get("pipeline_source_key") or "official_site"),
                    )
                elif kind == "contact":
                    contact_type = str(record.get("contact_type") or "").strip()
                    if contact_type not in CONTACT_TYPES:
                        raise EnrichmentError(f"未知のcontact_typeです: {contact_type}")
                    normalized = normalize_contact(contact_type, record.get("value"))
                    status = _state_for_record(record)
                    eligibility = str(record.get("sales_eligibility") or "review")
                    if eligibility not in SALES_ELIGIBILITY:
                        raise EnrichmentError(f"未知のsales_eligibilityです: {eligibility}")
                    if eligibility != "review":
                        raise EnrichmentError(
                            "generic importerからcontactの営業利用可否を確定できません。"
                            " review-contactで証拠と利用可否を確認してください。"
                        )
                    _execute_local(
                        con,
                        """
                        INSERT INTO enrichment.company_contact_points(
                            contact_id, corporate_number, contact_type, value_raw, value_normalized,
                            scope, publicness, source_evidence_id, source_url, observed_at, status,
                            confidence, verification_status, sales_eligibility, last_error
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (corporate_number, contact_type, value_normalized) DO UPDATE SET
                            value_raw = excluded.value_raw, scope = excluded.scope,
                            publicness = excluded.publicness, source_evidence_id = excluded.source_evidence_id,
                            source_url = excluded.source_url, observed_at = excluded.observed_at,
                            status = excluded.status, confidence = excluded.confidence,
                            verification_status = excluded.verification_status,
                            sales_eligibility = CASE
                                WHEN enrichment.company_contact_points.sales_eligibility
                                     IN ('allowed', 'not_allowed')
                                    THEN enrichment.company_contact_points.sales_eligibility
                                ELSE excluded.sales_eligibility
                            END,
                            last_error = excluded.last_error
                        """,
                        [
                            _new_id(), corporate_number, contact_type, record.get("value"), normalized,
                            record.get("scope", "company"), record.get("publicness", "unknown"), evidence_id,
                            source_url, observed_at, status, record.get("confidence"),
                            record.get("verification_status", "unverified"), eligibility, record.get("error"),
                        ],
                    )
                    field_name = contact_type
                    source_key = str(record.get("source_key") or record.get("source") or "official_site")
                    _upsert_state(
                        con,
                        corporate_number,
                        field_name,
                        source_key,
                        status,
                        evidence_id,
                        record.get("error"),
                        input_fingerprint=record.get("input_fingerprint"),
                        policy_code=record.get("policy_code"),
                        worker_run_id=record.get("worker_run_id"),
                    )
                elif kind == "location":
                    address = str(record.get("address_raw") or record.get("value") or "").strip()
                    if not address:
                        raise EnrichmentError("locationレコードにはaddress_rawまたはvalueが必要です。")
                    normalized_address = " ".join(address.split())
                    status = _state_for_record(record)
                    _execute_local(
                        con,
                        """
                        INSERT INTO enrichment.company_locations(
                            location_id, corporate_number, location_type, address_raw, address_normalized,
                            postal_code, prefecture_name, city_name, source_evidence_id, observed_at,
                            status, confidence
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (corporate_number, location_type, address_normalized) DO UPDATE SET
                            address_raw = excluded.address_raw, postal_code = excluded.postal_code,
                            prefecture_name = excluded.prefecture_name, city_name = excluded.city_name,
                            source_evidence_id = excluded.source_evidence_id, observed_at = excluded.observed_at,
                            status = excluded.status, confidence = excluded.confidence
                        """,
                        [
                            _new_id(), corporate_number, record.get("location_type", "head_office"),
                            address, normalized_address, record.get("postal_code"), record.get("prefecture_name"),
                            record.get("city_name"), evidence_id, observed_at, status, record.get("confidence"),
                        ],
                    )
                    source_key = str(record.get("source_key") or record.get("source") or "official_site")
                    _upsert_state(
                        con,
                        corporate_number,
                        "location",
                        source_key,
                        status,
                        evidence_id,
                        record.get("error"),
                        input_fingerprint=record.get("input_fingerprint"),
                        policy_code=record.get("policy_code"),
                        worker_run_id=record.get("worker_run_id"),
                    )
                elif kind == "suppression":
                    suppression_type = str(record.get("suppression_type") or "").strip()
                    normalized = normalize_suppression_value(suppression_type, record.get("value"))
                    _execute_local(
                        con,
                        """
                        INSERT INTO compliance.suppressions(
                            suppression_id, corporate_number, suppression_type, value_normalized,
                            value_sha256, reason, source, source_evidence_id, effective_from,
                            effective_to, created_at, notes
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (corporate_number, suppression_type, value_sha256) DO UPDATE SET
                            corporate_number = excluded.corporate_number, reason = excluded.reason,
                            source = excluded.source, source_evidence_id = excluded.source_evidence_id,
                            effective_from = excluded.effective_from, effective_to = excluded.effective_to,
                            notes = excluded.notes
                        """,
                        [
                            _new_id(), corporate_number if corporate_number else record.get("corporate_number"),
                            suppression_type, normalized, hash_normalized(normalized),
                            record.get("reason", "user_request"), record.get("source", "internal"), evidence_id,
                            _as_timestamp(record.get("effective_from")),
                            _as_timestamp(record.get("effective_to")) if record.get("effective_to") else None,
                            observed_at,
                            record.get("notes"),
                        ],
                    )
                else:
                    raise EnrichmentError(f"未知のenrichment record kindです: {kind}")
                counts[kind] = counts.get(kind, 0) + 1
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise

    try:
        _attach_canonical(con, database_path)
        attached = True
        initialize_enrichment_schema(con, company_relation="canonical.core.companies")
        for record in records:
            if not isinstance(record, Mapping):
                raise EnrichmentError("JSONLの各行はオブジェクトである必要があります。")
            buffered.append(record)
            if len(buffered) >= batch_size:
                process(buffered)
                buffered.clear()
        process(buffered)
        con.execute("CHECKPOINT")
        return counts
    finally:
        if attached:
            con.execute("DETACH canonical")
        con.close()
        writer_lock.release()


def import_enrichment_jsonl(
    database_path: Path,
    jsonl_path: Path,
    *,
    enrichment_path: Path = DEFAULT_ENRICHMENT_DB,
    batch_size: int = 1000,
) -> dict[str, int]:
    path = Path(jsonl_path)
    if not path.is_file():
        raise EnrichmentError(f"JSONLがありません: {path}")

    def records() -> Iterator[Mapping[str, Any]]:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise EnrichmentError(f"JSONL {line_number}行目が不正です: {exc}") from exc
                if not isinstance(value, Mapping):
                    raise EnrichmentError(f"JSONL {line_number}行目はオブジェクトではありません。")
                yield value

    return import_enrichment_records(
        database_path,
        records(),
        enrichment_path=enrichment_path,
        batch_size=batch_size,
    )


def claim_enrichment_tasks(
    database_path: Path = DEFAULT_DB,
    *,
    enrichment_path: Path = DEFAULT_ENRICHMENT_DB,
    worker_id: str,
    field_name: str | None = None,
    source_key: str | None = None,
    batch_size: int = 100,
    lease_seconds: int = 900,
    require_url: bool = False,
) -> list[dict[str, Any]]:
    if not worker_id.strip():
        raise EnrichmentError("worker_idが空です。")
    if not 1 <= batch_size <= 10_000:
        raise EnrichmentError("batch_sizeは1〜10000です。")
    if lease_seconds < 1:
        raise EnrichmentError("lease_secondsは1以上です。")
    con, writer_lock = _open_writer(Path(enrichment_path).resolve())
    now = datetime.now(timezone.utc)
    lease_until = now + timedelta(seconds=lease_seconds)
    attached = False
    try:
        _attach_canonical(con, Path(database_path).resolve())
        attached = True
        initialize_enrichment_schema(con, company_relation="canonical.core.companies")
        con.execute("BEGIN TRANSACTION")
        clauses = [
            "(st.state = 'pending' OR (st.state = 'leased' AND (st.lease_until IS NULL OR st.lease_until < ?)))",
            "(st.next_attempt_at IS NULL OR st.next_attempt_at <= ?)",
        ]
        params: list[Any] = [now, now]
        if field_name:
            clauses.append("st.field_name = ?")
            params.append(field_name)
        if source_key:
            clauses.append("st.source_key = ?")
            params.append(source_key)
        if require_url:
            clauses.append(
                "EXISTS ("
                "SELECT 1 FROM canonical.core.companies c "
                "WHERE c.corporate_number = st.corporate_number "
                "AND nullif(trim(c.company_url), '') IS NOT NULL"
                ")"
            )
        rows = _execute_local(
            con,
            f"""
            SELECT st.corporate_number, st.field_name, st.source_key
            FROM enrichment.enrichment_state st
            WHERE {' AND '.join(clauses)}
            ORDER BY st.updated_at, st.corporate_number
            LIMIT ?
            """,
            [*params, batch_size],
        ).fetchall()
        claimed_rows: list[tuple[Any, Any, Any, str]] = []
        for corporate_number, task_field, task_source in rows:
            lease_token = _new_id()
            _execute_local(
                con,
                """
                UPDATE enrichment.enrichment_state
                SET state = 'leased', attempt_count = attempt_count + 1,
                    lease_owner = ?, lease_token = ?, lease_until = ?,
                    last_started_at = ?, updated_at = ?
                WHERE corporate_number = ? AND field_name = ? AND source_key = ?
                """,
                [
                    worker_id,
                    lease_token,
                    lease_until,
                    now,
                    now,
                    corporate_number,
                    task_field,
                    task_source,
                ],
            )
            claimed_rows.append(
                (corporate_number, task_field, task_source, lease_token)
            )
        con.execute("COMMIT")
        return [
            {
                "corporate_number": row[0],
                "field_name": row[1],
                "source_key": row[2],
                "lease_owner": worker_id,
                "lease_token": row[3],
                "lease_until": lease_until.isoformat(),
            }
            for row in claimed_rows
        ]
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        if attached:
            con.execute("DETACH canonical")
        con.close()
        writer_lock.release()


def complete_enrichment_task(
    database_path: Path = DEFAULT_DB,
    *,
    enrichment_path: Path = DEFAULT_ENRICHMENT_DB,
    corporate_number: str,
    field_name: str,
    source_key: str,
    state: str,
    worker_id: str | None = None,
    lease_token: str | None = None,
    evidence_id: str | None = None,
    error: str | None = None,
) -> None:
    corporate_number = _require_corporate_number(corporate_number)
    if state not in ENRICHMENT_STATES - {"pending", "leased"}:
        raise EnrichmentError(f"完了処理に使えない状態です: {state}")
    worker_id = str(worker_id or "").strip()
    lease_token = str(lease_token or "").strip()
    if not worker_id or not lease_token:
        raise EnrichmentError("完了にはworker_idとclaimで返されたlease_tokenが必要です。")
    con, writer_lock = _open_writer(Path(enrichment_path).resolve())
    attached = False
    try:
        _attach_canonical(con, Path(database_path).resolve())
        attached = True
        initialize_enrichment_schema(con, company_relation="canonical.core.companies")
        _complete_leased_state(
            con,
            corporate_number,
            field_name,
            source_key,
            state,
            worker_id,
            lease_token,
            evidence_id,
            error,
        )
        con.execute("CHECKPOINT")
    finally:
        if attached:
            con.execute("DETACH canonical")
        con.close()
        writer_lock.release()


def review_contact(
    database_path: Path = DEFAULT_DB,
    *,
    enrichment_path: Path = DEFAULT_ENRICHMENT_DB,
    corporate_number: str,
    contact_type: str,
    value: str,
    decision: str,
    reviewer: str,
    reason: str,
) -> dict[str, str]:
    """Record an explicit review and change sales eligibility atomically."""

    corporate_number = _require_corporate_number(corporate_number)
    if contact_type not in CONTACT_TYPES:
        raise EnrichmentError(f"未知のcontact_typeです: {contact_type}")
    if decision not in {"allowed", "not_allowed"}:
        raise EnrichmentError("decisionはallowedまたはnot_allowedです。")
    reviewer = reviewer.strip()
    reason = reason.strip()
    if not reviewer or len(reviewer) > 128:
        raise EnrichmentError("reviewerは1〜128文字で指定してください。")
    if not reason or len(reason) > 2_000:
        raise EnrichmentError("reasonは1〜2000文字で指定してください。")
    normalized = normalize_contact(contact_type, value)
    con, writer_lock = _open_writer(Path(enrichment_path).resolve())
    attached = False
    try:
        _attach_canonical(con, Path(database_path).resolve())
        attached = True
        initialize_enrichment_schema(con, company_relation="canonical.core.companies")
        con.execute("BEGIN TRANSACTION")
        row = _execute_local(
            con,
            """
            SELECT contact_id, sales_eligibility
            FROM enrichment.company_contact_points
            WHERE corporate_number = ? AND contact_type = ? AND value_normalized = ?
              AND status IN ('found', 'verified')
              AND source_evidence_id IS NOT NULL
              AND nullif(trim(source_url), '') IS NOT NULL
            """,
            [corporate_number, contact_type, normalized],
        ).fetchone()
        if row is None:
            raise EnrichmentError("証拠付きのreview対象contactがありません。")
        contact_id, previous = str(row[0]), str(row[1])
        review_id = _new_id()
        _execute_local(
            con,
            """
            INSERT INTO enrichment.contact_reviews(
                review_id, contact_id, corporate_number, contact_type,
                value_normalized, previous_sales_eligibility, decision,
                reviewer, reason, reviewed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, current_timestamp)
            """,
            [
                review_id,
                contact_id,
                corporate_number,
                contact_type,
                normalized,
                previous,
                decision,
                reviewer,
                reason,
            ],
        )
        _execute_local(
            con,
            """
            UPDATE enrichment.company_contact_points
            SET sales_eligibility = ?
            WHERE contact_id = ?
            """,
            [decision, contact_id],
        )
        con.execute("COMMIT")
        con.execute("CHECKPOINT")
        return {
            "review_id": review_id,
            "contact_id": contact_id,
            "corporate_number": corporate_number,
            "contact_type": contact_type,
            "value_normalized": normalized,
            "previous_sales_eligibility": previous,
            "decision": decision,
            "reviewer": reviewer,
        }
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        if attached:
            con.execute("DETACH canonical")
        con.close()
        writer_lock.release()


def export_sales_ready_accounts(
    database_path: Path = DEFAULT_DB,
    *,
    enrichment_path: Path = DEFAULT_ENRICHMENT_DB,
    max_rows: int | None = 100_000,
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """Read the policy-filtered CRM view without mutating either database."""

    if max_rows is not None and max_rows < 1:
        raise EnrichmentError("max_rows は1以上で指定してください。")
    enrichment_path = Path(enrichment_path).resolve()
    if not enrichment_path.is_file():
        raise EnrichmentError(f"拡張DBがありません: {enrichment_path}")
    con = _duckdb().connect(str(enrichment_path), read_only=True)
    attached = False
    try:
        _attach_canonical(con, Path(database_path).resolve())
        attached = True
        limit_sql = "" if max_rows is None else " LIMIT ?"
        result = _execute_local(
            con,
            "SELECT * FROM crm.v_sales_ready_accounts ORDER BY corporate_number" + limit_sql,
            [] if max_rows is None else [max_rows],
        )
        columns = [str(item[0]) for item in result.description]
        return columns, [tuple(row) for row in result.fetchall()]
    except Exception as exc:
        if isinstance(exc, EnrichmentError):
            raise
        raise EnrichmentError(
            "営業利用可否ビューを読めません。先に init-enrichment を実行してください。"
        ) from exc
    finally:
        if attached:
            con.execute("DETACH canonical")
        con.close()


def export_establishment_contacts(
    database_path: Path = DEFAULT_DB,
    *,
    enrichment_path: Path = DEFAULT_ENRICHMENT_DB,
    prefecture: str | None = None,
    service_type: str | None = None,
    max_rows: int | None = 100_000,
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """Return public establishment contacts without calling them HQ contacts."""

    if max_rows is not None and max_rows < 1:
        raise EnrichmentError("max_rows は1以上で指定してください。")
    enrichment_path = Path(enrichment_path).resolve()
    if not enrichment_path.is_file():
        raise EnrichmentError(f"拡張DBがありません: {enrichment_path}")
    con = _duckdb().connect(str(enrichment_path), read_only=True)
    attached = False
    try:
        _attach_canonical(con, Path(database_path).resolve())
        attached = True
        clauses = ["e.status IN ('found', 'verified')"]
        params: list[Any] = []
        if prefecture:
            clauses.append("c.prefecture_name = ?")
            params.append(prefecture)
        if service_type:
            clauses.append("e.service_type = ?")
            params.append(service_type)
        limit_sql = "" if max_rows is None else " LIMIT ?"
        if max_rows is not None:
            params.append(max_rows)
        result = _execute_local(
            con,
            """
            SELECT
                c.corporate_number,
                c.company_name,
                c.prefecture_name,
                c.city_name,
                e.source_record_id AS establishment_number,
                e.establishment_name,
                e.service_type,
                e.address AS establishment_address,
                e.phone_normalized AS establishment_phone,
                e.url AS establishment_url,
                e.source_key,
                e.contact_scope,
                e.confidence,
                e.observed_at
            FROM enrichment.company_establishments e
            JOIN canonical.core.companies c USING (corporate_number)
            WHERE """
            + " AND ".join(clauses)
            + " ORDER BY c.corporate_number, e.source_key, e.source_record_id"
            + limit_sql,
            params,
        )
        columns = [str(item[0]) for item in result.description]
        return columns, [tuple(row) for row in result.fetchall()]
    finally:
        if attached:
            con.execute("DETACH canonical")
        con.close()


def iter_jsonl_records(path: Path) -> Iterator[Mapping[str, Any]]:
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                value = json.loads(line)
                if isinstance(value, Mapping):
                    yield value


__all__ = [
    "CONTACT_TYPES",
    "DEFAULT_DB",
    "DEFAULT_ENRICHMENT_DB",
    "ENRICHMENT_SCHEMA_VERSION",
    "ENRICHMENT_STATES",
    "ENRICHMENT_TASK_FIELDS",
    "EnrichmentError",
    "SALES_ELIGIBILITY",
    "claim_enrichment_tasks",
    "complete_enrichment_task",
    "export_sales_ready_accounts",
    "export_establishment_contacts",
    "hash_normalized",
    "import_enrichment_jsonl",
    "import_enrichment_records",
    "initialize_database",
    "initialize_enrichment_schema",
    "normalize_contact",
    "review_contact",
    "normalize_email",
    "normalize_phone",
    "normalize_suppression_value",
    "normalize_url",
    "preserve_enrichment_layer",
    "seed_enrichment",
    "sync_embedded_public_enrichment",
]
