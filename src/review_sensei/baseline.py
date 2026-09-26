"""Baseline-aware verification planning for issue #136 C4.

A complete compatible review is the last-assessed baseline. Later operator
passes reuse ADR 0042 incremental plans and ADR 0037/#40 related paths to
cover existing concerns plus changed and impacted code. Late blockers need an
explicit trusted reason and optional causal lineage. Omission is not a fix.
``legacy`` stays unscoped. Round refusal remains C5.

The provider-neutral contract is recorded in
``docs/adr/0048-baseline-aware-verification.md``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from typing import Sequence

from .context import (
    MAX_CACHE_METADATA_ITEMS,
    FindingLifecycle,
    IncrementalReviewPlan,
    ReviewContextCacheKey,
    cache_key_is_compatible,
    comment_defect_kind,
    finding_lifecycle_for_comment,
)
from .convergence import (
    ATTRIBUTIONS,
    LATE_REASONS,
    OPERATOR_REVIEW_MODES,
    BlockerCandidate,
    PREVIOUS_DEFAULT_MAX_COMPLETED_VERIFICATION_ROUNDS,
    ReviewConvergencePolicy,
    derive_blocker_candidate,
)
from .coverage import coverage_approval_state
from .errors import ContextLoadError, ReviewInputError
from .models import ReviewComment, ReviewResult
from .planning import MAX_RELATED_PATHS, related_paths_for_change
from .schemas import validate_public_document
from .validation import validate_repository_path

PUBLIC_SCHEMA_VERSION = "1.0"
_SHA256 = re.compile(r"^[a-f0-9]{64}$")

SCOPE_STATUSES = frozenset(
    {
        "legacy-unscoped",
        "baseline-required",
        "incomplete-baseline",
        "incompatible",
        "verify",
        "ledger-untrusted",
    }
)
INVALIDATION_REASONS = frozenset(
    {
        "rebase-or-base-change",
        "model-change",
        "engine-change",
        "profile-change",
        "policy-change",
        "prompt-or-stage-digest-change",
        "context-digest-change",
        "learning-digest-change",
        "incomplete-baseline",
        "missing-baseline",
        "coverage-incomplete",
        "ledger-untrusted",
    }
)
FINDING_CLASSIFICATIONS = frozenset(
    {
        "continuing-concern",
        "reworded-or-moved",
        "verified-fixed",
        "omitted-uncertain",
        "new-regression",
        "substantiated-missed-defect",
        "already-reviewed-optional",
        "pre-existing",
        "needs-human",
        "new-on-initial-pass",
    }
)
LINEAGE_REASONS = frozenset(
    {
        "none",
        "same-concern",
        "reworded-or-moved",
        "fix-introduced-on-changed-path",
        "fix-introduced-on-related-path",
        "missed-on-already-reviewed-path",
        "unattributed",
        "ambiguous-identity",
    }
)
COVERAGE_MODES = frozenset({"full", "incremental", "fallback-full", "unscoped"})
MAX_VERIFICATION_CONCERNS = MAX_CACHE_METADATA_ITEMS
# ADR 0053 reserves room in the 4096-byte envelope for three trusted blocker-set
# identities, so the persisted baseline retains at most two findings. The
# runtime baseline keeps the wider shared metadata budget; only its persisted
# projection is narrowed here.
MAX_HISTORY_FINDINGS = 2
# New F3 writes retain two findings to reserve space for blocker progress, but
# readers accept the three-finding F2 envelope during rolling upgrades. A
# later checkpoint rewrites the bounded projection using MAX_HISTORY_FINDINGS.
MAX_HISTORY_READ_FINDINGS = 3
# The verification-scope and session-record schemas mirror these bounds; update
# their parity tests whenever the shared metadata budget changes.


def _require_bool(value: object, *, label: str) -> None:
    if not isinstance(value, bool):
        raise ReviewInputError(f"{label} must be a boolean")


def _require_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ReviewInputError(f"{label} must be a SHA-256 digest")
    return value


def _token(value: object, *, allowed: frozenset[str], label: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ReviewInputError(f"{label} is unsupported")
    return value


def _optional_token(
    value: object, *, allowed: frozenset[str], label: str
) -> str | None:
    if value is None:
        return None
    return _token(value, allowed=allowed, label=label)


def _bounded_paths(values: Sequence[str], *, label: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ReviewInputError(f"{label} must be a sequence of paths")
    paths: list[str] = []
    seen: set[str] = set()
    for index, path in enumerate(values):
        if index >= MAX_CACHE_METADATA_ITEMS:
            raise ReviewInputError(f"{label} paths exceed MAX_CACHE_METADATA_ITEMS")
        validate_repository_path(path, label=label)
        if path not in seen:
            seen.add(path)
            paths.append(path)
    return tuple(paths)


def _bounded_related_paths(values: Sequence[str], *, label: str) -> tuple[str, ...]:
    paths = _bounded_paths(values, label=label)
    if len(paths) > MAX_RELATED_PATHS:
        raise ReviewInputError("related paths exceed MAX_RELATED_PATHS")
    return paths


def _merge_related_paths(*groups: Sequence[str]) -> tuple[str, ...]:
    paths: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for path in group:
            if path in seen:
                continue
            if len(paths) >= MAX_RELATED_PATHS:
                raise ReviewInputError("related paths exceed MAX_RELATED_PATHS")
            seen.add(path)
            paths.append(path)
    return tuple(paths)


def resolution_criterion_digest(
    *,
    fingerprint: str,
    evidence_id: str | None,
    defect_kind: str,
    path: str | None,
    symbol: str | None,
) -> str:
    """Digest the original resolution identity for a baseline finding."""

    _require_sha256(fingerprint, label="resolution fingerprint")
    payload = json.dumps(
        {
            "fingerprint": fingerprint,
            "evidence_id": evidence_id or "",
            "defect_kind": defect_kind,
            "path": path or "",
            "symbol": symbol or "",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BaselineFinding:
    """One concern recorded by a completed compatible review."""

    fingerprint: str
    resolution_criterion: str
    concern: str | None = None
    path: str | None = None
    symbol: str | None = None
    defect_kind: str = "unknown"
    generation: int = 0
    blocking: bool = False

    def __post_init__(self) -> None:
        _require_sha256(self.fingerprint, label="baseline finding fingerprint")
        _require_sha256(
            self.resolution_criterion, label="baseline resolution criterion"
        )
        if self.concern is not None:
            _require_sha256(self.concern, label="baseline finding concern")
        if self.path is not None:
            validate_repository_path(self.path, label="baseline finding path")
        if self.symbol is not None and (
            not isinstance(self.symbol, str) or not self.symbol.strip()
        ):
            raise ReviewInputError("baseline finding symbol is invalid")
        if not isinstance(self.defect_kind, str) or not self.defect_kind.strip():
            raise ReviewInputError("baseline finding defect_kind is invalid")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 0
        ):
            raise ReviewInputError("baseline finding generation is invalid")
        _require_bool(self.blocking, label="baseline finding blocking")

    def identity_key(self) -> tuple[str, str, str]:
        return (self.path or "", self.symbol or "", self.defect_kind)


def baseline_finding_from_comment(
    comment: ReviewComment, *, generation: int = 0
) -> BaselineFinding:
    """Record a finding's identity and resolution criterion from a comment."""

    lifecycle = finding_lifecycle_for_comment(comment, generation=generation)
    defect_kind = comment_defect_kind(comment)
    return BaselineFinding(
        fingerprint=lifecycle.fingerprint,
        resolution_criterion=resolution_criterion_digest(
            fingerprint=lifecycle.fingerprint,
            evidence_id=comment.evidence_id,
            defect_kind=defect_kind,
            path=comment.path,
            symbol=comment.symbol,
        ),
        concern=lifecycle.concern,
        path=comment.path,
        symbol=comment.symbol,
        defect_kind=defect_kind,
        generation=generation,
        blocking=comment.blocks_approval,
    )


