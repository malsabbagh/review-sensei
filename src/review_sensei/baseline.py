"""Baseline-aware verification planning for issue #136 C4.

A complete compatible review is the last-assessed baseline. Later operator
passes reuse ADR 0042 incremental plans and ADR 0037/#40 related paths to
cover existing concerns plus changed and impacted code. Late blockers need an
explicit trusted reason and optional causal lineage. Omission is not a fix.
``legacy`` stays unscoped. Round refusal remains C5.
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
            raise ReviewInputError(f"{label} exceed the metadata bound")
        validate_repository_path(path, label=label)
        if path not in seen:
            seen.add(path)
            paths.append(path)
    return tuple(paths)


def _bounded_related_paths(values: Sequence[str], *, label: str) -> tuple[str, ...]:
    paths = _bounded_paths(values, label=label)
    if len(paths) > MAX_RELATED_PATHS:
        raise ReviewInputError(f"{label} exceed the related-path bound")
    return paths


def _merge_related_paths(*groups: Sequence[str]) -> tuple[str, ...]:
    paths: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for path in group:
            if path in seen:
                continue
            if len(paths) >= MAX_RELATED_PATHS:
                raise ReviewInputError("related paths exceed the related-path bound")
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
    coverage_complete = result.coverage is None or coverage_state == "reviewed"
    complete = result.review_status == "complete" and coverage_complete
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
                    raise ReviewInputError("reviewed paths exceed the metadata bound")
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
            "fallback-full",
        }:
            raise ReviewInputError(
                "verification requires incremental or fallback-full coverage"
            )
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
    """Doctor/plan preview when a full ``ReviewBaseline`` is not available."""

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
    if completed_initial_reviews is None or completed_initial_reviews < 1:
        return _scope(
            status="baseline-required",
            round_kind="initial",
            late_admission_required=False,
            coverage_mode="full",
            invalidation_reason="missing-baseline",
            reviewed_paths=changed,
            related_paths=related,
            changed_paths=changed,
        )
    reviewed = _unique_paths(changed, related)
    return _scope(
        status="verify",
        round_kind="verification",
        late_admission_required=True,
        coverage_mode="incremental",
        reviewed_paths=reviewed,
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
    extra_related = (
        _bounded_related_paths(related_paths_for_change(changed), label="related")
        if related_paths is None
        else _bounded_related_paths(related_paths, label="related")
    )
    if policy.mode not in OPERATOR_REVIEW_MODES:
        return preview_verification_scope(
            policy=policy, changed_paths=changed, related_paths=extra_related
        )
    if baseline is None:
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
    related = _merge_related_paths(baseline.related_paths, extra_related)
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
        return _scope(
            status=status,
            round_kind="initial",
            late_admission_required=False,
            coverage_mode="fallback-full",
            invalidation_reason=reason,
            reviewed_paths=changed,
            related_paths=related,
            changed_paths=changed,
            existing_concerns=len(baseline.findings),
        )
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
        coverage_mode="incremental" if context_complete else "fallback-full",
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


def _match_baseline_finding(
    comment: ReviewComment, baseline: ReviewBaseline
) -> BaselineFinding | None:
    lifecycle = finding_lifecycle_for_comment(comment)
    for finding in baseline.findings:
        if finding.fingerprint == lifecycle.fingerprint:
            return finding
    if lifecycle.concern is not None:
        for finding in baseline.findings:
            if finding.concern == lifecycle.concern:
                return finding
    key = (comment.path, comment.symbol or "", comment_defect_kind(comment))
    matches = [
        finding for finding in baseline.findings if finding.identity_key() == key
    ]
    if len(matches) == 1:
        return matches[0]
    return None


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
        for finding in baseline.findings:
            if finding.path in changed and _shares_lineage_identity(comment, finding):
                return finding, "fix-introduced-on-changed-path"
    if comment.path in related:
        for finding in baseline.findings:
            if (
                finding.blocking
                and (finding.path in changed or finding.path in related)
                and _shares_lineage_identity(comment, finding)
            ):
                return finding, "fix-introduced-on-related-path"
        for finding in baseline.findings:
            if (
                finding.path in changed or finding.path in related
            ) and _shares_lineage_identity(comment, finding):
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
    lifecycle = finding_lifecycle_for_comment(comment)
    if scope.status != "verify":
        return LaterFindingClassification(
            fingerprint=lifecycle.fingerprint,
            classification="new-on-initial-pass",
            is_late_relative_to_baseline=False,
            is_duplicate=False,
            attribution="pr-change" if on_changed_path else "unattributed",
        )
    matches = [
        finding
        for finding in baseline.findings
        if finding.identity_key()
        == (comment.path, comment.symbol or "", comment_defect_kind(comment))
    ]
    if len(matches) > 1:
        return LaterFindingClassification(
            fingerprint=lifecycle.fingerprint,
            classification="needs-human",
            is_late_relative_to_baseline=True,
            is_duplicate=False,
            lineage_reason="ambiguous-identity",
            attribution="unattributed",
        )
    matched = _match_baseline_finding(comment, baseline)
    if matched is not None:
        same = matched.fingerprint == lifecycle.fingerprint
        if evidence_confirmed and matched.concern is not None:
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
            classification="new-regression",
            is_late_relative_to_baseline=True,
            is_duplicate=False,
            late_reason="new-regression",
            lineage_reason=lineage if parent is not None else "none",
            attribution="fix-regression" if parent is not None else "pr-change",
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
    path_reviewed: bool = True,
) -> LaterFindingClassification:
    """Omission on a later pass is never proof the original concern is fixed."""

    if not isinstance(finding, BaselineFinding):
        raise ReviewInputError("baseline finding is invalid")
    if not isinstance(scope, VerificationScope):
        raise ReviewInputError("verification scope is invalid")
    if evidence_confirmed and finding.concern is not None:
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
    candidate = derive_blocker_candidate(
        comment,
        on_changed_path=on_changed_path,
        evidence_locations_validated=evidence_locations_validated,
        has_failure_condition=has_failure_condition,
        has_independent_artifact=has_independent_artifact,
        has_actionable_remedy=has_actionable_remedy,
        is_duplicate=classification.is_duplicate,
        has_contradictory_evidence=has_contradictory_evidence
        or classification.classification == "needs-human",
        is_late_relative_to_baseline=classification.is_late_relative_to_baseline,
        late_reason=classification.late_reason,
        has_specific_violation=has_specific_violation,
        has_required_contract=has_required_contract,
    )
    return replace(candidate, attribution=classification.attribution)


__all__ = [
    "BaselineFinding",
    "FINDING_CLASSIFICATIONS",
    "INVALIDATION_REASONS",
    "LaterFindingClassification",
    "LINEAGE_REASONS",
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
    "plan_verification_scope",
    "preview_verification_scope",
    "resolution_criterion_digest",
]
