"""Compose GitHub capabilities, publishers, and opt-ins for issue #64."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from ...baseline import ReviewBaseline
from ...budgets import ReviewWorkBudgets
from ...context import ReviewContextCacheKey, finding_lifecycle_for_comment
from ...convergence import (
    OPERATOR_REVIEW_MODES,
    BlockerCandidate,
    ReviewConvergencePolicy,
    RoundSessionState,
    detect_no_progress,
    observe_shadow_admission,
)
from ...conversation import ConversationService
from ...coverage import coverage_approval_state
from ...diff import analyze_diff
from ...disposition import MaintainerCommand
from ...errors import ReviewInputError
from ...human_assessment import (
    HumanAssessmentDecision,
    HumanAssessmentReply,
    HumanAssessmentService,
)
from ...models import ConversationReply, ReviewResult, ReviewTransaction
from ...outcomes import RecoveryArtifact, ResourceBudget, ResourceBudgetTracker
from ...planning import related_paths_for_change
from ...providers.base import ReviewProvider
from ...reassessment_work import HumanAssessmentWork, reassess
from ...scope import ContextRequest

if TYPE_CHECKING:
    from ...service import ReviewService
    from ...work_recovery import WorkRecoveryStore
from ...session import (
    SessionIdentity,
    SessionLedger,
    admission_diagnostic,
    blocker_set_digest,
    complete_review_publication,
    complete_session_round,
    convergence_progress_blocker_markers,
    load_review_transaction_for_publication,
    prepare_session_round,
    read_session_baseline,
    record_admitted_blocker_progress,
    record_session_failed_attempt,
    review_analysis_checkpoint_eligible,
    session_reservation_id,
    should_skip_automation,
)
from ...verifier import CandidateFinding, PublishableReview
from .approval import (
    ReviewApprovalEligibility,
    has_blocking_findings,
    has_human_adjudication_findings,
    qualification_state_for_publication,
)
from .broker_client import BrokerClient
from .checks import REVIEW_POLICY_MODES
from .conversation import (
    ConversationPublisher,
    PreparedConversation,
    ReplyResult,
)
from .errors import (
    GitHubBrokerClientError,
    GitHubHTTPTransientError,
    GitHubPublicationError,
)
from .http import GitHubHttp
from .human_assessment import HumanAssessmentPublisher
from .learning_pr import LearningPRPublisher, LearningPRResult
from .publication import (
    PublicationResult,
    ReviewPublisher,
    prepare_publication_review,
)


@dataclass(frozen=True)
class GitHubWriteOptions:
    """GitHub publication switches with default-on approval safety gates.

    ``reviews_policy`` is the canonical ``github.reviews`` mode. It is
    ``None`` for callers that only have the legacy approval boolean, which then
    selects the compatible mode: approving eligible reviews, or enforcing
    required fixes through the check gate without approving.
    """

    auto_review: bool = False
    auto_approve: bool = True
    reviews_policy: str | None = None
    github_writes: bool = False
    learning_prs: bool = False
    mention_replies: bool = False
    upload_artifacts: bool = False
    github_session_ledger: bool = False

    def review_policy(self) -> str:
        """Return the effective review policy mode for this invocation."""

        if self.reviews_policy is None:
            return "auto-approve" if self.auto_approve else "blocking"
        if self.reviews_policy not in REVIEW_POLICY_MODES:
            raise GitHubPublicationError("review policy mode is invalid")
        return self.reviews_policy


def _conversation_bytes(value: object) -> bytes:
    if not isinstance(value, ConversationReply):
        return b""
    return json.dumps(value.to_dict(), sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _conversation_from_bytes(payload: bytes) -> ConversationReply:
    loaded = json.loads(payload)
    if not isinstance(loaded, dict):
        raise ReviewInputError("operation entry refused: result_missing")
    return ConversationReply.from_dict(loaded)


def _assessment_bytes(value: object) -> bytes:
    reply = value.reply if isinstance(value, HumanAssessmentWork) else value
    if not isinstance(reply, HumanAssessmentReply):
        return b""
    body = {
        "body": reply.body,
        "decisions": [item.to_dict() for item in reply.decisions],
        "context_requests": [item.to_dict() for item in reply.context_requests],
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _assessment_from_bytes(payload: bytes) -> HumanAssessmentReply:
    loaded = json.loads(payload)
    if not isinstance(loaded, dict):
        raise ReviewInputError("operation entry refused: result_missing")
    decisions = loaded.get("decisions")
    requests = loaded.get("context_requests")
    if not isinstance(decisions, list) or not isinstance(requests, list):
        raise ReviewInputError("operation entry refused: result_missing")
    return HumanAssessmentReply(
        str(loaded.get("body", "")),
        tuple(
            HumanAssessmentDecision(
                str(item["fingerprint"]),
                str(item["decision"]),
                str(item["rationale"]),
                str(item["human_evidence"]),
                str(item["diff_evidence"]),
                tuple(
                    (str(pair["path"]), str(pair["excerpt"]))
                    for pair in item.get("related_diff_evidence", ())
                ),
            )
            for item in decisions
            if isinstance(item, dict)
        ),
        tuple(
            ContextRequest(
                str(item["reference"]),
                str(item["kind"]),
                tuple(str(path) for path in item["required_paths"]),
                str(item["reason"]),
            )
            for item in requests
            if isinstance(item, dict) and isinstance(item.get("required_paths"), list)
        ),
    )


def _approval_bytes(value: object) -> bytes:
    approved = bool(getattr(getattr(value, "approval", None), "approved", False))
    return json.dumps({"approved": approved}, separators=(",", ":")).encode("utf-8")


def _approval_from_bytes(payload: bytes) -> object:
    loaded = json.loads(payload)
    approved = isinstance(loaded, dict) and loaded.get("approved") is True

    class _Approval:
        def __init__(self) -> None:
            self.approved = approved

    class _Assessment:
        def __init__(self) -> None:
            self.approval = _Approval()

    return _Assessment()


def _command_bytes(value: object) -> bytes:
    from ...disposition import MaintainerCommandResult

    if not isinstance(value, MaintainerCommandResult):
        return b""
    return json.dumps(
        {
            "action": value.action,
            "applied": value.applied,
            "operator_paused": value.operator_paused,
            "summary": value.summary,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _command_from_bytes(payload: bytes) -> object:
    from ...disposition import MaintainerCommandResult

    loaded = json.loads(payload)
    if not isinstance(loaded, dict):
        raise ReviewInputError("operation entry refused: result_missing")
    return MaintainerCommandResult(
        action=str(loaded.get("action", "")),
        applied=loaded.get("applied") is True,
        operator_paused=loaded.get("operator_paused") is True,
        summary=str(loaded.get("summary", "")),
    )


class GitHubApplication:
    """Thin composition seam for workflow callers."""

    def __init__(
        self,
        *,
        broker: BrokerClient,
        http: GitHubHttp,
        reviewer: ReviewPublisher,
        learner: LearningPRPublisher,
        replier: ConversationPublisher,
        session_ledger: SessionLedger | None = None,
    ) -> None:
        self.broker = broker
        self.http = http
        self.reviewer = reviewer
        self.learner = learner
        self.replier = replier
        self.session_ledger = session_ledger

    def publish_review(
        self,
        *,
        options: GitHubWriteOptions,
        oidc_token: str | None,
        repository: str,
        repository_id: int,
        pull_request: int,
        head_sha: str,
        base_branch: str | None = None,
        base_sha: str | None = None,
        result: ReviewResult,
        diff: str,
        app_slug: str,
        candidates: Sequence[CandidateFinding] | None = None,
        snapshot: Mapping[str, str] | None = None,
        snapshot_sha256: str | None = None,
        evidence_policy: str = "legacy",
        convergence_policy: ReviewConvergencePolicy | None = None,
        blocker_candidates: Sequence[BlockerCandidate] | None = None,
        input_blocker_candidates: Sequence[BlockerCandidate] | None = None,
        baseline: ReviewBaseline | None = None,
        current_key: ReviewContextCacheKey | None = None,
        changed_paths: Sequence[str] | None = None,
        related_paths: Sequence[str] | None = None,
        evidence_confirmed_concerns: Sequence[str] = (),
        configuration_context: Mapping[str, object] | None = None,
        evidence_context: Mapping[str, object] | None = None,
        retained_eligibility: ReviewApprovalEligibility | None = None,
        continuation_digest: str | None = None,
        source_guard: Callable[[], bool] | None = None,
    ) -> PublicationResult:
        if not options.github_writes or not options.auto_review:
            return PublicationResult(status="disabled")
        if base_branch is None or base_sha is None:
            raise GitHubPublicationError(
                "review publication requires expected base branch and sha"
            )
        if current_key is not None:
            if not isinstance(current_key, ReviewContextCacheKey):
                raise GitHubPublicationError("review current cache key is invalid")
            if (
                current_key.repository != repository
                or current_key.pull_request != pull_request
                or current_key.base_sha != base_sha
                or current_key.head_sha != head_sha
            ):
                raise GitHubPublicationError(
                    "review current cache key does not match publication identity"
                )
        if evidence_policy not in {"legacy", "confirmed"}:
            raise GitHubPublicationError("evidence policy is unsupported")
        if evidence_policy == "confirmed" and (
            snapshot is None or snapshot_sha256 is None
        ):
            raise GitHubPublicationError(
                "confirmed evidence policy requires a reviewed snapshot"
            )
        if evidence_policy == "legacy" and (
            candidates or snapshot is not None or snapshot_sha256 is not None
        ):
            raise GitHubPublicationError(
                "legacy evidence policy cannot include candidate verification inputs"
            )
        policy = (
            convergence_policy
            if isinstance(convergence_policy, ReviewConvergencePolicy)
            else ReviewConvergencePolicy()
        )
        operator_mode = policy.mode in OPERATOR_REVIEW_MODES
        operator_baseline_requested = operator_mode and (
            (isinstance(result, ReviewResult) and result.transaction is not None)
            or options.github_session_ledger
            or baseline is not None
            or current_key is not None
        )
        identity = SessionIdentity(
            repository=repository,
            pull_request=pull_request,
            repository_id=repository_id,
        )
        transaction_record = None
        transaction_ledger: SessionLedger | None = None
        expected_configuration_digest: str | None = None
        expected_evidence_digest: str | None = None

        def load_transaction(source_ledger: SessionLedger):
            """Translate durable transaction validation into a publication error."""

            if (
                expected_configuration_digest is None
                or expected_evidence_digest is None
            ):
                raise GitHubPublicationError(
                    "identity-bound review publication context is incomplete"
                )
            try:
                return load_review_transaction_for_publication(
                    source_ledger,
                    identity,
                    result,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    policy_digest=policy.digest(),
                    configuration_digest=expected_configuration_digest,
                    evidence_digest=expected_evidence_digest,
                )
            except ReviewInputError as exc:
                raise GitHubPublicationError(
                    "review transaction validation failed"
                ) from exc

        if isinstance(result, ReviewResult) and result.transaction is not None:
            if self.session_ledger is None and not options.github_session_ledger:
                raise GitHubPublicationError(
                    "identity-bound review publication requires a session ledger"
                )
            if configuration_context is None:
                raise GitHubPublicationError(
                    "identity-bound review publication requires trusted configuration context"
                )
            try:
                expected_configuration_digest = (
                    ReviewTransaction.compute_configuration_digest(
                        configuration_context
                    )
                )
            except ReviewInputError as exc:
                raise GitHubPublicationError(
                    "review transaction configuration validation failed"
                ) from exc
            if evidence_context is None:
                evidence_context = {
                    "evidence_policy": evidence_policy,
                    "snapshot_sha256": snapshot_sha256,
                }
            try:
                expected_evidence_digest = ReviewTransaction.compute_evidence_digest(
                    evidence_context
                )
            except ReviewInputError as exc:
                raise GitHubPublicationError(
                    "review transaction evidence validation failed"
                ) from exc
            if self.session_ledger is not None:
                transaction_ledger = self.session_ledger
                transaction_record = load_transaction(self.session_ledger)
                if (
                    transaction_record.transaction is not None
                    and transaction_record.transaction.phase == "publication_succeeded"
                    and not options.github_session_ledger
                ):
                    return PublicationResult(
                        status="already_published",
                        diagnostic="transaction-publication-complete",
                    )
        exchange_input = oidc_token or self.broker.request_oidc_token()
        # The publication capability is obtained before the session enrollment
        # is recorded. Enrollment is a durable broker-side witness that this
        # pull request already has a session marker, so recording it for a run
        # that never reaches publication would leave the next run seeing a
        # known witness with no marker, and require authenticated recovery
        # after nothing worse than a transient broker failure.
        token = self.broker.exchange(exchange_input, capability="review_publish")
        session_token: str | None = None
        session_state: str | None = None
        # Only a hosted run has a broker enrollment witness to consult, so the
        # deleted-marker check below applies to exactly this branch. A caller
        # that injects its own ledger (the in-process and test path) has no
        # witness, so it is trusted to manage its own durability and does not
        # receive the authenticated-deletion protection the ADR promises.
        hosted_session_ledger = (
            options.github_session_ledger and self.session_ledger is None
        )
        # Session mutation is separate authority from review publication. A
        # hosted comment ledger obtains a broker-attested, current-head session
        # token, while the publisher keeps the review_publish capability above.
        # The ledger adapter is never allowed to fall back to that capability
        # token. Enrollment is still recorded as the last broker call before
        # the marker is used, so only a failure that happens after enrollment
        # (a failed marker create) can require recovery; that state is what
        # `reenroll` and the enrollment retention window exist for.
        if hosted_session_ledger:
            session_token, session_state, _owner = _open_broker_session(
                self.broker,
                exchange_input,
                repository_id=repository_id,
                pull_request=pull_request,
                head_sha=head_sha,
            )
        ledger = self._session_ledger_for_token(
            session_token if session_token is not None else token,
            options=options,
            app_slug=app_slug,
        )
        _require_present_marker(ledger, identity, session_state)
        if (
            transaction_record is None
            and ledger is not None
            and isinstance(result, ReviewResult)
            and result.transaction is not None
        ):
            transaction_record = load_transaction(ledger)
            transaction_ledger = ledger
            if (
                transaction_record.transaction is not None
                and transaction_record.transaction.phase == "publication_succeeded"
            ):
                return PublicationResult(
                    status="already_published",
                    diagnostic="transaction-publication-complete",
                )
        if (
            transaction_record is not None
            and transaction_ledger is not None
            and ledger is not transaction_ledger
        ):
            if ledger is None:
                raise GitHubPublicationError(
                    "identity-bound review publication context is incomplete"
                )
            # A local preflight ledger is not authoritative once the broker
            # selects a different token-bound ledger.  Revalidate the result
            # against that ledger before rebinding or short-circuiting.
            transaction_record = load_transaction(ledger)
            if (
                transaction_record.transaction is not None
                and transaction_record.transaction.phase == "publication_succeeded"
            ):
                return PublicationResult(
                    status="already_published",
                    diagnostic="transaction-publication-complete",
                )
        if (
            transaction_record is not None
            and transaction_record.transaction is not None
        ):
            durable_transaction = transaction_record.transaction
            if result.transaction is None:
                raise GitHubPublicationError(
                    "durable transaction requires an identity-bound result"
                )
            if (
                durable_transaction.result_sha256 is None
                or result.content_digest() != durable_transaction.result_sha256
            ):
                raise GitHubPublicationError(
                    "review result digest does not match durable transaction"
                )
            if result.transaction != durable_transaction:
                result = replace(result, transaction=durable_transaction)
        if (
            transaction_record is not None
            and transaction_record.transaction is not None
            and transaction_record.transaction.phase == "publication_succeeded"
        ):
            return PublicationResult(
                status="already_published",
                diagnostic="transaction-publication-complete",
            )
        prepared = None
        reservation = session_reservation_id(
            repository=repository,
            pull_request=pull_request,
            head_sha=head_sha,
            kind="publish",
        )
        flags = {
            "coverage_complete": False,
            "independently_approval_eligible": False,
            "latest_head_reviewed": False,
            "no_progress": False,
        }
        if isinstance(result, ReviewResult):
            flags = _publication_round_flags(result)
        if (
            ledger is not None
            and transaction_record is None
            and not (
                isinstance(result, ReviewResult) and result.transaction is not None
            )
        ):
            if not isinstance(head_sha, str) or not head_sha.strip():
                raise GitHubPublicationError(
                    "session ledger requires a non-empty head_sha"
                )
            try:
                prepared = prepare_session_round(
                    ledger,
                    identity,
                    policy,
                    reservation_id=reservation,
                    head_sha=head_sha,
                    coverage_complete=flags["coverage_complete"],
                    independently_approval_eligible=flags[
                        "independently_approval_eligible"
                    ],
                    latest_head_reviewed=flags["latest_head_reviewed"],
                )
                if should_skip_automation(prepared.decision, inference=False):
                    return _with_shadow(
                        PublicationResult(
                            status="handoff",
                            diagnostic=admission_diagnostic(prepared.decision),
                        ),
                        _shadow_observation(_shadow_state(prepared, flags)),
                    )
            except BaseException as preparation_error:
                cleanup_error = self._abort_held_session_reservation(
                    ledger, identity, reservation
                )
                if cleanup_error is not None:
                    preparation_error.add_note(
                        "session reservation cleanup failed: "
                        f"{type(cleanup_error).__name__}: "
                        f"{str(cleanup_error).replace(chr(10), ' ')[:160]}"
                    )
                raise
        if isinstance(result, ReviewResult) and result.transaction is not None:
            if ledger is None:
                raise GitHubPublicationError(
                    "identity-bound review publication requires a session ledger"
                )
            if transaction_record is None:
                if (
                    expected_configuration_digest is None
                    or expected_evidence_digest is None
                ):
                    raise GitHubPublicationError(
                        "identity-bound review publication context is incomplete"
                    )
            if (
                expected_configuration_digest is None
                or expected_evidence_digest is None
            ):
                raise GitHubPublicationError(
                    "identity-bound review publication context is incomplete"
                )
            if transaction_record is None:
                transaction_record = load_transaction(ledger)
            if transaction_record.transaction is not None:
                result = replace(result, transaction=transaction_record.transaction)
        validated_transaction_recovery = (
            # A pending identity-bound result has already crossed the F1
            # checkpoint and digest gate. It is the explicit transaction
            # recovery path, so it does not need to reconstruct F3 admission
            # state before replaying the same publication.
            isinstance(result, ReviewResult)
            and result.transaction is not None
            and transaction_record is not None
            and transaction_record.transaction is not None
            and transaction_record.transaction.result_sha256 is not None
            and result.content_digest() == transaction_record.transaction.result_sha256
        )
        if (
            validated_transaction_recovery
            and transaction_record is not None
            and transaction_record.transaction is not None
            and transaction_record.transaction.phase == "publication_suppressed"
        ):
            return PublicationResult(
                status="handoff",
                diagnostic="no_progress",
                transaction_id=transaction_record.transaction.transaction_id,
                generation=transaction_record.generation,
            )
        baseline_admission_required = (
            operator_baseline_requested and not validated_transaction_recovery
        )
        # A validated transaction is enough to replay an initial checkpoint,
        # but it does not carry the *prior* baseline needed to classify a later
        # verification result.  Once a verification round has already been
        # completed, publishing without both trusted admission inputs would
        # silently downgrade the result to a fresh review.
        verification_transaction_recovery_required = (
            validated_transaction_recovery
            and transaction_record is not None
            and transaction_record.completed_verification_rounds > 0
            and (baseline is None or current_key is None)
        )
        authorized_dispositions: tuple[object, ...] = ()
        durable_baseline = baseline
        baseline_recovery_required = False
        if ledger is not None:
            from ...disposition import session_dispositions

            if prepared is not None:
                authorized_dispositions = session_dispositions(prepared.record)
            elif transaction_record is not None:
                authorized_dispositions = session_dispositions(transaction_record)
            if baseline_admission_required:
                record_for_baseline = (
                    prepared.record if prepared is not None else transaction_record
                )
                if (
                    durable_baseline is None
                    and record_for_baseline is not None
                    and record_for_baseline.completed_initial_reviews > 0
                ):
                    history = record_for_baseline.convergence_history
                    if not (
                        isinstance(history, Mapping)
                        and history.get("state") == "completed"
                    ):
                        baseline_recovery_required = True
                    else:
                        try:
                            durable_baseline = read_session_baseline(
                                ledger, record_for_baseline
                            )
                            if durable_baseline is None:
                                baseline_recovery_required = True
                        except ReviewInputError:
                            baseline_recovery_required = True
        if (
            operator_mode
            and ledger is not None
            and (not isinstance(result, ReviewResult) or result.transaction is None)
            and durable_baseline is None
            and not baseline_recovery_required
            and baseline is None
            and current_key is None
        ):
            # F3 admission is bound to the durable transaction and its
            # marker. A fresh operator-ledger call without a transaction or
            # trusted baseline cannot safely infer or suppress a review, so
            # release any reservation and hand off for the caller to create
            # the identity-bound transaction first.
            if prepared is not None and prepared.reservation_id is not None:
                cleanup_error = self._abort_held_session_reservation(
                    ledger, identity, prepared.reservation_id
                )
                if cleanup_error is not None:
                    raise GitHubPublicationError(
                        "operator transaction is unavailable and reservation "
                        "cleanup failed"
                    ) from cleanup_error
            return _with_shadow(
                PublicationResult(
                    status="handoff",
                    diagnostic="identity-bound transaction required",
                ),
                _shadow_observation(_shadow_state(prepared, flags)),
            )
        # A persisted baseline is not self-authenticating for a new head: the
        # caller must supply the independently constructed current context key.
        # Falling back to the prior key would treat an unknown head/configuration
        # as compatible and turn stale evidence into admission authority.  Do
        # not silently downgrade to a fresh review when the caller omitted the
        # key; make the recovery requirement visible and retryable instead.
        publication_related_paths: Sequence[str]
        if related_paths is None and durable_baseline is not None:
            # An omitted scope must be derived from the current head. The
            # publisher separately unions this fresh impact set with the
            # persisted baseline scope; borrowing the old scope here would
            # make a stale publication input look current at the boundary.
            current_changed_paths = changed_paths
            if current_changed_paths is None:
                try:
                    current_changed_paths = analyze_diff(diff).changed_paths
                except ReviewInputError as exc:
                    raise GitHubPublicationError(
                        "review diff failed validation"
                    ) from exc
            try:
                publication_related_paths = related_paths_for_change(
                    current_changed_paths
                )
            except ReviewInputError as exc:
                raise GitHubPublicationError(
                    "review related-path scope failed validation"
                ) from exc
        else:
            # An explicit empty tuple is a deliberate narrow scope. Do not
            # silently widen it with paths persisted for an earlier head.
            publication_related_paths = () if related_paths is None else related_paths
        try:
            prepared_publishable: PublishableReview | None = None
            if (
                operator_mode
                and ledger is not None
                and transaction_record is not None
                and transaction_record.transaction is not None
                and (
                    callable(getattr(self.reviewer, "prepare", None))
                    or not validated_transaction_recovery
                    or result.review_status == "partial"
                )
            ):
                prepare = getattr(self.reviewer, "prepare", None)
                # This is the sole post-admission result: its effective
                # blockers are both recorded for F3 and passed unchanged to
                # the publisher below. A publisher adapter may expose only
                # ``publish``; use the shared preparation routine in that
                # case rather than silently dropping the caller's blocker
                # facts or bypassing admission.
                if callable(prepare):
                    prepared_publishable = prepare(
                        result=result,
                        diff=diff,
                        head_sha=head_sha,
                        candidates=candidates,
                        snapshot=snapshot,
                        snapshot_sha256=snapshot_sha256,
                        evidence_policy=evidence_policy,
                        convergence_policy=convergence_policy,
                        blocker_candidates=blocker_candidates,
                        input_blocker_candidates=input_blocker_candidates,
                        baseline=durable_baseline,
                        current_key=current_key,
                        changed_paths=changed_paths,
                        related_paths=publication_related_paths,
                        evidence_confirmed_concerns=evidence_confirmed_concerns,
                        authorized_dispositions=authorized_dispositions,
                    )
                else:
                    prepared_publishable = prepare_publication_review(
                        result=result,
                        diff=diff,
                        head_sha=head_sha,
                        candidates=candidates,
                        snapshot=snapshot,
                        snapshot_sha256=snapshot_sha256,
                        evidence_policy=evidence_policy,
                        convergence_policy=convergence_policy,
                        blocker_candidates=blocker_candidates,
                        input_blocker_candidates=input_blocker_candidates,
                        baseline=durable_baseline,
                        current_key=current_key,
                        changed_paths=changed_paths,
                        related_paths=publication_related_paths,
                        evidence_confirmed_concerns=evidence_confirmed_concerns,
                        authorized_dispositions=authorized_dispositions,
                    )
                if result.review_status == "complete":
                    current_blockers = blocker_set_digest(
                        tuple(
                            finding_lifecycle_for_comment(comment).fingerprint
                            for comment in prepared_publishable.result.comments
                            if comment.effective_blocking is True
                        )
                    )
                    prior_markers = convergence_progress_blocker_markers(
                        transaction_record.convergence_history
                    )
                    prior_blockers = tuple(
                        (digest, count)
                        for digest, count, _transaction_id in prior_markers
                    )
                    if (
                        validated_transaction_recovery
                        and transaction_record.transaction
                    ):
                        history = transaction_record.convergence_history
                        progress = (
                            history.get("progress")
                            if isinstance(history, Mapping)
                            else None
                        )
                        if (
                            isinstance(progress, list)
                            and progress
                            and isinstance(progress[-1], Mapping)
                            and "blocker_set_sha256" in progress[-1]
                            and "blocker_count" in progress[-1]
                            and progress[-1].get("transaction_id")
                            == transaction_record.transaction.transaction_id
                        ):
                            # This result already has a durable blocker marker. It
                            # belongs to the recovery attempt being replayed, not
                            # to the prior round window used for no-progress.
                            # Filter by ownership rather than by a projected list
                            # position: lifecycle-only placeholders are omitted
                            # from ``prior_markers``.
                            prior_markers = tuple(
                                marker
                                for marker in prior_markers
                                if marker[2]
                                != transaction_record.transaction.transaction_id
                            )
                            prior_blockers = tuple(
                                (digest, count)
                                for digest, count, _transaction_id in prior_markers
                            )

                    def blocker_set_identity(value: tuple[str, int]) -> tuple[str, ...]:
                        """Wrap the canonical whole-set digest for the detector."""

                        # ``blocker_set_sha256`` already identifies the complete
                        # admitted set; it is not one finding fingerprint. The
                        # digest/count pair is storage metadata, so an empty
                        # admitted set must not become a synthetic singleton.
                        return (value[0],) if value[1] else ()

                    no_progress = detect_no_progress(
                        previous_blocking=(*blocker_set_identity(prior_blockers[-1]),)
                        if prior_blockers
                        else (),
                        current_blocking=blocker_set_identity(current_blockers),
                        earlier_blocking=(*blocker_set_identity(prior_blockers[-2]),)
                        if len(prior_blockers) >= 2
                        else (),
                    )
                    flags["no_progress"] = no_progress
                    durable_transaction = transaction_record.transaction
                    # Every admitted set is recorded, including an empty set. An
                    # empty set is progress, but retaining it in the bounded
                    # window is what lets a later A -> empty -> A regression be
                    # classified as oscillation.
                    admitted_record = record_admitted_blocker_progress(
                        ledger,
                        identity,
                        durable_transaction,
                        blocker_set_sha256=current_blockers[0],
                        blocker_count=current_blockers[1],
                        suppress_publication=no_progress,
                    )
                    if (
                        admitted_record.transaction is not None
                        and admitted_record.transaction.phase == "publication_succeeded"
                    ):
                        return PublicationResult(
                            status="already_published",
                            diagnostic="transaction-publication-complete",
                            transaction_id=admitted_record.transaction.transaction_id,
                            generation=admitted_record.generation,
                        )
                    if no_progress:
                        return _with_shadow(
                            PublicationResult(
                                status="handoff",
                                diagnostic="no_progress",
                                transaction_id=durable_transaction.transaction_id,
                                generation=admitted_record.generation,
                            ),
                            _shadow_observation(_shadow_state(prepared, flags)),
                        )
            publisher_has_prepare = callable(getattr(self.reviewer, "prepare", None))
            publisher_result = (
                prepared_publishable.result
                if prepared_publishable is not None and not publisher_has_prepare
                else result
            )
            review_policy = options.review_policy()
            publisher_arguments: dict[str, Any] = {
                "token": token,
                "repository": repository,
                "repository_id": repository_id,
                "pull_request": pull_request,
                "head_sha": head_sha,
                "base_branch": base_branch,
                "base_sha": base_sha,
                "result": publisher_result,
                "diff": diff,
                "app_slug": app_slug,
                # The policy mode is the single approval authority: the legacy
                # boolean is resolved into it, so a caller cannot ask for
                # approval while the mode withholds it or the reverse.
                "auto_approve": review_policy == "auto-approve",
                "reviews_policy": review_policy,
                "check_token": self._check_capability_token(exchange_input),
                "qualification": qualification_state_for_publication(
                    provider=result.provider,
                    model=result.model,
                    provider_identity=_provider_identity_from_context(
                        configuration_context
                    ),
                ),
                "candidates": candidates,
                "snapshot": snapshot,
                "snapshot_sha256": snapshot_sha256,
                "evidence_policy": evidence_policy,
                "convergence_policy": convergence_policy,
                "blocker_candidates": blocker_candidates,
                "input_blocker_candidates": input_blocker_candidates,
                "baseline": durable_baseline,
                "current_key": current_key,
                "changed_paths": changed_paths,
                "related_paths": publication_related_paths,
                "evidence_confirmed_concerns": evidence_confirmed_concerns,
                "authorized_dispositions": authorized_dispositions,
            }
            if publisher_has_prepare:
                publisher_arguments["prepared_review"] = prepared_publishable
            if retained_eligibility is not None:
                publisher_arguments.update(
                    retained_eligibility=retained_eligibility,
                    continuation_digest=continuation_digest,
                    source_guard=source_guard,
                )
            publication = (
                PublicationResult(
                    status="handoff",
                    diagnostic="durable_baseline_recovery_required",
                )
                if operator_baseline_requested
                and (
                    (baseline_admission_required and baseline_recovery_required)
                    or (durable_baseline is not None and current_key is None)
                    or verification_transaction_recovery_required
                )
                else self.reviewer.publish(**publisher_arguments)
            )
        except BaseException as publication_error:
            if (
                ledger is not None
                and isinstance(result, ReviewResult)
                and result.transaction is not None
            ):
                if not isinstance(publication_error, (KeyboardInterrupt, SystemExit)):
                    durable_transaction = (
                        transaction_record.transaction
                        if transaction_record is not None
                        else None
                    )
                    if durable_transaction is not None:
                        try:
                            complete_review_publication(
                                ledger,
                                identity,
                                durable_transaction,
                                published=False,
                            )
                        except BaseException as cleanup_error:
                            publication_error.add_note(
                                "transaction phase cleanup failed: "
                                f"{type(cleanup_error).__name__}: "
                                f"{str(cleanup_error).replace(chr(10), ' ')[:160]}"
                            )
                    else:
                        publication_error.add_note(
                            "transaction phase cleanup skipped: durable transaction unavailable"
                        )
            elif ledger is not None and policy.mode in OPERATOR_REVIEW_MODES:
                if isinstance(publication_error, (KeyboardInterrupt, SystemExit)):
                    if prepared is not None:
                        try:
                            complete_session_round(
                                ledger, identity, prepared, published=False
                            )
                        except BaseException as cleanup_error:
                            publication_error.add_note(
                                "session reservation cleanup failed: "
                                f"{type(cleanup_error).__name__}: "
                                f"{str(cleanup_error).replace(chr(10), ' ')[:160]}"
                            )
                else:
                    try:
                        record_session_failed_attempt(
                            ledger,
                            identity,
                            reservation_id=reservation,
                            head_sha=head_sha,
                        )
                    except BaseException as cleanup_error:
                        publication_error.add_note(
                            "session reservation cleanup failed: "
                            f"{type(cleanup_error).__name__}: "
                            f"{str(cleanup_error).replace(chr(10), ' ')[:160]}"
                        )
            elif ledger is not None and prepared is not None:
                try:
                    complete_session_round(ledger, identity, prepared, published=False)
                except BaseException as cleanup_error:
                    publication_error.add_note(
                        "session reservation cleanup failed: "
                        f"{type(cleanup_error).__name__}: "
                        f"{str(cleanup_error).replace(chr(10), ' ')[:160]}"
                    )
            raise
        if (
            ledger is not None
            and isinstance(result, ReviewResult)
            and result.transaction is not None
        ):
            success_statuses = {
                "published",
                "already_published",
                "already_approved",
                "already_changes_requested",
                "auto_approval_disabled",
            }
            durable_transaction = (
                transaction_record.transaction
                if transaction_record is not None
                else None
            )
            if durable_transaction is None:
                raise GitHubPublicationError(
                    "identity-bound publication has no durable transaction"
                )
            complete_review_publication(
                ledger,
                identity,
                durable_transaction,
                published=publication.status in success_statuses,
            )
        elif ledger is not None and prepared is not None:
            complete_session_round(
                ledger,
                identity,
                prepared,
                published=publication.status == "published",
            )
        return _with_shadow(
            publication,
            _shadow_observation(_shadow_state(prepared, flags)),
        )

    def apply_maintainer_command(
        self,
        *,
        options: GitHubWriteOptions,
        oidc_token: str | None,
        repository: str,
        repository_id: int,
        pull_request: int,
        head_sha: str,
        body: str,
        actor_login: str,
        actor_type: str = "User",
        association: str,
        app_slug: str,
        source_comment_id: int | None = None,
        session_attestation: Mapping[str, object] | None = None,
        actor_permission: str | None = None,
        finding_fingerprints: Sequence[str] | None = None,
        media_publisher: object | None = None,
        review_result: object | None = None,
        request_comment_id: int | None = None,
        operation_entry: str = "disabled",
        app_id: int | None = None,
        run_id: str | None = None,
        run_attempt: str = "1",
        operation_store: object | None = None,
        operation_parts: object | None = None,
    ):
        """Apply a maintainer command through its applicable authority boundary.

        A local injected ledger retains the existing operator-facing command
        seam.  In contrast, a hosted mutation must be reconstructed from the
        broker's current GitHub attestation: workflow event actor metadata is
        only an untrusted routing hint and never authorizes a remote write.
        """

        from ...disposition import (
            MaintainerCommandResult,
            apply_session_command,
            authorized_maintainer,
            authorized_override,
            parse_maintainer_command,
        )

        # Parse only the command spelling before choosing an authority path.
        # This placeholder deliberately prevents untrusted event actor/head
        # values from becoming persisted command identity in the hosted path.
        parsed_command = parse_maintainer_command(body, actor="untrusted")
        if parsed_command is None:
            return MaintainerCommandResult(
                action="status",
                applied=False,
                operator_paused=False,
                summary="not-a-command",
            )
        if (
            parsed_command.action != "status"
            and options.github_session_ledger
            and self.session_ledger is not None
        ):
            # An injected ledger is the explicit local authority seam. It must
            # never coexist with the hosted grant boundary, otherwise caller
            # supplied actor fields could reach a local mutation while the
            # caller claims to have selected broker-backed mode.
            raise GitHubPublicationError(
                "hosted maintainer mutations cannot use an injected local session ledger"
            )
        hosted_mutation = (
            parsed_command.action != "status" and options.github_session_ledger
        )
        command: MaintainerCommand | None = None
        session_grant: str | None = None
        broker_attestation: Mapping[str, object] | None = None
        if hosted_mutation:
            if not options.github_writes:
                return MaintainerCommandResult(
                    action=parsed_command.action,
                    applied=False,
                    operator_paused=False,
                    summary="writes_disabled",
                )
            if (
                isinstance(source_comment_id, bool)
                or not isinstance(source_comment_id, int)
                or source_comment_id <= 0
            ):
                raise GitHubPublicationError(
                    "hosted maintainer command requires a source comment identity"
                )
            if not isinstance(session_attestation, Mapping):
                raise GitHubPublicationError(
                    "hosted maintainer command requires a session attestation"
                )
            command_exchange_input = oidc_token or self.broker.request_oidc_token()
            grant = self.broker.authorize_session_mutation(
                command_exchange_input,
                repository_id=repository_id,
                pull_request=pull_request,
                head_sha=head_sha,
                session_attestation=session_attestation,
            )
            session_grant = getattr(grant, "grant", None)
            broker_attestation = getattr(grant, "attestation", None)
            returned_token = getattr(grant, "token", None)
            if not isinstance(returned_token, str) or not returned_token.strip():
                raise GitHubPublicationError(
                    "broker command authorization returned an invalid token"
                )
            token = returned_token
            if not isinstance(session_grant, str) or not session_grant.strip():
                raise GitHubPublicationError(
                    "broker command authorization returned an invalid grant"
                )
            command = _command_from_broker_attestation(
                body=body,
                attestation=broker_attestation,
                repository=repository,
                repository_id=repository_id,
                pull_request=pull_request,
                head_sha=head_sha,
                source_comment_id=source_comment_id,
                app_slug=app_slug,
            )
        else:
            command = parse_maintainer_command(
                body, actor=actor_login, head_sha=head_sha
            )
            if command is None:
                raise GitHubPublicationError("maintainer command parsing was unstable")
            if not authorized_maintainer(
                login=actor_login,
                user_type=actor_type,
                association=association,
                app_slug=app_slug,
            ):
                return MaintainerCommandResult(
                    action=command.action,
                    applied=False,
                    operator_paused=False,
                    summary="unauthorized",
                )
        if command is None:
            raise GitHubPublicationError("maintainer command reconstruction failed")
        if command.action == "approve-media" or command.finding_reference:
            attested_association = association
            if hosted_mutation and isinstance(broker_attestation, Mapping):
                attested = broker_attestation.get("association")
                if isinstance(attested, str):
                    attested_association = attested
            if not authorized_override(
                association=attested_association, permission=actor_permission
            ):
                return MaintainerCommandResult(
                    action=command.action,
                    applied=False,
                    operator_paused=False,
                    summary="unauthorized",
                )
        if (
            command.action == "approve-media"
            and operation_entry == "enabled"
            and operation_store is not None
        ):
            confirmer = getattr(media_publisher, "confirm", None)
            finalizer = getattr(media_publisher, "publish_mixed_approval", None)
            if (
                not callable(confirmer)
                or not callable(finalizer)
                or review_result is None
                or not isinstance(request_comment_id, int)
                or isinstance(source_comment_id, bool)
                or not isinstance(source_comment_id, int)
            ):
                return MaintainerCommandResult(
                    action="approve-media",
                    applied=False,
                    operator_paused=False,
                    summary="media approval requires the current review result",
                )
            media_digest = sha256(
                f"{repository_id}:{pull_request}:{head_sha}".encode()
            ).hexdigest()

            def approve_media() -> object:
                confirmer(
                    request_comment_id=request_comment_id,
                    source_comment_id=source_comment_id,
                    result=review_result,
                )
                return finalizer(
                    request_comment_id=request_comment_id,
                    result=review_result,
                )

            assessment = self._through_operation(
                operation_entry=operation_entry,
                operation_store=operation_store,
                operation_parts=operation_parts,
                repository_id=repository_id,
                pull_request=pull_request,
                app_id=app_id,
                run_id=run_id,
                run_attempt=run_attempt,
                trigger="verify",
                reservation_id=(
                    f"media:{repository_id}:{pull_request}:{source_comment_id}"
                ),
                attestation_digest=media_digest,
                repository=repository,
                oidc_token=oidc_token,
                delivery_id=f"media:{source_comment_id}",
                grant=f"media:{source_comment_id}",
                execute=approve_media,
                encode=_approval_bytes,
                decode=_approval_from_bytes,
            )
            approved = bool(
                getattr(getattr(assessment, "approval", None), "approved", False)
            )
            return MaintainerCommandResult(
                action="approve-media",
                applied=approved,
                operator_paused=False,
                summary=(
                    "media files approved for this head"
                    if approved
                    else "media approval withheld"
                ),
            )
        if command.action == "approve-media":
            approver = getattr(media_publisher, "approve_reviewed_media", None)
            if (
                not callable(approver)
                or review_result is None
                or not isinstance(request_comment_id, int)
                or isinstance(source_comment_id, bool)
                or not isinstance(source_comment_id, int)
            ):
                return MaintainerCommandResult(
                    action="approve-media",
                    applied=False,
                    operator_paused=False,
                    summary="media approval requires the current review result",
                )
            assessment = approver(
                request_comment_id=request_comment_id,
                source_comment_id=source_comment_id,
                result=review_result,
            )
            approved = bool(
                getattr(getattr(assessment, "approval", None), "approved", False)
            )
            return MaintainerCommandResult(
                action="approve-media",
                applied=approved,
                operator_paused=False,
                summary=(
                    "media files approved for this head"
                    if approved
                    else "media approval withheld"
                ),
            )
        identity = SessionIdentity(
            repository=repository,
            pull_request=pull_request,
            repository_id=repository_id,
        )
        # A local status read is already bounded by the injected ledger and
        # does not need a broker capability or a GitHub write opt-in. Hosted
        # ledgers still exchange below because the issue comment must be read
        # through the broker-owned installation token.
        ledger = (
            self.session_ledger
            if command.action == "status" and not options.github_session_ledger
            else None
        )
        if ledger is None:
            if command.action != "status" and not options.github_writes:
                return MaintainerCommandResult(
                    action=command.action,
                    applied=False,
                    operator_paused=False,
                    summary="writes_disabled",
                )
            if command.action == "status":
                if oidc_token is None:
                    raise GitHubPublicationError(
                        "hosted maintainer status requires a caller-supplied OIDC token"
                    )
                exchange_input = oidc_token
                capability = "review_status"
            elif hosted_mutation:
                # The broker-issued session grant is both the GitHub token and
                # the one-use authority for the following ledger mutation.
                # It is intentionally distinct from review_publish.
                capability = None
            else:
                # Every hosted mutation is broker-authorized. The caller may
                # supply the OIDC assertion, but it is never treated as a
                # capability token or as proof of maintainer identity.
                exchange_input = oidc_token or self.broker.request_oidc_token()
                capability = "review_publish"
            if not hosted_mutation:
                token = self.broker.exchange(
                    exchange_input,
                    capability=capability,
                )
            ledger = self._session_ledger_for_token(
                token,
                options=options,
                app_slug=app_slug,
                prefer_remote=command.action == "status",
                broker=self.broker if hosted_mutation else None,
                session_grant=session_grant,
                session_attestation=broker_attestation,
                head_sha=head_sha if hosted_mutation else None,
                actions_token_provider=(
                    lambda: self.broker.exchange(
                        command_exchange_input, capability="review_actions"
                    )
                )
                if hosted_mutation and command.action == "continue"
                else None,
            )
        if ledger is None:
            raise GitHubPublicationError("maintainer commands require a session ledger")
        grant_bound_ledger = getattr(ledger, "_broker", None) is not None
        if grant_bound_ledger and not callable(
            getattr(ledger, "initialize_with_mutation", None)
        ):
            raise GitHubPublicationError(
                "hosted maintainer mutations require atomic session initialization"
            )
        if (
            command.action == "verify"
            and operation_entry == "enabled"
            and operation_store is not None
        ):
            produced = self._through_operation(
                operation_entry=operation_entry,
                operation_store=operation_store,
                operation_parts=operation_parts,
                repository_id=repository_id,
                pull_request=pull_request,
                app_id=app_id,
                run_id=run_id,
                run_attempt=run_attempt,
                trigger="verify",
                delivery_id=f"verify:{source_comment_id or 0}:{head_sha}",
                grant=f"verify:{source_comment_id or 0}:{head_sha}",
                reservation_id=f"verify:{repository_id}:{pull_request}:{head_sha}",
                attestation_digest=sha256(
                    f"{repository_id}:{pull_request}:{head_sha}".encode()
                ).hexdigest(),
                repository=repository,
                oidc_token=oidc_token,
                execute=lambda: apply_session_command(
                    ledger,
                    identity,
                    command,
                    finding_fingerprints=finding_fingerprints,
                )[1],
                encode=_command_bytes,
                decode=_command_from_bytes,
            )
            return produced
        _record, result = apply_session_command(
            ledger,
            identity,
            command,
            finding_fingerprints=finding_fingerprints,
        )
        return result

    def _check_capability_token(self, exchange_input: str) -> str | None:
        """Request Checks: write for the merge gate, degrading to no gate.

        The gate is a separate authority from review publication, so a broker
        that does not grant it must not suppress the review itself. A missing
        token leaves the gate unpublished and reports that diagnostic. It does
        not prevent approval when the review otherwise qualifies.
        """

        try:
            return self.broker.exchange(exchange_input, capability="check_publish")
        except (GitHubBrokerClientError, GitHubHTTPTransientError):
            return None

    def _session_ledger_for_token(
        self,
        token: str,
        *,
        options: GitHubWriteOptions,
        app_slug: str | None = None,
        prefer_remote: bool = False,
        broker: BrokerClient | None = None,
        session_grant: str | None = None,
        session_attestation: Mapping[str, object] | None = None,
        head_sha: str | None = None,
        actions_token_provider: Callable[[], str] | None = None,
    ) -> SessionLedger | None:
        if self.session_ledger is not None and not prefer_remote:
            return self.session_ledger
        if not options.github_session_ledger:
            return None
        from .session_ledger import GitHubIssueCommentSessionLedger

        if self.http is None:
            raise GitHubPublicationError("GitHub session ledger requires HTTP")
        ledger = GitHubIssueCommentSessionLedger(
            self.http,
            token=token,
            app_slug=app_slug,
            broker=broker,
            session_grant=session_grant,
            session_attestation=session_attestation,
            head_sha=head_sha,
            actions_token_provider=actions_token_provider,
        )
        if session_grant is not None:
            self._bind_consumed_grant(ledger)
        return ledger

    def _bind_consumed_grant(self, ledger: Any) -> None:
        """Record a consumed grant digest while the legacy session path can still run."""

        from .operation_entry import consume_recorded_grant
        from .operation_host import InMemoryOperationStore

        store = getattr(self, "_consumed_grant_store", None)
        if not isinstance(store, InMemoryOperationStore):
            store = InMemoryOperationStore()
            self._consumed_grant_store = store

        def remember(grant: str) -> bool:
            return consume_recorded_grant(store, grant)

        ledger._remember_consumed_grant = remember

    @staticmethod
    def _abort_held_session_reservation(
        ledger: SessionLedger,
        identity: SessionIdentity,
        reservation_id: str,
    ) -> BaseException | None:
        """Best-effort cleanup when preparation fails after reserving.

        The helper revalidates ownership immediately before aborting. A full
        reload is intentional because preparation may fail after a remote
        reserve response has been applied; if another writer advanced the
        generation, abort returns a diagnostic and the caller preserves the
        original preparation error rather than masking it.
        """

        try:
            loaded = ledger.load(identity)
            if (
                loaded.status in {"ok", "migrated"}
                and loaded.record is not None
                and loaded.record.reservation_id == reservation_id
            ):
                ledger.abort(
                    identity,
                    reservation_id=reservation_id,
                    expected_generation=loaded.record.generation,
                )
        except BaseException as exc:
            # Return every cleanup failure. The caller preserves an original
            # interrupt while attaching a bounded note, instead of silently
            # hiding a stuck reservation.
            return exc
        return None

    def recover_review(
        self,
        *,
        options: GitHubWriteOptions,
        oidc_token: str | None,
        repository: str,
        repository_id: int,
        pull_request: int,
        head_sha: str,
        base_branch: str,
        base_sha: str,
        artifact: RecoveryArtifact,
        diff: str,
        app_slug: str,
        now=None,
        convergence_policy: ReviewConvergencePolicy | None = None,
    ) -> PublicationResult:
        """Publish a retained result without invoking a model or writing learnings."""

        artifact.validate(
            repository=repository,
            pull_request_number=pull_request,
            base_sha=base_sha,
            head_sha=head_sha,
            now=now,
        )
        if not options.github_writes or not options.auto_review:
            return PublicationResult(status="disabled")
        result = ReviewResult.from_dict(artifact.result)
        if result.review_status == "incomplete" or (
            result.review_status == "partial"
            and not review_analysis_checkpoint_eligible(result)
        ):
            raise ReviewInputError(
                "recovery artifact result is incomplete",
                diagnostic="recovery_artifact_incomplete",
            )
        if convergence_policy is not None and not isinstance(
            convergence_policy, ReviewConvergencePolicy
        ):
            raise GitHubPublicationError("review convergence policy is invalid")
        if (
            convergence_policy is not None
            and convergence_policy.enforcement == "publication"
        ):
            raise GitHubPublicationError(
                "operator-mode recovery cannot re-admit a serialized result"
            )
        exchange_input = oidc_token or self.broker.request_oidc_token()
        token = self.broker.exchange(exchange_input, capability="review_publish")
        review_policy = options.review_policy()
        # A recovery publishes through the same gate as the run it replaces, so
        # the check conclusion always describes the review that is standing for
        # this head rather than whichever attempt happened to finish last.
        return self.reviewer.publish(
            token=token,
            repository=repository,
            repository_id=repository_id,
            pull_request=pull_request,
            head_sha=head_sha,
            base_branch=base_branch,
            base_sha=base_sha,
            result=result,
            diff=diff,
            app_slug=app_slug,
            auto_approve=review_policy == "auto-approve",
            reviews_policy=review_policy,
            check_token=self._check_capability_token(exchange_input),
            qualification=qualification_state_for_publication(
                provider=result.provider,
                model=result.model,
            ),
            convergence_policy=convergence_policy,
        )

    def publish_learning(
        self,
        *,
        options: GitHubWriteOptions,
        oidc_token: str | None,
        repository: str,
        repository_id: int,
        pull_request: int,
        head_sha: str,
        base_branch: str,
        base_sha: str,
        proposal,
    ) -> LearningPRResult:
        if not options.github_writes or not options.learning_prs:
            return LearningPRResult(status="disabled")
        token = self.broker.exchange(
            oidc_token or self.broker.request_oidc_token(),
            capability="learning_write",
        )
        return self.learner.propose(
            token=token,
            repository=repository,
            repository_id=repository_id,
            pull_request=pull_request,
            head_sha=head_sha,
            base_branch=base_branch,
            base_sha=base_sha,
            proposal=proposal,
        )

    def publish_learning_proposals(
        self,
        *,
        options: GitHubWriteOptions,
        oidc_token: str | None,
        repository: str,
        repository_id: int,
        pull_request: int,
        head_sha: str,
        base_branch: str,
        base_sha: str,
        result: ReviewResult,
    ) -> tuple[LearningPRResult, ...]:
        """Publish every validated proposal with one learning capability."""

        if (
            not options.github_writes
            or not options.learning_prs
            or not result.learning_proposals
        ):
            return ()
        token = self.broker.exchange(
            oidc_token or self.broker.request_oidc_token(),
            capability="learning_write",
        )
        return (
            self.learner.propose_batch(
                token=token,
                repository=repository,
                repository_id=repository_id,
                pull_request=pull_request,
                head_sha=head_sha,
                base_branch=base_branch,
                base_sha=base_sha,
                proposals=result.learning_proposals,
            ),
        )

    def publish_reply(
        self,
        *,
        options: GitHubWriteOptions,
        oidc_token: str | None,
        repository: str,
        pull_request: int,
        source_comment_id: int,
        source_updated_at: str,
        head_sha: str,
        reply,
        app_slug: str,
        root_comment_id: int,
        source_kind: str = "inline",
    ) -> ReplyResult:
        if not options.github_writes or not options.mention_replies:
            return ReplyResult(status="disabled")
        capability = "inline_reply" if source_kind == "inline" else "issue_reply"
        token = self.broker.exchange(
            oidc_token or self.broker.request_oidc_token(),
            capability=capability,
        )
        return self.replier.publish(
            token=token,
            repository=repository,
            pull_request=pull_request,
            source_comment_id=source_comment_id,
            source_updated_at=source_updated_at,
            head_sha=head_sha,
            reply=reply,
            app_slug=app_slug,
            root_comment_id=root_comment_id,
            source_kind=source_kind,
        )

    def _through_operation(
        self,
        *,
        operation_entry: str,
        operation_store: object | None,
        operation_parts: object | None,
        repository_id: int | None,
        pull_request: int,
        app_id: int | None,
        run_id: str | None,
        run_attempt: str,
        trigger: str,
        delivery_id: str,
        grant: str,
        reservation_id: str,
        attestation_digest: str,
        execute: Callable[[], object],
        encode: Callable[[object], bytes],
        decode: Callable[[bytes], object],
        repository: str | None = None,
        oidc_token: str | None = None,
    ) -> object:
        """Use the shared entry when this call has a store or Actions identity.

        A call without either keeps the previous function. ``disabled`` always
        keeps that function.
        """

        if operation_entry != "enabled":
            return execute()
        from .operation_comments import ResultPartStore
        from .operation_entry import call_operation_trigger
        from .operation_host import GitHubCommentOperationStore, InMemoryOperationStore

        store = operation_store
        parts = (
            operation_parts if isinstance(operation_parts, ResultPartStore) else None
        )
        if store is None and isinstance(self.http, GitHubHttp):
            ready = (
                isinstance(repository, str)
                and repository
                and isinstance(repository_id, int)
                and not isinstance(repository_id, bool)
                and repository_id > 0
                and isinstance(app_id, int)
                and not isinstance(app_id, bool)
                and app_id > 0
                and isinstance(run_id, str)
                and run_id.strip()
            )
            if (
                not ready
                or not isinstance(repository, str)
                or not isinstance(repository_id, int)
                or isinstance(repository_id, bool)
            ):
                return execute()
            token = self.broker.exchange(
                oidc_token or self.broker.request_oidc_token(),
                capability="review_session",
            )
            status, body = self.http.request("GET", "/user", token=token)
            user_id = body.get("id") if isinstance(body, dict) else None
            if (
                status != 200
                or not isinstance(body, dict)
                or body.get("type") != "Bot"
                or isinstance(user_id, bool)
                or not isinstance(user_id, int)
                or user_id <= 0
            ):
                raise ReviewInputError("operation entry refused: unavailable")
            from .operation_comments import GitHubIssueCommentOperationPort

            port = GitHubIssueCommentOperationPort(
                self.http,
                token=token,
                repository=repository,
                pull_request=pull_request,
                app_user_id=user_id,
            )
            store = GitHubCommentOperationStore(port, app_user_id=user_id)
            parts = ResultPartStore(port, app_user_id=user_id)
        if not isinstance(store, InMemoryOperationStore):
            return execute()
        if (
            not isinstance(repository_id, int)
            or isinstance(repository_id, bool)
            or repository_id <= 0
            or not isinstance(app_id, int)
            or isinstance(app_id, bool)
            or app_id <= 0
            or not isinstance(run_id, str)
            or not run_id.strip()
        ):
            raise ReviewInputError("operation entry refused: invalid")
        return call_operation_trigger(
            store,
            trigger=trigger,
            repository_id=repository_id,
            pull_request=pull_request,
            attestation_digest=attestation_digest,
            run_id=run_id.strip(),
            run_attempt=run_attempt.strip() or "1",
            app_id=app_id,
            reservation_id=reservation_id,
            delivery_id=delivery_id,
            grant=grant,
            execute=execute,
            encode=encode,
            decode=decode,
            parts=parts,
        )

    def generate_and_publish_reply(
        self,
        *,
        options: GitHubWriteOptions,
        oidc_token: str | None,
        read_token: str,
        repository: str,
        pull_request: int,
        source_comment_id: int,
        source_updated_at: str,
        expected_head_sha: str | None,
        reply_provider: ReviewProvider,
        model: str | None,
        app_slug: str,
        root_comment_id: int | None,
        source_kind: str = "inline",
        work_budgets: ReviewWorkBudgets | None = None,
        budget: ResourceBudget | None = None,
        work_recovery: WorkRecoveryStore | None = None,
        broader_service: ReviewService | None = None,
        operation_entry: str = "disabled",
        repository_id: int | None = None,
        app_id: int | None = None,
        run_id: str | None = None,
        run_attempt: str = "1",
        operation_store: object | None = None,
        operation_parts: object | None = None,
    ) -> ReplyResult:
        """Authorize, generate, validate, and publish one mention reply."""

        if not options.github_writes or not options.mention_replies:
            return ReplyResult(status="disabled")
        if (
            work_recovery is not None
            and work_recovery.enabled
            and not options.upload_artifacts
        ):
            raise ReviewInputError(
                "work recovery requires explicit diagnostics artifact policy"
            )
        if broader_service is not None and not options.auto_review:
            raise ReviewInputError(
                "broader discovery requires trusted full-review policy"
            )
        prepared = self.replier.prepare_context(
            token=read_token,
            repository=repository,
            pull_request=pull_request,
            source_comment_id=source_comment_id,
            source_updated_at=source_updated_at,
            app_slug=app_slug,
            root_comment_id=root_comment_id,
            source_kind=source_kind,
            expected_head_sha=expected_head_sha,
        )
        if not isinstance(prepared, PreparedConversation):
            return prepared
        capability_token = self.broker.exchange(
            oidc_token or self.broker.request_oidc_token(),
            capability=(
                "inline_reply" if prepared.source_kind == "inline" else "issue_reply"
            ),
        )
        reaction = self.replier.add_processing_reaction(
            token=capability_token,
            repository=repository,
            source_comment_id=prepared.source_comment_id,
            source_kind=prepared.source_kind,
        )
        try:
            # Adapter test seams may implement only the original conversation
            # interface. The real GitHub adapter shares one bounded transport.
            if isinstance(self.replier, ConversationPublisher):
                work_tracker = ResourceBudgetTracker(budget or ResourceBudget.create())
                assessor = HumanAssessmentPublisher(http=self.replier.http)
                human = assessor.prepare(
                    token=read_token,
                    repository=repository,
                    pull_request=pull_request,
                    prepared=prepared,
                    app_slug=app_slug,
                    work_budgets=work_budgets,
                    work_tracker=work_tracker,
                )
                if human is not None:
                    assert human.eligibility.human_review is not None
                    inventory = human.eligibility.human_review
                    work_assessment = None
                    if human.evidence_diagnostic:
                        assessment = HumanAssessmentReply(
                            body=f"Human reassessment has insufficient current diff evidence (reason: `{human.evidence_diagnostic}`). No findings were cleared; approval requirements remain unchanged. After the required evidence is available within the context budget, rerun a full review for the current head and submit a new authorized mention.",
                            decisions=(),
                        )
                    elif inventory.pending and human.evidence_bundle is not None:
                        evidence_bundle = human.evidence_bundle
                        produced = self._through_operation(
                            operation_entry=operation_entry,
                            operation_store=operation_store,
                            operation_parts=operation_parts,
                            repository_id=repository_id,
                            pull_request=pull_request,
                            app_id=app_id,
                            run_id=run_id,
                            run_attempt=run_attempt,
                            trigger="reassessment",
                            delivery_id=(
                                f"reassessment:{prepared.source_comment_id}:"
                                f"{prepared.source_updated_at}"
                            ),
                            grant=(
                                f"reassessment:{prepared.source_comment_id}:"
                                f"{prepared.source_updated_at}"
                            ),
                            reservation_id=(
                                f"reassessment:{repository_id}:{pull_request}:"
                                f"{prepared.source_comment_id}"
                            ),
                            attestation_digest=sha256(
                                f"{repository_id}:{pull_request}:{prepared.head_sha}".encode()
                            ).hexdigest(),
                            repository=repository,
                            oidc_token=oidc_token,
                            execute=lambda: reassess(
                                provider=reply_provider,
                                pending=inventory,
                                bundle=evidence_bundle,
                                source_body=human.source_body,
                                authority_digest=human.authority_digest,
                                work_budgets=work_budgets or ReviewWorkBudgets(),
                                model=model,
                                tracker=work_tracker,
                                recovery=work_recovery,
                                broader_service=broader_service,
                            ),
                            encode=_assessment_bytes,
                            decode=_assessment_from_bytes,
                        )
                        if isinstance(produced, HumanAssessmentWork):
                            work_assessment = produced
                            assessment = produced.reply
                        elif isinstance(produced, HumanAssessmentReply):
                            work_assessment = None
                            assessment = produced
                        else:
                            raise ReviewInputError(
                                "operation entry refused: result_missing"
                            )
                    elif inventory.pending:
                        produced_reply = self._through_operation(
                            operation_entry=operation_entry,
                            operation_store=operation_store,
                            operation_parts=operation_parts,
                            repository_id=repository_id,
                            pull_request=pull_request,
                            app_id=app_id,
                            run_id=run_id,
                            run_attempt=run_attempt,
                            trigger="reassessment",
                            delivery_id=(
                                f"reassessment-reply:{prepared.source_comment_id}:"
                                f"{prepared.source_updated_at}"
                            ),
                            grant=(
                                f"reassessment-reply:{prepared.source_comment_id}:"
                                f"{prepared.source_updated_at}"
                            ),
                            reservation_id=(
                                f"reassessment-reply:{repository_id}:{pull_request}:"
                                f"{prepared.source_comment_id}"
                            ),
                            attestation_digest=sha256(
                                f"{repository_id}:{pull_request}:{prepared.head_sha}".encode()
                            ).hexdigest(),
                            repository=repository,
                            oidc_token=oidc_token,
                            execute=lambda: HumanAssessmentService(
                                reply_provider
                            ).reply(
                                context=human.conversation.context,
                                pending=inventory,
                                source_body=human.source_body,
                                model=model,
                                tracker=work_tracker,
                            ),
                            encode=_assessment_bytes,
                            decode=_assessment_from_bytes,
                        )
                        if not isinstance(produced_reply, HumanAssessmentReply):
                            raise ReviewInputError(
                                "operation entry refused: result_missing"
                            )
                        assessment = produced_reply
                    else:
                        assessment = HumanAssessmentReply(
                            body="Retrying approval finalization for previously reassessed findings; all approval requirements still apply.",
                            decisions=(),
                        )
                    accepted = not inventory.pending or any(
                        item.decision != "unresolved" for item in assessment.decisions
                    )
                    if work_assessment is not None:
                        accepted = accepted or work_assessment.inventory != inventory
                    review_token = None
                    if accepted:
                        review_token = self.broker.exchange(
                            oidc_token or self.broker.request_oidc_token(),
                            capability="review_publish",
                        )
                    reply_outcome = assessor.publish(
                        token=capability_token,
                        review_token=review_token,
                        repository=repository,
                        pull_request=pull_request,
                        prepared=human,
                        reply=work_assessment
                        if work_assessment is not None
                        else assessment,
                        app_slug=app_slug,
                    )
                    if (
                        work_assessment is not None
                        and human.evidence_bundle is not None
                        and work_assessment.discovery is not None
                        and reply_outcome.status in {"replied", "already_replied"}
                    ):
                        if (
                            work_assessment.discovery.outcome.status != "reviewed"
                            or work_assessment.discovery.result is None
                        ):
                            return replace(
                                reply_outcome,
                                assessment_status="broader_review_pending",
                            )
                        updated_inventory = work_assessment.inventory.apply(
                            assessment.decisions
                        )
                        retained = replace(
                            human.eligibility,
                            human_review=updated_inventory,
                            facts=replace(
                                human.eligibility.facts,
                                has_human_adjudication_findings=bool(
                                    updated_inventory.pending
                                ),
                            ),
                        )

                        def source_guard() -> bool:
                            source = assessor._source(
                                token=read_token,
                                repository=repository,
                                pull_request=pull_request,
                                prepared=prepared,
                                app_slug=app_slug,
                            )
                            return assessor.source_matches(source, human)

                        if not source_guard():
                            return replace(
                                reply_outcome, status="skipped_edited_source"
                            )
                        status, current_pr = self.http.request(
                            "GET",
                            self.http.repository_path(
                                repository, f"/pulls/{pull_request}"
                            ),
                            token=read_token,
                        )
                        if (
                            status != 200
                            or not isinstance(current_pr, dict)
                            or not isinstance(current_pr.get("base"), dict)
                        ):
                            raise GitHubPublicationError(
                                "broader review publication identity is unavailable"
                            )
                        base = current_pr["base"]
                        repository_info = base.get("repo")
                        if (
                            not isinstance(repository_info, dict)
                            or not isinstance(base.get("ref"), str)
                            or type(repository_info.get("id")) is not int
                        ):
                            raise GitHubPublicationError(
                                "broader review publication identity is invalid"
                            )
                        publication = self.publish_review(
                            options=options,
                            oidc_token=oidc_token,
                            repository=repository,
                            repository_id=repository_info["id"],
                            pull_request=pull_request,
                            head_sha=prepared.head_sha,
                            base_sha=inventory.base_sha,
                            base_branch=base["ref"],
                            result=work_assessment.discovery.result,
                            diff="".join(
                                record.diff for record in human.evidence_bundle.records
                            ),
                            app_slug=app_slug,
                            retained_eligibility=retained,
                            continuation_digest=human.authority_digest,
                            source_guard=source_guard,
                        )
                        return replace(
                            reply_outcome,
                            assessment_status="broader_review_" + publication.status,
                            assessment_diagnostic=publication.diagnostic,
                        )
                    return reply_outcome
            reply = self._through_operation(
                operation_entry=operation_entry,
                operation_store=operation_store,
                operation_parts=operation_parts,
                repository_id=repository_id,
                pull_request=pull_request,
                app_id=app_id,
                run_id=run_id,
                run_attempt=run_attempt,
                trigger="reply",
                delivery_id=(
                    f"reply:{prepared.source_comment_id}:{prepared.source_updated_at}"
                ),
                grant=(
                    f"reply:{prepared.source_comment_id}:{prepared.source_updated_at}"
                ),
                reservation_id=(
                    f"reply:{repository_id}:{pull_request}:{prepared.source_comment_id}"
                ),
                attestation_digest=sha256(
                    f"{repository_id}:{pull_request}:{prepared.head_sha}".encode()
                ).hexdigest(),
                repository=repository,
                oidc_token=oidc_token,
                execute=lambda: ConversationService(reply_provider).reply(
                    prepared.context,
                    model=model,
                ),
                encode=_conversation_bytes,
                decode=_conversation_from_bytes,
            )
            if not isinstance(reply, ConversationReply):
                raise ReviewInputError("operation entry refused: result_missing")
            return self.replier.publish(
                token=capability_token,
                repository=repository,
                pull_request=pull_request,
                source_comment_id=prepared.source_comment_id,
                source_updated_at=prepared.source_updated_at,
                head_sha=prepared.head_sha,
                reply=reply,
                app_slug=app_slug,
                root_comment_id=prepared.root_comment_id,
                source_kind=prepared.source_kind,
            )
        finally:
            self.replier.remove_processing_reaction(
                token=capability_token,
                repository=repository,
                source_comment_id=prepared.source_comment_id,
                source_kind=prepared.source_kind,
                reaction_id=reaction.reaction_id,
            )


def _provider_identity_from_context(
    configuration_context: Mapping[str, object] | None,
) -> Mapping[str, object] | None:
    """Return the trusted provider document bound to a configuration context.

    Publication only trusts provider details that the analyzed run itself
    recorded, so an approval decision can never rest on ambient environment
    that merely happened to be set when the review was published.
    """

    if not isinstance(configuration_context, Mapping):
        return None
    provider = configuration_context.get("provider")
    return provider if isinstance(provider, Mapping) else None


def _open_broker_session(
    broker: BrokerClient,
    exchange_input: str | None,
    *,
    repository_id: int,
    pull_request: int,
    head_sha: str,
) -> tuple[str, str, Mapping[str, object] | None]:
    """Open one broker session witness and require a usable session token."""

    session = broker.open_session(
        exchange_input if exchange_input is not None else broker.request_oidc_token(),
        repository_id=repository_id,
        pull_request=pull_request,
        head_sha=head_sha,
    )
    token = session.token
    if not isinstance(token, str) or not token.strip():
        raise GitHubPublicationError(
            "hosted session ledger requires a broker-attested session token"
        )
    return token, session.state, getattr(session, "reservation_owner", None)


def _require_present_marker(
    ledger: SessionLedger | None,
    identity: SessionIdentity,
    session_state: str | None,
) -> None:
    """Fail closed when the broker witnessed a marker the ledger cannot find."""

    if (
        ledger is not None
        and session_state == "known"
        and ledger.load(identity).status == "missing"
    ):
        # The broker's authenticated witness says this session already
        # exists, so a missing marker means the comment was deleted. Fail
        # closed before any ledger call below can re-create it, and name
        # the recovery command so an operator does not have to infer it.
        raise GitHubPublicationError(
            "session ledger marker is missing; authenticated recovery is "
            "required: a maintainer must comment `@reviewsensei review reenroll` "
            "to re-establish this session"
        )


def resolve_hosted_session_ledger(
    *,
    broker: BrokerClient,
    http: GitHubHttp,
    oidc_token: str | None,
    repository: str,
    repository_id: int,
    pull_request: int,
    head_sha: str,
    app_slug: str | None = None,
) -> SessionLedger:
    """Open the broker-attested comment ledger for one hosted identity.

    Publication reads the identity-bound transaction from this durability
    adapter, and the analysis checkpoint writes it there, so both boundaries
    of one logical review resolve the same issue comment. A fresh hosted job
    therefore continues the rounds, baselines, and dispositions durable
    earlier jobs recorded instead of depending on a runner-local file that
    the next runner cannot read.
    """

    from .session_ledger import GitHubIssueCommentSessionLedger

    session_token, session_state, owner = _open_broker_session(
        broker,
        oidc_token,
        repository_id=repository_id,
        pull_request=pull_request,
        head_sha=head_sha,
    )
    # No mutation grant is bound here: this is an unattended analysis job, not
    # an attested maintainer command, so the ledger constructor takes only the
    # broker's head-scoped session token. That is the same authority the
    # publication boundary of the same logical review writes with.
    ledger = GitHubIssueCommentSessionLedger(
        http,
        token=session_token,
        app_slug=app_slug,
        reservation_owner=owner,
    )
    identity = SessionIdentity(
        repository=repository,
        pull_request=pull_request,
        repository_id=repository_id,
    )
    _require_present_marker(ledger, identity, session_state)
    return ledger


def _command_from_broker_attestation(
    *,
    body: str,
    attestation: object,
    repository: str,
    repository_id: int,
    pull_request: int,
    head_sha: str,
    source_comment_id: int,
    app_slug: str,
) -> MaintainerCommand:
    """Build a persisted command only from broker-attested GitHub identity."""

    from ...disposition import authorized_maintainer, parse_maintainer_command

    if not isinstance(attestation, Mapping):
        raise GitHubPublicationError("broker command attestation was invalid")
    if (
        attestation.get("repository") != repository
        or attestation.get("repository_id") != repository_id
        or attestation.get("pull_request") != pull_request
        or attestation.get("head_sha") != head_sha
        or attestation.get("operation") != "command"
        or attestation.get("source_comment_id") != source_comment_id
    ):
        raise GitHubPublicationError("broker command attestation scope was invalid")
    actor = attestation.get("actor")
    if not isinstance(actor, str) or not actor.strip():
        raise GitHubPublicationError("broker command attestation actor was invalid")
    actor_type = attestation.get("actor_type")
    association = attestation.get("association")
    if (
        not isinstance(actor_type, str)
        or actor_type.lower() != "user"
        or not isinstance(association, str)
        or association.upper() not in {"OWNER", "MEMBER", "COLLABORATOR"}
    ):
        raise GitHubPublicationError(
            "broker command attestation authorization fields were invalid"
        )
    command_id = attestation.get("command_id")
    if (
        isinstance(command_id, bool)
        or not isinstance(command_id, int)
        or command_id <= 0
    ):
        raise GitHubPublicationError(
            "broker command attestation command identity was invalid"
        )
    command_digest = attestation.get("command_digest")
    if (
        not isinstance(command_digest, str)
        or command_digest != sha256(body.encode("utf-8")).hexdigest()
    ):
        raise GitHubPublicationError("broker command attestation command was stale")
    command = parse_maintainer_command(
        body, actor=actor, head_sha=head_sha, command_id=str(command_id)
    )
    if command is None:
        raise GitHubPublicationError(
            "broker command attestation did not bind a command"
        )
    if not authorized_maintainer(
        login=actor,
        user_type=actor_type,
        association=association,
        app_slug=app_slug,
    ):
        raise GitHubPublicationError("broker command attestation was unauthorized")
    return command


def _shadow_state(prepared: object, flags: Mapping[str, bool]) -> RoundSessionState:
    if prepared is None:
        return RoundSessionState(
            coverage_complete=bool(flags.get("coverage_complete", False)),
            independently_approval_eligible=bool(
                flags.get("independently_approval_eligible", False)
            ),
            latest_head_reviewed=bool(flags.get("latest_head_reviewed", False)),
            no_progress=bool(flags.get("no_progress", False)),
        )
    extra = dict(flags)
    record = getattr(prepared, "record", None)
    decision = getattr(prepared, "decision", None)
    if record is not None and bool(getattr(record, "operator_paused", False)):
        extra["paused"] = True
    if decision is not None and getattr(decision, "handoff_reason", None) == "paused":
        extra["paused"] = True
    if record is not None and hasattr(record, "to_round_state"):
        return record.to_round_state(**extra)
    return RoundSessionState(
        coverage_complete=bool(extra.get("coverage_complete", False)),
        independently_approval_eligible=bool(
            extra.get("independently_approval_eligible", False)
        ),
        latest_head_reviewed=bool(extra.get("latest_head_reviewed", False)),
        no_progress=bool(extra.get("no_progress", False)),
        paused=bool(extra.get("paused", False)),
    )


def _shadow_observation(
    state: RoundSessionState,
) -> dict[str, object] | None:
    decision = observe_shadow_admission(state)
    if decision is None:
        return None
    return {
        "mode": decision.mode,
        "admit": decision.admit,
        "handoff": decision.handoff,
        "handoff_reason": decision.handoff_reason,
        "may_emit_approve": decision.may_emit_approve,
        "observation_only": True,
    }


def _with_shadow(
    result: PublicationResult, shadow: Mapping[str, object] | None
) -> PublicationResult:
    if not shadow:
        return result
    return PublicationResult(
        status=result.status,
        review_id=result.review_id,
        diagnostic=result.diagnostic,
        shadow=dict(shadow),
        transaction_id=result.transaction_id,
        generation=result.generation,
    )


def _publication_round_flags(result: ReviewResult) -> dict[str, bool]:
    coverage = coverage_approval_state(result.coverage)
    coverage_complete = result.coverage is None or coverage == "reviewed"
    eligible = (
        coverage_complete
        and result.review_status == "complete"
        and not has_blocking_findings(result)
        and not has_human_adjudication_findings(result)
    )
    return {
        "coverage_complete": coverage_complete,
        "independently_approval_eligible": eligible,
        # ReviewPublisher performs the exact-head preflight immediately before
        # publishing. A stale head returns a non-published result, and the
        # caller aborts the reservation rather than counting the round.
        "latest_head_reviewed": True,
        "no_progress": False,
    }
