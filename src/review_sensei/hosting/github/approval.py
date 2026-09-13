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
    enabled: bool = False,
    app_authored: bool,
    result: ReviewResult,
    has_open_review_threads: bool | None = None,
) -> AutoApprovalDecision:
    """Apply the repository's conservative approval criteria.

    Every inline finding emitted by the review service is an open actionable
    comment until a later GitHub thread-resolution sweep observes it resolved.
    Unknown classification values are intentionally not special-cased here:
    they remain visible to readers, but they cannot weaken the no-open-finding
    approval boundary.
    """

    blockers: list[str] = []
    # Approval is an explicit, repository-controlled capability.  A missing
    # or false opt-in always selects COMMENT while retaining the same review
    # publication path and marker/idempotency guarantees.
    if enabled is not True:
        blockers.append("auto-approval-disabled")
    if app_authored:
        blockers.append("app-authored-pull-request")
    if result.comments:
        blockers.append("inline-findings-open")
    if has_open_review_threads is None:
        blockers.append("review-threads-incomplete")
    elif has_open_review_threads:
        blockers.append("review-threads-open")
    status = getattr(result, "review_status", "complete")
    if status in {"partial", "incomplete", "summary-only"}:
        blockers.append(f"review-{status}")
    elif status != "complete":
        blockers.append("review-status-invalid")
    return AutoApprovalDecision(approved=not blockers, blockers=tuple(blockers))


__all__ = ["AutoApprovalDecision", "evaluate_auto_approval"]
