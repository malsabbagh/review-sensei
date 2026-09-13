"""Bounded run outcomes and publication-only recovery artifacts.

The review engine intentionally keeps this module provider-neutral.  It is a
small wire contract for callers that need to distinguish a skipped run from a
partial or failed run without retaining prompts, responses, or source text.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping

from .errors import ReviewInputError
from .schemas import validate_public_document

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


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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

    def to_dict(self) -> dict[str, object]:
        value = {
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
    ) -> "RecoveryArtifact":
        import json

        created = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        canonical = json.dumps(
            result, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
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
        import json

        if (
            self.repository,
            self.pull_request_number,
            self.base_sha,
            self.head_sha,
        ) != (repository, pull_request_number, base_sha, head_sha):
            raise ReviewInputError(
                "recovery artifact identity does not match current review"
            )
        canonical = json.dumps(
            self.result, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        if _digest(canonical) != self.result_sha256:
            raise ReviewInputError("recovery artifact integrity check failed")
        try:
            expiry = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ReviewInputError("recovery artifact expiry is invalid") from exc
        current = now or datetime.now(timezone.utc)
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
