"""ReviewSensei v6 GitHub-governed recovery orchestrator (Phases 2 & 3).

Coordinates exclusive broker writer-ownership fencing, immutable write
intent/receipt persistence, stability-verified pagination, persistent
no-progress guarding, write-response-as-readback request compression,
reconcile-before-retry lost-write recovery, and PR-resident evidence checks
without ever starting a fresh model call as a recovery fallback.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeAlias
from urllib.parse import urlsplit

from review_sensei.bounded_evidence import (
    AuthenticatedPart,
    EvidenceReadBudget,
    evidence_manifest,
    partition_evidence,
    read_partitioned_evidence,
)
from review_sensei.configuration import default_configuration
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
from .v6_fresh_analysis import (
    V6FreshAnalysisContext,
    V6FreshAnalysisError,
    compute_trusted_policy_and_config_digests,
    execute_v6_batch,
    finalize_v6_fresh_analysis_result,
    verify_expected_hunks_reviewed,
)
from .v6_pagination import (
    NoProgressGuard,
    PaginatedRecord,
    StabilityVerifiedPaginator,
)
from .v6_recovery_ledger import (
    MAX_MARKER_ENCODED_BYTES,
    VALID_NON_SUBMISSION_PROOF_KINDS,
    AdvisoryProgressHint,
    BatchDispatchRecord,
    BatchOutputRecord,
    BatchPlanManifestRecord,
    BatchPlanPartRecord,
    BatchPlanRecord,
    CompletionReceiptRecord,
    DurableHoldRecord,
    NonSubmissionProofRecord,
    OwnerMarkerRecord,
    ResultManifestRecord,
    ResultPartRecord,
    UnresolvedResultRecord,
    V6LedgerIntegrityError,
    V6LedgerRecord,
    WriteIntentRecord,
    WriterOwnershipProof,
    _record_from_mapping,
    _reject_json_constant,
    _to_json_compatible,
    canonical_json_dumps,
    canonical_sha256,
    extract_v6_record_markers,
    format_v6_record_marker,
    reconcile_immutable_records,
    record_to_dict,
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
DEFAULT_MAX_NATIVE_REVIEW_BODY_BYTES = 65_536
DEFAULT_MAX_NATIVE_REVIEW_INLINE_COMMENTS = 50
_PR_SCOPED_PATH_RE = re.compile(
    r"^/repos/([^/]+)/([^/]+)/(pulls|issues)/([1-9][0-9]*)(?:/|$)"
)
_SHA1_40_RE = re.compile(r"^[0-9a-f]{40}$")


def _default_trusted_digests() -> tuple[str, str]:
    global _CACHED_DEFAULT_DIGESTS
    if _CACHED_DEFAULT_DIGESTS is None:
        _CACHED_DEFAULT_DIGESTS = compute_trusted_policy_and_config_digests(
            default_configuration(),
            provider_name="ollama",
            model_name="llama3.2",
        )
    return _CACHED_DEFAULT_DIGESTS


_CACHED_DEFAULT_DIGESTS: tuple[str, str] | None = None


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
    reconciled_after_lost_write: bool = False
    detail_comment_ids: tuple[int, ...] = ()
    detail_resolutions: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if type(self.model_calls_started) is not int or self.model_calls_started < 0:
            raise ValueError(
                "v6 recovery model_calls_started must be a non-negative integer"
            )
        if (
            self.reconciled_after_lost_write
            and not self.reconciled_from_existing_effect
        ):
            raise ValueError(
                "reconciled_after_lost_write requires reconciled_from_existing_effect"
            )
        if (
            self.reconciled_from_existing_effect
            and not self.reconciled_after_lost_write
            and self.model_calls_started != 0
        ):
            raise ValueError(
                "v6 recovery from existing effect must never start a fresh model call"
            )
        if self.reconciled_after_lost_write and self.model_calls_started > 1:
            raise ValueError(
                "v6 recovery after lost write must start at most one model call"
            )


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
    initial_result_loader: (
        Callable[[], tuple[Sequence[Mapping[str, Any]], Sequence[str]]] | None
    ) = None
    pre_publish_head_verifier: Callable[[], Any] | None = None
    base_sha: str | None = None
    merge_base_sha: str | None = None
    inventory_sha256: str | None = None
    policy_digest: str | None = None
    configuration_digest: str | None = None
    max_native_review_body_bytes: int = DEFAULT_MAX_NATIVE_REVIEW_BODY_BYTES
    max_native_review_inline_comments: int = DEFAULT_MAX_NATIVE_REVIEW_INLINE_COMMENTS
    fresh_analysis_context_loader: Callable[[], V6FreshAnalysisContext] | None = None


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
    result_parts: tuple[ResultPartRecord, ...] = ()
    result_manifests: tuple[ResultManifestRecord, ...] = ()
    batch_plans: tuple[BatchPlanRecord, ...] = ()
    batch_plan_parts: tuple[BatchPlanPartRecord, ...] = ()
    batch_plan_manifests: tuple[BatchPlanManifestRecord, ...] = ()
    batch_dispatches: tuple[BatchDispatchRecord, ...] = ()
    batch_outputs: tuple[BatchOutputRecord, ...] = ()


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


_LEDGER_MARKER_PREFIXES: tuple[str, ...] = (
    "<!-- reviewsensei:v6:intent",
    "<!-- reviewsensei:v6:receipt",
    "<!-- reviewsensei:v6:owner",
    "<!-- reviewsensei:v6:hold",
    "<!-- reviewsensei:v6:result",
    "<!-- reviewsensei:v6:batch-",
    "<!-- reviewsensei:v6:non-submission",
    "<!-- reviewsensei:v6:hint",
)


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
    if any(prefix in body for prefix in _LEDGER_MARKER_PREFIXES):
        return False
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


_REVIEW_ENVELOPE_VERSION = "reviewsensei:v6:review_envelope:v1"
_REVIEW_ENVELOPE_HINT_KEYS = frozenset(
    {"_v6_review_envelope", "review_body", "comments", "event"}
)


def _effective_binding_inputs(
    request: RecoveryRequest, *, pr_head_sha: str
) -> tuple[str, str, str, str, str]:
    base_sha = (
        request.base_sha
        or request.merge_base_sha
        or canonical_sha256(
            {
                "repository": f"{request.owner}/{request.repo}",
                "pull_request": request.pull_request,
                "head_sha": pr_head_sha,
            }
        )[:40]
    )
    merge_base_sha = request.merge_base_sha or base_sha
    inventory_sha256 = request.inventory_sha256 or canonical_sha256(
        {"head_sha": pr_head_sha, "base_sha": merge_base_sha}
    )
    default_policy_digest, default_config_digest = _default_trusted_digests()
    policy_digest = request.policy_digest or default_policy_digest
    configuration_digest = request.configuration_digest or default_config_digest
    return (
        base_sha,
        merge_base_sha,
        inventory_sha256,
        policy_digest,
        configuration_digest,
    )


def _request_has_explicit_inputs(request: RecoveryRequest) -> bool:
    return any(
        val is not None
        for val in (
            request.base_sha,
            request.merge_base_sha,
            request.inventory_sha256,
            request.policy_digest,
            request.configuration_digest,
        )
    )


def _request_input_digest(request: RecoveryRequest, *, pr_head_sha: str) -> str:
    base_sha, merge_base_sha, inventory_sha256, policy_digest, configuration_digest = (
        _effective_binding_inputs(request, pr_head_sha=pr_head_sha)
    )
    return canonical_sha256(
        {
            "base_sha": base_sha,
            "merge_base_sha": merge_base_sha,
            "head_sha": pr_head_sha,
            "inventory_sha256": inventory_sha256,
            "policy_digest": policy_digest,
            "configuration_digest": configuration_digest,
        }
    )[:12]


def _input_binding_tags(
    request: RecoveryRequest, *, pr_head_sha: str
) -> tuple[str, ...]:
    base_sha, merge_base_sha, inventory_sha256, policy_digest, configuration_digest = (
        _effective_binding_inputs(request, pr_head_sha=pr_head_sha)
    )
    return (
        f"v6:base:{base_sha}",
        f"v6:merge_base:{merge_base_sha}",
        f"v6:inventory:{inventory_sha256}",
        f"v6:policy:{policy_digest}",
        f"v6:config:{configuration_digest}",
    )


def _split_v6_binding_tags(
    digests: Sequence[str],
) -> tuple[dict[str, str], tuple[str, ...]]:
    binding_map: dict[str, str] = {}
    plain: list[str] = []
    for item in digests:
        if item.startswith("v6:"):
            parts = item.split(":", 2)
            if len(parts) == 3 and parts[1] in {
                "base",
                "merge_base",
                "inventory",
                "policy",
                "config",
            }:
                binding_map[parts[1]] = parts[2]
                continue
        plain.append(item)
    return binding_map, tuple(plain)


def _result_matches_request_inputs(
    request: RecoveryRequest,
    *,
    required_digests: Sequence[str],
    pr_head_sha: str,
) -> bool:
    stored_tags, _ = _split_v6_binding_tags(required_digests)
    if not stored_tags:
        return True
    base_sha, merge_base_sha, inventory_sha256, policy_digest, configuration_digest = (
        _effective_binding_inputs(request, pr_head_sha=pr_head_sha)
    )
    expected: dict[str, str] = {}
    if request.base_sha is not None:
        expected["base"] = base_sha
    if request.merge_base_sha is not None:
        expected["merge_base"] = merge_base_sha
    if request.inventory_sha256 is not None:
        expected["inventory"] = inventory_sha256
    if request.policy_digest is not None:
        expected["policy"] = policy_digest
    if request.configuration_digest is not None:
        expected["config"] = configuration_digest
    if not expected:
        # Even without explicit overrides, mismatched non-default stored config/policy must fail closed.
        expected = {
            "policy": policy_digest,
            "config": configuration_digest,
        }
    for key, expected_val in expected.items():
        if key in stored_tags and stored_tags[key] != expected_val:
            return False
    return True


def _evidence_binding_for_request(
    request: RecoveryRequest, *, pr_head_sha: str
) -> dict[str, Any]:
    producer = (
        f"github-bot:{request.expected_app_id}"
        if type(request.expected_app_id) is int and request.expected_app_id > 0
        else "local-ledger"
    )
    base_sha, _, _, policy_digest, configuration_digest = _effective_binding_inputs(
        request, pr_head_sha=pr_head_sha
    )
    return {
        "repository": f"{request.owner}/{request.repo}",
        "repository_id": request.repository_id,
        "pull_request": request.pull_request,
        "base_sha": base_sha,
        "head_sha": pr_head_sha,
        "policy_digest": policy_digest,
        "configuration_digest": configuration_digest,
        "generation": 0,
        "producer": producer,
        "purpose": "evidence",
        "schema_version": "1.0",
    }


def _partition_storage_id(idx: int, part_record_id: str) -> str:
    digest = hashlib.sha256(part_record_id.encode("utf-8")).hexdigest()[:48]
    return f"part_{idx:03d}_{digest}"


def _reconstruct_partitioned_result(
    *,
    request: RecoveryRequest,
    manifest_rec: ResultManifestRecord,
    result_parts: Sequence[ResultPartRecord],
    comment_ids_by_record_id: Mapping[str, int] | None,
) -> UnresolvedResultRecord:
    expected_binding = _evidence_binding_for_request(
        request, pr_head_sha=manifest_rec.pr_head_sha
    )
    parts_by_record_id = {
        part.record_id: part
        for part in result_parts
        if part.operation_id == manifest_rec.operation_id
        and part.pr_head_sha == manifest_rec.pr_head_sha
        and part.payload_sha256 == manifest_rec.payload_sha256
    }
    manifest_dict = _to_json_compatible(manifest_rec.manifest)
    manifest_parts = (
        manifest_dict.get("parts") if isinstance(manifest_dict, Mapping) else None
    )
    if not isinstance(manifest_parts, list) or len(manifest_parts) != len(
        manifest_rec.part_record_ids
    ):
        raise ReviewInputError(
            "partition manifest parts length mismatch",
            diagnostic="corrupted_pr_unresolved_result_digest",
        )

    storage_to_part: dict[str, ResultPartRecord] = {}
    for idx, (part_rec_id, expected_cid, manifest_part_ref) in enumerate(
        zip(
            manifest_rec.part_record_ids,
            manifest_rec.part_comment_ids,
            manifest_parts,
        )
    ):
        if not isinstance(manifest_part_ref, Mapping):
            raise ReviewInputError(
                "partition manifest entry is not a mapping",
                diagnostic="corrupted_pr_unresolved_result_digest",
            )
        storage_id = manifest_part_ref.get("storage_id")
        part_rec = parts_by_record_id.get(part_rec_id)
        if not isinstance(storage_id, str) or part_rec is None:
            raise ReviewInputError(
                f"missing result part {part_rec_id!r} referenced by manifest",
                diagnostic="corrupted_pr_unresolved_result_digest",
            )
        if part_rec.part_index != idx or part_rec.part_count != len(
            manifest_rec.part_record_ids
        ):
            raise ReviewInputError(
                f"result part {part_rec_id!r} index/count mismatch",
                diagnostic="corrupted_pr_unresolved_result_digest",
            )
        part_payload_plain = _to_json_compatible(part_rec.part_payload)
        if canonical_sha256(part_payload_plain) != part_rec.part_sha256:
            raise ReviewInputError(
                f"result part {part_rec_id!r} SHA-256 mismatch",
                diagnostic="corrupted_pr_unresolved_result_digest",
            )
        if comment_ids_by_record_id is not None:
            observed_cid = comment_ids_by_record_id.get(part_rec_id)
            if observed_cid is not None and observed_cid != expected_cid:
                raise ReviewInputError(
                    f"result part {part_rec_id!r} comment ID mismatch",
                    diagnostic="corrupted_pr_unresolved_result_digest",
                )
        storage_to_part[storage_id] = part_rec

    def _read_part(sid: str) -> AuthenticatedPart:
        part_rec = storage_to_part.get(sid)
        if part_rec is None:
            raise ReviewInputError(
                f"missing storage part {sid!r}",
                diagnostic="corrupted_pr_unresolved_result_digest",
            )
        return AuthenticatedPart(
            document=_to_json_compatible(part_rec.part_payload),
            producer=str(expected_binding["producer"]),
        )

    restored = read_partitioned_evidence(
        manifest_dict,
        reader=_read_part,
        expected_binding=expected_binding,
        budget=EvidenceReadBudget(),
    )
    if not isinstance(restored, Mapping) or not isinstance(
        restored.get("findings_payload"), list
    ):
        raise ReviewInputError(
            "reconstructed partitioned result missing findings_payload list",
            diagnostic="corrupted_pr_unresolved_result_digest",
        )
    restored_findings = tuple(dict(item) for item in restored["findings_payload"])
    if canonical_sha256(restored_findings) != manifest_rec.payload_sha256:
        raise ReviewInputError(
            "reconstructed partitioned findings digest mismatch",
            diagnostic="corrupted_pr_unresolved_result_digest",
        )
    return UnresolvedResultRecord(
        record_id=manifest_rec.record_id,
        operation_id=manifest_rec.operation_id,
        pr_head_sha=manifest_rec.pr_head_sha,
        payload_sha256=manifest_rec.payload_sha256,
        findings_payload=restored_findings,
        required_evidence_digests=manifest_rec.required_evidence_digests,
        artifact_cache_sha256=manifest_rec.artifact_cache_sha256,
    )


def _escaped_json_string_content_bytes(text: str) -> int:
    """Return UTF-8 byte length of ``text`` after JSON string and HTML-comment escaping."""

    escaped = (
        json.dumps(text, ensure_ascii=False, allow_nan=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )
    return len(escaped.encode("utf-8")) - 2


def _split_json_into_escaped_chunks(
    plan_json: str, *, max_escaped_bytes: int
) -> tuple[str, ...]:
    """Split ``plan_json`` deterministically into substrings bounded after escaping."""

    if max_escaped_bytes < 16:
        raise V6LedgerIntegrityError(
            "insufficient remaining marker budget for batch-plan part chunk",
            diagnostic="v6_marker_too_large",
        )
    if not plan_json:
        return ("",)
    chunks: list[str] = []
    start = 0
    total_len = len(plan_json)
    while start < total_len:
        hi = min(total_len, start + max_escaped_bytes)
        if _escaped_json_string_content_bytes(plan_json[start:hi]) <= max_escaped_bytes:
            best = hi
        else:
            lo = start + 1
            if (
                _escaped_json_string_content_bytes(plan_json[start:lo])
                > max_escaped_bytes
            ):
                raise V6LedgerIntegrityError(
                    "single character exceeded remaining batch-plan marker budget",
                    diagnostic="v6_marker_too_large",
                )
            best = lo
            while lo <= hi:
                mid = (lo + hi) // 2
                if (
                    _escaped_json_string_content_bytes(plan_json[start:mid])
                    <= max_escaped_bytes
                ):
                    best = mid
                    lo = mid + 1
                else:
                    hi = mid - 1
        chunks.append(plan_json[start:best])
        start = best
    return tuple(chunks)


def _split_batch_plan_into_parts(
    plan_rec: BatchPlanRecord,
    *,
    input_digest: str,
) -> tuple[str, tuple[BatchPlanPartRecord, ...], BatchPlanManifestRecord]:
    """Split an oversized ``BatchPlanRecord`` deterministically into bounded parts + manifest."""

    plan_dict = record_to_dict(plan_rec)
    plan_sha256 = canonical_sha256(plan_dict)
    plan_json = canonical_json_dumps(plan_dict)
    part_prefix = (
        f"batch-plan-part:{plan_rec.operation_id}:"
        f"{plan_rec.pr_head_sha[:12]}:{input_digest}"
    )
    probe_part = BatchPlanPartRecord(
        record_id=f"{part_prefix}:999999",
        operation_id=plan_rec.operation_id,
        pr_head_sha=plan_rec.pr_head_sha,
        plan_sha256=plan_sha256,
        part_index=999_998,
        total_parts=999_999,
        part_sha256="0" * 64,
        part_payload={"chunk": ""},
    )
    overhead_bytes = len(format_v6_record_marker(probe_part).encode("utf-8"))
    max_escaped_chunk_bytes = MAX_MARKER_ENCODED_BYTES - overhead_bytes - 16
    chunks = _split_json_into_escaped_chunks(
        plan_json, max_escaped_bytes=max_escaped_chunk_bytes
    )
    total_parts = len(chunks)

    parts: list[BatchPlanPartRecord] = []
    for idx, chunk in enumerate(chunks):
        part_payload = {"chunk": chunk}
        part_sha256 = canonical_sha256(part_payload)
        part_rec = BatchPlanPartRecord(
            record_id=f"{part_prefix}:{idx:04d}",
            operation_id=plan_rec.operation_id,
            pr_head_sha=plan_rec.pr_head_sha,
            plan_sha256=plan_sha256,
            part_index=idx,
            total_parts=total_parts,
            part_sha256=part_sha256,
            part_payload=part_payload,
        )
        format_v6_record_marker(part_rec)
        parts.append(part_rec)

    manifest_rec = BatchPlanManifestRecord(
        record_id=(
            f"batch-plan-manifest:{plan_rec.operation_id}:"
            f"{plan_rec.pr_head_sha[:12]}:{input_digest}"
        ),
        operation_id=plan_rec.operation_id,
        base_sha=plan_rec.base_sha,
        merge_base_sha=plan_rec.merge_base_sha,
        pr_head_sha=plan_rec.pr_head_sha,
        inventory_sha256=plan_rec.inventory_sha256,
        policy_digest=plan_rec.policy_digest,
        configuration_digest=plan_rec.configuration_digest,
        max_provider_calls=plan_rec.max_provider_calls,
        plan_sha256=plan_sha256,
        total_parts=total_parts,
        part_record_ids=tuple(part.record_id for part in parts),
        part_sha256s=tuple(part.part_sha256 for part in parts),
        batch_count=len(plan_rec.batches),
    )
    format_v6_record_marker(manifest_rec)
    return plan_sha256, tuple(parts), manifest_rec


def _reconstruct_split_batch_plan(
    *,
    manifest_rec: BatchPlanManifestRecord,
    plan_parts: Sequence[BatchPlanPartRecord],
) -> BatchPlanRecord:
    """Verify all referenced ``BatchPlanPartRecord``s and reconstruct ``BatchPlanRecord``."""

    expected_rids = set(manifest_rec.part_record_ids)
    parts_by_record_id: dict[str, BatchPlanPartRecord] = {}
    for part in plan_parts:
        if part.operation_id != manifest_rec.operation_id:
            continue
        if part.record_id not in expected_rids:
            continue
        existing = parts_by_record_id.get(part.record_id)
        if existing is not None and existing != part:
            raise ReviewInputError(
                f"conflicting batch plan part {part.record_id!r}",
                diagnostic="corrupted_pr_batch_plan",
            )
        parts_by_record_id[part.record_id] = part

    chunks: list[str] = []
    for idx, (expected_rid, expected_sha) in enumerate(
        zip(manifest_rec.part_record_ids, manifest_rec.part_sha256s)
    ):
        part_rec = parts_by_record_id.get(expected_rid)
        if part_rec is None:
            raise ReviewInputError(
                f"missing batch plan part {expected_rid!r} referenced by manifest",
                diagnostic="incomplete_partitioned_batch_plan_missing_part",
            )
        if (
            part_rec.pr_head_sha != manifest_rec.pr_head_sha
            or part_rec.plan_sha256 != manifest_rec.plan_sha256
            or part_rec.part_index != idx
            or part_rec.total_parts != manifest_rec.total_parts
            or part_rec.part_sha256 != expected_sha
        ):
            raise ReviewInputError(
                f"batch plan part {expected_rid!r} metadata or digest mismatch",
                diagnostic="corrupted_pr_batch_plan",
            )
        part_payload_plain = _to_json_compatible(part_rec.part_payload)
        if canonical_sha256(part_payload_plain) != expected_sha:
            raise ReviewInputError(
                f"batch plan part {expected_rid!r} payload SHA-256 mismatch",
                diagnostic="corrupted_pr_batch_plan_digest",
            )
        chunk = part_rec.part_payload.get("chunk")
        if type(chunk) is not str or set(part_rec.part_payload.keys()) != {"chunk"}:
            raise ReviewInputError(
                f"batch plan part {expected_rid!r} has invalid chunk payload",
                diagnostic="corrupted_pr_batch_plan",
            )
        chunks.append(chunk)

    reassembled_json = "".join(chunks)
    try:
        parsed = json.loads(reassembled_json, parse_constant=_reject_json_constant)
    except ValueError as error:
        raise ReviewInputError(
            f"reassembled batch plan JSON is malformed: {error}",
            diagnostic="corrupted_pr_batch_plan",
        ) from error
    if not isinstance(parsed, Mapping):
        raise ReviewInputError(
            "reassembled batch plan must decode to a JSON object",
            diagnostic="corrupted_pr_batch_plan",
        )
    if canonical_sha256(parsed) != manifest_rec.plan_sha256:
        raise ReviewInputError(
            "reassembled batch plan SHA-256 mismatch",
            diagnostic="corrupted_pr_batch_plan_digest",
        )
    reconstructed = _record_from_mapping(parsed)
    if not isinstance(reconstructed, BatchPlanRecord):
        raise ReviewInputError(
            "reassembled batch plan decoded to unexpected record type",
            diagnostic="corrupted_pr_batch_plan",
        )
    if canonical_sha256(record_to_dict(reconstructed)) != manifest_rec.plan_sha256:
        raise ReviewInputError(
            "reconstructed BatchPlanRecord SHA-256 mismatch",
            diagnostic="corrupted_pr_batch_plan_digest",
        )
    if (
        reconstructed.operation_id != manifest_rec.operation_id
        or reconstructed.base_sha != manifest_rec.base_sha
        or reconstructed.merge_base_sha != manifest_rec.merge_base_sha
        or reconstructed.pr_head_sha != manifest_rec.pr_head_sha
        or reconstructed.inventory_sha256 != manifest_rec.inventory_sha256
        or reconstructed.policy_digest != manifest_rec.policy_digest
        or reconstructed.configuration_digest != manifest_rec.configuration_digest
        or reconstructed.max_provider_calls != manifest_rec.max_provider_calls
        or len(reconstructed.batches) != manifest_rec.batch_count
    ):
        raise ReviewInputError(
            "reconstructed BatchPlanRecord does not match manifest bindings",
            diagnostic="corrupted_pr_batch_plan",
        )
    return reconstructed


@dataclass(frozen=True)
class _PlannedDetailComment:
    """Single bounded, deterministically addressable PR issue detail comment."""

    index: int
    operation_id: str
    payload_sha256: str
    content_markdown: str
    posted_body: str
    effect_path: str


@dataclass(frozen=True)
class _PlannedDelivery:
    """Preflighted delivery plan: K>=0 detail comments + 1 bounded native review."""

    detail_comments: tuple[_PlannedDetailComment, ...]
    native_lead_body: str
    native_inline_comments: tuple[Mapping[str, Any], ...]
    payload_sha256: str


def _utf8_split_chunks(text: str, max_bytes: int) -> list[str]:
    """Split ``text`` losslessly into UTF-8 chunks each at most ``max_bytes``."""

    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if not text:
        return []
    encoded_total = len(text.encode("utf-8"))
    if encoded_total <= max_bytes:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        raw = remaining.encode("utf-8")
        if len(raw) <= max_bytes:
            chunks.append(remaining)
            break
        prefix_bytes = raw[:max_bytes]
        cut_prefix = prefix_bytes.decode("utf-8", errors="ignore")
        if not cut_prefix:
            # Ensure forward progress even if a single codepoint somehow exceeds limit.
            cut_prefix = remaining[0]
        # Prefer splitting cleanly at a newline inside the last half of the chunk.
        newline_pos = cut_prefix.rfind("\n")
        if newline_pos > len(cut_prefix) // 2:
            split_at = newline_pos + 1
        else:
            split_at = len(cut_prefix)
        chunk = remaining[:split_at]
        if not chunk:
            chunk = cut_prefix
            split_at = len(chunk)
        chunks.append(chunk)
        remaining = remaining[split_at:]
    return chunks


def _extract_detail_comment_url(
    payload: Mapping[str, Any] | None,
    *,
    owner: str,
    repo: str,
    pull_request: int,
    comment_id: str | int,
) -> str:
    if isinstance(payload, Mapping):
        raw_url = payload.get("html_url")
        if isinstance(raw_url, str) and raw_url.strip():
            return raw_url.strip()
    return f"https://github.com/{owner}/{repo}/pull/{pull_request}#issuecomment-{comment_id}"


def _format_overflow_finding_section(
    idx: int,
    comment: Mapping[str, Any],
) -> str:
    path = str(comment.get("path") or "<general>")
    line = comment.get("line")
    side = comment.get("side")
    loc = f"`{path}:{line}`" if isinstance(line, int) and line > 0 else f"`{path}`"
    if isinstance(side, str) and side:
        loc = f"{loc} ({side})"
    body = str(comment.get("body") or "").strip()
    return f"### Finding {idx + 1} — {loc}\n\n{body}\n"


def _extract_review_components(
    request: RecoveryRequest,
    *,
    pr_result: UnresolvedResultRecord,
) -> tuple[str, list[dict[str, Any]]]:
    findings = pr_result.findings_payload
    has_envelope_hints = any(
        bool(_REVIEW_ENVELOPE_HINT_KEYS.intersection(entry.keys()))
        for entry in findings
    )
    if has_envelope_hints:
        if len(findings) != 1:
            raise ReviewInputError(
                "review envelope findings_payload must contain exactly one envelope mapping",
                diagnostic="v6_invalid_review_envelope",
            )
        envelope = findings[0]
        if (
            envelope.get("_v6_review_envelope") != _REVIEW_ENVELOPE_VERSION
            or not isinstance(envelope.get("review_body"), str)
            or not isinstance(envelope.get("comments"), Sequence)
            or isinstance(envelope.get("comments"), (str, bytes, bytearray))
        ):
            raise ReviewInputError(
                "malformed v6 review envelope",
                diagnostic="v6_invalid_review_envelope",
            )
        raw_comments = envelope["comments"]
        if not all(isinstance(c, Mapping) for c in raw_comments):
            raise ReviewInputError(
                "review envelope comments must be JSON objects",
                diagnostic="v6_invalid_review_envelope",
            )
        raw_event = envelope.get("event")
        if raw_event is not None and raw_event != "COMMENT":
            raise ReviewInputError(
                f"v6 pilot publisher forbids non-COMMENT review event {raw_event!r}",
                diagnostic="v6_unsupported_review_event",
            )
        return (
            str(envelope["review_body"]).rstrip(),
            [dict(c) for c in raw_comments],
        )

    return (
        f"ReviewSensei recovery publication ({len(findings)} finding(s)).",
        [dict(entry) for entry in findings],
    )


def _build_detail_comment_bodies(
    sections: Sequence[str],
    *,
    max_content_bytes: int,
) -> list[str]:
    """Pack markdown sections into detail comments <= ``max_content_bytes`` losslessly."""

    if not sections:
        return []
    items: list[str] = []
    for section in sections:
        if len(section.encode("utf-8")) <= max_content_bytes:
            items.append(section)
            continue
        sub_chunks = _utf8_split_chunks(section, max(512, max_content_bytes - 128))
        for part_idx, sub in enumerate(sub_chunks):
            items.append(
                f"_(Continued chunk {part_idx + 1}/{len(sub_chunks)})_\n\n{sub.rstrip()}\n"
            )

    grouped: list[str] = []
    current_parts: list[str] = []
    current_bytes = 0
    for item in items:
        sep = "\n\n---\n\n" if current_parts else ""
        candidate_bytes = len((sep + item).encode("utf-8"))
        if current_parts and current_bytes + candidate_bytes > max_content_bytes:
            grouped.append("\n\n---\n\n".join(current_parts).rstrip())
            current_parts = [item]
            current_bytes = len(item.encode("utf-8"))
        else:
            current_parts.append(item)
            current_bytes += candidate_bytes
    if current_parts:
        grouped.append("\n\n---\n\n".join(current_parts).rstrip())
    return grouped


def _render_native_review_with_detail_links(
    *,
    request: RecoveryRequest,
    lead_body: str,
    inline_comments: Sequence[Mapping[str, Any]],
    payload_sha256: str,
    detail_links: Sequence[tuple[int, str]],
) -> dict[str, Any]:
    effect_tag = _effect_tag(request.operation_id, payload_sha256)
    parts: list[str] = []
    cleaned_lead = lead_body.rstrip()
    if cleaned_lead:
        parts.append(cleaned_lead)
    if detail_links:
        total = len(detail_links)
        link_lines = [
            f"- [Detail comment {idx + 1}/{total}]({url})" for idx, url in detail_links
        ]
        parts.append("### Detail Comments\n" + "\n".join(link_lines))
    joined = "\n\n".join(parts).rstrip()
    if effect_tag not in joined.splitlines():
        joined = f"{joined}\n\n{effect_tag}" if joined else effect_tag
    return {
        "commit_id": request.ownership_proof.pr_head_sha,
        "event": "COMMENT",
        "body": joined,
        "comments": [dict(c) for c in inline_comments],
    }


def _plan_review_and_detail_comments(
    request: RecoveryRequest,
    *,
    pr_result: UnresolvedResultRecord,
    payload_sha256: str,
) -> _PlannedDelivery:
    """Partition findings/body into K>=0 bounded detail comments + 1 bounded native review."""

    max_review_bytes = max(
        512,
        min(request.max_native_review_body_bytes, MAX_MARKER_ENCODED_BYTES),
    )
    max_inline = max(0, request.max_native_review_inline_comments)
    raw_review_body, raw_comments = _extract_review_components(
        request, pr_result=pr_result
    )

    inline_kept: list[dict[str, Any]] = []
    overflow_comments: list[tuple[int, dict[str, Any]]] = []
    for idx, comment in enumerate(raw_comments):
        c_body = comment.get("body")
        c_path = comment.get("path")
        c_line = comment.get("line")
        is_valid_inline = (
            isinstance(c_path, str)
            and bool(c_path.strip())
            and type(c_line) is int
            and c_line > 0
            and isinstance(c_body, str)
            and len(c_body.encode("utf-8")) <= 16_384
        )
        if is_valid_inline and len(inline_kept) < max_inline:
            inline_kept.append(comment)
        else:
            overflow_comments.append((idx, comment))

    effect_tag = _effect_tag(request.operation_id, payload_sha256)
    effect_tag_bytes = len(effect_tag.encode("utf-8")) + 4
    worst_link_len = len(
        f"- [Detail comment 999/999](https://github.com/{request.owner}/{request.repo}/pull/{request.pull_request}#issuecomment-999999999999)\n".encode(
            "utf-8"
        )
    )
    detail_effect_overhead = (
        len(_effect_tag(f"{request.operation_id}:detail:999", "0" * 64).encode("utf-8"))
        + 64
    )
    max_detail_content_bytes = max(
        1024, MAX_MARKER_ENCODED_BYTES - detail_effect_overhead
    )
    issue_comments_path = (
        f"/repos/{request.owner}/{request.repo}/issues/{request.pull_request}/comments"
    )
    head_sha = request.ownership_proof.pr_head_sha

    overflow_sections = [
        _format_overflow_finding_section(idx, c) for idx, c in overflow_comments
    ]

    # Converge lead body prefix + detail comments so final native review fits even with all links.
    estimated_detail_count = max(1, len(overflow_sections)) if overflow_sections else 0
    for _ in range(4):
        links_overhead = (
            len("\n\n### Detail Comments\n".encode("utf-8"))
            + (estimated_detail_count * worst_link_len)
            if estimated_detail_count > 0
            else 0
        )
        continuation_note = "\n\n_(Review output exceeded native review limits; full details continue in linked PR detail comment(s) below.)_"
        note_bytes = len(continuation_note.encode("utf-8"))
        lead_budget = max(256, max_review_bytes - effect_tag_bytes - links_overhead)

        detail_sections: list[str] = []
        raw_body_bytes = len(raw_review_body.encode("utf-8"))
        if raw_body_bytes <= lead_budget and (
            not overflow_sections or raw_body_bytes <= lead_budget
        ):
            lead_body = raw_review_body
            if overflow_sections and continuation_note not in lead_body:
                candidate = (
                    f"{lead_body}{continuation_note}"
                    if lead_body
                    else continuation_note.strip()
                )
                if len(candidate.encode("utf-8")) <= lead_budget:
                    lead_body = candidate
                else:
                    split_budget = max(128, lead_budget - note_bytes)
                    chunks = _utf8_split_chunks(raw_review_body, split_budget)
                    lead_body = (
                        f"{chunks[0].rstrip()}{continuation_note}"
                        if chunks
                        else continuation_note.strip()
                    )
                    remainder = "".join(chunks[1:]).lstrip()
                    if remainder:
                        detail_sections.append(
                            f"### Review Body (Continued)\n\n{remainder}\n"
                        )
        else:
            split_budget = max(128, lead_budget - note_bytes)
            chunks = _utf8_split_chunks(raw_review_body, split_budget)
            lead_body = (
                f"{chunks[0].rstrip()}{continuation_note}"
                if chunks
                else continuation_note.strip()
            )
            remainder = "".join(chunks[1:]).lstrip()
            if remainder:
                detail_sections.append(f"### Review Body (Continued)\n\n{remainder}\n")

        detail_sections.extend(overflow_sections)
        grouped_contents = _build_detail_comment_bodies(
            detail_sections, max_content_bytes=max_detail_content_bytes
        )
        if len(grouped_contents) <= estimated_detail_count:
            break
        estimated_detail_count = len(grouped_contents)

    detail_records: list[_PlannedDetailComment] = []
    total_details = len(grouped_contents)
    for idx, content in enumerate(grouped_contents):
        header = f"## ReviewSensei Detail ({idx + 1}/{total_details})\n\n"
        full_content = f"{header}{content.rstrip()}"
        d_op_id = f"{request.operation_id}:detail:{idx}"
        canonical_detail = {
            "kind": "v6_detail_comment",
            "parent_operation_id": request.operation_id,
            "detail_index": idx,
            "detail_count": total_details,
            "pr_head_sha": head_sha,
            "content": full_content,
        }
        d_sha = canonical_sha256(canonical_detail)
        d_tag = _effect_tag(d_op_id, d_sha)
        posted_body = f"{full_content}\n\n{d_tag}"
        if len(posted_body.encode("utf-8")) > MAX_MARKER_ENCODED_BYTES:
            raise ReviewInputError(
                f"detail comment {idx} exceeded 65536 byte ceiling after splitting",
                diagnostic="v6_detail_comment_too_large",
            )
        detail_records.append(
            _PlannedDetailComment(
                index=idx,
                operation_id=d_op_id,
                payload_sha256=d_sha,
                content_markdown=full_content,
                posted_body=posted_body,
                effect_path=issue_comments_path,
            )
        )

    # Verify worst-case native review payload strictly respects bounds upfront.
    worst_links = [
        (
            d.index,
            f"https://github.com/{request.owner}/{request.repo}/pull/{request.pull_request}#issuecomment-999999999999",
        )
        for d in detail_records
    ]
    worst_payload = _render_native_review_with_detail_links(
        request=request,
        lead_body=lead_body,
        inline_comments=inline_kept,
        payload_sha256=payload_sha256,
        detail_links=worst_links,
    )
    if (
        len(str(worst_payload["body"]).encode("utf-8"))
        > request.max_native_review_body_bytes
    ):
        raise ReviewInputError(
            "bounded native COMMENT review body exceeded max_native_review_body_bytes",
            diagnostic="v6_native_review_too_large",
        )
    if len(inline_kept) > request.max_native_review_inline_comments:
        raise ReviewInputError(
            "bounded native COMMENT review exceeded max_native_review_inline_comments",
            diagnostic="v6_native_review_too_many_comments",
        )

    return _PlannedDelivery(
        detail_comments=tuple(detail_records),
        native_lead_body=lead_body,
        native_inline_comments=tuple(inline_kept),
        payload_sha256=payload_sha256,
    )


def _build_review_effect_payload(
    request: RecoveryRequest,
    *,
    pr_result: UnresolvedResultRecord,
    payload_sha256: str,
    detail_links: Sequence[tuple[int, str]] = (),
) -> dict[str, Any]:
    """Validate result payload strictly (comment-only) and build bounded GitHub review body."""

    planned = _plan_review_and_detail_comments(
        request,
        pr_result=pr_result,
        payload_sha256=payload_sha256,
    )
    effective_links = detail_links
    if planned.detail_comments and not effective_links:
        effective_links = tuple(
            (
                d.index,
                _extract_detail_comment_url(
                    None,
                    owner=request.owner,
                    repo=request.repo,
                    pull_request=request.pull_request,
                    comment_id=0,
                ),
            )
            for d in planned.detail_comments
        )
    return _render_native_review_with_detail_links(
        request=request,
        lead_body=planned.native_lead_body,
        inline_comments=planned.native_inline_comments,
        payload_sha256=payload_sha256,
        detail_links=effective_links,
    )


def _definitive_rejection_diagnostic(status: int, body: Any = None) -> str:
    if status == 413:
        return "github_native_review_size_refused"
    if status == 422 and body is not None:
        text = str(body).lower()
        if any(
            token in text
            for token in (
                "too large",
                "exceed",
                "size",
                "limit",
                "length",
                "characters",
                "bytes",
                "maximum",
            )
        ):
            return "github_native_review_size_refused"
    return f"definitive_github_rejection_{status}"


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
        result_parts: list[ResultPartRecord] = []
        result_manifests: list[ResultManifestRecord] = []
        batch_plans: list[BatchPlanRecord] = []
        batch_plan_parts: list[BatchPlanPartRecord] = []
        batch_plan_manifests: list[BatchPlanManifestRecord] = []
        batch_dispatches: list[BatchDispatchRecord] = []
        batch_outputs: list[BatchOutputRecord] = []
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
                elif isinstance(rec, ResultPartRecord):
                    result_parts.append(rec)
                elif isinstance(rec, ResultManifestRecord):
                    result_manifests.append(rec)
                elif isinstance(rec, BatchPlanRecord):
                    batch_plans.append(rec)
                elif isinstance(rec, BatchPlanPartRecord):
                    batch_plan_parts.append(rec)
                elif isinstance(rec, BatchPlanManifestRecord):
                    batch_plan_manifests.append(rec)
                elif isinstance(rec, BatchDispatchRecord):
                    batch_dispatches.append(rec)
                elif isinstance(rec, BatchOutputRecord):
                    batch_outputs.append(rec)
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
            result_parts=tuple(result_parts),
            result_manifests=tuple(result_manifests),
            batch_plans=tuple(batch_plans),
            batch_plan_parts=tuple(batch_plan_parts),
            batch_plan_manifests=tuple(batch_plan_manifests),
            batch_dispatches=tuple(batch_dispatches),
            batch_outputs=tuple(batch_outputs),
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
        operation_id: str | None = None,
        effect_kind: str | None = None,
    ) -> tuple[bool, bool, str | int | None]:
        """Return ``(outcome_known, landed, verified_effect_id)`` via broker."""

        target_op_id = operation_id or request.operation_id
        canonical_kind = _canonical_native_effect_kind(
            effect_kind or request.effect_kind
        )
        verify_path = (
            f"/repos/{request.owner}/{request.repo}/issues/comments/{candidate_effect_id}"
            if canonical_kind == "issue_comment"
            else (
                f"/repos/{request.owner}/{request.repo}/pulls/"
                f"{request.pull_request}/reviews/{candidate_effect_id}"
            )
        )
        try:
            resp = self._execute_with_throttle(
                request=request,
                guard=guard,
                prior_durable_hint=prior_hint,
                call_fn=lambda ts: self.client.execute(
                    "broker_effect_verification",
                    method="GET",
                    path=verify_path,
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
            operation_id=target_op_id,
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
        operation_id: str | None = None,
        effect_kind: str | None = None,
    ) -> tuple[bool, bool, str | int | None, int]:
        """Determine whether the target mutation landed without duplicate writes."""

        target_op_id = operation_id or request.operation_id
        effective_kind = effect_kind or request.effect_kind
        canonical_kind = _canonical_native_effect_kind(effective_kind)
        surfaces = scanned_reviews
        if rescan_before_verify:
            exec_fn = self._throttled_page_executor(
                request=request, guard=guard, prior_hint=prior_hint
            )
            if canonical_kind == "issue_comment":
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
                    phase_name="reconcile_issue_comments",
                    now_epoch=self.now_fn(),
                    execute_fn=exec_fn,
                )
            else:
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
            surfaces = rescan.records

        candidate_ids: list[str | int] = []
        for record in surfaces:
            if not isinstance(record.payload, Mapping):
                continue
            if _has_contradictory_effect_marker(
                record.payload,
                operation_id=target_op_id,
                payload_sha256=expected_payload_sha256,
                expected_head_sha=expected_head_sha,
                expected_app_id=request.expected_app_id,
                expected_bot_login=request.expected_bot_login,
                expected_bot_user_id=request.expected_bot_user_id,
            ):
                return False, False, None, 0
            if _matches_effect_payload(
                record.payload,
                operation_id=target_op_id,
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
            operation_id=target_op_id,
            effect_kind=effective_kind,
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
        operation_id: str | None = None,
    ) -> tuple[CompletionReceiptRecord, int, int | None]:
        target_op_id = operation_id or request.operation_id
        for existing in existing_receipts:
            if (
                existing.operation_id == target_op_id
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
            record_id=f"receipt:{target_op_id}:{request.ownership_proof.writer_epoch}",
            operation_id=target_op_id,
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
        operation_id: str | None = None,
    ) -> int | None:
        if (
            not request.has_active_write_lease
            or request.slot_key is None
            or request.generation is None
            or proof_kind not in VALID_NON_SUBMISSION_PROOF_KINDS
        ):
            return None
        target_op_id = operation_id or request.operation_id
        self._verify_write_boundary(
            request=request,
            guard=guard,
            prior_durable_hint=prior_hint,
        )
        proof_record = NonSubmissionProofRecord(
            record_id=(
                f"non-submission:{target_op_id}:{request.generation}:"
                f"{request.ownership_proof.writer_epoch}"
            ),
            slot_key=request.slot_key,
            generation=request.generation,
            operation_id=target_op_id,
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
        result_parts: Sequence[ResultPartRecord] = (),
        result_manifests: Sequence[ResultManifestRecord] = (),
        comment_ids_by_record_id: Mapping[str, int] | None = None,
    ) -> UnresolvedResultRecord | RecoveryHold:
        head_sha = request.ownership_proof.pr_head_sha
        all_for_head = [
            rec
            for rec in unresolved_results
            if rec.operation_id == request.operation_id and rec.pr_head_sha == head_sha
        ]
        matching = [
            rec
            for rec in all_for_head
            if _result_matches_request_inputs(
                request,
                required_digests=rec.required_evidence_digests,
                pr_head_sha=head_sha,
            )
        ]
        if _request_has_explicit_inputs(request):
            expected_suffix = f":{_request_input_digest(request, pr_head_sha=head_sha)}"
            scoped_matching = [
                rec for rec in matching if rec.record_id.endswith(expected_suffix)
            ]
            if scoped_matching:
                matching = scoped_matching

        stale_inputs_changed = bool(all_for_head) and not matching

        if matching:
            result = matching[-1]
            expected_digest = canonical_sha256(result.findings_payload)
            if result.payload_sha256 != expected_digest:
                return RecoveryHold(
                    operation_id=request.operation_id,
                    blocker="corrupted_pr_unresolved_result_digest",
                    missing_evidence=(f"payload_sha256:{result.payload_sha256}",),
                    zero_model_calls_started=True,
                )
        else:
            all_manifests_for_head = [
                rec
                for rec in result_manifests
                if rec.operation_id == request.operation_id
                and rec.pr_head_sha == head_sha
            ]
            expected_binding = _evidence_binding_for_request(
                request, pr_head_sha=head_sha
            )
            matching_manifests = [
                rec
                for rec in all_manifests_for_head
                if _result_matches_request_inputs(
                    request,
                    required_digests=rec.required_evidence_digests,
                    pr_head_sha=head_sha,
                )
                and (
                    not isinstance(rec.manifest.get("binding"), Mapping)
                    or (
                        rec.manifest["binding"].get("base_sha")
                        == expected_binding["base_sha"]
                        and rec.manifest["binding"].get("policy_digest")
                        == expected_binding["policy_digest"]
                        and rec.manifest["binding"].get("configuration_digest")
                        == expected_binding["configuration_digest"]
                    )
                )
            ]
            if all_manifests_for_head and not matching_manifests:
                stale_inputs_changed = True
            if matching_manifests:
                manifest_rec = matching_manifests[-1]
                try:
                    result = _reconstruct_partitioned_result(
                        request=request,
                        manifest_rec=manifest_rec,
                        result_parts=result_parts,
                        comment_ids_by_record_id=comment_ids_by_record_id,
                    )
                except ReviewInputError:
                    return RecoveryHold(
                        operation_id=request.operation_id,
                        blocker="corrupted_pr_unresolved_result_digest",
                        missing_evidence=(
                            f"result_manifest:{manifest_rec.record_id}:{manifest_rec.payload_sha256}",
                        ),
                        zero_model_calls_started=True,
                    )
            else:
                if stale_inputs_changed:
                    return RecoveryHold(
                        operation_id=request.operation_id,
                        blocker="stale_unpublished_result_inputs_changed",
                        missing_evidence=(
                            f"input_binding:{request.operation_id}:{head_sha}",
                        ),
                        zero_model_calls_started=True,
                    )
                orphan_parts = [
                    part
                    for part in result_parts
                    if part.operation_id == request.operation_id
                    and part.pr_head_sha == head_sha
                    and not part.record_id.startswith("batch-part:")
                ]
                if orphan_parts:
                    return RecoveryHold(
                        operation_id=request.operation_id,
                        blocker="incomplete_partitioned_result_missing_manifest",
                        missing_evidence=tuple(p.record_id for p in orphan_parts),
                        zero_model_calls_started=True,
                    )
                return RecoveryHold(
                    operation_id=request.operation_id,
                    blocker="missing_pr_unresolved_result",
                    missing_evidence=(
                        f"unresolved_result:{request.operation_id}:{head_sha}",
                    ),
                    zero_model_calls_started=True,
                )

        if request.observed_evidence_digests is not None:
            observed = set(request.observed_evidence_digests)
            _, plain_required = _split_v6_binding_tags(result.required_evidence_digests)
            missing = tuple(
                digest for digest in plain_required if digest not in observed
            )
            if missing:
                return RecoveryHold(
                    operation_id=request.operation_id,
                    blocker="missing_required_pr_evidence",
                    missing_evidence=missing,
                    zero_model_calls_started=True,
                )
        return result

    def _post_ledger_marker_with_rescan(
        self,
        *,
        request: RecoveryRequest,
        guard: NoProgressGuard,
        prior_hint: AdvisoryProgressHint | None,
        record: V6LedgerRecord,
        effect_kind_label: str,
        find_existing_cid: Callable[[_CollectedLedger], int | None],
    ) -> int:
        marker_body = format_v6_record_marker(record)
        self._verify_write_boundary(
            request=request,
            guard=guard,
            prior_durable_hint=prior_hint,
        )
        self.mutation_bound.consume("write_intent_comment", effect_kind_label)
        try:
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
                    body={"body": marker_body},
                    now_epoch=ts,
                ),
            )
            eff_id = verify_write_response_readback(resp, expected_marker=record)
            if type(eff_id) is int and eff_id > 0:
                return eff_id
            raise _AmbiguousWriteOutcomeError(
                "ledger comment response missing positive integer id"
            )
        except (
            _AmbiguousWriteOutcomeError,
            TimeoutError,
            ConnectionError,
            GitHubHTTPTransientError,
        ) as exc:
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
                phase_name="rescan_result_parts",
                now_epoch=self.now_fn(),
                execute_fn=exec_fn,
            )
            rescanned = self._collect_full_ledger(
                rescan.records,
                expected_app_id=request.expected_app_id,
                expected_bot_login=request.expected_bot_login,
                expected_bot_user_id=request.expected_bot_user_id,
            )
            found_cid = find_existing_cid(rescanned)
            if found_cid is not None:
                return found_cid
            raise _AmbiguousWriteOutcomeError(
                f"ledger marker write unconfirmed for {effect_kind_label}"
            ) from exc

    def _persist_initial_result(
        self,
        *,
        request: RecoveryRequest,
        guard: NoProgressGuard,
        prior_hint: AdvisoryProgressHint | None,
        findings_tuple: tuple[dict[str, Any], ...],
        raw_digests: Sequence[str],
        collected: _CollectedLedger,
    ) -> tuple[
        tuple[UnresolvedResultRecord, ...],
        tuple[ResultPartRecord, ...],
        tuple[ResultManifestRecord, ...],
        dict[str, int],
    ]:
        head_sha = request.ownership_proof.pr_head_sha
        payload_sha256 = canonical_sha256(findings_tuple)
        has_explicit_inputs = _request_has_explicit_inputs(request)
        id_suffix = (
            f":{_request_input_digest(request, pr_head_sha=head_sha)}"
            if has_explicit_inputs
            else ""
        )
        effective_digests: list[str] = list(raw_digests)
        if has_explicit_inputs:
            for tag in _input_binding_tags(request, pr_head_sha=head_sha):
                if tag not in effective_digests:
                    effective_digests.append(tag)

        target_result_record_id = (
            f"result:{request.operation_id}:{head_sha[:12]}{id_suffix}"
        )
        inline_rec = UnresolvedResultRecord(
            record_id=target_result_record_id,
            operation_id=request.operation_id,
            pr_head_sha=head_sha,
            payload_sha256=payload_sha256,
            findings_payload=findings_tuple,
            required_evidence_digests=tuple(effective_digests),
        )
        fits_inline = False
        inline_marker = ""
        try:
            inline_marker = format_v6_record_marker(inline_rec)
            fits_inline = len(inline_marker.encode("utf-8")) <= MAX_MARKER_ENCODED_BYTES
        except V6LedgerIntegrityError as exc:
            if exc.diagnostic != "v6_marker_too_large":
                raise
            fits_inline = False

        updated_cids = dict(collected.comment_ids_by_record_id)
        if fits_inline:

            def _find_inline_cid(ledger: _CollectedLedger) -> int | None:
                for cand in ledger.unresolved_results:
                    if (
                        cand.record_id == inline_rec.record_id
                        and cand.operation_id == inline_rec.operation_id
                        and cand.pr_head_sha == inline_rec.pr_head_sha
                        and cand.payload_sha256 == inline_rec.payload_sha256
                    ):
                        return ledger.comment_ids_by_record_id.get(cand.record_id)
                return None

            cid = self._post_ledger_marker_with_rescan(
                request=request,
                guard=guard,
                prior_hint=prior_hint,
                record=inline_rec,
                effect_kind_label="unresolved_result",
                find_existing_cid=_find_inline_cid,
            )
            if type(cid) is int and cid > 0:
                updated_cids[inline_rec.record_id] = cid
            return (
                (*collected.unresolved_results, inline_rec),
                collected.result_parts,
                collected.result_manifests,
                updated_cids,
            )

        # Inline exceeded 64 KiB marker bound: partition losslessly into result-part:v1 + manifest-last.
        binding = _evidence_binding_for_request(request, pr_head_sha=head_sha)
        envelope_value = {"findings_payload": _to_json_compatible(findings_tuple)}
        raw_parts = partition_evidence(
            envelope_value,
            binding=binding,
            item_count=len(findings_tuple),
        )
        existing_parts_by_idx: dict[int, tuple[ResultPartRecord, int]] = {}
        for existing_part in collected.result_parts:
            if (
                existing_part.operation_id == request.operation_id
                and existing_part.pr_head_sha == head_sha
                and existing_part.payload_sha256 == payload_sha256
                and existing_part.part_count == len(raw_parts)
                and (not id_suffix or id_suffix in existing_part.record_id)
            ):
                ecid = updated_cids.get(existing_part.record_id)
                if type(ecid) is int and ecid > 0:
                    existing_parts_by_idx[existing_part.part_index] = (
                        existing_part,
                        ecid,
                    )

        # Preflight the COMPLETE multipart write plan before posting part 0.
        candidate_parts: list[ResultPartRecord] = []
        storage_ids: list[str] = []
        pending_part_writes = 0
        for idx, part_dict in enumerate(raw_parts):
            part_record_id = f"result-part:{request.operation_id}:{head_sha[:12]}{id_suffix}:{idx:03d}"
            storage_ids.append(_partition_storage_id(idx, part_record_id))
            part_sha = canonical_sha256(part_dict)
            part_rec = ResultPartRecord(
                record_id=part_record_id,
                operation_id=request.operation_id,
                pr_head_sha=head_sha,
                payload_sha256=payload_sha256,
                part_index=idx,
                part_count=len(raw_parts),
                part_sha256=part_sha,
                part_payload=part_dict,
            )
            format_v6_record_marker(part_rec)
            reused = existing_parts_by_idx.get(idx)
            if reused is None or reused[0].part_sha256 != part_sha:
                pending_part_writes += 1
            candidate_parts.append(part_rec)

        manifest_dict = evidence_manifest(
            envelope_value,
            binding=binding,
            item_count=len(findings_tuple),
            parts=raw_parts,
            storage_ids=tuple(storage_ids),
        )
        manifest_record_id = (
            f"result-manifest:{request.operation_id}:{head_sha[:12]}{id_suffix}"
        )
        prospective_manifest = ResultManifestRecord(
            record_id=manifest_record_id,
            operation_id=request.operation_id,
            pr_head_sha=head_sha,
            payload_sha256=payload_sha256,
            part_record_ids=tuple(p.record_id for p in candidate_parts),
            part_comment_ids=tuple(999_999_999_999 for _ in candidate_parts),
            manifest=manifest_dict,
            required_evidence_digests=tuple(effective_digests),
        )
        format_v6_record_marker(prospective_manifest)
        planned_multipart_writes = pending_part_writes + 1
        if (
            self.mutation_bound.writes_attempted + planned_multipart_writes
            > self.mutation_bound.max_writes
        ):
            raise MutationSanityBoundError(
                f"mutation sanity bound insufficient for complete multipart write plan "
                f"({self.mutation_bound.writes_attempted}+{planned_multipart_writes}/"
                f"{self.mutation_bound.max_writes})",
                diagnostic="v6_mutation_sanity_bound_exceeded",
            )

        posted_parts: list[ResultPartRecord] = []
        part_cids: list[int] = []
        for idx, part_rec in enumerate(candidate_parts):
            reused = existing_parts_by_idx.get(idx)
            if reused is not None and reused[0].part_sha256 == part_rec.part_sha256:
                posted_parts.append(reused[0])
                part_cids.append(reused[1])
                continue

            def _find_part_cid(
                ledger: _CollectedLedger,
                *,
                expected_rec: ResultPartRecord = part_rec,
            ) -> int | None:
                for cand in ledger.result_parts:
                    if (
                        cand.record_id == expected_rec.record_id
                        and cand.operation_id == expected_rec.operation_id
                        and cand.pr_head_sha == expected_rec.pr_head_sha
                        and cand.payload_sha256 == expected_rec.payload_sha256
                        and cand.part_sha256 == expected_rec.part_sha256
                    ):
                        return ledger.comment_ids_by_record_id.get(cand.record_id)
                return None

            part_cid = self._post_ledger_marker_with_rescan(
                request=request,
                guard=guard,
                prior_hint=prior_hint,
                record=part_rec,
                effect_kind_label="result_part",
                find_existing_cid=_find_part_cid,
            )
            updated_cids[part_rec.record_id] = part_cid
            posted_parts.append(part_rec)
            part_cids.append(part_cid)

        manifest_rec = ResultManifestRecord(
            record_id=manifest_record_id,
            operation_id=request.operation_id,
            pr_head_sha=head_sha,
            payload_sha256=payload_sha256,
            part_record_ids=tuple(p.record_id for p in posted_parts),
            part_comment_ids=tuple(part_cids),
            manifest=manifest_dict,
            required_evidence_digests=tuple(effective_digests),
        )

        def _find_manifest_cid(ledger: _CollectedLedger) -> int | None:
            for cand in ledger.result_manifests:
                if (
                    cand.record_id == manifest_rec.record_id
                    and cand.operation_id == manifest_rec.operation_id
                    and cand.pr_head_sha == manifest_rec.pr_head_sha
                    and cand.payload_sha256 == manifest_rec.payload_sha256
                ):
                    return ledger.comment_ids_by_record_id.get(cand.record_id)
            return None

        manifest_cid = self._post_ledger_marker_with_rescan(
            request=request,
            guard=guard,
            prior_hint=prior_hint,
            record=manifest_rec,
            effect_kind_label="result_manifest",
            find_existing_cid=_find_manifest_cid,
        )
        updated_cids[manifest_rec.record_id] = manifest_cid
        return (
            collected.unresolved_results,
            (*collected.result_parts, *posted_parts),
            (*collected.result_manifests, manifest_rec),
            updated_cids,
        )

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

    def _reconcile_unreconciled_detail_intents(
        self,
        *,
        request: RecoveryRequest,
        guard: NoProgressGuard,
        prior_hint: AdvisoryProgressHint | None,
        comment_records: Sequence[PaginatedRecord],
        intents: Sequence[WriteIntentRecord],
        receipts: Sequence[CompletionReceiptRecord],
        non_submissions: Sequence[NonSubmissionProofRecord],
        owners: Sequence[OwnerMarkerRecord],
        owner_cid: int | None,
        comment_ids_by_record_id: dict[str, int],
        summary: Any,
    ) -> tuple[
        tuple[CompletionReceiptRecord, ...],
        Any,
        int | None,
        int,
        RecoveryHold | None,
    ]:
        """Reconcile any unfinished ``:detail:*`` intents before new analysis or publication."""

        detail_prefix = f"{request.operation_id}:detail:"
        detail_unreconciled = [
            intent
            for intent in summary.unreconciled_intents
            if intent.operation_id.startswith(detail_prefix)
        ]
        if not detail_unreconciled:
            return tuple(receipts), summary, owner_cid, 0, None

        readback_gets = 0
        updated_receipts = list(receipts)
        by_op: dict[str, list[WriteIntentRecord]] = {}
        for intent in detail_unreconciled:
            by_op.setdefault(intent.operation_id, []).append(intent)

        for d_op_id, d_intents in by_op.items():
            latest = d_intents[-1]
            outcome_known, landed, verified_id, gets = self._reconcile_effect_surface(
                request=request,
                guard=guard,
                prior_hint=prior_hint,
                expected_payload_sha256=latest.payload_sha256,
                scanned_reviews=comment_records,
                expected_head_sha=latest.pr_head_sha,
                rescan_before_verify=False,
                operation_id=d_op_id,
                effect_kind=latest.effect_kind or "issue_comment",
            )
            readback_gets += gets
            if not outcome_known:
                return (
                    tuple(updated_receipts),
                    summary,
                    owner_cid,
                    readback_gets,
                    self._finalize_hold(
                        request=request,
                        guard=guard,
                        prior_hint=prior_hint,
                        blocker="ambiguous_write_outcome",
                        missing_evidence=(
                            f"effect_verification:{d_op_id}:{latest.payload_sha256}",
                        ),
                    ),
                )
            if landed and verified_id is not None:
                guard.record_step(
                    new_records_count=0,
                    reconciled_effect=True,
                    new_phase="reconcile_detail_landed",
                    state_digest=latest.payload_sha256,
                )
                if owner_cid is None:
                    owner_cid = self._persist_owner_marker_if_needed(
                        request=request,
                        guard=guard,
                        prior_hint=prior_hint,
                        existing_owners=owners,
                        comment_ids_by_record_id=comment_ids_by_record_id,
                    )
                try:
                    receipt, r_gets, r_cid = self._persist_completion_receipt(
                        request=request,
                        guard=guard,
                        prior_hint=prior_hint,
                        github_effect_id=verified_id,
                        payload_sha256=latest.payload_sha256,
                        existing_receipts=updated_receipts,
                        comment_ids_by_record_id=comment_ids_by_record_id,
                        operation_id=d_op_id,
                    )
                except _AmbiguousReceiptOutcomeError:
                    return (
                        tuple(updated_receipts),
                        summary,
                        owner_cid,
                        readback_gets,
                        self._finalize_hold(
                            request=request,
                            guard=guard,
                            prior_hint=prior_hint,
                            blocker="ambiguous_receipt_outcome",
                            missing_evidence=(
                                f"receipt_verification:{d_op_id}:{latest.payload_sha256}",
                            ),
                        ),
                    )
                readback_gets += r_gets
                updated_receipts.append(receipt)
                if r_cid is not None:
                    comment_ids_by_record_id[receipt.record_id] = r_cid
                continue

            has_non_sub = any(
                proof.operation_id == latest.operation_id
                and proof.payload_sha256 == latest.payload_sha256
                and proof.pr_head_sha == latest.pr_head_sha
                and proof.proof_kind in VALID_NON_SUBMISSION_PROOF_KINDS
                and (request.slot_key is None or proof.slot_key == request.slot_key)
                and (latest.generation is None or proof.generation == latest.generation)
                for proof in non_submissions
            )
            if not has_non_sub and not request.affirmative_non_submission:
                if request.has_active_write_lease:
                    self._verify_write_boundary(
                        request=request,
                        guard=guard,
                        prior_durable_hint=prior_hint,
                    )
                return (
                    tuple(updated_receipts),
                    summary,
                    owner_cid,
                    readback_gets,
                    self._finalize_hold(
                        request=request,
                        guard=guard,
                        prior_hint=prior_hint,
                        blocker="unproved_non_submission_after_intent",
                        missing_evidence=(
                            f"non_submission_proof:{d_op_id}:{latest.payload_sha256}",
                        ),
                    ),
                )

        new_summary = reconcile_immutable_records(intents, updated_receipts)
        return tuple(updated_receipts), new_summary, owner_cid, readback_gets, None

    def _ensure_pr_batch_plan(
        self,
        *,
        request: RecoveryRequest,
        context: V6FreshAnalysisContext,
        input_digest: str,
        guard: NoProgressGuard,
        prior_hint: AdvisoryProgressHint | None,
        collected: _CollectedLedger,
        comment_ids_by_record_id: dict[str, int],
    ) -> tuple[BatchPlanRecord | None, RecoveryHold | None]:
        """Load, reconstruct, or persist the PR-resident batch plan (inline or split)."""

        matching_plans = [
            p
            for p in collected.batch_plans
            if p.operation_id == request.operation_id
            and p.base_sha == context.base_sha
            and p.merge_base_sha == context.merge_base_sha
            and p.pr_head_sha == context.head_sha
            and p.inventory_sha256 == context.inventory_sha256
            and p.policy_digest == context.policy_digest
            and p.configuration_digest == context.configuration_digest
        ]
        matching_manifests = [
            m
            for m in collected.batch_plan_manifests
            if m.operation_id == request.operation_id
            and m.base_sha == context.base_sha
            and m.merge_base_sha == context.merge_base_sha
            and m.pr_head_sha == context.head_sha
            and m.inventory_sha256 == context.inventory_sha256
            and m.policy_digest == context.policy_digest
            and m.configuration_digest == context.configuration_digest
        ]
        if len({canonical_sha256(record_to_dict(p)) for p in matching_plans}) > 1:
            return (
                None,
                self._finalize_hold(
                    request=request,
                    guard=guard,
                    prior_hint=prior_hint,
                    blocker="corrupted_pr_batch_plan",
                    missing_evidence=tuple(
                        f"conflicting_batch_plan:{p.record_id}" for p in matching_plans
                    ),
                ),
            )
        if len({m.plan_sha256 for m in matching_manifests}) > 1:
            return (
                None,
                self._finalize_hold(
                    request=request,
                    guard=guard,
                    prior_hint=prior_hint,
                    blocker="corrupted_pr_batch_plan",
                    missing_evidence=tuple(
                        f"conflicting_batch_plan_manifest:{m.record_id}"
                        for m in matching_manifests
                    ),
                ),
            )
        if matching_plans and matching_manifests:
            plan_sha = canonical_sha256(record_to_dict(matching_plans[-1]))
            manifest_sha = matching_manifests[-1].plan_sha256
            if plan_sha != manifest_sha:
                return (
                    None,
                    self._finalize_hold(
                        request=request,
                        guard=guard,
                        prior_hint=prior_hint,
                        blocker="corrupted_pr_batch_plan",
                        missing_evidence=(
                            f"conflicting_batch_plan:{matching_plans[-1].record_id}",
                            f"conflicting_batch_plan_manifest:{matching_manifests[-1].record_id}",
                        ),
                    ),
                )
        if matching_plans:
            return matching_plans[-1], None

        if matching_manifests:
            manifest_rec = matching_manifests[-1]
            try:
                reconstructed = _reconstruct_split_batch_plan(
                    manifest_rec=manifest_rec,
                    plan_parts=collected.batch_plan_parts,
                )
            except ReviewInputError as exc:
                return (
                    None,
                    self._finalize_hold(
                        request=request,
                        guard=guard,
                        prior_hint=prior_hint,
                        blocker=exc.diagnostic or "corrupted_pr_batch_plan",
                        missing_evidence=(
                            f"batch_plan_manifest:{manifest_rec.record_id}",
                            str(exc),
                        ),
                    ),
                )
            return reconstructed, None

        plan_rec = BatchPlanRecord(
            record_id=(
                f"batch-plan:{request.operation_id}:"
                f"{context.head_sha[:12]}:{input_digest}"
            ),
            operation_id=request.operation_id,
            base_sha=context.base_sha,
            merge_base_sha=context.merge_base_sha,
            pr_head_sha=context.head_sha,
            inventory_sha256=context.inventory_sha256,
            policy_digest=context.policy_digest,
            configuration_digest=context.configuration_digest,
            max_provider_calls=context.max_provider_calls,
            batches=tuple(b.to_plan_dict() for b in context.planned_batches),
            non_text_changes=context.non_text_changes,
        )

        fits_inline = False
        try:
            inline_marker = format_v6_record_marker(plan_rec)
            fits_inline = len(inline_marker.encode("utf-8")) <= MAX_MARKER_ENCODED_BYTES
        except V6LedgerIntegrityError as exc:
            if exc.diagnostic != "v6_marker_too_large":
                raise
            fits_inline = False

        if fits_inline:

            def _find_plan_cid(ledger: _CollectedLedger) -> int | None:
                for cand in ledger.batch_plans:
                    if cand.record_id == plan_rec.record_id:
                        return ledger.comment_ids_by_record_id.get(cand.record_id)
                return None

            try:
                plan_cid = self._post_ledger_marker_with_rescan(
                    request=request,
                    guard=guard,
                    prior_hint=prior_hint,
                    record=plan_rec,
                    effect_kind_label="batch_plan",
                    find_existing_cid=_find_plan_cid,
                )
                comment_ids_by_record_id[plan_rec.record_id] = plan_cid
                return plan_rec, None
            except (
                _AmbiguousWriteOutcomeError,
                TimeoutError,
                ConnectionError,
                GitHubHTTPTransientError,
            ):
                return (
                    None,
                    self._finalize_hold(
                        request=request,
                        guard=guard,
                        prior_hint=prior_hint,
                        blocker="ambiguous_write_outcome",
                        missing_evidence=(f"batch_plan:{plan_rec.record_id}",),
                    ),
                )

        plan_sha256, candidate_parts, manifest_rec = _split_batch_plan_into_parts(
            plan_rec, input_digest=input_digest
        )
        expected_by_rid = {part.record_id: part for part in candidate_parts}
        reusable_parts_by_idx: dict[int, BatchPlanPartRecord] = {}
        for existing in collected.batch_plan_parts:
            if existing.operation_id != request.operation_id:
                continue
            if existing.record_id in expected_by_rid:
                expected = expected_by_rid[existing.record_id]
                if (
                    existing != expected
                    or canonical_sha256(_to_json_compatible(existing.part_payload))
                    != expected.part_sha256
                ):
                    return (
                        None,
                        self._finalize_hold(
                            request=request,
                            guard=guard,
                            prior_hint=prior_hint,
                            blocker="corrupted_pr_batch_plan_digest",
                            missing_evidence=(
                                f"batch_plan_part:{existing.record_id}:{existing.part_sha256}",
                            ),
                        ),
                    )
                reusable_parts_by_idx[expected.part_index] = existing
            elif (
                existing.pr_head_sha == context.head_sha
                and existing.plan_sha256 == plan_sha256
            ):
                if (
                    existing.part_index >= len(candidate_parts)
                    or existing.total_parts != len(candidate_parts)
                    or existing != candidate_parts[existing.part_index]
                    or canonical_sha256(_to_json_compatible(existing.part_payload))
                    != candidate_parts[existing.part_index].part_sha256
                ):
                    return (
                        None,
                        self._finalize_hold(
                            request=request,
                            guard=guard,
                            prior_hint=prior_hint,
                            blocker="corrupted_pr_batch_plan_digest",
                            missing_evidence=(
                                f"batch_plan_part:{existing.record_id}:{existing.part_sha256}",
                            ),
                        ),
                    )
                reusable_parts_by_idx[existing.part_index] = existing

        pending_part_writes = sum(
            1
            for part in candidate_parts
            if part.part_index not in reusable_parts_by_idx
        )
        planned_writes = pending_part_writes + 1
        if (
            self.mutation_bound.writes_attempted + planned_writes
            > self.mutation_bound.max_writes
        ):
            raise MutationSanityBoundError(
                f"mutation sanity bound insufficient for split batch plan "
                f"({self.mutation_bound.writes_attempted}+{planned_writes}/"
                f"{self.mutation_bound.max_writes})",
                diagnostic="v6_mutation_sanity_bound_exceeded",
            )

        persisted_parts: list[BatchPlanPartRecord] = []
        for part_rec in candidate_parts:
            reused = reusable_parts_by_idx.get(part_rec.part_index)
            if reused is not None:
                persisted_parts.append(reused)
                continue

            def _find_plan_part_cid(
                ledger: _CollectedLedger,
                *,
                expected_part: BatchPlanPartRecord = part_rec,
            ) -> int | None:
                for cand in ledger.batch_plan_parts:
                    if (
                        cand == expected_part
                        and canonical_sha256(_to_json_compatible(cand.part_payload))
                        == expected_part.part_sha256
                    ):
                        return ledger.comment_ids_by_record_id.get(cand.record_id)
                return None

            try:
                part_cid = self._post_ledger_marker_with_rescan(
                    request=request,
                    guard=guard,
                    prior_hint=prior_hint,
                    record=part_rec,
                    effect_kind_label="batch_plan_part",
                    find_existing_cid=_find_plan_part_cid,
                )
                comment_ids_by_record_id[part_rec.record_id] = part_cid
                persisted_parts.append(part_rec)
            except (
                _AmbiguousWriteOutcomeError,
                TimeoutError,
                ConnectionError,
                GitHubHTTPTransientError,
            ):
                return (
                    None,
                    self._finalize_hold(
                        request=request,
                        guard=guard,
                        prior_hint=prior_hint,
                        blocker="ambiguous_write_outcome",
                        missing_evidence=(f"batch_plan_part:{part_rec.record_id}",),
                    ),
                )

        def _find_plan_manifest_cid(ledger: _CollectedLedger) -> int | None:
            for cand in ledger.batch_plan_manifests:
                if cand == manifest_rec:
                    return ledger.comment_ids_by_record_id.get(cand.record_id)
            return None

        try:
            manifest_cid = self._post_ledger_marker_with_rescan(
                request=request,
                guard=guard,
                prior_hint=prior_hint,
                record=manifest_rec,
                effect_kind_label="batch_plan_manifest",
                find_existing_cid=_find_plan_manifest_cid,
            )
            comment_ids_by_record_id[manifest_rec.record_id] = manifest_cid
        except (
            _AmbiguousWriteOutcomeError,
            TimeoutError,
            ConnectionError,
            GitHubHTTPTransientError,
        ):
            return (
                None,
                self._finalize_hold(
                    request=request,
                    guard=guard,
                    prior_hint=prior_hint,
                    blocker="ambiguous_write_outcome",
                    missing_evidence=(f"batch_plan_manifest:{manifest_rec.record_id}",),
                ),
            )

        try:
            verified_plan = _reconstruct_split_batch_plan(
                manifest_rec=manifest_rec,
                plan_parts=persisted_parts,
            )
        except ReviewInputError as exc:
            return (
                None,
                self._finalize_hold(
                    request=request,
                    guard=guard,
                    prior_hint=prior_hint,
                    blocker=exc.diagnostic or "corrupted_pr_batch_plan",
                    missing_evidence=(
                        f"batch_plan_manifest:{manifest_rec.record_id}",
                        str(exc),
                    ),
                ),
            )
        return verified_plan, None

    def _run_restartable_batch_analysis(
        self,
        *,
        request: RecoveryRequest,
        guard: NoProgressGuard,
        prior_hint: AdvisoryProgressHint | None,
        collected: _CollectedLedger,
        owners: tuple[OwnerMarkerRecord, ...],
        owner_cid: int | None,
        unresolved_results: tuple[UnresolvedResultRecord, ...],
        result_parts: tuple[ResultPartRecord, ...],
        result_manifests: tuple[ResultManifestRecord, ...],
        comment_ids_by_record_id: dict[str, int],
    ) -> tuple[
        RecoveryRequest,
        UnresolvedResultRecord | None,
        int,
        tuple[OwnerMarkerRecord, ...],
        int | None,
        tuple[UnresolvedResultRecord, ...],
        tuple[ResultPartRecord, ...],
        tuple[ResultManifestRecord, ...],
        dict[str, int],
        RecoveryHold | None,
    ]:
        """Run or resume prompt-budgeted restartable batch analysis."""

        assert request.fresh_analysis_context_loader is not None
        try:
            context = request.fresh_analysis_context_loader()
        except V6FreshAnalysisError as exc:
            return (
                request,
                None,
                0,
                owners,
                owner_cid,
                unresolved_results,
                result_parts,
                result_manifests,
                comment_ids_by_record_id,
                self._finalize_hold(
                    request=request,
                    guard=guard,
                    prior_hint=prior_hint,
                    blocker=exc.diagnostic or "v6_fresh_analysis_failed",
                    missing_evidence=(str(exc),),
                ),
            )

        scoped_request = replace(
            request,
            base_sha=context.base_sha,
            merge_base_sha=context.merge_base_sha,
            inventory_sha256=context.inventory_sha256,
            policy_digest=context.policy_digest,
            configuration_digest=context.configuration_digest,
        )
        existing_validated = self._validate_pr_resident_result(
            request=scoped_request,
            unresolved_results=unresolved_results,
            result_parts=result_parts,
            result_manifests=result_manifests,
            comment_ids_by_record_id=comment_ids_by_record_id,
        )
        if isinstance(existing_validated, UnresolvedResultRecord):
            return (
                scoped_request,
                existing_validated,
                0,
                owners,
                owner_cid,
                unresolved_results,
                result_parts,
                result_manifests,
                comment_ids_by_record_id,
                None,
            )
        if isinstance(
            existing_validated, RecoveryHold
        ) and existing_validated.blocker in {
            "corrupted_pr_unresolved_result_digest",
            "incomplete_partitioned_result_missing_manifest",
            "missing_required_pr_evidence",
        }:
            return (
                scoped_request,
                None,
                0,
                owners,
                owner_cid,
                unresolved_results,
                result_parts,
                result_manifests,
                comment_ids_by_record_id,
                self._finalize_hold(
                    request=scoped_request,
                    guard=guard,
                    prior_hint=prior_hint,
                    blocker=existing_validated.blocker,
                    missing_evidence=existing_validated.missing_evidence,
                ),
            )

        if owner_cid is None:
            owner_cid = self._persist_owner_marker_if_needed(
                request=scoped_request,
                guard=guard,
                prior_hint=prior_hint,
                existing_owners=owners,
                comment_ids_by_record_id=comment_ids_by_record_id,
            )
            if (
                scoped_request.owner_marker is not None
                and scoped_request.owner_marker not in owners
            ):
                owners = (*owners, scoped_request.owner_marker)

        input_digest = _request_input_digest(
            scoped_request, pr_head_sha=context.head_sha
        )
        calls_started_this_run = 0
        batch_results_by_id: dict[str, Any] = {}

        if context.planned_batches:
            plan_rec, plan_hold = self._ensure_pr_batch_plan(
                request=scoped_request,
                context=context,
                input_digest=input_digest,
                guard=guard,
                prior_hint=prior_hint,
                collected=collected,
                comment_ids_by_record_id=comment_ids_by_record_id,
            )
            if plan_hold is not None:
                return (
                    scoped_request,
                    None,
                    calls_started_this_run,
                    owners,
                    owner_cid,
                    unresolved_results,
                    result_parts,
                    result_manifests,
                    comment_ids_by_record_id,
                    plan_hold,
                )
            if plan_rec is None:
                return (
                    scoped_request,
                    None,
                    calls_started_this_run,
                    owners,
                    owner_cid,
                    unresolved_results,
                    result_parts,
                    result_manifests,
                    comment_ids_by_record_id,
                    self._finalize_hold(
                        request=scoped_request,
                        guard=guard,
                        prior_hint=prior_hint,
                        blocker="corrupted_pr_batch_plan",
                        missing_evidence=(
                            f"batch_plan:{scoped_request.operation_id}:{context.head_sha[:12]}",
                        ),
                    ),
                )

            plan_sha256 = canonical_sha256(record_to_dict(plan_rec))
            planned_by_id = {b.batch_id: b for b in context.planned_batches}
            matching_dispatches = [
                d
                for d in collected.batch_dispatches
                if d.operation_id == scoped_request.operation_id
                and d.plan_sha256 == plan_sha256
            ]
            matching_outputs = [
                o
                for o in collected.batch_outputs
                if o.operation_id == scoped_request.operation_id
                and o.plan_sha256 == plan_sha256
            ]
            outputs_by_id: dict[str, BatchOutputRecord] = {}
            for out_rec in matching_outputs:
                if out_rec.batch_id in planned_by_id:
                    expected_hunks = set(
                        planned_by_id[out_rec.batch_id].primary_hunk_ids
                    )
                    if (
                        out_rec.review_status != "complete"
                        or set(out_rec.hunk_ids_reviewed) != expected_hunks
                    ):
                        return (
                            scoped_request,
                            None,
                            calls_started_this_run,
                            owners,
                            owner_cid,
                            unresolved_results,
                            result_parts,
                            result_manifests,
                            comment_ids_by_record_id,
                            self._finalize_hold(
                                request=scoped_request,
                                guard=guard,
                                prior_hint=prior_hint,
                                blocker="ambiguous_batch_inference_outcome",
                                missing_evidence=(
                                    f"batch_hunk_coverage:{out_rec.batch_id}",
                                ),
                            ),
                        )
                    outputs_by_id[out_rec.batch_id] = out_rec

            for dispatch in matching_dispatches:
                matched_out = outputs_by_id.get(dispatch.batch_id)
                if matched_out is None or matched_out.attempt < dispatch.attempt:
                    return (
                        scoped_request,
                        None,
                        calls_started_this_run,
                        owners,
                        owner_cid,
                        unresolved_results,
                        result_parts,
                        result_manifests,
                        comment_ids_by_record_id,
                        self._finalize_hold(
                            request=scoped_request,
                            guard=guard,
                            prior_hint=prior_hint,
                            blocker="ambiguous_batch_inference_outcome",
                            missing_evidence=(
                                f"batch_dispatch:{dispatch.batch_id}:attempt:{dispatch.attempt}",
                            ),
                        ),
                    )

            output_calls_by_key: dict[tuple[str, int], int] = {}
            for out_rec in matching_outputs:
                key = (out_rec.batch_id, out_rec.attempt)
                output_calls_by_key[key] = max(
                    output_calls_by_key.get(key, 1), out_rec.provider_calls
                )
            consumed_keys: set[tuple[str, int]] = {
                (d.batch_id, d.attempt) for d in matching_dispatches
            } | set(output_calls_by_key)
            used_provider_calls = sum(
                output_calls_by_key.get(k, 1) for k in consumed_keys
            )
            dispatch_gen = (
                scoped_request.generation
                if type(scoped_request.generation) is int
                and scoped_request.generation >= 1
                else max(1, scoped_request.ownership_proof.writer_epoch)
            )

            from review_sensei.models import ReviewComment, ReviewResult

            for b in context.planned_batches:
                existing_out = outputs_by_id.get(b.batch_id)
                if existing_out is not None:
                    loaded_findings: Sequence[Mapping[str, Any]] = existing_out.findings
                    loaded_summary = existing_out.summary
                    if not loaded_findings and loaded_summary.startswith(
                        "__partitioned__:"
                    ):
                        try:
                            _, exp_bsha, exp_count_s = loaded_summary.split(":", 2)
                            exp_count = int(exp_count_s)
                            bpart_prefix = (
                                f"batch-part:{scoped_request.operation_id}:"
                                f"{context.head_sha[:12]}:{input_digest}:{b.batch_id}:"
                            )
                            bparts = sorted(
                                (
                                    p
                                    for p in result_parts
                                    if p.operation_id == scoped_request.operation_id
                                    and p.pr_head_sha == context.head_sha
                                    and p.payload_sha256 == exp_bsha
                                    and p.record_id.startswith(bpart_prefix)
                                ),
                                key=lambda p: p.part_index,
                            )
                            if len(bparts) != exp_count or [
                                p.part_index for p in bparts
                            ] != list(range(exp_count)):
                                raise ReviewInputError("missing batch output part(s)")
                            b_binding = _evidence_binding_for_request(
                                scoped_request, pr_head_sha=context.head_sha
                            )
                            # Reassemble raw partitioned JSON parts directly via AuthenticatedPart.
                            part_map: dict[str, AuthenticatedPart] = {}
                            for idx_p, part_rec_b in enumerate(bparts):
                                if (
                                    canonical_sha256(part_rec_b.part_payload)
                                    != part_rec_b.part_sha256
                                ):
                                    raise ReviewInputError(
                                        "corrupted batch output part"
                                    )
                                chunk_doc = dict(part_rec_b.part_payload)
                                chunk_doc.pop("_batch_manifest", None)
                                sid = _partition_storage_id(idx_p, part_rec_b.record_id)
                                part_map[sid] = AuthenticatedPart(
                                    document=_to_json_compatible(chunk_doc),
                                    producer=str(b_binding["producer"]),
                                )
                            b_manifest = bparts[0].part_payload.get("_batch_manifest")
                            if not isinstance(b_manifest, Mapping):
                                raise ReviewInputError("missing batch output manifest")

                            def _read_bpart(sid: str) -> AuthenticatedPart:
                                if sid not in part_map:
                                    raise ReviewInputError(
                                        f"missing batch output part {sid!r}"
                                    )
                                return part_map[sid]

                            restored = read_partitioned_evidence(
                                b_manifest,
                                reader=_read_bpart,
                                expected_binding=b_binding,
                                budget=EvidenceReadBudget(),
                            )
                            if (
                                not isinstance(restored, Mapping)
                                or canonical_sha256(dict(restored)) != exp_bsha
                            ):
                                raise ReviewInputError("batch output digest mismatch")
                            loaded_summary = str(restored.get("summary") or "")
                            raw_restored_findings = restored.get("findings")
                            if not isinstance(
                                raw_restored_findings, Sequence
                            ) or isinstance(
                                raw_restored_findings, (str, bytes, bytearray)
                            ):
                                raise ReviewInputError(
                                    "invalid restored batch findings"
                                )
                            loaded_findings = tuple(
                                dict(f) for f in raw_restored_findings
                            )
                        except (ReviewInputError, ValueError, KeyError, IndexError):
                            return (
                                scoped_request,
                                None,
                                calls_started_this_run,
                                owners,
                                owner_cid,
                                unresolved_results,
                                result_parts,
                                result_manifests,
                                comment_ids_by_record_id,
                                self._finalize_hold(
                                    request=scoped_request,
                                    guard=guard,
                                    prior_hint=prior_hint,
                                    blocker="ambiguous_batch_inference_outcome",
                                    missing_evidence=(
                                        f"batch_output_parts:{b.batch_id}",
                                    ),
                                ),
                            )

                    reconstructed_comments = tuple(
                        ReviewComment(
                            path=str(c["path"]),
                            line=(None if c.get("line") is None else int(c["line"])),
                            body=str(c["body"]),
                            side=str(c.get("side") or "RIGHT"),
                            severity=(
                                str(c["severity"])
                                if isinstance(c.get("severity"), str)
                                else None
                            ),
                        )
                        for c in loaded_findings
                    )
                    batch_results_by_id[b.batch_id] = ReviewResult(
                        summary=loaded_summary,
                        comments=reconstructed_comments,
                        provider=existing_out.provider,
                        model=existing_out.model,
                        review_status=existing_out.review_status,
                    )
                    continue

                if used_provider_calls >= plan_rec.max_provider_calls:
                    return (
                        scoped_request,
                        None,
                        calls_started_this_run,
                        owners,
                        owner_cid,
                        unresolved_results,
                        result_parts,
                        result_manifests,
                        comment_ids_by_record_id,
                        self._finalize_hold(
                            request=scoped_request,
                            guard=guard,
                            prior_hint=prior_hint,
                            blocker="review_provider_call_budget_exhausted",
                            missing_evidence=(
                                f"batch_budget:{used_provider_calls}/{plan_rec.max_provider_calls}",
                            ),
                        ),
                    )

                attempt = 1
                batch_prompt_sha256 = canonical_sha256(b.to_plan_dict())
                dispatch_rec = BatchDispatchRecord(
                    record_id=(
                        f"batch-dispatch:{scoped_request.operation_id}:"
                        f"{context.head_sha[:12]}:{input_digest}:{b.batch_id}:{attempt}"
                    ),
                    operation_id=scoped_request.operation_id,
                    batch_id=b.batch_id,
                    attempt=attempt,
                    plan_sha256=plan_sha256,
                    prompt_sha256=batch_prompt_sha256,
                    generation=dispatch_gen,
                )

                def _find_dispatch_cid(
                    ledger: _CollectedLedger,
                    *,
                    expected: BatchDispatchRecord = dispatch_rec,
                ) -> int | None:
                    for cand in ledger.batch_dispatches:
                        if cand.record_id == expected.record_id:
                            return ledger.comment_ids_by_record_id.get(cand.record_id)
                    return None

                try:
                    dcid = self._post_ledger_marker_with_rescan(
                        request=scoped_request,
                        guard=guard,
                        prior_hint=prior_hint,
                        record=dispatch_rec,
                        effect_kind_label="batch_dispatch",
                        find_existing_cid=_find_dispatch_cid,
                    )
                    comment_ids_by_record_id[dispatch_rec.record_id] = dcid
                except (
                    _AmbiguousWriteOutcomeError,
                    TimeoutError,
                    ConnectionError,
                    GitHubHTTPTransientError,
                ):
                    return (
                        scoped_request,
                        None,
                        calls_started_this_run,
                        owners,
                        owner_cid,
                        unresolved_results,
                        result_parts,
                        result_manifests,
                        comment_ids_by_record_id,
                        self._finalize_hold(
                            request=scoped_request,
                            guard=guard,
                            prior_hint=prior_hint,
                            blocker="ambiguous_write_outcome",
                            missing_evidence=(
                                f"batch_dispatch:{dispatch_rec.record_id}",
                            ),
                        ),
                    )

                batch_res, batch_calls = execute_v6_batch(
                    context,
                    b,
                    remaining_provider_calls=(
                        plan_rec.max_provider_calls - used_provider_calls
                    ),
                )
                used_provider_calls += batch_calls
                calls_started_this_run += batch_calls
                if batch_res.review_status != "complete":
                    return (
                        scoped_request,
                        None,
                        calls_started_this_run,
                        owners,
                        owner_cid,
                        unresolved_results,
                        result_parts,
                        result_manifests,
                        comment_ids_by_record_id,
                        self._finalize_hold(
                            request=scoped_request,
                            guard=guard,
                            prior_hint=prior_hint,
                            blocker="v6_batch_incomplete_coverage",
                            missing_evidence=(
                                f"batch_incomplete:{b.batch_id}:{batch_res.review_status}",
                            ),
                        ),
                    )
                batch_results_by_id[b.batch_id] = batch_res

                serialized_findings = tuple(
                    {
                        "path": c.path,
                        "line": c.line,
                        "side": c.side,
                        "body": c.body,
                        **(
                            {"severity": c.severity}
                            if getattr(c, "severity", None) is not None
                            else {}
                        ),
                    }
                    for c in batch_res.comments
                )
                output_rec = BatchOutputRecord(
                    record_id=(
                        f"batch-output:{scoped_request.operation_id}:"
                        f"{context.head_sha[:12]}:{input_digest}:{b.batch_id}:{attempt}"
                    ),
                    operation_id=scoped_request.operation_id,
                    batch_id=b.batch_id,
                    plan_sha256=plan_sha256,
                    hunk_ids_reviewed=b.primary_hunk_ids,
                    findings=serialized_findings,
                    summary=batch_res.summary,
                    provider=batch_res.provider,
                    model=batch_res.model,
                    review_status=batch_res.review_status,
                    attempt=attempt,
                    provider_calls=batch_calls,
                )
                try:
                    format_v6_record_marker(output_rec)
                except V6LedgerIntegrityError as marker_exc:
                    if marker_exc.diagnostic != "v6_marker_too_large":
                        raise
                    # Partition large batch output losslessly into ResultPartRecords <= 64 KiB.
                    b_binding = _evidence_binding_for_request(
                        scoped_request, pr_head_sha=context.head_sha
                    )
                    b_env = {
                        "summary": batch_res.summary,
                        "findings": _to_json_compatible(serialized_findings),
                    }
                    b_sha = canonical_sha256(b_env)
                    raw_bparts = partition_evidence(
                        b_env,
                        binding=b_binding,
                        item_count=len(serialized_findings),
                    )
                    b_storage_ids = tuple(
                        _partition_storage_id(
                            idx_p,
                            f"batch-part:{scoped_request.operation_id}:{context.head_sha[:12]}:{input_digest}:{b.batch_id}:{idx_p:03d}",
                        )
                        for idx_p in range(len(raw_bparts))
                    )
                    b_manifest = evidence_manifest(
                        b_env,
                        binding=b_binding,
                        item_count=len(serialized_findings),
                        parts=raw_bparts,
                        storage_ids=b_storage_ids,
                    )
                    candidate_bparts: list[ResultPartRecord] = []
                    for idx_p, part_d in enumerate(raw_bparts):
                        payload_copy = dict(part_d)
                        if idx_p == 0:
                            payload_copy["_batch_manifest"] = b_manifest
                        part_rid = (
                            f"batch-part:{scoped_request.operation_id}:"
                            f"{context.head_sha[:12]}:{input_digest}:{b.batch_id}:{idx_p:03d}"
                        )
                        bp_rec = ResultPartRecord(
                            record_id=part_rid,
                            operation_id=scoped_request.operation_id,
                            pr_head_sha=context.head_sha,
                            payload_sha256=b_sha,
                            part_index=idx_p,
                            part_count=len(raw_bparts),
                            part_sha256=canonical_sha256(payload_copy),
                            part_payload=payload_copy,
                        )
                        format_v6_record_marker(bp_rec)
                        candidate_bparts.append(bp_rec)

                    if (
                        self.mutation_bound.writes_attempted + len(candidate_bparts) + 1
                        > self.mutation_bound.max_writes
                    ):
                        raise MutationSanityBoundError(
                            f"mutation sanity bound insufficient for batch-output parts "
                            f"({self.mutation_bound.writes_attempted}+"
                            f"{len(candidate_bparts) + 1}/{self.mutation_bound.max_writes})",
                            diagnostic="v6_mutation_sanity_bound_exceeded",
                        )

                    for bp_rec in candidate_bparts:

                        def _find_bp_cid(
                            ledger: _CollectedLedger,
                            *,
                            expected_bp: ResultPartRecord = bp_rec,
                        ) -> int | None:
                            for cand in ledger.result_parts:
                                if cand.record_id == expected_bp.record_id:
                                    return ledger.comment_ids_by_record_id.get(
                                        cand.record_id
                                    )
                            return None

                        try:
                            bpcid = self._post_ledger_marker_with_rescan(
                                request=scoped_request,
                                guard=guard,
                                prior_hint=prior_hint,
                                record=bp_rec,
                                effect_kind_label="batch_output_part",
                                find_existing_cid=_find_bp_cid,
                            )
                            comment_ids_by_record_id[bp_rec.record_id] = bpcid
                            result_parts = (*result_parts, bp_rec)
                        except (
                            _AmbiguousWriteOutcomeError,
                            TimeoutError,
                            ConnectionError,
                            GitHubHTTPTransientError,
                        ):
                            return (
                                scoped_request,
                                None,
                                calls_started_this_run,
                                owners,
                                owner_cid,
                                unresolved_results,
                                result_parts,
                                result_manifests,
                                comment_ids_by_record_id,
                                self._finalize_hold(
                                    request=scoped_request,
                                    guard=guard,
                                    prior_hint=prior_hint,
                                    blocker="ambiguous_write_outcome",
                                    missing_evidence=(
                                        f"batch_output_part:{bp_rec.record_id}",
                                    ),
                                ),
                            )

                    output_rec = BatchOutputRecord(
                        record_id=output_rec.record_id,
                        operation_id=scoped_request.operation_id,
                        batch_id=b.batch_id,
                        plan_sha256=plan_sha256,
                        hunk_ids_reviewed=b.primary_hunk_ids,
                        findings=(),
                        summary=f"__partitioned__:{b_sha}:{len(candidate_bparts)}",
                        provider=batch_res.provider,
                        model=batch_res.model,
                        review_status=batch_res.review_status,
                        attempt=attempt,
                        provider_calls=batch_calls,
                    )

                def _find_output_cid(
                    ledger: _CollectedLedger,
                    *,
                    expected: BatchOutputRecord = output_rec,
                ) -> int | None:
                    for cand in ledger.batch_outputs:
                        if cand.record_id == expected.record_id:
                            return ledger.comment_ids_by_record_id.get(cand.record_id)
                    return None

                try:
                    ocid = self._post_ledger_marker_with_rescan(
                        request=scoped_request,
                        guard=guard,
                        prior_hint=prior_hint,
                        record=output_rec,
                        effect_kind_label="batch_output",
                        find_existing_cid=_find_output_cid,
                    )
                    comment_ids_by_record_id[output_rec.record_id] = ocid
                except (
                    _AmbiguousWriteOutcomeError,
                    TimeoutError,
                    ConnectionError,
                    GitHubHTTPTransientError,
                ):
                    return (
                        scoped_request,
                        None,
                        calls_started_this_run,
                        owners,
                        owner_cid,
                        unresolved_results,
                        result_parts,
                        result_manifests,
                        comment_ids_by_record_id,
                        self._finalize_hold(
                            request=scoped_request,
                            guard=guard,
                            prior_hint=prior_hint,
                            blocker="ambiguous_write_outcome",
                            missing_evidence=(f"batch_output:{output_rec.record_id}",),
                        ),
                    )

        ordered_batch_results = [
            batch_results_by_id[b.batch_id] for b in context.planned_batches
        ]
        reviewed_hunk_ids = [
            hid
            for b in context.planned_batches
            if batch_results_by_id[b.batch_id].review_status == "complete"
            for hid in b.primary_hunk_ids
        ]
        try:
            verify_expected_hunks_reviewed(context, reviewed_hunk_ids)
        except V6FreshAnalysisError as cov_exc:
            return (
                scoped_request,
                None,
                calls_started_this_run,
                owners,
                owner_cid,
                unresolved_results,
                result_parts,
                result_manifests,
                comment_ids_by_record_id,
                self._finalize_hold(
                    request=scoped_request,
                    guard=guard,
                    prior_hint=prior_hint,
                    blocker=cov_exc.diagnostic or "v6_batch_incomplete_coverage",
                    missing_evidence=(str(cov_exc),),
                ),
            )
        raw_findings, raw_digests = finalize_v6_fresh_analysis_result(
            context, ordered_batch_results
        )
        findings_tuple = tuple(dict(entry) for entry in raw_findings)
        persisted_collected = replace(
            collected,
            unresolved_results=unresolved_results,
            result_parts=result_parts,
            result_manifests=result_manifests,
            comment_ids_by_record_id=comment_ids_by_record_id,
        )
        try:
            (
                unresolved_results,
                result_parts,
                result_manifests,
                comment_ids_by_record_id,
            ) = self._persist_initial_result(
                request=scoped_request,
                guard=guard,
                prior_hint=prior_hint,
                findings_tuple=findings_tuple,
                raw_digests=raw_digests,
                collected=persisted_collected,
            )
        except (
            _AmbiguousWriteOutcomeError,
            TimeoutError,
            ConnectionError,
            GitHubHTTPTransientError,
        ):
            return (
                scoped_request,
                None,
                calls_started_this_run,
                owners,
                owner_cid,
                unresolved_results,
                result_parts,
                result_manifests,
                comment_ids_by_record_id,
                self._finalize_hold(
                    request=scoped_request,
                    guard=guard,
                    prior_hint=prior_hint,
                    blocker="ambiguous_write_outcome",
                    missing_evidence=(
                        f"result_persistence:{scoped_request.operation_id}:{context.head_sha}",
                    ),
                ),
            )

        validated = self._validate_pr_resident_result(
            request=scoped_request,
            unresolved_results=unresolved_results,
            result_parts=result_parts,
            result_manifests=result_manifests,
            comment_ids_by_record_id=comment_ids_by_record_id,
        )
        if isinstance(validated, RecoveryHold):
            return (
                scoped_request,
                None,
                calls_started_this_run,
                owners,
                owner_cid,
                unresolved_results,
                result_parts,
                result_manifests,
                comment_ids_by_record_id,
                self._finalize_hold(
                    request=scoped_request,
                    guard=guard,
                    prior_hint=prior_hint,
                    blocker=validated.blocker,
                    missing_evidence=validated.missing_evidence,
                ),
            )
        return (
            scoped_request,
            validated,
            calls_started_this_run,
            owners,
            owner_cid,
            unresolved_results,
            result_parts,
            result_manifests,
            comment_ids_by_record_id,
            None,
        )

    def _deliver_detail_comments(
        self,
        *,
        request: RecoveryRequest,
        guard: NoProgressGuard,
        prior_hint: AdvisoryProgressHint | None,
        planned_delivery: _PlannedDelivery,
        comment_records: Sequence[PaginatedRecord],
        intents: Sequence[WriteIntentRecord],
        receipts: Sequence[CompletionReceiptRecord],
        non_submissions: Sequence[NonSubmissionProofRecord],
        owners: tuple[OwnerMarkerRecord, ...],
        owner_cid: int | None,
        comment_ids_by_record_id: dict[str, int],
        summary: Any,
        main_review_has_current_intent: bool,
    ) -> tuple[
        tuple[tuple[int, str], ...],
        tuple[int, ...],
        tuple[Mapping[str, Any], ...],
        int | None,
        int,
        RecoveryHold | None,
    ]:
        """Publish or reconcile every planned PR detail comment before native review."""

        if not planned_delivery.detail_comments:
            return (), (), (), owner_cid, 0, None

        # Preflight write plan capacity across all pending detail comments + native review.
        planned_writes = 0
        for detail in planned_delivery.detail_comments:
            if detail.operation_id in summary.completed_operations:
                continue
            has_cur_intent = any(
                i.operation_id == detail.operation_id
                and (
                    (
                        request.generation is not None
                        and i.generation == request.generation
                    )
                    or (
                        request.generation is None
                        and i.writer_epoch == request.ownership_proof.writer_epoch
                    )
                )
                for i in intents
            )
            planned_writes += (0 if has_cur_intent else 1) + 2
        planned_writes += (0 if main_review_has_current_intent else 1) + 2
        if (
            self.mutation_bound.writes_attempted + planned_writes
            > self.mutation_bound.max_writes
        ):
            raise MutationSanityBoundError(
                f"mutation sanity bound insufficient for detail+review delivery plan "
                f"({self.mutation_bound.writes_attempted}+{planned_writes}/"
                f"{self.mutation_bound.max_writes})",
                diagnostic="v6_mutation_sanity_bound_exceeded",
            )

        if request.pre_publish_head_verifier is not None:
            try:
                request.pre_publish_head_verifier()
            except ReviewInputError as head_exc:
                return (
                    (),
                    (),
                    (),
                    owner_cid,
                    0,
                    self._finalize_hold(
                        request=request,
                        guard=guard,
                        prior_hint=prior_hint,
                        blocker=head_exc.diagnostic or "v6_pr_head_mismatch",
                        missing_evidence=(
                            f"pr_head_match:{request.ownership_proof.pr_head_sha}",
                        ),
                    ),
                )

        comment_payload_by_id: dict[int, Mapping[str, Any]] = {}
        for rec in comment_records:
            if isinstance(rec.payload, Mapping):
                raw_id = rec.payload.get("id", rec.record_id)
                if type(raw_id) is int and raw_id > 0:
                    comment_payload_by_id[raw_id] = rec.payload

        detail_links: list[tuple[int, str]] = []
        detail_cids: list[int] = []
        detail_resolutions: list[Mapping[str, Any]] = []
        readback_gets = 0
        updated_receipts = list(receipts)

        for detail in planned_delivery.detail_comments:
            d_op_id = detail.operation_id
            _assert_target_pr_binding(request, path=detail.effect_path)
            if d_op_id in summary.completed_operations:
                matched = next(
                    r for r in summary.matched_receipts if r.operation_id == d_op_id
                )
                if matched.payload_sha256 != detail.payload_sha256:
                    return (
                        (),
                        (),
                        (),
                        owner_cid,
                        readback_gets,
                        self._finalize_hold(
                            request=request,
                            guard=guard,
                            prior_hint=prior_hint,
                            blocker="stale_detail_receipt_payload_mismatch",
                            missing_evidence=(
                                f"detail_receipt_payload_mismatch:{d_op_id}",
                            ),
                        ),
                    )
                eff_id = matched.github_effect_id
                eff_int = int(eff_id) if str(eff_id).isdigit() else 0
                if eff_int > 0:
                    detail_cids.append(eff_int)
                url = _extract_detail_comment_url(
                    comment_payload_by_id.get(eff_int),
                    owner=request.owner,
                    repo=request.repo,
                    pull_request=request.pull_request,
                    comment_id=eff_id,
                )
                detail_links.append((detail.index, url))
                continue

            cur_intent = next(
                (
                    i
                    for i in reversed(intents)
                    if i.operation_id == d_op_id
                    and (
                        (
                            request.generation is not None
                            and i.generation == request.generation
                        )
                        or (
                            request.generation is None
                            and i.writer_epoch == request.ownership_proof.writer_epoch
                        )
                    )
                ),
                None,
            )
            cur_intent_cid = (
                comment_ids_by_record_id.get(cur_intent.record_id)
                if cur_intent is not None
                else None
            )

            existing_d_intent = next(
                (
                    i
                    for i in reversed(summary.unreconciled_intents)
                    if i.operation_id == d_op_id
                ),
                None,
            )
            if existing_d_intent is not None:
                if existing_d_intent.payload_sha256 != detail.payload_sha256:
                    return (
                        tuple(detail_links),
                        tuple(detail_cids),
                        tuple(detail_resolutions),
                        owner_cid,
                        readback_gets,
                        self._finalize_hold(
                            request=request,
                            guard=guard,
                            prior_hint=prior_hint,
                            blocker="pr_result_digest_mismatch_with_intent",
                            missing_evidence=(
                                f"detail_intent_digest:{existing_d_intent.record_id}",
                            ),
                        ),
                    )
                outcome_known, landed, verified_id, gets = (
                    self._reconcile_effect_surface(
                        request=request,
                        guard=guard,
                        prior_hint=prior_hint,
                        expected_payload_sha256=detail.payload_sha256,
                        scanned_reviews=comment_records,
                        expected_head_sha=existing_d_intent.pr_head_sha,
                        rescan_before_verify=False,
                        operation_id=d_op_id,
                        effect_kind="issue_comment",
                    )
                )
                readback_gets += gets
                if not outcome_known:
                    return (
                        tuple(detail_links),
                        tuple(detail_cids),
                        tuple(detail_resolutions),
                        owner_cid,
                        readback_gets,
                        self._finalize_hold(
                            request=request,
                            guard=guard,
                            prior_hint=prior_hint,
                            blocker="ambiguous_write_outcome",
                            missing_evidence=(
                                f"effect_verification:{d_op_id}:{detail.payload_sha256}",
                            ),
                        ),
                    )
                if landed and verified_id is not None:
                    if owner_cid is None:
                        owner_cid = self._persist_owner_marker_if_needed(
                            request=request,
                            guard=guard,
                            prior_hint=prior_hint,
                            existing_owners=owners,
                            comment_ids_by_record_id=comment_ids_by_record_id,
                        )
                    try:
                        receipt, r_gets, r_cid = self._persist_completion_receipt(
                            request=request,
                            guard=guard,
                            prior_hint=prior_hint,
                            github_effect_id=verified_id,
                            payload_sha256=detail.payload_sha256,
                            existing_receipts=updated_receipts,
                            comment_ids_by_record_id=comment_ids_by_record_id,
                            operation_id=d_op_id,
                        )
                    except _AmbiguousReceiptOutcomeError:
                        return (
                            tuple(detail_links),
                            tuple(detail_cids),
                            tuple(detail_resolutions),
                            owner_cid,
                            readback_gets,
                            self._finalize_hold(
                                request=request,
                                guard=guard,
                                prior_hint=prior_hint,
                                blocker="ambiguous_receipt_outcome",
                                missing_evidence=(
                                    f"receipt_verification:{d_op_id}:{detail.payload_sha256}",
                                ),
                            ),
                        )
                    readback_gets += r_gets
                    updated_receipts.append(receipt)
                    eff_int = int(verified_id) if str(verified_id).isdigit() else 0
                    if eff_int > 0:
                        detail_cids.append(eff_int)
                    url = _extract_detail_comment_url(
                        comment_payload_by_id.get(eff_int),
                        owner=request.owner,
                        repo=request.repo,
                        pull_request=request.pull_request,
                        comment_id=verified_id,
                    )
                    detail_links.append((detail.index, url))
                    if (
                        request.generation is not None
                        and cur_intent_cid is not None
                        and r_cid is not None
                    ):
                        detail_resolutions.append(
                            {
                                "operation_id": d_op_id,
                                "intent_comment_id": cur_intent_cid,
                                "resolution": "receipt_present",
                                "receipt_comment_id": r_cid,
                            }
                        )
                    continue

                has_non_sub = any(
                    proof.operation_id == existing_d_intent.operation_id
                    and proof.payload_sha256 == existing_d_intent.payload_sha256
                    and proof.pr_head_sha == existing_d_intent.pr_head_sha
                    and proof.proof_kind in VALID_NON_SUBMISSION_PROOF_KINDS
                    and (request.slot_key is None or proof.slot_key == request.slot_key)
                    and (
                        existing_d_intent.generation is None
                        or proof.generation == existing_d_intent.generation
                    )
                    for proof in non_submissions
                )
                if not has_non_sub and not request.affirmative_non_submission:
                    return (
                        tuple(detail_links),
                        tuple(detail_cids),
                        tuple(detail_resolutions),
                        owner_cid,
                        readback_gets,
                        self._finalize_hold(
                            request=request,
                            guard=guard,
                            prior_hint=prior_hint,
                            blocker="unproved_non_submission_after_intent",
                            missing_evidence=(
                                f"non_submission_proof:{d_op_id}:{detail.payload_sha256}",
                            ),
                        ),
                    )

            if cur_intent is None:
                if owner_cid is None:
                    owner_cid = self._persist_owner_marker_if_needed(
                        request=request,
                        guard=guard,
                        prior_hint=prior_hint,
                        existing_owners=owners,
                        comment_ids_by_record_id=comment_ids_by_record_id,
                    )
                self._verify_write_boundary(
                    request=request,
                    guard=guard,
                    prior_durable_hint=prior_hint,
                    target_path=detail.effect_path,
                )
                cur_intent = WriteIntentRecord(
                    record_id=(
                        f"intent:{d_op_id}:g{request.generation or 0}:"
                        f"{request.ownership_proof.writer_epoch}"
                    ),
                    operation_id=d_op_id,
                    writer_epoch=request.ownership_proof.writer_epoch,
                    pr_head_sha=request.ownership_proof.pr_head_sha,
                    effect_kind="issue_comment",
                    payload_sha256=detail.payload_sha256,
                    target_digest=detail.payload_sha256,
                    generation=request.generation,
                    target_location=detail.effect_path,
                )
                self.mutation_bound.consume("write_intent_comment", "write_intent")
                intent_marker_body = format_v6_record_marker(cur_intent)
                intent_path = detail.effect_path

                def _post_detail_intent(ts: float) -> DualCredentialResponse:
                    return self.client.execute(
                        "write_intent_comment",
                        method="POST",
                        path=intent_path,
                        event=request.event,
                        visibility=request.visibility,
                        granted_scopes=request.granted_scopes,
                        credential_kind=CredentialKind.APP_BROKER,
                        body={"body": intent_marker_body},
                        now_epoch=ts,
                    )

                intent_resp = self._execute_with_throttle(
                    request=request,
                    guard=guard,
                    prior_durable_hint=prior_hint,
                    call_fn=_post_detail_intent,
                )
                c_id = verify_write_response_readback(
                    intent_resp, expected_marker=cur_intent
                )
                if type(c_id) is int and c_id > 0:
                    cur_intent_cid = c_id
                    comment_ids_by_record_id[cur_intent.record_id] = c_id

            if request.pre_publish_head_verifier is not None:
                try:
                    request.pre_publish_head_verifier()
                except ReviewInputError as head_exc:
                    blocker = head_exc.diagnostic or "v6_pr_head_mismatch"
                    if (
                        request.has_active_write_lease
                        and request.slot_key is not None
                        and request.generation is not None
                    ):
                        try:
                            self._persist_non_submission_proof(
                                request=request,
                                guard=guard,
                                prior_hint=prior_hint,
                                payload_sha256=detail.payload_sha256,
                                proof_kind="pre_dispatch_terminal_abort",
                                http_status=None,
                                diagnostic=blocker,
                                operation_id=d_op_id,
                            )
                        except StaleWriterOwnershipError:
                            pass
                    return (
                        tuple(detail_links),
                        tuple(detail_cids),
                        tuple(detail_resolutions),
                        owner_cid,
                        readback_gets,
                        self._finalize_hold(
                            request=request,
                            guard=guard,
                            prior_hint=prior_hint,
                            blocker=blocker,
                            missing_evidence=(
                                f"non_submission_proof:{d_op_id}:{detail.payload_sha256}",
                            ),
                        ),
                    )

            self._verify_write_boundary(
                request=request,
                guard=guard,
                prior_durable_hint=prior_hint,
                target_path=detail.effect_path,
            )
            self.mutation_bound.consume("write_intent_comment", "issue_comment")
            detail_effect_id: str | int | None = None
            detail_resp_body: Mapping[str, Any] | None = None
            try:
                detail_body_to_post = detail.posted_body
                detail_post_path = detail.effect_path

                def _post_detail_comment(ts: float) -> DualCredentialResponse:
                    return self.client.execute(
                        "write_intent_comment",
                        method="POST",
                        path=detail_post_path,
                        event=request.event,
                        visibility=request.visibility,
                        granted_scopes=request.granted_scopes,
                        credential_kind=CredentialKind.APP_BROKER,
                        body={"body": detail_body_to_post},
                        now_epoch=ts,
                    )

                post_resp = self._execute_with_throttle(
                    request=request,
                    guard=guard,
                    prior_durable_hint=prior_hint,
                    call_fn=_post_detail_comment,
                )
                if post_resp.status in {400, 404, 409, 410, 413, 422}:
                    blocker = _definitive_rejection_diagnostic(
                        post_resp.status, post_resp.body
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
                            payload_sha256=detail.payload_sha256,
                            proof_kind="definitive_github_rejection",
                            http_status=post_resp.status,
                            diagnostic=blocker,
                            operation_id=d_op_id,
                        )
                    return (
                        tuple(detail_links),
                        tuple(detail_cids),
                        tuple(detail_resolutions),
                        owner_cid,
                        readback_gets,
                        self._finalize_hold(
                            request=request,
                            guard=guard,
                            prior_hint=prior_hint,
                            blocker=blocker,
                            missing_evidence=(
                                f"non_submission_proof:{d_op_id}:{detail.payload_sha256}",
                            ),
                        ),
                    )
                detail_effect_id = verify_write_response_readback(
                    post_resp,
                    expected_operation_id=d_op_id,
                    expected_payload_sha256=detail.payload_sha256,
                )
                if isinstance(post_resp.body, Mapping):
                    detail_resp_body = post_resp.body
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
                        expected_payload_sha256=detail.payload_sha256,
                        scanned_reviews=comment_records,
                        expected_head_sha=request.ownership_proof.pr_head_sha,
                        rescan_before_verify=True,
                        operation_id=d_op_id,
                        effect_kind="issue_comment",
                    )
                )
                readback_gets += gets
                if not outcome_known or not landed or verified_id is None:
                    return (
                        tuple(detail_links),
                        tuple(detail_cids),
                        tuple(detail_resolutions),
                        owner_cid,
                        readback_gets,
                        self._finalize_hold(
                            request=request,
                            guard=guard,
                            prior_hint=prior_hint,
                            blocker="lost_write_outcome_unverified",
                            missing_evidence=(
                                f"effect_verification:{d_op_id}:{detail.payload_sha256}",
                            ),
                        ),
                    )
                detail_effect_id = verified_id

            try:
                receipt, r_gets, r_cid = self._persist_completion_receipt(
                    request=request,
                    guard=guard,
                    prior_hint=prior_hint,
                    github_effect_id=detail_effect_id,
                    payload_sha256=detail.payload_sha256,
                    existing_receipts=updated_receipts,
                    comment_ids_by_record_id=comment_ids_by_record_id,
                    operation_id=d_op_id,
                )
            except _AmbiguousReceiptOutcomeError:
                return (
                    tuple(detail_links),
                    tuple(detail_cids),
                    tuple(detail_resolutions),
                    owner_cid,
                    readback_gets,
                    self._finalize_hold(
                        request=request,
                        guard=guard,
                        prior_hint=prior_hint,
                        blocker="ambiguous_receipt_outcome",
                        missing_evidence=(
                            f"receipt_verification:{d_op_id}:{detail.payload_sha256}",
                        ),
                    ),
                )
            readback_gets += r_gets
            updated_receipts.append(receipt)
            if r_cid is not None:
                comment_ids_by_record_id[receipt.record_id] = r_cid
            eff_int = int(detail_effect_id) if str(detail_effect_id).isdigit() else 0
            if eff_int > 0:
                detail_cids.append(eff_int)
            url = _extract_detail_comment_url(
                detail_resp_body or comment_payload_by_id.get(eff_int),
                owner=request.owner,
                repo=request.repo,
                pull_request=request.pull_request,
                comment_id=detail_effect_id,
            )
            detail_links.append((detail.index, url))
            if cur_intent_cid is not None and r_cid is not None:
                detail_resolutions.append(
                    {
                        "operation_id": d_op_id,
                        "intent_comment_id": cur_intent_cid,
                        "resolution": "receipt_present",
                        "receipt_comment_id": r_cid,
                    }
                )

        return (
            tuple(detail_links),
            tuple(detail_cids),
            tuple(detail_resolutions),
            owner_cid,
            readback_gets,
            None,
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
            detail_prefix = f"{request.operation_id}:detail:"
            op_conflicts = [
                c
                for c in summary.conflicting_records
                if c.operation_id == request.operation_id
                or c.operation_id.startswith(detail_prefix)
            ]
            if op_conflicts:
                return self._finalize_hold(
                    request=request,
                    guard=guard,
                    prior_hint=prior_hint,
                    blocker="conflicting_immutable_records",
                    missing_evidence=tuple(c.record_id for c in op_conflicts),
                )

            comment_ids_by_record_id = dict(collected.comment_ids_by_record_id)
            (
                receipts,
                summary,
                owner_cid,
                detail_recon_gets,
                early_detail_hold,
            ) = self._reconcile_unreconciled_detail_intents(
                request=request,
                guard=guard,
                prior_hint=prior_hint,
                comment_records=comment_records,
                intents=intents,
                receipts=receipts,
                non_submissions=non_submissions,
                owners=owners,
                owner_cid=owner_cid,
                comment_ids_by_record_id=comment_ids_by_record_id,
                summary=summary,
            )
            if early_detail_hold is not None:
                return early_detail_hold

            existing_detail_cids = tuple(
                int(r.github_effect_id)
                for r in summary.matched_receipts
                if r.operation_id.startswith(detail_prefix)
                and str(r.github_effect_id).isdigit()
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
                    readback_gets_emitted=detail_recon_gets,
                    model_calls_started=0,
                    owner_comment_id=owner_cid,
                    receipt_comment_id=comment_ids_by_record_id.get(matched.record_id),
                    detail_comment_ids=existing_detail_cids,
                )

            op_unreconciled = [
                i
                for i in summary.unreconciled_intents
                if i.operation_id == request.operation_id
            ]
            readback_gets = detail_recon_gets
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
                comment_ids_by_record_id.get(current_intent.record_id)
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
                            comment_ids_by_record_id=comment_ids_by_record_id,
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
                                comment_ids_by_record_id=comment_ids_by_record_id,
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
                        detail_comment_ids=existing_detail_cids,
                    )

            # Effect not yet landed: recover from PR-resident result or execute restartable batch analysis.
            model_calls_started = 0
            result_parts = collected.result_parts
            result_manifests = collected.result_manifests
            effective_request = request
            validated: UnresolvedResultRecord | RecoveryHold
            if (
                existing_intent is None
                and request.operation_id not in summary.completed_operations
                and request.fresh_analysis_context_loader is not None
                and request.has_active_write_lease
            ):
                (
                    effective_request,
                    batch_validated,
                    model_calls_started,
                    owners,
                    owner_cid,
                    unresolved_results,
                    result_parts,
                    result_manifests,
                    comment_ids_by_record_id,
                    batch_hold,
                ) = self._run_restartable_batch_analysis(
                    request=request,
                    guard=guard,
                    prior_hint=prior_hint,
                    collected=collected,
                    owners=owners,
                    owner_cid=owner_cid,
                    unresolved_results=unresolved_results,
                    result_parts=result_parts,
                    result_manifests=result_manifests,
                    comment_ids_by_record_id=comment_ids_by_record_id,
                )
                if batch_hold is not None:
                    return batch_hold
                assert batch_validated is not None
                validated = batch_validated
            else:
                validated = self._validate_pr_resident_result(
                    request=request,
                    unresolved_results=unresolved_results,
                    result_parts=result_parts,
                    result_manifests=result_manifests,
                    comment_ids_by_record_id=comment_ids_by_record_id,
                )
                head_sha_prefix = request.ownership_proof.pr_head_sha[:12]
                id_suffix = (
                    f":{_request_input_digest(request, pr_head_sha=request.ownership_proof.pr_head_sha)}"
                    if _request_has_explicit_inputs(request)
                    else ""
                )
                target_result_record_id = (
                    f"result:{request.operation_id}:{head_sha_prefix}{id_suffix}"
                )
                target_manifest_record_id = f"result-manifest:{request.operation_id}:{head_sha_prefix}{id_suffix}"
                result_id_absent = (
                    target_result_record_id not in comment_ids_by_record_id
                    and target_manifest_record_id not in comment_ids_by_record_id
                    and not any(
                        r.record_id == target_result_record_id
                        for r in unresolved_results
                    )
                    and not any(
                        m.record_id == target_manifest_record_id
                        for m in result_manifests
                    )
                )
                if (
                    existing_intent is None
                    and request.operation_id not in summary.completed_operations
                    and isinstance(validated, RecoveryHold)
                    and validated.blocker == "missing_pr_unresolved_result"
                    and result_id_absent
                    and request.initial_result_loader is not None
                    and request.has_active_write_lease
                ):
                    if owner_cid is None:
                        owner_cid = self._persist_owner_marker_if_needed(
                            request=request,
                            guard=guard,
                            prior_hint=prior_hint,
                            existing_owners=owners,
                            comment_ids_by_record_id=comment_ids_by_record_id,
                        )
                        if (
                            request.owner_marker is not None
                            and request.owner_marker not in owners
                        ):
                            owners = (*owners, request.owner_marker)
                    raw_findings, raw_digests = request.initial_result_loader()
                    model_calls_started = 1
                    findings_tuple = tuple(dict(entry) for entry in raw_findings)
                    try:
                        (
                            unresolved_results,
                            result_parts,
                            result_manifests,
                            comment_ids_by_record_id,
                        ) = self._persist_initial_result(
                            request=request,
                            guard=guard,
                            prior_hint=prior_hint,
                            findings_tuple=findings_tuple,
                            raw_digests=raw_digests,
                            collected=replace(
                                collected,
                                unresolved_results=unresolved_results,
                                result_parts=result_parts,
                                result_manifests=result_manifests,
                                comment_ids_by_record_id=comment_ids_by_record_id,
                            ),
                        )
                    except (
                        _AmbiguousWriteOutcomeError,
                        TimeoutError,
                        ConnectionError,
                        GitHubHTTPTransientError,
                    ):
                        return self._finalize_hold(
                            request=request,
                            guard=guard,
                            prior_hint=prior_hint,
                            blocker="ambiguous_write_outcome",
                            missing_evidence=(
                                f"result_persistence:{request.operation_id}:{request.ownership_proof.pr_head_sha}",
                            ),
                        )
                    validated = self._validate_pr_resident_result(
                        request=request,
                        unresolved_results=unresolved_results,
                        result_parts=result_parts,
                        result_manifests=result_manifests,
                        comment_ids_by_record_id=comment_ids_by_record_id,
                    )
            if isinstance(validated, RecoveryHold):
                return self._finalize_hold(
                    request=effective_request,
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
                    request=effective_request,
                    guard=guard,
                    prior_hint=prior_hint,
                    blocker="pr_result_digest_mismatch_with_intent",
                    missing_evidence=(
                        f"intent_digest_match:{existing_intent.record_id}:"
                        f"{existing_intent.payload_sha256}",
                    ),
                )

            effect_path = (
                effective_request.effect_path
                if effective_request.effect_path is not None
                else (
                    f"/repos/{effective_request.owner}/{effective_request.repo}/pulls/"
                    f"{effective_request.pull_request}/reviews"
                )
            )
            _assert_target_pr_binding(effective_request, path=effect_path)

            if existing_intent is not None:
                has_pr_non_submission_proof = any(
                    proof.operation_id == existing_intent.operation_id
                    and proof.payload_sha256 == existing_intent.payload_sha256
                    and proof.pr_head_sha == existing_intent.pr_head_sha
                    and proof.proof_kind in VALID_NON_SUBMISSION_PROOF_KINDS
                    and (
                        effective_request.slot_key is None
                        or proof.slot_key == effective_request.slot_key
                    )
                    and (
                        existing_intent.generation is None
                        or proof.generation == existing_intent.generation
                    )
                    for proof in non_submissions
                )
                if (
                    not has_pr_non_submission_proof
                    and not effective_request.affirmative_non_submission
                ):
                    if effective_request.has_active_write_lease:
                        self._verify_write_boundary(
                            request=effective_request,
                            guard=guard,
                            prior_durable_hint=prior_hint,
                            target_path=effect_path,
                        )
                    return self._finalize_hold(
                        request=effective_request,
                        guard=guard,
                        prior_hint=prior_hint,
                        blocker="unproved_non_submission_after_intent",
                        missing_evidence=(
                            f"non_submission_proof:{effective_request.operation_id}:{existing_intent.payload_sha256}",
                        ),
                    )

            planned_delivery = _plan_review_and_detail_comments(
                effective_request,
                pr_result=pr_result,
                payload_sha256=payload_sha256,
            )
            (
                detail_links,
                detail_cids,
                detail_resolutions,
                owner_cid,
                detail_gets,
                detail_hold,
            ) = self._deliver_detail_comments(
                request=effective_request,
                guard=guard,
                prior_hint=prior_hint,
                planned_delivery=planned_delivery,
                comment_records=comment_records,
                intents=intents,
                receipts=receipts,
                non_submissions=non_submissions,
                owners=owners,
                owner_cid=owner_cid,
                comment_ids_by_record_id=comment_ids_by_record_id,
                summary=summary,
                main_review_has_current_intent=current_intent is not None,
            )
            readback_gets += detail_gets
            if detail_hold is not None:
                return detail_hold

            effect_payload = _render_native_review_with_detail_links(
                request=effective_request,
                lead_body=planned_delivery.native_lead_body,
                inline_comments=planned_delivery.native_inline_comments,
                payload_sha256=payload_sha256,
                detail_links=detail_links,
            )
            effect_payload_sha256 = canonical_sha256(effect_payload)

            if current_intent is None:
                if request.pre_publish_head_verifier is not None:
                    try:
                        request.pre_publish_head_verifier()
                    except ReviewInputError as head_exc:
                        return self._finalize_hold(
                            request=request,
                            guard=guard,
                            prior_hint=prior_hint,
                            blocker=head_exc.diagnostic or "v6_pr_head_mismatch",
                            missing_evidence=(
                                f"pr_head_match:{request.ownership_proof.pr_head_sha}",
                            ),
                        )
                if owner_cid is None:
                    owner_cid = self._persist_owner_marker_if_needed(
                        request=request,
                        guard=guard,
                        prior_hint=prior_hint,
                        existing_owners=owners,
                        comment_ids_by_record_id=comment_ids_by_record_id,
                    )
                self._verify_write_boundary(
                    request=request,
                    guard=guard,
                    prior_durable_hint=prior_hint,
                    target_path=effect_path,
                )
                target_digest = request.target_digest or effect_payload_sha256
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

            # Verify live PR head and exclusive writer ownership again immediately before external write.
            if request.pre_publish_head_verifier is not None:
                try:
                    request.pre_publish_head_verifier()
                except ReviewInputError as head_exc:
                    blocker = head_exc.diagnostic or "v6_pr_head_mismatch"
                    missing = (
                        f"non_submission_proof:{request.operation_id}:{payload_sha256}",
                    )
                    if (
                        request.has_active_write_lease
                        and request.slot_key is not None
                        and request.generation is not None
                    ):
                        try:
                            self._persist_non_submission_proof(
                                request=request,
                                guard=guard,
                                prior_hint=prior_hint,
                                payload_sha256=payload_sha256,
                                proof_kind="pre_dispatch_terminal_abort",
                                http_status=None,
                                diagnostic=blocker,
                            )
                        except StaleWriterOwnershipError:
                            pass
                    return self._finalize_hold(
                        request=request,
                        guard=guard,
                        prior_hint=prior_hint,
                        blocker=blocker,
                        missing_evidence=missing,
                    )

            self._verify_write_boundary(
                request=request,
                guard=guard,
                prior_durable_hint=prior_hint,
                target_path=effect_path,
            )

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
                if write_resp.status in {400, 404, 409, 410, 413, 422}:
                    blocker = _definitive_rejection_diagnostic(
                        write_resp.status, write_resp.body
                    )
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
            # Catch transient/dropped-reply errors before GitHubHTTPError because
            # GitHubHTTPTransientError subclasses GitHubHTTPError.
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
                    and http_err.http_status in {400, 404, 409, 410, 413, 422}
                    and request.has_active_write_lease
                    and request.slot_key is not None
                    and request.generation is not None
                ):
                    blocker = _definitive_rejection_diagnostic(
                        http_err.http_status, str(http_err)
                    )
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

            try:
                receipt, receipt_gets, receipt_cid = self._persist_completion_receipt(
                    request=effective_request,
                    guard=guard,
                    prior_hint=prior_hint,
                    github_effect_id=effect_id,
                    payload_sha256=payload_sha256,
                    existing_receipts=receipts,
                    comment_ids_by_record_id=comment_ids_by_record_id,
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
                used_pr_resident_result=model_calls_started == 0,
                readback_gets_emitted=readback_gets + receipt_gets,
                model_calls_started=model_calls_started,
                owner_comment_id=owner_cid,
                intent_comment_id=intent_cid,
                receipt_comment_id=receipt_cid,
                intent_record=current_intent,
                reconciled_after_lost_write=reconciled_after_lost_write,
                detail_comment_ids=detail_cids,
                detail_resolutions=detail_resolutions,
            )
        except _ThrottlePausedSignal as paused:
            return paused.outcome
