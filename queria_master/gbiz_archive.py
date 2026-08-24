"""Safe, opt-in reader for historical gBizINFO ``Hojinjoho`` archives.

This module has no connection to the canonical refresh pipeline.  Its Python
API offers two explicit operations, and the opt-in CLI exposes the staging
import:

* :func:`iter_normalized_batches` reads and validates an archive without
  writing anything; and
* :func:`import_archive_to_staging` creates a *new* caller-named DuckDB file.

The staging database retains canonicalized record JSON together with exact
archive/member hashes and normalized-record hashes.  Promotion into the
canonical database is intentionally a separate, reviewable operation.
"""

from __future__ import annotations

import codecs
import hashlib
import json
import math
import os
import re
import stat
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterator, Mapping, Sequence
from uuid import uuid4


IMPORTER_VERSION = "1"
_CORPORATE_NUMBER = re.compile(r"[0-9]{13}")
_ACTIVITY_LIST_FIELDS = (
    "subsidy",
    "procurement",
    "patent",
    "certification",
    "commendation",
    "corporation-info",
)


class GBizArchiveError(RuntimeError):
    """Base error for the historical archive importer."""


class ArchiveValidationError(GBizArchiveError):
    """The ZIP container or one of its JSON records is unsafe or invalid."""


class StagingDatabaseError(GBizArchiveError):
    """The requested output is not a safe, new staging database."""


@dataclass(frozen=True)
class ArchiveLimits:
    """Hard limits applied before and while an archive is decompressed."""

    max_archive_bytes: int = 1024**3
    max_members: int = 256
    max_json_members: int = 128
    max_member_uncompressed_bytes: int = 512 * 1024**2
    max_total_uncompressed_bytes: int = 8 * 1024**3
    max_compression_ratio: float = 100.0
    max_json_value_chars: int = 4 * 1024**2
    max_activities_per_record: int = 100_000
    max_normalized_record_bytes: int = 16 * 1024**2
    max_batch_normalized_bytes: int = 64 * 1024**2
    read_chunk_bytes: int = 1024**2

    def validate(self) -> None:
        integer_limits = {
            "max_archive_bytes": self.max_archive_bytes,
            "max_members": self.max_members,
            "max_json_members": self.max_json_members,
            "max_member_uncompressed_bytes": self.max_member_uncompressed_bytes,
            "max_total_uncompressed_bytes": self.max_total_uncompressed_bytes,
            "max_json_value_chars": self.max_json_value_chars,
            "max_activities_per_record": self.max_activities_per_record,
            "max_normalized_record_bytes": self.max_normalized_record_bytes,
            "max_batch_normalized_bytes": self.max_batch_normalized_bytes,
            "read_chunk_bytes": self.read_chunk_bytes,
        }
        invalid = [name for name, value in integer_limits.items() if value < 1]
        if invalid or self.max_compression_ratio < 1:
            names = invalid + (["max_compression_ratio"] if self.max_compression_ratio < 1 else [])
            raise ValueError("archive limits must be positive: " + ", ".join(names))
        if not 64 * 1024 <= self.read_chunk_bytes <= 8 * 1024**2:
            raise ValueError("read_chunk_bytes must be between 64 KiB and 8 MiB")
        if self.max_json_value_chars > 64 * 1024**2:
            raise ValueError("max_json_value_chars must not exceed 64 Mi characters")
        if self.max_json_value_chars > self.read_chunk_bytes * 64:
            raise ValueError(
                "max_json_value_chars must not exceed 64 read chunks"
            )
        if self.max_batch_normalized_bytes < self.max_normalized_record_bytes:
            raise ValueError(
                "max_batch_normalized_bytes must be at least max_normalized_record_bytes"
            )


@dataclass(frozen=True)
class NormalizedActivity:
    activity_type: str
    activity_index: int
    source_key: str | None
    normalized_json: str
    normalized_sha256: str
    normalized_bytes: int


@dataclass(frozen=True)
class NormalizedCompany:
    source_archive_name: str
    source_archive_sha256: str
    source_member: str
    source_record_index: int
    corporate_number: str
    name: str | None
    kana: str | None
    name_en: str | None
    postal_code: str | None
    location: str | None
    kind: str | None
    process: str | None
    status: str | None
    representative_name: str | None
    capital_stock: str | None
    employee_number: str | None
    business_summary: str | None
    company_url: str | None
    founding_year: str | None
    update_date: str | None
    industry_codes: tuple[str, ...]
    is_infocom: bool
    normalized_json: str
    normalized_record_sha256: str
    normalized_bytes: int
    activities: tuple[NormalizedActivity, ...]