@dataclass(frozen=True)
class ReviewBaseline:
    """Last compatible completed review used as the verification baseline."""

    cache_key: ReviewContextCacheKey
    policy_digest: str
    complete: bool
    findings: tuple[BaselineFinding, ...] = ()
    reviewed_paths: tuple[str, ...] = ()
    related_paths: tuple[str, ...] = ()
    coverage_complete: bool = True
    generation: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.cache_key, ReviewContextCacheKey):
            raise ReviewInputError("review baseline cache_key is invalid")
        _require_sha256(self.policy_digest, label="baseline policy_digest")
        _require_bool(self.complete, label="baseline complete")
        _require_bool(self.coverage_complete, label="baseline coverage_complete")
        if not isinstance(self.findings, tuple) or any(
            not isinstance(item, BaselineFinding) for item in self.findings
        ):
            raise ReviewInputError("review baseline findings are invalid")
        if len(self.findings) > MAX_CACHE_METADATA_ITEMS:
            raise ReviewInputError("review baseline findings exceed the metadata bound")
        object.__setattr__(
            self,
            "reviewed_paths",
            _bounded_paths(self.reviewed_paths, label="reviewed"),
        )
        object.__setattr__(
            self,
            "related_paths",
            _bounded_related_paths(self.related_paths, label="related"),
        )
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 0
        ):
            raise ReviewInputError("review baseline generation is invalid")


def baseline_history_document(baseline: ReviewBaseline) -> dict[str, object]:
    """Return the closed metadata needed to rebuild a completed baseline.

    This is deliberately identity and evidence metadata only: it never carries
    review prompts, diffs, provider output, or rendered finding text.

    The bounds enforced here are the ones the published session-record schema
    enforces on the persisted envelope. Checking them before serialization
    keeps a checkpoint that Python accepts from becoming a record the schema
    rejects on the next read, which would strand the pull request in an
    unreadable session state.
    """

    if not isinstance(baseline, ReviewBaseline):
        raise ReviewInputError("review baseline is invalid")
    if len(baseline.reviewed_paths) > MAX_CACHE_METADATA_ITEMS:
        raise ReviewInputError("baseline reviewed paths exceed the persisted bound")
    if len(baseline.related_paths) > MAX_RELATED_PATHS:
        raise ReviewInputError("baseline related paths exceed the persisted bound")
    # The envelope carries a bounded finding set. Selecting by fingerprint is
    # deterministic and content-derived, so the persisted history does not
    # depend on provider ordering or on how many retries a run took, and a
    # review with more findings than the envelope allows still checkpoints
    # instead of writing a document the schema rejects on the next read.
    findings = sorted(baseline.findings, key=lambda item: item.fingerprint)[
        :MAX_HISTORY_FINDINGS
    ]
    return {
        "cache_key": baseline_cache_key_document(baseline.cache_key),
        "policy_digest": baseline.policy_digest,
        "complete": baseline.complete,
        "coverage_complete": baseline.coverage_complete,
        "generation": baseline.generation,
        "findings": [
            {
                "fingerprint": finding.fingerprint,
                "resolution_criterion": finding.resolution_criterion,
                "concern": finding.concern,
                "path": finding.path,
                "symbol": finding.symbol,
                "defect_kind": finding.defect_kind,
                "generation": finding.generation,
                "blocking": finding.blocking,
            }
            for finding in findings
        ],
        "reviewed_paths": list(baseline.reviewed_paths),
        "related_paths": list(baseline.related_paths),
    }


_BASELINE_CACHE_KEY_FIELDS = frozenset(
    {
        "repository",
        "pull_request",
        "base_sha",
        "head_sha",
        "engine",
        "model",
        "profile",
        "stage_digest",
        "context_digest",
        "learning_digest",
    }
)
_BASELINE_FINDING_FIELDS = frozenset(
    {
        "fingerprint",
        "resolution_criterion",
        "concern",
        "path",
        "symbol",
        "defect_kind",
        "generation",
        "blocking",
    }
)


