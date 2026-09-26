"""Deterministic, fail-closed approval policy for GitHub review publication."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping

from ...coverage import coverage_approval_state
from ...errors import ReviewInputError
from ...models import ReviewResult

APPROVAL_ELIGIBILITY_SCHEMA_VERSION = "1"
REVIEW_STATUSES = frozenset({"complete", "partial", "incomplete", "summary-only"})
EVIDENCE_POLICIES = frozenset({"legacy", "confirmed"})
# ``not-required`` covers backends without a published qualification slice.
# ``qualified`` requires a verified supported record for the trusted effective
# configuration; ``missing`` and ``unverified`` both withhold approval but stay
# distinguishable so operators can see whether evidence was absent or wrong.
QUALIFICATION_STATES = frozenset({"not-required", "qualified", "missing", "unverified"})
_COVERAGE_BLOCKER_STATES = frozenset(
    {
        "reviewed",
        "partial",
        "incomplete",
        "unknown",
    }
)


@dataclass(frozen=True)
class AutoApprovalDecision:
    """Explain whether a validated review may use GitHub's APPROVE event."""

    approved: bool
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class ApprovalFacts:
    """Everything one approval decision depends on, besides the live thread scan.

    The result-derived fields are frozen at publication time so a delayed
    finalization cannot re-derive eligibility from a different result. The
    unresolved-blocking-thread state is deliberately excluded: the finalizer
    reads it live from the reviewed head instead of trusting a persisted value.
    """

    enabled: bool
    app_authored: bool
    review_status: str
    evidence_policy: str
    has_blocking_findings: bool
    has_human_adjudication_findings: bool
    coverage_blocker: str | None
    qualification: str = "not-required"
    check_published: bool = True
    has_open_review_threads: bool | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": APPROVAL_ELIGIBILITY_SCHEMA_VERSION,
            "enabled": self.enabled,
            "app_authored": self.app_authored,
            "review_status": self.review_status,
            "evidence_policy": self.evidence_policy,
            "has_blocking_findings": self.has_blocking_findings,
            "has_human_adjudication_findings": self.has_human_adjudication_findings,
            "coverage_blocker": self.coverage_blocker,
            "qualification": self.qualification,
            "check_published": self.check_published,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ApprovalFacts":
        """Parse the persisted result-side facts; the thread state is not stored."""

        if not isinstance(value, Mapping):
            raise ReviewInputError("approval facts must be an object")
        expected = {
            "schema_version",
            "enabled",
            "app_authored",
            "review_status",
            "evidence_policy",
            "has_blocking_findings",
            "has_human_adjudication_findings",
            "coverage_blocker",
            "qualification",
            "check_published",
        }
        if set(value) != expected:
            raise ReviewInputError("approval facts must contain the documented fields")
        if value.get("schema_version") != APPROVAL_ELIGIBILITY_SCHEMA_VERSION:
            raise ReviewInputError("approval facts schema_version is invalid")
        for label in (
            "enabled",
            "app_authored",
            "has_blocking_findings",
            "has_human_adjudication_findings",
            "check_published",
        ):
            if not isinstance(value.get(label), bool):
                raise ReviewInputError(f"approval facts {label} must be a boolean")
        if value.get("review_status") not in REVIEW_STATUSES:
            raise ReviewInputError("approval facts review_status is invalid")
        if value.get("evidence_policy") not in EVIDENCE_POLICIES:
            raise ReviewInputError("approval facts evidence_policy is invalid")
        coverage_blocker = value.get("coverage_blocker")
        if coverage_blocker is not None and coverage_blocker not in (
            _COVERAGE_BLOCKER_STATES
        ):
            raise ReviewInputError("approval facts coverage_blocker is invalid")
        if value.get("qualification") not in QUALIFICATION_STATES:
            raise ReviewInputError("approval facts qualification is invalid")
        return cls(
            enabled=value["enabled"],
            app_authored=value["app_authored"],
            review_status=value["review_status"],
            evidence_policy=value["evidence_policy"],
            has_blocking_findings=value["has_blocking_findings"],
            has_human_adjudication_findings=value["has_human_adjudication_findings"],
            coverage_blocker=coverage_blocker,
            qualification=value["qualification"],
            check_published=value["check_published"],
        )


