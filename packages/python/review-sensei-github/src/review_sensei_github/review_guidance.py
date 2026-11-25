"""Read-only next-safe-action guidance across normal publication and review status."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from review_sensei.convergence import DEFAULT_MAX_FAILED_ATTEMPTS, MAX_FAILED_ATTEMPTS
from review_sensei.disposition import session_honors_coverage_uncertainty
from review_sensei.human_file_review import (
    coverage_only_binary,
    is_human_confirmable_file,
    is_human_confirmable_hunk,
)
from review_sensei.models import ReviewResult
from review_sensei.presentation import sanitize_finding_markdown
from review_sensei.session import (
    SessionRecord,
    failed_attempts_for_head,
    has_unconsumed_successor_grant,
)

from .errors import (
    GitHubHTTPTransientError,
    GitHubPublicationTransientError,
)
from .operation_diagnostics import OperationDiagnostics

if TYPE_CHECKING:
    from review_sensei.disposition import FindingDisposition

    from .approval_progress import ApprovalProgress
    from .artifact_authority import ArtifactEvaluationDecision

_SHA_HEX = re.compile(r"^[a-f0-9]{40}$")
_SAFE_STAGE_LOOKUPS = {
    "eligibility.readback": "eligibility readback lookup (HTTP 403)",
    "check.publication": "check-run publication (HTTP 403)",
    "check.capability": "check capability exchange (HTTP 403)",
    "cleanup.discovery": "review thread cleanup discovery (HTTP 403)",
    "cleanup.hold": "review thread cleanup hold check (HTTP 403)",
    "cleanup.capability": "review thread cleanup capability exchange (HTTP 403)",
    "cleanup.root-readback": "review thread root readback (HTTP 403)",
    "cleanup.authority-fence": "review thread authority fence lookup (HTTP 403)",
    "cleanup.resolve-request": "review thread resolve request (HTTP 403)",
    "cleanup.resolve-readback": "review thread resolve readback (HTTP 403)",
    "approval.preflight": "pull request metadata lookup (approval.preflight, HTTP 403)",
    "approval.thread-scan": "review thread lookup (approval.thread-scan, HTTP 403)",
    "approval.persisted-readback": "pull request reviews readback (approval.persisted-readback, HTTP 403)",
    "approval.request": "pull request review submission (approval.request, HTTP 403)",
    "approval.readback": "approval review readback (approval.readback, HTTP 403)",
}
GUIDANCE_STATES = frozenset(
    {
        "completed",
        "remaining_text_coverage",
        "ready_for_verify",
        "response_loss_reconciled",
        "response_loss_unknown_hold",
        "lookup_forbidden_hold",
        "changed_head",
        "budget_exhausted",
        "paused",
        "held_reservation",
        "required_fixes_open",
        "human_decisions_pending",
        "incomplete_non_text_coverage",
        "publication_failed_hold",
        "no_review",
    }
)
STALLED_BODY_STATES = frozenset(
    {
        "remaining_text_coverage",
        "ready_for_verify",
        "response_loss_unknown_hold",
        "lookup_forbidden_hold",
        "changed_head",
        "budget_exhausted",
    }
)
MAX_STATUS_GUIDANCE_BYTES = 768


def _short_sha(value: str | None) -> str:
    if isinstance(value, str) and value:
        return value[:7]
    return "unknown"


def _clean_text(value: str, *, max_bytes: int = 320) -> str:
    collapsed = " ".join(str(value).split())
    limit = max(3, int(max_bytes))
    encoded = collapsed.encode("utf-8")
    if len(encoded) <= limit:
        return collapsed
    target = limit - 3
    chars: list[str] = []
    used = 0
    for ch in collapsed:
        ch_len = len(ch.encode("utf-8"))
        if used + ch_len > target:
            break
        chars.append(ch)
        used += ch_len
    return "".join(chars).rstrip() + "..."


@dataclass(frozen=True)
class NextActionGuidance:
    """Consistent read-only guidance shared by PR publication, status, and job summary."""

    schema_version: str
    state: str
    completed: str
    blocker: str | None
    next_action: str
    command: str | None
    requires_model_call: bool
    model_call_note: str
    missing_evidence: tuple[str, ...] = ()
    failed_lookup: str | None = None

    def __post_init__(self) -> None:
        if self.state not in GUIDANCE_STATES:
            raise ValueError(f"unsupported guidance state: {self.state}")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "state": self.state,
            "completed": self.completed,
            "blocker": self.blocker,
            "next_action": self.next_action,
            "command": self.command,
            "requires_model_call": self.requires_model_call,
            "model_call_note": self.model_call_note,
            "missing_evidence": list(self.missing_evidence),
            "failed_lookup": self.failed_lookup,
        }


def remaining_reviewable_text_summary(
    result: ReviewResult | None,
) -> tuple[int, int, tuple[str, ...]] | None:
    """Return ``(reviewed, total, sample_remaining_paths)`` when reviewable text remains."""
    if (
        not isinstance(result, ReviewResult)
        or result.review_status != "partial"
        or result.coverage_only_partial
        or result.coverage is None
        or not result.coverage.enumeration_complete
    ):
        return None
    coverage = result.coverage
    if coverage_only_binary(coverage):
        return None
    excluded_generated = {
        entry.path
        for entry in coverage.files
        if entry.outcome == "excluded-by-policy" and entry.reason == "generated"
    }
    text_hunks = [
        entry
        for entry in coverage.hunks
        if not (entry.path in excluded_generated and is_human_confirmable_hunk(entry))
    ]
    if text_hunks:
        reviewed = sum(1 for entry in text_hunks if entry.outcome == "reviewed")
        remaining_paths = tuple(
            dict.fromkeys(
                entry.path for entry in text_hunks if entry.outcome != "reviewed"
            )
        )[:5]
        if reviewed < len(text_hunks):
            return reviewed, len(text_hunks), remaining_paths
        return None
    text_files = [
        entry for entry in coverage.files if not is_human_confirmable_file(entry)
    ]
    if not text_files:
        return None
    reviewed = sum(1 for entry in text_files if entry.outcome == "reviewed")
    remaining_paths = tuple(
        dict.fromkeys(entry.path for entry in text_files if entry.outcome != "reviewed")
    )[:5]
    if reviewed < len(text_files):
        return reviewed, len(text_files), remaining_paths
    return None


def _sanitize_lookup_label(raw: str) -> str:
    cleaned = _clean_text(raw, max_bytes=160)
    if cleaned.endswith("(HTTP 403)") or "(HTTP 403)" in cleaned:
        return cleaned
    return f"{cleaned} (HTTP 403)"


def extract_forbidden_lookup(
    *,
    error: BaseException | None = None,
    operation_diagnostics: OperationDiagnostics | None = None,
    publication: object | None = None,
    check_rejection: Mapping[str, object] | None = None,
) -> str | None:
    """Identify the exact failed lookup when a required request returned HTTP 403."""
    if isinstance(operation_diagnostics, OperationDiagnostics):
        for name in ("eligibility", "check", "cleanup", "approval"):
            phase = getattr(operation_diagnostics, name, None)
            if phase is None:
                continue
            first = phase.first
            if first.http_status == 403 or "FORBIDDEN" in first.graphql_categories:
                return _SAFE_STAGE_LOOKUPS.get(
                    first.stage,
                    f"{first.stage} lookup (HTTP 403)",
                )
    if isinstance(check_rejection, Mapping) and check_rejection.get("status") == 403:
        return "check-run publication (HTTP 403)"
    pub_diag = getattr(publication, "diagnostic", None)
    if pub_diag in {"check_access_denied", "check_permission"}:
        return "check-run publication (HTTP 403)"
    current: BaseException | None = error
    generic_label: str | None = None
    for _ in range(4):
        if current is None:
            break
        if isinstance(
            current, (GitHubHTTPTransientError, GitHubPublicationTransientError)
        ):
            # Rate-limited 403 is surfaced as transient; do not classify as static permission 403.
            current = current.__cause__
            continue
        status = getattr(current, "http_status", None)
        failed_lookup = getattr(current, "failed_lookup", None)
        if status == 403:
            if isinstance(failed_lookup, str) and failed_lookup.strip():
                return _sanitize_lookup_label(failed_lookup)
            msg = str(current)
            if "thread" in msg.lower():
                generic_label = generic_label or "review thread lookup (HTTP 403)"
            elif "review publication lacks permission" in msg.lower():
                generic_label = (
                    generic_label or "pull request review submission (HTTP 403)"
                )
            elif "approval finalization lacks permission" in msg.lower():
                generic_label = generic_label or "approval review submission (HTTP 403)"
            else:
                generic_label = generic_label or "GitHub repository lookup (HTTP 403)"
            current = current.__cause__
            continue
        msg_lower = str(current).lower()
        if "graphql access denied" in msg_lower or "http 403" in msg_lower:
            if "thread" in msg_lower:
                generic_label = generic_label or "review thread lookup (HTTP 403)"
            else:
                generic_label = generic_label or "GitHub API lookup (HTTP 403)"
        elif "check_access_denied" in msg_lower:
            generic_label = generic_label or "check-run publication (HTTP 403)"
        elif "review publication lacks permission" in msg_lower:
            generic_label = generic_label or "pull request review submission (HTTP 403)"
        elif "approval finalization lacks permission" in msg_lower:
            generic_label = generic_label or "approval review submission (HTTP 403)"
        current = current.__cause__
    return generic_label


def _is_budget_exhausted_error(error: BaseException | None) -> str | None:
    current: BaseException | None = error
    for _ in range(4):
        if current is None:
            break
        msg = str(current).lower()
        for needle, label in (
            ("partition shared request budget exhausted", "evidence read budget"),
            ("review scan shared budget exhausted", "evidence read budget"),
            ("caller api", "caller API request budget"),
            ("allowance", "caller API request allowance"),
            ("budget exhausted", "review budget"),
            ("pagination exhausted", "GitHub pagination budget"),
        ):
            if needle in msg:
                return label
        current = current.__cause__
    return None


def _is_response_loss_or_ambiguous_error(error: BaseException | None) -> str | None:
    current: BaseException | None = error
    for _ in range(4):
        if current is None:
            break
        if isinstance(current, GitHubPublicationTransientError):
            return str(current)
        msg = str(current).lower()
        if any(
            token in msg
            for token in (
                "response loss",
                "response-loss",
                "publication_ambiguous",
                "reconciliation",
                "outcome is unconfirmed",
                "does not prove complete publication",
                "intended review is absent",
            )
        ):
            return str(current)
        current = current.__cause__
    return None


def _all_obligations_honored_by_dispositions(
    *,
    result: ReviewResult | None,
    head_sha: str | None,
    dispositions: Sequence[FindingDisposition],
    record: SessionRecord | None,
    ledger: object | None = None,
) -> tuple[bool, int, int]:
    """Return ``(eligible_for_verify, open_required_fixes, open_human_decisions)``."""
    if (
        result is None
        or not isinstance(head_sha, str)
        or not _SHA_HEX.fullmatch(head_sha)
    ):
        return False, 0, 0
    from review_sensei.context import finding_lifecycle_for_comment
    from review_sensei.human_assessment import finding_instance_fingerprint

    lifecycle_states = {
        item.fingerprint: item.state for item in result.finding_lifecycles
    }
    open_fixes = 0
    open_human = 0
    for comment in result.comments:
        concern_fp = finding_lifecycle_for_comment(comment).fingerprint
        if lifecycle_states.get(concern_fp, "new") in {"fixed", "outdated"}:
            continue
        instance_fp = finding_instance_fingerprint(comment)
        honored = any(
            item.honors(concern_fp, head_sha=head_sha)
            or item.honors(instance_fp, head_sha=head_sha)
            for item in dispositions
        )
        if honored:
            continue
        if comment.blocks_approval:
            open_fixes += 1
        elif comment.needs_human:
            open_human += 1
    if open_fixes or open_human:
        return False, open_fixes, open_human
    if result.review_status != "complete" or (
        result.coverage is not None and not result.coverage.fully_reviewed
    ):
        coverage_accepted = False
        if record is not None:
            coverage_accepted = session_honors_coverage_uncertainty(
                record,
                head_sha=head_sha,
                ledger=ledger,
            )
        if not coverage_accepted:
            coverage_accepted = any(
                item.action == "accept-coverage-uncertainty"
                and (item.head_sha is None or item.head_sha == head_sha)
                for item in dispositions
            )
        if not coverage_accepted:
            return False, 0, 0
    return True, 0, 0


def _completed_summary_prefix(
    *,
    head_sha: str | None,
    record: SessionRecord | None,
    retained_result: ReviewResult | None,
    approval_progress: ApprovalProgress | None,
    publication: object | None,
    reconciled_review_id: int | None,
    honored_count: int,
) -> str:
    short = _short_sha(head_sha)
    parts: list[str] = []
    if retained_result is not None:
        text_summary = remaining_reviewable_text_summary(retained_result)
        if text_summary is not None:
            reviewed, total, _ = text_summary
            parts.append(
                f"Analyzed {reviewed} of {total} reviewable text units on commit `{short}`"
            )
        elif retained_result.review_status == "complete":
            parts.append(f"Analysis completed on commit `{short}`")
        else:
            parts.append(
                f"Analysis recorded status `{retained_result.review_status}` on commit `{short}`"
            )
    elif record is not None and record.transaction is not None:
        tx = record.transaction
        parts.append(
            f"Session transaction `{tx.phase}` recorded for commit `{_short_sha(tx.head_sha)}`"
        )
    review_id = reconciled_review_id or getattr(publication, "review_id", None)
    if type(review_id) is int and review_id > 0:
        parts.append(f"review #{review_id} confirmed on GitHub")
    elif (
        approval_progress is not None
        and type(approval_progress.source_review_id) is int
        and approval_progress.source_review_id > 0
    ):
        parts.append(
            f"source review #{approval_progress.source_review_id} observed on GitHub"
        )
    if honored_count > 0:
        parts.append(f"{honored_count} maintainer decision(s) recorded")
    if approval_progress is not None and approval_progress.approval_state in {
        "submitted",
        "observed-existing",
    }:
        parts.append(f"approval `{approval_progress.approval_state}`")
    if not parts:
        return "No review write completed yet"
    return "; ".join(parts)


def resolve_next_action_guidance(
    *,
    head_sha: str | None,
    record: SessionRecord | None = None,
    retained_result: ReviewResult | None = None,
    approval_progress: ApprovalProgress | None = None,
    operation_diagnostics: OperationDiagnostics | None = None,
    publication: object | None = None,
    response_loss_hold: ArtifactEvaluationDecision | None = None,
    error: BaseException | None = None,
    dispositions: Sequence[FindingDisposition] = (),
    reconciled_review_id: int | None = None,
    check_rejection: Mapping[str, object] | None = None,
    auto_approve: bool = True,
    reviews_policy: str | None = None,
    ledger: object | None = None,
    lifecycle_document: Mapping[str, object] | None = None,
) -> NextActionGuidance:
    """Compute one consistent read-only next-action answer from existing evidence."""
    short = _short_sha(head_sha)
    eff_progress = approval_progress or getattr(publication, "approval_progress", None)
    eff_op_diag = operation_diagnostics or getattr(
        publication, "operation_diagnostics", None
    )
    if eff_op_diag is None and error is not None:
        maybe_diag = getattr(error, "operation_diagnostics", None)
        if isinstance(maybe_diag, OperationDiagnostics):
            eff_op_diag = maybe_diag
    eff_review_id = reconciled_review_id or getattr(publication, "review_id", None)
    if (
        eff_review_id is None
        and eff_progress is not None
        and type(eff_progress.source_review_id) is int
        and eff_progress.source_review_id > 0
    ):
        eff_review_id = eff_progress.source_review_id
    head_dispositions = tuple(
        item
        for item in dispositions
        if head_sha is None or item.head_sha is None or item.head_sha == head_sha
    )
    honored_count = (
        eff_progress.honored_findings
        if eff_progress is not None
        else len(head_dispositions)
    )
    completed = _completed_summary_prefix(
        head_sha=head_sha,
        record=record,
        retained_result=retained_result,
        approval_progress=eff_progress,
        publication=publication,
        reconciled_review_id=eff_review_id,
        honored_count=honored_count,
    )

    # 1. Required lookup returned HTTP 403 -> identify failed lookup and hold.
    failed_lookup = extract_forbidden_lookup(
        error=error,
        operation_diagnostics=eff_op_diag,
        publication=publication,
        check_rejection=check_rejection,
    )
    if failed_lookup is not None:
        return NextActionGuidance(
            schema_version="1.0",
            state="lookup_forbidden_hold",
            completed=completed,
            blocker=f"Required GitHub lookup returned HTTP 403: {failed_lookup}.",
            next_action=(
                f"Hold for investigation: check GitHub App/token permissions and "
                f"repository access for {failed_lookup}."
            ),
            command=None,
            requires_model_call=False,
            model_call_note="0 model calls (hold — investigate permissions before taking action)",
            failed_lookup=failed_lookup,
        )

    # 2. Lost response / unverifiable write -> report missing evidence and hold.
    missing_evidence: list[str] = []
    if response_loss_hold is not None and not response_loss_hold.authority_intact:
        reason_code = response_loss_hold.reason_code
        if "duplicate" in reason_code:
            missing_evidence.append(
                f"unique matching App review on commit `{short}` (duplicate reviews observed)"
            )
        elif "absent" in reason_code:
            missing_evidence.append(
                f"confirmed matching App review readback on commit `{short}`"
            )
        elif "edited" in reason_code or "digest" in reason_code:
            missing_evidence.append(
                f"unedited App review body matching expected digest on commit `{short}`"
            )
        else:
            missing_evidence.append(
                f"verified App review readback ({reason_code}) on commit `{short}`"
            )
    if (
        isinstance(eff_op_diag, OperationDiagnostics)
        and eff_op_diag.approval_request_attempted
        and eff_progress is not None
        and eff_progress.approval_state == "not-observed"
    ):
        missing_evidence.append(
            f"confirmed native APPROVE review readback on commit `{short}`"
        )
    pub_status = getattr(publication, "status", None)
    pub_diag = getattr(publication, "diagnostic", None)
    if pub_status == "publication_incomplete" or pub_diag == "publication_ambiguous":
        missing_evidence.append(
            f"confirmed matching PR review and finding index on commit `{short}`"
        )
    if isinstance(lifecycle_document, Mapping) and lifecycle_document.get(
        "unknown_trust_effect"
    ):
        missing_evidence.append(
            f"reconciled native review/check write outcome on commit `{short}`"
        )
    ambiguous_error = _is_response_loss_or_ambiguous_error(error)
    if ambiguous_error is not None:
        missing_evidence.append(
            f"confirmed matching App review readback on commit `{short}`"
        )
    if (
        record is not None
        and record.transaction is not None
        and record.transaction.phase in {"publication_pending", "publication_failed"}
        and (head_sha is None or record.transaction.head_sha == head_sha)
        and eff_review_id is None
        and pub_status
        not in {"published", "already_published", "auto_approval_disabled"}
    ):
        accounting = (
            record.api_run_accounting
            if isinstance(record.api_run_accounting, Mapping)
            else None
        )
        uncertain_calls = (
            accounting.get("uncertain", 0) if accounting is not None else 0
        )
        if (
            record.transaction.phase == "publication_failed"
            or (isinstance(uncertain_calls, int) and uncertain_calls > 0)
            or (
                record.transaction.phase == "publication_pending"
                and record.reservation_id is None
            )
        ):
            missing_evidence.append(
                f"reconciled publication outcome for transaction `{record.transaction.transaction_id[:12]}` on commit `{short}`"
            )
    if missing_evidence:
        deduped_missing = tuple(dict.fromkeys(missing_evidence))
        return NextActionGuidance(
            schema_version="1.0",
            state="response_loss_unknown_hold",
            completed=completed,
            blocker=(
                "Write outcome could not be verified after a lost response or "
                "interrupted publication."
            ),
            next_action=(
                "Hold and reconcile the original write against GitHub PR reviews "
                "and checks to establish the missing evidence; do not start a "
                "blind replacement write."
            ),
            command=None,
            requires_model_call=False,
            model_call_note="0 model calls (hold — reconcile original write before continuing)",
            missing_evidence=deduped_missing,
        )

    # 3. Changed head -> prior review/checkpoint does not qualify current head.
    tx = record.transaction if record is not None else None
    if tx is None and retained_result is not None:
        tx = retained_result.transaction
    prior_head_sha: str | None = tx.head_sha if tx is not None else None
    if (
        prior_head_sha is None
        and record is not None
        and isinstance(record.convergence_history, Mapping)
    ):
        baseline_doc = record.convergence_history.get("baseline")
        if isinstance(baseline_doc, Mapping):
            cache_key = baseline_doc.get("cache_key")
            if isinstance(cache_key, Mapping):
                raw_head = cache_key.get("head_sha")
                if isinstance(raw_head, str) and _SHA_HEX.fullmatch(raw_head):
                    prior_head_sha = raw_head
    if (
        (
            eff_progress is not None
            and eff_progress.next_action == "refresh_current_source"
        )
        or pub_status in {"skipped_stale_head", "skipped_stale_base"}
        or (
            isinstance(prior_head_sha, str)
            and isinstance(head_sha, str)
            and _SHA_HEX.fullmatch(head_sha) is not None
            and prior_head_sha != head_sha
        )
    ):
        old_short = _short_sha(prior_head_sha) if prior_head_sha is not None else None
        rescan_cmd = (
            f"@reviewsensei rescan commit {head_sha}"
            if isinstance(head_sha, str) and _SHA_HEX.fullmatch(head_sha)
            else "@reviewsensei rescan"
        )
        blocker_text = (
            f"PR head changed to `{short}`; prior evidence on `{old_short}` "
            "cannot approve the current commit."
            if old_short is not None
            else (
                f"PR head or base changed relative to prior evidence on `{short}`; "
                "prior evidence cannot approve the current commit."
            )
        )
        return NextActionGuidance(
            schema_version="1.0",
            state="changed_head",
            completed=(
                f"Prior review evidence was recorded for commit `{old_short}`"
                if old_short is not None
                else completed
            ),
            blocker=blocker_text,
            next_action=(
                f"Wait for automatic review on commit `{short}` or comment "
                f"`{rescan_cmd}` to run a bounded review on the current commit."
            ),
            command=rescan_cmd,
            requires_model_call=True,
            model_call_note="Requires model calls (new analysis for the current commit)",
        )

    # 4. Paused or held reservation (ADR 0079).
    if record is not None and (
        record.operator_paused or record.reservation_id is not None
    ):
        if record.reservation_id is not None:
            owner = record.reservation_owner
            owner_text = (
                f"run {owner.get('run_id')}"
                if isinstance(owner, Mapping) and owner.get("run_id")
                else "unknown owner"
            )
            return NextActionGuidance(
                schema_version="1.0",
                state="held_reservation",
                completed=completed,
                blocker=f"Blocked by held reservation ({owner_text}).",
                next_action=(
                    f"Wait for {owner_text} to cease; once stopped, comment "
                    "`@reviewsensei review continue` if the session remains blocked."
                ),
                command="@reviewsensei review continue",
                requires_model_call=False,
                model_call_note="0 model calls for `@reviewsensei review continue` recovery",
            )
        return NextActionGuidance(
            schema_version="1.0",
            state="paused",
            completed=completed,
            blocker="Review session is paused by a maintainer.",
            next_action="Comment `@reviewsensei review continue` to unpause the review session.",
            command="@reviewsensei review continue",
            requires_model_call=False,
            model_call_note="0 model calls to unpause (`@reviewsensei review continue`)",
        )

    # 5. Exhausted budget (evidence/API budget or failed-attempt ceiling).
    budget_reason = _is_budget_exhausted_error(error)
    if isinstance(eff_op_diag, OperationDiagnostics) and any(
        getattr(eff_op_diag, phase) is not None
        and getattr(eff_op_diag, phase).first.reason in {"call-limit", "deadline"}
        for phase in ("eligibility", "check", "cleanup", "approval")
    ):
        budget_reason = "publication API call or deadline budget"
    if budget_reason is not None:
        return NextActionGuidance(
            schema_version="1.0",
            state="budget_exhausted",
            completed=completed,
            blocker=f"Exhausted {budget_reason}; original budget accounting is preserved.",
            next_action=(
                "Hold and preserve existing review evidence and budget accounting; "
                "do not reset or refill the exhausted budget."
            ),
            command=None,
            requires_model_call=False,
            model_call_note="0 model calls (budget exhausted — preserved without reset)",
        )
    if (
        record is not None
        and isinstance(head_sha, str)
        and _SHA_HEX.fullmatch(head_sha)
    ):
        failed = failed_attempts_for_head(record, head_sha)
        if failed >= MAX_FAILED_ATTEMPTS:
            return NextActionGuidance(
                schema_version="1.0",
                state="budget_exhausted",
                completed=completed,
                blocker=(
                    f"Hard failed-attempt ceiling ({MAX_FAILED_ATTEMPTS}) reached on "
                    f"commit `{short}`."
                ),
                next_action="Push a new commit to start a fresh review budget.",
                command=None,
                requires_model_call=True,
                model_call_note="Requires model calls on a new commit",
            )
        if (
            failed >= DEFAULT_MAX_FAILED_ATTEMPTS
            and has_unconsumed_successor_grant(record, head_sha) is None
        ):
            return NextActionGuidance(
                schema_version="1.0",
                state="budget_exhausted",
                completed=completed,
                blocker=(
                    f"Failed-attempt budget ({failed}/{DEFAULT_MAX_FAILED_ATTEMPTS}) "
                    f"is exhausted on commit `{short}`."
                ),
                next_action=(
                    "A maintainer or admin must comment `@reviewsensei review continue` "
                    "to authorize one bounded continuation window on this commit."
                ),
                command="@reviewsensei review continue",
                requires_model_call=False,
                model_call_note=(
                    "0 model calls for `@reviewsensei review continue`; subsequent "
                    "continuation analysis requires model calls"
                ),
            )

    # 6. Reviewable text remains on the same commit -> `review remaining commit <sha>`.
    text_remaining = remaining_reviewable_text_summary(retained_result)
    if (
        text_remaining is not None
        and isinstance(head_sha, str)
        and _SHA_HEX.fullmatch(head_sha)
    ):
        coverage_accepted = (
            session_honors_coverage_uncertainty(
                record, head_sha=head_sha, ledger=ledger
            )
            if record is not None
            else False
        )
        if not coverage_accepted:
            coverage_accepted = any(
                item.action == "accept-coverage-uncertainty"
                and (item.head_sha is None or item.head_sha == head_sha)
                for item in head_dispositions
            )
        if not coverage_accepted:
            reviewed, total, sample_paths = text_remaining
            cmd = f"@reviewsensei review remaining commit {head_sha}"
            remaining_count = total - reviewed
            sample_note = (
                f" (including {', '.join(f'`{sanitize_finding_markdown(p)}`' for p in sample_paths)})"
                if sample_paths
                else ""
            )
            return NextActionGuidance(
                schema_version="1.0",
                state="remaining_text_coverage",
                completed=completed,
                blocker=(
                    f"Reviewable text remains unreviewed on commit `{short}` "
                    f"({remaining_count} unit(s) remaining{sample_note})."
                ),
                next_action=(
                    f"Comment `{cmd}` to authorize one bounded analysis window "
                    "for the remaining reviewable text on this commit."
                ),
                command=cmd,
                requires_model_call=True,
                model_call_note=(
                    "Requires model calls (authorizes one bounded analysis window "
                    "within the configured review budget)"
                ),
            )

    # 7. Non-text / media-only partial coverage.
    if retained_result is not None and retained_result.coverage_only_partial:
        return NextActionGuidance(
            schema_version="1.0",
            state="incomplete_non_text_coverage",
            completed=completed,
            blocker="Only unsupported binary or media files remain unreviewed.",
            next_action=(
                "Comment `@reviewsensei I reviewed the media files and I approve` "
                "after inspecting those files."
            ),
            command="@reviewsensei I reviewed the media files and I approve",
            requires_model_call=False,
            model_call_note="0 model calls (explicit maintainer media confirmation)",
        )
    if (
        eff_progress is not None
        and (
            eff_progress.review_status != "complete"
            or eff_progress.coverage_blocker is not None
        )
        and retained_result is None
    ):
        return NextActionGuidance(
            schema_version="1.0",
            state="incomplete_non_text_coverage",
            completed=completed,
            blocker=(
                "Coverage remains incomplete (`"
                f"{eff_progress.coverage_blocker or eff_progress.review_status}`)."
            ),
            next_action=(
                "Inspect the retained review on this commit: use "
                f"`@reviewsensei review remaining commit {head_sha}` if reviewable "
                "text remains, or confirm unsupported media files with the media "
                "approval command."
                if isinstance(head_sha, str) and _SHA_HEX.fullmatch(head_sha)
                else "Inspect the retained review before continuing."
            ),
            command=None,
            requires_model_call=False,
            model_call_note="Depends on retained coverage (0 model calls for media confirmation; model calls for `review remaining`)",
        )

    # 8 & 9. Check open obligations vs decisions recorded and only finalization remains.
    all_honored, open_fixes, open_human = _all_obligations_honored_by_dispositions(
        result=retained_result,
        head_sha=head_sha,
        dispositions=head_dispositions,
        record=record,
        ledger=ledger,
    )
    if open_fixes > 0:
        return NextActionGuidance(
            schema_version="1.0",
            state="required_fixes_open",
            completed=completed,
            blocker=f"{open_fixes} required fix(es) remain open on commit `{short}`.",
            next_action=(
                "Address the required fixes and push a new commit, or record an "
                "authorized override followed by `@reviewsensei verify`."
            ),
            command=None,
            requires_model_call=True,
            model_call_note="Requires model calls if pushing a fix; 0 model calls if overriding and running `@reviewsensei verify`",
        )
    if open_human > 0:
        return NextActionGuidance(
            schema_version="1.0",
            state="human_decisions_pending",
            completed=completed,
            blocker=(
                f"{open_human} required human decision(s) remain pending on commit `{short}`."
            ),
            next_action=(
                "Record a maintainer decision (`@reviewsensei override <id> acceptable risk`, "
                "`@reviewsensei accept-risk <id> --reason <reason>`, or "
                "`@reviewsensei dismiss <id> --reason <reason>`), then run "
                "`@reviewsensei verify`."
            ),
            command=None,
            requires_model_call=False,
            model_call_note="0 model calls (maintainer decision + `@reviewsensei verify`)",
        )

    finalization_already_done = (
        eff_progress is not None
        and eff_progress.approval_state in {"submitted", "observed-existing"}
        and eff_progress.check_state == "confirmed"
        and eff_progress.decided_thread_reconciliation == "confirmed"
    )
    progress_ready_for_verify = (
        eff_progress is not None
        and eff_progress.next_action in {"retry_verify", "reconcile_current_check"}
        and eff_progress.pending_human_assessments in {0, None}
        and not eff_progress.body_blocker_remaining
        and eff_progress.review_status == "complete"
        and eff_progress.coverage_blocker is None
        and eff_progress.recorded_qualification not in {"missing", "unverified"}
    )
    decisions_ready_without_progress = (
        not finalization_already_done
        and all_honored
        and (
            bool(head_dispositions)
            or pub_diag in {"approval_withheld", "check_reconciliation_failed"}
        )
        and (
            pub_status not in {"approved", "already_approved"}
            and (
                eff_progress is None
                or eff_progress.approval_state not in {"submitted", "observed-existing"}
                or eff_progress.check_state != "confirmed"
                or eff_progress.decided_thread_reconciliation == "unconfirmed"
            )
        )
    )
    if progress_ready_for_verify or decisions_ready_without_progress:
        return NextActionGuidance(
            schema_version="1.0",
            state="ready_for_verify",
            completed=completed,
            blocker=(
                "Recorded decisions are complete; finalization (thread cleanup, "
                "check run, and/or native approval) remains."
            ),
            next_action=(
                "Comment `@reviewsensei verify` to finalize using recorded decisions "
                "and existing evidence."
            ),
            command="@reviewsensei verify",
            requires_model_call=False,
            model_call_note="0 model calls (finalization only using recorded decisions)",
        )

    # 10. Response-loss write reconciled and reused, or fully completed.
    if (
        pub_status == "already_published"
        and eff_review_id is not None
        and (
            finalization_already_done
            or not auto_approve
            or (reviews_policy is not None and reviews_policy != "auto-approve")
        )
    ):
        return NextActionGuidance(
            schema_version="1.0",
            state="response_loss_reconciled",
            completed=(
                f"Reconciled and reused confirmed matching review #{eff_review_id} "
                f"on commit `{short}`"
                + (
                    f"; approval `{eff_progress.approval_state}`"
                    if eff_progress is not None
                    and eff_progress.approval_state
                    in {"submitted", "observed-existing"}
                    else ""
                )
            ),
            blocker=None,
            next_action="No further action required.",
            command=None,
            requires_model_call=False,
            model_call_note="0 model calls (reconciled existing review)",
        )

    if finalization_already_done or (
        pub_status in {"published", "already_published"}
        and (
            not auto_approve
            or (reviews_policy is not None and reviews_policy != "auto-approve")
        )
    ):
        return NextActionGuidance(
            schema_version="1.0",
            state="completed",
            completed=completed,
            blocker=None,
            next_action="No further action required.",
            command=None,
            requires_model_call=False,
            model_call_note="0 model calls",
        )

    # 11. Generic publication failure or no review yet.
    if error is not None or pub_status == "publication_failed":
        return NextActionGuidance(
            schema_version="1.0",
            state="publication_failed_hold",
            completed=completed,
            blocker="GitHub publication did not complete.",
            next_action=(
                "Inspect the publication failure details and workflow permissions "
                "before taking further action."
            ),
            command=None,
            requires_model_call=False,
            model_call_note="0 model calls (hold — inspect publication failure)",
        )

    if (
        record is not None
        and record.transaction is not None
        and record.transaction.phase == "publication_succeeded"
        and (head_sha is None or record.transaction.head_sha == head_sha)
    ):
        if (
            head_dispositions
            and auto_approve
            and (reviews_policy is None or reviews_policy == "auto-approve")
        ):
            return NextActionGuidance(
                schema_version="1.0",
                state="ready_for_verify",
                completed=completed,
                blocker="Decisions are recorded; finalization may remain pending.",
                next_action="Comment `@reviewsensei verify` to finalize using recorded decisions.",
                command="@reviewsensei verify",
                requires_model_call=False,
                model_call_note="0 model calls (finalization only using recorded decisions)",
            )
        return NextActionGuidance(
            schema_version="1.0",
            state="completed",
            completed=completed,
            blocker=None,
            next_action=(
                "Review is published on this commit. Comment `@reviewsensei verify` "
                "only if approval or thread cleanup still needs finalization."
            ),
            command="@reviewsensei verify",
            requires_model_call=False,
            model_call_note="0 model calls",
        )

    return NextActionGuidance(
        schema_version="1.0",
        state="no_review",
        completed=completed,
        blocker="No review result is recorded for the current commit yet.",
        next_action=(
            f"Wait for automatic review on commit `{short}` or comment "
            f"`@reviewsensei rescan commit {head_sha}` to analyze the current commit."
            if isinstance(head_sha, str) and _SHA_HEX.fullmatch(head_sha)
            else "Wait for the review run or trigger a review on the pull request."
        ),
        command=(
            f"@reviewsensei rescan commit {head_sha}"
            if isinstance(head_sha, str) and _SHA_HEX.fullmatch(head_sha)
            else None
        ),
        requires_model_call=True,
        model_call_note="Requires model calls (no review recorded yet)",
    )


def render_next_action_markdown(guidance: NextActionGuidance) -> str:
    """Render shared human-readable guidance for PR bodies and Actions summaries."""
    lines = [
        f"- **Completed:** {guidance.completed}",
    ]
    if guidance.blocker:
        lines.append(f"- **Blocking:** {guidance.blocker}")
    if guidance.failed_lookup:
        lines.append(f"- **Failed lookup:** `{guidance.failed_lookup}`")
    if guidance.missing_evidence:
        lines.append("- **Missing evidence:** " + "; ".join(guidance.missing_evidence))
    lines.append(f"- **Next action:** {guidance.next_action}")
    lines.append(f"- **Model calls:** {guidance.model_call_note}")
    return "\n".join(lines)


def render_status_guidance_lines(guidance: NextActionGuidance) -> str:
    """Render bounded status lines strictly consistent with ``NextActionGuidance``."""
    next_line = f"next_action={_clean_text(guidance.next_action, max_bytes=220)}"
    model_line = "model_calls=" + _clean_text(
        f"{'required' if guidance.requires_model_call else '0'} "
        f"({guidance.model_call_note})",
        max_bytes=160,
    )
    prefix_lines = [
        f"state={guidance.state}",
        f"completed={_clean_text(guidance.completed, max_bytes=200)}",
    ]
    if guidance.blocker:
        prefix_lines.append(f"blocking={_clean_text(guidance.blocker, max_bytes=200)}")
    if guidance.failed_lookup:
        prefix_lines.append(
            f"failed_lookup={_clean_text(guidance.failed_lookup, max_bytes=140)}"
        )
    if guidance.missing_evidence:
        prefix_lines.append(
            "missing_evidence="
            + _clean_text("; ".join(guidance.missing_evidence), max_bytes=180)
        )
    all_lines = [*prefix_lines, next_line, model_line]
    rendered = "\n".join(all_lines)
    if len(rendered.encode("utf-8")) <= MAX_STATUS_GUIDANCE_BYTES:
        return rendered
    # Never truncate inside the actionable command/model_calls lines; shrink optional lines first.
    essential = "\n".join([f"state={guidance.state}", next_line, model_line])
    remaining = MAX_STATUS_GUIDANCE_BYTES - len(essential.encode("utf-8")) - 1
    kept_middle: list[str] = []
    for line in prefix_lines[1:]:
        cost = len(line.encode("utf-8")) + 1
        if cost <= remaining:
            kept_middle.append(line)
            remaining -= cost
    bounded = "\n".join(
        [f"state={guidance.state}", *kept_middle, next_line, model_line]
    )
    if len(bounded.encode("utf-8")) <= MAX_STATUS_GUIDANCE_BYTES:
        return bounded
    return _clean_text(essential, max_bytes=MAX_STATUS_GUIDANCE_BYTES)


def render_publication_failure_step_summary(
    guidance: NextActionGuidance,
    *,
    repository: str | None = None,
    pull_request: int | None = None,
    head_sha: str | None = None,
    diagnostic: str | None = None,
) -> str:
    """Render a sanitized Actions job summary explaining a publication failure."""
    lines = ["## ReviewSensei GitHub publication did not complete"]
    if isinstance(repository, str) and repository:
        lines.append(f"- repository: `{_clean_text(repository, max_bytes=128)}`")
    if type(pull_request) is int and pull_request > 0:
        lines.append(f"- pull_request: #{pull_request}")
    if isinstance(head_sha, str) and _SHA_HEX.fullmatch(head_sha):
        lines.append(f"- head: `{_short_sha(head_sha)}`")
    if isinstance(diagnostic, str) and diagnostic:
        lines.append(f"- diagnostic: `{_clean_text(diagnostic, max_bytes=96)}`")
    lines.append(render_next_action_markdown(guidance))
    return "\n".join(lines) + "\n"


def write_publication_failure_step_summary(
    guidance: NextActionGuidance,
    *,
    repository: str | None = None,
    pull_request: int | None = None,
    head_sha: str | None = None,
    diagnostic: str | None = None,
) -> bool:
    """Append sanitized publication failure guidance to ``GITHUB_STEP_SUMMARY`` if set."""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if not summary_path:
        return False
    body = render_publication_failure_step_summary(
        guidance,
        repository=repository,
        pull_request=pull_request,
        head_sha=head_sha,
        diagnostic=diagnostic,
    )
    try:
        with Path(summary_path).open("a", encoding="utf-8") as handle:
            handle.write(body)
        return True
    except OSError:
        return False


from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
