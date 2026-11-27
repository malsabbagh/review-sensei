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
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, TypeAlias

from review_sensei.errors import ReviewInputError


class V6LedgerIntegrityError(ReviewInputError):
    """Raised when a v6 ledger marker, schema, field type, or digest is invalid."""

    error_category = "v6_ledger_integrity"


OWNER_SCHEMA = "reviewsensei:v6:owner:v1"
INTENT_SCHEMA = "reviewsensei:v6:intent:v1"
RECEIPT_SCHEMA = "reviewsensei:v6:receipt:v1"
NON_SUBMISSION_SCHEMA = "reviewsensei:v6:non-submission:v1"
HOLD_SCHEMA = "reviewsensei:v6:hold:v1"
UNRESOLVED_RESULT_SCHEMA = "reviewsensei:v6:unresolved:v1"
RESULT_PART_SCHEMA = "reviewsensei:v6:result-part:v1"
RESULT_MANIFEST_SCHEMA = "reviewsensei:v6:result-manifest:v1"
PROGRESS_HINT_SCHEMA = "reviewsensei:v6:progress:v1"

MAX_MARKER_ENCODED_BYTES = 65536

VALID_NON_SUBMISSION_PROOF_KINDS: frozenset[str] = frozenset(
    {
        "definitive_github_rejection",
        "pre_dispatch_terminal_abort",
    }
)

