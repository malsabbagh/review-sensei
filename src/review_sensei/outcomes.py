"""Bounded run outcomes and publication-only recovery artifacts.

The review engine intentionally keeps this module provider-neutral.  It is a
small wire contract for callers that need to distinguish a skipped run from a
partial or failed run without retaining prompts, responses, or source text.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

from .errors import ReviewInputError
from .schemas import validate_public_document
from .validation import DEFAULT_REVIEW_LIMITS, ReviewLimits, utf8_size

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
MAX_RECOVERY_RESULT_KEY_LENGTH = 256
MAX_STAGE_SUMMARY_ENTRIES = 64
MAX_STAGE_SUMMARY_KEY_LENGTH = 128
MAX_STAGE_SUMMARY_VALUE_LENGTH = 128
DEFAULT_RESOURCE_BUDGET_MAX_PROVIDER_CALLS = 8
DEFAULT_RESOURCE_BUDGET_MAX_RETRY_ATTEMPTS = 2
DEFAULT_RESOURCE_BUDGET_TIMEOUT_MS = 120_000
DEFAULT_RESOURCE_BUDGET_MAX_PROMPT_BYTES = 1_048_576
DEFAULT_RESOURCE_BUDGET_MAX_OUTPUT_BYTES = 1_048_576
DEFAULT_RECOVERY_TTL_SECONDS = 6 * 60 * 60
MAX_RECOVERY_TTL_SECONDS = 24 * 60 * 60
PUBLIC_SCHEMA_VERSION = "1.0"
FAILURE_RUN_STATUSES = frozenset(
    {"provider_failed", "budget_exhausted", "publication_failed"}
)
PUBLIC_DIAGNOSTICS = frozenset(
    {
        "already_published",
        "cancelled",
        "deadline_exceeded",
        "draft_pr",
        "fork_not_allowed",
        "invalid_provider_output",
        "output_budget",
        "partial_coverage",
        "pr_not_open",
        "prompt_budget",
        "provider_call_limit",
        "provider_failed",
        "publication_ambiguous",
        "publication_failed",
        "recovery_artifact_expired",
        "recovery_artifact_identity_mismatch",
        "recovery_artifact_incomplete",
        "recovery_artifact_missing",
        "recovery_artifact_stale",
        "recovery_artifact_tampered",
        "recovery_learning_prs_refused",
        "secret_redacted",
        "skipped_pr_state",
        "skipped_repository_mismatch",
        "skipped_stale_base",
        "skipped_stale_head",
        "stage_failed",
        "transport_retry_exhausted",
        "writes_disabled",
    }
)
_ACTIONS_SUMMARY_TITLES = {
    "reviewed": "Review completed",
    "partial": "Review completed with partial coverage",
    "skipped_stale": "Review skipped because the pull request is stale",
    "skipped_policy": "Review skipped by policy",
    "provider_failed": "Review failed during provider execution",
    "budget_exhausted": "Review stopped after exhausting a resource budget",
    "publication_failed": "Review publication failed",
    "already_published": "Review already published for this head",
}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _aware_now(now: datetime | None = None) -> datetime:
    """Return an aware UTC timestamp, normalizing naive inputs when provided."""

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _parse_aware_datetime(value: str, *, label: str) -> datetime:
    normalized = value.replace("Z", "+00:00").replace("z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ReviewInputError(f"{label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReviewInputError(f"{label} must include a timezone")
    return parsed


def _is_public_text(value: str) -> bool:
    return value.isprintable() and not any(
        unicodedata.category(character).startswith("C")
        or unicodedata.category(character) == "Cn"
        for character in value
    )


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
            or not _is_public_text(key)
        ):
            raise ReviewInputError("run outcome stage_summary key is invalid")
        if (
            not isinstance(value, str)
            or not value
            or len(value) > MAX_STAGE_SUMMARY_VALUE_LENGTH
            or not _is_public_text(value)
        ):
            raise ReviewInputError("run outcome stage_summary value is invalid")


def _validate_recovery_window(*, created_at: str, expires_at: str) -> None:
    created = _parse_aware_datetime(created_at, label="recovery artifact created_at")
    expiry = _parse_aware_datetime(expires_at, label="recovery artifact expires_at")
    if expiry <= created:
        raise ReviewInputError("recovery artifact expires_at must be after created_at")


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
    if limit < 1:
        raise ReviewInputError(
            "recovery artifact result exceeds the configured depth limit"
        )
    if isinstance(value, Mapping):
        if not value:
            return 1
        child_depths: list[int] = []
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or not key
                or len(key) > MAX_RECOVERY_RESULT_KEY_LENGTH
            ):
                raise ReviewInputError("recovery artifact result key is invalid")
            child_depths.append(_json_value_depth(item, limit=limit - 1))
        return 1 + max(child_depths)
    if isinstance(value, (list, tuple)):
        if not value:
            return 1
        return 1 + max(_json_value_depth(item, limit=limit - 1) for item in value)
    return 1


def _canonical_recovery_result(result: Mapping[str, object]) -> str:
    """Validate and serialize a complete review result before hashing it.

    Recovery artifacts intentionally retain only publisher-validated
    ``review-result`` documents.  Partial or interrupted provider output must
    be normalized upstream before persistence; this helper does not accept a
    looser envelope.
    """

    if not isinstance(result, Mapping):
        raise ReviewInputError("recovery artifact result must be an object")
    document = dict(result)
    _json_value_depth(document)
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
    validate_public_document(document, "review-result")
    return canonical


@dataclass(frozen=True)
class ResourceBudget:
    """Declarative wire contract for one run's resource ceilings.

    Embedders publish these bounds alongside ``RunOutcome`` so callers can
    reason about budget exhaustion consistently.  ``ReviewService.run``
    enforces the ceilings: transport retries stay distinct from the one
    structural-correction attempt, and a hard deadline/admission check happens
    before each provider call.

    Defaults are public downward-only ceilings.  Direct construction validates
    field types only; ``create(limits=...)`` is the fail-closed entry point that
    enforces both the public profile and the active ``ReviewLimits`` profile.
    Published result size is governed separately by ``ReviewLimits.max_result_bytes``.
    """

    max_provider_calls: int = DEFAULT_RESOURCE_BUDGET_MAX_PROVIDER_CALLS
    max_retry_attempts: int = DEFAULT_RESOURCE_BUDGET_MAX_RETRY_ATTEMPTS
    timeout_ms: int = DEFAULT_RESOURCE_BUDGET_TIMEOUT_MS
    max_prompt_bytes: int = DEFAULT_RESOURCE_BUDGET_MAX_PROMPT_BYTES
    max_output_bytes: int = DEFAULT_RESOURCE_BUDGET_MAX_OUTPUT_BYTES

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

    def validate_against_limits(
        self, limits: ReviewLimits = DEFAULT_REVIEW_LIMITS
    ) -> None:
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
        ceilings = {
            "max_provider_calls": DEFAULT_RESOURCE_BUDGET_MAX_PROVIDER_CALLS,
            "max_retry_attempts": DEFAULT_RESOURCE_BUDGET_MAX_RETRY_ATTEMPTS,
            "timeout_ms": DEFAULT_RESOURCE_BUDGET_TIMEOUT_MS,
            "max_prompt_bytes": limits.max_prompt_bytes,
            "max_output_bytes": limits.max_provider_response_bytes,
        }
        for name, ceiling in ceilings.items():
            if getattr(self, name) > ceiling:
                raise ReviewInputError(f"{name} exceeds the configured ceiling")

    @classmethod
    def create(
        cls,
        *,
        limits: ReviewLimits = DEFAULT_REVIEW_LIMITS,
        max_provider_calls: int = DEFAULT_RESOURCE_BUDGET_MAX_PROVIDER_CALLS,
        max_retry_attempts: int = DEFAULT_RESOURCE_BUDGET_MAX_RETRY_ATTEMPTS,
        timeout_ms: int = DEFAULT_RESOURCE_BUDGET_TIMEOUT_MS,
        max_prompt_bytes: int = DEFAULT_RESOURCE_BUDGET_MAX_PROMPT_BYTES,
        max_output_bytes: int = DEFAULT_RESOURCE_BUDGET_MAX_OUTPUT_BYTES,
    ) -> "ResourceBudget":
        budget = cls(
            max_provider_calls=max_provider_calls,
            max_retry_attempts=max_retry_attempts,
            timeout_ms=timeout_ms,
            max_prompt_bytes=max_prompt_bytes,
            max_output_bytes=max_output_bytes,
        )
        budget.validate_against_limits(limits)
        return budget

    @classmethod
    def for_limits(
        cls, limits: ReviewLimits = DEFAULT_REVIEW_LIMITS
    ) -> "ResourceBudget":
        """Return the public budget profile clamped to ``limits``."""

        return cls.create(
            limits=limits,
            max_prompt_bytes=min(
                DEFAULT_RESOURCE_BUDGET_MAX_PROMPT_BYTES, limits.max_prompt_bytes
            ),
            max_output_bytes=min(
                DEFAULT_RESOURCE_BUDGET_MAX_OUTPUT_BYTES,
                limits.max_provider_response_bytes,
            ),
        )


@dataclass(frozen=True)
class RunOutcome:
    """Bounded run summary for one review attempt.

    ``stage_summary`` keys and values must be printable Unicode strings without
    control characters at runtime.  The public JSON schema enforces structural
    bounds only; runtime validation in ``__post_init__`` is authoritative for
    character content.
    """

    status: str
    repository: str | None = None
    pull_request_number: int | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    stage_summary: Mapping[str, str] = field(default_factory=dict)
    provider_calls: int = 0
    retry_attempts: int = 0
    structural_retries: int = 0
    prompt_bytes: int = 0
    response_bytes: int = 0
    elapsed_ms: int = 0
    diagnostic: str | None = None

    def __post_init__(self) -> None:
        if self.status not in RUN_STATUSES:
            raise ReviewInputError("run outcome status is unsupported")
        if self.repository is not None and (
            not isinstance(self.repository, str)
            or not self.repository.strip()
            or len(self.repository) > 256
        ):
            raise ReviewInputError("run outcome repository is invalid")
        if self.pull_request_number is not None and (
            isinstance(self.pull_request_number, bool)
            or not isinstance(self.pull_request_number, int)
            or self.pull_request_number < 1
        ):
            raise ReviewInputError("run outcome pull_request_number is invalid")
        for name in (
            "provider_calls",
            "retry_attempts",
            "structural_retries",
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
            "schema_version": PUBLIC_SCHEMA_VERSION,
            "status": self.status,
            "repository": self.repository,
            "pull_request_number": self.pull_request_number,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "stage_summary": dict(self.stage_summary),
            "provider_calls": self.provider_calls,
            "retry_attempts": self.retry_attempts,
            "structural_retries": self.structural_retries,
            "prompt_bytes": self.prompt_bytes,
            "response_bytes": self.response_bytes,
            "elapsed_ms": self.elapsed_ms,
            "diagnostic": self.diagnostic,
        }
        validate_public_document(value, "run-outcome")
        return value


@dataclass(frozen=True)
class RecoveryArtifact:
    """Identity-bound, opt-in artifact for publication-only recovery.

    The embedded ``result`` must already be a complete, schema-valid
    ``review-result`` document.  Partial run payloads belong in ``RunOutcome``,
    not in a recovery artifact.
    """

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
        _validate_recovery_window(
            created_at=self.created_at, expires_at=self.expires_at
        )
        canonical = _canonical_recovery_result(self.result)
        if _digest(canonical) != self.result_sha256:
            raise ReviewInputError("recovery artifact integrity check failed")
        object.__setattr__(self, "result", json.loads(canonical))

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
            created = _aware_now(now).replace(microsecond=0).isoformat()
        else:
            created = created_at
        _validate_recovery_window(created_at=created, expires_at=expires_at)
        canonical = _canonical_recovery_result(result)
        return cls(
            repository,
            pull_request_number,
            base_sha,
            head_sha,
            json.loads(canonical),
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
                "recovery artifact identity does not match current review",
                diagnostic="recovery_artifact_identity_mismatch",
            )
        canonical = _canonical_recovery_result(self.result)
        if _digest(canonical) != self.result_sha256:
            raise ReviewInputError(
                "recovery artifact integrity check failed",
                diagnostic="recovery_artifact_tampered",
            )
        expiry = _parse_aware_datetime(
            self.expires_at, label="recovery artifact expiry"
        )
        if expiry <= _aware_now(now):
            raise ReviewInputError(
                "recovery artifact has expired",
                diagnostic="recovery_artifact_expired",
            )

    def to_dict(self) -> dict[str, object]:
        _canonical_recovery_result(self.result)
        value = {
            "schema_version": PUBLIC_SCHEMA_VERSION,
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


def sanitize_diagnostic(value: str | None) -> str | None:
    """Return a closed diagnostic token, never caller or provider text."""

    if value is None:
        return None
    if value in PUBLIC_DIAGNOSTICS:
        return value
    return "secret_redacted"


def run_outcome_exit_code(status: str) -> int:
    """Return the CLI exit status for one structured run outcome."""

    return 1 if status in FAILURE_RUN_STATUSES else 0


def render_actions_summary(outcome: RunOutcome) -> str:
    """Return a human-readable Actions summary without source or secrets."""

    title = _ACTIONS_SUMMARY_TITLES.get(outcome.status, "Review run finished")
    lines = [
        f"## {title}",
        f"- status: `{outcome.status}`",
        f"- provider_calls: {outcome.provider_calls}",
        f"- retry_attempts: {outcome.retry_attempts}",
        f"- structural_retries: {outcome.structural_retries}",
        f"- prompt_bytes: {outcome.prompt_bytes}",
        f"- response_bytes: {outcome.response_bytes}",
        f"- elapsed_ms: {outcome.elapsed_ms}",
    ]
    if outcome.pull_request_number is not None:
        lines.append(f"- pull_request: {outcome.pull_request_number}")
    if outcome.diagnostic:
        lines.append(f"- diagnostic: `{sanitize_diagnostic(outcome.diagnostic)}`")
    if outcome.stage_summary:
        lines.append("- stages:")
        for name, status in outcome.stage_summary.items():
            lines.append(f"  - `{name}`: {status}")
    return "\n".join(lines) + "\n"


def emit_host_outcome(outcome: RunOutcome, *, output_path: Path | None = None) -> None:
    """Write the machine-readable outcome and optional Actions annotations."""

    outcome = replace(outcome, diagnostic=sanitize_diagnostic(outcome.diagnostic))
    document = json.dumps(outcome.to_dict(), indent=2) + "\n"
    if output_path is not None:
        output_path.write_text(document, encoding="utf-8")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as handle:
            handle.write(render_actions_summary(outcome))
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with Path(github_output).open("a", encoding="utf-8") as handle:
            handle.write(f"outcome_status={outcome.status}\n")
            if outcome.diagnostic is not None:
                handle.write(f"outcome_diagnostic={outcome.diagnostic}\n")


def recovery_expires_at(
    *,
    ttl_seconds: int = DEFAULT_RECOVERY_TTL_SECONDS,
    now: datetime | None = None,
) -> str:
    """Return an aware ISO-8601 expiry no later than the public TTL ceiling."""

    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
        raise ReviewInputError("recovery ttl_seconds must be an integer")
    if ttl_seconds < 1 or ttl_seconds > MAX_RECOVERY_TTL_SECONDS:
        raise ReviewInputError("recovery ttl_seconds exceeds the configured ceiling")
    created = _aware_now(now).replace(microsecond=0)
    return (created + timedelta(seconds=ttl_seconds)).isoformat()


def load_recovery_artifact(path: Path) -> RecoveryArtifact:
    """Load one identity-bound recovery artifact from disk."""

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ReviewInputError(
            "recovery artifact is missing",
            diagnostic="recovery_artifact_missing",
        ) from exc
    except OSError as exc:
        raise ReviewInputError(
            "recovery artifact could not be read",
            diagnostic="recovery_artifact_tampered",
        ) from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReviewInputError(
            "recovery artifact integrity check failed",
            diagnostic="recovery_artifact_tampered",
        ) from exc
    if not isinstance(document, Mapping):
        raise ReviewInputError(
            "recovery artifact integrity check failed",
            diagnostic="recovery_artifact_tampered",
        )
    payload = dict(document)
    payload.pop("schema_version", None)
    try:
        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise ReviewInputError(
                "recovery artifact integrity check failed",
                diagnostic="recovery_artifact_tampered",
            )
        return RecoveryArtifact(
            repository=payload.get("repository"),  # type: ignore[arg-type]
            pull_request_number=payload.get("pull_request_number"),  # type: ignore[arg-type]
            base_sha=payload.get("base_sha"),  # type: ignore[arg-type]
            head_sha=payload.get("head_sha"),  # type: ignore[arg-type]
            result=result,
            created_at=payload.get("created_at"),  # type: ignore[arg-type]
            expires_at=payload.get("expires_at"),  # type: ignore[arg-type]
            result_sha256=payload.get("result_sha256"),  # type: ignore[arg-type]
        )
    except ReviewInputError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ReviewInputError(
            "recovery artifact integrity check failed",
            diagnostic="recovery_artifact_tampered",
        ) from exc


def diagnostic_for_recovery_error(exc: BaseException) -> str:
    """Map a recovery validation failure to a closed diagnostic token."""

    diagnostic = getattr(exc, "diagnostic", None)
    if isinstance(diagnostic, str) and diagnostic in PUBLIC_DIAGNOSTICS:
        return diagnostic
    return "recovery_artifact_tampered"


def outcome_for_skip_reason(
    skip_reason: str | None,
    *,
    repository: str | None = None,
    pull_request_number: int | None = None,
    base_sha: str | None = None,
    head_sha: str | None = None,
) -> RunOutcome:
    """Return a skip outcome for an ineligible execution plan."""

    status = (
        "skipped_stale" if skip_reason and "stale" in skip_reason else "skipped_policy"
    )
    diagnostic = sanitize_diagnostic(skip_reason) if skip_reason else "skipped_pr_state"
    return RunOutcome(
        status,
        repository=repository,
        pull_request_number=pull_request_number,
        base_sha=base_sha,
        head_sha=head_sha,
        diagnostic=diagnostic,
    )


@dataclass
class ResourceBudgetTracker:
    """Mutable admission counters for one ``ResourceBudget`` envelope."""

    budget: ResourceBudget
    monotonic: Callable[[], float] = time.monotonic
    sleeper: Callable[[float], None] = time.sleep
    started: float = field(init=False)
    provider_calls: int = 0
    transport_retries: int = 0
    structural_retries: int = 0
    prompt_bytes: int = 0
    response_bytes: int = 0

    def __post_init__(self) -> None:
        self.started = self.monotonic()

    def elapsed_ms(self) -> int:
        return max(0, int((self.monotonic() - self.started) * 1000))

    def remaining_seconds(self) -> float:
        remaining_ms = self.budget.timeout_ms - self.elapsed_ms()
        return max(0.0, remaining_ms / 1000)

    def admit_call(self, prompt: str) -> str | None:
        """Return a diagnostic token when a provider call cannot start."""

        if self.elapsed_ms() >= self.budget.timeout_ms:
            return "deadline_exceeded"
        if self.provider_calls >= self.budget.max_provider_calls:
            return "provider_call_limit"
        size = utf8_size(prompt, label="review prompt")
        if size > self.budget.max_prompt_bytes:
            return "prompt_budget"
        return None

    def record_prompt_attempt(self, prompt: str) -> None:
        self.prompt_bytes += utf8_size(prompt, label="review prompt")

    def record_provider_call(self) -> None:
        self.provider_calls += 1

    def record_response(self, response_text: str) -> None:
        if response_text:
            self.response_bytes += utf8_size(response_text, label="provider response")

    def record_call(self, prompt: str, response_text: str = "") -> None:
        self.record_prompt_attempt(prompt)
        self.record_provider_call()
        self.record_response(response_text)

    def admit_transport_retry(self, retry_after_seconds: float | None) -> str | None:
        if self.transport_retries >= self.budget.max_retry_attempts:
            return "transport_retry_exhausted"
        if self.elapsed_ms() >= self.budget.timeout_ms:
            return "deadline_exceeded"
        wait = 0.0 if retry_after_seconds is None else float(retry_after_seconds)
        if wait > self.remaining_seconds():
            return "deadline_exceeded"
        return None

    def sleep_transport_retry(self, retry_after_seconds: float | None) -> None:
        wait = (
            0.0
            if not retry_after_seconds
            else min(float(retry_after_seconds), self.remaining_seconds())
        )
        if wait > 0:
            self.sleeper(wait)
        self.transport_retries += 1
