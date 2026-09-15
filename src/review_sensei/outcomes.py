"""Bounded run outcomes and publication-only recovery artifacts.

The review engine intentionally keeps this module provider-neutral.  It is a
small wire contract for callers that need to distinguish a skipped run from a
partial or failed run without retaining prompts, responses, or source text.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping

from .errors import ReviewInputError
from .schemas import validate_public_document
from .validation import DEFAULT_REVIEW_LIMITS

RUN_STATUSES = frozenset(
    {
        "reviewed",
        "partial",
        "skipped_stale",
        "skipped_policy",
        "provider_failed",
        "budget_exhausted",
        "publication_failed",
        "already_published",
    }
)
_SNAPSHOT = re.compile(r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")
MAX_RECOVERY_RESULT_BYTES = DEFAULT_REVIEW_LIMITS.max_result_bytes
MAX_RECOVERY_RESULT_DEPTH = 32
MAX_STAGE_SUMMARY_ENTRIES = 64
MAX_STAGE_SUMMARY_KEY_LENGTH = 128
MAX_STAGE_SUMMARY_VALUE_LENGTH = 128


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parse_aware_datetime(value: str, *, label: str) -> datetime:
    normalized = value.replace("Z", "+00:00").replace("z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ReviewInputError(f"{label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReviewInputError(f"{label} must include a timezone")
    return parsed


def _validate_stage_summary(stage_summary: Mapping[str, str]) -> None:
    if not isinstance(stage_summary, Mapping):
        raise ReviewInputError("run outcome stage_summary must be a mapping")
    if len(stage_summary) > MAX_STAGE_SUMMARY_ENTRIES:
        raise ReviewInputError("run outcome stage_summary has too many entries")
    for key, value in stage_summary.items():
        if (
            not isinstance(key, str)
            or not key
            or len(key) > MAX_STAGE_SUMMARY_KEY_LENGTH
        ):
            raise ReviewInputError("run outcome stage_summary key is invalid")
        if (
            not isinstance(value, str)
            or not value
            or len(value) > MAX_STAGE_SUMMARY_VALUE_LENGTH
        ):
            raise ReviewInputError("run outcome stage_summary value is invalid")


def _validate_recovery_identity(
    *,
    repository: str,
    pull_request_number: int,
    base_sha: str,
    head_sha: str,
) -> None:
    if (
        not isinstance(repository, str)
        or not repository.strip()
        or len(repository) > 256
    ):
        raise ReviewInputError("recovery artifact repository is invalid")
    if (
        isinstance(pull_request_number, bool)
        or not isinstance(pull_request_number, int)
        or pull_request_number < 1
    ):
        raise ReviewInputError("recovery artifact pull_request_number is invalid")
    if not isinstance(base_sha, str) or not _SNAPSHOT.fullmatch(base_sha):
        raise ReviewInputError("recovery artifact base_sha is invalid")
    if not isinstance(head_sha, str) or not _SNAPSHOT.fullmatch(head_sha):
        raise ReviewInputError("recovery artifact head_sha is invalid")


def _json_value_depth(value: object, *, limit: int = MAX_RECOVERY_RESULT_DEPTH) -> int:
    if isinstance(value, Mapping):
        if limit <= 0:
            raise ReviewInputError(
                "recovery artifact result exceeds the configured depth limit"
            )
        if not value:
            return 1
        return 1 + max(
            _json_value_depth(item, limit=limit - 1) for item in value.values()
        )
    if isinstance(value, (list, tuple)):
        if limit <= 0:
            raise ReviewInputError(
                "recovery artifact result exceeds the configured depth limit"
            )
        if not value:
            return 1
        return 1 + max(_json_value_depth(item, limit=limit - 1) for item in value)
    return 1


def _canonical_recovery_result(result: Mapping[str, object]) -> str:
    """Validate and serialize a retained review result before hashing it."""

    if not isinstance(result, Mapping):
        raise ReviewInputError("recovery artifact result must be an object")
    document = dict(result)
    _json_value_depth(document)
    validate_public_document(document, "review-result")
    try:
        canonical = json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            check_circular=True,
        )
    except RecursionError as exc:
        raise ReviewInputError(
            "recovery artifact result exceeds the configured depth limit"
        ) from exc
    except (TypeError, ValueError) as exc:
        raise ReviewInputError(
            "recovery artifact result is not JSON-serializable"
        ) from exc
    if len(canonical.encode("utf-8")) > MAX_RECOVERY_RESULT_BYTES:
        raise ReviewInputError(
            "recovery artifact result exceeds the configured size limit"
        )
    return canonical


@dataclass(frozen=True)
class ResourceBudget:
    """Hard upper bounds for one run; zero means no work is admitted."""

    max_provider_calls: int = 8
    max_retry_attempts: int = 2
    timeout_ms: int = 120_000
    max_prompt_bytes: int = 1_048_576
    max_output_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        for name in (
            "max_provider_calls",
            "max_retry_attempts",
            "timeout_ms",
            "max_prompt_bytes",
            "max_output_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ReviewInputError(f"{name} must be a non-negative integer")


@dataclass(frozen=True)
class RunOutcome:
    status: str
    repository: str | None = None
    pull_request_number: int | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    stage_summary: Mapping[str, str] = field(default_factory=dict)
    provider_calls: int = 0
    retry_attempts: int = 0
    prompt_bytes: int = 0
    response_bytes: int = 0
    elapsed_ms: int = 0
    diagnostic: str | None = None

    def __post_init__(self) -> None:
        if self.status not in RUN_STATUSES:
            raise ReviewInputError("run outcome status is unsupported")
        for name in (
            "provider_calls",
            "retry_attempts",
            "prompt_bytes",
            "response_bytes",
            "elapsed_ms",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ReviewInputError(f"run outcome {name} must be non-negative")
        if self.base_sha is not None and not _SNAPSHOT.fullmatch(self.base_sha):
            raise ReviewInputError(
                "run outcome base_sha must be a commit or snapshot digest"
            )
        if self.head_sha is not None and not _SNAPSHOT.fullmatch(self.head_sha):
            raise ReviewInputError(
                "run outcome head_sha must be a commit or snapshot digest"
            )
        if self.diagnostic is not None and (
            not isinstance(self.diagnostic, str) or len(self.diagnostic) > 512
        ):
            raise ReviewInputError("run outcome diagnostic is too long")
        _validate_stage_summary(self.stage_summary)

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": "1.0",
            "status": self.status,
            "repository": self.repository,
            "pull_request_number": self.pull_request_number,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "stage_summary": dict(self.stage_summary),
            "provider_calls": self.provider_calls,
            "retry_attempts": self.retry_attempts,
            "prompt_bytes": self.prompt_bytes,
            "response_bytes": self.response_bytes,
            "elapsed_ms": self.elapsed_ms,
            "diagnostic": self.diagnostic,
        }
        validate_public_document(value, "run-outcome")
        return value


@dataclass(frozen=True)
class RecoveryArtifact:
    """Identity-bound, opt-in artifact for publication-only recovery."""

    repository: str
    pull_request_number: int
    base_sha: str
    head_sha: str
    result: Mapping[str, object]
    created_at: str
    expires_at: str
    result_sha256: str

    def __post_init__(self) -> None:
        _validate_recovery_identity(
            repository=self.repository,
            pull_request_number=self.pull_request_number,
            base_sha=self.base_sha,
            head_sha=self.head_sha,
        )
        if not isinstance(self.created_at, str) or not self.created_at.strip():
            raise ReviewInputError("recovery artifact created_at is invalid")
        if not isinstance(self.expires_at, str) or not self.expires_at.strip():
            raise ReviewInputError("recovery artifact expires_at is invalid")
        if not isinstance(self.result_sha256, str) or not re.fullmatch(
            r"^[a-f0-9]{64}$", self.result_sha256
        ):
            raise ReviewInputError("recovery artifact result_sha256 is invalid")
        _parse_aware_datetime(self.created_at, label="recovery artifact created_at")
        _parse_aware_datetime(self.expires_at, label="recovery artifact expires_at")

    @classmethod
    def create(
        cls,
        *,
        repository: str,
        pull_request_number: int,
        base_sha: str,
        head_sha: str,
        result: Mapping[str, object],
        expires_at: str,
        created_at: str | None = None,
        now: datetime | None = None,
    ) -> "RecoveryArtifact":
        _validate_recovery_identity(
            repository=repository,
            pull_request_number=pull_request_number,
            base_sha=base_sha,
            head_sha=head_sha,
        )
        if created_at is not None:
            _parse_aware_datetime(created_at, label="recovery artifact created_at")
        _parse_aware_datetime(expires_at, label="recovery artifact expires_at")
        if created_at is None:
            current = now or datetime.now(timezone.utc)
            if current.tzinfo is None or current.utcoffset() is None:
                current = current.replace(tzinfo=timezone.utc)
            created = current.replace(microsecond=0).isoformat()
        else:
            created = created_at
        canonical = _canonical_recovery_result(result)
        return cls(
            repository,
            pull_request_number,
            base_sha,
            head_sha,
            result,
            created,
            expires_at,
            _digest(canonical),
        )

    def validate(
        self,
        *,
        repository: str,
        pull_request_number: int,
        base_sha: str,
        head_sha: str,
        now: datetime | None = None,
    ) -> None:
        if (
            self.repository,
            self.pull_request_number,
            self.base_sha,
            self.head_sha,
        ) != (repository, pull_request_number, base_sha, head_sha):
            raise ReviewInputError(
                "recovery artifact identity does not match current review"
            )
        canonical = _canonical_recovery_result(self.result)
        if _digest(canonical) != self.result_sha256:
            raise ReviewInputError("recovery artifact integrity check failed")
        expiry = _parse_aware_datetime(
            self.expires_at, label="recovery artifact expiry"
        )
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            current = current.replace(tzinfo=timezone.utc)
        if expiry <= current:
            raise ReviewInputError("recovery artifact has expired")

    def to_dict(self) -> dict[str, object]:
        value = {
            "schema_version": "1.0",
            "repository": self.repository,
            "pull_request_number": self.pull_request_number,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "result": dict(self.result),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "result_sha256": self.result_sha256,
        }
        validate_public_document(value, "recovery-artifact")
        return value
