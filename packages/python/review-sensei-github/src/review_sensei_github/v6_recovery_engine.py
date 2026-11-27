"""ReviewSensei v6 GitHub-governed recovery orchestrator (Phases 2 & 3).

Coordinates exclusive broker writer-ownership fencing, immutable write
intent/receipt persistence, stability-verified pagination, persistent
no-progress guarding, write-response-as-readback request compression,
reconcile-before-retry lost-write recovery, and PR-resident evidence checks
without ever starting a fresh model call as a recovery fallback.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeAlias
from urllib.parse import urlsplit

from review_sensei.errors import ReviewInputError

from .errors import (
    GitHubHTTPError,
    GitHubHTTPPermissionError,
    GitHubHTTPRateLimitError,
    GitHubHTTPTransientError,
)
from .v6_credential_matrix import (
    CredentialKind,
    GitHubEventKind,
    RepositoryVisibility,
    authorize_v6_endpoint_credential,
)
from .v6_pagination import (
    NoProgressGuard,
    PaginatedRecord,
    StabilityVerifiedPaginator,
)
from .v6_recovery_ledger import (
    VALID_NON_SUBMISSION_PROOF_KINDS,
    AdvisoryProgressHint,
    CompletionReceiptRecord,
    DurableHoldRecord,
    NonSubmissionProofRecord,
    OwnerMarkerRecord,
    UnresolvedResultRecord,
    V6LedgerIntegrityError,
    V6LedgerRecord,
    WriteIntentRecord,
    WriterOwnershipProof,
    canonical_sha256,
    extract_v6_record_markers,
    format_v6_record_marker,
    reconcile_immutable_records,
    redact_secrets,
    validate_owner_lineage,
    verify_app_authored_record,
)
from .v6_transport import (
    DEFAULT_MAX_IN_JOB_SLEEP_SECONDS,
    MAINTAINER_RERUN_COMMAND,
    DualCredentialGitHubClient,
    DualCredentialResponse,
    evaluate_throttle_wait,
)

DEFAULT_MUTATION_SANITY_MAX_WRITES = 16
DEFAULT_CLOCK_MARGIN_MS = 300_000
_PR_SCOPED_PATH_RE = re.compile(
    r"^/repos/([^/]+)/([^/]+)/(pulls|issues)/([1-9][0-9]*)(?:/|$)"
)
_SHA1_40_RE = re.compile(r"^[0-9a-f]{40}$")


class StaleWriterOwnershipError(ReviewInputError):
    """Raised at a write boundary when exclusive writer ownership cannot be proved."""

    error_category = "v6_stale_writer_ownership"


class MutationSanityBoundError(ReviewInputError):
    """Raised when an operation exceeds its per-operation mutation sanity bound."""

    error_category = "v6_mutation_sanity_bound"


@dataclass
class MutationSanityBound:
    """Bounded per-operation write guard independent of removed read ceilings."""

    max_writes: int = DEFAULT_MUTATION_SANITY_MAX_WRITES
    writes_attempted: int = 0
    write_history: list[tuple[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if type(self.max_writes) is not int or self.max_writes <= 0:
            raise ValueError("max_writes must be a positive integer")
        if type(self.writes_attempted) is not int or self.writes_attempted < 0:
            raise ValueError("writes_attempted must be a non-negative integer")

    def consume(self, endpoint_id: str, effect_kind: str) -> None:
        """Record an impending mutation or fail closed if ``max_writes`` is reached."""

        if self.writes_attempted + 1 > self.max_writes:
            raise MutationSanityBoundError(
                f"mutation sanity bound exceeded ({self.writes_attempted}/{self.max_writes}) "
                f"before dispatching {endpoint_id!r} ({effect_kind})",
                diagnostic="v6_mutation_sanity_bound_exceeded",
            )
        self.writes_attempted += 1
        self.write_history.append((endpoint_id, effect_kind))


@dataclass(frozen=True)
class RecoveryCompleted:
    """Terminal success outcome of v6 GitHub-governed recovery/publication."""

    operation_id: str
    pr_head_sha: str
    receipt: CompletionReceiptRecord
    github_effect_id: str | int
    reconciled_from_existing_effect: bool
    used_pr_resident_result: bool
    readback_gets_emitted: int = 0
    model_calls_started: int = 0
    owner_comment_id: int | None = None
    intent_comment_id: int | None = None
    receipt_comment_id: int | None = None
    intent_record: WriteIntentRecord | None = None

    def __post_init__(self) -> None:
        if self.model_calls_started != 0:
            raise ValueError("v6 recovery must never start a fresh model call")


@dataclass(frozen=True)
class RecoveryPaused:
    """Clean v6.0 pause outcome on long rate-limit wait awaiting maintainer rerun."""

    operation_id: str
    resume_after_epoch: float
    checkpoint_state: AdvisoryProgressHint
    reason: str
    rerun_command: str = MAINTAINER_RERUN_COMMAND
    reused_prior_durable_state: bool = False
    model_calls_started: int = 0

    def __post_init__(self) -> None:
        if self.model_calls_started != 0:
            raise ValueError("v6 recovery must never start a fresh model call")


@dataclass(frozen=True)
class RecoveryHold:
    """Fail-closed hold outcome identifying exact missing evidence or blocker."""

    operation_id: str
    blocker: str
    missing_evidence: tuple[str, ...]
    zero_model_calls_started: bool = True

    def __post_init__(self) -> None:
        if self.zero_model_calls_started is not True:
            raise ValueError(
                "RecoveryHold must guarantee zero_model_calls_started=True"
            )


RecoveryOutcome: TypeAlias = RecoveryCompleted | RecoveryPaused | RecoveryHold


@dataclass(frozen=True)
class TakeoverReconciliationResult:
    """Read-only takeover reconciliation outcome prior to write authority."""

    reconciliation_outcome: str
    adopted_effects: tuple[tuple[WriteIntentRecord, str | int], ...]
    unsubmitted_intents: tuple[WriteIntentRecord, ...]
    pending_unresolved_results: tuple[UnresolvedResultRecord, ...]
    owner_lineage: tuple[OwnerMarkerRecord, ...]
    unresolved_holds: tuple[DurableHoldRecord, ...]
    completed_operations: tuple[str, ...]
    reconciliation_evidence: Mapping[str, Any] | None = None
    old_owner_comment_id: int | None = None
    blocker: str | None = None
    missing_evidence: tuple[str, ...] = ()
    anchor_comment_id: int | None = None
    model_calls_started: int = 0

    def __post_init__(self) -> None:
        if self.reconciliation_outcome not in {
            "clean_receipts",
            "adopted_missing_receipt",
            "proved_not_submitted",
            "ambiguous_hold",
            "incomplete",
        }:
            raise ValueError(
                f"invalid reconciliation_outcome: {self.reconciliation_outcome!r}"
            )
        if self.model_calls_started != 0:
            raise ValueError("takeover reconciliation must never start model calls")


@dataclass(frozen=True)
class RecoveryRequest:
    """Inputs defining a bounded v6 recovery and publication operation."""

    owner: str
    repo: str
    repository_id: int
    pull_request: int
    operation_id: str
    ownership_proof: WriterOwnershipProof
    event: GitHubEventKind
    visibility: RepositoryVisibility
    granted_scopes: Collection[str]
    effect_kind: str = "pr_review_publish"
    effect_endpoint_id: str = "pr_review_publish"
    effect_method: str = "POST"
    effect_path: str | None = None
    target_digest: str = ""
    expected_app_id: int | None = None
    expected_bot_login: str | None = None
    expected_bot_user_id: int | None = None
    advisory_comment_id: int | None = None
    observed_evidence_digests: Collection[str] | None = None
    artifact_cache: Mapping[str, Any] | None = None
    known_records: tuple[PaginatedRecord, ...] = ()
    graphql_query: str | None = None
    graphql_stability_probe: Mapping[str, Any] | None = None
    slot_key: str | None = None
    generation: int | None = None
    token_expires_at_ms: int | None = None
    clock_margin_ms: int = DEFAULT_CLOCK_MARGIN_MS
    owner_marker: OwnerMarkerRecord | None = None
    mac_verifier: Callable[[OwnerMarkerRecord], bool] | None = None
    affirmative_non_submission: bool = False
    has_active_write_lease: bool = True
    emit_durable_hold_on_pr: bool = False
    step_summary_sink: Callable[[str], None] | Path | None = None


@dataclass(frozen=True)
class _CollectedLedger:
    owners: tuple[OwnerMarkerRecord, ...]
    intents: tuple[WriteIntentRecord, ...]
    receipts: tuple[CompletionReceiptRecord, ...]
    non_submissions: tuple[NonSubmissionProofRecord, ...]
    holds: tuple[DurableHoldRecord, ...]
    unresolved_results: tuple[UnresolvedResultRecord, ...]
    hints: tuple[AdvisoryProgressHint, ...]
    app_marker_comments: tuple[PaginatedRecord, ...]
    anchor_comment_id: int | None
    comment_ids_by_record_id: Mapping[str, int]
    owner_comment_ids_by_generation: Mapping[int, int]


class _AmbiguousWriteOutcomeError(RuntimeError):
    """Internal signal that a mutation response was lost, 5xx, or unverified."""


class _AmbiguousReceiptOutcomeError(RuntimeError):
    """Internal signal that a receipt write outcome could not be confirmed."""


class _ThrottlePausedSignal(Exception):
    """Internal control-flow carrying a clean ``RecoveryPaused`` outcome."""

    def __init__(self, outcome: RecoveryPaused) -> None:
        super().__init__(outcome.reason)
        self.outcome = outcome


def render_v6_hold_summary(
    *,
    operation_id: str,
    blocker: str,
    missing_evidence: Sequence[str] = (),
    slot_key: str | None = None,
    repository: str | None = None,
    pull_request: int | None = None,
    pr_head_sha: str | None = None,
) -> str:
    """Render a sanitized Markdown diagnostic summary for ``GITHUB_STEP_SUMMARY``."""

    lines = ["## ReviewSensei v6 recovery hold"]
    if repository:
        lines.append(f"- repository: `{redact_secrets(repository)}`")
    if type(pull_request) is int and pull_request > 0:
        lines.append(f"- pull_request: #{pull_request}")
    if slot_key:
        lines.append(f"- slot_key: `{redact_secrets(slot_key)}`")
    if pr_head_sha:
        lines.append(f"- head: `{redact_secrets(pr_head_sha[:12])}`")
    lines.append(f"- operation_id: `{redact_secrets(operation_id)}`")
    lines.append(f"- blocker: `{redact_secrets(blocker)}`")
    if missing_evidence:
        cleaned = ", ".join(f"`{redact_secrets(item)}`" for item in missing_evidence)
        lines.append(f"- missing_evidence: {cleaned}")
    lines.append(
        "- model_calls: 0 (fail-closed hold without mutating PR or clearing obligations)"
    )
    return "\n".join(lines) + "\n"


def write_v6_step_summary(
    markdown: str,
    *,
    sink: Callable[[str], None] | Path | None = None,
) -> bool:
    """Write sanitized Markdown to a configurable sink or ``GITHUB_STEP_SUMMARY``."""

    sanitized = redact_secrets(markdown)
    if callable(sink):
        sink(sanitized)
        return True
    target: Path | None = None
    if isinstance(sink, Path):
        target = sink
    else:
        env_path = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
        if env_path:
            target = Path(env_path)
    if target is None:
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(sanitized)
        return True
    except OSError:
        return False


def _assert_target_pr_binding(
    request: RecoveryRequest,
    *,
    path: str,
) -> None:
    """Refuse any cross-repository or cross-PR mutation despite repo-scoped tokens."""

    proof = request.ownership_proof
    if (
        proof.repository_id != request.repository_id
        or proof.pull_request != request.pull_request
    ):
        raise StaleWriterOwnershipError(
            "writer ownership proof target mismatch: "
            f"proof=({proof.repository_id},{proof.pull_request}) != "
            f"request=({request.repository_id},{request.pull_request})",
            diagnostic="v6_cross_pr_write_refused",
        )
    if request.owner_marker is not None and (
        request.owner_marker.pr_head_sha != proof.pr_head_sha
        or (
            request.slot_key is not None
            and request.owner_marker.slot_key != request.slot_key
        )
    ):
        raise StaleWriterOwnershipError(
            "owner_marker binding does not match active lease slot/head",
            diagnostic="v6_cross_pr_write_refused",
        )

    parsed_path = urlsplit(path).path
    if parsed_path == "/graphql":
        return
    expected_prefix = f"/repos/{request.owner}/{request.repo}/"
    if not parsed_path.startswith(expected_prefix):
        raise StaleWriterOwnershipError(
            f"refusing mutation outside leased repository {request.owner}/{request.repo}: {parsed_path!r}",
            diagnostic="v6_cross_pr_write_refused",
        )
    pr_match = _PR_SCOPED_PATH_RE.match(parsed_path)
    if pr_match is not None and int(pr_match.group(4)) != request.pull_request:
        raise StaleWriterOwnershipError(
            f"refusing cross-PR mutation targeting PR #{pr_match.group(4)} "
            f"under lease for PR #{request.pull_request}",
            diagnostic="v6_cross_pr_write_refused",
        )


def verify_writer_ownership(
    proof: WriterOwnershipProof,
    *,
    live_repository_id: int,
    live_pull_request: int,
    live_pr_head_sha: str,
    active_writer_epoch: int,
    pr_state: str = "open",
    active_run_id: int | None = None,
    active_run_attempt: int | None = None,
    run_status: str | None = None,
) -> None:
    """Fail closed unless ``proof`` proves exclusive ownership against live PR state."""

    if not isinstance(proof, WriterOwnershipProof):
        raise TypeError("proof must be a WriterOwnershipProof")
    if not proof.verified_by_broker:
        raise StaleWriterOwnershipError(
            f"writer ownership proof for {proof.operation_id!r} was not verified by broker",
            diagnostic="v6_unverified_writer_proof",
        )
    if (
        proof.repository_id != live_repository_id
        or proof.pull_request != live_pull_request
    ):
        raise StaleWriterOwnershipError(
            "writer ownership proof target mismatch: expected "
            f"repo={live_repository_id}/pr={live_pull_request}, got "
            f"repo={proof.repository_id}/pr={proof.pull_request}",
            diagnostic="v6_writer_target_mismatch",
        )
    if pr_state.lower() != "open":
        raise StaleWriterOwnershipError(
            f"refusing write to non-open PR state {pr_state!r}",
            diagnostic="v6_pr_not_open",
        )
    if proof.pr_head_sha != live_pr_head_sha:
        raise StaleWriterOwnershipError(
            f"stale writer head SHA {proof.pr_head_sha} != live {live_pr_head_sha}",
            diagnostic="v6_stale_writer_head",
        )
    if proof.writer_epoch != active_writer_epoch:
        raise StaleWriterOwnershipError(
            f"superseded writer epoch {proof.writer_epoch} != active {active_writer_epoch}",
            diagnostic="v6_superseded_writer_epoch",
        )
    if active_run_id is not None and proof.run_id != active_run_id:
        raise StaleWriterOwnershipError(
            f"superseded writer run_id {proof.run_id} != active {active_run_id}",
            diagnostic="v6_superseded_writer_run",
        )
    if active_run_attempt is not None and proof.run_attempt != active_run_attempt:
        raise StaleWriterOwnershipError(
            f"superseded writer run_attempt {proof.run_attempt} != active {active_run_attempt}",
            diagnostic="v6_superseded_writer_attempt",
        )
    if run_status is not None and run_status.lower() in {
        "completed",
        "cancelled",
        "failure",
        "timed_out",
    }:
        raise StaleWriterOwnershipError(
            f"writer run {proof.run_id} is already terminal ({run_status})",
            diagnostic="v6_terminal_writer_run",
        )


def verify_write_response_readback(
    response: DualCredentialResponse,
    *,
    expected_marker: V6LedgerRecord | None = None,
    expected_operation_id: str | None = None,
    expected_payload_sha256: str | None = None,
) -> str | int:
    """Validate a ``200``/``201`` mutation response directly without a readback GET."""

    if response.status not in (200, 201):
        raise _AmbiguousWriteOutcomeError(
            f"unexpected mutation HTTP status {response.status}"
        )
    body = response.body
    if not isinstance(body, Mapping):
        raise _AmbiguousWriteOutcomeError(
            "mutation response body is not a JSON mapping"
        )

    effect_id = body.get("id")
    if type(effect_id) is int:
        if effect_id <= 0:
            raise _AmbiguousWriteOutcomeError(
                f"invalid numeric effect id in response: {effect_id!r}"
            )
    elif not isinstance(effect_id, str) or not effect_id.strip():
        raise _AmbiguousWriteOutcomeError(
            f"missing effect id in {response.status} mutation response"
        )

    resp_body_text = body.get("body")
    if expected_marker is not None:
        if not isinstance(resp_body_text, str):
            raise _AmbiguousWriteOutcomeError(
                "mutation response missing body text for marker verification"
            )
        expected_marker_str = format_v6_record_marker(expected_marker)
        if expected_marker_str not in resp_body_text:
            raise _AmbiguousWriteOutcomeError(
                "mutation response body does not match written v6 marker byte-for-byte"
            )
        parsed_markers = extract_v6_record_markers(resp_body_text, strict=True)
        if expected_marker not in parsed_markers:
            raise _AmbiguousWriteOutcomeError(
                "parsed marker from mutation response did not match expected record"
            )
    elif expected_operation_id is not None and isinstance(resp_body_text, str):
        if expected_operation_id not in resp_body_text or (
            expected_payload_sha256 is not None
            and expected_payload_sha256 not in resp_body_text
        ):
            raise _AmbiguousWriteOutcomeError(
                "mutation response body missing expected operation_id/payload_sha256 tag"
            )

    return effect_id


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _effect_tag(operation_id: str, payload_sha256: str) -> str:
    return f"<!-- reviewsensei:v6:effect op={operation_id} sha256={payload_sha256} -->"


def _matches_effect_payload(
    payload: Mapping[str, Any],
    *,
    operation_id: str,
    payload_sha256: str,
    expected_app_id: int | None,
    expected_bot_login: str | None,
    expected_bot_user_id: int | None,
    expected_head_sha: str | None = None,
) -> bool:
    if not verify_app_authored_record(
        payload,
        expected_app_id=expected_app_id,
        expected_bot_login=expected_bot_login,
        expected_bot_user_id=expected_bot_user_id,
    ):
        return False
    if expected_head_sha is not None:
        commit_id = payload.get("commit_id")
        if isinstance(commit_id, str) and commit_id != expected_head_sha:
            return False
    body = payload.get("body")
    if not isinstance(body, str):
        return False
    if _effect_tag(operation_id, payload_sha256) in body:
        return True
    return operation_id in body and payload_sha256 in body


_EFFECT_OP_RE = re.compile(
    r"<!--\s*reviewsensei:v6:effect\s+op=([^\s]+)\s+sha256=([0-9a-f]{64})\s*-->"
)


def _has_contradictory_effect_marker(
    payload: Mapping[str, Any],
    *,
    operation_id: str,
    payload_sha256: str,
    expected_head_sha: str | None,
    expected_app_id: int | None,
    expected_bot_login: str | None,
    expected_bot_user_id: int | None,
) -> bool:
    if not verify_app_authored_record(
        payload,
        expected_app_id=expected_app_id,
        expected_bot_login=expected_bot_login,
        expected_bot_user_id=expected_bot_user_id,
    ):
        return False
    body = payload.get("body")
    if not isinstance(body, str):
        return False
    match = _EFFECT_OP_RE.search(body)
    if match is None:
        return False
    if match.group(1) != operation_id:
        return False
    if match.group(2) != payload_sha256:
        return True
    if expected_head_sha is not None:
        commit_id = payload.get("commit_id")
        if isinstance(commit_id, str) and commit_id != expected_head_sha:
            return True
    return False


def _match_check_run_payload(
    payload: Mapping[str, Any],
    *,
    intent: WriteIntentRecord,
    expected_app_id: int | None,
) -> tuple[bool, bool]:
    """Return ``(matched, contradictory)`` for a GitHub check-run object."""

    app_info = payload.get("app")
    if expected_app_id is not None:
        if not isinstance(app_info, Mapping) or app_info.get("id") != expected_app_id:
            return False, False
    elif isinstance(app_info, Mapping):
        if type(app_info.get("id")) is not int or int(app_info["id"]) <= 0:
            return False, False
    else:
        return False, False

    head_sha = payload.get("head_sha")
    output = payload.get("output")
    output_text = ""
    if isinstance(output, Mapping):
        summary = output.get("summary")
        text = output.get("text")
        output_text = "\n".join(
            part for part in (summary, text) if isinstance(part, str)
        )
    ext_id = payload.get("external_id")
    effect_tag = _effect_tag(intent.operation_id, intent.payload_sha256)
    has_op = ext_id == intent.operation_id or intent.operation_id in output_text
    has_sha = intent.payload_sha256 in output_text or effect_tag in output_text
    if has_op:
        if head_sha != intent.pr_head_sha:
            return False, True
        if has_sha or (ext_id == intent.operation_id and not output_text):
            return True, False
        if _EFFECT_OP_RE.search(output_text) is not None:
            return False, True
    return False, False


def _match_review_thread_node(
    payload: Mapping[str, Any],
    *,
    intent: WriteIntentRecord,
    expected_app_id: int | None,
    expected_bot_login: str | None,
    expected_bot_user_id: int | None,
) -> tuple[bool, bool]:
    """Return ``(matched, contradictory)`` for a GraphQL reviewThread node."""

    thread_id = payload.get("id")
    if not isinstance(thread_id, str) or not thread_id:
        return False, False
    candidate_digests = {
        thread_id,
        canonical_sha256({"thread_id": thread_id}),
        canonical_sha256({"thread_id": thread_id, "head": intent.pr_head_sha}),
    }
    comments_obj = payload.get("comments")
    comment_nodes: Sequence[Any] = (
        comments_obj.get("nodes", ())
        if isinstance(comments_obj, Mapping)
        and isinstance(comments_obj.get("nodes"), Sequence)
        else ()
    )
    for node in comment_nodes:
        if not isinstance(node, Mapping):
            continue
        if _has_contradictory_effect_marker(
            node,
            operation_id=intent.operation_id,
            payload_sha256=intent.payload_sha256,
            expected_head_sha=None,
            expected_app_id=expected_app_id,
            expected_bot_login=expected_bot_login,
            expected_bot_user_id=expected_bot_user_id,
        ):
            return False, True
        if _matches_effect_payload(
            node,
            operation_id=intent.operation_id,
            payload_sha256=intent.payload_sha256,
            expected_app_id=expected_app_id,
            expected_bot_login=expected_bot_login,
            expected_bot_user_id=expected_bot_user_id,
        ):
            return True, False

    if intent.target_digest in candidate_digests:
        expected_resolved = intent.effect_kind != "thread_unresolve"
        if payload.get("isResolved") is expected_resolved:
            return True, False
    return False, False


def _canonical_native_effect_kind(effect_kind: str) -> str:
    if effect_kind in {
        "pull_request_review",
        "pr_review_publish",
        "companion_prose_review_publish",
        "pr_approval_publish",
    }:
        return "pull_request_review"
    if effect_kind in {
        "pr_issue_comment",
        "issue_comment",
        "issue_comment_write",
        "command_reply",
        "issue_reply",
        "session_comment",
    }:
        return "issue_comment"
    if effect_kind in {
        "pull_request_review_comment",
        "pr_inline_reply",
        "inline_reply",
        "pr_review_comment",
    }:
        return "pull_request_review_comment"
    if effect_kind in {"check_run", "pr_check_run", "check_run_write", "check_publish"}:
        return "check_run"
    return effect_kind


def _match_native_effects_for_intent(
    intent: WriteIntentRecord,
    *,
    expected_app_id: int | None,
    expected_bot_login: str | None,
    expected_bot_user_id: int | None,
    reviews: Sequence[PaginatedRecord],
    issue_comments: Sequence[PaginatedRecord],
    review_comments: Sequence[PaginatedRecord],
    check_runs: Sequence[PaginatedRecord],
    review_threads: Sequence[PaginatedRecord],
) -> tuple[tuple[str | int, ...], bool]:
    """Match an unmatched intent against native PR surfaces using ``intent.pr_head_sha``."""

    kind = intent.effect_kind
    matched_ids: list[str | int] = []
    contradictory = False

    if kind in {
        "pull_request_review",
        "pr_review_publish",
        "companion_prose_review_publish",
        "pr_approval_publish",
    }:
        candidates = reviews
    elif kind in {
        "pr_issue_comment",
        "issue_comment",
        "issue_comment_write",
        "command_reply",
        "issue_reply",
        "session_comment",
    }:
        candidates = issue_comments
    elif kind in {
        "pull_request_review_comment",
        "pr_inline_reply",
        "inline_reply",
        "pr_review_comment",
    }:
        candidates = review_comments
    elif kind in {"check_run", "pr_check_run", "check_run_write", "check_publish"}:
        for rec in check_runs:
            if not isinstance(rec.payload, Mapping):
                continue
            matched, conflict = _match_check_run_payload(
                rec.payload, intent=intent, expected_app_id=expected_app_id
            )
            if conflict:
                contradictory = True
            elif matched:
                matched_ids.append(rec.payload.get("id", rec.record_id))
        return tuple(matched_ids), contradictory
    elif kind in {"review_thread_mutation", "thread_resolve", "thread_unresolve"}:
        for rec in review_threads:
            if not isinstance(rec.payload, Mapping):
                continue
            matched, conflict = _match_review_thread_node(
                rec.payload,
                intent=intent,
                expected_app_id=expected_app_id,
                expected_bot_login=expected_bot_login,
                expected_bot_user_id=expected_bot_user_id,
            )
            if conflict:
                contradictory = True
            elif matched:
                matched_ids.append(rec.payload.get("id", rec.record_id))
        return tuple(matched_ids), contradictory
    else:
        candidates = reviews

    for rec in candidates:
        if not isinstance(rec.payload, Mapping):
            continue
        body = rec.payload.get("body")
        if kind in {
            "pr_issue_comment",
            "issue_comment",
            "issue_comment_write",
            "command_reply",
            "issue_reply",
            "session_comment",
        } and isinstance(body, str):
            # Ignore pure v6 ledger markers when matching native issue comment effects.
            non_ledger_lines = [
                line
                for line in body.splitlines()
                if not line.strip().startswith("<!-- reviewsensei:v6:record ")
            ]
            if not any(line.strip() for line in non_ledger_lines):
                continue
        if _has_contradictory_effect_marker(
            rec.payload,
            operation_id=intent.operation_id,
            payload_sha256=intent.payload_sha256,
            expected_head_sha=intent.pr_head_sha,
            expected_app_id=expected_app_id,
            expected_bot_login=expected_bot_login,
            expected_bot_user_id=expected_bot_user_id,
        ):
            contradictory = True
            continue
        if _matches_effect_payload(
            rec.payload,
            operation_id=intent.operation_id,
            payload_sha256=intent.payload_sha256,
            expected_app_id=expected_app_id,
            expected_bot_login=expected_bot_login,
            expected_bot_user_id=expected_bot_user_id,
            expected_head_sha=intent.pr_head_sha,
        ):
            matched_ids.append(rec.payload.get("id", rec.record_id))
    return tuple(matched_ids), contradictory


class V6RecoveryEngine:
    """Orchestrates v6 GitHub-governed recovery and reconcile-before-retry writes."""

    def __init__(
        self,
        client: DualCredentialGitHubClient,
        *,
        paginator: StabilityVerifiedPaginator | None = None,
        mutation_bound: MutationSanityBound | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        max_in_job_sleep_seconds: float = DEFAULT_MAX_IN_JOB_SLEEP_SECONDS,
        remaining_job_seconds: float = 1800.0,
        now_fn: Callable[[], float] = time.time,
        broker_fence_verifier: Callable[[WriterOwnershipProof], Mapping[str, Any]]
        | None = None,
    ) -> None:
        if not isinstance(client, DualCredentialGitHubClient):
            raise TypeError("client must be a DualCredentialGitHubClient")
        self.client = client
        self.paginator = (
            StabilityVerifiedPaginator(client) if paginator is None else paginator
        )
        self.mutation_bound = (
            MutationSanityBound() if mutation_bound is None else mutation_bound
        )
        self.sleeper = sleeper
        self.max_in_job_sleep_seconds = max_in_job_sleep_seconds
        self.remaining_job_seconds = float(remaining_job_seconds)
        self.now_fn = now_fn
        self.broker_fence_verifier = broker_fence_verifier

    def _execute_with_throttle(
        self,
        *,
        request: RecoveryRequest,
        guard: NoProgressGuard,
        prior_durable_hint: AdvisoryProgressHint | None,
        call_fn: Callable[[float], DualCredentialResponse],
    ) -> DualCredentialResponse:
        attempt = 1
        while True:
            now_epoch = self.now_fn()
            try:
                return call_fn(now_epoch)
            except GitHubHTTPPermissionError:
                # Never retry 403 permission errors or fall back to App token.
                raise
            except GitHubHTTPRateLimitError as rate_err:
                decision = evaluate_throttle_wait(
                    rate_err,
                    max_in_job_sleep_seconds=self.max_in_job_sleep_seconds,
                    remaining_job_seconds=self.remaining_job_seconds,
                    attempt=attempt,
                    now_epoch=now_epoch,
                )
                if decision.action == "sleep":
                    self.sleeper(decision.wait_seconds)
                    self.remaining_job_seconds = max(
                        0.0, self.remaining_job_seconds - decision.wait_seconds
                    )
                    attempt += 1
                    continue

                checkpoint_hint, reused = self._persist_pause_checkpoint(
                    request=request,
                    guard=guard,
                    prior_durable_hint=prior_durable_hint,
                    resume_after_epoch=decision.resume_after_epoch,
                    reason=decision.reason,
                    now_epoch=now_epoch,
                )
                raise _ThrottlePausedSignal(
                    RecoveryPaused(
                        operation_id=request.operation_id,
                        resume_after_epoch=decision.resume_after_epoch,
                        rerun_command=decision.rerun_command
                        or MAINTAINER_RERUN_COMMAND,
                        checkpoint_state=checkpoint_hint,
                        reason=decision.reason,
                        reused_prior_durable_state=reused,
                        model_calls_started=0,
                    )
                ) from rate_err

    def _persist_pause_checkpoint(
        self,
        *,
        request: RecoveryRequest,
        guard: NoProgressGuard,
        prior_durable_hint: AdvisoryProgressHint | None,
        resume_after_epoch: float,
        reason: str,
        now_epoch: float,
    ) -> tuple[AdvisoryProgressHint, bool]:
        current_hint = guard.to_advisory_hint()
        if request.advisory_comment_id is None:
            return current_hint, False
        authorize_v6_endpoint_credential(
            "advisory_progress_hint",
            CredentialKind.APP_BROKER,
            event=request.event,
            visibility=request.visibility,
            granted_scopes=request.granted_scopes,
        )
        try:
            self.mutation_bound.consume(
                "advisory_progress_hint", "advisory_progress_hint"
            )
            self.client.execute(
                "advisory_progress_hint",
                method="PATCH",
                path=(
                    f"/repos/{request.owner}/{request.repo}/issues/"
                    f"{request.pull_request}/comments/{request.advisory_comment_id}"
                ),
                event=request.event,
                visibility=request.visibility,
                granted_scopes=request.granted_scopes,
                credential_kind=CredentialKind.APP_BROKER,
                body={
                    "body": (
                        f"ReviewSensei paused due to rate limit ({reason}); "
                        f"rerun `{MAINTAINER_RERUN_COMMAND}` after {int(resume_after_epoch)}.\n"
                        f"{format_v6_record_marker(current_hint)}"
                    )
                },
                allow_checkpoint_reserve_override=True,
                now_epoch=now_epoch,
            )
            return current_hint, False
        except GitHubHTTPPermissionError:
            raise
        except (GitHubHTTPRateLimitError, GitHubHTTPTransientError, TimeoutError):
            fallback = (
                prior_durable_hint if prior_durable_hint is not None else current_hint
            )
            return fallback, True

    def _verify_credential_not_expired(self, request: RecoveryRequest) -> None:
        if request.token_expires_at_ms is None:
            return
        now_ms = int(self.now_fn() * 1000)
        if (
            type(request.token_expires_at_ms) is not int
            or request.token_expires_at_ms <= 0
            or type(request.clock_margin_ms) is not int
            or request.clock_margin_ms < 0
            or now_ms + request.clock_margin_ms >= request.token_expires_at_ms
        ):
            raise StaleWriterOwnershipError(
                "lease installation token expired or within clock_margin_ms safety window",
                diagnostic="v6_lease_clock_untrusted",
            )

    def _verify_write_boundary(
        self,
        *,
        request: RecoveryRequest,
        guard: NoProgressGuard,
        prior_durable_hint: AdvisoryProgressHint | None,
        target_path: str | None = None,
    ) -> None:
        if not request.has_active_write_lease:
            raise StaleWriterOwnershipError(
                "refusing write without an active coordinator write lease",
                diagnostic="v6_no_active_write_lease",
            )
        check_path = (
            target_path
            if target_path is not None
            else (
                f"/repos/{request.owner}/{request.repo}/issues/"
                f"{request.pull_request}/comments"
            )
        )
        _assert_target_pr_binding(request, path=check_path)
        self._verify_credential_not_expired(request)

        proof = request.ownership_proof
        if self.broker_fence_verifier is not None:
            fence = self.broker_fence_verifier(proof)
            if not isinstance(fence, Mapping):
                raise StaleWriterOwnershipError(
                    "broker fence verifier returned invalid payload",
                    diagnostic="v6_missing_authoritative_fence",
                )
            if fence.get("verified_by_broker") is False:
                raise StaleWriterOwnershipError(
                    "broker fence rejected writer ownership verification",
                    diagnostic="v6_unverified_writer_proof",
                )
            live_repo_id = fence.get("repository_id")
            live_pr = fence.get("pull_request")
            live_head = fence.get("pr_head_sha")
            active_epoch = fence.get("active_writer_epoch", fence.get("writer_epoch"))
            if (
                type(live_repo_id) is not int
                or live_repo_id <= 0
                or type(live_pr) is not int
                or live_pr <= 0
                or type(live_head) is not str
                or _SHA1_40_RE.fullmatch(live_head) is None
                or type(active_epoch) is not int
                or active_epoch < 0
            ):
                raise StaleWriterOwnershipError(
                    "broker fence missing authoritative repository, PR, head, or epoch",
                    diagnostic="v6_missing_authoritative_fence",
                )
            verify_writer_ownership(
                proof,
                live_repository_id=live_repo_id,
                live_pull_request=live_pr,
                live_pr_head_sha=live_head,
                active_writer_epoch=active_epoch,
                pr_state=str(fence.get("pr_state", "open")),
                active_run_id=fence.get("active_run_id"),
                active_run_attempt=fence.get("active_run_attempt"),
                run_status=fence.get("run_status"),
            )
            return

        pr_resp = self._execute_with_throttle(
            request=request,
            guard=guard,
            prior_durable_hint=prior_durable_hint,
            call_fn=lambda ts: self.client.execute(
                "broker_pr_binding_fence",
                method="GET",
                path=(
                    f"/repos/{request.owner}/{request.repo}/pulls/"
                    f"{request.pull_request}"
                ),
                event=request.event,
                visibility=request.visibility,
                granted_scopes=request.granted_scopes,
                credential_kind=CredentialKind.APP_BROKER,
                now_epoch=ts,
            ),
        )
        run_resp = self._execute_with_throttle(
            request=request,
            guard=guard,
            prior_durable_hint=prior_durable_hint,
            call_fn=lambda ts: self.client.execute(
                "broker_active_writer_fence",
                method="GET",
                path=(
                    f"/repos/{request.owner}/{request.repo}/actions/runs/{proof.run_id}"
                ),
                event=request.event,
                visibility=request.visibility,
                granted_scopes=request.granted_scopes,
                credential_kind=CredentialKind.APP_BROKER,
                now_epoch=ts,
            ),
        )
        pr_body = pr_resp.body if isinstance(pr_resp.body, Mapping) else {}
        run_body = run_resp.body if isinstance(run_resp.body, Mapping) else {}
        raw_head = pr_body.get("head")
        head_info: Mapping[str, Any] = raw_head if isinstance(raw_head, Mapping) else {}
        raw_base = pr_body.get("base")
        base_info: Mapping[str, Any] = raw_base if isinstance(raw_base, Mapping) else {}
        raw_base_repo = base_info.get("repo")
        base_repo: Mapping[str, Any] = (
            raw_base_repo if isinstance(raw_base_repo, Mapping) else {}
        )
        live_repo_id = base_repo.get("id")
        live_head_sha = head_info.get("sha")
        pr_state = pr_body.get("state")
        active_epoch = (
            run_body.get("writer_epoch")
            if "writer_epoch" in run_body
            else run_body.get("lease_epoch")
        )
        active_run_id = (
            run_body.get("active_run_id")
            if "active_run_id" in run_body
            else run_body.get("id")
        )
        active_attempt = run_body.get("run_attempt")
        run_status = run_body.get("status")

        if (
            type(live_repo_id) is not int
            or live_repo_id <= 0
            or type(live_head_sha) is not str
            or _SHA1_40_RE.fullmatch(live_head_sha) is None
            or type(pr_state) is not str
            or not pr_state.strip()
            or type(active_epoch) is not int
            or active_epoch < 0
            or type(active_run_id) is not int
            or active_run_id <= 0
            or type(active_attempt) is not int
            or active_attempt < 1
            or type(run_status) is not str
            or not run_status.strip()
        ):
            raise StaleWriterOwnershipError(
                "missing authoritative coordinator/broker ownership fields at write boundary",
                diagnostic="v6_missing_authoritative_fence",
            )
        verify_writer_ownership(
            proof,
            live_repository_id=live_repo_id,
            live_pull_request=request.pull_request,
            live_pr_head_sha=live_head_sha,
            active_writer_epoch=active_epoch,
            pr_state=pr_state,
            active_run_id=active_run_id,
            active_run_attempt=active_attempt,
            run_status=run_status,
        )

    def _throttled_page_executor(
        self,
        *,
        request: RecoveryRequest,
        guard: NoProgressGuard,
        prior_hint: AdvisoryProgressHint | None,
    ) -> Callable[..., DualCredentialResponse]:
        def _exec(*args: Any, **kwargs: Any) -> DualCredentialResponse:
            return self._execute_with_throttle(
                request=request,
                guard=guard,
                prior_durable_hint=prior_hint,
                call_fn=lambda ts: self.client.execute(
                    *args,
                    **{**kwargs, "now_epoch": ts},
                ),
            )

        return _exec

    def _scan_pr_surfaces(
        self,
        *,
        request: RecoveryRequest,
        guard: NoProgressGuard,
        prior_hint: AdvisoryProgressHint | None,
    ) -> tuple[
        tuple[PaginatedRecord, ...],
        tuple[PaginatedRecord, ...],
        AdvisoryProgressHint | None,
    ]:
        exec_fn = self._throttled_page_executor(
            request=request, guard=guard, prior_hint=prior_hint
        )
        if request.graphql_query is not None:
            probe = request.graphql_stability_probe or {}
            gql_res = self.paginator.scan_graphql(
                query=request.graphql_query,
                variables={
                    "owner": request.owner,
                    "repo": request.repo,
                    "pullNumber": request.pull_request,
                },
                event=request.event,
                visibility=request.visibility,
                granted_scopes=request.granted_scopes,
                progress_guard=guard,
                advisory_hint=prior_hint,
                known_records=request.known_records,
                current_total_count=probe.get("current_total_count"),
                current_prefix_digest=probe.get("current_prefix_digest"),
                anchor_resolved_id=probe.get("anchor_resolved_id"),
                anchor_content_sha256=probe.get("anchor_content_sha256"),
                expected_anchor_sha256=probe.get("expected_anchor_sha256"),
                phase_name="scan_ledger",
                now_epoch=self.now_fn(),
                execute_fn=exec_fn,
            )
            comment_records = gql_res.records
            latest_hint = gql_res.updated_hint
        else:
            comment_res = self.paginator.scan_rest(
                endpoint_id="pr_issue_comments",
                path=(
                    f"/repos/{request.owner}/{request.repo}/issues/"
                    f"{request.pull_request}/comments?per_page=100"
                ),
                event=request.event,
                visibility=request.visibility,
                granted_scopes=request.granted_scopes,
                progress_guard=guard,
                advisory_hint=prior_hint,
                known_records=request.known_records,
                phase_name="scan_comments",
                now_epoch=self.now_fn(),
                execute_fn=exec_fn,
            )
            comment_records = comment_res.records
            latest_hint = comment_res.updated_hint

        review_res = self.paginator.scan_rest(
            endpoint_id="pr_reviews",
            path=(
                f"/repos/{request.owner}/{request.repo}/pulls/"
                f"{request.pull_request}/reviews?per_page=100"
            ),
            event=request.event,
            visibility=request.visibility,
            granted_scopes=request.granted_scopes,
            progress_guard=guard,
            phase_name="scan_reviews",
            now_epoch=self.now_fn(),
            execute_fn=exec_fn,
        )
        return comment_records, review_res.records, latest_hint

    def _collect_full_ledger(
        self,
        comment_records: Sequence[PaginatedRecord],
        *,
        expected_app_id: int | None,
        expected_bot_login: str | None,
        expected_bot_user_id: int | None,
    ) -> _CollectedLedger:
        owners: list[OwnerMarkerRecord] = []
        intents: list[WriteIntentRecord] = []
        receipts: list[CompletionReceiptRecord] = []
        non_submissions: list[NonSubmissionProofRecord] = []
        holds: list[DurableHoldRecord] = []
        unresolved_results: list[UnresolvedResultRecord] = []
        hints: list[AdvisoryProgressHint] = []
        app_marker_comments: list[PaginatedRecord] = []
        anchor_comment_id: int | None = None
        comment_ids_by_record_id: dict[str, int] = {}
        owner_comment_ids_by_generation: dict[int, int] = {}

        for item in comment_records:
            payload = item.payload
            if not isinstance(payload, Mapping):
                continue
            body = payload.get("body")
            if not isinstance(body, str) or "<!-- reviewsensei:v6:" not in body:
                continue
            if not verify_app_authored_record(
                payload,
                expected_app_id=expected_app_id,
                expected_bot_login=expected_bot_login,
                expected_bot_user_id=expected_bot_user_id,
            ):
                if "reviewsensei:v6:owner" in body:
                    # Preserve spoofed owner comments so validate_owner_lineage fails closed.
                    app_marker_comments.append(item)
                continue
            parsed = extract_v6_record_markers(body, strict=False)
            if not parsed:
                continue
            app_marker_comments.append(item)
            raw_cid = payload.get("id", item.record_id)
            cid: int | None = raw_cid if type(raw_cid) is int and raw_cid > 0 else None
            if cid is not None:
                anchor_comment_id = (
                    cid if anchor_comment_id is None else max(anchor_comment_id, cid)
                )
            for rec in parsed:
                rec_id = getattr(rec, "record_id", None)
                if isinstance(rec_id, str) and cid is not None:
                    comment_ids_by_record_id.setdefault(rec_id, cid)
                if isinstance(rec, OwnerMarkerRecord):
                    owners.append(rec)
                    if cid is not None:
                        owner_comment_ids_by_generation.setdefault(rec.generation, cid)
                elif isinstance(rec, WriteIntentRecord):
                    intents.append(rec)
                elif isinstance(rec, CompletionReceiptRecord):
                    receipts.append(rec)
                elif isinstance(rec, NonSubmissionProofRecord):
                    non_submissions.append(rec)
                elif isinstance(rec, DurableHoldRecord):
                    holds.append(rec)
                elif isinstance(rec, UnresolvedResultRecord):
                    unresolved_results.append(rec)
                elif isinstance(rec, AdvisoryProgressHint):
                    hints.append(rec)
        return _CollectedLedger(
            owners=tuple(owners),
            intents=tuple(intents),
            receipts=tuple(receipts),
            non_submissions=tuple(non_submissions),
            holds=tuple(holds),
            unresolved_results=tuple(unresolved_results),
            hints=tuple(hints),
            app_marker_comments=tuple(app_marker_comments),
            anchor_comment_id=anchor_comment_id,
            comment_ids_by_record_id=comment_ids_by_record_id,
            owner_comment_ids_by_generation=owner_comment_ids_by_generation,
        )

    def _collect_ledger_records(
        self,
        comment_records: Sequence[PaginatedRecord],
        *,
        request: RecoveryRequest,
    ) -> tuple[
        list[WriteIntentRecord],
        list[CompletionReceiptRecord],
        list[UnresolvedResultRecord],
        list[AdvisoryProgressHint],
    ]:
        collected = self._collect_full_ledger(
            comment_records,
            expected_app_id=request.expected_app_id,
            expected_bot_login=request.expected_bot_login,
            expected_bot_user_id=request.expected_bot_user_id,
        )
        return (
            list(collected.intents),
            list(collected.receipts),
            list(collected.unresolved_results),
            list(collected.hints),
        )

    def _verify_candidate_effect_with_broker(
        self,
        *,
        request: RecoveryRequest,
        guard: NoProgressGuard,
        prior_hint: AdvisoryProgressHint | None,
        candidate_effect_id: str | int,
        expected_payload_sha256: str,
        expected_head_sha: str | None = None,
    ) -> tuple[bool, bool, str | int | None]:
        """Return ``(outcome_known, landed, verified_effect_id)`` via broker."""

        try:
            resp = self._execute_with_throttle(
                request=request,
                guard=guard,
                prior_durable_hint=prior_hint,
                call_fn=lambda ts: self.client.execute(
                    "broker_effect_verification",
                    method="GET",
                    path=(
                        f"/repos/{request.owner}/{request.repo}/pulls/"
                        f"{request.pull_request}/reviews/{candidate_effect_id}"
                    ),
                    event=request.event,
                    visibility=request.visibility,
                    granted_scopes=request.granted_scopes,
                    credential_kind=CredentialKind.APP_BROKER,
                    now_epoch=ts,
                ),
            )
        except (GitHubHTTPTransientError, TimeoutError, ConnectionError):
            return False, False, None

        if resp.status in (404, 410):
            return True, False, None
        if resp.status not in (200, 201) or not isinstance(resp.body, Mapping):
            return False, False, None
        if _matches_effect_payload(
            resp.body,
            operation_id=request.operation_id,
            payload_sha256=expected_payload_sha256,
            expected_app_id=request.expected_app_id,
            expected_bot_login=request.expected_bot_login,
            expected_bot_user_id=request.expected_bot_user_id,
            expected_head_sha=expected_head_sha,
        ):
            verified_id = resp.body.get("id", candidate_effect_id)
            return True, True, verified_id
        return False, False, None

    def _reconcile_effect_surface(
        self,
        *,
        request: RecoveryRequest,
        guard: NoProgressGuard,
        prior_hint: AdvisoryProgressHint | None,
        expected_payload_sha256: str,
        scanned_reviews: Sequence[PaginatedRecord],
        expected_head_sha: str | None = None,
        rescan_before_verify: bool = False,
    ) -> tuple[bool, bool, str | int | None, int]:
        """Determine whether the target mutation landed without duplicate writes."""

        reviews = scanned_reviews
        if rescan_before_verify:
            exec_fn = self._throttled_page_executor(
                request=request, guard=guard, prior_hint=prior_hint
            )
            rescan = self.paginator.scan_rest(
                endpoint_id="pr_reviews",
                path=(
                    f"/repos/{request.owner}/{request.repo}/pulls/"
                    f"{request.pull_request}/reviews?per_page=100"
                ),
                event=request.event,
                visibility=request.visibility,
                granted_scopes=request.granted_scopes,
                progress_guard=guard,
                phase_name="reconcile_reviews",
                now_epoch=self.now_fn(),
                execute_fn=exec_fn,
            )
            reviews = rescan.records

        candidate_ids: list[str | int] = []
        for record in reviews:
            if not isinstance(record.payload, Mapping):
                continue
            if _has_contradictory_effect_marker(
                record.payload,
                operation_id=request.operation_id,
                payload_sha256=expected_payload_sha256,
                expected_head_sha=expected_head_sha,
                expected_app_id=request.expected_app_id,
                expected_bot_login=request.expected_bot_login,
                expected_bot_user_id=request.expected_bot_user_id,
            ):
                return False, False, None, 0
            if _matches_effect_payload(
                record.payload,
                operation_id=request.operation_id,
                payload_sha256=expected_payload_sha256,
                expected_app_id=request.expected_app_id,
                expected_bot_login=request.expected_bot_login,
                expected_bot_user_id=request.expected_bot_user_id,
                expected_head_sha=expected_head_sha,
            ):
                effect_id = record.payload.get("id", record.record_id)
                candidate_ids.append(effect_id)

        if not candidate_ids:
            return True, False, None, 0
        if len(candidate_ids) > 1:
            # Duplicate matches must hold rather than silently picking one.
            return False, False, None, 0

        readbacks = 1
        known, landed, verified_id = self._verify_candidate_effect_with_broker(
            request=request,
            guard=guard,
            prior_hint=prior_hint,
            candidate_effect_id=candidate_ids[0],
            expected_payload_sha256=expected_payload_sha256,
            expected_head_sha=expected_head_sha,
        )
        if not known:
            return False, False, None, readbacks
        if landed:
            return True, True, verified_id, readbacks
        return True, False, None, readbacks

    def _find_existing_receipt_on_pr(
        self,
        *,
        request: RecoveryRequest,
        guard: NoProgressGuard,
        prior_hint: AdvisoryProgressHint | None,
        receipt: CompletionReceiptRecord,
    ) -> tuple[CompletionReceiptRecord, int | None] | None:
        exec_fn = self._throttled_page_executor(
            request=request, guard=guard, prior_hint=prior_hint
        )
        rescan = self.paginator.scan_rest(
            endpoint_id="pr_issue_comments",
            path=(
                f"/repos/{request.owner}/{request.repo}/issues/"
                f"{request.pull_request}/comments?per_page=100"
            ),
            event=request.event,
            visibility=request.visibility,
            granted_scopes=request.granted_scopes,
            progress_guard=guard,
            phase_name="reconcile_receipts",
            now_epoch=self.now_fn(),
            execute_fn=exec_fn,
        )
        collected = self._collect_full_ledger(
            rescan.records,
            expected_app_id=request.expected_app_id,
            expected_bot_login=request.expected_bot_login,
            expected_bot_user_id=request.expected_bot_user_id,
        )
        for existing in collected.receipts:
            if (
                existing.operation_id == receipt.operation_id
                and existing.payload_sha256 == receipt.payload_sha256
                and str(existing.github_effect_id) == str(receipt.github_effect_id)
            ):
                return existing, collected.comment_ids_by_record_id.get(
                    existing.record_id
                )
        return None

    def _persist_completion_receipt(
        self,
        *,
        request: RecoveryRequest,
        guard: NoProgressGuard,
        prior_hint: AdvisoryProgressHint | None,
        github_effect_id: str | int,
        payload_sha256: str,
        existing_receipts: Sequence[CompletionReceiptRecord] = (),
        comment_ids_by_record_id: Mapping[str, int] | None = None,
    ) -> tuple[CompletionReceiptRecord, int, int | None]:
        for existing in existing_receipts:
            if (
                existing.operation_id == request.operation_id
                and existing.payload_sha256 == payload_sha256
                and str(existing.github_effect_id) == str(github_effect_id)
            ):
                existing_cid = (
                    comment_ids_by_record_id.get(existing.record_id)
                    if comment_ids_by_record_id is not None
                    else None
                )
                return existing, 0, existing_cid

        self._verify_write_boundary(
            request=request,
            guard=guard,
            prior_durable_hint=prior_hint,
        )
        receipt = CompletionReceiptRecord(
            record_id=f"receipt:{request.operation_id}:{request.ownership_proof.writer_epoch}",
            operation_id=request.operation_id,
            writer_epoch=request.ownership_proof.writer_epoch,
            github_effect_id=github_effect_id,
            payload_sha256=payload_sha256,
            completed_at=_utc_now_iso(),
        )
        self.mutation_bound.consume("completion_receipt_comment", "completion_receipt")
        try:
            resp = self._execute_with_throttle(
                request=request,
                guard=guard,
                prior_durable_hint=prior_hint,
                call_fn=lambda ts: self.client.execute(
                    "completion_receipt_comment",
                    method="POST",
                    path=(
                        f"/repos/{request.owner}/{request.repo}/issues/"
                        f"{request.pull_request}/comments"
                    ),
                    event=request.event,
                    visibility=request.visibility,
                    granted_scopes=request.granted_scopes,
                    credential_kind=CredentialKind.APP_BROKER,
                    body={"body": format_v6_record_marker(receipt)},
                    allow_checkpoint_reserve_override=True,
                    now_epoch=ts,
                ),
            )
            eff_id = verify_write_response_readback(resp, expected_marker=receipt)
            receipt_cid = eff_id if type(eff_id) is int and eff_id > 0 else None
            return receipt, 0, receipt_cid
        except (
            _AmbiguousWriteOutcomeError,
            TimeoutError,
            ConnectionError,
            GitHubHTTPTransientError,
        ) as exc:
            reconciled = self._find_existing_receipt_on_pr(
                request=request,
                guard=guard,
                prior_hint=prior_hint,
                receipt=receipt,
            )
            if reconciled is not None:
                found_receipt, found_cid = reconciled
                return found_receipt, 0, found_cid
            raise _AmbiguousReceiptOutcomeError(
                f"receipt write outcome unconfirmed for {request.operation_id!r}"
            ) from exc

    def _finalize_hold(
        self,
        *,
        request: RecoveryRequest,
        guard: NoProgressGuard,
        prior_hint: AdvisoryProgressHint | None,
        blocker: str,
        missing_evidence: tuple[str, ...],
    ) -> RecoveryHold:
        if (
            request.emit_durable_hold_on_pr
            and request.has_active_write_lease
            and request.slot_key is not None
            and request.generation is not None
        ):
            try:
                self._verify_write_boundary(
                    request=request,
                    guard=guard,
                    prior_durable_hint=prior_hint,
                )
                hold_record = DurableHoldRecord(
                    record_id=(
                        f"hold:{request.operation_id}:{request.generation}:"
                        f"{request.ownership_proof.writer_epoch}"
                    ),
                    slot_key=request.slot_key,
                    generation=request.generation,
                    lease_epoch=request.ownership_proof.writer_epoch,
                    operation_id=request.operation_id,
                    pr_head_sha=request.ownership_proof.pr_head_sha,
                    reason_code=blocker,
                    missing_evidence=missing_evidence or ("unresolved_evidence",),
                    held_at=_utc_now_iso(),
                    resolved_by_record_id=None,
                )
                self.mutation_bound.consume("write_intent_comment", "durable_hold")
                hold_resp = self._execute_with_throttle(
                    request=request,
                    guard=guard,
                    prior_durable_hint=prior_hint,
                    call_fn=lambda ts: self.client.execute(
                        "write_intent_comment",
                        method="POST",
                        path=(
                            f"/repos/{request.owner}/{request.repo}/issues/"
                            f"{request.pull_request}/comments"
                        ),
                        event=request.event,
                        visibility=request.visibility,
                        granted_scopes=request.granted_scopes,
                        credential_kind=CredentialKind.APP_BROKER,
                        body={"body": format_v6_record_marker(hold_record)},
                        allow_checkpoint_reserve_override=True,
                        now_epoch=ts,
                    ),
                )
                verify_write_response_readback(hold_resp, expected_marker=hold_record)
            except (
                StaleWriterOwnershipError,
                MutationSanityBoundError,
                _AmbiguousWriteOutcomeError,
                GitHubHTTPPermissionError,
                GitHubHTTPRateLimitError,
                GitHubHTTPTransientError,
                TimeoutError,
                ConnectionError,
                ReviewInputError,
            ):
                pass

        if not request.has_active_write_lease or request.step_summary_sink is not None:
            write_v6_step_summary(
                render_v6_hold_summary(
                    operation_id=request.operation_id,
                    blocker=blocker,
                    missing_evidence=missing_evidence,
                    slot_key=request.slot_key,
                    repository=f"{request.owner}/{request.repo}",
                    pull_request=request.pull_request,
                    pr_head_sha=request.ownership_proof.pr_head_sha,
                ),
                sink=request.step_summary_sink,
            )

        return RecoveryHold(
            operation_id=request.operation_id,
            blocker=blocker,
            missing_evidence=missing_evidence,
            zero_model_calls_started=True,
        )

    def _persist_owner_marker_if_needed(
        self,
        *,
        request: RecoveryRequest,
        guard: NoProgressGuard,
        prior_hint: AdvisoryProgressHint | None,
        existing_owners: Sequence[OwnerMarkerRecord],
        comment_ids_by_record_id: Mapping[str, int] | None = None,
    ) -> int | None:
        owner_marker = request.owner_marker
        if owner_marker is None:
            return None
        for existing in existing_owners:
            if (
                existing.slot_key == owner_marker.slot_key
                and existing.generation == owner_marker.generation
                and existing.lease_epoch == owner_marker.lease_epoch
                and existing.reservation_id == owner_marker.reservation_id
            ):
                if comment_ids_by_record_id is not None:
                    return comment_ids_by_record_id.get(existing.record_id)
                return None
        self._verify_write_boundary(
            request=request,
            guard=guard,
            prior_durable_hint=prior_hint,
        )
        self.mutation_bound.consume("write_intent_comment", "owner_marker")
        resp = self._execute_with_throttle(
            request=request,
            guard=guard,
            prior_durable_hint=prior_hint,
            call_fn=lambda ts: self.client.execute(
                "write_intent_comment",
                method="POST",
                path=(
                    f"/repos/{request.owner}/{request.repo}/issues/"
                    f"{request.pull_request}/comments"
                ),
                event=request.event,
                visibility=request.visibility,
                granted_scopes=request.granted_scopes,
                credential_kind=CredentialKind.APP_BROKER,
                body={"body": format_v6_record_marker(owner_marker)},
                now_epoch=ts,
            ),
        )
        eff_id = verify_write_response_readback(resp, expected_marker=owner_marker)
        return eff_id if type(eff_id) is int and eff_id > 0 else None

    def _persist_non_submission_proof(
        self,
        *,
        request: RecoveryRequest,
        guard: NoProgressGuard,
        prior_hint: AdvisoryProgressHint | None,
        payload_sha256: str,
        proof_kind: str,
        http_status: int | None,
        diagnostic: str | None,
    ) -> int | None:
        if (
            not request.has_active_write_lease
            or request.slot_key is None
            or request.generation is None
            or proof_kind not in VALID_NON_SUBMISSION_PROOF_KINDS
        ):
            return None
        self._verify_write_boundary(
            request=request,
            guard=guard,
            prior_durable_hint=prior_hint,
        )
        proof_record = NonSubmissionProofRecord(
            record_id=(
                f"non-submission:{request.operation_id}:{request.generation}:"
                f"{request.ownership_proof.writer_epoch}"
            ),
            slot_key=request.slot_key,
            generation=request.generation,
            operation_id=request.operation_id,
            pr_head_sha=request.ownership_proof.pr_head_sha,
            proof_kind=proof_kind,
            payload_sha256=payload_sha256,
            recorded_at=_utc_now_iso(),
            http_status=http_status,
            diagnostic=diagnostic,
        )
        self.mutation_bound.consume("write_intent_comment", "non_submission_proof")
        resp = self._execute_with_throttle(
            request=request,
            guard=guard,
            prior_durable_hint=prior_hint,
            call_fn=lambda ts: self.client.execute(
                "write_intent_comment",
                method="POST",
                path=(
                    f"/repos/{request.owner}/{request.repo}/issues/"
                    f"{request.pull_request}/comments"
                ),
                event=request.event,
                visibility=request.visibility,
                granted_scopes=request.granted_scopes,
                credential_kind=CredentialKind.APP_BROKER,
                body={"body": format_v6_record_marker(proof_record)},
                allow_checkpoint_reserve_override=True,
                now_epoch=ts,
            ),
        )
        eff_id = verify_write_response_readback(resp, expected_marker=proof_record)
        return eff_id if type(eff_id) is int and eff_id > 0 else None

    def _validate_pr_resident_result(
        self,
        *,
        request: RecoveryRequest,
        unresolved_results: Sequence[UnresolvedResultRecord],
    ) -> UnresolvedResultRecord | RecoveryHold:
        matching = [
            rec
            for rec in unresolved_results
            if rec.operation_id == request.operation_id
            and rec.pr_head_sha == request.ownership_proof.pr_head_sha
        ]
        if not matching:
            return RecoveryHold(
                operation_id=request.operation_id,
                blocker="missing_pr_unresolved_result",
                missing_evidence=(
                    f"unresolved_result:{request.operation_id}:{request.ownership_proof.pr_head_sha}",
                ),
                zero_model_calls_started=True,
            )
        result = matching[-1]
        expected_digest = canonical_sha256(result.findings_payload)
        if result.payload_sha256 != expected_digest:
            return RecoveryHold(
                operation_id=request.operation_id,
                blocker="corrupted_pr_unresolved_result_digest",
                missing_evidence=(f"payload_sha256:{result.payload_sha256}",),
                zero_model_calls_started=True,
            )
        if request.observed_evidence_digests is not None:
            observed = set(request.observed_evidence_digests)
            missing = tuple(
                digest
                for digest in result.required_evidence_digests
                if digest not in observed
            )
            if missing:
                return RecoveryHold(
                    operation_id=request.operation_id,
                    blocker="missing_required_pr_evidence",
                    missing_evidence=missing,
                    zero_model_calls_started=True,
                )
        return result

    def reconcile_takeover_read_only(
        self,
        *,
        owner: str,
        repo: str,
        repository_id: int,
        pull_request: int,
        slot_key: str,
        event: GitHubEventKind,
        visibility: RepositoryVisibility,
        granted_scopes: Collection[str],
        old_generation: int | None = None,
        old_lease_epoch: int | None = None,
        expected_app_id: int | None = None,
        expected_bot_login: str | None = None,
        expected_bot_user_id: int | None = None,
        mac_verifier: Callable[[OwnerMarkerRecord], bool] | None = None,
        affirmative_non_submission_ops: Collection[str] = (),
        scan_review_comments: bool = True,
        scan_check_runs: bool = True,
        graphql_query: str | None = None,
        step_summary_sink: Callable[[str], None] | Path | None = None,
        operation_id: str = "takeover-reconcile",
    ) -> TakeoverReconciliationResult:
        """Execute read-only takeover reconciliation strictly on ``GITHUB_TOKEN``."""

        guard = NoProgressGuard(operation_id=operation_id)
        dummy_proof = WriterOwnershipProof(
            repository_id=repository_id,
            pull_request=pull_request,
            operation_id=operation_id,
            run_id=1,
            run_attempt=1,
            writer_epoch=0,
            pr_head_sha="0" * 40,
            verified_by_broker=False,
        )
        req = RecoveryRequest(
            owner=owner,
            repo=repo,
            repository_id=repository_id,
            pull_request=pull_request,
            operation_id=operation_id,
            ownership_proof=dummy_proof,
            event=event,
            visibility=visibility,
            granted_scopes=granted_scopes,
            expected_app_id=expected_app_id,
            expected_bot_login=expected_bot_login,
            expected_bot_user_id=expected_bot_user_id,
            graphql_query=graphql_query,
            has_active_write_lease=False,
            step_summary_sink=step_summary_sink,
        )
        exec_fn = self._throttled_page_executor(
            request=req, guard=guard, prior_hint=None
        )

        comment_res = self.paginator.scan_rest(
            endpoint_id="pr_issue_comments",
            path=f"/repos/{owner}/{repo}/issues/{pull_request}/comments?per_page=100",
            event=event,
            visibility=visibility,
            granted_scopes=granted_scopes,
            progress_guard=guard,
            phase_name="takeover_scan_comments",
            now_epoch=self.now_fn(),
            execute_fn=exec_fn,
        )
        review_res = self.paginator.scan_rest(
            endpoint_id="pr_reviews",
            path=f"/repos/{owner}/{repo}/pulls/{pull_request}/reviews?per_page=100",
            event=event,
            visibility=visibility,
            granted_scopes=granted_scopes,
            progress_guard=guard,
            phase_name="takeover_scan_reviews",
            now_epoch=self.now_fn(),
            execute_fn=exec_fn,
        )
        review_comments: tuple[PaginatedRecord, ...] = ()
        if scan_review_comments:
            rc_res = self.paginator.scan_rest(
                endpoint_id="pr_review_comments",
                path=f"/repos/{owner}/{repo}/pulls/{pull_request}/comments?per_page=100",
                event=event,
                visibility=visibility,
                granted_scopes=granted_scopes,
                progress_guard=guard,
                phase_name="takeover_scan_review_comments",
                now_epoch=self.now_fn(),
                execute_fn=exec_fn,
            )
            review_comments = rc_res.records

        review_threads: tuple[PaginatedRecord, ...] = ()
        if graphql_query is not None:
            gql_res = self.paginator.scan_graphql(
                query=graphql_query,
                variables={"owner": owner, "repo": repo, "pullNumber": pull_request},
                event=event,
                visibility=visibility,
                granted_scopes=granted_scopes,
                progress_guard=guard,
                phase_name="takeover_scan_threads",
                now_epoch=self.now_fn(),
                execute_fn=exec_fn,
            )
            review_threads = gql_res.records

        collected = self._collect_full_ledger(
            comment_res.records,
            expected_app_id=expected_app_id,
            expected_bot_login=expected_bot_login,
            expected_bot_user_id=expected_bot_user_id,
        )
        owners = collected.owners
        intents = collected.intents
        receipts = collected.receipts
        non_submissions = collected.non_submissions
        holds = collected.holds
        unresolved_results = collected.unresolved_results
        app_marker_comments = collected.app_marker_comments
        anchor_comment_id = collected.anchor_comment_id

        # Validate App-authored owner lineage if slot/bot login is configured or owners exist.
        validated_owners: tuple[OwnerMarkerRecord, ...] = owners
        if expected_bot_login is not None:
            try:
                validated_owners = validate_owner_lineage(
                    app_marker_comments,
                    slot_key=slot_key,
                    repository_id=repository_id,
                    pull_request=pull_request,
                    app_login=expected_bot_login,
                    mac_verifier=mac_verifier,
                )
            except V6LedgerIntegrityError as exc:
                blocker = exc.diagnostic or "v6_lease_slot_continuity_unproved"
                write_v6_step_summary(
                    render_v6_hold_summary(
                        operation_id=operation_id,
                        blocker=blocker,
                        missing_evidence=(str(exc),),
                        slot_key=slot_key,
                        repository=f"{owner}/{repo}",
                        pull_request=pull_request,
                    ),
                    sink=step_summary_sink,
                )
                return TakeoverReconciliationResult(
                    reconciliation_outcome="ambiguous_hold",
                    adopted_effects=(),
                    unsubmitted_intents=(),
                    pending_unresolved_results=unresolved_results,
                    owner_lineage=(),
                    unresolved_holds=holds,
                    completed_operations=(),
                    reconciliation_evidence=None,
                    old_owner_comment_id=anchor_comment_id,
                    blocker=blocker,
                    missing_evidence=(str(exc),),
                    anchor_comment_id=anchor_comment_id,
                )

        if old_generation is not None and old_generation >= 1:
            effective_old_gen = old_generation
        elif validated_owners:
            effective_old_gen = max(o.generation for o in validated_owners)
        else:
            gen_ints = [i.generation for i in intents if i.generation is not None]
            effective_old_gen = max(gen_ints) if gen_ints else 1

        old_owner_comment_id = collected.owner_comment_ids_by_generation.get(
            effective_old_gen, anchor_comment_id
        )

        if old_generation is not None and old_generation >= 1:
            target_intents = [
                i
                for i in intents
                if (i.generation is not None and i.generation == old_generation)
                or (
                    i.generation is None
                    and old_lease_epoch is not None
                    and i.writer_epoch == old_lease_epoch
                )
            ]
        else:
            target_intents = list(intents)

        def _build_evidence(resolutions: list[dict[str, Any]]) -> dict[str, Any]:
            ev: dict[str, Any] = {
                "old_generation": effective_old_gen,
                "intent_resolutions": resolutions,
            }
            if old_owner_comment_id is not None:
                ev["old_owner_comment_id"] = old_owner_comment_id
            return ev

        def _unresolved_resolutions() -> list[dict[str, Any]]:
            items: list[dict[str, Any]] = []
            for ti in target_intents:
                tcid = collected.comment_ids_by_record_id.get(ti.record_id)
                if tcid is not None:
                    items.append(
                        {
                            "operation_id": ti.operation_id,
                            "intent_comment_id": tcid,
                            "resolution": "ambiguous_hold",
                        }
                    )
            return items

        unresolved_holds = tuple(h for h in holds if h.resolved_by_record_id is None)
        if unresolved_holds:
            blocker = "unresolved_durable_hold"
            missing = tuple(h.record_id for h in unresolved_holds)
            write_v6_step_summary(
                render_v6_hold_summary(
                    operation_id=operation_id,
                    blocker=blocker,
                    missing_evidence=missing,
                    slot_key=slot_key,
                    repository=f"{owner}/{repo}",
                    pull_request=pull_request,
                ),
                sink=step_summary_sink,
            )
            return TakeoverReconciliationResult(
                reconciliation_outcome="ambiguous_hold",
                adopted_effects=(),
                unsubmitted_intents=(),
                pending_unresolved_results=unresolved_results,
                owner_lineage=validated_owners,
                unresolved_holds=unresolved_holds,
                completed_operations=(),
                reconciliation_evidence=_build_evidence(_unresolved_resolutions()),
                old_owner_comment_id=old_owner_comment_id,
                blocker=blocker,
                missing_evidence=missing,
                anchor_comment_id=anchor_comment_id,
            )

        summary = reconcile_immutable_records(intents, receipts)
        if summary.conflicting_records:
            blocker = "conflicting_immutable_records"
            missing = tuple(c.record_id for c in summary.conflicting_records)
            write_v6_step_summary(
                render_v6_hold_summary(
                    operation_id=operation_id,
                    blocker=blocker,
                    missing_evidence=missing,
                    slot_key=slot_key,
                    repository=f"{owner}/{repo}",
                    pull_request=pull_request,
                ),
                sink=step_summary_sink,
            )
            return TakeoverReconciliationResult(
                reconciliation_outcome="ambiguous_hold",
                adopted_effects=(),
                unsubmitted_intents=(),
                pending_unresolved_results=unresolved_results,
                owner_lineage=validated_owners,
                unresolved_holds=(),
                completed_operations=summary.completed_operations,
                reconciliation_evidence=_build_evidence(_unresolved_resolutions()),
                old_owner_comment_id=old_owner_comment_id,
                blocker=blocker,
                missing_evidence=missing,
                anchor_comment_id=anchor_comment_id,
            )

        check_runs_by_head: dict[str, tuple[PaginatedRecord, ...]] = {}
        if scan_check_runs:
            for intent in target_intents:
                if intent.effect_kind in {
                    "check_run",
                    "pr_check_run",
                    "check_run_write",
                    "check_publish",
                } and (intent.pr_head_sha not in check_runs_by_head):
                    cr_res = self.paginator.scan_rest(
                        endpoint_id="check_runs_and_suites",
                        path=(
                            f"/repos/{owner}/{repo}/commits/"
                            f"{intent.pr_head_sha}/check-runs?per_page=100"
                        ),
                        event=event,
                        visibility=visibility,
                        granted_scopes=granted_scopes,
                        progress_guard=guard,
                        phase_name=f"takeover_scan_check_runs:{intent.pr_head_sha[:7]}",
                        now_epoch=self.now_fn(),
                        execute_fn=exec_fn,
                        record_extractor=lambda body: tuple(
                            PaginatedRecord.from_payload(
                                item.get("id", item.get("node_id")), item
                            )
                            for item in (
                                body.get("check_runs", ())
                                if isinstance(body, Mapping)
                                else body
                            )
                            if isinstance(item, Mapping)
                        ),
                    )
                    check_runs_by_head[intent.pr_head_sha] = cr_res.records

        receipt_by_op_payload: dict[tuple[str, str], CompletionReceiptRecord] = {
            (r.operation_id, r.payload_sha256): r for r in summary.matched_receipts
        }
        adopted: list[tuple[WriteIntentRecord, str | int]] = []
        unsubmitted: list[WriteIntentRecord] = []
        intent_resolutions: list[dict[str, Any]] = []
        hold_blocker: str | None = None
        hold_missing: list[str] = []
        hold_head_sha: str | None = None
        hold_op_id: str | None = None

        del (
            affirmative_non_submission_ops
        )  # Read-only Gate 2 requires PR-resident NonSubmissionProofRecord.

        for intent in target_intents:
            intent_cid = collected.comment_ids_by_record_id.get(intent.record_id)
            if intent_cid is None:
                hold_blocker = hold_blocker or "missing_intent_comment_id"
                hold_missing.append(f"intent_comment_id:{intent.record_id}")
                hold_head_sha = hold_head_sha or intent.pr_head_sha
                hold_op_id = hold_op_id or intent.operation_id
                continue

            existing_receipt = receipt_by_op_payload.get(
                (intent.operation_id, intent.payload_sha256)
            )
            if existing_receipt is not None:
                receipt_cid = collected.comment_ids_by_record_id.get(
                    existing_receipt.record_id
                )
                if receipt_cid is not None:
                    intent_resolutions.append(
                        {
                            "operation_id": intent.operation_id,
                            "intent_comment_id": intent_cid,
                            "resolution": "receipt_present",
                            "receipt_comment_id": receipt_cid,
                        }
                    )
                    continue
                hold_blocker = hold_blocker or "missing_receipt_comment_id"
                hold_missing.append(f"receipt_comment_id:{existing_receipt.record_id}")
                hold_head_sha = hold_head_sha or intent.pr_head_sha
                hold_op_id = hold_op_id or intent.operation_id
                intent_resolutions.append(
                    {
                        "operation_id": intent.operation_id,
                        "intent_comment_id": intent_cid,
                        "resolution": "ambiguous_hold",
                    }
                )
                continue

            matched_ids, contradictory = _match_native_effects_for_intent(
                intent,
                expected_app_id=expected_app_id,
                expected_bot_login=expected_bot_login,
                expected_bot_user_id=expected_bot_user_id,
                reviews=review_res.records,
                issue_comments=comment_res.records,
                review_comments=review_comments,
                check_runs=check_runs_by_head.get(intent.pr_head_sha, ()),
                review_threads=review_threads,
            )
            if contradictory:
                hold_blocker = hold_blocker or "contradictory_effect_match"
                hold_missing.append(f"contradictory_effect:{intent.operation_id}")
                hold_head_sha = hold_head_sha or intent.pr_head_sha
                hold_op_id = hold_op_id or intent.operation_id
                intent_resolutions.append(
                    {
                        "operation_id": intent.operation_id,
                        "intent_comment_id": intent_cid,
                        "resolution": "ambiguous_hold",
                    }
                )
                continue
            if len(matched_ids) > 1:
                hold_blocker = hold_blocker or "duplicate_effect_match"
                hold_missing.append(f"duplicate_effect:{intent.operation_id}")
                hold_head_sha = hold_head_sha or intent.pr_head_sha
                hold_op_id = hold_op_id or intent.operation_id
                intent_resolutions.append(
                    {
                        "operation_id": intent.operation_id,
                        "intent_comment_id": intent_cid,
                        "resolution": "ambiguous_hold",
                    }
                )
                continue
            if len(matched_ids) == 1:
                eff_id = matched_ids[0]
                adopted.append((intent, eff_id))
                intent_resolutions.append(
                    {
                        "operation_id": intent.operation_id,
                        "intent_comment_id": intent_cid,
                        "resolution": "adopted_effect",
                        "native_effect_kind": _canonical_native_effect_kind(
                            intent.effect_kind
                        ),
                        "native_effect_id": eff_id,
                    }
                )
                continue

            intent_gen = (
                intent.generation
                if intent.generation is not None
                else effective_old_gen
            )
            matched_proof: NonSubmissionProofRecord | None = None
            matched_proof_cid: int | None = None
            for proof in non_submissions:
                if (
                    proof.operation_id == intent.operation_id
                    and proof.generation == intent_gen
                    and proof.payload_sha256 == intent.payload_sha256
                    and proof.pr_head_sha == intent.pr_head_sha
                    and proof.slot_key == slot_key
                    and proof.proof_kind in VALID_NON_SUBMISSION_PROOF_KINDS
                ):
                    pcid = collected.comment_ids_by_record_id.get(proof.record_id)
                    if pcid is not None:
                        matched_proof = proof
                        matched_proof_cid = pcid
                        break

            if matched_proof is not None and matched_proof_cid is not None:
                unsubmitted.append(intent)
                intent_resolutions.append(
                    {
                        "operation_id": intent.operation_id,
                        "intent_comment_id": intent_cid,
                        "resolution": "proved_not_submitted",
                        "proof_comment_id": matched_proof_cid,
                    }
                )
                continue

            hold_blocker = hold_blocker or "unproved_non_submission_after_intent"
            hold_missing.append(
                f"effect_verification:{intent.operation_id}:{intent.payload_sha256}"
            )
            hold_head_sha = hold_head_sha or intent.pr_head_sha
            hold_op_id = hold_op_id or intent.operation_id
            intent_resolutions.append(
                {
                    "operation_id": intent.operation_id,
                    "intent_comment_id": intent_cid,
                    "resolution": "ambiguous_hold",
                }
            )

        evidence = _build_evidence(intent_resolutions)
        if hold_blocker is not None:
            missing_tuple = tuple(hold_missing)
            write_v6_step_summary(
                render_v6_hold_summary(
                    operation_id=hold_op_id or operation_id,
                    blocker=hold_blocker,
                    missing_evidence=missing_tuple,
                    slot_key=slot_key,
                    repository=f"{owner}/{repo}",
                    pull_request=pull_request,
                    pr_head_sha=hold_head_sha,
                ),
                sink=step_summary_sink,
            )
            return TakeoverReconciliationResult(
                reconciliation_outcome="ambiguous_hold",
                adopted_effects=tuple(adopted),
                unsubmitted_intents=(),
                pending_unresolved_results=unresolved_results,
                owner_lineage=validated_owners,
                unresolved_holds=(),
                completed_operations=summary.completed_operations,
                reconciliation_evidence=evidence,
                old_owner_comment_id=old_owner_comment_id,
                blocker=hold_blocker,
                missing_evidence=missing_tuple,
                anchor_comment_id=anchor_comment_id,
            )

        if adopted:
            outcome = "adopted_missing_receipt"
        elif unsubmitted:
            outcome = "proved_not_submitted"
        else:
            outcome = "clean_receipts"

        return TakeoverReconciliationResult(
            reconciliation_outcome=outcome,
            adopted_effects=tuple(adopted),
            unsubmitted_intents=tuple(unsubmitted),
            pending_unresolved_results=unresolved_results,
            owner_lineage=validated_owners,
            unresolved_holds=(),
            completed_operations=summary.completed_operations,
            reconciliation_evidence=evidence,
            old_owner_comment_id=old_owner_comment_id,
            anchor_comment_id=anchor_comment_id,
        )

    def recover_and_publish(
        self,
        request: RecoveryRequest,
        *,
        progress_guard: NoProgressGuard | None = None,
    ) -> RecoveryOutcome:
        """Execute v6 recovery & publication with zero fallback model calls."""

        if (request.slot_key is not None or request.owner_marker is not None) and (
            request.generation is None or request.generation < 1
        ):
            raise StaleWriterOwnershipError(
                "v6 pilot recovery requires a positive coordinator generation",
                diagnostic="v6_lease_slot_continuity_unproved",
            )

        guard = (
            NoProgressGuard(operation_id=request.operation_id)
            if progress_guard is None
            else progress_guard
        )
        guard.ensure_not_stalled()

        prior_hint: AdvisoryProgressHint | None = None
        try:
            comment_records, review_records, latest_hint = self._scan_pr_surfaces(
                request=request,
                guard=guard,
                prior_hint=None,
            )
            prior_hint = latest_hint

            collected = self._collect_full_ledger(
                comment_records,
                expected_app_id=request.expected_app_id,
                expected_bot_login=request.expected_bot_login,
                expected_bot_user_id=request.expected_bot_user_id,
            )
            owners = collected.owners
            intents = collected.intents
            receipts = collected.receipts
            non_submissions = collected.non_submissions
            holds = collected.holds
            unresolved_results = collected.unresolved_results
            found_hints = collected.hints
            app_marker_comments = collected.app_marker_comments
            owner_cid: int | None = (
                collected.owner_comment_ids_by_generation.get(request.generation)
                if request.generation is not None
                else None
            )

            matching_hints = [
                h for h in found_hints if h.operation_id == request.operation_id
            ]
            if matching_hints:
                persisted_hint = matching_hints[-1]
                prior_hint = persisted_hint
                if progress_guard is None:
                    guard = NoProgressGuard.from_advisory_hint(
                        persisted_hint,
                        max_stall_steps=guard.max_stall_steps,
                        max_transient_retries=guard.max_transient_retries,
                    )
                else:
                    guard.stall_count = max(
                        guard.stall_count, persisted_hint.stall_count
                    )
                    guard.transient_error_count = max(
                        guard.transient_error_count,
                        persisted_hint.transient_error_count,
                    )
                guard.ensure_not_stalled()

            if request.slot_key is not None and request.expected_bot_login is not None:
                try:
                    validate_owner_lineage(
                        app_marker_comments,
                        slot_key=request.slot_key,
                        repository_id=request.repository_id,
                        pull_request=request.pull_request,
                        app_login=request.expected_bot_login,
                        mac_verifier=request.mac_verifier,
                    )
                except V6LedgerIntegrityError as exc:
                    return self._finalize_hold(
                        request=request,
                        guard=guard,
                        prior_hint=prior_hint,
                        blocker=exc.diagnostic or "v6_lease_slot_continuity_unproved",
                        missing_evidence=(str(exc),),
                    )

            unresolved_holds = [
                h
                for h in holds
                if h.resolved_by_record_id is None
                and (
                    h.operation_id == request.operation_id
                    or (request.slot_key is not None and h.slot_key == request.slot_key)
                )
            ]
            if unresolved_holds:
                return self._finalize_hold(
                    request=request,
                    guard=guard,
                    prior_hint=prior_hint,
                    blocker="unresolved_durable_hold",
                    missing_evidence=tuple(h.record_id for h in unresolved_holds),
                )

            summary = reconcile_immutable_records(intents, receipts)
            op_conflicts = [
                c
                for c in summary.conflicting_records
                if c.operation_id == request.operation_id
            ]
            if op_conflicts:
                return self._finalize_hold(
                    request=request,
                    guard=guard,
                    prior_hint=prior_hint,
                    blocker="conflicting_immutable_records",
                    missing_evidence=tuple(c.record_id for c in op_conflicts),
                )

            if request.operation_id in summary.completed_operations:
                matched = next(
                    r
                    for r in summary.matched_receipts
                    if r.operation_id == request.operation_id
                )
                return RecoveryCompleted(
                    operation_id=request.operation_id,
                    pr_head_sha=request.ownership_proof.pr_head_sha,
                    receipt=matched,
                    github_effect_id=matched.github_effect_id,
                    reconciled_from_existing_effect=True,
                    used_pr_resident_result=False,
                    readback_gets_emitted=0,
                    model_calls_started=0,
                    owner_comment_id=owner_cid,
                    receipt_comment_id=collected.comment_ids_by_record_id.get(
                        matched.record_id
                    ),
                )

            op_unreconciled = [
                i
                for i in summary.unreconciled_intents
                if i.operation_id == request.operation_id
            ]
            readback_gets = 0
            existing_intent: WriteIntentRecord | None = (
                op_unreconciled[-1] if op_unreconciled else None
            )
            current_gen_intents = [
                i
                for i in op_unreconciled
                if (
                    request.generation is not None
                    and i.generation == request.generation
                )
                or (
                    request.generation is None
                    and i.writer_epoch == request.ownership_proof.writer_epoch
                )
            ]
            current_intent: WriteIntentRecord | None = (
                current_gen_intents[-1] if current_gen_intents else None
            )
            intent_cid: int | None = (
                collected.comment_ids_by_record_id.get(current_intent.record_id)
                if current_intent is not None
                else None
            )

            if existing_intent is not None:
                outcome_known, landed, verified_id, gets = (
                    self._reconcile_effect_surface(
                        request=request,
                        guard=guard,
                        prior_hint=prior_hint,
                        expected_payload_sha256=existing_intent.payload_sha256,
                        scanned_reviews=review_records,
                        expected_head_sha=existing_intent.pr_head_sha,
                        rescan_before_verify=False,
                    )
                )
                readback_gets += gets
                if not outcome_known:
                    return self._finalize_hold(
                        request=request,
                        guard=guard,
                        prior_hint=prior_hint,
                        blocker="ambiguous_write_outcome",
                        missing_evidence=(
                            f"effect_verification:{request.operation_id}:{existing_intent.payload_sha256}",
                        ),
                    )
                if landed and verified_id is not None:
                    guard.record_step(
                        new_records_count=0,
                        reconciled_effect=True,
                        new_phase="reconcile_landed",
                        state_digest=existing_intent.payload_sha256,
                    )
                    if owner_cid is None:
                        owner_cid = self._persist_owner_marker_if_needed(
                            request=request,
                            guard=guard,
                            prior_hint=prior_hint,
                            existing_owners=owners,
                            comment_ids_by_record_id=collected.comment_ids_by_record_id,
                        )
                    try:
                        receipt, receipt_gets, receipt_cid = (
                            self._persist_completion_receipt(
                                request=request,
                                guard=guard,
                                prior_hint=prior_hint,
                                github_effect_id=verified_id,
                                payload_sha256=existing_intent.payload_sha256,
                                existing_receipts=receipts,
                                comment_ids_by_record_id=collected.comment_ids_by_record_id,
                            )
                        )
                    except _AmbiguousReceiptOutcomeError:
                        return self._finalize_hold(
                            request=request,
                            guard=guard,
                            prior_hint=prior_hint,
                            blocker="ambiguous_receipt_outcome",
                            missing_evidence=(
                                f"receipt_verification:{request.operation_id}:{existing_intent.payload_sha256}",
                            ),
                        )
                    return RecoveryCompleted(
                        operation_id=request.operation_id,
                        pr_head_sha=request.ownership_proof.pr_head_sha,
                        receipt=receipt,
                        github_effect_id=verified_id,
                        reconciled_from_existing_effect=True,
                        used_pr_resident_result=False,
                        readback_gets_emitted=readback_gets + receipt_gets,
                        model_calls_started=0,
                        owner_comment_id=owner_cid,
                        intent_comment_id=intent_cid,
                        receipt_comment_id=receipt_cid,
                        intent_record=current_intent,
                    )

            # Effect not yet landed: must recover strictly from PR-resident result.
            validated = self._validate_pr_resident_result(
                request=request,
                unresolved_results=unresolved_results,
            )
            if isinstance(validated, RecoveryHold):
                return self._finalize_hold(
                    request=request,
                    guard=guard,
                    prior_hint=prior_hint,
                    blocker=validated.blocker,
                    missing_evidence=validated.missing_evidence,
                )
            pr_result = validated
            payload_sha256 = pr_result.payload_sha256

            if (
                existing_intent is not None
                and existing_intent.payload_sha256 != payload_sha256
            ):
                return self._finalize_hold(
                    request=request,
                    guard=guard,
                    prior_hint=prior_hint,
                    blocker="pr_result_digest_mismatch_with_intent",
                    missing_evidence=(
                        f"intent_digest_match:{existing_intent.record_id}:"
                        f"{existing_intent.payload_sha256}",
                    ),
                )

            effect_path = (
                request.effect_path
                if request.effect_path is not None
                else (
                    f"/repos/{request.owner}/{request.repo}/pulls/"
                    f"{request.pull_request}/reviews"
                )
            )
            _assert_target_pr_binding(request, path=effect_path)

            if existing_intent is not None:
                has_pr_non_submission_proof = any(
                    proof.operation_id == existing_intent.operation_id
                    and proof.payload_sha256 == existing_intent.payload_sha256
                    and proof.pr_head_sha == existing_intent.pr_head_sha
                    and proof.proof_kind in VALID_NON_SUBMISSION_PROOF_KINDS
                    and (request.slot_key is None or proof.slot_key == request.slot_key)
                    and (
                        existing_intent.generation is None
                        or proof.generation == existing_intent.generation
                    )
                    for proof in non_submissions
                )
                if (
                    not has_pr_non_submission_proof
                    and not request.affirmative_non_submission
                ):
                    if request.has_active_write_lease:
                        self._verify_write_boundary(
                            request=request,
                            guard=guard,
                            prior_durable_hint=prior_hint,
                            target_path=effect_path,
                        )
                    return self._finalize_hold(
                        request=request,
                        guard=guard,
                        prior_hint=prior_hint,
                        blocker="unproved_non_submission_after_intent",
                        missing_evidence=(
                            f"non_submission_proof:{request.operation_id}:{existing_intent.payload_sha256}",
                        ),
                    )

            if current_intent is None:
                owner_cid = self._persist_owner_marker_if_needed(
                    request=request,
                    guard=guard,
                    prior_hint=prior_hint,
                    existing_owners=owners,
                    comment_ids_by_record_id=collected.comment_ids_by_record_id,
                )
                self._verify_write_boundary(
                    request=request,
                    guard=guard,
                    prior_durable_hint=prior_hint,
                    target_path=effect_path,
                )
                target_digest = request.target_digest or canonical_sha256(
                    {
                        "repo": request.repository_id,
                        "pr": request.pull_request,
                        "head": request.ownership_proof.pr_head_sha,
                    }
                )
                intent_record_id = (
                    f"intent:{request.operation_id}:{request.ownership_proof.writer_epoch}"
                    if existing_intent is None
                    else (
                        f"intent:{request.operation_id}:g{request.generation or 0}:"
                        f"{request.ownership_proof.writer_epoch}"
                    )
                )
                current_intent = WriteIntentRecord(
                    record_id=intent_record_id,
                    operation_id=request.operation_id,
                    writer_epoch=request.ownership_proof.writer_epoch,
                    pr_head_sha=request.ownership_proof.pr_head_sha,
                    effect_kind=request.effect_kind,
                    payload_sha256=payload_sha256,
                    target_digest=target_digest,
                    generation=request.generation,
                    target_location=effect_path,
                )
                self.mutation_bound.consume("write_intent_comment", "write_intent")
                intent_resp = self._execute_with_throttle(
                    request=request,
                    guard=guard,
                    prior_durable_hint=prior_hint,
                    call_fn=lambda ts: self.client.execute(
                        "write_intent_comment",
                        method="POST",
                        path=(
                            f"/repos/{request.owner}/{request.repo}/issues/"
                            f"{request.pull_request}/comments"
                        ),
                        event=request.event,
                        visibility=request.visibility,
                        granted_scopes=request.granted_scopes,
                        credential_kind=CredentialKind.APP_BROKER,
                        body={"body": format_v6_record_marker(current_intent)},
                        now_epoch=ts,
                    ),
                )
                created_intent_id = verify_write_response_readback(
                    intent_resp, expected_marker=current_intent
                )
                if type(created_intent_id) is int and created_intent_id > 0:
                    intent_cid = created_intent_id

            # Verify exclusive writer ownership again immediately before external write.
            self._verify_write_boundary(
                request=request,
                guard=guard,
                prior_durable_hint=prior_hint,
                target_path=effect_path,
            )

            rendered_body = (
                f"ReviewSensei recovery publication ({len(pr_result.findings_payload)} finding(s)).\n"
                f"{_effect_tag(request.operation_id, payload_sha256)}"
            )
            effect_payload = {
                "commit_id": request.ownership_proof.pr_head_sha,
                "event": "COMMENT",
                "body": rendered_body,
                "comments": [dict(entry) for entry in pr_result.findings_payload],
            }

            self.mutation_bound.consume(request.effect_endpoint_id, request.effect_kind)
            effect_id: str | int | None = None
            reconciled_after_lost_write = False
            try:
                write_resp = self._execute_with_throttle(
                    request=request,
                    guard=guard,
                    prior_durable_hint=prior_hint,
                    call_fn=lambda ts: self.client.execute(
                        request.effect_endpoint_id,
                        method=request.effect_method,
                        path=effect_path,
                        event=request.event,
                        visibility=request.visibility,
                        granted_scopes=request.granted_scopes,
                        credential_kind=CredentialKind.APP_BROKER,
                        body=effect_payload,
                        now_epoch=ts,
                    ),
                )
                if write_resp.status in {400, 404, 409, 410, 422}:
                    blocker = f"definitive_github_rejection_{write_resp.status}"
                    missing = (
                        f"non_submission_proof:{request.operation_id}:{payload_sha256}",
                    )
                    if (
                        request.has_active_write_lease
                        and request.slot_key is not None
                        and request.generation is not None
                    ):
                        self._persist_non_submission_proof(
                            request=request,
                            guard=guard,
                            prior_hint=prior_hint,
                            payload_sha256=payload_sha256,
                            proof_kind="definitive_github_rejection",
                            http_status=write_resp.status,
                            diagnostic=blocker,
                        )
                        write_v6_step_summary(
                            render_v6_hold_summary(
                                operation_id=request.operation_id,
                                blocker=blocker,
                                missing_evidence=missing,
                                slot_key=request.slot_key,
                                repository=f"{request.owner}/{request.repo}",
                                pull_request=request.pull_request,
                                pr_head_sha=request.ownership_proof.pr_head_sha,
                            ),
                            sink=request.step_summary_sink,
                        )
                        return RecoveryHold(
                            operation_id=request.operation_id,
                            blocker=blocker,
                            missing_evidence=missing,
                            zero_model_calls_started=True,
                        )
                    return self._finalize_hold(
                        request=request,
                        guard=guard,
                        prior_hint=prior_hint,
                        blocker=blocker,
                        missing_evidence=missing,
                    )
                effect_id = verify_write_response_readback(
                    write_resp,
                    expected_operation_id=request.operation_id,
                    expected_payload_sha256=payload_sha256,
                )
            except GitHubHTTPError as http_err:
                if (
                    not isinstance(
                        http_err,
                        (
                            GitHubHTTPRateLimitError,
                            GitHubHTTPPermissionError,
                            GitHubHTTPTransientError,
                        ),
                    )
                    and http_err.http_status in {400, 404, 409, 410, 422}
                    and request.has_active_write_lease
                    and request.slot_key is not None
                    and request.generation is not None
                ):
                    blocker = f"definitive_github_rejection_{http_err.http_status}"
                    missing = (
                        f"non_submission_proof:{request.operation_id}:{payload_sha256}",
                    )
                    self._persist_non_submission_proof(
                        request=request,
                        guard=guard,
                        prior_hint=prior_hint,
                        payload_sha256=payload_sha256,
                        proof_kind="definitive_github_rejection",
                        http_status=http_err.http_status,
                        diagnostic=blocker,
                    )
                    write_v6_step_summary(
                        render_v6_hold_summary(
                            operation_id=request.operation_id,
                            blocker=blocker,
                            missing_evidence=missing,
                            slot_key=request.slot_key,
                            repository=f"{request.owner}/{request.repo}",
                            pull_request=request.pull_request,
                            pr_head_sha=request.ownership_proof.pr_head_sha,
                        ),
                        sink=request.step_summary_sink,
                    )
                    return RecoveryHold(
                        operation_id=request.operation_id,
                        blocker=blocker,
                        missing_evidence=missing,
                        zero_model_calls_started=True,
                    )
                raise
            except (
                _AmbiguousWriteOutcomeError,
                TimeoutError,
                ConnectionError,
                GitHubHTTPTransientError,
            ):
                outcome_known, landed, verified_id, gets = (
                    self._reconcile_effect_surface(
                        request=request,
                        guard=guard,
                        prior_hint=prior_hint,
                        expected_payload_sha256=payload_sha256,
                        scanned_reviews=review_records,
                        expected_head_sha=request.ownership_proof.pr_head_sha,
                        rescan_before_verify=True,
                    )
                )
                readback_gets += gets
                if not outcome_known or not landed or verified_id is None:
                    return self._finalize_hold(
                        request=request,
                        guard=guard,
                        prior_hint=prior_hint,
                        blocker="lost_write_outcome_unverified",
                        missing_evidence=(
                            f"effect_verification:{request.operation_id}:{payload_sha256}",
                        ),
                    )
                effect_id = verified_id
                reconciled_after_lost_write = True

            try:
                receipt, receipt_gets, receipt_cid = self._persist_completion_receipt(
                    request=request,
                    guard=guard,
                    prior_hint=prior_hint,
                    github_effect_id=effect_id,
                    payload_sha256=payload_sha256,
                    existing_receipts=receipts,
                    comment_ids_by_record_id=collected.comment_ids_by_record_id,
                )
            except _AmbiguousReceiptOutcomeError:
                return self._finalize_hold(
                    request=request,
                    guard=guard,
                    prior_hint=prior_hint,
                    blocker="ambiguous_receipt_outcome",
                    missing_evidence=(
                        f"receipt_verification:{request.operation_id}:{payload_sha256}",
                    ),
                )
            return RecoveryCompleted(
                operation_id=request.operation_id,
                pr_head_sha=request.ownership_proof.pr_head_sha,
                receipt=receipt,
                github_effect_id=effect_id,
                reconciled_from_existing_effect=reconciled_after_lost_write,
                used_pr_resident_result=True,
                readback_gets_emitted=readback_gets + receipt_gets,
                model_calls_started=0,
                owner_comment_id=owner_cid,
                intent_comment_id=intent_cid,
                receipt_comment_id=receipt_cid,
                intent_record=current_intent,
            )
        except _ThrottlePausedSignal as paused:
            return paused.outcome
