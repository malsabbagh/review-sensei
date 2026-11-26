"""ReviewSensei v6 PR-resident immutable recovery ledger and marker codecs.

Implements canonical SHA-256 helpers, strict App-authored HTML marker
serialization/parsing, frozen proof/intent/receipt/unresolved/advisory records,
and deterministic union reconciliation so stale writers can never erase an
active write intent or completed receipt.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, TypeAlias

from review_sensei.errors import ReviewInputError


class V6LedgerIntegrityError(ReviewInputError):
    """Raised when a v6 ledger marker, schema, field type, or digest is invalid."""

    error_category = "v6_ledger_integrity"


INTENT_SCHEMA = "reviewsensei:v6:intent:v1"
RECEIPT_SCHEMA = "reviewsensei:v6:receipt:v1"
UNRESOLVED_RESULT_SCHEMA = "reviewsensei:v6:unresolved:v1"
PROGRESS_HINT_SCHEMA = "reviewsensei:v6:progress:v1"

VALID_V6_RECORD_SCHEMAS: frozenset[str] = frozenset(
    {
        INTENT_SCHEMA,
        RECEIPT_SCHEMA,
        UNRESOLVED_RESULT_SCHEMA,
        PROGRESS_HINT_SCHEMA,
    }
)

_MARKER_PREFIX = "<!-- reviewsensei:v6:record "
_MARKER_RE = re.compile(
    r"^<!-- reviewsensei:v6:record sha256=(?P<sha>[0-9a-f]{64}) "
    r"payload=(?P<payload>\{.*\}) -->$"
)
_SHA1_40_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_64_RE = re.compile(r"^[0-9a-f]{64}$")

_FORBIDDEN_PROGRESS_HINT_KEYS: frozenset[str] = frozenset(
    {
        "pending_write_intent",
        "intent",
        "intents",
        "receipt",
        "receipts",
        "unresolved",
        "unresolved_result",
        "unresolved_results",
        "findings_payload",
        "github_effect_id",
        "effect_kind",
        "target_digest",
        "writer_epoch",
        "pr_head_sha",
    }
)


def _to_json_compatible(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise V6LedgerIntegrityError(
                f"non-finite float in v6 ledger payload: {value!r}",
                diagnostic="v6_non_finite_float",
            )
        return value
    if isinstance(value, Mapping):
        return {str(k): _to_json_compatible(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_json_compatible(item) for item in value]
    raise ReviewInputError(
        f"unsupported non-JSON value in v6 ledger payload: {type(value).__name__}",
        diagnostic="v6_non_json_value",
    )


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize ``value`` deterministically to compact UTF-8 canonical JSON."""

    normalized = _to_json_compatible(value)
    text = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return text.encode("utf-8")


def canonical_json_dumps(value: Any) -> str:
    """Serialize ``value`` deterministically to a compact canonical JSON string."""

    return canonical_json_bytes(value).decode("utf-8")


def canonical_sha256_bytes(data: bytes) -> str:
    """Return lowercase 64-hex SHA-256 digest of raw ``bytes``."""

    if not isinstance(data, (bytes, bytearray)):
        raise ReviewInputError(
            f"expected bytes for SHA-256 digest, got {type(data).__name__}",
            diagnostic="v6_invalid_digest_input",
        )
    return hashlib.sha256(bytes(data)).hexdigest()


def canonical_sha256(value: Any) -> str:
    """Return lowercase 64-hex SHA-256 digest of ``value``'s canonical JSON."""

    return canonical_sha256_bytes(canonical_json_bytes(value))