@dataclass(frozen=True)
class ReviewApprovalEligibility:
    """Trusted, exact-head eligibility carried from publication to finalization.

    The document is durable: it is persisted beside the published review and
    parsed back before a delayed finalization, so a missing, malformed, or
    stale document withholds approval instead of implying it.
    """

    head_sha: str
    result_digest: str
    facts: ApprovalFacts

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": APPROVAL_ELIGIBILITY_SCHEMA_VERSION,
            "head_sha": self.head_sha,
            "result_digest": self.result_digest,
            "facts": self.facts.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: object) -> "ReviewApprovalEligibility":
        if not isinstance(value, Mapping):
            raise ReviewInputError("approval eligibility must be an object")
        if set(value) != {"schema_version", "head_sha", "result_digest", "facts"}:
            raise ReviewInputError(
                "approval eligibility must contain the documented fields"
            )
        if value.get("schema_version") != APPROVAL_ELIGIBILITY_SCHEMA_VERSION:
            raise ReviewInputError("approval eligibility schema_version is invalid")
        head_sha = value.get("head_sha")
        result_digest = value.get("result_digest")
        if (
            not isinstance(head_sha, str)
            or len(head_sha) != 40
            or any(character not in "0123456789abcdef" for character in head_sha)
            or not isinstance(result_digest, str)
            or len(result_digest) != 64
            or any(character not in "0123456789abcdef" for character in result_digest)
        ):
            raise ReviewInputError("approval eligibility identity is invalid")
        return cls(
            head_sha=head_sha,
            result_digest=result_digest,
            facts=ApprovalFacts.from_dict(value.get("facts")),
        )

    def evaluate(
        self, *, app_authored: bool, has_open_review_threads: bool | None
    ) -> AutoApprovalDecision:
        """Evaluate the persisted facts with the live exact-head thread state."""

        return evaluate_approval_facts(
            replace(
                self.facts,
                app_authored=app_authored,
                has_open_review_threads=has_open_review_threads,
            )
        )


def approval_facts_from_result(
    result: ReviewResult,
    *,
    enabled: bool,
    app_authored: bool,
    qualification: str = "not-required",
    check_published: bool = True,
    has_open_review_threads: bool | None = None,
) -> ApprovalFacts:
    """Freeze the result-derived approval inputs for one validated review."""

    if not isinstance(result, ReviewResult):
        raise ReviewInputError("approval facts require a validated review result")
    coverage_state = (
        coverage_approval_state(result.coverage)
        if result.coverage is not None
        else None
    )
    return ApprovalFacts(
        enabled=enabled,
        app_authored=app_authored,
        review_status=result.review_status,
        evidence_policy=result.evidence_policy,
        has_blocking_findings=has_blocking_findings(result),
        has_human_adjudication_findings=has_human_adjudication_findings(result),
        coverage_blocker=coverage_state,
        qualification=qualification,
        check_published=check_published,
        has_open_review_threads=has_open_review_threads,
    )


def approval_eligibility_from_result(
    result: ReviewResult,
    *,
    head_sha: str,
    enabled: bool,
    app_authored: bool,
    qualification: str = "not-required",
    check_published: bool = True,
) -> ReviewApprovalEligibility:
    """Build the persisted eligibility document for one published review."""

    from .publication import review_result_digest

    return ReviewApprovalEligibility(
        head_sha=head_sha,
        result_digest=review_result_digest(result),
        facts=approval_facts_from_result(
            result,
            enabled=enabled,
            app_authored=app_authored,
            qualification=qualification,
            check_published=check_published,
        ),
    )