def baseline_cache_key_document(key: ReviewContextCacheKey) -> dict[str, object]:
    """Return the closed identity document for one review context cache key."""

    if not isinstance(key, ReviewContextCacheKey):
        raise ReviewInputError("review cache key is invalid")
    return {
        "repository": key.repository,
        "pull_request": key.pull_request,
        "base_sha": key.base_sha,
        "head_sha": key.head_sha,
        "engine": key.engine,
        "model": key.model,
        "profile": key.profile,
        "stage_digest": key.stage_digest,
        "context_digest": key.context_digest,
        "learning_digest": key.learning_digest,
    }


def cache_key_from_document(value: object) -> ReviewContextCacheKey:
    """Rebuild one cache key from a closed trusted or persisted document."""

    if not isinstance(value, dict) or set(value) != _BASELINE_CACHE_KEY_FIELDS:
        raise ReviewInputError("review cache key has an invalid shape")
    try:
        return ReviewContextCacheKey(**value)
    except (KeyError, TypeError, ContextLoadError, ReviewInputError) as exc:
        raise ReviewInputError("review cache key is invalid") from exc


def baseline_from_history_document(value: object) -> ReviewBaseline:
    """Rebuild one bounded baseline after a fresh durable-ledger load."""

    if not isinstance(value, dict):
        raise ReviewInputError("persisted baseline is invalid")
    required = {
        "cache_key",
        "policy_digest",
        "complete",
        "coverage_complete",
        "generation",
        "findings",
        "reviewed_paths",
        "related_paths",
    }
    if set(value) != required or not isinstance(value["cache_key"], dict):
        raise ReviewInputError("persisted baseline has an invalid shape")
    # F3 trusts this reconstruction, so the persisted shape must be provably
    # closed: an extra key is never silently dropped and a missing key is
    # never silently coerced to a BaselineFinding default.
    if set(value["cache_key"]) != _BASELINE_CACHE_KEY_FIELDS:
        raise ReviewInputError("persisted baseline has an invalid shape")
    findings_value = value["findings"]
    if not isinstance(findings_value, list):
        raise ReviewInputError("persisted baseline has an invalid shape")
    for item in findings_value:
        if not isinstance(item, dict) or set(item) != _BASELINE_FINDING_FIELDS:
            raise ReviewInputError("persisted baseline has an invalid shape")
    try:
        key = ReviewContextCacheKey(**value["cache_key"])
        findings = tuple(BaselineFinding(**item) for item in findings_value)
        return ReviewBaseline(
            cache_key=key,
            policy_digest=value["policy_digest"],
            complete=value["complete"],
            coverage_complete=value["coverage_complete"],
            generation=value["generation"],
            findings=findings,
            reviewed_paths=tuple(value["reviewed_paths"]),
            related_paths=tuple(value["related_paths"]),
        )
    except (KeyError, TypeError, ContextLoadError, ReviewInputError) as exc:
        raise ReviewInputError("persisted baseline is invalid") from exc


def admission_context_document(
    baseline: ReviewBaseline | None, current_key: ReviewContextCacheKey
) -> dict[str, object]:
    """Return the trusted publication admission inputs for one identity.

    F3 admission needs the prior baseline a verification round classifies
    against and the independently constructed cache key for the exact head and
    configuration being published. Neither is recoverable from the durable
    transaction alone, so an analysis emits them as one closed document for
    the trusted publication boundary of the same run. The document carries no
    prompt, diff, provider output, or rendered finding text.
    """

    if baseline is not None and not isinstance(baseline, ReviewBaseline):
        raise ReviewInputError("review baseline is invalid")
    return {
        "baseline": (None if baseline is None else baseline_history_document(baseline)),
        "current_key": baseline_cache_key_document(current_key),
    }


def admission_context_from_document(
    value: object,
) -> tuple[ReviewBaseline | None, ReviewContextCacheKey]:
    """Rebuild the trusted publication admission inputs from one document."""

    if not isinstance(value, dict) or set(value) != {"baseline", "current_key"}:
        raise ReviewInputError("admission context has an invalid shape")
    baseline_value = value["baseline"]
    baseline = (
        None
        if baseline_value is None
        else baseline_from_history_document(baseline_value)
    )
    return baseline, cache_key_from_document(value["current_key"])


def baseline_from_review(
    result: ReviewResult,
    *,
    cache_key: ReviewContextCacheKey,
    policy: ReviewConvergencePolicy,
    related_paths: Sequence[str] = (),
    reviewed_paths: Sequence[str] | None = None,
    generation: int = 0,
) -> ReviewBaseline:
    """Build a baseline from a prior review. Incomplete reviews stay incomplete."""

    if not isinstance(result, ReviewResult):
        raise ReviewInputError("review result is invalid")
    if not isinstance(policy, ReviewConvergencePolicy):
        raise ReviewInputError("review convergence policy is invalid")
    coverage_state = coverage_approval_state(result.coverage)
    coverage_complete = result.coverage is not None and coverage_state == "reviewed"
    findings = tuple(
        baseline_finding_from_comment(comment, generation=generation)
        for comment in result.comments
    )
    if reviewed_paths is None:
        paths = [finding.path for finding in findings if finding.path is not None]
        if result.coverage is not None:
            paths.extend(result.coverage.enumerated_paths)
        reviewed = _bounded_paths(paths, label="reviewed")
    else:
        reviewed = _bounded_paths(reviewed_paths, label="reviewed")
    # A legacy result without a coverage manifest is never a complete
    # baseline. Caller-supplied reviewed paths constrain the fallback scope,
    # but do not prove that the rest of the change was enumerated and reviewed.
    complete = result.review_status == "complete" and coverage_complete
    if complete and not reviewed:
        raise ReviewInputError(
            "complete baseline requires reviewed paths or complete coverage"
        )
    return ReviewBaseline(
        cache_key=cache_key,
        policy_digest=policy.digest(),
        complete=complete,
        findings=findings,
        reviewed_paths=reviewed,
        related_paths=_bounded_related_paths(related_paths, label="related"),
        coverage_complete=coverage_complete,
        generation=generation,
    )