@dataclass(frozen=True)
class ImportResult:
    staging_database: Path
    import_id: str
    source_sha256: str
    source_records: int
    imported_records: int
    activity_records: int
    json_member_count: int
    json_uncompressed_bytes: int


@dataclass(frozen=True)
class _MemberSummary:
    member_name: str
    member_index: int
    compressed_bytes: int
    uncompressed_bytes: int
    member_sha256: str
    source_records: int
    imported_records: int


@dataclass(frozen=True)
class _MemberComplete:
    summary: _MemberSummary


def _duckdb_module():
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - DuckDB is a project dependency.
        raise StagingDatabaseError("duckdb is required for staging import") from exc
    return duckdb


def _sha256_stream(handle: BinaryIO, chunk_size: int, max_bytes: int) -> str:
    digest = hashlib.sha256()
    total = 0
    while chunk := handle.read(chunk_size):
        total += len(chunk)
        if total > max_bytes:
            raise ArchiveValidationError(f"archive grew beyond {max_bytes} bytes while hashing")
        digest.update(chunk)
    return digest.hexdigest()


def _source_fingerprint(handle: BinaryIO) -> tuple[int, int, int, int, int]:
    info = os.fstat(handle.fileno())
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _safe_member_name(info: zipfile.ZipInfo) -> str:
    name = info.filename
    if not name or "\x00" in name or "\\" in name:
        raise ArchiveValidationError(f"unsafe ZIP member path: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ArchiveValidationError(f"unsafe ZIP member path: {name!r}")
    if path.parts and ":" in path.parts[0]:
        raise ArchiveValidationError(f"unsafe ZIP member path: {name!r}")
    mode = (info.external_attr >> 16) & 0xFFFF
    if stat.S_IFMT(mode) == stat.S_IFLNK:
        raise ArchiveValidationError(f"symbolic links are not allowed in ZIP: {name!r}")
    return path.as_posix()


def _preflight_archive(
    archive: zipfile.ZipFile,
    limits: ArchiveLimits,
) -> list[tuple[zipfile.ZipInfo, str]]:
    infos = archive.infolist()
    if len(infos) > limits.max_members:
        raise ArchiveValidationError(
            f"ZIP has too many members: {len(infos)} > {limits.max_members}"
        )

    total = 0
    json_members: list[tuple[zipfile.ZipInfo, str]] = []
    seen: set[str] = set()
    allowed_compression = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
    for info in infos:
        safe_name = _safe_member_name(info)
        folded = safe_name.casefold().rstrip("/")
        if folded in seen:
            raise ArchiveValidationError(f"duplicate ZIP member path: {safe_name!r}")
        seen.add(folded)
        if info.flag_bits & 0x1:
            raise ArchiveValidationError(f"encrypted ZIP member is not allowed: {safe_name!r}")
        if not info.is_dir() and info.compress_type not in allowed_compression:
            raise ArchiveValidationError(f"unsupported ZIP compression: {safe_name!r}")
        if info.file_size < 0 or info.compress_size < 0:
            raise ArchiveValidationError(f"invalid ZIP member size: {safe_name!r}")
        if info.file_size > limits.max_member_uncompressed_bytes:
            raise ArchiveValidationError(
                f"ZIP member is too large: {safe_name!r} ({info.file_size} bytes)"
            )
        total += info.file_size
        if total > limits.max_total_uncompressed_bytes:
            raise ArchiveValidationError("ZIP total uncompressed size exceeds the configured limit")
        if info.file_size:
            if info.compress_size == 0:
                raise ArchiveValidationError(f"invalid ZIP compression size: {safe_name!r}")
            ratio = info.file_size / info.compress_size
            if ratio > limits.max_compression_ratio:
                raise ArchiveValidationError(
                    f"ZIP compression ratio is too high: {safe_name!r} ({ratio:.1f})"
                )
        if not info.is_dir() and safe_name.lower().endswith(".json"):
            json_members.append((info, safe_name))

    if not json_members:
        raise ArchiveValidationError("ZIP does not contain a JSON member")
    if len(json_members) > limits.max_json_members:
        raise ArchiveValidationError(
            f"ZIP has too many JSON members: {len(json_members)} > {limits.max_json_members}"
        )
    json_members.sort(key=lambda item: item[1])
    return json_members


class _MemberReader:
    def __init__(
        self,
        stream: BinaryIO,
        *,
        member_name: str,
        limits: ArchiveLimits,
        total_counter: list[int],
    ) -> None:
        self.stream = stream
        self.member_name = member_name
        self.limits = limits
        self.total_counter = total_counter
        self.bytes_read = 0
        self.digest = hashlib.sha256()

    def read(self) -> bytes:
        chunk = self.stream.read(self.limits.read_chunk_bytes)
        if not chunk:
            return b""
        self.bytes_read += len(chunk)
        self.total_counter[0] += len(chunk)
        if self.bytes_read > self.limits.max_member_uncompressed_bytes:
            raise ArchiveValidationError(
                f"ZIP member expanded beyond its limit: {self.member_name!r}"
            )
        if self.total_counter[0] > self.limits.max_total_uncompressed_bytes:
            raise ArchiveValidationError("ZIP expanded beyond the total uncompressed limit")
        self.digest.update(chunk)
        return chunk


def _iter_json_array(
    reader: _MemberReader,
    *,
    member_name: str,
    max_value_chars: int,
) -> Iterator[Mapping[str, Any]]:
    utf8 = codecs.getincrementaldecoder("utf-8-sig")("strict")

    def reject_constant(value: str) -> Any:
        raise ArchiveValidationError(f"non-standard JSON number {value!r}: {member_name!r}")

    def parse_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ArchiveValidationError(f"non-finite JSON number: {member_name!r}")
        return parsed

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ArchiveValidationError(
                    f"duplicate JSON object key {key!r}: {member_name!r}"
                )
            value[key] = item
        return value

    decoder = json.JSONDecoder(
        object_pairs_hook=object_pairs,
        parse_constant=reject_constant,
        parse_float=parse_float,
    )
    buffer = ""
    position = 0
    eof = False

    def fill(*, compact: bool) -> None:
        nonlocal buffer, position, eof
        if compact and position:
            buffer = buffer[position:]
            position = 0
        chunk = reader.read()
        if chunk:
            try:
                buffer += utf8.decode(chunk, final=False)
            except UnicodeDecodeError as exc:
                raise ArchiveValidationError(
                    f"JSON member is not valid UTF-8: {member_name!r}"
                ) from exc
        elif not eof:
            eof = True
            try:
                buffer += utf8.decode(b"", final=True)
            except UnicodeDecodeError as exc:
                raise ArchiveValidationError(
                    f"JSON member is not valid UTF-8: {member_name!r}"
                ) from exc

    def skip_space() -> None:
        nonlocal position
        while position < len(buffer) and buffer[position].isspace():
            position += 1

    while not buffer and not eof:
        fill(compact=False)
    skip_space()
    while position >= len(buffer) and not eof:
        fill(compact=True)
        skip_space()
    if position >= len(buffer) or buffer[position] != "[":
        raise ArchiveValidationError(f"JSON member must be a top-level array: {member_name!r}")
    position += 1
    expect_value = True
    record_count = 0

    while True:
        skip_space()
        while position >= len(buffer) and not eof:
            fill(compact=True)
            skip_space()
        if position >= len(buffer):
            raise ArchiveValidationError(f"unterminated JSON array: {member_name!r}")

        if expect_value:
            if buffer[position] == "]":
                if record_count:
                    raise ArchiveValidationError(f"trailing comma in JSON array: {member_name!r}")
                position += 1
                break
            if position:
                buffer = buffer[position:]
                position = 0
            while True:
                try:
                    value, end = decoder.raw_decode(buffer, position)
                    break
                except json.JSONDecodeError as exc:
                    if eof:
                        raise ArchiveValidationError(
                            f"invalid JSON in {member_name!r}: {exc.msg} at char {exc.pos}"
                        ) from exc
                    if len(buffer) > max_value_chars:
                        raise ArchiveValidationError(
                            f"JSON value exceeds the configured limit: {member_name!r}"
                        )
                    fill(compact=False)
                except (ValueError, RecursionError) as exc:
                    raise ArchiveValidationError(
                        f"invalid JSON value in {member_name!r}: {exc}"
                    ) from exc
            if end > max_value_chars:
                raise ArchiveValidationError(
                    f"JSON value exceeds the configured limit: {member_name!r}"
                )
            if not isinstance(value, dict):
                raise ArchiveValidationError(
                    f"JSON array item {record_count + 1} is not an object: {member_name!r}"
                )
            position = end
            record_count += 1
            yield value
            expect_value = False
            continue

        if buffer[position] == ",":
            position += 1
            expect_value = True
            continue
        if buffer[position] == "]":
            position += 1
            break
        raise ArchiveValidationError(
            f"expected ',' or ']' after JSON item {record_count}: {member_name!r}"
        )

    while True:
        skip_space()
        if position < len(buffer):
            raise ArchiveValidationError(f"trailing data after JSON array: {member_name!r}")
        if eof:
            break
        fill(compact=True)


def _optional_text(record: Mapping[str, Any], key: str) -> str | None:
    value = record.get(key)
    if value is None:
        return None
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return str(value)
    raise ArchiveValidationError(f"field {key!r} must be a scalar or null")


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:  # Defensive: values came from json.JSONDecoder.
        raise ArchiveValidationError("record cannot be serialized as JSON") from exc


def _source_key(value: Mapping[str, Any]) -> str | None:
    metadata = value.get("meta-data")
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        raise ArchiveValidationError("activity 'meta-data' must be an object or null")
    key = metadata.get("key_field")
    if key is None:
        return None
    if not isinstance(key, (str, int, float)) or isinstance(key, bool):
        raise ArchiveValidationError("activity source key must be a scalar or null")
    return str(key)


def _activity(
    activity_type: str,
    index: int,
    value: Mapping[str, Any],
    *,
    source_key: str | None = None,
) -> NormalizedActivity:
    payload = _canonical_json(value)
    payload_bytes = payload.encode("utf-8")
    return NormalizedActivity(
        activity_type=activity_type,
        activity_index=index,
        source_key=source_key if source_key is not None else _source_key(value),
        normalized_json=payload,
        normalized_sha256=hashlib.sha256(payload_bytes).hexdigest(),
        normalized_bytes=len(payload_bytes),
    )


def _normalize_activities(
    record: Mapping[str, Any],
    *,
    max_activities: int,
) -> tuple[NormalizedActivity, ...]:
    activities: list[NormalizedActivity] = []

    def append(activity: NormalizedActivity) -> None:
        if len(activities) >= max_activities:
            raise ArchiveValidationError(
                f"record has more than {max_activities} normalized activities"
            )
        activities.append(activity)

    for field in _ACTIVITY_LIST_FIELDS:
        values = record.get(field)
        if values is None:
            continue
        if not isinstance(values, list):
            raise ArchiveValidationError(f"field {field!r} must be an array or null")
        for index, value in enumerate(values):
            if not isinstance(value, dict):
                raise ArchiveValidationError(f"field {field!r} contains a non-object item")
            append(_activity(field, index, value))

    workplace = record.get("workplace_info")
    if workplace is not None:
        if not isinstance(workplace, dict):
            raise ArchiveValidationError("field 'workplace_info' must be an object or null")
        append(_activity("workplace_info", 0, workplace))

    finance = record.get("finance")
    if finance is not None:
        if not isinstance(finance, dict):
            raise ArchiveValidationError("field 'finance' must be an object or null")
        management = finance.get("management_index")
        if management is not None:
            if not isinstance(management, list):
                raise ArchiveValidationError("field 'finance.management_index' must be an array")
            finance_context = {
                key: value for key, value in finance.items() if key != "management_index"
            }
            finance_key = _source_key(finance)
            if finance_context:
                append(_activity("finance.context", 0, finance_context, source_key=finance_key))
            for index, value in enumerate(management):
                if not isinstance(value, dict):
                    raise ArchiveValidationError(
                        "field 'finance.management_index' contains a non-object item"
                    )
                append(
                    _activity(
                        "finance.management_index",
                        index,
                        value,
                        source_key=finance_key,
                    )
                )
    return tuple(activities)


def _normalize_company(
    record: Mapping[str, Any],
    *,
    archive_name: str,
    archive_sha256: str,
    member_name: str,
    record_index: int,
    max_activities: int,
    max_normalized_bytes: int,
) -> NormalizedCompany:
    corporate_number_value = record.get("corporate_number")
    if isinstance(corporate_number_value, bool) or not isinstance(
        corporate_number_value, (str, int)
    ):
        raise ArchiveValidationError("field 'corporate_number' must be a 13-digit string")
    corporate_number = str(corporate_number_value).strip()
    if not _CORPORATE_NUMBER.fullmatch(corporate_number):
        raise ArchiveValidationError(
            f"field 'corporate_number' is not 13 digits: {corporate_number!r}"
        )

    industry = record.get("industry")
    if industry is None:
        industry_codes: tuple[str, ...] = ()
    elif isinstance(industry, list) and all(isinstance(value, str) for value in industry):
        industry_codes = tuple(industry)
    else:
        raise ArchiveValidationError("field 'industry' must be an array of strings or null")

    normalized_json = _canonical_json(record)
    normalized_json_bytes = normalized_json.encode("utf-8")
    activities = _normalize_activities(record, max_activities=max_activities)
    normalized_bytes = len(normalized_json_bytes) + sum(
        activity.normalized_bytes for activity in activities
    )
    if normalized_bytes > max_normalized_bytes:
        raise ArchiveValidationError(
            f"normalized record exceeds {max_normalized_bytes} bytes"
        )
    return NormalizedCompany(
        source_archive_name=archive_name,
        source_archive_sha256=archive_sha256,
        source_member=member_name,
        source_record_index=record_index,
        corporate_number=corporate_number,
        name=_optional_text(record, "name"),
        kana=_optional_text(record, "kana"),
        name_en=_optional_text(record, "name_en"),
        postal_code=_optional_text(record, "postal_code"),
        location=_optional_text(record, "location"),
        kind=_optional_text(record, "kind"),
        process=_optional_text(record, "process"),
        status=_optional_text(record, "status"),
        representative_name=_optional_text(record, "representative_name"),
        capital_stock=_optional_text(record, "capital_stock"),
        employee_number=_optional_text(record, "employee_number"),
        business_summary=_optional_text(record, "business_summary"),
        company_url=_optional_text(record, "company_url"),
        founding_year=_optional_text(record, "founding_year"),
        update_date=_optional_text(record, "update_date"),
        industry_codes=industry_codes,
        is_infocom="G" in industry_codes,
        normalized_json=normalized_json,
        normalized_record_sha256=hashlib.sha256(normalized_json_bytes).hexdigest(),
        normalized_bytes=normalized_bytes,
        activities=activities,
    )


def _normalize_target_industry(target_industry: str) -> str:
    target = str(target_industry).strip().upper()
    if target != "ALL" and not re.fullmatch(r"[A-T]", target):
        raise ValueError("target_industry must be ALL or one JSIC major code A-T")
    return target


def _iter_archive_events(
    archive: zipfile.ZipFile,
    *,
    archive_name: str,
    archive_sha256: str,
    target_industry: str,
    limits: ArchiveLimits,
) -> Iterator[NormalizedCompany | _MemberComplete]:
    members = _preflight_archive(archive, limits)
    total_counter = [0]
    for member_index, (info, member_name) in enumerate(members, 1):
        source_records = 0
        imported_records = 0
        try:
            with archive.open(info, "r") as stream:
                reader = _MemberReader(
                    stream,
                    member_name=member_name,
                    limits=limits,
                    total_counter=total_counter,
                )
                for source_record_index, raw_record in enumerate(
                    _iter_json_array(
                        reader,
                        member_name=member_name,
                        max_value_chars=limits.max_json_value_chars,
                    ),
                    1,
                ):
                    source_records += 1
                    company = _normalize_company(
                        raw_record,
                        archive_name=archive_name,
                        archive_sha256=archive_sha256,
                        member_name=member_name,
                        record_index=source_record_index,
                        max_activities=limits.max_activities_per_record,
                        max_normalized_bytes=limits.max_normalized_record_bytes,
                    )
                    if target_industry != "ALL" and target_industry not in company.industry_codes:
                        continue
                    imported_records += 1
                    yield company
        except (zipfile.BadZipFile, RuntimeError, EOFError) as exc:
            if isinstance(exc, ArchiveValidationError):
                raise
            raise ArchiveValidationError(f"cannot decompress ZIP member: {member_name!r}") from exc
        if reader.bytes_read != info.file_size:
            raise ArchiveValidationError(
                f"ZIP member size does not match its metadata: {member_name!r}"
            )
        yield _MemberComplete(
            _MemberSummary(
                member_name=member_name,
                member_index=member_index,
                compressed_bytes=info.compress_size,
                uncompressed_bytes=reader.bytes_read,
                member_sha256=reader.digest.hexdigest(),
                source_records=source_records,
                imported_records=imported_records,
            )
        )


def _open_validated_source(
    archive_path: Path,
    limits: ArchiveLimits,
) -> tuple[BinaryIO, str, tuple[int, int, int, int, int]]:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(archive_path, flags)
        handle = os.fdopen(descriptor, "rb")
        descriptor = None
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise ArchiveValidationError(f"cannot open archive: {archive_path}") from exc
    try:
        source_info = os.fstat(handle.fileno())
        if not stat.S_ISREG(source_info.st_mode):
            raise ArchiveValidationError(f"archive source is not a regular file: {archive_path}")
        fingerprint = _source_fingerprint(handle)
        if fingerprint[2] > limits.max_archive_bytes:
            raise ArchiveValidationError(
                f"archive is too large: {fingerprint[2]} > {limits.max_archive_bytes}"
            )
        sha256 = _sha256_stream(
            handle,
            limits.read_chunk_bytes,
            limits.max_archive_bytes,
        )
        handle.seek(0)
        return handle, sha256, fingerprint
    except Exception:
        handle.close()
        raise


def iter_normalized_batches(
    archive_path: str | Path,
    *,
    batch_size: int = 1000,
    target_industry: str = "G",
    limits: ArchiveLimits | None = None,
) -> Iterator[tuple[NormalizedCompany, ...]]:
    """Yield prevalidated company records without writing to a database.

    A complete validation pass runs before the first batch is exposed.  The
    caller should still exhaust the iterator so a concurrent source change
    during the second pass is detected by the final fingerprint check.
    """

    if not 1 <= batch_size <= 100_000:
        raise ValueError("batch_size must be between 1 and 100000")
    configured_limits = limits or ArchiveLimits()
    configured_limits.validate()
    target = _normalize_target_industry(target_industry)
    source = Path(archive_path).expanduser().resolve(strict=True)
    handle, archive_sha256, fingerprint = _open_validated_source(source, configured_limits)
    try:
        # Validate the complete archive before exposing any batch.  This first
        # pass prevents callers from persisting rows from a member whose CRC,
        # trailing JSON, or later member eventually fails validation.
        try:
            archive = zipfile.ZipFile(handle)
        except zipfile.BadZipFile as exc:
            raise ArchiveValidationError(f"not a valid ZIP archive: {source}") from exc
        with archive:
            for _event in _iter_archive_events(
                archive,
                archive_name=source.name,
                archive_sha256=archive_sha256,
                target_industry=target,
                limits=configured_limits,
            ):
                pass
        if _source_fingerprint(handle) != fingerprint:
            raise ArchiveValidationError("archive changed while it was being validated")

        handle.seek(0)
        try:
            archive = zipfile.ZipFile(handle)
        except zipfile.BadZipFile as exc:  # pragma: no cover - first pass already opened it.
            raise ArchiveValidationError(f"not a valid ZIP archive: {source}") from exc
        with archive:
            batch: list[NormalizedCompany] = []
            batch_bytes = 0
            for event in _iter_archive_events(
                archive,
                archive_name=source.name,
                archive_sha256=archive_sha256,
                target_industry=target,
                limits=configured_limits,
            ):
                if isinstance(event, _MemberComplete):
                    continue
                if batch and (
                    len(batch) >= batch_size
                    or batch_bytes + event.normalized_bytes
                    > configured_limits.max_batch_normalized_bytes
                ):
                    yield tuple(batch)
                    batch.clear()
                    batch_bytes = 0
                batch.append(event)
                batch_bytes += event.normalized_bytes
            if batch:
                yield tuple(batch)
        if _source_fingerprint(handle) != fingerprint:
            raise ArchiveValidationError("archive changed while it was being read")
    finally:
        handle.close()


_STAGING_SCHEMA = """
CREATE SCHEMA gbiz_archive;

CREATE TABLE gbiz_archive.import_runs (
    import_id VARCHAR PRIMARY KEY,
    source_path VARCHAR NOT NULL,
    source_filename VARCHAR NOT NULL,
    source_sha256 VARCHAR NOT NULL,
    source_size_bytes BIGINT NOT NULL,
    imported_at TIMESTAMPTZ NOT NULL,
    target_industry VARCHAR NOT NULL,
    source_json_member_count INTEGER NOT NULL,
    source_record_count BIGINT NOT NULL,
    imported_record_count BIGINT NOT NULL,
    activity_record_count BIGINT NOT NULL,
    json_uncompressed_bytes BIGINT NOT NULL,
    importer_version VARCHAR NOT NULL,
    limits_json VARCHAR NOT NULL
);

CREATE TABLE gbiz_archive.archive_members (
    import_id VARCHAR NOT NULL,
    member_index INTEGER NOT NULL,
    member_name VARCHAR NOT NULL,
    compressed_bytes BIGINT NOT NULL,
    uncompressed_bytes BIGINT NOT NULL,
    member_sha256 VARCHAR NOT NULL,
    source_records BIGINT NOT NULL,
    imported_records BIGINT NOT NULL,
    PRIMARY KEY (import_id, member_index)
);

CREATE TABLE gbiz_archive.companies (
    import_id VARCHAR NOT NULL,
    source_member VARCHAR NOT NULL,
    source_record_index BIGINT NOT NULL,
    corporate_number VARCHAR NOT NULL,
    name VARCHAR,
    kana VARCHAR,
    name_en VARCHAR,
    postal_code VARCHAR,
    location VARCHAR,
    kind VARCHAR,
    process VARCHAR,
    status VARCHAR,
    representative_name VARCHAR,
    capital_stock VARCHAR,
    employee_number VARCHAR,
    business_summary VARCHAR,
    company_url VARCHAR,
    founding_year VARCHAR,
    update_date VARCHAR,
    industry_codes_json VARCHAR NOT NULL,
    is_infocom BOOLEAN NOT NULL,
    normalized_json VARCHAR NOT NULL,
    normalized_record_sha256 VARCHAR NOT NULL,
    normalized_bytes BIGINT NOT NULL,
    PRIMARY KEY (import_id, source_member, source_record_index)
);

CREATE TABLE gbiz_archive.activities (
    import_id VARCHAR NOT NULL,
    source_member VARCHAR NOT NULL,
    source_record_index BIGINT NOT NULL,
    corporate_number VARCHAR NOT NULL,
    activity_type VARCHAR NOT NULL,
    activity_index INTEGER NOT NULL,
    source_key VARCHAR,
    normalized_json VARCHAR NOT NULL,
    normalized_sha256 VARCHAR NOT NULL,
    normalized_bytes BIGINT NOT NULL
);
"""


_INSERT_COMPANY = """
INSERT INTO gbiz_archive.companies VALUES (
    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
)
"""


def _company_row(import_id: str, company: NormalizedCompany) -> tuple[Any, ...]:
    return (
        import_id,
        company.source_member,
        company.source_record_index,
        company.corporate_number,
        company.name,
        company.kana,
        company.name_en,
        company.postal_code,
        company.location,
        company.kind,
        company.process,
        company.status,
        company.representative_name,
        company.capital_stock,
        company.employee_number,
        company.business_summary,
        company.company_url,
        company.founding_year,
        company.update_date,
        _canonical_json(company.industry_codes),
        company.is_infocom,
        company.normalized_json,
        company.normalized_record_sha256,
        company.normalized_bytes,
    )


def _insert_batch(connection: Any, import_id: str, companies: Sequence[NormalizedCompany]) -> int:
    if not companies:
        return 0
    connection.executemany(
        _INSERT_COMPANY,
        [_company_row(import_id, company) for company in companies],
    )
    inserted = 0
    activities: list[tuple[Any, ...]] = []
    for company in companies:
        for activity in company.activities:
            activities.append(
                (
                    import_id,
                    company.source_member,
                    company.source_record_index,
                    company.corporate_number,
                    activity.activity_type,
                    activity.activity_index,
                    activity.source_key,
                    activity.normalized_json,
                    activity.normalized_sha256,
                    activity.normalized_bytes,
                )
            )
            if len(activities) >= 1000:
                connection.executemany(
                    "INSERT INTO gbiz_archive.activities VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    activities,
                )
                inserted += len(activities)
                activities.clear()
    if activities:
        connection.executemany(
            "INSERT INTO gbiz_archive.activities VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            activities,
        )
        inserted += len(activities)
    return inserted


def import_archive_to_staging(
    archive_path: str | Path,
    staging_database: str | Path,
    *,
    batch_size: int = 1000,
    target_industry: str = "G",
    limits: ArchiveLimits | None = None,
) -> ImportResult:
    """Create a new staging DuckDB from a historical Hojinjoho ZIP.

    Existing files (including an existing canonical database) are always
    rejected.  The new file is removed if validation or import fails.
    """

    if not 1 <= batch_size <= 100_000:
        raise ValueError("batch_size must be between 1 and 100000")
    configured_limits = limits or ArchiveLimits()
    configured_limits.validate()
    target = _normalize_target_industry(target_industry)

    source = Path(archive_path).expanduser().resolve(strict=True)
    requested_staging = Path(staging_database).expanduser()
    if requested_staging.is_symlink() or requested_staging.exists():
        raise StagingDatabaseError(f"staging database already exists: {requested_staging}")
    staging = requested_staging.resolve(strict=False)
    if staging == source:
        raise StagingDatabaseError("archive and staging database paths must differ")
    if staging.suffix.lower() not in {".duckdb", ".ddb"}:
        raise StagingDatabaseError("staging database must use a .duckdb or .ddb suffix")
    staging.parent.mkdir(parents=True, exist_ok=True)
    building = staging.with_name(f".{staging.name}.{uuid4().hex}.building")

    handle, archive_sha256, fingerprint = _open_validated_source(source, configured_limits)
    import_id = str(uuid4())
    connection = None
    try:
        try:
            archive = zipfile.ZipFile(handle)
        except zipfile.BadZipFile as exc:
            raise ArchiveValidationError(f"not a valid ZIP archive: {source}") from exc

        connection = _duckdb_module().connect(str(building))
        connection.execute(_STAGING_SCHEMA)
        connection.execute("BEGIN TRANSACTION")
        member_summaries: list[_MemberSummary] = []
        company_batch: list[NormalizedCompany] = []
        company_batch_bytes = 0
        activity_records = 0
        with archive:
            for event in _iter_archive_events(
                archive,
                archive_name=source.name,
                archive_sha256=archive_sha256,
                target_industry=target,
                limits=configured_limits,
            ):
                if isinstance(event, _MemberComplete):
                    member_summaries.append(event.summary)
                    continue
                if company_batch and (
                    len(company_batch) >= batch_size
                    or company_batch_bytes + event.normalized_bytes
                    > configured_limits.max_batch_normalized_bytes
                ):
                    activity_records += _insert_batch(connection, import_id, company_batch)
                    company_batch.clear()
                    company_batch_bytes = 0
                company_batch.append(event)
                company_batch_bytes += event.normalized_bytes
            activity_records += _insert_batch(connection, import_id, company_batch)

        if _source_fingerprint(handle) != fingerprint:
            raise ArchiveValidationError("archive changed while it was being read")

        connection.executemany(
            "INSERT INTO gbiz_archive.archive_members VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    import_id,
                    summary.member_index,
                    summary.member_name,
                    summary.compressed_bytes,
                    summary.uncompressed_bytes,
                    summary.member_sha256,
                    summary.source_records,
                    summary.imported_records,
                )
                for summary in member_summaries
            ],
        )
        source_records = sum(summary.source_records for summary in member_summaries)
        imported_records = sum(summary.imported_records for summary in member_summaries)
        json_uncompressed_bytes = sum(
            summary.uncompressed_bytes for summary in member_summaries
        )
        connection.execute(
            """
            INSERT INTO gbiz_archive.import_runs
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                import_id,
                str(source),
                source.name,
                archive_sha256,
                fingerprint[2],
                datetime.now(timezone.utc),
                target,
                len(member_summaries),
                source_records,
                imported_records,
                activity_records,
                json_uncompressed_bytes,
                IMPORTER_VERSION,
                _canonical_json(asdict(configured_limits)),
            ],
        )
        connection.execute("COMMIT")
        connection.close()
        connection = None
        try:
            # A hard link is an atomic no-clobber publication on the same
            # filesystem.  It closes the exists()/connect() race that could
            # otherwise open an unrelated database created by another process.
            os.link(building, staging)
        except FileExistsError as exc:
            raise StagingDatabaseError(
                f"staging database was created concurrently: {staging}"
            ) from exc
        except OSError as exc:
            raise StagingDatabaseError(
                "cannot atomically publish the staging database on this filesystem"
            ) from exc
        try:
            building.unlink()
        except OSError:
            pass
        return ImportResult(
            staging_database=staging,
            import_id=import_id,
            source_sha256=archive_sha256,
            source_records=source_records,
            imported_records=imported_records,
            activity_records=activity_records,
            json_member_count=len(member_summaries),
            json_uncompressed_bytes=json_uncompressed_bytes,
        )
    except GBizArchiveError:
        raise
    except Exception as exc:
        raise GBizArchiveError(f"historical archive import failed: {exc}") from exc
    finally:
        handle.close()
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        for path in (building, Path(str(building) + ".wal")):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