VALID_V6_RECORD_SCHEMAS: frozenset[str] = frozenset(
    {
        OWNER_SCHEMA,
        INTENT_SCHEMA,
        RECEIPT_SCHEMA,
        NON_SUBMISSION_SCHEMA,
        HOLD_SCHEMA,
        UNRESOLVED_RESULT_SCHEMA,
        RESULT_PART_SCHEMA,
        RESULT_MANIFEST_SCHEMA,
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
_SLOT_KEY_RE = re.compile(
    r"^v6-pr:[a-z0-9.-]+:[1-9][0-9]*:[1-9][0-9]*:[1-9][0-9]*:[1-9][0-9]*$"
)
_POSITIVE_DECIMAL_RE = re.compile(r"^[1-9][0-9]*$")
_ISO8601_UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$"
)

NEVER_KEEP_LEDGER_FIELDS: frozenset[str] = frozenset(
    {
        "github_app_private_key",
        "reviewsensei_signing_key",
        "raw_aes_key",
        "oidc_token",
        "plaintext_installation_token",
        "encrypted_escrow",
        "iv_b64",
        "ciphertext_b64",
        "workflow_github_token",
        "provider_api_key",
        "ollama_api_key",
        "openrouter_api_key",
        "openai_api_key",
        "raw_github_error_body",
    }
)

_SECRET_VALUE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("github_token", re.compile(r"(?<![A-Za-z0-9_])(?:ghs|ghp)_[A-Za-z0-9_-]{6,}")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{6,}")),
    (
        "jwt_token",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{4,}\.eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\b"
        ),
    ),
    ("pem_private_key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
)

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


def _require_slot_key(value: Any, *, name: str = "slot_key") -> str:
    if type(value) is not str or _SLOT_KEY_RE.fullmatch(value) is None:
        raise ReviewInputError(
            f"{name} must match canonical v6 slot_key pattern, got {value!r}",
            diagnostic="v6_invalid_ledger_field",
        )
    return value


def _require_positive_decimal_str(value: Any, *, name: str) -> str:
    if type(value) is not str or _POSITIVE_DECIMAL_RE.fullmatch(value) is None:
        raise ReviewInputError(
            f"{name} must be a positive decimal string, got {value!r}",
            diagnostic="v6_invalid_ledger_field",
        )
    return value


def _require_iso8601_utc(value: Any, *, name: str) -> str:
    if type(value) is not str or _ISO8601_UTC_RE.fullmatch(value) is None:
        raise ReviewInputError(
            f"{name} must be an ISO-8601 UTC timestamp, got {value!r}",
            diagnostic="v6_invalid_ledger_field",
        )
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ReviewInputError(
            f"{name} is not a valid ISO-8601 UTC timestamp: {error}",
            diagnostic="v6_invalid_ledger_field",
        ) from error
    return value


def redact_secrets(text: str) -> str:
    """Redact GitHub tokens, Bearer headers, raw JWTs, and PEM private keys."""

    if not isinstance(text, str):
        return ""
    sanitized = text
    for _, pattern in _SECRET_VALUE_PATTERNS:
        sanitized = pattern.sub("[REDACTED]", sanitized)
    return sanitized


def assert_no_secrets_in_text(text: str, *, context: str = "payload") -> None:
    """Fail closed if ``text`` matches any banned secret/token pattern."""

    if not isinstance(text, str):
        return
    for label, pattern in _SECRET_VALUE_PATTERNS:
        if pattern.search(text) is not None:
            raise V6LedgerIntegrityError(
                f"v6 {context} contains forbidden secret pattern ({label})",
                diagnostic="v6_secret_in_ledger",
            )


def assert_no_secrets_in_mapping(value: Any, *, path: str = "payload") -> None:
    """Recursively reject banned secret field names or token patterns."""

    if isinstance(value, str):
        assert_no_secrets_in_text(value, context=path)
        return
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key_str = str(raw_key)
            if key_str in NEVER_KEEP_LEDGER_FIELDS:
                raise V6LedgerIntegrityError(
                    f"forbidden secret field {key_str!r} in v6 ledger {path}",
                    diagnostic="v6_secret_in_ledger",
                )
            assert_no_secrets_in_mapping(item, path=f"{path}.{key_str}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            assert_no_secrets_in_mapping(item, path=f"{path}[{index}]")


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
    generation: int | None = None
    target_location: str | None = None
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
        if self.generation is not None:
            _require_strict_int(self.generation, name="generation", minimum=1)
        if self.target_location is not None:
            if (
                type(self.target_location) is not str
                or not self.target_location.strip()
                or "\n" in self.target_location
                or "\r" in self.target_location
            ):
                raise ReviewInputError(
                    "target_location must be a non-empty single-line string",
                    diagnostic="v6_invalid_ledger_field",
                )


@dataclass(frozen=True)
class NonSubmissionProofRecord:
    """Affirmative App-authored proof that an intent was not submitted or rejected."""

    record_id: str
    slot_key: str
    generation: int
    operation_id: str
    pr_head_sha: str
    proof_kind: str
    payload_sha256: str
    recorded_at: str
    http_status: int | None = None
    diagnostic: str | None = None
    schema: str = NON_SUBMISSION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != NON_SUBMISSION_SCHEMA:
            raise ReviewInputError(
                f"invalid NonSubmissionProofRecord schema: {self.schema!r}",
                diagnostic="v6_unknown_schema",
            )
        _require_non_empty_str(self.record_id, name="record_id")
        _require_slot_key(self.slot_key, name="slot_key")
        _require_strict_int(self.generation, name="generation", minimum=1)
        _require_non_empty_str(self.operation_id, name="operation_id")
        _require_sha1_40(self.pr_head_sha, name="pr_head_sha")
        if (
            type(self.proof_kind) is not str
            or self.proof_kind not in VALID_NON_SUBMISSION_PROOF_KINDS
        ):
            raise ReviewInputError(
                f"invalid NonSubmissionProofRecord proof_kind: {self.proof_kind!r}",
                diagnostic="v6_invalid_ledger_field",
            )
        _require_sha256_64(self.payload_sha256, name="payload_sha256")
        _require_iso8601_utc(self.recorded_at, name="recorded_at")
        if self.http_status is not None:
            _require_strict_int(self.http_status, name="http_status", minimum=100)
        if self.diagnostic is not None:
            _require_non_empty_str(self.diagnostic, name="diagnostic")
            assert_no_secrets_in_text(self.diagnostic, context="diagnostic")


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
class ResultPartRecord:
    """Lossless partitioned chunk of an oversized PR-resident review result."""

    record_id: str
    operation_id: str
    pr_head_sha: str
    payload_sha256: str
    part_index: int
    part_count: int
    part_sha256: str
    part_payload: Mapping[str, Any]
    schema: str = RESULT_PART_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != RESULT_PART_SCHEMA:
            raise ReviewInputError(
                f"invalid ResultPartRecord schema: {self.schema!r}",
                diagnostic="v6_unknown_schema",
            )
        _require_non_empty_str(self.record_id, name="record_id")
        _require_non_empty_str(self.operation_id, name="operation_id")
        _require_sha1_40(self.pr_head_sha, name="pr_head_sha")
        _require_sha256_64(self.payload_sha256, name="payload_sha256")
        idx = _require_strict_int(self.part_index, name="part_index", minimum=0)
        count = _require_strict_int(self.part_count, name="part_count", minimum=1)
        if idx >= count:
            raise ReviewInputError(
                f"part_index {idx} must be < part_count {count}",
                diagnostic="v6_invalid_ledger_field",
            )
        _require_sha256_64(self.part_sha256, name="part_sha256")
        if not isinstance(self.part_payload, Mapping):
            raise ReviewInputError(
                "part_payload must be a mapping",
                diagnostic="v6_invalid_ledger_field",
            )
        frozen_part = MappingProxyType(_to_json_compatible(dict(self.part_payload)))
        object.__setattr__(self, "part_payload", frozen_part)


@dataclass(frozen=True)
class ResultManifestRecord:
    """Manifest binding partitioned ResultPartRecord comments into an exact payload."""

    record_id: str
    operation_id: str
    pr_head_sha: str
    payload_sha256: str
    part_record_ids: tuple[str, ...]
    part_comment_ids: tuple[int, ...]
    manifest: Mapping[str, Any]
    required_evidence_digests: tuple[str, ...] = ()
    artifact_cache_sha256: str | None = None
    schema: str = RESULT_MANIFEST_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != RESULT_MANIFEST_SCHEMA:
            raise ReviewInputError(
                f"invalid ResultManifestRecord schema: {self.schema!r}",
                diagnostic="v6_unknown_schema",
            )
        _require_non_empty_str(self.record_id, name="record_id")
        _require_non_empty_str(self.operation_id, name="operation_id")
        _require_sha1_40(self.pr_head_sha, name="pr_head_sha")
        _require_sha256_64(self.payload_sha256, name="payload_sha256")
        if (
            not isinstance(self.part_record_ids, Sequence)
            or isinstance(self.part_record_ids, (str, bytes, bytearray))
            or not self.part_record_ids
        ):
            raise ReviewInputError(
                "part_record_ids must be a non-empty sequence of record IDs",
                diagnostic="v6_invalid_ledger_field",
            )
        validated_part_ids = tuple(
            _require_non_empty_str(pid, name="part_record_id")
            for pid in self.part_record_ids
        )
        if len(set(validated_part_ids)) != len(validated_part_ids):
            raise ReviewInputError(
                "part_record_ids must be unique",
                diagnostic="v6_invalid_ledger_field",
            )
        object.__setattr__(self, "part_record_ids", validated_part_ids)
        if (
            not isinstance(self.part_comment_ids, Sequence)
            or isinstance(self.part_comment_ids, (str, bytes, bytearray))
            or len(self.part_comment_ids) != len(validated_part_ids)
        ):
            raise ReviewInputError(
                "part_comment_ids must match length of part_record_ids",
                diagnostic="v6_invalid_ledger_field",
            )
        validated_cids = tuple(
            _require_strict_int(cid, name="part_comment_id", minimum=1)
            for cid in self.part_comment_ids
        )
        object.__setattr__(self, "part_comment_ids", validated_cids)
        if not isinstance(self.manifest, Mapping):
            raise ReviewInputError(
                "manifest must be a mapping",
                diagnostic="v6_invalid_ledger_field",
            )
        frozen_manifest = MappingProxyType(_to_json_compatible(dict(self.manifest)))
        object.__setattr__(self, "manifest", frozen_manifest)
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


@dataclass(frozen=True)
class OwnerMarkerRecord:
    """Coordinator-attested PR-resident ownership anchor (reviewsensei:v6:owner:v1)."""

    record_id: str
    slot_key: str
    generation: int
    lease_epoch: int
    reservation_id: str
    operation_id: str
    run_id: str
    run_attempt: int
    pr_head_sha: str
    workflow_sha: str
    acquired_at: str
    expires_at: str
    coordinator_attestation_mac: str
    schema: str = OWNER_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != OWNER_SCHEMA:
            raise ReviewInputError(
                f"invalid OwnerMarkerRecord schema: {self.schema!r}",
                diagnostic="v6_unknown_schema",
            )
        _require_non_empty_str(self.record_id, name="record_id")
        _require_slot_key(self.slot_key, name="slot_key")
        _require_strict_int(self.generation, name="generation", minimum=1)
        _require_strict_int(self.lease_epoch, name="lease_epoch", minimum=1)
        _require_non_empty_str(self.reservation_id, name="reservation_id")
        _require_non_empty_str(self.operation_id, name="operation_id")
        _require_positive_decimal_str(self.run_id, name="run_id")
        _require_strict_int(self.run_attempt, name="run_attempt", minimum=1)
        _require_sha1_40(self.pr_head_sha, name="pr_head_sha")
        _require_sha1_40(self.workflow_sha, name="workflow_sha")
        _require_iso8601_utc(self.acquired_at, name="acquired_at")
        _require_iso8601_utc(self.expires_at, name="expires_at")
        _require_sha256_64(
            self.coordinator_attestation_mac, name="coordinator_attestation_mac"
        )


@dataclass(frozen=True)
class DurableHoldRecord:
    """Explicit PR-resident durable hold marker (reviewsensei:v6:hold:v1)."""

    record_id: str
    slot_key: str
    generation: int
    lease_epoch: int
    operation_id: str
    pr_head_sha: str
    reason_code: str
    missing_evidence: tuple[str, ...]
    held_at: str
    resolved_by_record_id: str | None = None
    schema: str = HOLD_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != HOLD_SCHEMA:
            raise ReviewInputError(
                f"invalid DurableHoldRecord schema: {self.schema!r}",
                diagnostic="v6_unknown_schema",
            )
        _require_non_empty_str(self.record_id, name="record_id")
        _require_slot_key(self.slot_key, name="slot_key")
        _require_strict_int(self.generation, name="generation", minimum=1)
        _require_strict_int(self.lease_epoch, name="lease_epoch", minimum=0)
        _require_non_empty_str(self.operation_id, name="operation_id")
        _require_sha1_40(self.pr_head_sha, name="pr_head_sha")
        _require_non_empty_str(self.reason_code, name="reason_code")
        if not isinstance(self.missing_evidence, Sequence) or isinstance(
            self.missing_evidence, (str, bytes, bytearray)
        ):
            raise ReviewInputError(
                "missing_evidence must be a sequence of strings",
                diagnostic="v6_invalid_ledger_field",
            )
        validated_missing = tuple(
            _require_non_empty_str(item, name="missing_evidence_item")
            for item in self.missing_evidence
        )
        object.__setattr__(self, "missing_evidence", validated_missing)
        _require_iso8601_utc(self.held_at, name="held_at")
        if self.resolved_by_record_id is not None:
            _require_non_empty_str(
                self.resolved_by_record_id, name="resolved_by_record_id"
            )


V6LedgerRecord: TypeAlias = (
    OwnerMarkerRecord
    | WriteIntentRecord
    | CompletionReceiptRecord
    | NonSubmissionProofRecord
    | DurableHoldRecord
    | UnresolvedResultRecord
    | ResultPartRecord
    | ResultManifestRecord
    | AdvisoryProgressHint
)


def record_to_dict(record: V6LedgerRecord) -> dict[str, Any]:
    """Convert a v6 ledger record dataclass into a canonical JSON-compatible dict."""

    if isinstance(record, OwnerMarkerRecord):
        return {
            "schema": record.schema,
            "record_id": record.record_id,
            "slot_key": record.slot_key,
            "generation": record.generation,
            "lease_epoch": record.lease_epoch,
            "reservation_id": record.reservation_id,
            "operation_id": record.operation_id,
            "run_id": record.run_id,
            "run_attempt": record.run_attempt,
            "pr_head_sha": record.pr_head_sha,
            "workflow_sha": record.workflow_sha,
            "acquired_at": record.acquired_at,
            "expires_at": record.expires_at,
            "coordinator_attestation_mac": record.coordinator_attestation_mac,
        }
    if isinstance(record, WriteIntentRecord):
        intent_dict: dict[str, Any] = {
            "schema": record.schema,
            "record_id": record.record_id,
            "operation_id": record.operation_id,
            "writer_epoch": record.writer_epoch,
            "pr_head_sha": record.pr_head_sha,
            "effect_kind": record.effect_kind,
            "payload_sha256": record.payload_sha256,
            "target_digest": record.target_digest,
        }
        if record.generation is not None:
            intent_dict["generation"] = record.generation
        if record.target_location is not None:
            intent_dict["target_location"] = record.target_location
        return intent_dict
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
    if isinstance(record, NonSubmissionProofRecord):
        proof_dict: dict[str, Any] = {
            "schema": record.schema,
            "record_id": record.record_id,
            "slot_key": record.slot_key,
            "generation": record.generation,
            "operation_id": record.operation_id,
            "pr_head_sha": record.pr_head_sha,
            "proof_kind": record.proof_kind,
            "payload_sha256": record.payload_sha256,
            "recorded_at": record.recorded_at,
        }
        if record.http_status is not None:
            proof_dict["http_status"] = record.http_status
        if record.diagnostic is not None:
            proof_dict["diagnostic"] = record.diagnostic
        return proof_dict
    if isinstance(record, DurableHoldRecord):
        return {
            "schema": record.schema,
            "record_id": record.record_id,
            "slot_key": record.slot_key,
            "generation": record.generation,
            "lease_epoch": record.lease_epoch,
            "operation_id": record.operation_id,
            "pr_head_sha": record.pr_head_sha,
            "reason_code": record.reason_code,
            "missing_evidence": list(record.missing_evidence),
            "held_at": record.held_at,
            "resolved_by_record_id": record.resolved_by_record_id,
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
    if isinstance(record, ResultPartRecord):
        return {
            "schema": record.schema,
            "record_id": record.record_id,
            "operation_id": record.operation_id,
            "pr_head_sha": record.pr_head_sha,
            "payload_sha256": record.payload_sha256,
            "part_index": record.part_index,
            "part_count": record.part_count,
            "part_sha256": record.part_sha256,
            "part_payload": _to_json_compatible(record.part_payload),
        }
    if isinstance(record, ResultManifestRecord):
        return {
            "schema": record.schema,
            "record_id": record.record_id,
            "operation_id": record.operation_id,
            "pr_head_sha": record.pr_head_sha,
            "payload_sha256": record.payload_sha256,
            "part_record_ids": list(record.part_record_ids),
            "part_comment_ids": list(record.part_comment_ids),
            "manifest": _to_json_compatible(record.manifest),
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


def _require_mapping_opt_int(payload: Mapping[str, Any], key: str) -> int | None:
    value = payload[key]
    if value is not None and type(value) is not int:
        raise V6LedgerIntegrityError(
            f"field {key!r} must be strict int or None, got {type(value).__name__}",
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
        assert_no_secrets_in_mapping(payload)

        if schema == OWNER_SCHEMA:
            expected_keys = {
                "schema",
                "record_id",
                "slot_key",
                "generation",
                "lease_epoch",
                "reservation_id",
                "operation_id",
                "run_id",
                "run_attempt",
                "pr_head_sha",
                "workflow_sha",
                "acquired_at",
                "expires_at",
                "coordinator_attestation_mac",
            }
            if set(payload.keys()) != expected_keys:
                raise V6LedgerIntegrityError(
                    "OwnerMarkerRecord has unexpected or missing keys",
                    diagnostic="v6_malformed_marker",
                )
            return OwnerMarkerRecord(
                schema=schema,
                record_id=_require_mapping_str(payload, "record_id"),
                slot_key=_require_mapping_str(payload, "slot_key"),
                generation=_require_mapping_int(payload, "generation"),
                lease_epoch=_require_mapping_int(payload, "lease_epoch"),
                reservation_id=_require_mapping_str(payload, "reservation_id"),
                operation_id=_require_mapping_str(payload, "operation_id"),
                run_id=_require_mapping_str(payload, "run_id"),
                run_attempt=_require_mapping_int(payload, "run_attempt"),
                pr_head_sha=_require_mapping_str(payload, "pr_head_sha"),
                workflow_sha=_require_mapping_str(payload, "workflow_sha"),
                acquired_at=_require_mapping_str(payload, "acquired_at"),
                expires_at=_require_mapping_str(payload, "expires_at"),
                coordinator_attestation_mac=_require_mapping_str(
                    payload, "coordinator_attestation_mac"
                ),
            )

        if schema == HOLD_SCHEMA:
            expected_keys = {
                "schema",
                "record_id",
                "slot_key",
                "generation",
                "lease_epoch",
                "operation_id",
                "pr_head_sha",
                "reason_code",
                "missing_evidence",
                "held_at",
                "resolved_by_record_id",
            }
            if set(payload.keys()) != expected_keys:
                raise V6LedgerIntegrityError(
                    "DurableHoldRecord has unexpected or missing keys",
                    diagnostic="v6_malformed_marker",
                )
            raw_missing = payload["missing_evidence"]
            if not isinstance(raw_missing, (list, tuple)) or any(
                type(item) is not str for item in raw_missing
            ):
                raise V6LedgerIntegrityError(
                    "field 'missing_evidence' must be a list or tuple of strings",
                    diagnostic="v6_malformed_marker",
                )
            return DurableHoldRecord(
                schema=schema,
                record_id=_require_mapping_str(payload, "record_id"),
                slot_key=_require_mapping_str(payload, "slot_key"),
                generation=_require_mapping_int(payload, "generation"),
                lease_epoch=_require_mapping_int(payload, "lease_epoch"),
                operation_id=_require_mapping_str(payload, "operation_id"),
                pr_head_sha=_require_mapping_str(payload, "pr_head_sha"),
                reason_code=_require_mapping_str(payload, "reason_code"),
                missing_evidence=tuple(raw_missing),
                held_at=_require_mapping_str(payload, "held_at"),
                resolved_by_record_id=_require_mapping_opt_str(
                    payload, "resolved_by_record_id"
                ),
            )

        if schema == INTENT_SCHEMA:
            required_keys = {
                "schema",
                "record_id",
                "operation_id",
                "writer_epoch",
                "pr_head_sha",
                "effect_kind",
                "payload_sha256",
                "target_digest",
            }
            optional_keys = {"generation", "target_location"}
            keys = set(payload.keys())
            if not required_keys.issubset(keys) or not keys.issubset(
                required_keys | optional_keys
            ):
                raise V6LedgerIntegrityError(
                    "WriteIntentRecord has unexpected or missing keys",
                    diagnostic="v6_malformed_marker",
                )
            generation = (
                _require_mapping_opt_int(payload, "generation")
                if "generation" in payload
                else None
            )
            target_location = (
                _require_mapping_opt_str(payload, "target_location")
                if "target_location" in payload
                else None
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
                generation=generation,
                target_location=target_location,
            )

        if schema == NON_SUBMISSION_SCHEMA:
            required_keys = {
                "schema",
                "record_id",
                "slot_key",
                "generation",
                "operation_id",
                "pr_head_sha",
                "proof_kind",
                "payload_sha256",
                "recorded_at",
            }
            optional_keys = {"http_status", "diagnostic"}
            keys = set(payload.keys())
            if not required_keys.issubset(keys) or not keys.issubset(
                required_keys | optional_keys
            ):
                raise V6LedgerIntegrityError(
                    "NonSubmissionProofRecord has unexpected or missing keys",
                    diagnostic="v6_malformed_marker",
                )
            http_status = (
                _require_mapping_opt_int(payload, "http_status")
                if "http_status" in payload
                else None
            )
            diagnostic = (
                _require_mapping_opt_str(payload, "diagnostic")
                if "diagnostic" in payload
                else None
            )
            return NonSubmissionProofRecord(
                schema=schema,
                record_id=_require_mapping_str(payload, "record_id"),
                slot_key=_require_mapping_str(payload, "slot_key"),
                generation=_require_mapping_int(payload, "generation"),
                operation_id=_require_mapping_str(payload, "operation_id"),
                pr_head_sha=_require_mapping_str(payload, "pr_head_sha"),
                proof_kind=_require_mapping_str(payload, "proof_kind"),
                payload_sha256=_require_mapping_str(payload, "payload_sha256"),
                recorded_at=_require_mapping_str(payload, "recorded_at"),
                http_status=http_status,
                diagnostic=diagnostic,
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

        if schema == RESULT_PART_SCHEMA:
            expected_keys = {
                "schema",
                "record_id",
                "operation_id",
                "pr_head_sha",
                "payload_sha256",
                "part_index",
                "part_count",
                "part_sha256",
                "part_payload",
            }
            if set(payload.keys()) != expected_keys:
                raise V6LedgerIntegrityError(
                    "ResultPartRecord has unexpected or missing keys",
                    diagnostic="v6_malformed_marker",
                )
            raw_part = payload["part_payload"]
            if not isinstance(raw_part, Mapping):
                raise V6LedgerIntegrityError(
                    "field 'part_payload' must be a mapping",
                    diagnostic="v6_malformed_marker",
                )
            return ResultPartRecord(
                schema=schema,
                record_id=_require_mapping_str(payload, "record_id"),
                operation_id=_require_mapping_str(payload, "operation_id"),
                pr_head_sha=_require_mapping_str(payload, "pr_head_sha"),
                payload_sha256=_require_mapping_str(payload, "payload_sha256"),
                part_index=_require_mapping_int(payload, "part_index"),
                part_count=_require_mapping_int(payload, "part_count"),
                part_sha256=_require_mapping_str(payload, "part_sha256"),
                part_payload=raw_part,
            )

        if schema == RESULT_MANIFEST_SCHEMA:
            expected_keys = {
                "schema",
                "record_id",
                "operation_id",
                "pr_head_sha",
                "payload_sha256",
                "part_record_ids",
                "part_comment_ids",
                "manifest",
                "required_evidence_digests",
                "artifact_cache_sha256",
            }
            if set(payload.keys()) != expected_keys:
                raise V6LedgerIntegrityError(
                    "ResultManifestRecord has unexpected or missing keys",
                    diagnostic="v6_malformed_marker",
                )
            raw_part_ids = payload["part_record_ids"]
            if not isinstance(raw_part_ids, (list, tuple)) or any(
                type(item) is not str for item in raw_part_ids
            ):
                raise V6LedgerIntegrityError(
                    "field 'part_record_ids' must be a list of strings",
                    diagnostic="v6_malformed_marker",
                )
            raw_cids = payload["part_comment_ids"]
            if not isinstance(raw_cids, (list, tuple)) or any(
                type(item) is not int for item in raw_cids
            ):
                raise V6LedgerIntegrityError(
                    "field 'part_comment_ids' must be a list of integers",
                    diagnostic="v6_malformed_marker",
                )
            raw_manifest = payload["manifest"]
            if not isinstance(raw_manifest, Mapping):
                raise V6LedgerIntegrityError(
                    "field 'manifest' must be a mapping",
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
            return ResultManifestRecord(
                schema=schema,
                record_id=_require_mapping_str(payload, "record_id"),
                operation_id=_require_mapping_str(payload, "operation_id"),
                pr_head_sha=_require_mapping_str(payload, "pr_head_sha"),
                payload_sha256=_require_mapping_str(payload, "payload_sha256"),
                part_record_ids=tuple(raw_part_ids),
                part_comment_ids=tuple(raw_cids),
                manifest=raw_manifest,
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
    assert_no_secrets_in_mapping(payload_dict)
    digest = canonical_sha256(payload_dict)
    raw_json = canonical_json_dumps(payload_dict)
    # Escape angle brackets so embedded strings cannot close or open HTML comments.
    safe_json = raw_json.replace("<", "\\u003c").replace(">", "\\u003e")
    if "-->" in safe_json or "<!--" in safe_json:
        raise V6LedgerIntegrityError(
            "escaped marker payload still contains forbidden HTML comment delimiter",
            diagnostic="v6_malformed_marker",
        )
    marker = f"{_MARKER_PREFIX}sha256={digest} payload={safe_json} -->"
    if len(marker.encode("utf-8")) > MAX_MARKER_ENCODED_BYTES:
        raise V6LedgerIntegrityError(
            f"v6 marker exceeds maximum encoded size ({MAX_MARKER_ENCODED_BYTES} bytes)",
            diagnostic="v6_marker_too_large",
        )
    return marker


def _reject_json_constant(constant: str) -> Any:
    raise V6LedgerIntegrityError(
        f"invalid JSON constant {constant!r} in v6 marker",
        diagnostic="v6_malformed_marker",
    )


def parse_v6_record_marker(marker_text: str) -> V6LedgerRecord:
    """Parse and SHA-256 verify a single v6 HTML comment marker (fail-closed)."""

    if not isinstance(marker_text, str):
        raise V6LedgerIntegrityError(
            "v6 marker must be a string",
            diagnostic="v6_malformed_marker",
        )
    line = marker_text.strip()
    if len(line.encode("utf-8")) > MAX_MARKER_ENCODED_BYTES:
        raise V6LedgerIntegrityError(
            f"v6 marker exceeds maximum encoded size ({MAX_MARKER_ENCODED_BYTES} bytes)",
            diagnostic="v6_marker_too_large",
        )
    match = _MARKER_RE.fullmatch(line)
    if match is None:
        raise V6LedgerIntegrityError(
            "malformed v6 ledger HTML marker",
            diagnostic="v6_malformed_marker",
        )
    expected_sha = match.group("sha")
    raw_payload = match.group("payload")
    if "-->" in raw_payload or "<!--" in raw_payload:
        raise V6LedgerIntegrityError(
            "v6 marker payload contains forbidden HTML comment delimiter",
            diagnostic="v6_malformed_marker",
        )
    assert_no_secrets_in_text(raw_payload, context="marker_payload")
    try:
        parsed = json.loads(raw_payload, parse_constant=_reject_json_constant)
    except ValueError as error:
        raise V6LedgerIntegrityError(
            f"invalid JSON in v6 marker: {error}",
            diagnostic="v6_malformed_marker",
        ) from error
    if not isinstance(parsed, Mapping):
        raise V6LedgerIntegrityError(
            "v6 marker payload must decode to a JSON object",
            diagnostic="v6_malformed_marker",
        )
    record = _record_from_mapping(parsed)
    actual_sha = canonical_sha256(record_to_dict(record))
    if not hmac.compare_digest(actual_sha, expected_sha):
        raise V6LedgerIntegrityError(
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


def parse_v6_slot_key(slot_key: str) -> tuple[str, int, int, int, int]:
    """Parse and validate a canonical v6 slot key into its five components."""

    _require_slot_key(slot_key, name="slot_key")
    _, host, app_id, installation_id, base_repo_id, pr_number = slot_key.split(":")
    return (
        host,
        int(app_id),
        int(installation_id),
        int(base_repo_id),
        int(pr_number),
    )


def validate_owner_lineage(
    records: Iterable[Any],
    *,
    slot_key: str,
    repository_id: int,
    pull_request: int,
    app_login: str,
    mac_verifier: Callable[[OwnerMarkerRecord], bool] | None = None,
) -> tuple[OwnerMarkerRecord, ...]:
    """Validate App-authored PR owner marker lineage, MACs, and monotonicity.

    Accepts comment mappings, ``PaginatedRecord`` instances wrapping comment
    mappings, or ``(OwnerMarkerRecord, metadata)`` pairs. Fails closed with
    ``V6LedgerIntegrityError`` on spoofed/untrusted authors, malformed owner
    markers, invalid MACs, slot/repo/PR mismatches, competing owner records,
    or broken/non-monotonic predecessor lineage.
    """

    try:
        _, _, _, slot_repo_id, slot_pr = parse_v6_slot_key(slot_key)
    except ReviewInputError as error:
        raise V6LedgerIntegrityError(
            str(error), diagnostic="v6_invalid_slot_key"
        ) from error
    if type(repository_id) is not int or repository_id != slot_repo_id:
        raise V6LedgerIntegrityError(
            f"slot_key repo_id {slot_repo_id} != repository_id {repository_id!r}",
            diagnostic="v6_owner_slot_mismatch",
        )
    if type(pull_request) is not int or pull_request != slot_pr:
        raise V6LedgerIntegrityError(
            f"slot_key pull_request {slot_pr} != pull_request {pull_request!r}",
            diagnostic="v6_owner_slot_mismatch",
        )
    if type(app_login) is not str or not app_login.strip():
        raise V6LedgerIntegrityError(
            "app_login must be a non-empty string for owner lineage verification",
            diagnostic="v6_untrusted_owner_author",
        )

    owners: list[OwnerMarkerRecord] = []
    for item in records:
        if isinstance(item, OwnerMarkerRecord):
            raise V6LedgerIntegrityError(
                "bare OwnerMarkerRecord lacks App authorship metadata",
                diagnostic="v6_untrusted_owner_author",
            )
        if (
            isinstance(item, tuple)
            and len(item) == 2
            and isinstance(item[0], OwnerMarkerRecord)
            and isinstance(item[1], Mapping)
        ):
            rec, meta = item
            if not verify_app_authored_record(meta, expected_bot_login=app_login):
                raise V6LedgerIntegrityError(
                    f"owner marker {rec.record_id!r} failed App authorship check",
                    diagnostic="v6_untrusted_owner_author",
                )
            owners.append(rec)
            continue

        payload = getattr(item, "payload", item)
        if not isinstance(payload, Mapping):
            raise V6LedgerIntegrityError(
                f"unsupported record item type in owner lineage: {type(item).__name__}",
                diagnostic="v6_malformed_marker",
            )
        body = payload.get("body")
        if not isinstance(body, str) or "<!-- reviewsensei:v6:" not in body:
            continue
        is_app = verify_app_authored_record(payload, expected_bot_login=app_login)
        if not is_app:
            if OWNER_SCHEMA in body or "reviewsensei:v6:owner" in body:
                raise V6LedgerIntegrityError(
                    "untrusted or non-App author on v6 owner marker",
                    diagnostic="v6_untrusted_owner_author",
                )
            continue
        try:
            parsed_records = extract_v6_record_markers(body, strict=True)
        except ReviewInputError as error:
            raise V6LedgerIntegrityError(
                f"malformed App-authored v6 ledger marker: {error}",
                diagnostic="v6_malformed_marker",
            ) from error
        for rec in parsed_records:
            if isinstance(rec, OwnerMarkerRecord):
                owners.append(rec)

    seen_record_ids: set[str] = set()
    seen_reservation_ids: set[str] = set()
    seen_generations: dict[int, OwnerMarkerRecord] = {}
    prev: OwnerMarkerRecord | None = None

    for owner in owners:
        if owner.slot_key != slot_key:
            raise V6LedgerIntegrityError(
                f"owner marker slot_key {owner.slot_key!r} != expected {slot_key!r}",
                diagnostic="v6_owner_slot_mismatch",
            )
        if _SHA256_64_RE.fullmatch(owner.coordinator_attestation_mac) is None:
            raise V6LedgerIntegrityError(
                f"owner marker {owner.record_id!r} has invalid coordinator_attestation_mac",
                diagnostic="v6_invalid_owner_attestation",
            )
        if mac_verifier is not None and not mac_verifier(owner):
            raise V6LedgerIntegrityError(
                f"owner marker {owner.record_id!r} failed MAC verification",
                diagnostic="v6_invalid_owner_attestation",
            )
        if owner.generation in seen_generations:
            raise V6LedgerIntegrityError(
                f"competing owner markers detected for generation {owner.generation}",
                diagnostic="v6_competing_owner_marker",
            )
        if owner.record_id in seen_record_ids:
            raise V6LedgerIntegrityError(
                f"duplicate owner record_id {owner.record_id!r} in lineage",
                diagnostic="v6_competing_owner_marker",
            )
        if owner.reservation_id in seen_reservation_ids:
            raise V6LedgerIntegrityError(
                f"reused reservation_id {owner.reservation_id!r} in owner lineage",
                diagnostic="v6_competing_owner_marker",
            )
        if prev is None:
            if owner.generation != 1:
                raise V6LedgerIntegrityError(
                    f"owner lineage missing predecessor: first observed generation is {owner.generation}",
                    diagnostic="v6_broken_owner_lineage",
                )
        else:
            if owner.generation < prev.generation:
                raise V6LedgerIntegrityError(
                    f"non-monotonic owner generation {owner.generation} after {prev.generation}",
                    diagnostic="v6_non_monotonic_owner_lineage",
                )
            if owner.generation != prev.generation + 1:
                raise V6LedgerIntegrityError(
                    f"broken owner predecessor linkage: generation {owner.generation} after {prev.generation}",
                    diagnostic="v6_broken_owner_lineage",
                )
            if owner.lease_epoch < prev.lease_epoch:
                raise V6LedgerIntegrityError(
                    f"non-monotonic owner lease_epoch {owner.lease_epoch} after {prev.lease_epoch}",
                    diagnostic="v6_non_monotonic_owner_lineage",
                )
        seen_generations[owner.generation] = owner
        seen_record_ids.add(owner.record_id)
        seen_reservation_ids.add(owner.reservation_id)
        prev = owner

    return tuple(owners)