def evaluate_baseline_compatibility(
    baseline: ReviewBaseline,
    *,
    current_key: ReviewContextCacheKey,
    policy: ReviewConvergencePolicy,
) -> str | None:
    """Return the first invalidation reason, or None when the baseline is reusable."""

    if not isinstance(baseline, ReviewBaseline):
        raise ReviewInputError("review baseline is invalid")
    if not isinstance(current_key, ReviewContextCacheKey):
        raise ReviewInputError("current review cache key is invalid")
    if not isinstance(policy, ReviewConvergencePolicy):
        raise ReviewInputError("review convergence policy is invalid")
    if not baseline.complete:
        return (
            "incomplete-baseline"
            if baseline.coverage_complete
            else "coverage-incomplete"
        )
    if policy.digest() != baseline.policy_digest:
        previous_allowance = replace(
            policy,
            max_completed_verification_rounds=(
                PREVIOUS_DEFAULT_MAX_COMPLETED_VERIFICATION_ROUNDS
            ),
        )
        if previous_allowance.digest() != baseline.policy_digest:
            return "policy-change"
    previous = baseline.cache_key
    if previous.base_sha != current_key.base_sha:
        return "rebase-or-base-change"
    if previous.model != current_key.model:
        return "model-change"
    if previous.engine != current_key.engine:
        return "engine-change"
    if previous.profile != current_key.profile:
        return "profile-change"
    if previous.stage_digest != current_key.stage_digest:
        return "prompt-or-stage-digest-change"
    if previous.context_digest != current_key.context_digest:
        return "context-digest-change"
    if previous.learning_digest != current_key.learning_digest:
        return "learning-digest-change"
    if not cache_key_is_compatible(current_key, previous):
        return "rebase-or-base-change"
    return None


def _unique_paths(*groups: Sequence[str]) -> tuple[str, ...]:
    paths: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for path in group:
            if path not in seen:
                if len(paths) >= MAX_CACHE_METADATA_ITEMS:
                    raise ReviewInputError(
                        "reviewed paths exceed MAX_CACHE_METADATA_ITEMS"
                    )
                seen.add(path)
                paths.append(path)
    return tuple(paths)


@dataclass(frozen=True)
class VerificationScope:
    """Trusted re-review scope for one later pass."""

    status: str
    round_kind: str
    late_admission_required: bool
    coverage_mode: str
    invalidation_reason: str | None = None
    reviewed_paths: tuple[str, ...] = ()
    related_paths: tuple[str, ...] = ()
    changed_paths: tuple[str, ...] = ()
    existing_concerns: int = 0
    incremental: IncrementalReviewPlan | None = None

    def __post_init__(self) -> None:
        _token(self.status, allowed=SCOPE_STATUSES, label="verification status")
        if self.round_kind not in {"none", "initial", "verification"}:
            raise ReviewInputError("verification round_kind is unsupported")
        _require_bool(self.late_admission_required, label="late_admission_required")
        _token(self.coverage_mode, allowed=COVERAGE_MODES, label="coverage_mode")
        _optional_token(
            self.invalidation_reason,
            allowed=INVALIDATION_REASONS,
            label="invalidation_reason",
        )
        object.__setattr__(
            self,
            "reviewed_paths",
            _bounded_paths(self.reviewed_paths, label="reviewed"),
        )
        object.__setattr__(
            self,
            "related_paths",
            _bounded_related_paths(self.related_paths, label="related"),
        )
        object.__setattr__(
            self, "changed_paths", _bounded_paths(self.changed_paths, label="changed")
        )
        if (
            isinstance(self.existing_concerns, bool)
            or not isinstance(self.existing_concerns, int)
            or self.existing_concerns < 0
            or self.existing_concerns > MAX_VERIFICATION_CONCERNS
        ):
            raise ReviewInputError("existing_concerns is invalid")
        if self.incremental is not None and not isinstance(
            self.incremental, IncrementalReviewPlan
        ):
            raise ReviewInputError("verification incremental plan is invalid")
        if self.status == "verify" and not self.late_admission_required:
            raise ReviewInputError("verification requires late admission")
        if self.late_admission_required and self.round_kind != "verification":
            raise ReviewInputError("late admission applies only to verification")
        if self.status == "verify" and self.coverage_mode not in {
            "incremental",
        }:
            raise ReviewInputError("verification requires incremental coverage")
        if self.status == "verify" and self.incremental is None:
            raise ReviewInputError("verification requires an incremental plan")
        if self.status == "legacy-unscoped" and self.round_kind != "none":
            raise ReviewInputError("legacy-unscoped scopes must have no round")
        if self.status in {"incompatible", "incomplete-baseline"}:
            if self.coverage_mode != "fallback-full":
                raise ReviewInputError(
                    "invalidated verification scopes require fallback-full coverage"
                )
            if self.late_admission_required:
                raise ReviewInputError(
                    "invalidated verification scopes cannot require late admission"
                )

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": PUBLIC_SCHEMA_VERSION,
            "status": self.status,
            "round_kind": self.round_kind,
            "late_admission_required": self.late_admission_required,
            "coverage_mode": self.coverage_mode,
            "invalidation_reason": self.invalidation_reason,
            "reviewed_paths": list(self.reviewed_paths),
            "related_paths": list(self.related_paths),
            "changed_paths": list(self.changed_paths),
            "existing_concerns": self.existing_concerns,
        }
        validate_public_document(value, "verification-scope")
        return value