def evaluate_approval_facts(facts: ApprovalFacts) -> AutoApprovalDecision:
    """Apply the repository's conservative approval criteria to frozen facts."""

    blockers: list[str] = []
    if not isinstance(facts.enabled, bool):
        blockers.append("auto-approval-enabled-invalid")
    if not isinstance(facts.app_authored, bool):
        blockers.append("app-authored-flag-invalid")
    if facts.has_open_review_threads is not None and not isinstance(
        facts.has_open_review_threads, bool
    ):
        blockers.append("review-threads-invalid")
    # Approval remains the compatible default. A repository may explicitly
    # disable it while retaining the same review-publication and
    # marker/idempotency guarantees.
    if facts.enabled is not True:
        blockers.append("auto-approval-disabled")
    if facts.app_authored is True:
        blockers.append("app-authored-pull-request")
    if facts.check_published is not True:
        blockers.append("check-not-published")
    if facts.has_blocking_findings is True:
        blockers.append("blocking-findings-open")
    if facts.has_human_adjudication_findings is True:
        blockers.append("human-adjudication-open")
    if facts.has_open_review_threads is None:
        blockers.append("review-threads-incomplete")
    elif facts.has_open_review_threads is True:
        blockers.append("review-threads-open")
    status = facts.review_status
    if status in {"partial", "incomplete", "summary-only"}:
        blockers.append(f"review-{status}")
    elif status != "complete":
        blockers.append("review-status-invalid")
    if facts.evidence_policy not in {"legacy", "confirmed"}:
        blockers.append("evidence-policy-invalid")
    # Confirmed reviews that are not fully verified keep the status blocker
    # above and add review-unverified so callers can distinguish evidence-
    # gated partial coverage from other partial reviews.
    elif facts.evidence_policy == "confirmed" and status != "complete":
        blockers.append("review-unverified")
    if facts.coverage_blocker is not None and facts.coverage_blocker != "reviewed":
        blockers.append(f"coverage-{facts.coverage_blocker}")
    if facts.qualification not in {"not-required", "qualified"}:
        blockers.append(f"qualification-{facts.qualification}")
    return AutoApprovalDecision(approved=not blockers, blockers=tuple(blockers))


def qualification_state_for_configuration(
    *,
    provider: str,
    model: str | None = None,
    upstream_provider: str | None = None,
    base_url: str | None = None,
    qualification_status: str = "unknown",
) -> str:
    """Map one effective provider configuration onto a qualification state.

    The qualification contract is scoped to the backends a published
    qualification slice covers, so every other backend is ``not-required``.
    For an OpenRouter deployment the slice key is the full model, upstream
    provider, and base URL combination: a configuration the published slices do
    not cover is ``unverified`` because no published evidence could apply to
    it, and a covered configuration is ``missing`` until the deployment itself
    declares verified qualification.
    """

    from ...openrouter_qualification import PUBLISHED_QUALIFICATION_SLICES

    if not isinstance(provider, str) or provider.strip().lower() != "openrouter":
        return "not-required"
    slice_key = (
        model if isinstance(model, str) else None,
        upstream_provider if isinstance(upstream_provider, str) else None,
        base_url if isinstance(base_url, str) else None,
    )
    if slice_key not in PUBLISHED_QUALIFICATION_SLICES:
        return "unverified"
    if qualification_status == "qualified":
        return "qualified"
    return "missing"


def qualification_state_for_publication(
    *,
    provider: str,
    model: str | None,
    provider_identity: Mapping[str, object] | None = None,
) -> str:
    """Derive the qualification verdict for one published review.

    ``provider_identity`` is the trusted provider document the reviewed run was
    bound to (``configuration_context['provider']``), which is the only source
    that carries the endpoint and upstream routing a qualification slice is
    keyed by. Without it the review's own provider and model are all that can
    be established: enough to clear a backend with no qualification contract,
    and never enough to claim qualification for an OpenRouter deployment.
    """

    from ...providers.profiles import get_provider_profile

    identity = provider_identity if isinstance(provider_identity, Mapping) else {}
    base_url = identity.get("base_url")
    if not isinstance(base_url, str):
        base_url = None
    policy = identity.get("openrouter_policy")
    upstream_provider = (
        policy.get("upstream_provider") if isinstance(policy, Mapping) else None
    )
    profile_name = identity.get("profile")
    qualification_status = "unknown"
    if isinstance(profile_name, str) and profile_name.strip():
        try:
            qualification_status = get_provider_profile(
                profile_name
            ).qualification_status
        except ReviewInputError:
            qualification_status = "unknown"
    return qualification_state_for_configuration(
        provider=provider,
        model=model,
        upstream_provider=upstream_provider,
        base_url=base_url,
        qualification_status=qualification_status,
    )


