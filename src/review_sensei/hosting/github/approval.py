"""Deterministic, fail-closed approval policy for GitHub review publication."""

from __future__ import annotations

from dataclasses import dataclass

from ...models import ReviewResult

_BLOCKING_SEVERITY_LEVELS = frozenset(("critical", "high"))


@dataclass(frozen=True)
class AutoApprovalDecision:
    """Explain whether a validated review may use GitHub's APPROVE event."""

    approved: bool
    blockers: tuple[str, ...] = ()


def evaluate_auto_approval(
    *,
    app_authored: bool,
    result: ReviewResult,
    has_open_review_threads: bool,
) -> AutoApprovalDecision:
    """Apply the repository's conservative approval criteria.

    A finding blocks when it is explicitly classified as blocking. When the
    optional classification is absent, only canonical critical/high severity
    blocks. Missing, lower-severity, and legacy free-form severity values are
    non-blocking. Unresolved GitHub review threads remain an independent
    fail-closed gate.
    """

    blockers: list[str] = []
    if app_authored:
        blockers.append("app-authored-pull-request")
    if has_blocking_findings(result):
        blockers.append("blocking-findings-open")
    if has_open_review_threads:
        blockers.append("review-threads-open")
    return AutoApprovalDecision(approved=not blockers, blockers=tuple(blockers))


def has_blocking_findings(result: ReviewResult) -> bool:
    """Whether a validated result contains a finding that blocks approval."""

    return any(
        comment.blocking is True
        or (
            comment.blocking is None
            and comment.severity is not None
            and comment.severity.lower() in _BLOCKING_SEVERITY_LEVELS
        )
        for comment in result.comments
    )


__all__ = [
    "AutoApprovalDecision",
    "evaluate_auto_approval",
    "has_blocking_findings",
]
