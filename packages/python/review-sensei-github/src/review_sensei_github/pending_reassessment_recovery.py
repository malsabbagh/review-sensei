"""Zero-decision pending reassessment attribution and recovery qualification (G30).

Separates unfinished human reassessment from failed approval/check/thread
finalization, preserves durable pending-receipt attribution across delayed
follow-ups and fresh processes, enforces duplicate-receipt and source/head/base
fences, bounds consecutive zero-decision no-progress windows, and provides an
authenticated `controlled-restart-registry-v1` recovery descriptor whenever an
unclosed `action.phase == "pending"` session root blocks successor runs.

All classifications and descriptors carry `authority=False` and never fabricate
model decisions, clear pending human findings, or bypass approval gates.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from review_sensei.api_work import ApiRunAccounting
from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.session import SessionIdentity, SessionRecord

from . import controlled_restart
from .approval import ReviewApprovalEligibility
from .conversation import (
    CONVERSATION_COMMENT_PAGE_SIZES,
    MARKER_PREFIX,
    MAX_REPLY_BYTES,
    ReplyResult,
    reply_marker,
)

_HEX40 = re.compile(r"[a-f0-9]{40}")
_HEX64 = re.compile(r"[a-f0-9]{64}")
_PART_TAIL_RE = re.compile(r"(\d+)/(\d+) content=([a-f0-9]{64}) -->")

PENDING_REASSESSMENT_CLASSIFIER_ID = "pending-reassessment-attribution-v1"
DEFAULT_MAX_ZERO_DECISION_ATTEMPTS = 2

PENDING_REASSESSMENT_STATES: frozenset[str] = frozenset(
    {
        "unfinished_zero_decision_assessment",
        "unfinished_partial_decision_assessment",
        "failed_finalizer_after_reassessment",
        "pending_receipt_delivery_incomplete",
        "pending_reassessment_duplicate_receipt_conflict",
        "pending_reassessment_source_changed",
        "pending_reassessment_head_or_base_changed",
        "pending_reassessment_no_progress_exhausted",
        "reassessment_completed_settled",
    }
)

PENDING_REASSESSMENT_ROUTES: frozenset[str] = frozenset(
    {
        "delayed_followup_authorized_mention",
        "replay_same_source_pending_receipt",
        "resume_exact_head_finalizer",
        "targeted_human_disposition",
        "controlled_serialized_restart",
        "ordinary_rollover",
    }
)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_hex(value: str, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ReviewInputError(f"invalid {label}")
    return value


@dataclass(frozen=True)
class PendingReceiptObservation:
    """Verified observation of durable `pending_reply_pages` comments on GitHub."""

    source_comment_id: int
    source_updated_digest: str
    pull_request: int
    head_sha: str
    report_digest: str
    expected_pages: int
    part_comment_ids: tuple[tuple[int, int], ...]
    complete: bool
    duplicate_conflict: bool = False
    content_digests_valid: bool = True

    def __post_init__(self) -> None:
        if type(self.source_comment_id) is not int or self.source_comment_id <= 0:
            raise ReviewInputError("pending receipt source_comment_id must be positive")
        _require_hex(self.source_updated_digest, _HEX64, "source_updated_digest")
        if type(self.pull_request) is not int or self.pull_request <= 0:
            raise ReviewInputError("pending receipt pull_request must be positive")
        _require_hex(self.head_sha, _HEX40, "head_sha")
        _require_hex(self.report_digest, _HEX64, "report_digest")
        if type(self.expected_pages) is not int or self.expected_pages <= 0:
            raise ReviewInputError("pending receipt expected_pages must be positive")
        if self.complete and (
            self.duplicate_conflict
            or not self.content_digests_valid
            or len(self.part_comment_ids) != self.expected_pages
        ):
            raise ReviewInputError(
                "complete pending receipt requires valid, conflict-free pages"
            )

    @property
    def terminal_comment_id(self) -> int | None:
        if not self.part_comment_ids:
            return None
        by_part = dict(self.part_comment_ids)
        return by_part.get(self.expected_pages)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_comment_id": self.source_comment_id,
            "source_updated_digest": self.source_updated_digest,
            "pull_request": self.pull_request,
            "head_sha": self.head_sha,
            "report_digest": self.report_digest,
            "expected_pages": self.expected_pages,
            "part_comment_ids": [
                {"part": part, "comment_id": cid} for part, cid in self.part_comment_ids
            ],
            "complete": self.complete,
            "duplicate_conflict": self.duplicate_conflict,
            "content_digests_valid": self.content_digests_valid,
        }


@dataclass(frozen=True)
class PendingReassessmentWindow:
    """Single bounded reassessment attempt outcome used for progress accounting."""

    source_comment_id: int
    source_updated_at: str
    source_body_sha256: str
    head_sha: str
    base_sha: str
    pending_before: tuple[str, ...]
    pending_after: tuple[str, ...]
    report_digest: str | None = None
    receipt_comment_id: int | None = None
    evidence_diagnostic: str | None = None

    def __post_init__(self) -> None:
        if type(self.source_comment_id) is not int or self.source_comment_id <= 0:
            raise ReviewInputError("window source_comment_id must be positive")
        if not isinstance(self.source_updated_at, str) or not self.source_updated_at:
            raise ReviewInputError("window source_updated_at must be non-empty")
        _require_hex(self.source_body_sha256, _HEX64, "source_body_sha256")
        _require_hex(self.head_sha, _HEX40, "head_sha")
        _require_hex(self.base_sha, _HEX40, "base_sha")
        if not set(self.pending_after).issubset(set(self.pending_before)):
            raise ReviewInputError(
                "reassessment window cannot invent new pending finding IDs"
            )
        if self.report_digest is not None:
            _require_hex(self.report_digest, _HEX64, "report_digest")
        if self.receipt_comment_id is not None and (
            type(self.receipt_comment_id) is not int or self.receipt_comment_id <= 0
        ):
            raise ReviewInputError("window receipt_comment_id must be positive")

    @property
    def event_key(self) -> tuple[int, str, str, str]:
        return (
            self.source_comment_id,
            self.source_updated_at,
            self.source_body_sha256,
            self.head_sha,
        )

    @property
    def newly_resolved_ids(self) -> tuple[str, ...]:
        remaining = set(self.pending_after)
        return tuple(fid for fid in self.pending_before if fid not in remaining)

    @property
    def zero_decision(self) -> bool:
        return len(self.newly_resolved_ids) == 0 and len(self.pending_after) > 0


@dataclass(frozen=True)
class ReassessmentProgressSummary:
    """Deterministic attribution and no-progress accounting across windows."""

    distinct_window_count: int
    duplicate_replay_count: int
    consecutive_zero_decision_attempts: int
    max_zero_decision_attempts: int
    no_progress_exhausted: bool
    remaining_pending_ids: tuple[str, ...]
    cumulative_resolved_ids: tuple[str, ...]
    attributed_receipt_digests: tuple[str, ...]
    attributed_source_ids: tuple[int, ...]
    authority: bool = False

    def __post_init__(self) -> None:
        if self.authority is not False:
            raise ReviewInputError(
                "reassessment progress summary never grants authority"
            )
        if (
            self.distinct_window_count < 0
            or self.duplicate_replay_count < 0
            or self.consecutive_zero_decision_attempts < 0
            or self.max_zero_decision_attempts <= 0
        ):
            raise ReviewInputError("reassessment progress counters must be valid")


@dataclass(frozen=True)
class PendingReassessmentAttribution:
    """Exact classification separating unfinished assessment from failed finalizer."""

    classifier_id: str
    state_code: str
    terminal: bool
    assessment_unfinished: bool
    zero_decisions_recorded: bool
    finalizer_entered: bool
    finalizer_failed: bool
    source_comment_id: int
    source_updated_at: str
    source_body_sha256: str
    source_kind: str
    source_actor: str | None
    bound_base_sha: str
    bound_head_sha: str
    current_base_sha: str
    current_head_sha: str
    pending_finding_ids: tuple[str, ...]
    resolved_finding_ids: tuple[str, ...]
    newly_resolved_finding_ids: tuple[str, ...]
    pending_receipt: PendingReceiptObservation | None
    prior_receipt_digests: tuple[str, ...]
    consecutive_zero_decision_attempts: int
    max_zero_decision_attempts: int
    evidence_diagnostic: str | None
    predecessor_run_id: int | None
    operation_key: str | None
    primary_route: str
    fallback_routes: tuple[str, ...]
    operator_action: str
    authority: bool = False

    def __post_init__(self) -> None:
        if self.authority is not False:
            raise ReviewInputError(
                "pending reassessment attribution never grants runtime authority"
            )
        if self.classifier_id != PENDING_REASSESSMENT_CLASSIFIER_ID:
            raise ReviewInputError("unsupported pending reassessment classifier")
        if self.state_code not in PENDING_REASSESSMENT_STATES:
            raise ReviewInputError(
                f"unsupported pending reassessment state: {self.state_code}"
            )
        if self.primary_route not in PENDING_REASSESSMENT_ROUTES or any(
            route not in PENDING_REASSESSMENT_ROUTES for route in self.fallback_routes
        ):
            raise ReviewInputError("unsupported pending reassessment recovery route")
        _require_hex(self.source_body_sha256, _HEX64, "source_body_sha256")
        _require_hex(self.bound_base_sha, _HEX40, "bound_base_sha")
        _require_hex(self.bound_head_sha, _HEX40, "bound_head_sha")
        _require_hex(self.current_base_sha, _HEX40, "current_base_sha")
        _require_hex(self.current_head_sha, _HEX40, "current_head_sha")
        if self.state_code == "unfinished_zero_decision_assessment" and (
            not self.assessment_unfinished
            or not self.zero_decisions_recorded
            or self.finalizer_entered
            or self.finalizer_failed
        ):
            raise ReviewInputError(
                "unfinished zero-decision assessment cannot mark finalizer entered or failed"
            )
        if self.state_code == "failed_finalizer_after_reassessment" and (
            not self.finalizer_entered or not self.finalizer_failed
        ):
            raise ReviewInputError(
                "failed_finalizer_after_reassessment requires finalizer_entered and finalizer_failed"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "classifier_id": self.classifier_id,
            "authority": False,
            "state_code": self.state_code,
            "terminal": self.terminal,
            "assessment_unfinished": self.assessment_unfinished,
            "zero_decisions_recorded": self.zero_decisions_recorded,
            "finalizer_entered": self.finalizer_entered,
            "finalizer_failed": self.finalizer_failed,
            "source_comment_id": self.source_comment_id,
            "source_updated_at": self.source_updated_at,
            "source_body_sha256": self.source_body_sha256,
            "source_kind": self.source_kind,
            "source_actor": self.source_actor,
            "bound_base_sha": self.bound_base_sha,
            "bound_head_sha": self.bound_head_sha,
            "current_base_sha": self.current_base_sha,
            "current_head_sha": self.current_head_sha,
            "pending_finding_ids": list(self.pending_finding_ids),
            "resolved_finding_ids": list(self.resolved_finding_ids),
            "newly_resolved_finding_ids": list(self.newly_resolved_finding_ids),
            "pending_receipt": (
                self.pending_receipt.to_dict()
                if self.pending_receipt is not None
                else None
            ),
            "prior_receipt_digests": list(self.prior_receipt_digests),
            "consecutive_zero_decision_attempts": (
                self.consecutive_zero_decision_attempts
            ),
            "max_zero_decision_attempts": self.max_zero_decision_attempts,
            "evidence_diagnostic": self.evidence_diagnostic,
            "predecessor_run_id": self.predecessor_run_id,
            "operation_key": self.operation_key,
            "primary_route": self.primary_route,
            "fallback_routes": list(self.fallback_routes),
            "operator_action": self.operator_action,
        }


@dataclass(frozen=True)
class PendingReassessmentRecoveryDescriptor:
    """Authenticated operator recovery descriptor for an unclosed pending reassessment."""

    classifier_id: str
    attribution_state: str
    predecessor_run_id: int
    predecessor_root_sha256: str
    retained_nonstartup_caller_liability: int
    retained_total_caller_liability: int
    pending_finding_ids: tuple[str, ...]
    prior_receipt_digests: tuple[str, ...]
    controlled_restart_registration: dict[str, Any]
    operator_instructions: str
    authority: bool = False

    def __post_init__(self) -> None:
        if self.authority is not False:
            raise ReviewInputError(
                "pending reassessment recovery descriptor never grants authority"
            )
        if self.classifier_id != PENDING_REASSESSMENT_CLASSIFIER_ID:
            raise ReviewInputError("unsupported pending reassessment classifier")

    @property
    def request_id(self) -> str:
        return str(self.controlled_restart_registration["request_id"])

    def registry_document(self) -> dict[str, Any]:
        return {
            "schema_version": "controlled-restart-registry-v1",
            "registrations": [dict(self.controlled_restart_registration)],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "classifier_id": self.classifier_id,
            "authority": False,
            "attribution_state": self.attribution_state,
            "predecessor_run_id": self.predecessor_run_id,
            "predecessor_root_sha256": self.predecessor_root_sha256,
            "retained_nonstartup_caller_liability": (
                self.retained_nonstartup_caller_liability
            ),
            "retained_total_caller_liability": self.retained_total_caller_liability,
            "pending_finding_ids": list(self.pending_finding_ids),
            "prior_receipt_digests": list(self.prior_receipt_digests),
            "controlled_restart_registration": dict(
                self.controlled_restart_registration
            ),
            "operator_instructions": self.operator_instructions,
        }


def inspect_durable_pending_receipts(
    *,
    http: Any,
    token: str,
    repository: str,
    pull_request: int,
    source_kind: str,
    source_comment_id: int,
    source_updated_at: str,
    head_sha: str,
    app_slug: str,
    expected_report_digest: str | None = None,
) -> PendingReceiptObservation | None:
    """Scan GitHub comments for durable `pending_reply_pages` receipts.

    Validates Bot authorship, content SHA-256 integrity, page completeness, and
    absence of duplicate or conflicting report digests/pages for the bound
    `(source_comment_id, source_updated_at, head_sha)`.
    """
    if source_kind not in {"inline", "issue", "review_comment", "issue_comment"}:
        raise ReviewInputError("invalid pending receipt source_kind")
    _require_hex(head_sha, _HEX40, "head_sha")
    if expected_report_digest is not None:
        _require_hex(expected_report_digest, _HEX64, "expected_report_digest")

    updated_digest = _sha256_text(source_updated_at)
    base_marker = (
        reply_marker(
            source_comment_id=source_comment_id,
            source_updated_digest=updated_digest,
            pull_request=pull_request,
            head_sha=head_sha,
        )[:-4]
        + " report="
    )
    normalized_kind = (
        "inline" if source_kind in {"inline", "review_comment"} else "issue"
    )
    path = http.repository_path(
        repository,
        f"/pulls/{pull_request}/comments"
        if normalized_kind == "inline"
        else f"/issues/{pull_request}/comments",
    )
    comments = http.paginate(
        path=path, token=token, page_sizes=CONVERSATION_COMMENT_PAGE_SIZES
    )

    observed_reports: set[str] = set()
    observed_counts: set[int] = set()
    parts_by_index: dict[int, int] = {}
    duplicate_conflict = False
    digests_valid = True

    for comment in comments:
        if not isinstance(comment, dict):
            continue
        user = comment.get("user")
        body = comment.get("body")
        if (
            not isinstance(user, dict)
            or user.get("login") != app_slug
            or not isinstance(body, str)
            or base_marker not in body
        ):
            continue
        if (
            user.get("type") != "Bot"
            or len(body.encode("utf-8")) > MAX_REPLY_BYTES
            or type(comment.get("id")) is not int
            or comment["id"] < 1
        ):
            duplicate_conflict = True
            digests_valid = False
            continue
        content, sep, after_base = body.rpartition("\n\n" + base_marker)
        if not sep or len(after_base) < 70 or not after_base[64:].startswith(" part="):
            duplicate_conflict = True
            digests_valid = False
            continue
        report_digest = after_base[:64]
        if _HEX64.fullmatch(report_digest) is None:
            duplicate_conflict = True
            digests_valid = False
            continue
        if (
            expected_report_digest is not None
            and report_digest != expected_report_digest
        ):
            duplicate_conflict = True
            continue
        tail_match = _PART_TAIL_RE.fullmatch(after_base[70:])
        if tail_match is None:
            duplicate_conflict = True
            digests_valid = False
            continue
        part_idx = int(tail_match[1])
        total_pages = int(tail_match[2])
        claimed_content_sha = tail_match[3]
        if part_idx < 1 or part_idx > total_pages or MARKER_PREFIX in content:
            duplicate_conflict = True
            digests_valid = False
            continue
        actual_content_sha = _sha256_text(content)
        if actual_content_sha != claimed_content_sha:
            digests_valid = False
        observed_reports.add(report_digest)
        observed_counts.add(total_pages)
        if part_idx in parts_by_index:
            duplicate_conflict = True
        else:
            parts_by_index[part_idx] = comment["id"]

    if not observed_reports and not duplicate_conflict:
        return None
    if len(observed_reports) > 1 or len(observed_counts) > 1:
        duplicate_conflict = True
    chosen_report = (
        expected_report_digest
        if expected_report_digest is not None
        else (sorted(observed_reports)[0] if observed_reports else "0" * 64)
    )
    chosen_total = min(observed_counts) if observed_counts else 1
    ordered_parts = tuple(sorted(parts_by_index.items()))
    complete = (
        not duplicate_conflict
        and digests_valid
        and len(ordered_parts) == chosen_total
        and [idx for idx, _ in ordered_parts] == list(range(1, chosen_total + 1))
    )
    return PendingReceiptObservation(
        source_comment_id=source_comment_id,
        source_updated_digest=updated_digest,
        pull_request=pull_request,
        head_sha=head_sha,
        report_digest=chosen_report,
        expected_pages=chosen_total,
        part_comment_ids=ordered_parts,
        complete=complete,
        duplicate_conflict=duplicate_conflict,
        content_digests_valid=digests_valid,
    )


def evaluate_reassessment_progress(
    windows: Sequence[PendingReassessmentWindow],
    *,
    initial_pending_ids: Sequence[str],
    max_zero_decision_attempts: int = DEFAULT_MAX_ZERO_DECISION_ATTEMPTS,
) -> ReassessmentProgressSummary:
    """Evaluate multi-window reassessment progress, duplicate replays, and exhaustion.

    - Exact duplicate `(source_comment_id, source_updated_at, body_sha256, head_sha)`
      replays are counted in `duplicate_replay_count` and never double-charge the
      no-progress allowance or duplicate attributed receipt digests.
    - Productive windows (`len(newly_resolved_ids) > 0`) reset
      `consecutive_zero_decision_attempts` to 0 while preserving prior source/receipt
      attribution.
    - Consecutive zero-decision windows reaching `max_zero_decision_attempts`
      mark `no_progress_exhausted=True` and forbid further unchanged reassessment
      inference until new evidence, explicit human disposition, or full rescan.
    """
    if type(max_zero_decision_attempts) is not int or max_zero_decision_attempts <= 0:
        raise ReviewInputError("max_zero_decision_attempts must be a positive integer")
    current_pending = list(initial_pending_ids)
    cumulative_resolved: list[str] = []
    seen_events: dict[tuple[int, str, str, str], PendingReassessmentWindow] = {}
    receipt_digests: list[str] = []
    source_ids: list[int] = []
    distinct_count = 0
    duplicate_count = 0
    consecutive_zero = 0
    active_head: str | None = None
    active_base: str | None = None

    for window in windows:
        if not isinstance(window, PendingReassessmentWindow):
            raise ReviewInputError("invalid PendingReassessmentWindow entry")
        if active_head is None:
            active_head = window.head_sha
            active_base = window.base_sha
        elif window.head_sha != active_head or window.base_sha != active_base:
            raise ReviewInputError(
                "reassessment progress sequence cannot silently span head or base drift"
            )
        if window.event_key in seen_events:
            prior = seen_events[window.event_key]
            if (
                window.pending_after != prior.pending_after
                or window.report_digest != prior.report_digest
            ):
                raise ReviewInputError(
                    "duplicate reassessment replay conflicts with recorded pending outcome"
                )
            duplicate_count += 1
            continue
        if tuple(window.pending_before) != tuple(current_pending):
            raise ReviewInputError(
                "reassessment window pending_before diverges from current pending inventory"
            )
        seen_events[window.event_key] = window
        distinct_count += 1
        if window.source_comment_id not in source_ids:
            source_ids.append(window.source_comment_id)
        if (
            window.report_digest is not None
            and window.report_digest not in receipt_digests
        ):
            receipt_digests.append(window.report_digest)
        for resolved_id in window.newly_resolved_ids:
            if resolved_id not in cumulative_resolved:
                cumulative_resolved.append(resolved_id)
        current_pending = list(window.pending_after)
        if window.zero_decision:
            consecutive_zero += 1
        else:
            consecutive_zero = 0

    return ReassessmentProgressSummary(
        distinct_window_count=distinct_count,
        duplicate_replay_count=duplicate_count,
        consecutive_zero_decision_attempts=consecutive_zero,
        max_zero_decision_attempts=max_zero_decision_attempts,
        no_progress_exhausted=(
            len(current_pending) > 0 and consecutive_zero >= max_zero_decision_attempts
        ),
        remaining_pending_ids=tuple(current_pending),
        cumulative_resolved_ids=tuple(cumulative_resolved),
        attributed_receipt_digests=tuple(receipt_digests),
        attributed_source_ids=tuple(source_ids),
    )


def qualify_pending_reassessment(
    *,
    original_eligibility: ReviewApprovalEligibility,
    latest_eligibility: ReviewApprovalEligibility | None,
    bound_source_comment_id: int,
    bound_source_updated_at: str,
    bound_source_body_sha256: str,
    bound_source_kind: str,
    bound_base_sha: str,
    bound_head_sha: str,
    current_base_sha: str,
    current_head_sha: str,
    live_source_updated_at: str | None = None,
    live_source_body_sha256: str | None = None,
    source_actor: str | None = None,
    reply_result: ReplyResult | None = None,
    pending_receipt: PendingReceiptObservation | None = None,
    record: SessionRecord | None = None,
    prior_receipt_digests: Sequence[str] = (),
    consecutive_zero_decision_attempts: int = 1,
    max_zero_decision_attempts: int = DEFAULT_MAX_ZERO_DECISION_ATTEMPTS,
) -> PendingReassessmentAttribution:
    """Classify a pending or completed human reassessment with exact attribution.

    Strictly distinguishes:
    - **Unfinished zero-decision assessment**: zero findings resolved in durable
      eligibility (`latest_eligibility == original_eligibility`), `finalizer_entered`
      is `False`, `finalizer_failed` is `False`, and durable pending receipt
      attribution is preserved.
    - **Failed finalizer after reassessment**: durable eligibility review WAS
      published (`latest_eligibility != original_eligibility` or zero pending
      items remain), `finalizer_entered` is `True`, and finalization left check,
      thread cleanup, or approval unconfirmed (`finalizer_failed=True`).
    """
    if not isinstance(original_eligibility, ReviewApprovalEligibility):
        raise ReviewInputError("original_eligibility is required")
    if original_eligibility.human_review is None:
        raise ReviewInputError("original_eligibility has no human_review inventory")
    if (
        type(max_zero_decision_attempts) is not int
        or max_zero_decision_attempts <= 0
        or type(consecutive_zero_decision_attempts) is not int
        or consecutive_zero_decision_attempts < 0
    ):
        raise ReviewInputError("zero-decision attempt bounds must be valid integers")

    orig_inv = original_eligibility.human_review
    effective_inv = (
        latest_eligibility.human_review
        if latest_eligibility is not None
        and latest_eligibility.human_review is not None
        else orig_inv
    )
    pending_ids = tuple(item.fingerprint for item in effective_inv.pending)
    resolved_ids = tuple(effective_inv.resolved)
    orig_resolved = set(orig_inv.resolved)
    newly_resolved = tuple(fid for fid in resolved_ids if fid not in orig_resolved)
    zero_decisions = len(newly_resolved) == 0 and len(orig_inv.pending) > 0

    predecessor_run_id: int | None = None
    operation_key: str | None = None
    if record is not None and record.api_run_accounting is not None:
        old = ApiRunAccounting.from_document(record.api_run_accounting)
        predecessor_run_id = old.identity.run_id
        if isinstance(old.action, Mapping) and isinstance(
            old.action.get("intent"), Mapping
        ):
            raw_key = old.action["intent"].get("operation_key")
            if isinstance(raw_key, str):
                operation_key = raw_key

    evidence_diag = (
        reply_result.assessment_diagnostic if reply_result is not None else None
    )
    normalized_prior = list(prior_receipt_digests)
    if (
        pending_receipt is not None
        and pending_receipt.complete
        and pending_receipt.report_digest not in normalized_prior
    ):
        normalized_prior.append(pending_receipt.report_digest)

    # 1. Head or base drift invalidates continuing on the bound snapshot.
    if (
        current_head_sha != bound_head_sha
        or current_base_sha != bound_base_sha
        or (
            reply_result is not None
            and reply_result.status in {"skipped_stale_head", "skipped_stale_base"}
        )
    ):
        return PendingReassessmentAttribution(
            classifier_id=PENDING_REASSESSMENT_CLASSIFIER_ID,
            state_code="pending_reassessment_head_or_base_changed",
            terminal=False,
            assessment_unfinished=len(pending_ids) > 0,
            zero_decisions_recorded=zero_decisions,
            finalizer_entered=False,
            finalizer_failed=False,
            source_comment_id=bound_source_comment_id,
            source_updated_at=bound_source_updated_at,
            source_body_sha256=bound_source_body_sha256,
            source_kind=bound_source_kind,
            source_actor=source_actor,
            bound_base_sha=bound_base_sha,
            bound_head_sha=bound_head_sha,
            current_base_sha=current_base_sha,
            current_head_sha=current_head_sha,
            pending_finding_ids=pending_ids,
            resolved_finding_ids=resolved_ids,
            newly_resolved_finding_ids=newly_resolved,
            pending_receipt=pending_receipt,
            prior_receipt_digests=tuple(normalized_prior),
            consecutive_zero_decision_attempts=consecutive_zero_decision_attempts,
            max_zero_decision_attempts=max_zero_decision_attempts,
            evidence_diagnostic=evidence_diag,
            predecessor_run_id=predecessor_run_id,
            operation_key=operation_key,
            primary_route="controlled_serialized_restart",
            fallback_routes=("targeted_human_disposition",),
            operator_action=(
                "Pull request head or base changed relative to the pending "
                "reassessment scope; do not reuse stale pending receipts. "
                "Recover any open pending action via controlled serialized restart "
                "and re-establish current-head review authority."
            ),
        )

    # 2. Source comment edit/deletion invalidates replay of the mutated comment.
    source_edited = (
        (
            live_source_updated_at is not None
            and live_source_updated_at != bound_source_updated_at
        )
        or (
            live_source_body_sha256 is not None
            and live_source_body_sha256 != bound_source_body_sha256
        )
        or (reply_result is not None and reply_result.status == "skipped_edited_source")
    )
    if source_edited:
        return PendingReassessmentAttribution(
            classifier_id=PENDING_REASSESSMENT_CLASSIFIER_ID,
            state_code="pending_reassessment_source_changed",
            terminal=False,
            assessment_unfinished=len(pending_ids) > 0,
            zero_decisions_recorded=zero_decisions,
            finalizer_entered=False,
            finalizer_failed=False,
            source_comment_id=bound_source_comment_id,
            source_updated_at=bound_source_updated_at,
            source_body_sha256=bound_source_body_sha256,
            source_kind=bound_source_kind,
            source_actor=source_actor,
            bound_base_sha=bound_base_sha,
            bound_head_sha=bound_head_sha,
            current_base_sha=current_base_sha,
            current_head_sha=current_head_sha,
            pending_finding_ids=pending_ids,
            resolved_finding_ids=resolved_ids,
            newly_resolved_finding_ids=newly_resolved,
            pending_receipt=pending_receipt,
            prior_receipt_digests=tuple(normalized_prior),
            consecutive_zero_decision_attempts=consecutive_zero_decision_attempts,
            max_zero_decision_attempts=max_zero_decision_attempts,
            evidence_diagnostic=evidence_diag,
            predecessor_run_id=predecessor_run_id,
            operation_key=operation_key,
            primary_route="delayed_followup_authorized_mention",
            fallback_routes=(
                "targeted_human_disposition",
                "controlled_serialized_restart",
            ),
            operator_action=(
                "Source comment was edited or removed after admission; old source "
                "authority is invalidated. Submit a new standalone authorized "
                "`@reviewsensei` mention (or recover an open pending action via "
                "controlled serialized restart)."
            ),
        )

    # 3. Duplicate or corrupt pending receipt conflict on GitHub.
    if pending_receipt is not None and (
        pending_receipt.duplicate_conflict or not pending_receipt.content_digests_valid
    ):
        return PendingReassessmentAttribution(
            classifier_id=PENDING_REASSESSMENT_CLASSIFIER_ID,
            state_code="pending_reassessment_duplicate_receipt_conflict",
            terminal=False,
            assessment_unfinished=len(pending_ids) > 0,
            zero_decisions_recorded=zero_decisions,
            finalizer_entered=False,
            finalizer_failed=False,
            source_comment_id=bound_source_comment_id,
            source_updated_at=bound_source_updated_at,
            source_body_sha256=bound_source_body_sha256,
            source_kind=bound_source_kind,
            source_actor=source_actor,
            bound_base_sha=bound_base_sha,
            bound_head_sha=bound_head_sha,
            current_base_sha=current_base_sha,
            current_head_sha=current_head_sha,
            pending_finding_ids=pending_ids,
            resolved_finding_ids=resolved_ids,
            newly_resolved_finding_ids=newly_resolved,
            pending_receipt=pending_receipt,
            prior_receipt_digests=tuple(normalized_prior),
            consecutive_zero_decision_attempts=consecutive_zero_decision_attempts,
            max_zero_decision_attempts=max_zero_decision_attempts,
            evidence_diagnostic=evidence_diag,
            predecessor_run_id=predecessor_run_id,
            operation_key=operation_key,
            primary_route="controlled_serialized_restart",
            fallback_routes=("targeted_human_disposition",),
            operator_action=(
                "Conflicting or corrupted duplicate pending-receipt pages detected "
                "for the same source and head; refuse silent reconciliation and "
                "recover via controlled serialized restart."
            ),
        )

    # 4. Durably recorded decisions (`latest_eligibility != original_eligibility`
    #    or `not orig_inv.pending`) entered finalization path!
    decisions_durably_published = (
        latest_eligibility is not None
        and latest_eligibility != original_eligibility
        and len(newly_resolved) > 0
    ) or len(orig_inv.pending) == 0

    if decisions_durably_published:
        finalizer_confirmed = (
            reply_result is not None
            and reply_result.finalization_completion is not None
            and reply_result.resolution_status not in {"unconfirmed", "failed"}
            and reply_result.approval_status
            not in {None, "unconfirmed", "not-observed", "failed"}
        )
        if not finalizer_confirmed:
            return PendingReassessmentAttribution(
                classifier_id=PENDING_REASSESSMENT_CLASSIFIER_ID,
                state_code="failed_finalizer_after_reassessment",
                terminal=False,
                assessment_unfinished=len(pending_ids) > 0,
                zero_decisions_recorded=False,
                finalizer_entered=True,
                finalizer_failed=True,
                source_comment_id=bound_source_comment_id,
                source_updated_at=bound_source_updated_at,
                source_body_sha256=bound_source_body_sha256,
                source_kind=bound_source_kind,
                source_actor=source_actor,
                bound_base_sha=bound_base_sha,
                bound_head_sha=bound_head_sha,
                current_base_sha=current_base_sha,
                current_head_sha=current_head_sha,
                pending_finding_ids=pending_ids,
                resolved_finding_ids=resolved_ids,
                newly_resolved_finding_ids=newly_resolved,
                pending_receipt=pending_receipt,
                prior_receipt_digests=tuple(normalized_prior),
                consecutive_zero_decision_attempts=0,
                max_zero_decision_attempts=max_zero_decision_attempts,
                evidence_diagnostic=evidence_diag,
                predecessor_run_id=predecessor_run_id,
                operation_key=operation_key,
                primary_route="resume_exact_head_finalizer",
                fallback_routes=("controlled_serialized_restart",),
                operator_action=(
                    "Reassessment decisions are already durably recorded in the "
                    "eligibility review, and the subsequent finalizer (check, "
                    "thread cleanup, or native approval) failed or remained "
                    "unconfirmed. Resume exact-head finalization (`@reviewsensei "
                    "verify` or recorded-decision replay) without re-running model inference."
                ),
            )
        if len(pending_ids) > 0:
            return PendingReassessmentAttribution(
                classifier_id=PENDING_REASSESSMENT_CLASSIFIER_ID,
                state_code="unfinished_partial_decision_assessment",
                terminal=True,
                assessment_unfinished=True,
                zero_decisions_recorded=False,
                finalizer_entered=True,
                finalizer_failed=False,
                source_comment_id=bound_source_comment_id,
                source_updated_at=bound_source_updated_at,
                source_body_sha256=bound_source_body_sha256,
                source_kind=bound_source_kind,
                source_actor=source_actor,
                bound_base_sha=bound_base_sha,
                bound_head_sha=bound_head_sha,
                current_base_sha=current_base_sha,
                current_head_sha=current_head_sha,
                pending_finding_ids=pending_ids,
                resolved_finding_ids=resolved_ids,
                newly_resolved_finding_ids=newly_resolved,
                pending_receipt=pending_receipt,
                prior_receipt_digests=tuple(normalized_prior),
                consecutive_zero_decision_attempts=0,
                max_zero_decision_attempts=max_zero_decision_attempts,
                evidence_diagnostic=evidence_diag,
                predecessor_run_id=predecessor_run_id,
                operation_key=operation_key,
                primary_route="delayed_followup_authorized_mention",
                fallback_routes=(
                    "targeted_human_disposition",
                    "ordinary_rollover",
                ),
                operator_action=(
                    "Partial reassessment cleared a subset of findings and settled "
                    "finalization cleanly while remaining human findings stay open. "
                    "Submit a new authorized mention with evidence for remaining IDs."
                ),
            )
        return PendingReassessmentAttribution(
            classifier_id=PENDING_REASSESSMENT_CLASSIFIER_ID,
            state_code="reassessment_completed_settled",
            terminal=True,
            assessment_unfinished=False,
            zero_decisions_recorded=False,
            finalizer_entered=True,
            finalizer_failed=False,
            source_comment_id=bound_source_comment_id,
            source_updated_at=bound_source_updated_at,
            source_body_sha256=bound_source_body_sha256,
            source_kind=bound_source_kind,
            source_actor=source_actor,
            bound_base_sha=bound_base_sha,
            bound_head_sha=bound_head_sha,
            current_base_sha=current_base_sha,
            current_head_sha=current_head_sha,
            pending_finding_ids=(),
            resolved_finding_ids=resolved_ids,
            newly_resolved_finding_ids=newly_resolved,
            pending_receipt=pending_receipt,
            prior_receipt_digests=tuple(normalized_prior),
            consecutive_zero_decision_attempts=0,
            max_zero_decision_attempts=max_zero_decision_attempts,
            evidence_diagnostic=evidence_diag,
            predecessor_run_id=predecessor_run_id,
            operation_key=operation_key,
            primary_route="ordinary_rollover",
            fallback_routes=(),
            operator_action=(
                "All human reassessment findings are durably resolved and "
                "finalization settled cleanly."
            ),
        )

    # 5. Zero-decision reassessment path (`finalizer_entered=False`):
    #    Check whether the durable pending receipt itself finished delivering.
    receipt_incomplete = (
        pending_receipt is None
        or not pending_receipt.complete
        or (
            reply_result is not None
            and (
                reply_result.assessment_status == "pending_delivery_incomplete"
                or reply_result.status == "pending_delivery_incomplete"
            )
        )
    )
    if receipt_incomplete:
        return PendingReassessmentAttribution(
            classifier_id=PENDING_REASSESSMENT_CLASSIFIER_ID,
            state_code="pending_receipt_delivery_incomplete",
            terminal=False,
            assessment_unfinished=True,
            zero_decisions_recorded=True,
            finalizer_entered=False,
            finalizer_failed=False,
            source_comment_id=bound_source_comment_id,
            source_updated_at=bound_source_updated_at,
            source_body_sha256=bound_source_body_sha256,
            source_kind=bound_source_kind,
            source_actor=source_actor,
            bound_base_sha=bound_base_sha,
            bound_head_sha=bound_head_sha,
            current_base_sha=current_base_sha,
            current_head_sha=current_head_sha,
            pending_finding_ids=pending_ids,
            resolved_finding_ids=resolved_ids,
            newly_resolved_finding_ids=(),
            pending_receipt=pending_receipt,
            prior_receipt_digests=tuple(normalized_prior),
            consecutive_zero_decision_attempts=consecutive_zero_decision_attempts,
            max_zero_decision_attempts=max_zero_decision_attempts,
            evidence_diagnostic=evidence_diag,
            predecessor_run_id=predecessor_run_id,
            operation_key=operation_key,
            primary_route="replay_same_source_pending_receipt",
            fallback_routes=(
                "delayed_followup_authorized_mention",
                "controlled_serialized_restart",
            ),
            operator_action=(
                "Zero-decision reassessment stopped before confirming all "
                "pending-receipt pages; replay the stored operation receipt in a "
                "fresh process without repeating inference, or isolate via "
                "controlled serialized restart."
            ),
        )

    # 6. Complete pending receipt with zero cleared decisions: check no-progress cap.
    if consecutive_zero_decision_attempts >= max_zero_decision_attempts:
        return PendingReassessmentAttribution(
            classifier_id=PENDING_REASSESSMENT_CLASSIFIER_ID,
            state_code="pending_reassessment_no_progress_exhausted",
            terminal=False,
            assessment_unfinished=True,
            zero_decisions_recorded=True,
            finalizer_entered=False,
            finalizer_failed=False,
            source_comment_id=bound_source_comment_id,
            source_updated_at=bound_source_updated_at,
            source_body_sha256=bound_source_body_sha256,
            source_kind=bound_source_kind,
            source_actor=source_actor,
            bound_base_sha=bound_base_sha,
            bound_head_sha=bound_head_sha,
            current_base_sha=current_base_sha,
            current_head_sha=current_head_sha,
            pending_finding_ids=pending_ids,
            resolved_finding_ids=resolved_ids,
            newly_resolved_finding_ids=(),
            pending_receipt=pending_receipt,
            prior_receipt_digests=tuple(normalized_prior),
            consecutive_zero_decision_attempts=consecutive_zero_decision_attempts,
            max_zero_decision_attempts=max_zero_decision_attempts,
            evidence_diagnostic=evidence_diag,
            predecessor_run_id=predecessor_run_id,
            operation_key=operation_key,
            primary_route="targeted_human_disposition",
            fallback_routes=("controlled_serialized_restart",),
            operator_action=(
                f"Reached {consecutive_zero_decision_attempts}/"
                f"{max_zero_decision_attempts} consecutive zero-decision "
                "reassessment attempts with no finding progress. Do not loop "
                "unchanged mention replies; apply explicit maintainer disposition "
                "(`@reviewsensei override` / `accept-risk`), push a code fix, or "
                "recover an unclosed pending action via controlled serialized restart."
            ),
        )

    return PendingReassessmentAttribution(
        classifier_id=PENDING_REASSESSMENT_CLASSIFIER_ID,
        state_code="unfinished_zero_decision_assessment",
        terminal=False,
        assessment_unfinished=True,
        zero_decisions_recorded=True,
        finalizer_entered=False,
        finalizer_failed=False,
        source_comment_id=bound_source_comment_id,
        source_updated_at=bound_source_updated_at,
        source_body_sha256=bound_source_body_sha256,
        source_kind=bound_source_kind,
        source_actor=source_actor,
        bound_base_sha=bound_base_sha,
        bound_head_sha=bound_head_sha,
        current_base_sha=current_base_sha,
        current_head_sha=current_head_sha,
        pending_finding_ids=pending_ids,
        resolved_finding_ids=resolved_ids,
        newly_resolved_finding_ids=(),
        pending_receipt=pending_receipt,
        prior_receipt_digests=tuple(normalized_prior),
        consecutive_zero_decision_attempts=consecutive_zero_decision_attempts,
        max_zero_decision_attempts=max_zero_decision_attempts,
        evidence_diagnostic=evidence_diag,
        predecessor_run_id=predecessor_run_id,
        operation_key=operation_key,
        primary_route="delayed_followup_authorized_mention",
        fallback_routes=(
            "targeted_human_disposition",
            "controlled_serialized_restart",
        ),
        operator_action=(
            "Assessment finished with zero cleared findings and published a "
            "durable pending receipt (`finalizer_entered=False`; finalizer did "
            "NOT fail). Do not duplicate this receipt; provide current-head code "
            "and human evidence in a new authorized follow-up mention, or recover "
            "an unclosed `action.pending` ledger via controlled serialized restart."
        ),
    )


def build_pending_reassessment_recovery_descriptor(
    *,
    identity: SessionIdentity,
    record: SessionRecord,
    attribution: PendingReassessmentAttribution,
    owner_ceased: bool,
    old_comments: Sequence[Mapping[str, Any]],
    designated_run_id: str,
    base_sha: str,
    head_sha: str,
    policy_commit: str,
    workflow_sha: str,
    issuance_disabled_at: int,
    retire_after: int,
    job: str = "hosted",
    extra_old_runs: Sequence[int] = (),
) -> PendingReassessmentRecoveryDescriptor:
    """Build a schema-validated `controlled-restart-registry-v1` recovery descriptor.

    Ensures a pull request whose session ledger remained in `action.phase == "pending"`
    after a zero-decision reassessment, head advance, or no-progress exhaustion
    is never permanently stranded, while preserving conservative caller liability
    and exact pending finding + receipt attribution.
    """
    if not isinstance(identity, SessionIdentity) or not isinstance(
        record, SessionRecord
    ):
        raise ReviewInputError(
            "pending reassessment recovery descriptor requires valid identity and record"
        )
    if record.api_run_accounting is None:
        raise ReviewInputError(
            "pending reassessment recovery descriptor requires API-accounted state"
        )
    if attribution.terminal:
        raise ReviewInputError(
            "settled reassessment does not require controlled restart recovery"
        )
    if not owner_ceased:
        raise ReviewInputError(
            "cannot issue pending reassessment recovery descriptor before owner ceases"
        )

    old = ApiRunAccounting.from_document(record.api_run_accounting)
    nonstartup = (
        old.prior_nonstartup_liability
        + old.confirmed
        + old.uncertain
        + old.reserved_undispatched
    )
    total = old.startup.liability + nonstartup

    old_run_ids: list[int] = [old.identity.run_id]
    for run_id in extra_old_runs:
        if type(run_id) is not int or run_id <= 0:
            raise ReviewInputError(
                "extra_old_runs must contain positive integer run IDs"
            )
        if run_id not in old_run_ids:
            old_run_ids.append(run_id)

    row_body: dict[str, Any] = {
        "repository": identity.repository,
        "repository_id": identity.repository_id,
        "pull_request": identity.pull_request,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "policy_commit": policy_commit,
        "workflow_sha": workflow_sha,
        "run_id": designated_run_id,
        "job": job,
        "issuance_disabled_at": issuance_disabled_at,
        "retire_after": retire_after,
        "old_runs": old_run_ids,
        "old_comments": [dict(ref) for ref in old_comments],
    }
    row_body["request_id"] = hashlib.sha256(canonical_bytes(row_body)).hexdigest()
    registry_bytes = canonical_bytes(
        {
            "schema_version": "controlled-restart-registry-v1",
            "registrations": [row_body],
        }
    )
    validated_row = controlled_restart.read_registry(registry_bytes)[0]

    return PendingReassessmentRecoveryDescriptor(
        classifier_id=PENDING_REASSESSMENT_CLASSIFIER_ID,
        attribution_state=attribution.state_code,
        predecessor_run_id=old.identity.run_id,
        predecessor_root_sha256=record.record_sha256,
        retained_nonstartup_caller_liability=nonstartup,
        retained_total_caller_liability=total,
        pending_finding_ids=attribution.pending_finding_ids,
        prior_receipt_digests=attribution.prior_receipt_digests,
        controlled_restart_registration=validated_row,
        operator_instructions=(
            f"Register request_id={validated_row['request_id']} in "
            "`controlled_restarts.json`, wait until retire_after="
            f"{validated_row['retire_after']}, and dispatch `restart-run "
            f"--restart-id {validated_row['request_id']}` on run_id="
            f"{validated_row['run_id']}. Preserve pending finding obligations "
            f"({len(attribution.pending_finding_ids)} item(s)) across recovery."
        ),
    )


__all__ = [
    "DEFAULT_MAX_ZERO_DECISION_ATTEMPTS",
    "PENDING_REASSESSMENT_CLASSIFIER_ID",
    "PENDING_REASSESSMENT_ROUTES",
    "PENDING_REASSESSMENT_STATES",
    "PendingReceiptObservation",
    "PendingReassessmentAttribution",
    "PendingReassessmentRecoveryDescriptor",
    "PendingReassessmentWindow",
    "ReassessmentProgressSummary",
    "build_pending_reassessment_recovery_descriptor",
    "evaluate_reassessment_progress",
    "inspect_durable_pending_receipts",
    "qualify_pending_reassessment",
]