def _scope(
    *,
    status: str,
    round_kind: str,
    late_admission_required: bool,
    coverage_mode: str,
    invalidation_reason: str | None = None,
    reviewed_paths: Sequence[str] = (),
    related_paths: Sequence[str] = (),
    changed_paths: Sequence[str] = (),
    existing_concerns: int = 0,
    incremental: IncrementalReviewPlan | None = None,
) -> VerificationScope:
    return VerificationScope(
        status=status,
        round_kind=round_kind,
        late_admission_required=late_admission_required,
        coverage_mode=coverage_mode,
        invalidation_reason=invalidation_reason,
        reviewed_paths=tuple(reviewed_paths),
        related_paths=tuple(related_paths),
        changed_paths=tuple(changed_paths),
        existing_concerns=existing_concerns,
        incremental=incremental,
    )


def preview_verification_scope(
    *,
    policy: ReviewConvergencePolicy,
    completed_initial_reviews: int | None = None,
    session_status: str | None = None,
    changed_paths: Sequence[str] = (),
    related_paths: Sequence[str] | None = None,
) -> VerificationScope:
    """Display a safe preview when a full ``ReviewBaseline`` is unavailable.

    A session counter alone cannot prove that the stored baseline is compatible
    with the current head and policy.  Consequently this preview never returns
    ``verify``; only :func:`plan_verification_scope`, with an actual compatible
    baseline, can authorize late admission.
    """

    if not isinstance(policy, ReviewConvergencePolicy):
        raise ReviewInputError("review convergence policy is invalid")
    changed = _bounded_paths(changed_paths, label="changed")
    related = (
        _bounded_related_paths(related_paths_for_change(changed), label="related")
        if related_paths is None
        else _bounded_related_paths(related_paths, label="related")
    )
    if policy.mode not in OPERATOR_REVIEW_MODES:
        return _scope(
            status="legacy-unscoped",
            round_kind="none",
            late_admission_required=False,
            coverage_mode="unscoped",
            changed_paths=changed,
            related_paths=related,
        )
    if session_status not in {None, "ok", "missing"}:
        return _scope(
            status="ledger-untrusted",
            round_kind="none",
            late_admission_required=False,
            coverage_mode="unscoped",
            invalidation_reason="ledger-untrusted",
            changed_paths=changed,
            related_paths=related,
        )
    # A preview has no baseline cache key to compare with the current head.
    # It may describe the operator's next step, but it must never authorize
    # late admission solely from a session counter. The counter still tells
    # the display whether the next planned round is initial or verification.
    preview_round = (
        "verification"
        if completed_initial_reviews is not None and completed_initial_reviews >= 1
        else "initial"
    )
    return _scope(
        status="baseline-required",
        round_kind=preview_round,
        late_admission_required=False,
        coverage_mode="full",
        invalidation_reason="missing-baseline",
        reviewed_paths=changed,
        related_paths=related,
        changed_paths=changed,
    )


def plan_verification_scope(
    *,
    policy: ReviewConvergencePolicy,
    baseline: ReviewBaseline | None,
    current_key: ReviewContextCacheKey | None = None,
    changed_paths: Sequence[str] = (),
    related_paths: Sequence[str] | None = None,
    confirmed_concerns: Sequence[str] = (),
    context_complete: bool = True,
) -> VerificationScope:
    """Plan the next pass against a stored complete baseline."""

    if not isinstance(policy, ReviewConvergencePolicy):
        raise ReviewInputError("review convergence policy is invalid")
    changed = _bounded_paths(changed_paths, label="changed")
    if policy.mode not in OPERATOR_REVIEW_MODES:
        raw_extra_related = (
            related_paths_for_change(changed)
            if related_paths is None
            else related_paths
        )
        extra_related = _bounded_related_paths(raw_extra_related, label="related")
        return preview_verification_scope(
            policy=policy, changed_paths=changed, related_paths=extra_related
        )
    if baseline is None:
        raw_extra_related = (
            related_paths_for_change(changed)
            if related_paths is None
            else related_paths
        )
        extra_related = _bounded_related_paths(raw_extra_related, label="related")
        return _scope(
            status="baseline-required",
            round_kind="initial",
            late_admission_required=False,
            coverage_mode="full",
            invalidation_reason="missing-baseline",
            reviewed_paths=changed,
            related_paths=extra_related,
            changed_paths=changed,
        )
    if current_key is None:
        current_key = baseline.cache_key
    reason = evaluate_baseline_compatibility(
        baseline, current_key=current_key, policy=policy
    )
    existing_paths = tuple(
        finding.path for finding in baseline.findings if finding.path is not None
    )
    if reason is not None:
        status = (
            "incomplete-baseline"
            if reason
            in {
                "incomplete-baseline",
                "coverage-incomplete",
            }
            else "incompatible"
        )
        # Validate path syntax and the general metadata bound, but do not
        # refuse an already-invalidated baseline merely because its optional
        # related context exceeds the incremental related-path cap.  This
        # branch is an explicit fallback-full scope, so that context is not
        # trusted for late admission.
        # A fallback-full scope does not trust related context.  Validate only
        # explicitly supplied paths; deriving directory siblings here would do
        # unnecessary work and could turn a safe invalidation into an input
        # failure for unrelated context.
        if related_paths is not None:
            _bounded_paths(related_paths, label="related")
        return _scope(
            status=status,
            round_kind="initial",
            late_admission_required=False,
            coverage_mode="fallback-full",
            invalidation_reason=reason,
            reviewed_paths=changed,
            related_paths=(),
            changed_paths=changed,
            existing_concerns=len(baseline.findings),
        )
    if not context_complete:
        if related_paths is not None:
            _bounded_paths(related_paths, label="related")
        return _scope(
            status="incomplete-baseline",
            round_kind="initial",
            late_admission_required=False,
            coverage_mode="fallback-full",
            invalidation_reason="coverage-incomplete",
            reviewed_paths=changed,
            related_paths=(),
            changed_paths=changed,
            existing_concerns=len(baseline.findings),
        )
    raw_extra_related = (
        related_paths_for_change(changed) if related_paths is None else related_paths
    )
    extra_related = _bounded_related_paths(raw_extra_related, label="related")
    related = _merge_related_paths(baseline.related_paths, extra_related)
    reviewed = _unique_paths(existing_paths, baseline.reviewed_paths, changed, related)
    confirmed = tuple(
        _require_sha256(item, label="confirmed concern") for item in confirmed_concerns
    )
    try:
        incremental = IncrementalReviewPlan(
            previous_key=baseline.cache_key,
            previous_findings=tuple(
                FindingLifecycle(
                    fingerprint=finding.fingerprint,
                    state="still-present",
                    evidence=finding.resolution_criterion,
                    concern=finding.concern,
                    path=finding.path,
                    generation=finding.generation,
                )
                for finding in baseline.findings
            ),
            reviewed_paths=reviewed,
            related_paths=related,
            context_complete=context_complete,
            generation=baseline.generation + 1,
            evidence_confirmed_concerns=confirmed,
        )
    except ContextLoadError as exc:
        raise ReviewInputError(str(exc)) from exc
    return _scope(
        status="verify",
        round_kind="verification",
        late_admission_required=True,
        coverage_mode="incremental",
        reviewed_paths=reviewed,
        related_paths=related,
        changed_paths=changed,
        existing_concerns=len(baseline.findings),
        incremental=incremental,
    )


