"""Deterministic, fail-closed approval policy for GitHub review publication."""

from __future__ import annotations

from dataclasses import dataclass

from ...models import ReviewResult


@dataclass(frozen=True)
class AutoApprovalDecision:
    """Explain whether a validated review may use GitHub's APPROVE event."""

    approved: bool
    blockers: tuple[str, ...] = ()


def evaluate_auto_approval(
    *,
    enabled: bool = True,
    app_authored: bool,
    result: ReviewResult,
    has_open_review_threads: bool | None = None,
) -> AutoApprovalDecision:
    """Apply the repository's conservative approval criteria.

    A finding blocks when it is explicitly classified as blocking. When the
    optional classification is absent, only canonical critical/high severity
    blocks. Missing, lower-severity, and legacy free-form severity values are
    non-blocking. ``has_open_review_threads`` must describe only unresolved
    *blocking ReviewSensei findings*; non-blocking and human threads are not
    automatic-approval blockers.
    """

    blockers: list[str] = []
    if not isinstance(enabled, bool):
        blockers.append("auto-approval-enabled-invalid")
    if not isinstance(app_authored, bool):
        blockers.append("app-authored-flag-invalid")
    if has_open_review_threads is not None and not isinstance(
        has_open_review_threads, bool
    ):
        blockers.append("review-threads-invalid")
    if not isinstance(result, ReviewResult):
        blockers.append("review-result-invalid")
    # Approval remains the compatible default. A repository may explicitly
    # disable it while retaining the same review-publication and
    # marker/idempotency guarantees.
    if enabled is not True:
        blockers.append("auto-approval-disabled")
    if app_authored is True:
        blockers.append("app-authored-pull-request")
    if isinstance(result, ReviewResult) and has_blocking_findings(result):
        blockers.append("blocking-findings-open")
    if has_open_review_threads is None:
        blockers.append("review-threads-incomplete")
    elif has_open_review_threads is True:
        blockers.append("review-threads-open")
    status = (
        result.review_status
        if isinstance(result, ReviewResult)
        else getattr(result, "review_status", "incomplete")
    )
    if status in {"partial", "incomplete", "summary-only"}:
        blockers.append(f"review-{status}")
    elif status != "complete":
        blockers.append("review-status-invalid")
    policy = (
        result.evidence_policy
        if isinstance(result, ReviewResult)
        else getattr(result, "evidence_policy", None)
    )
    if policy not in {"legacy", "confirmed"}:
        blockers.append("evidence-policy-invalid")
    # Confirmed reviews that are not fully verified keep the status blocker
    # above and add review-unverified so callers can distinguish evidence-
    # gated partial coverage from other partial reviews.
    elif policy == "confirmed" and status != "complete":
        blockers.append("review-unverified")
    return AutoApprovalDecision(approved=not blockers, blockers=tuple(blockers))


def has_blocking_findings(result: ReviewResult) -> bool:
    """Whether a validated result contains a finding that blocks approval."""

    return any(comment.blocks_approval for comment in result.comments)


__all__ = [
    "AutoApprovalDecision",
    "evaluate_auto_approval",
    "has_blocking_findings",
]
