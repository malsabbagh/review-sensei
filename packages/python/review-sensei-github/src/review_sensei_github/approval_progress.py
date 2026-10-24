"""Bounded observations from existing finalization reads; never gate inputs."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace

from review_sensei.disposition import MaintainerCommandResult


@dataclass(frozen=True)
class ApprovalProgress:
    schema_version: str
    head_sha: str
    result_digest: str
    source_review_id: int
    honored_findings: int
    pending_human_assessments: int | None
    body_blocker_remaining: bool
    review_status: str
    coverage_blocker: str | None
    recorded_qualification: str
    decided_thread_reconciliation: str
    approval_state: str
    approval_review_id: int | None
    check_state: str
    check_run_id: int | None
    requested_check_conclusion: str | None
    next_action: str
    # Neither the finalizer's eligibility facts nor its finding index establish
    # these coverage/media facts. Unknown is distinct from zero or complete.
    covered_units: int | None = None
    pending_units: int | None = None
    media_source_comment_id: int | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def build_approval_progress(
    *,
    head_sha: str,
    result_digest: str,
    source_review_id: int,
    honored_findings: int,
    pending_human_assessments: int | None,
    body_blocker_remaining: bool,
    review_status: str,
    coverage_blocker: str | None,
    recorded_qualification: str,
    decided_thread_reconciliation: str,
    approval_status: str,
    approval_review_id: int | None,
    check_attempted: bool,
    check_confirmed: bool,
    check_run_id: int | None,
    requested_check_conclusion: str | None,
) -> ApprovalProgress:
    """Use authenticated observations already obtained by the caller, no I/O."""
    approval = {
        "approved": "submitted",
        "already_approved": "observed-existing",
        "approval_withheld": "withheld",
    }.get(approval_status, "not-observed")
    check = (
        ("confirmed" if check_confirmed else "unconfirmed")
        if check_attempted
        else "not-attempted"
    )
    if approval_status in {
        "skipped_stale_head",
        "skipped_stale_base",
        "skipped_pr_state",
        "skipped_repository_mismatch",
    }:
        action = "refresh_current_source"
    elif check == "unconfirmed" or decided_thread_reconciliation == "unconfirmed":
        action = "retry_verify"
    elif approval in {"submitted", "observed-existing"}:
        action = "none" if check != "not-attempted" else "reconcile_current_check"
    elif pending_human_assessments is not None and pending_human_assessments > 0:
        action = "assess_pending_findings"
    elif body_blocker_remaining:
        action = "address_required_findings"
    elif review_status != "complete" or coverage_blocker is not None:
        action = "inspect_partial_source"
    elif recorded_qualification in {"missing", "unverified"}:
        action = "verify_required_qualification"
    else:
        action = "retry_verify"
    return ApprovalProgress(
        "1.0",
        head_sha,
        result_digest,
        source_review_id,
        honored_findings,
        pending_human_assessments,
        body_blocker_remaining,
        review_status,
        coverage_blocker,
        recorded_qualification,
        decided_thread_reconciliation,
        approval,
        approval_review_id,
        check,
        check_run_id,
        requested_check_conclusion,
        action,
    )


def render_command_progress(
    result: MaintainerCommandResult, publication: object
) -> MaintainerCommandResult:
    """Append an allowlisted observation after human text; preserve all flags."""
    progress = getattr(publication, "approval_progress", None)
    if type(progress) is not ApprovalProgress:
        return result
    return replace(
        result,
        summary=result.summary
        + "\napproval-progress: "
        + json.dumps(
            progress.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ),
    )