@dataclass(frozen=True)
class LaterFindingClassification:
    """Trusted late-admission facts for one later finding."""

    fingerprint: str
    classification: str
    is_late_relative_to_baseline: bool
    is_duplicate: bool
    late_reason: str | None = None
    causal_parent: str | None = None
    lineage_reason: str = "none"
    attribution: str = "unattributed"

    def __post_init__(self) -> None:
        _require_sha256(self.fingerprint, label="classification fingerprint")
        _token(
            self.classification,
            allowed=FINDING_CLASSIFICATIONS,
            label="finding classification",
        )
        _require_bool(
            self.is_late_relative_to_baseline, label="is_late_relative_to_baseline"
        )
        _require_bool(self.is_duplicate, label="is_duplicate")
        _optional_token(self.late_reason, allowed=LATE_REASONS, label="late_reason")
        if self.causal_parent is not None:
            _require_sha256(self.causal_parent, label="causal_parent")
        _token(self.lineage_reason, allowed=LINEAGE_REASONS, label="lineage_reason")
        _token(self.attribution, allowed=ATTRIBUTIONS, label="attribution")
        if self.is_duplicate and self.classification not in {
            "continuing-concern",
            "reworded-or-moved",
            "verified-fixed",
            "omitted-uncertain",
        }:
            raise ReviewInputError("duplicates must keep an existing concern identity")
        if self.late_reason is not None and not self.is_late_relative_to_baseline:
            raise ReviewInputError("late_reason requires a late finding")
        if self.is_late_relative_to_baseline and self.late_reason is None:
            raise ReviewInputError("late finding requires an explicit late_reason")

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": PUBLIC_SCHEMA_VERSION,
            "fingerprint": self.fingerprint,
            "classification": self.classification,
            "is_late_relative_to_baseline": self.is_late_relative_to_baseline,
            "late_reason": self.late_reason,
            "is_duplicate": self.is_duplicate,
            "causal_parent": self.causal_parent,
            "lineage_reason": self.lineage_reason,
            "attribution": self.attribution,
        }
        validate_public_document(value, "later-finding-classification")
        return value


def _baseline_match_candidates(
    comment: ReviewComment, baseline: ReviewBaseline
) -> tuple[BaselineFinding | None, bool]:
    lifecycle = finding_lifecycle_for_comment(comment)
    key = (comment.path, comment.symbol or "", comment_defect_kind(comment))
    exact_matches = [
        finding for finding in baseline.findings if finding.identity_key() == key
    ]
    if len(exact_matches) > 1:
        return None, True
    fingerprint_matches = [
        finding
        for finding in baseline.findings
        if finding.fingerprint == lifecycle.fingerprint
    ]
    if len(fingerprint_matches) == 1:
        return fingerprint_matches[0], False
    if len(fingerprint_matches) > 1:
        return None, True
    if lifecycle.concern is not None:
        concern_matches = [
            finding
            for finding in baseline.findings
            if finding.concern == lifecycle.concern
        ]
        if len(concern_matches) == 1:
            return concern_matches[0], False
        if len(concern_matches) > 1:
            return None, True
    if len(exact_matches) == 1:
        return exact_matches[0], False
    # A moved or reworded finding can legitimately change its symbol while
    # retaining the same path and defect kind.  Admit that fallback only when
    # the path/kind pair is unique; ambiguity must remain human-adjudicated.
    comment_kind = comment_defect_kind(comment)
    if comment_kind != "unknown":
        path_kind_matches = [
            finding
            for finding in baseline.findings
            if finding.path == comment.path and finding.defect_kind == comment_kind
        ]
        if len(path_kind_matches) == 1:
            return path_kind_matches[0], False
        if len(path_kind_matches) > 1:
            return None, True
    return None, False


def match_baseline_finding(
    comment: ReviewComment, baseline: ReviewBaseline
) -> BaselineFinding | None:
    """Return a unique baseline match, or ``None`` when absent or ambiguous."""

    matched, _ambiguous = _baseline_match_candidates(comment, baseline)
    return matched


def _shares_lineage_identity(comment: ReviewComment, finding: BaselineFinding) -> bool:
    comment_kind = comment_defect_kind(comment)
    if (
        comment.symbol is not None
        and finding.symbol is not None
        and comment.symbol == finding.symbol
    ):
        return True
    return (
        comment_kind != "unknown"
        and finding.defect_kind != "unknown"
        and comment_kind == finding.defect_kind
    )


