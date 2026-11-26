"""ReviewSensei v6 GitHub-governed recovery orchestrator (Phases 2 & 3).

Coordinates exclusive broker writer-ownership fencing, immutable write
intent/receipt persistence, stability-verified pagination, persistent
no-progress guarding, write-response-as-readback request compression,
reconcile-before-retry lost-write recovery, and PR-resident evidence checks
without ever starting a fresh model call as a recovery fallback.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, TypeAlias

from review_sensei.errors import ReviewInputError

from .errors import (
    GitHubHTTPPermissionError,
    GitHubHTTPRateLimitError,
    GitHubHTTPTransientError,
)
from .v6_credential_matrix import (
    CredentialKind,
    GitHubEventKind,
    RepositoryVisibility,
)
from .v6_pagination import (
    NoProgressGuard,
    PaginatedRecord,
    StabilityVerifiedPaginator,
)
from .v6_recovery_ledger import (
    AdvisoryProgressHint,
    CompletionReceiptRecord,
    UnresolvedResultRecord,
    V6LedgerRecord,
    WriteIntentRecord,
    WriterOwnershipProof,
    canonical_sha256,
    extract_v6_record_markers,
    format_v6_record_marker,
    reconcile_immutable_records,
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


class _AmbiguousWriteOutcomeError(RuntimeError):
    """Internal signal that a mutation response was lost, 5xx, or unverified."""


class _ThrottlePausedSignal(Exception):
    """Internal control-flow carrying a clean ``RecoveryPaused`` outcome."""

    def __init__(self, outcome: RecoveryPaused) -> None:
        super().__init__(outcome.reason)
        self.outcome = outcome


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
    if _effect_tag(operation_id, payload_sha256) in body:
        return True
    return operation_id in body and payload_sha256 in body


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
        except (GitHubHTTPRateLimitError, GitHubHTTPTransientError, TimeoutError):
            fallback = (
                prior_durable_hint if prior_durable_hint is not None else current_hint
            )
            return fallback, True

    def _verify_write_boundary(
        self,
        *,
        request: RecoveryRequest,
        guard: NoProgressGuard,
        prior_durable_hint: AdvisoryProgressHint | None,
    ) -> None:
        proof = request.ownership_proof
        if self.broker_fence_verifier is not None:
            fence = self.broker_fence_verifier(proof)
            verify_writer_ownership(
                proof,
                live_repository_id=int(
                    fence.get("repository_id", request.repository_id)
                ),
                live_pull_request=int(fence.get("pull_request", request.pull_request)),
                live_pr_head_sha=str(fence["pr_head_sha"]),
                active_writer_epoch=int(fence["active_writer_epoch"]),
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
        live_repo_id = base_repo.get("id", request.repository_id)
        live_head_sha = str(head_info.get("sha", ""))
        pr_state = str(pr_body.get("state", "open"))
        active_epoch = run_body.get("writer_epoch", proof.writer_epoch)
        active_run_id = run_body.get("active_run_id", run_body.get("id"))
        active_attempt = run_body.get("run_attempt")
        run_status = run_body.get("status")
        verify_writer_ownership(
            proof,
            live_repository_id=int(live_repo_id),
            live_pull_request=request.pull_request,
            live_pr_head_sha=live_head_sha,
            active_writer_epoch=int(active_epoch),
            pr_state=pr_state,
            active_run_id=(int(active_run_id) if type(active_run_id) is int else None),
            active_run_attempt=(
                int(active_attempt) if type(active_attempt) is int else None
            ),
            run_status=str(run_status) if run_status is not None else None,
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
        intents: list[WriteIntentRecord] = []
        receipts: list[CompletionReceiptRecord] = []
        unresolved_results: list[UnresolvedResultRecord] = []
        hints: list[AdvisoryProgressHint] = []

        for item in comment_records:
            payload = item.payload
            if not isinstance(payload, Mapping):
                continue
            body = payload.get("body")
            if not isinstance(body, str) or "<!-- reviewsensei:v6:" not in body:
                continue
            parsed = extract_v6_record_markers(body, strict=False)
            if not parsed:
                continue
            if not verify_app_authored_record(
                payload,
                expected_app_id=request.expected_app_id,
                expected_bot_login=request.expected_bot_login,
                expected_bot_user_id=request.expected_bot_user_id,
            ):
                continue
            for rec in parsed:
                if isinstance(rec, WriteIntentRecord):
                    intents.append(rec)
                elif isinstance(rec, CompletionReceiptRecord):
                    receipts.append(rec)
                elif isinstance(rec, UnresolvedResultRecord):
                    unresolved_results.append(rec)
                elif isinstance(rec, AdvisoryProgressHint):
                    hints.append(rec)
        return intents, receipts, unresolved_results, hints

    def _verify_candidate_effect_with_broker(
        self,
        *,
        request: RecoveryRequest,
        guard: NoProgressGuard,
        prior_hint: AdvisoryProgressHint | None,
        candidate_effect_id: str | int,
        expected_payload_sha256: str,
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

        if resp.status == 404:
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
        ):
            verified_id = resp.body.get("id", candidate_effect_id)
            return True, True, verified_id
        return True, False, None

    def _reconcile_effect_surface(
        self,
        *,
        request: RecoveryRequest,
        guard: NoProgressGuard,
        prior_hint: AdvisoryProgressHint | None,
        expected_payload_sha256: str,
        scanned_reviews: Sequence[PaginatedRecord],
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
            if _matches_effect_payload(
                record.payload,
                operation_id=request.operation_id,
                payload_sha256=expected_payload_sha256,
                expected_app_id=request.expected_app_id,
                expected_bot_login=request.expected_bot_login,
                expected_bot_user_id=request.expected_bot_user_id,
            ):
                effect_id = record.payload.get("id", record.record_id)
                candidate_ids.append(effect_id)

        if not candidate_ids:
            return True, False, None, 0

        readbacks = 0
        for candidate_id in candidate_ids:
            readbacks += 1
            known, landed, verified_id = self._verify_candidate_effect_with_broker(
                request=request,
                guard=guard,
                prior_hint=prior_hint,
                candidate_effect_id=candidate_id,
                expected_payload_sha256=expected_payload_sha256,
            )
            if not known:
                return False, False, None, readbacks
            if landed:
                return True, True, verified_id, readbacks
        return True, False, None, readbacks

    def _persist_completion_receipt(
        self,
        *,
        request: RecoveryRequest,
        guard: NoProgressGuard,
        prior_hint: AdvisoryProgressHint | None,
        github_effect_id: str | int,
        payload_sha256: str,
    ) -> tuple[CompletionReceiptRecord, int]:
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
        verify_write_response_readback(resp, expected_marker=receipt)
        return receipt, 0

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

    def recover_and_publish(
        self,
        request: RecoveryRequest,
        *,
        progress_guard: NoProgressGuard | None = None,
    ) -> RecoveryOutcome:
        """Execute v6 recovery & publication with zero fallback model calls."""

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

            intents, receipts, unresolved_results, found_hints = (
                self._collect_ledger_records(comment_records, request=request)
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

            summary = reconcile_immutable_records(intents, receipts)
            op_conflicts = [
                c
                for c in summary.conflicting_records
                if c.operation_id == request.operation_id
            ]
            if op_conflicts:
                return RecoveryHold(
                    operation_id=request.operation_id,
                    blocker="conflicting_immutable_records",
                    missing_evidence=tuple(c.record_id for c in op_conflicts),
                    zero_model_calls_started=True,
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

            if existing_intent is not None:
                outcome_known, landed, verified_id, gets = (
                    self._reconcile_effect_surface(
                        request=request,
                        guard=guard,
                        prior_hint=prior_hint,
                        expected_payload_sha256=existing_intent.payload_sha256,
                        scanned_reviews=review_records,
                        rescan_before_verify=False,
                    )
                )
                readback_gets += gets
                if not outcome_known:
                    return RecoveryHold(
                        operation_id=request.operation_id,
                        blocker="ambiguous_write_outcome",
                        missing_evidence=(
                            f"effect_verification:{request.operation_id}:{existing_intent.payload_sha256}",
                        ),
                        zero_model_calls_started=True,
                    )
                if landed and verified_id is not None:
                    guard.record_step(
                        new_records_count=0,
                        reconciled_effect=True,
                        new_phase="reconcile_landed",
                        state_digest=existing_intent.payload_sha256,
                    )
                    receipt, receipt_gets = self._persist_completion_receipt(
                        request=request,
                        guard=guard,
                        prior_hint=prior_hint,
                        github_effect_id=verified_id,
                        payload_sha256=existing_intent.payload_sha256,
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
                    )

            # Effect not yet landed: must recover strictly from PR-resident result.
            validated = self._validate_pr_resident_result(
                request=request,
                unresolved_results=unresolved_results,
            )
            if isinstance(validated, RecoveryHold):
                return validated
            pr_result = validated
            payload_sha256 = pr_result.payload_sha256

            if existing_intent is None:
                self._verify_write_boundary(
                    request=request,
                    guard=guard,
                    prior_durable_hint=prior_hint,
                )
                target_digest = request.target_digest or canonical_sha256(
                    {
                        "repo": request.repository_id,
                        "pr": request.pull_request,
                        "head": request.ownership_proof.pr_head_sha,
                    }
                )
                intent = WriteIntentRecord(
                    record_id=(
                        f"intent:{request.operation_id}:"
                        f"{request.ownership_proof.writer_epoch}"
                    ),
                    operation_id=request.operation_id,
                    writer_epoch=request.ownership_proof.writer_epoch,
                    pr_head_sha=request.ownership_proof.pr_head_sha,
                    effect_kind=request.effect_kind,
                    payload_sha256=payload_sha256,
                    target_digest=target_digest,
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
                        body={"body": format_v6_record_marker(intent)},
                        now_epoch=ts,
                    ),
                )
                verify_write_response_readback(intent_resp, expected_marker=intent)

            # Verify exclusive writer ownership again immediately before external write.
            self._verify_write_boundary(
                request=request,
                guard=guard,
                prior_durable_hint=prior_hint,
            )

            effect_path = (
                request.effect_path
                if request.effect_path is not None
                else (
                    f"/repos/{request.owner}/{request.repo}/pulls/"
                    f"{request.pull_request}/reviews"
                )
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
                effect_id = verify_write_response_readback(
                    write_resp,
                    expected_operation_id=request.operation_id,
                    expected_payload_sha256=payload_sha256,
                )
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
                        rescan_before_verify=True,
                    )
                )
                readback_gets += gets
                if not outcome_known or not landed or verified_id is None:
                    return RecoveryHold(
                        operation_id=request.operation_id,
                        blocker="lost_write_outcome_unverified",
                        missing_evidence=(
                            f"effect_verification:{request.operation_id}:{payload_sha256}",
                        ),
                        zero_model_calls_started=True,
                    )
                effect_id = verified_id
                reconciled_after_lost_write = True

            receipt, receipt_gets = self._persist_completion_receipt(
                request=request,
                guard=guard,
                prior_hint=prior_hint,
                github_effect_id=effect_id,
                payload_sha256=payload_sha256,
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
            )
        except _ThrottlePausedSignal as paused:
            return paused.outcome
