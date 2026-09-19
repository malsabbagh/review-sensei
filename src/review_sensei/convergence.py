"""Deterministic review-convergence policy and pure admission evaluators.

Issue #136 C1 defines a versioned trusted-configuration contract for review
modes, blocker admission, round counting, and human handoff.  These helpers
compute decisions from structured facts.  They do not parse free-text or
trust a model ``blocking`` boolean as authority outside ``legacy``.

C2 sits ``evaluate_blocker_admission`` between candidate findings and
publication.  ``legacy`` keeps ADR 0032/0035 events.  Operator modes apply
the evaluator before GitHub review events and comment rendering.
``REVIEWSENSEI_AUTO_APPROVE`` default-on semantics are unchanged; advisory
mode additionally withholds automatic GitHub review events.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Mapping, Sequence

from .errors import ReviewInputError
from .models import COMMENT_SIDES, ReviewComment, ReviewResult
from .schemas import validate_public_document

if TYPE_CHECKING:
    from .baseline import ReviewBaseline
    from .context import ReviewContextCacheKey

PUBLIC_SCHEMA_VERSION = "1.0"
REVIEW_MODE_ENV = "REVIEWSENSEI_REVIEW_MODE"
DEFAULT_REVIEW_MODE = "legacy"
REVIEW_MODES = frozenset({"legacy", "advisory", "merge-focused", "strict"})
OPERATOR_REVIEW_MODES = frozenset({"advisory", "merge-focused", "strict"})
ENFORCEMENT_MODES = frozenset({"display-only", "publication"})
DEFAULT_MAX_COMPLETED_INITIAL_REVIEWS = 1
DEFAULT_MAX_COMPLETED_VERIFICATION_ROUNDS = 2
DEFAULT_MAX_FAILED_ATTEMPTS = 6
MAX_COMPLETED_INITIAL_REVIEWS = 8
MAX_COMPLETED_VERIFICATION_ROUNDS = 8
MAX_FAILED_ATTEMPTS = 32
MATERIAL_SEVERITIES = frozenset({"high", "critical"})
ATTRIBUTIONS = frozenset(
    {
        "pr-change",
        "fix-regression",
        "documented-exception",
        "pre-existing",
        "already-reviewed-optional",
        "unattributed",
    }
)
LATE_REASONS = frozenset({"new-regression", "substantiated-missed-defect"})
DISPOSITIONS = frozenset(
    {
        "block",
        "advisory",
        "human-adjudication",
        "suppressed-duplicate",
        "honored-disposition",
    }
)
SEVERITY_REASONS = frozenset(
    {
        "legacy-explicit-blocking",
        "legacy-severity-fallback",
        "material-high-critical",
        "named-mandatory-rule",
        "below-material-threshold",
        "preference-or-optional",
        "not-applicable",
    }
)
EVIDENCE_REASONS = frozenset(
    {
        "legacy-unverified",
        "validated-evidence-and-failure-condition",
        "independent-analyzer-or-test-artifact",
        "insufficient-evidence",
        "json-or-line-validity-only",
        "model-confidence-or-agreement-only",
        "not-applicable",
    }
)
SCOPE_REASONS = frozenset(
    {
        "legacy-unscoped",
        "pr-attributed",
        "fix-introduced-regression",
        "documented-exception",
        "pre-existing-unaffected",
        "already-reviewed-optional",
        "unattributed",
        "not-applicable",
    }
)
ADMISSION_REASONS = frozenset(
    {
        "legacy-classification",
        "admitted-blocker",
        "new-regression",
        "substantiated-missed-defect",
        "duplicate-of-existing",
        "authorized-disposition-still-valid",
        "contradictory-evidence-needs-human",
        "weak-high-impact-needs-human",
        "not-admitted-advisory",
    }
)
HANDOFF_REASONS = frozenset(
    {
        "round-budget-exhausted",
        "failed-attempt-budget-exhausted",
        "no-progress",
        "incomplete-coverage",
        "unreviewed-head",
    }
)
ROUND_KINDS = frozenset({"initial", "verification", "none"})


def _require_bool(value: object, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise ReviewInputError(f"{label} must be a boolean")
    return value


def _require_optional_bool(value: object, *, label: str) -> bool | None:
    if value is None:
        return None
    return _require_bool(value, label=label)


def _require_bounded_int(
    value: object,
    *,
    label: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReviewInputError(f"{label} must be an integer")
    if value < minimum or value > maximum:
        raise ReviewInputError(f"{label} is out of range")
    return value


def _optional_token(
    value: object, *, allowed: frozenset[str], label: str
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in allowed:
        raise ReviewInputError(f"{label} is invalid")
    return value


def _token(value: object, *, allowed: frozenset[str], label: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ReviewInputError(f"{label} is invalid")
    return value


def normalize_review_mode(value: object) -> str:
    """Return a canonical review mode or raise ``ReviewInputError``."""

    if value is None:
        return DEFAULT_REVIEW_MODE
    if not isinstance(value, str):
        raise ReviewInputError("review mode must be a string")
    mode = value.strip().lower()
    if not mode:
        return DEFAULT_REVIEW_MODE
    if mode not in REVIEW_MODES:
        raise ReviewInputError("review mode is unsupported")
    return mode


def resolve_review_mode(explicit: str | None = None) -> str:
    """Resolve CLI, environment, then the compatible ``legacy`` default."""

    if explicit is not None:
        return normalize_review_mode(explicit)
    return normalize_review_mode(os.getenv(REVIEW_MODE_ENV))


def _legacy_blocks(*, proposed_blocking: bool | None, severity: str | None) -> bool:
    if proposed_blocking is not None:
        return proposed_blocking
    return severity is not None and severity.strip().lower() in MATERIAL_SEVERITIES


def _material_severity(severity: str | None) -> bool:
    return severity is not None and severity.strip().lower() in MATERIAL_SEVERITIES


def _scope_reason_for(attribution: str) -> str:
    return {
        "pr-change": "pr-attributed",
        "fix-regression": "fix-introduced-regression",
        "documented-exception": "documented-exception",
        "pre-existing": "pre-existing-unaffected",
        "already-reviewed-optional": "already-reviewed-optional",
        "unattributed": "unattributed",
    }[attribution]


@dataclass(frozen=True)
class ReviewConvergencePolicy:
    """Versioned trusted configuration for review-loop convergence."""

    mode: str = DEFAULT_REVIEW_MODE
    enforcement: str = "display-only"
    max_completed_initial_reviews: int = DEFAULT_MAX_COMPLETED_INITIAL_REVIEWS
    max_completed_verification_rounds: int = DEFAULT_MAX_COMPLETED_VERIFICATION_ROUNDS
    max_failed_attempts: int = DEFAULT_MAX_FAILED_ATTEMPTS

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", normalize_review_mode(self.mode))
        _token(self.enforcement, allowed=ENFORCEMENT_MODES, label="enforcement")
        if self.mode in OPERATOR_REVIEW_MODES and self.enforcement != "publication":
            object.__setattr__(self, "enforcement", "publication")
        _require_bounded_int(
            self.max_completed_initial_reviews,
            label="max_completed_initial_reviews",
            minimum=1,
            maximum=MAX_COMPLETED_INITIAL_REVIEWS,
        )
        _require_bounded_int(
            self.max_completed_verification_rounds,
            label="max_completed_verification_rounds",
            minimum=0,
            maximum=MAX_COMPLETED_VERIFICATION_ROUNDS,
        )
        _require_bounded_int(
            self.max_failed_attempts,
            label="max_failed_attempts",
            minimum=1,
            maximum=MAX_FAILED_ATTEMPTS,
        )

    @property
    def automatic_github_review_events(self) -> bool:
        """Return whether this mode may emit approve/request-changes events.

        Advisory is comment-only.  Legacy keeps today's host-controlled
        ``REVIEWSENSEI_AUTO_APPROVE`` path and is not reinterpreted here.
        """

        return self.mode != "advisory"

    @property
    def inline_advisory_threads(self) -> bool:
        """Return whether advisory observations may become inline threads.

        Operator modes prefer a consolidated non-thread summary so GitHub
        required-conversation-resolution cannot turn optional notes into
        mechanical blockers.  Legacy keeps current inline publication.
        """

        return self.mode == "legacy"

    @property
    def compatibility(self) -> str:
        if self.mode == "legacy":
            return "existing-installations-unchanged"
        return "explicit-opt-in"

    def identity_fields(self) -> dict[str, object]:
        return {
            "schema_version": PUBLIC_SCHEMA_VERSION,
            "mode": self.mode,
            "enforcement": self.enforcement,
            "max_completed_initial_reviews": self.max_completed_initial_reviews,
            "max_completed_verification_rounds": self.max_completed_verification_rounds,
            "max_failed_attempts": self.max_failed_attempts,
            "automatic_github_review_events": self.automatic_github_review_events,
            "inline_advisory_threads": self.inline_advisory_threads,
        }

    def digest(self) -> str:
        payload = json.dumps(
            self.identity_fields(), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, object]:
        value = dict(self.identity_fields())
        value["policy_digest"] = self.digest()
        validate_public_document(value, "review-convergence-policy")
        return value

    def doctor_detail(self) -> str:
        return (
            f"mode={self.mode} enforcement={self.enforcement} "
            f"compatibility={self.compatibility} "
            f"initial={self.max_completed_initial_reviews} "
            f"verification={self.max_completed_verification_rounds} "
            f"failed_attempts={self.max_failed_attempts}"
        )


def publication_enforcement_for_mode(mode: str) -> str:
    """Return C2 enforcement for a resolved review mode."""

    return "display-only" if mode == "legacy" else "publication"


def resolve_review_convergence_policy(
    *,
    mode: str | None = None,
    enforcement: str | None = None,
) -> ReviewConvergencePolicy:
    """Resolve the trusted review-convergence policy for doctor, plan, and publication."""

    resolved_mode = resolve_review_mode(mode)
    if enforcement is None:
        enforcement = publication_enforcement_for_mode(resolved_mode)
    return ReviewConvergencePolicy(
        mode=resolved_mode,
        enforcement=enforcement,
    )


@dataclass(frozen=True)
class BlockerCandidate:
    """Structured facts for deterministic blocker admission.

    C2 derives these facts from structured comment and verification fields.
    The evaluator never parses free-text or trusts a model boolean.
    """

    proposed_blocking: bool | None = None
    severity: str | None = None
    has_specific_violation: bool = False
    has_required_contract: bool = False
    named_mandatory_rule: str | None = None
    has_actionable_remedy: bool = False
    evidence_locations_validated: bool = False
    has_failure_condition: bool = False
    has_independent_artifact: bool = False
    evidence_is_json_or_line_validity_only: bool = False
    evidence_is_model_confidence_or_agreement_only: bool = False
    attribution: str = "unattributed"
    is_duplicate: bool = False
    has_authorized_disposition: bool = False
    has_contradictory_evidence: bool = False
    is_preference_or_optional: bool = False
    is_late_relative_to_baseline: bool = False
    late_reason: str | None = None
    high_impact_weakly_supported: bool = False
    path: str | None = None
    line: int | None = None
    side: str | None = None

    def __post_init__(self) -> None:
        _require_optional_bool(self.proposed_blocking, label="proposed_blocking")
        if self.severity is not None:
            if not isinstance(self.severity, str) or not self.severity.strip():
                raise ReviewInputError("severity must be a non-empty string")
            if not self.severity.isprintable():
                raise ReviewInputError(
                    "severity contains a forbidden control character"
                )
        for label in (
            "has_specific_violation",
            "has_required_contract",
            "has_actionable_remedy",
            "evidence_locations_validated",
            "has_failure_condition",
            "has_independent_artifact",
            "evidence_is_json_or_line_validity_only",
            "evidence_is_model_confidence_or_agreement_only",
            "is_duplicate",
            "has_authorized_disposition",
            "has_contradictory_evidence",
            "is_preference_or_optional",
            "is_late_relative_to_baseline",
            "high_impact_weakly_supported",
        ):
            _require_bool(getattr(self, label), label=label)
        if self.named_mandatory_rule is not None:
            if (
                not isinstance(self.named_mandatory_rule, str)
                or not self.named_mandatory_rule.strip()
                or not self.named_mandatory_rule.isprintable()
            ):
                raise ReviewInputError("named_mandatory_rule is invalid")
        _token(self.attribution, allowed=ATTRIBUTIONS, label="attribution")
        _optional_token(self.late_reason, allowed=LATE_REASONS, label="late_reason")
        if self.path is not None:
            from .validation import validate_repository_path

            validate_repository_path(self.path, label="blocker candidate path")
        if self.side is not None and self.side not in COMMENT_SIDES:
            raise ReviewInputError(
                "blocker candidate side must be LEFT, RIGHT, or FILE"
            )
        if self.line is not None:
            if (
                isinstance(self.line, bool)
                or not isinstance(self.line, int)
                or self.line < 1
            ):
                raise ReviewInputError(
                    "blocker candidate line must be a positive integer"
                )


@dataclass(frozen=True)
class BlockerAdmissionDecision:
    """Effective disposition computed from trusted policy, not model output."""

    mode: str
    proposed_blocking: bool | None
    effective_blocking: bool
    needs_human: bool
    withholds_automatic_approval: bool
    automatic_github_review_events: bool
    inline_advisory_threads: bool
    disposition: str
    severity_reason: str
    evidence_reason: str
    scope_reason: str
    admission_reason: str

    def __post_init__(self) -> None:
        normalize_review_mode(self.mode)
        _require_optional_bool(self.proposed_blocking, label="proposed_blocking")
        for label in (
            "effective_blocking",
            "needs_human",
            "withholds_automatic_approval",
            "automatic_github_review_events",
            "inline_advisory_threads",
        ):
            _require_bool(getattr(self, label), label=label)
        _token(self.disposition, allowed=DISPOSITIONS, label="disposition")
        _token(self.severity_reason, allowed=SEVERITY_REASONS, label="severity_reason")
        _token(self.evidence_reason, allowed=EVIDENCE_REASONS, label="evidence_reason")
        _token(self.scope_reason, allowed=SCOPE_REASONS, label="scope_reason")
        _token(
            self.admission_reason, allowed=ADMISSION_REASONS, label="admission_reason"
        )
        if self.effective_blocking and self.disposition != "block":
            raise ReviewInputError("blocking decisions must use the block disposition")
        if self.disposition == "block" and not self.effective_blocking:
            raise ReviewInputError("block disposition requires effective_blocking")
        if self.needs_human and self.effective_blocking:
            raise ReviewInputError(
                "human adjudication must not invent a proven blocker"
            )
        if self.cap_would_approve():  # pragma: no cover - invariant helper
            raise ReviewInputError("admission must not mint approval")

    def cap_would_approve(self) -> bool:
        return False

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": PUBLIC_SCHEMA_VERSION,
            "mode": self.mode,
            "proposed_blocking": self.proposed_blocking,
            "effective_blocking": self.effective_blocking,
            "needs_human": self.needs_human,
            "withholds_automatic_approval": self.withholds_automatic_approval,
            "automatic_github_review_events": self.automatic_github_review_events,
            "inline_advisory_threads": self.inline_advisory_threads,
            "disposition": self.disposition,
            "severity_reason": self.severity_reason,
            "evidence_reason": self.evidence_reason,
            "scope_reason": self.scope_reason,
            "admission_reason": self.admission_reason,
        }
        validate_public_document(value, "blocker-admission")
        return value


def _evidence_assessment(candidate: BlockerCandidate) -> tuple[bool, str]:
    if candidate.evidence_is_json_or_line_validity_only:
        return False, "json-or-line-validity-only"
    if candidate.evidence_is_model_confidence_or_agreement_only:
        return False, "model-confidence-or-agreement-only"
    if candidate.has_independent_artifact:
        return True, "independent-analyzer-or-test-artifact"
    if candidate.evidence_locations_validated and candidate.has_failure_condition:
        return True, "validated-evidence-and-failure-condition"
    return False, "insufficient-evidence"


def _severity_assessment(
    candidate: BlockerCandidate,
    *,
    named_rule: bool,
) -> tuple[bool, str]:
    if candidate.is_preference_or_optional and not named_rule:
        return False, "preference-or-optional"
    if named_rule:
        return True, "named-mandatory-rule"
    if _material_severity(candidate.severity):
        return True, "material-high-critical"
    return False, "below-material-threshold"


def _admission(
    *,
    policy: ReviewConvergencePolicy,
    candidate: BlockerCandidate,
    disposition: str,
    effective_blocking: bool,
    needs_human: bool,
    severity_reason: str,
    evidence_reason: str,
    scope_reason: str,
    admission_reason: str,
) -> BlockerAdmissionDecision:
    withholds = effective_blocking or needs_human
    return BlockerAdmissionDecision(
        mode=policy.mode,
        proposed_blocking=candidate.proposed_blocking,
        effective_blocking=effective_blocking,
        needs_human=needs_human,
        withholds_automatic_approval=withholds,
        automatic_github_review_events=policy.automatic_github_review_events,
        inline_advisory_threads=policy.inline_advisory_threads,
        disposition=disposition,
        severity_reason=severity_reason,
        evidence_reason=evidence_reason,
        scope_reason=scope_reason,
        admission_reason=admission_reason,
    )


def evaluate_blocker_admission(
    candidate: BlockerCandidate,
    policy: ReviewConvergencePolicy,
) -> BlockerAdmissionDecision:
    """Compute effective blocker disposition from trusted policy."""

    if not isinstance(candidate, BlockerCandidate):
        raise ReviewInputError("blocker candidate is invalid")
    if not isinstance(policy, ReviewConvergencePolicy):
        raise ReviewInputError("review convergence policy is invalid")

    if policy.mode == "legacy":
        blocking = _legacy_blocks(
            proposed_blocking=candidate.proposed_blocking,
            severity=candidate.severity,
        )
        severity_reason = (
            "legacy-explicit-blocking"
            if candidate.proposed_blocking is not None
            else "legacy-severity-fallback"
        )
        return _admission(
            policy=policy,
            candidate=candidate,
            disposition="block" if blocking else "advisory",
            effective_blocking=blocking,
            needs_human=False,
            severity_reason=severity_reason,
            evidence_reason="legacy-unverified",
            scope_reason="legacy-unscoped",
            admission_reason="legacy-classification",
        )

    if candidate.is_duplicate:
        return _admission(
            policy=policy,
            candidate=candidate,
            disposition="suppressed-duplicate",
            effective_blocking=False,
            needs_human=False,
            severity_reason="not-applicable",
            evidence_reason="not-applicable",
            scope_reason="not-applicable",
            admission_reason="duplicate-of-existing",
        )
    if candidate.has_authorized_disposition:
        return _admission(
            policy=policy,
            candidate=candidate,
            disposition="honored-disposition",
            effective_blocking=False,
            needs_human=False,
            severity_reason="not-applicable",
            evidence_reason="not-applicable",
            scope_reason="not-applicable",
            admission_reason="authorized-disposition-still-valid",
        )
    if candidate.has_contradictory_evidence:
        return _admission(
            policy=policy,
            candidate=candidate,
            disposition="human-adjudication",
            effective_blocking=False,
            needs_human=True,
            severity_reason="not-applicable",
            evidence_reason="not-applicable",
            scope_reason=_scope_reason_for(candidate.attribution),
            admission_reason="contradictory-evidence-needs-human",
        )
    if candidate.high_impact_weakly_supported:
        return _admission(
            policy=policy,
            candidate=candidate,
            disposition="human-adjudication",
            effective_blocking=False,
            needs_human=True,
            severity_reason=(
                "material-high-critical"
                if _material_severity(candidate.severity)
                else "below-material-threshold"
            ),
            evidence_reason=_evidence_assessment(candidate)[1],
            scope_reason=_scope_reason_for(candidate.attribution),
            admission_reason="weak-high-impact-needs-human",
        )

    named_rule = policy.mode == "strict" and bool(candidate.named_mandatory_rule)
    material, severity_reason = _severity_assessment(candidate, named_rule=named_rule)
    evidence_ok, evidence_reason = _evidence_assessment(candidate)
    violation = (
        candidate.has_specific_violation
        or candidate.has_required_contract
        or named_rule
    )
    if named_rule:
        material = True
        severity_reason = "named-mandatory-rule"

    scope_reason = _scope_reason_for(candidate.attribution)
    attributed = candidate.attribution in {
        "pr-change",
        "fix-regression",
        "documented-exception",
    }
    late_ok = True
    admission_reason = "admitted-blocker"
    if candidate.is_late_relative_to_baseline:
        if candidate.late_reason in LATE_REASONS:
            admission_reason = candidate.late_reason
        else:
            late_ok = False
            scope_reason = "already-reviewed-optional"

    if (
        violation
        and material
        and candidate.has_actionable_remedy
        and evidence_ok
        and attributed
        and late_ok
        and (not candidate.is_preference_or_optional or named_rule)
    ):
        return _admission(
            policy=policy,
            candidate=candidate,
            disposition="block",
            effective_blocking=True,
            needs_human=False,
            severity_reason=severity_reason,
            evidence_reason=evidence_reason,
            scope_reason=scope_reason,
            admission_reason=admission_reason,
        )

    if candidate.is_preference_or_optional and not named_rule:
        severity_reason = "preference-or-optional"
    return _admission(
        policy=policy,
        candidate=candidate,
        disposition="advisory",
        effective_blocking=False,
        needs_human=False,
        severity_reason=severity_reason,
        evidence_reason=evidence_reason,
        scope_reason=scope_reason,
        admission_reason="not-admitted-advisory",
    )


REQUIRED_CONTRACT_KINDS = frozenset(
    {"required-contract", "api-contract", "compatibility-contract"}
)
PREFERENCE_CATEGORIES = frozenset({"style", "nit", "preference"})


def derive_blocker_candidate(
    comment: ReviewComment,
    *,
    on_changed_path: bool = False,
    evidence_locations_validated: bool = False,
    has_failure_condition: bool = False,
    has_independent_artifact: bool = False,
    has_actionable_remedy: bool | None = None,
    evidence_is_json_or_line_validity_only: bool = False,
    evidence_is_model_confidence_or_agreement_only: bool = False,
    is_duplicate: bool = False,
    has_authorized_disposition: bool = False,
    has_contradictory_evidence: bool = False,
    is_late_relative_to_baseline: bool = False,
    late_reason: str | None = None,
    named_mandatory_rule: str | None = None,
    has_required_contract: bool | None = None,
    has_specific_violation: bool | None = None,
) -> BlockerCandidate:
    """Map structured finding fields to admission facts without parsing bodies.

    Free-form ``defect_kind`` is never a specific-violation,
    named-mandatory-rule, or required-contract signal. Callers opt into
    those gates with ``has_specific_violation``, ``named_mandatory_rule``,
    or ``has_required_contract``. ``REQUIRED_CONTRACT_KINDS`` names the
    closed contract kinds callers may opt into; derivation does not sniff
    ``defect_kind`` for them. Preference classification uses
    ``PREFERENCE_CATEGORIES`` only. Derived facts bind ``path`` / ``line``
    / ``side`` so a mis-zipped candidate fails closed.
    """

    if not isinstance(comment, ReviewComment):
        raise ReviewInputError("blocker comment is invalid")
    if has_actionable_remedy is None:
        effort = (comment.fix_effort or "").strip().casefold()
        has_actionable_remedy = bool(effort) and effort != "unknown"
    category = (comment.category or "").strip().casefold()
    preference = category in PREFERENCE_CATEGORIES
    high_impact = _material_severity(comment.severity)
    evidence_ok = has_independent_artifact or (
        evidence_locations_validated and has_failure_condition
    )
    weakly = high_impact and not evidence_ok
    if has_required_contract is None:
        has_required_contract = False
    if has_specific_violation is None:
        has_specific_violation = False
    return BlockerCandidate(
        proposed_blocking=comment.blocking,
        severity=comment.severity,
        has_specific_violation=has_specific_violation,
        has_required_contract=has_required_contract,
        named_mandatory_rule=named_mandatory_rule,
        has_actionable_remedy=has_actionable_remedy,
        evidence_locations_validated=evidence_locations_validated,
        has_failure_condition=has_failure_condition,
        has_independent_artifact=has_independent_artifact,
        evidence_is_json_or_line_validity_only=evidence_is_json_or_line_validity_only,
        evidence_is_model_confidence_or_agreement_only=(
            evidence_is_model_confidence_or_agreement_only
        ),
        attribution="pr-change" if on_changed_path else "unattributed",
        is_duplicate=is_duplicate,
        has_authorized_disposition=has_authorized_disposition,
        has_contradictory_evidence=has_contradictory_evidence,
        is_preference_or_optional=preference,
        is_late_relative_to_baseline=is_late_relative_to_baseline,
        late_reason=late_reason,
        high_impact_weakly_supported=weakly,
        path=comment.path,
        line=comment.line,
        side=comment.side,
    )


def comment_targets_pr_change(
    comment: ReviewComment,
    *,
    changed_lines: Mapping[str, frozenset[int]] | None = None,
    deleted_lines: Mapping[str, frozenset[int]] | None = None,
) -> bool:
    """Return whether a finding targets a changed line on its declared side.

    ``RIGHT`` comments use new-file ``changed_lines``. ``LEFT`` comments use
    old-file ``deleted_lines``. File-level comments are not line-attributed.
    Missing maps fail closed as unattributed.
    """

    if not isinstance(comment, ReviewComment):
        raise ReviewInputError("blocker comment is invalid")
    if comment.line is None:
        return False
    if comment.side == "LEFT":
        if deleted_lines is None:
            return False
        allowed = deleted_lines.get(comment.path)
        return bool(allowed and comment.line in allowed)
    if comment.side == "RIGHT":
        if changed_lines is None:
            return False
        allowed = changed_lines.get(comment.path)
        return bool(allowed and comment.line in allowed)
    return False


def _require_candidate_matches_comment(
    candidate: BlockerCandidate, comment: ReviewComment
) -> None:
    if candidate.path is None:
        raise ReviewInputError("blocker candidate must bind comment identity")
    if candidate.path != comment.path:
        raise ReviewInputError("blocker candidate must match review comment")
    if candidate.side is not None and candidate.side != comment.side:
        raise ReviewInputError("blocker candidate must match review comment")
    if candidate.line is not None and candidate.line != comment.line:
        raise ReviewInputError("blocker candidate must match review comment")


def admit_review_result(
    result: ReviewResult,
    policy: ReviewConvergencePolicy,
    *,
    candidates: Sequence[BlockerCandidate] | None = None,
    changed_lines: Mapping[str, frozenset[int]] | None = None,
    deleted_lines: Mapping[str, frozenset[int]] | None = None,
    baseline: ReviewBaseline | None = None,
    changed_paths: Sequence[str] | None = None,
    related_paths: Sequence[str] = (),
    current_key: ReviewContextCacheKey | None = None,
    evidence_confirmed_concerns: Sequence[str] = (),
) -> ReviewResult:
    """Apply trusted blocker admission to each finding before publication.

    Operator modes always carry ``enforcement="publication"`` (the dataclass
    coerces that invariant), so admission runs. ``legacy`` stays
    ``display-only`` and returns the result unchanged. An optional C4
    ``baseline`` classifies later findings before the evaluator runs.
    Caller-supplied ``candidates`` keep explicit late-admission facts.
    """

    if not isinstance(result, ReviewResult):
        raise ReviewInputError("review result is invalid")
    if not isinstance(policy, ReviewConvergencePolicy):
        raise ReviewInputError("review convergence policy is invalid")
    if policy.enforcement != "publication":
        return result
    if candidates is not None and len(candidates) != len(result.comments):
        raise ReviewInputError("blocker candidates must align with review comments")
    verification_scope = None
    verification_changed: tuple[str, ...] = ()
    if candidates is None and baseline is not None:
        from .baseline import ReviewBaseline, plan_verification_scope
        from .context import ReviewContextCacheKey

        if not isinstance(baseline, ReviewBaseline):
            raise ReviewInputError("review baseline is invalid")
        if current_key is not None and not isinstance(
            current_key, ReviewContextCacheKey
        ):
            raise ReviewInputError("current review cache key is invalid")
        if changed_paths is not None:
            verification_changed = tuple(changed_paths)
        elif changed_lines is not None:
            verification_changed = tuple(changed_lines)
        verification_scope = plan_verification_scope(
            policy=policy,
            baseline=baseline,
            current_key=current_key,
            changed_paths=verification_changed,
            related_paths=None if not related_paths else related_paths,
            confirmed_concerns=evidence_confirmed_concerns,
        )
    admitted: list[ReviewComment] = []
    for index, comment in enumerate(result.comments):
        if candidates is not None:
            candidate = candidates[index]
            _require_candidate_matches_comment(candidate, comment)
        elif verification_scope is not None:
            from .baseline import candidate_from_later_finding, classify_later_finding
            from .context import finding_lifecycle_for_comment

            if baseline is None:
                raise ReviewInputError("review baseline is invalid")
            on_changed_path = comment_targets_pr_change(
                comment,
                changed_lines=changed_lines,
                deleted_lines=deleted_lines,
            )
            lifecycle = finding_lifecycle_for_comment(comment)
            classification = classify_later_finding(
                comment,
                baseline=baseline,
                scope=verification_scope,
                changed_paths=verification_changed,
                related_paths=verification_scope.related_paths,
                evidence_confirmed=bool(
                    lifecycle.concern
                    and lifecycle.concern in set(evidence_confirmed_concerns)
                ),
                on_changed_path=on_changed_path,
                is_preference_or_optional=(comment.category or "").strip().casefold()
                in PREFERENCE_CATEGORIES,
            )
            candidate = candidate_from_later_finding(
                comment,
                classification,
                on_changed_path=on_changed_path,
            )
        else:
            candidate = derive_blocker_candidate(
                comment,
                on_changed_path=comment_targets_pr_change(
                    comment,
                    changed_lines=changed_lines,
                    deleted_lines=deleted_lines,
                ),
            )
        decision = evaluate_blocker_admission(candidate, policy)
        admitted.append(
            replace(
                comment,
                effective_blocking=decision.effective_blocking,
                needs_human=decision.needs_human,
            )
        )
    return replace(result, comments=tuple(admitted))


@dataclass(frozen=True)
class RoundSessionState:
    """PR-wide counters and flags for round admission.  No raw source."""

    completed_initial_reviews: int = 0
    completed_verification_rounds: int = 0
    failed_attempts: int = 0
    same_head_duplicate: bool = False
    publication_recovery: bool = False
    transport_or_structural_retry: bool = False
    no_progress: bool = False
    coverage_complete: bool = False
    independently_approval_eligible: bool = False
    latest_head_reviewed: bool = False

    def __post_init__(self) -> None:
        for label in (
            "completed_initial_reviews",
            "completed_verification_rounds",
            "failed_attempts",
        ):
            _require_bounded_int(
                getattr(self, label),
                label=label,
                minimum=0,
                maximum=MAX_FAILED_ATTEMPTS,
            )
        for label in (
            "same_head_duplicate",
            "publication_recovery",
            "transport_or_structural_retry",
            "no_progress",
            "coverage_complete",
            "independently_approval_eligible",
            "latest_head_reviewed",
        ):
            _require_bool(getattr(self, label), label=label)


@dataclass(frozen=True)
class RoundAdmissionDecision:
    """Whether another autonomous review round may run, and how to hand off."""

    mode: str
    admit: bool
    count_as_completed_round: bool
    round_kind: str
    remaining_initial_reviews: int
    remaining_verification_rounds: int
    handoff: bool
    handoff_reason: str | None
    may_emit_approve: bool
    cap_creates_approval: bool = False

    def __post_init__(self) -> None:
        normalize_review_mode(self.mode)
        for label in (
            "admit",
            "count_as_completed_round",
            "handoff",
            "may_emit_approve",
            "cap_creates_approval",
        ):
            _require_bool(getattr(self, label), label=label)
        _token(self.round_kind, allowed=ROUND_KINDS, label="round_kind")
        _require_bounded_int(
            self.remaining_initial_reviews,
            label="remaining_initial_reviews",
            minimum=0,
            maximum=MAX_COMPLETED_INITIAL_REVIEWS,
        )
        _require_bounded_int(
            self.remaining_verification_rounds,
            label="remaining_verification_rounds",
            minimum=0,
            maximum=MAX_COMPLETED_VERIFICATION_ROUNDS,
        )
        _optional_token(
            self.handoff_reason, allowed=HANDOFF_REASONS, label="handoff_reason"
        )
        if self.cap_creates_approval:
            raise ReviewInputError("round cap must not create approval eligibility")
        if self.admit and self.round_kind == "none":
            raise ReviewInputError("admitted rounds must name a round kind")
        if not self.admit and self.count_as_completed_round:
            raise ReviewInputError("rejected work cannot count as a completed round")

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": PUBLIC_SCHEMA_VERSION,
            "mode": self.mode,
            "admit": self.admit,
            "count_as_completed_round": self.count_as_completed_round,
            "round_kind": self.round_kind,
            "remaining_initial_reviews": self.remaining_initial_reviews,
            "remaining_verification_rounds": self.remaining_verification_rounds,
            "handoff": self.handoff,
            "handoff_reason": self.handoff_reason,
            "may_emit_approve": self.may_emit_approve,
            "cap_creates_approval": False,
        }
        validate_public_document(value, "review-round-decision")
        return value


def _remaining(used: int, budget: int) -> int:
    return max(0, budget - used)


def evaluate_round_admission(
    state: RoundSessionState,
    policy: ReviewConvergencePolicy,
) -> RoundAdmissionDecision:
    """Admit or hand off a logical review/verification round."""

    if not isinstance(state, RoundSessionState):
        raise ReviewInputError("round session state is invalid")
    if not isinstance(policy, ReviewConvergencePolicy):
        raise ReviewInputError("review convergence policy is invalid")

    remaining_initial = _remaining(
        state.completed_initial_reviews, policy.max_completed_initial_reviews
    )
    remaining_verification = _remaining(
        state.completed_verification_rounds, policy.max_completed_verification_rounds
    )
    independently_eligible = (
        state.independently_approval_eligible
        and state.coverage_complete
        and state.latest_head_reviewed
    )

    def decision(
        *,
        admit: bool,
        count: bool,
        kind: str,
        handoff: bool,
        reason: str | None,
        may_approve: bool,
    ) -> RoundAdmissionDecision:
        return RoundAdmissionDecision(
            mode=policy.mode,
            admit=admit,
            count_as_completed_round=count,
            round_kind=kind,
            remaining_initial_reviews=remaining_initial,
            remaining_verification_rounds=remaining_verification,
            handoff=handoff,
            handoff_reason=reason,
            may_emit_approve=may_approve and independently_eligible,
            cap_creates_approval=False,
        )

    if policy.mode == "legacy":
        kind = "initial" if state.completed_initial_reviews == 0 else "verification"
        return decision(
            admit=True,
            count=True,
            kind=kind,
            handoff=False,
            reason=None,
            may_approve=independently_eligible,
        )

    if state.no_progress:
        return decision(
            admit=False,
            count=False,
            kind="none",
            handoff=True,
            reason="no-progress",
            may_approve=False,
        )

    if state.failed_attempts >= policy.max_failed_attempts:
        return decision(
            admit=False,
            count=False,
            kind="none",
            handoff=True,
            reason="failed-attempt-budget-exhausted",
            may_approve=False,
        )

    if (
        state.same_head_duplicate
        or state.publication_recovery
        or state.transport_or_structural_retry
    ):
        return decision(
            admit=False,
            count=False,
            kind="none",
            handoff=False,
            reason=None,
            may_approve=False,
        )

    if remaining_initial > 0:
        return decision(
            admit=True,
            count=True,
            kind="initial",
            handoff=False,
            reason=None,
            may_approve=independently_eligible,
        )
    if remaining_verification > 0:
        return decision(
            admit=True,
            count=True,
            kind="verification",
            handoff=False,
            reason=None,
            may_approve=independently_eligible,
        )

    if not state.latest_head_reviewed:
        return decision(
            admit=False,
            count=False,
            kind="none",
            handoff=True,
            reason="unreviewed-head",
            may_approve=False,
        )
    if not state.coverage_complete:
        return decision(
            admit=False,
            count=False,
            kind="none",
            handoff=True,
            reason="incomplete-coverage",
            may_approve=False,
        )
    return decision(
        admit=False,
        count=False,
        kind="none",
        handoff=True,
        reason="round-budget-exhausted",
        may_approve=independently_eligible,
    )


def policy_from_mapping(value: Mapping[str, object]) -> ReviewConvergencePolicy:
    """Load a policy document without trusting unknown fields."""

    if not isinstance(value, Mapping):
        raise ReviewInputError("review convergence policy must be an object")
    unknown = set(value) - {
        "schema_version",
        "mode",
        "enforcement",
        "max_completed_initial_reviews",
        "max_completed_verification_rounds",
        "max_failed_attempts",
        "automatic_github_review_events",
        "inline_advisory_threads",
        "policy_digest",
    }
    if unknown:
        raise ReviewInputError("review convergence policy contains unknown fields")
    schema_version = value.get("schema_version", PUBLIC_SCHEMA_VERSION)
    if schema_version != PUBLIC_SCHEMA_VERSION:
        raise ReviewInputError(
            "review convergence policy schema_version is unsupported"
        )
    policy = ReviewConvergencePolicy(
        mode=normalize_review_mode(value.get("mode")),
        enforcement=_token(
            value.get("enforcement", "display-only"),
            allowed=ENFORCEMENT_MODES,
            label="enforcement",
        ),
        max_completed_initial_reviews=_require_bounded_int(
            value.get(
                "max_completed_initial_reviews", DEFAULT_MAX_COMPLETED_INITIAL_REVIEWS
            ),
            label="max_completed_initial_reviews",
            minimum=1,
            maximum=MAX_COMPLETED_INITIAL_REVIEWS,
        ),
        max_completed_verification_rounds=_require_bounded_int(
            value.get(
                "max_completed_verification_rounds",
                DEFAULT_MAX_COMPLETED_VERIFICATION_ROUNDS,
            ),
            label="max_completed_verification_rounds",
            minimum=0,
            maximum=MAX_COMPLETED_VERIFICATION_ROUNDS,
        ),
        max_failed_attempts=_require_bounded_int(
            value.get("max_failed_attempts", DEFAULT_MAX_FAILED_ATTEMPTS),
            label="max_failed_attempts",
            minimum=1,
            maximum=MAX_FAILED_ATTEMPTS,
        ),
    )
    declared = value.get("policy_digest")
    if declared is not None and declared != policy.digest():
        raise ReviewInputError("review convergence policy_digest does not match")
    return policy


__all__ = [
    "ADMISSION_REASONS",
    "ATTRIBUTIONS",
    "BlockerAdmissionDecision",
    "BlockerCandidate",
    "DEFAULT_REVIEW_MODE",
    "EVIDENCE_REASONS",
    "HANDOFF_REASONS",
    "OPERATOR_REVIEW_MODES",
    "PREFERENCE_CATEGORIES",
    "PUBLIC_SCHEMA_VERSION",
    "REQUIRED_CONTRACT_KINDS",
    "REVIEW_MODE_ENV",
    "REVIEW_MODES",
    "ReviewConvergencePolicy",
    "RoundAdmissionDecision",
    "RoundSessionState",
    "admit_review_result",
    "comment_targets_pr_change",
    "derive_blocker_candidate",
    "evaluate_blocker_admission",
    "evaluate_round_admission",
    "normalize_review_mode",
    "policy_from_mapping",
    "publication_enforcement_for_mode",
    "resolve_review_convergence_policy",
    "resolve_review_mode",
]