def _require_evidence_criterion(
    finding: BaselineFinding,
    *,
    evidence_confirmed: bool,
    evidence_criterion: str | None,
) -> None:
    """Require independent confirmation to bind to the stored criterion."""

    _require_bool(evidence_confirmed, label="evidence_confirmed")
    if not evidence_confirmed:
        if evidence_criterion is not None:
            raise ReviewInputError(
                "evidence_criterion requires evidence_confirmed=True"
            )
        return
    if evidence_criterion is None:
        raise ReviewInputError(
            "evidence_confirmed requires the original resolution criterion"
        )
    _require_sha256(evidence_criterion, label="evidence criterion")
    if evidence_criterion != finding.resolution_criterion:
        raise ReviewInputError("evidence criterion does not match the baseline")


def _causal_parent(
    comment: ReviewComment,
    *,
    baseline: ReviewBaseline,
    changed_paths: Sequence[str],
    related_paths: Sequence[str],
) -> tuple[BaselineFinding | None, str]:
    changed = set(changed_paths)
    related = set(related_paths)
    if comment.path in changed:
        for finding in baseline.findings:
            if (
                finding.blocking
                and finding.path in changed
                and _shares_lineage_identity(comment, finding)
            ):
                return finding, "fix-introduced-on-changed-path"
    if comment.path in related:
        for finding in baseline.findings:
            if (
                finding.blocking
                and (finding.path in changed or finding.path in related)
                and _shares_lineage_identity(comment, finding)
            ):
                return finding, "fix-introduced-on-related-path"
    return None, "none"


def classify_later_finding(
    comment: ReviewComment,
    *,
    baseline: ReviewBaseline,
    scope: VerificationScope,
    changed_paths: Sequence[str] = (),
    related_paths: Sequence[str] = (),
    evidence_confirmed: bool = False,
    evidence_criterion: str | None = None,
    on_changed_path: bool = False,
    is_preference_or_optional: bool = False,
    has_contradictory_evidence: bool = False,
) -> LaterFindingClassification:
    """Classify a later finding against the last complete compatible review."""

    if not isinstance(comment, ReviewComment):
        raise ReviewInputError("blocker comment is invalid")
    if not isinstance(baseline, ReviewBaseline):
        raise ReviewInputError("review baseline is invalid")
    if not isinstance(scope, VerificationScope):
        raise ReviewInputError("verification scope is invalid")
    _require_bool(evidence_confirmed, label="evidence_confirmed")
    lifecycle = finding_lifecycle_for_comment(comment)
    if scope.status != "verify":
        if scope.status in {"incompatible", "incomplete-baseline"}:
            # A fallback-full pass has no compatible baseline facts from which
            # to prove that a finding was missed. Keep the result explicitly
            # human-adjudicated rather than minting the strong
            # ``substantiated-missed-defect`` public reason unconditionally.
            return LaterFindingClassification(
                fingerprint=lifecycle.fingerprint,
                classification="needs-human",
                is_late_relative_to_baseline=True,
                is_duplicate=False,
                late_reason="human-adjudication",
                lineage_reason="ambiguous-identity",
                attribution="pr-change" if on_changed_path else "unattributed",
            )
        return LaterFindingClassification(
            fingerprint=lifecycle.fingerprint,
            classification="new-on-initial-pass",
            is_late_relative_to_baseline=False,
            is_duplicate=False,
            attribution="pr-change" if on_changed_path else "unattributed",
        )
    matched, ambiguous = _baseline_match_candidates(comment, baseline)
    if ambiguous:
        return LaterFindingClassification(
            fingerprint=lifecycle.fingerprint,
            classification="needs-human",
            is_late_relative_to_baseline=True,
            is_duplicate=False,
            late_reason="human-adjudication",
            lineage_reason="ambiguous-identity",
            attribution="unattributed",
        )
    if matched is None and evidence_confirmed:
        raise ReviewInputError("evidence_confirmed requires a matched baseline finding")
    if matched is not None:
        same = matched.fingerprint == lifecycle.fingerprint
        if evidence_confirmed:
            _require_evidence_criterion(
                matched,
                evidence_confirmed=evidence_confirmed,
                evidence_criterion=evidence_criterion,
            )
            return LaterFindingClassification(
                fingerprint=lifecycle.fingerprint,
                classification="verified-fixed",
                is_late_relative_to_baseline=False,
                is_duplicate=True,
                causal_parent=matched.fingerprint,
                lineage_reason="same-concern" if same else "reworded-or-moved",
                attribution="pr-change" if on_changed_path else "unattributed",
            )
        return LaterFindingClassification(
            fingerprint=lifecycle.fingerprint,
            classification="continuing-concern" if same else "reworded-or-moved",
            is_late_relative_to_baseline=False,
            is_duplicate=True,
            causal_parent=matched.fingerprint,
            lineage_reason="same-concern" if same else "reworded-or-moved",
            attribution="pr-change" if on_changed_path else "unattributed",
        )
    if has_contradictory_evidence:
        return LaterFindingClassification(
            fingerprint=lifecycle.fingerprint,
            classification="needs-human",
            is_late_relative_to_baseline=True,
            is_duplicate=False,
            late_reason="human-adjudication",
            lineage_reason="ambiguous-identity",
            attribution="unattributed",
        )
    parent, lineage = _causal_parent(
        comment,
        baseline=baseline,
        changed_paths=changed_paths,
        related_paths=related_paths,
    )
    already_reviewed = comment.path in set(baseline.reviewed_paths)
    if is_preference_or_optional and already_reviewed:
        return LaterFindingClassification(
            fingerprint=lifecycle.fingerprint,
            classification="already-reviewed-optional",
            is_late_relative_to_baseline=True,
            is_duplicate=False,
            late_reason="human-adjudication",
            causal_parent=None if parent is None else parent.fingerprint,
            lineage_reason="missed-on-already-reviewed-path",
            attribution="already-reviewed-optional",
        )
    if (
        not on_changed_path
        and comment.path not in set(changed_paths)
        and comment.path not in set(related_paths)
        and not already_reviewed
    ):
        return LaterFindingClassification(
            fingerprint=lifecycle.fingerprint,
            classification="pre-existing",
            is_late_relative_to_baseline=False,
            is_duplicate=False,
            lineage_reason="unattributed",
            attribution="pre-existing",
        )
    if parent is not None and lineage in {
        "fix-introduced-on-changed-path",
        "fix-introduced-on-related-path",
    }:
        return LaterFindingClassification(
            fingerprint=lifecycle.fingerprint,
            classification="new-regression",
            is_late_relative_to_baseline=True,
            is_duplicate=False,
            late_reason="new-regression",
            causal_parent=parent.fingerprint,
            lineage_reason=lineage,
            attribution="fix-regression",
        )
    if on_changed_path:
        return LaterFindingClassification(
            fingerprint=lifecycle.fingerprint,
            classification="needs-human",
            is_late_relative_to_baseline=True,
            is_duplicate=False,
            late_reason="human-adjudication",
            lineage_reason="none",
            attribution="pr-change",
        )
    return LaterFindingClassification(
        fingerprint=lifecycle.fingerprint,
        classification="substantiated-missed-defect",
        is_late_relative_to_baseline=True,
        is_duplicate=False,
        late_reason="substantiated-missed-defect",
        lineage_reason="missed-on-already-reviewed-path",
        attribution="pr-change",
    )