def _require_strict_int(value: Any, *, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ReviewInputError(
            f"{name} must be an integer >= {minimum}, got {value!r}",
            diagnostic="v6_invalid_ledger_field",
        )
    return value


def _require_non_empty_str(value: Any, *, name: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise ReviewInputError(
            f"{name} must be a non-empty trimmed string, got {value!r}",
            diagnostic="v6_invalid_ledger_field",
        )
    if "\n" in value or "\r" in value:
        raise ReviewInputError(
            f"{name} must not contain line breaks",
            diagnostic="v6_invalid_ledger_field",
        )
    return value


def _require_single_line_str(
    value: Any, *, name: str, allow_empty: bool = False
) -> str:
    if type(value) is not str:
        raise ReviewInputError(
            f"{name} must be a string, got {type(value).__name__}",
            diagnostic="v6_invalid_ledger_field",
        )
    if "\n" in value or "\r" in value:
        raise ReviewInputError(
            f"{name} must not contain line breaks",
            diagnostic="v6_invalid_ledger_field",
        )
    if not allow_empty and (not value or value.strip() != value):
        raise ReviewInputError(
            f"{name} must be non-empty without surrounding whitespace",
            diagnostic="v6_invalid_ledger_field",
        )
    return value


def _require_sha1_40(value: Any, *, name: str) -> str:
    if type(value) is not str or _SHA1_40_RE.fullmatch(value) is None:
        raise ReviewInputError(
            f"{name} must be a 40-character lowercase hex SHA, got {value!r}",
            diagnostic="v6_invalid_ledger_field",
        )
    return value


def _require_sha256_64(value: Any, *, name: str) -> str:
    if type(value) is not str or _SHA256_64_RE.fullmatch(value) is None:
        raise ReviewInputError(
            f"{name} must be a 64-character lowercase hex SHA-256, got {value!r}",
            diagnostic="v6_invalid_ledger_field",
        )
    return value


def _require_effect_or_record_id(value: Any, *, name: str) -> str | int:
    if type(value) is int:
        if value <= 0:
            raise ReviewInputError(
                f"{name} integer ID must be > 0, got {value!r}",
                diagnostic="v6_invalid_ledger_field",
            )
        return value
    return _require_non_empty_str(value, name=name)


def _freeze_findings_payload(raw_payload: Any) -> tuple[Mapping[str, Any], ...]:
    parsed: Any = raw_payload
    if isinstance(raw_payload, str):
        try:
            parsed = json.loads(raw_payload)
        except ValueError as error:
            raise ReviewInputError(
                f"findings_payload JSON is malformed: {error}",
                diagnostic="v6_invalid_ledger_field",
            ) from error
    if not isinstance(parsed, Sequence) or isinstance(parsed, (str, bytes, bytearray)):
        raise ReviewInputError(
            "findings_payload must be a sequence of mapping objects",
            diagnostic="v6_invalid_ledger_field",
        )
    frozen: list[Mapping[str, Any]] = []
    for index, item in enumerate(parsed):
        if not isinstance(item, Mapping):
            raise ReviewInputError(
                f"findings_payload[{index}] must be a mapping",
                diagnostic="v6_invalid_ledger_field",
            )
        normalized = _to_json_compatible(dict(item))
        frozen.append(MappingProxyType(normalized))
    return tuple(frozen)


@dataclass(frozen=True)
class WriterOwnershipProof:
    """Broker-verified exclusive writer ownership proof bound to a live PR head."""

    repository_id: int
    pull_request: int
    operation_id: str
    run_id: int
    run_attempt: int
    writer_epoch: int
    pr_head_sha: str
    verified_by_broker: bool

    def __post_init__(self) -> None:
        _require_strict_int(self.repository_id, name="repository_id", minimum=1)
        _require_strict_int(self.pull_request, name="pull_request", minimum=1)
        _require_non_empty_str(self.operation_id, name="operation_id")
        _require_strict_int(self.run_id, name="run_id", minimum=1)
        _require_strict_int(self.run_attempt, name="run_attempt", minimum=1)
        _require_strict_int(self.writer_epoch, name="writer_epoch", minimum=0)
        _require_sha1_40(self.pr_head_sha, name="pr_head_sha")
        if type(self.verified_by_broker) is not bool:
            raise ReviewInputError(
                "verified_by_broker must be a strict bool",
                diagnostic="v6_invalid_ledger_field",
            )


@dataclass(frozen=True)
class WriteIntentRecord:
    """Immutable App-authored write intent recorded prior to external mutation."""

    record_id: str
    operation_id: str
    writer_epoch: int
    pr_head_sha: str
    effect_kind: str
    payload_sha256: str
    target_digest: str
    schema: str = INTENT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != INTENT_SCHEMA:
            raise ReviewInputError(
                f"invalid WriteIntentRecord schema: {self.schema!r}",
                diagnostic="v6_unknown_schema",
            )
        _require_non_empty_str(self.record_id, name="record_id")
        _require_non_empty_str(self.operation_id, name="operation_id")
        _require_strict_int(self.writer_epoch, name="writer_epoch", minimum=0)
        _require_sha1_40(self.pr_head_sha, name="pr_head_sha")
        _require_non_empty_str(self.effect_kind, name="effect_kind")
        _require_sha256_64(self.payload_sha256, name="payload_sha256")
        _require_non_empty_str(self.target_digest, name="target_digest")


@dataclass(frozen=True)
class CompletionReceiptRecord:
    """Immutable App-authored receipt recording a confirmed/reconciled GitHub write."""

    record_id: str
    operation_id: str
    writer_epoch: int
    github_effect_id: str | int
    payload_sha256: str
    completed_at: str
    schema: str = RECEIPT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != RECEIPT_SCHEMA:
            raise ReviewInputError(
                f"invalid CompletionReceiptRecord schema: {self.schema!r}",
                diagnostic="v6_unknown_schema",
            )
        _require_non_empty_str(self.record_id, name="record_id")
        _require_non_empty_str(self.operation_id, name="operation_id")
        _require_strict_int(self.writer_epoch, name="writer_epoch", minimum=0)
        _require_effect_or_record_id(self.github_effect_id, name="github_effect_id")
        _require_sha256_64(self.payload_sha256, name="payload_sha256")
        _require_non_empty_str(self.completed_at, name="completed_at")


@dataclass(frozen=True)
class UnresolvedResultRecord:
    """Authoritative PR-resident unresolved review/finding result."""

    record_id: str
    operation_id: str
    pr_head_sha: str
    payload_sha256: str
    findings_payload: tuple[Mapping[str, Any], ...]
    required_evidence_digests: tuple[str, ...]
    artifact_cache_sha256: str | None = None
    schema: str = UNRESOLVED_RESULT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != UNRESOLVED_RESULT_SCHEMA:
            raise ReviewInputError(
                f"invalid UnresolvedResultRecord schema: {self.schema!r}",
                diagnostic="v6_unknown_schema",
            )
        _require_non_empty_str(self.record_id, name="record_id")
        _require_non_empty_str(self.operation_id, name="operation_id")
        _require_sha1_40(self.pr_head_sha, name="pr_head_sha")
        _require_sha256_64(self.payload_sha256, name="payload_sha256")
        frozen_findings = _freeze_findings_payload(self.findings_payload)
        object.__setattr__(self, "findings_payload", frozen_findings)
        if not isinstance(self.required_evidence_digests, Sequence) or isinstance(
            self.required_evidence_digests, (str, bytes, bytearray)
        ):
            raise ReviewInputError(
                "required_evidence_digests must be a sequence of digest strings",
                diagnostic="v6_invalid_ledger_field",
            )
        validated_digests = tuple(
            _require_non_empty_str(digest, name="required_evidence_digest")
            for digest in self.required_evidence_digests
        )
        object.__setattr__(self, "required_evidence_digests", validated_digests)
        if self.artifact_cache_sha256 is not None:
            _require_sha256_64(self.artifact_cache_sha256, name="artifact_cache_sha256")


@dataclass(frozen=True)
class AdvisoryProgressHint:
    """Strictly read-phase progress/stall hint; never holds sole write or result truth."""

    operation_id: str
    recovery_phase: str
    cursor_hint: str | None
    verified_prefix_count: int
    verified_prefix_digest: str
    last_verified_id: str | int | None
    stall_count: int
    last_progress_digest: str
    transient_error_count: int
    schema: str = PROGRESS_HINT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PROGRESS_HINT_SCHEMA:
            raise ReviewInputError(
                f"invalid AdvisoryProgressHint schema: {self.schema!r}",
                diagnostic="v6_unknown_schema",
            )
        _require_non_empty_str(self.operation_id, name="operation_id")
        _require_non_empty_str(self.recovery_phase, name="recovery_phase")
        if self.cursor_hint is not None:
            _require_single_line_str(self.cursor_hint, name="cursor_hint")
        prefix_count = _require_strict_int(
            self.verified_prefix_count, name="verified_prefix_count", minimum=0
        )
        if prefix_count > 0 or self.verified_prefix_digest != "":
            _require_sha256_64(
                self.verified_prefix_digest, name="verified_prefix_digest"
            )
        if self.last_verified_id is not None:
            _require_effect_or_record_id(self.last_verified_id, name="last_verified_id")
        _require_strict_int(self.stall_count, name="stall_count", minimum=0)
        _require_single_line_str(
            self.last_progress_digest, name="last_progress_digest", allow_empty=True
        )
        _require_strict_int(
            self.transient_error_count, name="transient_error_count", minimum=0
        )


V6LedgerRecord: TypeAlias = (
    WriteIntentRecord
    | CompletionReceiptRecord
    | UnresolvedResultRecord
    | AdvisoryProgressHint
)


def record_to_dict(record: V6LedgerRecord) -> dict[str, Any]:
    """Convert a v6 ledger record dataclass into a canonical JSON-compatible dict."""

    if isinstance(record, WriteIntentRecord):
        return {
            "schema": record.schema,
            "record_id": record.record_id,
            "operation_id": record.operation_id,
            "writer_epoch": record.writer_epoch,
            "pr_head_sha": record.pr_head_sha,
            "effect_kind": record.effect_kind,
            "payload_sha256": record.payload_sha256,
            "target_digest": record.target_digest,
        }
    if isinstance(record, CompletionReceiptRecord):
        return {
            "schema": record.schema,
            "record_id": record.record_id,
            "operation_id": record.operation_id,
            "writer_epoch": record.writer_epoch,
            "github_effect_id": record.github_effect_id,
            "payload_sha256": record.payload_sha256,
            "completed_at": record.completed_at,
        }
    if isinstance(record, UnresolvedResultRecord):
        return {
            "schema": record.schema,
            "record_id": record.record_id,
            "operation_id": record.operation_id,
            "pr_head_sha": record.pr_head_sha,
            "payload_sha256": record.payload_sha256,
            "findings_payload": _to_json_compatible(record.findings_payload),
            "required_evidence_digests": list(record.required_evidence_digests),
            "artifact_cache_sha256": record.artifact_cache_sha256,
        }
    if isinstance(record, AdvisoryProgressHint):
        return {
            "schema": record.schema,
            "operation_id": record.operation_id,
            "recovery_phase": record.recovery_phase,
            "cursor_hint": record.cursor_hint,
            "verified_prefix_count": record.verified_prefix_count,
            "verified_prefix_digest": record.verified_prefix_digest,
            "last_verified_id": record.last_verified_id,
            "stall_count": record.stall_count,
            "last_progress_digest": record.last_progress_digest,
            "transient_error_count": record.transient_error_count,
        }
    raise ReviewInputError(
        f"unsupported v6 ledger record type: {type(record).__name__}",
        diagnostic="v6_unknown_schema",
    )


def _require_mapping_str(payload: Mapping[str, Any], key: str) -> str:
    value = payload[key]
    if type(value) is not str:
        raise V6LedgerIntegrityError(
            f"field {key!r} must be str, got {type(value).__name__}",
            diagnostic="v6_malformed_marker",
        )
    return value


def _require_mapping_opt_str(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload[key]
    if value is not None and type(value) is not str:
        raise V6LedgerIntegrityError(
            f"field {key!r} must be str or None, got {type(value).__name__}",
            diagnostic="v6_malformed_marker",
        )
    return value


def _require_mapping_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload[key]
    if type(value) is not int:
        raise V6LedgerIntegrityError(
            f"field {key!r} must be strict int, got {type(value).__name__}",
            diagnostic="v6_malformed_marker",
        )
    return value


def _require_mapping_id(payload: Mapping[str, Any], key: str) -> str | int:
    value = payload[key]
    if type(value) is not int and type(value) is not str:
        raise V6LedgerIntegrityError(
            f"field {key!r} must be str or strict int, got {type(value).__name__}",
            diagnostic="v6_malformed_marker",
        )
    return value


def _require_mapping_opt_id(payload: Mapping[str, Any], key: str) -> str | int | None:
    value = payload[key]
    if value is not None and type(value) is not int and type(value) is not str:
        raise V6LedgerIntegrityError(
            f"field {key!r} must be str, strict int, or None, got {type(value).__name__}",
            diagnostic="v6_malformed_marker",
        )
    return value


def _record_from_mapping(payload: Mapping[str, Any]) -> V6LedgerRecord:
    schema = payload.get("schema")
    if not isinstance(schema, str) or schema not in VALID_V6_RECORD_SCHEMAS:
        raise V6LedgerIntegrityError(
            f"unknown v6 ledger record schema: {schema!r}",
            diagnostic="v6_unknown_schema",
        )

    try:
        if schema == INTENT_SCHEMA:
            expected_keys = {
                "schema",
                "record_id",
                "operation_id",
                "writer_epoch",
                "pr_head_sha",
                "effect_kind",
                "payload_sha256",
                "target_digest",
            }
            if set(payload.keys()) != expected_keys:
                raise V6LedgerIntegrityError(
                    "WriteIntentRecord has unexpected or missing keys",
                    diagnostic="v6_malformed_marker",
                )
            return WriteIntentRecord(
                schema=schema,
                record_id=_require_mapping_str(payload, "record_id"),
                operation_id=_require_mapping_str(payload, "operation_id"),
                writer_epoch=_require_mapping_int(payload, "writer_epoch"),
                pr_head_sha=_require_mapping_str(payload, "pr_head_sha"),
                effect_kind=_require_mapping_str(payload, "effect_kind"),
                payload_sha256=_require_mapping_str(payload, "payload_sha256"),
                target_digest=_require_mapping_str(payload, "target_digest"),
            )

        if schema == RECEIPT_SCHEMA:
            expected_keys = {
                "schema",
                "record_id",
                "operation_id",
                "writer_epoch",
                "github_effect_id",
                "payload_sha256",
                "completed_at",
            }
            if set(payload.keys()) != expected_keys:
                raise V6LedgerIntegrityError(
                    "CompletionReceiptRecord has unexpected or missing keys",
                    diagnostic="v6_malformed_marker",
                )
            return CompletionReceiptRecord(
                schema=schema,
                record_id=_require_mapping_str(payload, "record_id"),
                operation_id=_require_mapping_str(payload, "operation_id"),
                writer_epoch=_require_mapping_int(payload, "writer_epoch"),
                github_effect_id=_require_mapping_id(payload, "github_effect_id"),
                payload_sha256=_require_mapping_str(payload, "payload_sha256"),
                completed_at=_require_mapping_str(payload, "completed_at"),
            )

        if schema == UNRESOLVED_RESULT_SCHEMA:
            expected_keys = {
                "schema",
                "record_id",
                "operation_id",
                "pr_head_sha",
                "payload_sha256",
                "findings_payload",
                "required_evidence_digests",
                "artifact_cache_sha256",
            }
            if set(payload.keys()) != expected_keys:
                raise V6LedgerIntegrityError(
                    "UnresolvedResultRecord has unexpected or missing keys",
                    diagnostic="v6_malformed_marker",
                )
            raw_findings = payload["findings_payload"]
            if not isinstance(raw_findings, (list, tuple)):
                raise V6LedgerIntegrityError(
                    "field 'findings_payload' must be a list or tuple of mappings",
                    diagnostic="v6_malformed_marker",
                )
            raw_digests = payload["required_evidence_digests"]
            if not isinstance(raw_digests, (list, tuple)) or any(
                type(item) is not str for item in raw_digests
            ):
                raise V6LedgerIntegrityError(
                    "field 'required_evidence_digests' must be a list or tuple of strings",
                    diagnostic="v6_malformed_marker",
                )
            return UnresolvedResultRecord(
                schema=schema,
                record_id=_require_mapping_str(payload, "record_id"),
                operation_id=_require_mapping_str(payload, "operation_id"),
                pr_head_sha=_require_mapping_str(payload, "pr_head_sha"),
                payload_sha256=_require_mapping_str(payload, "payload_sha256"),
                findings_payload=tuple(raw_findings),
                required_evidence_digests=tuple(raw_digests),
                artifact_cache_sha256=_require_mapping_opt_str(
                    payload, "artifact_cache_sha256"
                ),
            )

        # PROGRESS_HINT_SCHEMA
        forbidden = _FORBIDDEN_PROGRESS_HINT_KEYS.intersection(payload.keys())
        if forbidden:
            raise V6LedgerIntegrityError(
                "AdvisoryProgressHint cannot contain write/result fields: "
                f"{sorted(forbidden)}",
                diagnostic="v6_forbidden_advisory_payload",
            )
        expected_keys = {
            "schema",
            "operation_id",
            "recovery_phase",
            "cursor_hint",
            "verified_prefix_count",
            "verified_prefix_digest",
            "last_verified_id",
            "stall_count",
            "last_progress_digest",
            "transient_error_count",
        }
        if set(payload.keys()) != expected_keys:
            raise V6LedgerIntegrityError(
                "AdvisoryProgressHint has unexpected or missing keys",
                diagnostic="v6_malformed_marker",
            )
        return AdvisoryProgressHint(
            schema=schema,
            operation_id=_require_mapping_str(payload, "operation_id"),
            recovery_phase=_require_mapping_str(payload, "recovery_phase"),
            cursor_hint=_require_mapping_opt_str(payload, "cursor_hint"),
            verified_prefix_count=_require_mapping_int(
                payload, "verified_prefix_count"
            ),
            verified_prefix_digest=_require_mapping_str(
                payload, "verified_prefix_digest"
            ),
            last_verified_id=_require_mapping_opt_id(payload, "last_verified_id"),
            stall_count=_require_mapping_int(payload, "stall_count"),
            last_progress_digest=_require_mapping_str(payload, "last_progress_digest"),
            transient_error_count=_require_mapping_int(
                payload, "transient_error_count"
            ),
        )
    except KeyError as error:
        raise V6LedgerIntegrityError(
            f"missing field {error} in v6 ledger record",
            diagnostic="v6_malformed_marker",
        ) from error
    except V6LedgerIntegrityError:
        raise
    except ReviewInputError as error:
        raise V6LedgerIntegrityError(
            str(error),
            diagnostic="v6_malformed_marker",
        ) from error


def format_v6_record_marker(record: V6LedgerRecord) -> str:
    """Format a v6 record into an integrity-checked HTML comment marker."""

    payload_dict = record_to_dict(record)
    digest = canonical_sha256(payload_dict)
    raw_json = canonical_json_dumps(payload_dict)
    # Escape angle brackets so embedded strings cannot close the HTML comment.
    safe_json = raw_json.replace("<", "\\u003c").replace(">", "\\u003e")
    return f"{_MARKER_PREFIX}sha256={digest} payload={safe_json} -->"


def _reject_json_constant(constant: str) -> Any:
    raise ReviewInputError(
        f"invalid JSON constant {constant!r} in v6 marker",
        diagnostic="v6_malformed_marker",
    )


def parse_v6_record_marker(marker_text: str) -> V6LedgerRecord:
    """Parse and SHA-256 verify a single v6 HTML comment marker (fail-closed)."""

    if not isinstance(marker_text, str):
        raise ReviewInputError(
            "v6 marker must be a string",
            diagnostic="v6_malformed_marker",
        )
    line = marker_text.strip()
    match = _MARKER_RE.fullmatch(line)
    if match is None:
        raise ReviewInputError(
            "malformed v6 ledger HTML marker",
            diagnostic="v6_malformed_marker",
        )
    expected_sha = match.group("sha")
    raw_payload = match.group("payload")
    if "-->" in raw_payload:
        raise ReviewInputError(
            "v6 marker payload contains premature HTML comment terminator",
            diagnostic="v6_malformed_marker",
        )
    try:
        parsed = json.loads(raw_payload, parse_constant=_reject_json_constant)
    except ValueError as error:
        raise ReviewInputError(
            f"invalid JSON in v6 marker: {error}",
            diagnostic="v6_malformed_marker",
        ) from error
    if not isinstance(parsed, Mapping):
        raise ReviewInputError(
            "v6 marker payload must decode to a JSON object",
            diagnostic="v6_malformed_marker",
        )
    record = _record_from_mapping(parsed)
    actual_sha = canonical_sha256(record_to_dict(record))
    if not hmac.compare_digest(actual_sha, expected_sha):
        raise ReviewInputError(
            f"v6 marker SHA-256 mismatch: expected {expected_sha}, got {actual_sha}",
            diagnostic="v6_marker_digest_mismatch",
        )
    return record


def extract_v6_record_markers(
    body: str, *, strict: bool = True
) -> tuple[V6LedgerRecord, ...]:
    """Extract all v6 HTML comment markers from a GitHub comment/review body."""

    if not isinstance(body, str):
        if strict:
            raise ReviewInputError(
                "comment/review body must be a string",
                diagnostic="v6_malformed_marker",
            )
        return ()

    records: list[V6LedgerRecord] = []
    for raw_line in body.splitlines():
        stripped = raw_line.strip()
        if "<!-- reviewsensei:v6:" not in stripped:
            continue
        if strict:
            records.append(parse_v6_record_marker(stripped))
        else:
            try:
                records.append(parse_v6_record_marker(stripped))
            except ReviewInputError:
                continue
    return tuple(records)


def verify_app_authored_record(
    record_metadata: Mapping[str, Any],
    *,
    expected_app_id: int | None = None,
    expected_bot_login: str | None = None,
    expected_bot_user_id: int | None = None,
) -> bool:
    """Verify GitHub App / bot authorship provenance for a PR comment or review."""

    if not isinstance(record_metadata, Mapping):
        return False

    app_info = record_metadata.get("performed_via_github_app")
    has_verified_app = False
    if app_info is not None:
        if not isinstance(app_info, Mapping):
            return False
        app_id = app_info.get("id")
        if type(app_id) is not int or app_id <= 0:
            return False
        if expected_app_id is not None and app_id != expected_app_id:
            return False
        has_verified_app = True

    user_info = record_metadata.get("user")
    if user_info is None:
        user_info = record_metadata.get("author")
    if user_info is not None and not isinstance(user_info, Mapping):
        return False

    if isinstance(user_info, Mapping):
        user_type = user_info.get("type") or user_info.get("__typename")
        if user_type is not None and str(user_type).lower() != "bot":
            return False
        if expected_bot_user_id is not None:
            uid = user_info.get("id")
            if type(uid) is not int or uid != expected_bot_user_id:
                return False
        if expected_bot_login is not None:
            login = user_info.get("login")
            if not isinstance(login, str) or login != expected_bot_login:
                return False
        if not has_verified_app:
            has_explicit_bot_identity = (
                expected_bot_user_id is not None or expected_bot_login is not None
            )
            has_verified_app = (
                expected_app_id is None
                and has_explicit_bot_identity
                and str(user_type or "").lower() == "bot"
            )

    return has_verified_app


def require_app_authored_record(
    record_metadata: Mapping[str, Any],
    *,
    expected_app_id: int | None = None,
    expected_bot_login: str | None = None,
    expected_bot_user_id: int | None = None,
) -> None:
    """Fail closed unless ``record_metadata`` proves ReviewSensei App authorship."""

    if not verify_app_authored_record(
        record_metadata,
        expected_app_id=expected_app_id,
        expected_bot_login=expected_bot_login,
        expected_bot_user_id=expected_bot_user_id,
    ):
        raise ReviewInputError(
            "untrusted or non-App author on v6 ledger record",
            diagnostic="v6_untrusted_record_author",
        )


@dataclass(frozen=True)
class ImmutableReconciliationSummary:
    """Deterministic reconciliation result over immutable intents and receipts."""

    unreconciled_intents: tuple[WriteIntentRecord, ...]
    completed_operations: tuple[str, ...]
    matched_receipts: tuple[CompletionReceiptRecord, ...]
    orphan_receipts: tuple[CompletionReceiptRecord, ...]
    conflicting_records: tuple[WriteIntentRecord | CompletionReceiptRecord, ...]
    unique_intents: tuple[WriteIntentRecord, ...]
    unique_receipts: tuple[CompletionReceiptRecord, ...]


def reconcile_immutable_records(
    intents: Iterable[WriteIntentRecord],
    receipts: Iterable[CompletionReceiptRecord],
) -> ImmutableReconciliationSummary:
    """Reconcile immutable write intents and receipts without destructive overwrites.

    Deduplicates identically matching records by ``(record_id, operation_id,
    payload_sha256)`` and surfaces any conflicting reuse of a ``record_id`` or
    conflicting payload/effect for an ``operation_id`` in ``conflicting_records``.
    """

    intent_by_triple: dict[tuple[str, str, str], WriteIntentRecord] = {}
    intents_by_record_id: dict[str, WriteIntentRecord] = {}
    intents_by_op: dict[str, list[WriteIntentRecord]] = {}
    conflicts: list[WriteIntentRecord | CompletionReceiptRecord] = []

    for intent in intents:
        if not isinstance(intent, WriteIntentRecord):
            raise TypeError("intents must contain WriteIntentRecord instances")
        triple = (intent.record_id, intent.operation_id, intent.payload_sha256)
        existing_triple = intent_by_triple.get(triple)
        if existing_triple is not None:
            if existing_triple != intent:
                conflicts.extend((existing_triple, intent))
            continue
        existing_id = intents_by_record_id.get(intent.record_id)
        if existing_id is not None and existing_id != intent:
            conflicts.extend((existing_id, intent))
            continue
        intent_by_triple[triple] = intent
        intents_by_record_id[intent.record_id] = intent
        intents_by_op.setdefault(intent.operation_id, []).append(intent)

    for op_intents in intents_by_op.values():
        payload_hashes = {item.payload_sha256 for item in op_intents}
        if len(payload_hashes) > 1:
            conflicts.extend(op_intents)

    receipt_by_triple: dict[tuple[str, str, str], CompletionReceiptRecord] = {}
    receipts_by_record_id: dict[str, CompletionReceiptRecord] = {}
    receipts_by_op: dict[str, list[CompletionReceiptRecord]] = {}

    for receipt in receipts:
        if not isinstance(receipt, CompletionReceiptRecord):
            raise TypeError("receipts must contain CompletionReceiptRecord instances")
        triple = (receipt.record_id, receipt.operation_id, receipt.payload_sha256)
        existing_receipt_triple = receipt_by_triple.get(triple)
        if existing_receipt_triple is not None:
            if existing_receipt_triple != receipt:
                conflicts.extend((existing_receipt_triple, receipt))
            continue
        existing_receipt_id = receipts_by_record_id.get(receipt.record_id)
        if existing_receipt_id is not None and existing_receipt_id != receipt:
            conflicts.extend((existing_receipt_id, receipt))
            continue
        receipt_by_triple[triple] = receipt
        receipts_by_record_id[receipt.record_id] = receipt
        receipts_by_op.setdefault(receipt.operation_id, []).append(receipt)

    for op_receipts in receipts_by_op.values():
        effect_pairs = {
            (item.payload_sha256, str(item.github_effect_id)) for item in op_receipts
        }
        if len(effect_pairs) > 1:
            conflicts.extend(op_receipts)

    conflicting_set = set(conflicts)
    conflicting_ops = {item.operation_id for item in conflicts}

    sorted_intents = tuple(
        sorted(
            intent_by_triple.values(),
            key=lambda item: (item.operation_id, item.writer_epoch, item.record_id),
        )
    )
    sorted_receipts = tuple(
        sorted(
            receipt_by_triple.values(),
            key=lambda item: (item.operation_id, item.writer_epoch, item.record_id),
        )
    )

    receipt_by_op_payload: dict[tuple[str, str], CompletionReceiptRecord] = {}
    for receipt in sorted_receipts:
        if receipt.operation_id in conflicting_ops or receipt in conflicting_set:
            continue
        receipt_by_op_payload.setdefault(
            (receipt.operation_id, receipt.payload_sha256), receipt
        )

    unreconciled: list[WriteIntentRecord] = []
    matched_receipts: list[CompletionReceiptRecord] = []
    matched_keys: set[tuple[str, str]] = set()
    completed_ops: list[str] = []

    for intent in sorted_intents:
        if intent.operation_id in conflicting_ops or intent in conflicting_set:
            continue
        key = (intent.operation_id, intent.payload_sha256)
        matched = receipt_by_op_payload.get(key)
        if matched is not None:
            if key not in matched_keys:
                matched_keys.add(key)
                matched_receipts.append(matched)
                completed_ops.append(intent.operation_id)
        else:
            unreconciled.append(intent)

    orphan_receipts = tuple(
        receipt
        for receipt in sorted_receipts
        if receipt.operation_id not in conflicting_ops
        and receipt not in conflicting_set
        and (receipt.operation_id, receipt.payload_sha256) not in matched_keys
    )

    dedup_conflicts: list[WriteIntentRecord | CompletionReceiptRecord] = []
    seen_conflicts: set[WriteIntentRecord | CompletionReceiptRecord] = set()
    for item in conflicts:
        if item not in seen_conflicts:
            seen_conflicts.add(item)
            dedup_conflicts.append(item)

    return ImmutableReconciliationSummary(
        unreconciled_intents=tuple(unreconciled),
        completed_operations=tuple(sorted(set(completed_ops))),
        matched_receipts=tuple(matched_receipts),
        orphan_receipts=orphan_receipts,
        conflicting_records=tuple(dedup_conflicts),
        unique_intents=sorted_intents,
        unique_receipts=sorted_receipts,
    )
