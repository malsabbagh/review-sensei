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
    # Approval is an explicit, repository-controlled capability.  A missing
    # or false opt-in always selects COMMENT while retaining the same review
    # publication path and marker/idempotency guarantees.
    if enabled is not True:
        blockers.append("auto-approval-disabled")
    if app_authored is True:
        blockers.append("app-authored-pull-request")
    if isinstance(result, ReviewResult) and result.comments:
        blockers.append("inline-findings-open")
    if has_open_review_threads is None:
        blockers.append("review-threads-incomplete")
    elif has_open_review_threads is True:
        blockers.append("review-threads-open")
    status = getattr(result, "review_status", "incomplete")
    if status in {"partial", "incomplete", "summary-only"}:
        blockers.append(f"review-{status}")
    elif status != "complete":
        blockers.append("review-status-invalid")
    return AutoApprovalDecision(approved=not blockers, blockers=tuple(blockers))


__all__ = ["AutoApprovalDecision", "evaluate_auto_approval"]