def classify_omitted_finding(
    finding: BaselineFinding,
    *,
    scope: VerificationScope,
    evidence_confirmed: bool = False,
    evidence_criterion: str | None = None,
    path_reviewed: bool = True,
) -> LaterFindingClassification:
    """Omission on a later pass is never proof the original concern is fixed."""

    if not isinstance(finding, BaselineFinding):
        raise ReviewInputError("baseline finding is invalid")
    if not isinstance(scope, VerificationScope):
        raise ReviewInputError("verification scope is invalid")
    _require_bool(evidence_confirmed, label="evidence_confirmed")
    if evidence_confirmed:
        _require_evidence_criterion(
            finding,
            evidence_confirmed=evidence_confirmed,
            evidence_criterion=evidence_criterion,
        )
        return LaterFindingClassification(
            fingerprint=finding.fingerprint,
            classification="verified-fixed",
            is_late_relative_to_baseline=False,
            is_duplicate=True,
            causal_parent=finding.fingerprint,
            lineage_reason="same-concern",
        )
    if not path_reviewed:
        return LaterFindingClassification(
            fingerprint=finding.fingerprint,
            classification="continuing-concern",
            is_late_relative_to_baseline=False,
            is_duplicate=True,
            causal_parent=finding.fingerprint,
            lineage_reason="same-concern",
        )
    return LaterFindingClassification(
        fingerprint=finding.fingerprint,
        classification="omitted-uncertain",
        is_late_relative_to_baseline=False,
        is_duplicate=True,
        causal_parent=finding.fingerprint,
        lineage_reason="same-concern",
    )


def candidate_from_later_finding(
    comment: ReviewComment,
    classification: LaterFindingClassification,
    *,
    on_changed_path: bool = False,
    evidence_locations_validated: bool = False,
    has_failure_condition: bool = False,
    has_independent_artifact: bool = False,
    has_actionable_remedy: bool | None = None,
    has_contradictory_evidence: bool = False,
    has_specific_violation: bool | None = None,
    has_required_contract: bool | None = None,
) -> BlockerCandidate:
    """Map a later-finding classification onto C2 admission facts."""

    if not isinstance(classification, LaterFindingClassification):
        raise ReviewInputError("later finding classification is invalid")
    if (
        classification.is_late_relative_to_baseline
        and classification.late_reason is None
    ):
        raise ReviewInputError(
            "late finding classification requires an explicit late reason"
        )
    if (
        classification.late_reason is not None
        and not classification.is_late_relative_to_baseline
    ):
        raise ReviewInputError(
            "late finding classification reason requires a late finding"
        )
    candidate = derive_blocker_candidate(
        comment,
        on_changed_path=on_changed_path,
        evidence_locations_validated=evidence_locations_validated,
        has_failure_condition=has_failure_condition,
        has_independent_artifact=has_independent_artifact,
        has_actionable_remedy=has_actionable_remedy,
        is_duplicate=classification.is_duplicate,
        has_contradictory_evidence=has_contradictory_evidence,
        is_late_relative_to_baseline=classification.is_late_relative_to_baseline,
        late_reason=classification.late_reason,
        has_specific_violation=has_specific_violation,
        has_required_contract=has_required_contract,
    )
    return replace(
        candidate,
        attribution=classification.attribution,
        needs_human=classification.classification == "needs-human",
    )


__all__ = [
    "BaselineFinding",
    "FINDING_CLASSIFICATIONS",
    "INVALIDATION_REASONS",
    "LaterFindingClassification",
    "LINEAGE_REASONS",
    "MAX_HISTORY_FINDINGS",
    "MAX_HISTORY_READ_FINDINGS",
    "MAX_VERIFICATION_CONCERNS",
    "PUBLIC_SCHEMA_VERSION",
    "ReviewBaseline",
    "SCOPE_STATUSES",
    "VerificationScope",
    "baseline_finding_from_comment",
    "baseline_from_review",
    "candidate_from_later_finding",
    "classify_later_finding",
    "classify_omitted_finding",
    "evaluate_baseline_compatibility",
    "match_baseline_finding",
    "plan_verification_scope",
    "preview_verification_scope",
    "resolution_criterion_digest",
]