def evaluate_auto_approval(
    *,
    enabled: bool = True,
    app_authored: bool,
    result: ReviewResult,
    has_open_review_threads: bool | None = None,
    qualification: str = "not-required",
) -> AutoApprovalDecision:
    """Apply the repository's conservative approval criteria.

    A finding blocks when it is explicitly classified as blocking. When the
    optional classification is absent, only canonical critical/high severity
    blocks. Missing, lower-severity, and legacy free-form severity values are
    non-blocking. ``has_open_review_threads`` must describe only unresolved
    *blocking ReviewSensei findings*; non-blocking and human threads are not
    automatic-approval blockers. ``qualification`` carries the #115
    OpenRouter qualification verdict; a configuration outside the qualified
    slice or without verified evidence withholds approval.
    """

    if not isinstance(result, ReviewResult):
        # An unvalidated result keeps the original vocabulary: the caller can
        # still tell an invalid input apart from a disabled approval, and none
        # of the result-derived checks below can run.
        blockers: list[str] = []
        if not isinstance(enabled, bool):
            blockers.append("auto-approval-enabled-invalid")
        if not isinstance(app_authored, bool):
            blockers.append("app-authored-flag-invalid")
        if has_open_review_threads is not None and not isinstance(
            has_open_review_threads, bool
        ):
            blockers.append("review-threads-invalid")
        blockers.append("review-result-invalid")
        if enabled is not True:
            blockers.append("auto-approval-disabled")
        if app_authored is True:
            blockers.append("app-authored-pull-request")
        if has_open_review_threads is None:
            blockers.append("review-threads-incomplete")
        elif has_open_review_threads is True:
            blockers.append("review-threads-open")
        blockers.append("review-incomplete")
        return AutoApprovalDecision(approved=False, blockers=tuple(blockers))
    return evaluate_approval_facts(
        approval_facts_from_result(
            result,
            enabled=enabled,
            app_authored=app_authored,
            qualification=qualification,
            has_open_review_threads=has_open_review_threads,
        )
    )


def has_blocking_findings(result: ReviewResult) -> bool:
    """Whether a validated result contains a finding that blocks approval."""

    return any(comment.blocks_approval for comment in result.comments)


def has_human_adjudication_findings(result: ReviewResult) -> bool:
    """Whether a validated result still needs a human merge decision."""

    return any(comment.needs_human for comment in result.comments)


_WITHHELD_DIAGNOSTICS = {
    "auto-approval-disabled": "auto_approval_disabled",
    "auto-approval-enabled-invalid": "approval_withheld",
    "app-authored-pull-request": "app_authored",
    "app-authored-flag-invalid": "approval_withheld",
    "blocking-findings-open": "required_fixes_open",
    "check-not-published": "check_permission",
    "human-adjudication-open": "human_adjudication_open",
    "review-threads-incomplete": "review_threads_incomplete",
    "review-threads-invalid": "approval_withheld",
    "review-threads-open": "required_fixes_open",
    "review-partial": "review_incomplete",
    "review-incomplete": "review_incomplete",
    "review-summary-only": "review_incomplete",
    "review-status-invalid": "review_incomplete",
    "review-unverified": "review_incomplete",
    "evidence-policy-invalid": "review_incomplete",
    "coverage-partial": "review_incomplete",
    "coverage-incomplete": "review_incomplete",
    "coverage-unknown": "review_incomplete",
    "qualification-missing": "qualification_unverified",
    "qualification-unverified": "qualification_unverified",
}


def approval_withheld_diagnostic(decision: AutoApprovalDecision) -> str:
    """Map an approval decision's blockers onto one bounded public diagnostic."""

    for blocker in decision.blockers:
        mapped = _WITHHELD_DIAGNOSTICS.get(blocker)
        if mapped is not None:
            return mapped
    return "approval_withheld"


__all__ = [
    "APPROVAL_ELIGIBILITY_SCHEMA_VERSION",
    "ApprovalFacts",
    "AutoApprovalDecision",
    "ReviewApprovalEligibility",
    "approval_eligibility_from_result",
    "approval_facts_from_result",
    "approval_withheld_diagnostic",
    "evaluate_approval_facts",
    "evaluate_auto_approval",
    "has_blocking_findings",
    "has_human_adjudication_findings",
    "qualification_state_for_configuration",
    "qualification_state_for_publication",
]
