"""Compose GitHub capabilities, publishers, and opt-ins for issue #64."""

from __future__ import annotations

import json
from collections.abc import Callable
from copy import copy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Mapping, Sequence, TypedDict, cast

from review_sensei.active_finding_window import (
    ActiveWindowRoundState,
    AuthorizedCoverageAcceptance,
    AuthorizedHumanOverride,
    ConfirmedFixEvidence,
    assert_no_deferred_leakage,
)
from review_sensei.baseline import ReviewBaseline
from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.budgets import ReviewWorkBudgets
from review_sensei.context import ReviewContextCacheKey, finding_lifecycle_for_comment
from review_sensei.convergence import (
    OPERATOR_REVIEW_MODES,
    BlockerCandidate,
    ReviewConvergencePolicy,
    RoundSessionState,
    detect_no_progress,
    observe_shadow_admission,
)
from review_sensei.conversation import ConversationService
from review_sensei.coverage import coverage_approval_state
from review_sensei.diff import analyze_diff
from review_sensei.disposition import MaintainerCommand
from review_sensei.errors import ReviewInputError
from review_sensei.finding_drift import (
    DriftEvaluationResult,
    FileDriftDescriptor,
    project_drift_review_result,
)
from review_sensei.human_assessment import (
    MAX_HUMAN_FINDINGS,
    HumanAssessmentDecision,
    HumanAssessmentReply,
    HumanAssessmentService,
    finding_instance_fingerprint,
)
from review_sensei.models import ConversationReply, ReviewResult, ReviewTransaction
from review_sensei.outcomes import (
    RecoveryArtifact,
    ResourceBudget,
    ResourceBudgetTracker,
)
from review_sensei.planning import related_paths_for_change
from review_sensei.providers.base import ReviewProvider
from review_sensei.reassessment_work import HumanAssessmentWork, reassess
from review_sensei.rescan import AuthorizedRescan
from review_sensei.scope import ContextRequest

if TYPE_CHECKING:
    from review_sensei.service import ReviewService
    from review_sensei.work_recovery import WorkRecoveryStore
from review_sensei.session import (
    SessionIdentity,
    SessionLedger,
    SessionLoadError,
    SessionLoadReason,
    admission_diagnostic,
    blocker_set_digest,
    complete_review_publication,
    complete_session_round,
    convergence_progress_blocker_markers,
    prepare_session_round,
    read_session_baseline,
    record_session_failed_attempt,
    review_analysis_checkpoint_eligible,
    session_reservation_id,
    should_skip_automation,
)
from review_sensei.verifier import CandidateFinding, PublishableReview

from .action_representability import guard_current_command_representability
from .approval import (
    ReviewApprovalEligibility,
    has_blocking_findings,
    has_human_adjudication_findings,
    qualification_state_for_publication,
)
from .artifact_authority import (
    ArchiveChainPartRef,
    ArtifactEvaluationDecision,
    ObservedGitHubArtifact,
    OperatorReenrollReceipt,
    OperatorReenrollRequest,
    evaluate_operator_reenroll,
    execute_restricted_operator_reenroll,
)
from .broker_client import BrokerClient
from .check_diagnostics import CheckCapability, check_failure, observed_http_status
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
    ReviewApprovalFinalizer,
    ReviewPublisher,
    prepare_publication_review,
)
from .session_ledger import GitHubIssueCommentSessionLedger, _marker_shaped
from .trust_transitions import (
    AuthenticatedTrustMigration,
    AuthenticatedTrustSnapshot,
    TrustMigrationReceipt,
    TrustTransitionDecision,
)


class _ConversationBaseBinding(TypedDict, total=False):
    context_base_sha: str | None
    context_base_ref: str | None


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
    operation_entry: str = "enabled"
    complete_inventory: bool = False
    max_active_findings: int | None = None
    enable_drift_tracking: bool = False

    def __post_init__(self) -> None:
        if self.max_active_findings is not None and (
            isinstance(self.max_active_findings, bool)
            or not isinstance(self.max_active_findings, int)
            or self.max_active_findings < 1
        ):
            raise ReviewInputError("max_active_findings must be a positive integer")
        if not isinstance(self.enable_drift_tracking, bool):
            raise ReviewInputError("enable_drift_tracking must be a boolean")

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


def _assessment_bytes(value: object, *, replay_context: str | None = None) -> bytes:
    reply = value.reply if isinstance(value, HumanAssessmentWork) else value
    if not isinstance(reply, HumanAssessmentReply):
        return b""
    body = {
        "body": reply.body,
        "decisions": [item.to_dict() for item in reply.decisions],
        "context_requests": [item.to_dict() for item in reply.context_requests],
        "replay_context": replay_context,
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class _RecordedHumanAssessmentReply(HumanAssessmentReply):
    """Validated reply bytes restored from an accepted operation result."""

    replay_context: str | None = None


def _assessment_from_bytes(payload: bytes) -> HumanAssessmentReply:
    loaded = json.loads(payload)
    if not isinstance(loaded, dict):
        raise ReviewInputError("operation entry refused: result_missing")
    decisions = loaded.get("decisions")
    requests = loaded.get("context_requests")
    if not isinstance(decisions, list) or not isinstance(requests, list):
        raise ReviewInputError("operation entry refused: result_missing")
    return _RecordedHumanAssessmentReply(
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
        replay_context=loaded.get("replay_context"),
    )


def _media_approval_fields(value: object) -> tuple[bool, tuple[str, ...]]:
    approval = getattr(value, "approval", None)
    approved = getattr(approval, "approved", None)
    raw_blockers = getattr(approval, "blockers", ())
    if (
        not isinstance(approved, bool)
        or not isinstance(raw_blockers, (tuple, list))
        or any(not isinstance(item, str) for item in raw_blockers)
        or (approved and bool(raw_blockers))
    ):
        raise GitHubPublicationError(
            "media approval assessment is unavailable or invalid"
        )
    return approved, tuple(raw_blockers)


def _approval_bytes(value: object) -> bytes:
    approved, blockers = _media_approval_fields(value)
    payload: dict[str, object] = {"approved": approved}
    if blockers:
        payload["blockers"] = blockers
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _approval_from_bytes(payload: bytes) -> object:
    loaded = json.loads(payload)
    if (
        not isinstance(loaded, dict)
        or not isinstance(loaded.get("approved"), bool)
        or set(loaded) - {"approved", "blockers"}
    ):
        raise GitHubPublicationError(
            "media approval assessment is unavailable or invalid"
        )
    approved = loaded["approved"]
    raw_blockers = loaded.get("blockers", ())

    class _Approval:
        def __init__(self) -> None:
            self.approved = approved
            self.blockers = raw_blockers

    class _Assessment:
        def __init__(self) -> None:
            self.approval = _Approval()

    assessment = _Assessment()
    _, blockers = _media_approval_fields(assessment)
    assessment.approval.blockers = blockers
    return assessment


def _command_bytes(value: object) -> bytes:
    from review_sensei.disposition import MaintainerCommandResult

    if not isinstance(value, MaintainerCommandResult):
        return b""
    payload = {
        "action": value.action,
        "applied": value.applied,
        "operator_paused": value.operator_paused,
        "summary": value.summary,
    }
    if value.receipts:
        payload["receipts"] = [
            {
                "reference": item.reference,
                "fingerprint": item.fingerprint,
                "outcome": item.outcome,
                "detail": item.detail,
            }
            for item in value.receipts
        ]
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _command_from_bytes(payload: bytes) -> object:
    from review_sensei.disposition import MaintainerCommandResult

    loaded = json.loads(payload)
    if not isinstance(loaded, dict):
        raise ReviewInputError("operation entry refused: result_missing")
    from review_sensei.disposition import OverrideReceipt

    raw_receipts = loaded.get("receipts", ())
    receipts: tuple[OverrideReceipt, ...] = ()
    if isinstance(raw_receipts, list):
        receipts = tuple(
            OverrideReceipt(
                reference=str(item.get("reference", "")),
                fingerprint=item.get("fingerprint")
                if isinstance(item.get("fingerprint"), str)
                else None,
                outcome=str(item.get("outcome", "unknown")),
                detail=str(item.get("detail", "")),
            )
            for item in raw_receipts
            if isinstance(item, dict)
        )
    return MaintainerCommandResult(
        action=str(loaded.get("action", "")),
        applied=loaded.get("applied") is True,
        operator_paused=loaded.get("operator_paused") is True,
        summary=str(loaded.get("summary", "")),
        receipts=receipts,
    )


class _PreparedReviewCapability:
    """A review_publish token minted by this application's own exchange.

    Tokens are opaque strings, so a finalizer cannot otherwise distinguish one
    this operation minted from a caller-supplied value. Binding the token to the
    broker instance and OIDC assertion it was exchanged for closes that gap
    without making the credential reusable by any other context. Redemption is
    idempotent for that same pair: an in-operation retry after a transient
    failure must not be refused because a token it never sent was already
    claimed.
    """

    __slots__ = ("_broker", "_assertion_digest", "_token")

    def __init__(self, broker: object, assertion: str, token: str) -> None:
        self._broker = broker
        self._assertion_digest = sha256(assertion.encode("utf-8")).digest()
        self._token = token

    def redeem(self, broker: object, assertion: str) -> str:
        if broker is not self._broker:
            raise GitHubPublicationError("prepared review capability broker mismatch")
        if (
            not isinstance(assertion, str)
            or sha256(assertion.encode("utf-8")).digest() != self._assertion_digest
        ):
            raise GitHubPublicationError(
                "prepared review capability assertion mismatch"
            )
        return self._token


class _PreparedCheckCapability:
    """Existing check_publish capability moved earlier, never minted twice."""

    __slots__ = ("_broker", "_assertion_digest", "_budget", "_token")

    def __init__(self, broker, assertion, budget, token):
        self._broker = broker
        self._assertion_digest = sha256(assertion.encode("utf-8")).digest()
        self._budget = budget
        self._token = token

    def redeem(self, broker, assertion, budget):
        if (
            broker is not self._broker
            or budget is not self._budget
            or not isinstance(assertion, str)
            or sha256(assertion.encode("utf-8")).digest() != self._assertion_digest
        ):
            raise GitHubPublicationError("prepared check capability scope changed")
        return self._token


@dataclass(frozen=True)
class _PreparedCommandFinalization:
    plan: Any
    check_capability: _PreparedCheckCapability
    reuse_lifecycle: bool


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
        enable_disposition_snapshot_writes: bool = False,
    ) -> None:
        self.broker = broker
        self.http = http
        self.reviewer = reviewer
        self._last_publication_result: PublicationResult | None = None
        self.learner = learner
        self.replier = replier
        self.session_ledger = session_ledger
        self.enable_disposition_snapshot_writes = enable_disposition_snapshot_writes
        self._finding_lifecycle_contexts: dict[
            tuple[int, int], tuple[Any, SessionIdentity, str, str]
        ] = {}
        self._quarantine_hold_readers: dict[tuple[str, int], Any] = {}
        self._human_inventory_readers: dict[tuple, Any] = {}
        self._historical_assessment_readers: dict[tuple[str, int], Any] = {}
        if getattr(
            getattr(session_ledger, "caller", None),
            "_has_quarantine_assessments",
            False,
        ):
            for publisher in (reviewer, replier):
                publisher.finalizer.quarantine_reader_factory = (
                    self._quarantine_hold_reader
                )
        if getattr(
            getattr(session_ledger, "caller", None),
            "_has_historical_assessments",
            False,
        ):
            for publisher in (reviewer, replier):
                publisher.finalizer.historical_assessment_reader_factory = (
                    self._historical_assessment_reader
                )
        # Every ordinary fresh application can reconstruct supported default
        # inventories from its independently selected durable ledger.
        for publisher in (reviewer, replier):
            if issubclass(type(publisher), (ReviewPublisher, ConversationPublisher)):
                publisher.finalizer.human_inventory_reader_factory = (
                    self._human_inventory_reader
                )

    def _quarantine_hold_reader(self, *, token, repository, pull_request, head_sha):
        from .quarantine_holds import QuarantineHoldReader

        key = repository, pull_request
        if key not in self._quarantine_hold_readers:
            self._quarantine_hold_readers[key] = QuarantineHoldReader(
                ledger=self.session_ledger,
                repository=repository,
                pull_request=pull_request,
            )
        return self._quarantine_hold_readers[key]

    def _historical_assessment_reader(
        self, *, token, repository, pull_request, head_sha
    ):
        from .historical_assessments import HistoricalAssessmentReader

        key = (repository, pull_request)
        if key not in self._historical_assessment_readers:
            self._historical_assessment_readers[key] = HistoricalAssessmentReader(
                ledger=self.session_ledger,
                http=self.http,
                repository=repository,
                pull_request=pull_request,
                token=token,
            )
        return self._historical_assessment_readers[key]

    def _human_inventory_reader(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        head_sha: str,
        app_slug: str,
        budget: EvidenceReadBudget,
    ) -> Any:
        import hashlib

        from .default_human_reader import DefaultHumanInventoryReader
        from .session_ledger import GitHubIssueCommentSessionLedger

        key = (
            hashlib.sha256(token.encode()).hexdigest(),
            repository,
            pull_request,
            head_sha,
            app_slug.casefold(),
            id(budget),
        )
        from .api_run_requests import ApiRunSessionLedger

        ledger = self.session_ledger
        native_scoped = (
            isinstance(ledger, ApiRunSessionLedger) and ledger._quarantine_required
        )
        if native_scoped and key in self._human_inventory_readers:
            return self._human_inventory_readers[key]
        if ledger is None:
            ledger = GitHubIssueCommentSessionLedger(
                self.http, token=token, app_slug=app_slug, evidence_budget=budget
            )
        reader = DefaultHumanInventoryReader(
            http=self.http,
            ledger=ledger,
            token=token,
            repository=repository,
            pull_request=pull_request,
            head_sha=head_sha,
            app_slug=app_slug,
            budget=budget,
        )
        # One application is one finite caller operation. Only the original
        # scoped context/immutable admission is shared; every native callback
        # still rereads its source, manifest, complete union and mutable fences.
        if native_scoped:
            self._human_inventory_readers[key] = reader
            current_read = self._quarantine_hold_readers.get((repository, pull_request))
            if current_read is not None:
                reader.attach_current_read(current_read)
        return reader

    def recover_retained_review_result(
        self,
        *,
        token: str,
        repository: str,
        repository_id: int,
        pull_request: int,
        base_sha: str,
        head_sha: str,
        app_slug: str,
        ledger: SessionLedger,
        configuration_context: Mapping[str, object],
        evidence_context: Mapping[str, object],
        convergence_policy: ReviewConvergencePolicy | None = None,
        budget: EvidenceReadBudget | None = None,
        recovery_phase: Any | None = None,
    ) -> ReviewResult:
        """Load exact same-PR bytes without inference or acceptance fabrication.

        The caller supplies its independently selected ledger and trusted
        publication inputs. Recovered bytes do not supply these expectations.
        Pass the same budget to the subsequent ``publish_review`` call.
        """
        from .publication_recovery import recover_retained_review_result

        try:
            recovered = recover_retained_review_result(
                http=self.http,
                token=token,
                repository=repository,
                repository_id=repository_id,
                pull_request=pull_request,
                base_sha=base_sha,
                head_sha=head_sha,
                app_slug=app_slug,
                ledger=ledger,
                configuration_context=configuration_context,
                evidence_context=evidence_context,
                convergence_policy=convergence_policy,
                budget=budget,
                recovery_phase=recovery_phase,
            )
        except (ReviewInputError, GitHubPublicationError) as exc:
            if isinstance(self.reviewer, ReviewPublisher):
                msg_lower = str(exc).lower()
                missing_part = any(
                    word in msg_lower
                    for word in ("missing", "absent", "not found", "unavailable")
                )
                self.reviewer.observe_artifact(
                    repository_id=repository_id,
                    pull_request=pull_request,
                    observed=ObservedGitHubArtifact(
                        artifact_kind="retained_result_part",
                        artifact_id=f"retained:{pull_request}:{head_sha}",
                        present=not missing_part,
                        duplicate_count=0 if missing_part else 1,
                        edited_by_human=not missing_part,
                    ),
                )
            raise
        if isinstance(self.reviewer, ReviewPublisher):
            from .finding_lifecycle import restore_finding_lifecycle

            restore_finding_lifecycle(
                self.reviewer,
                ledger,
                SessionIdentity(repository, pull_request, repository_id),
            )
            digest = recovered.content_digest()
            self.reviewer.observe_artifact(
                repository_id=repository_id,
                pull_request=pull_request,
                observed=ObservedGitHubArtifact(
                    artifact_kind="retained_result_part",
                    artifact_id=f"retained:{pull_request}:{head_sha}",
                    present=True,
                    expected_sha256=digest,
                    observed_sha256=digest,
                ),
            )
        return recovered

    def _bind_finding_lifecycle_finalizer(
        self,
        finalizer,
        *,
        options,
        repository,
        pull_request,
        head_sha,
        app_slug,
        token,
        repository_id=None,
        evidence_budget=None,
    ):
        """Bind the actual invocation consumer, never a cache-only substitute."""
        if not isinstance(self.reviewer, ReviewPublisher):
            return None
        ledger = self.session_ledger
        if ledger is None:
            if not options.github_session_ledger:
                return None  # Explicit standalone legacy projection profile.
            ledger = GitHubIssueCommentSessionLedger(
                self.http,
                token=token,
                app_slug=app_slug,
                evidence_budget=evidence_budget,
            )  # Read-only: no session mutation grant or writer is invented.
        identity = SessionIdentity(repository, pull_request, repository_id)
        selected = None
        if repository_id is None:
            selected = ledger.load(identity)
            if selected.record is not None:
                identity = selected.record.identity()
        from review_sensei.session import LocalSessionLedger

        # Match the producer's explicit standalone/local legacy profile. A local
        # transaction is not itself enrollment in active-window/drift storage.
        # Keep its ledger and existing human-inventory/finalizer authority; this
        # selects no lifecycle, reconstructs nothing and grants no new writer.
        # An existing reference or in-memory lifecycle state always wins over
        # disabled writes, so read-only callers cannot downgrade an enrollment.
        if (
            type(ledger) is LocalSessionLedger
            and type(self.reviewer) is ReviewPublisher
            and type(finalizer) is ReviewApprovalFinalizer
            and not options.github_session_ledger
            and ledger.enable_finding_lifecycle_writes is False
            and options.max_active_findings is None
            and not options.enable_drift_tracking
            and not any(
                getattr(owner, name, None)
                for owner in (self.reviewer, finalizer)
                for name in (
                    "_active_window_states",
                    "_active_window_inputs",
                    "_drift_states",
                    "_drift_inputs",
                    "_drift_concern_history",
                    "_native_finding_receipt_records",
                    "_trust_snapshots",
                    "_trust_decisions",
                    "_unknown_effect_holds",
                    "_response_loss_holds",
                    "_artifact_decisions",
                    "_finding_lifecycle_journal",
                    "_finding_lifecycle_consumer",
                )
            )
        ):
            selected = selected if selected is not None else ledger.load(identity)
            if selected.status not in {"ok", "migrated", "missing"}:
                raise ReviewInputError(
                    "finding lifecycle selected session is unavailable: "
                    + selected.status
                )
            if selected.record is None or selected.record.finding_lifecycle is None:
                return None
        from .finding_lifecycle_consumers import finding_lifecycle_consumer

        consumer = finding_lifecycle_consumer(
            self.reviewer,
            ledger,
            identity,
            head_sha=head_sha,
            app_slug=app_slug,
            review_token=token,
        )
        consumer.bind(finalizer)
        return consumer

    def active_window_state(
        self, *, repository_id: int, pull_request: int
    ) -> ActiveWindowRoundState | None:
        if isinstance(self.reviewer, ReviewPublisher):
            return self.reviewer.active_window_state(
                repository_id=repository_id, pull_request=pull_request
            )
        return None

    def active_window_public_status(
        self, *, repository_id: int, pull_request: int
    ) -> dict[str, object] | None:
        if isinstance(self.reviewer, ReviewPublisher):
            return self.reviewer.active_window_public_status(
                repository_id=repository_id, pull_request=pull_request
            )
        return None

    def _reconcile_durable_finding(
        self,
        *,
        mode,
        repository_id,
        pull_request,
        target,
        resolved_state,
        proof_sha256,
        repository=None,
        token=None,
        app_slug=None,
    ):
        selected = self._finding_lifecycle_contexts.get((repository_id, pull_request))
        ledger = self.session_ledger or (selected[0] if selected is not None else None)
        if ledger is None:
            return None
        if repository is None and selected is not None:
            identity = selected[1]
        elif repository is not None:
            identity = SessionIdentity(repository, pull_request, repository_id)
        else:
            raise ReviewInputError(
                "durable finding reconciliation requires selected repository identity"
            )
        from .finding_lifecycle_reconciliation import reconcile_finding_lifecycle

        return reconcile_finding_lifecycle(
            self.reviewer,
            ledger,
            identity,
            mode=mode,
            target=target,
            resolved_state=resolved_state,
            proof_sha256=proof_sha256,
            token=token or (selected[3] if selected is not None else None),
            app_slug=app_slug or (selected[2] if selected is not None else None),
        )

    def reconcile_active_window_slot(
        self,
        *,
        repository_id: int,
        pull_request: int,
        target: str,
        resolved_state: str,
        proof_sha256: str,
        repository: str | None = None,
        token: str | None = None,
        app_slug: str | None = None,
    ) -> ActiveWindowRoundState:
        if not isinstance(self.reviewer, ReviewPublisher):
            raise ReviewInputError(
                "active window reconciliation requires a ReviewPublisher"
            )
        durable = self._reconcile_durable_finding(
            mode="window",
            repository_id=repository_id,
            pull_request=pull_request,
            target=target,
            resolved_state=resolved_state,
            proof_sha256=proof_sha256,
            repository=repository,
            token=token,
            app_slug=app_slug,
        )
        if durable is not None:
            return durable
        return self.reviewer.reconcile_active_window_slot(
            repository_id=repository_id,
            pull_request=pull_request,
            target=target,
            resolved_state=resolved_state,
            proof_sha256=proof_sha256,
        )

    def drift_state(
        self, *, repository_id: int, pull_request: int
    ) -> DriftEvaluationResult | None:
        if isinstance(self.reviewer, ReviewPublisher):
            return self.reviewer.drift_state(
                repository_id=repository_id, pull_request=pull_request
            )
        return None

    def drift_public_status(
        self, *, repository_id: int, pull_request: int
    ) -> dict[str, object] | None:
        if isinstance(self.reviewer, ReviewPublisher):
            return self.reviewer.drift_public_status(
                repository_id=repository_id, pull_request=pull_request
            )
        return None

    def drift_baseline(
        self,
        *,
        repository_id: int,
        pull_request: int,
        baseline_template: ReviewBaseline,
    ) -> ReviewBaseline | None:
        if isinstance(self.reviewer, ReviewPublisher):
            return self.reviewer.drift_baseline(
                repository_id=repository_id,
                pull_request=pull_request,
                baseline_template=baseline_template,
            )
        return None

    def reconcile_drift_thread(
        self,
        *,
        repository_id: int,
        pull_request: int,
        thread_id: str,
        resolved_state: str,
        proof_sha256: str,
        repository: str | None = None,
        token: str | None = None,
        app_slug: str | None = None,
    ) -> DriftEvaluationResult:
        if not isinstance(self.reviewer, ReviewPublisher):
            raise ReviewInputError(
                "drift thread reconciliation requires a ReviewPublisher"
            )
        durable = self._reconcile_durable_finding(
            mode="drift",
            repository_id=repository_id,
            pull_request=pull_request,
            target=thread_id,
            resolved_state=resolved_state,
            proof_sha256=proof_sha256,
            repository=repository,
            token=token,
            app_slug=app_slug,
        )
        if durable is not None:
            return durable
        return self.reviewer.reconcile_drift_thread(
            repository_id=repository_id,
            pull_request=pull_request,
            thread_id=thread_id,
            resolved_state=resolved_state,
            proof_sha256=proof_sha256,
        )

    def artifact_decisions(
        self, *, repository_id: int, pull_request: int
    ) -> tuple[ArtifactEvaluationDecision, ...]:
        if isinstance(self.reviewer, ReviewPublisher):
            return self.reviewer.artifact_decisions(
                repository_id=repository_id, pull_request=pull_request
            )
        return ()

    def response_loss_hold(
        self, *, repository_id: int, pull_request: int, head_sha: str
    ) -> ArtifactEvaluationDecision | None:
        if isinstance(self.reviewer, ReviewPublisher):
            return self.reviewer.response_loss_hold(
                repository_id=repository_id,
                pull_request=pull_request,
                head_sha=head_sha,
            )
        return None

    def observe_artifact(
        self,
        *,
        repository_id: int,
        pull_request: int,
        observed: ObservedGitHubArtifact,
        identity: SessionIdentity | None = None,
        enrollment_witness_present: bool = False,
        has_active_reservation: bool = False,
    ) -> ArtifactEvaluationDecision:
        if not isinstance(self.reviewer, ReviewPublisher):
            raise ReviewInputError("artifact observation requires a ReviewPublisher")
        return self.reviewer.observe_artifact(
            repository_id=repository_id,
            pull_request=pull_request,
            observed=observed,
            identity=identity,
            enrollment_witness_present=enrollment_witness_present,
            has_active_reservation=has_active_reservation,
        )

    def verify_archive_chain(
        self,
        *,
        repository_id: int,
        pull_request: int,
        expected_chain: Sequence[ArchiveChainPartRef],
        observed_comments: Mapping[int, ObservedGitHubArtifact],
    ) -> ArtifactEvaluationDecision:
        if not isinstance(self.reviewer, ReviewPublisher):
            raise ReviewInputError(
                "archive chain verification requires a ReviewPublisher"
            )
        return self.reviewer.verify_archive_chain(
            repository_id=repository_id,
            pull_request=pull_request,
            expected_chain=expected_chain,
            observed_comments=observed_comments,
        )

    def reconcile_response_loss_hold(
        self,
        *,
        repository_id: int,
        pull_request: int,
        head_sha: str,
        observed: ObservedGitHubArtifact | None = None,
        absent_proof_sha256: str | None = None,
    ) -> ArtifactEvaluationDecision:
        if not isinstance(self.reviewer, ReviewPublisher):
            raise ReviewInputError(
                "response-loss reconciliation requires a ReviewPublisher"
            )
        return self.reviewer.reconcile_response_loss_hold(
            repository_id=repository_id,
            pull_request=pull_request,
            head_sha=head_sha,
            observed=observed,
            absent_proof_sha256=absent_proof_sha256,
        )

    def execute_operator_reenroll(
        self,
        *,
        ledger: GitHubIssueCommentSessionLedger,
        request: OperatorReenrollRequest,
        now: Any = None,
    ) -> tuple[Any, OperatorReenrollReceipt]:
        enriched = request
        if (
            isinstance(self.reviewer, ReviewPublisher)
            and type(request.identity.repository_id) is int
        ):
            rid = request.identity.repository_id
            pr = request.identity.pull_request
            active_blocking = max(
                request.active_blocking_obligations,
                self.reviewer.active_blocking_obligations_count(
                    repository_id=rid, pull_request=pr
                ),
            )
            has_uncertain = request.has_uncertain_write or any(
                hrid == rid and hpr == pr
                for (hrid, hpr, _sha) in self.reviewer._response_loss_holds
            )
            if (
                active_blocking != request.active_blocking_obligations
                or has_uncertain != request.has_uncertain_write
            ):
                enriched = replace(
                    request,
                    active_blocking_obligations=active_blocking,
                    has_uncertain_write=has_uncertain,
                )
        return execute_restricted_operator_reenroll(ledger, enriched, now=now)

    def enroll_trust_snapshot(
        self, *, snapshot: AuthenticatedTrustSnapshot
    ) -> AuthenticatedTrustSnapshot:
        if not isinstance(self.reviewer, ReviewPublisher):
            raise ReviewInputError(
                "trust snapshot enrollment requires a ReviewPublisher"
            )
        return self.reviewer.enroll_trust_snapshot(snapshot=snapshot)

    def trust_snapshot(
        self, *, repository_id: int, pull_request: int
    ) -> AuthenticatedTrustSnapshot | None:
        if isinstance(self.reviewer, ReviewPublisher):
            return self.reviewer.trust_snapshot(
                repository_id=repository_id, pull_request=pull_request
            )
        return None

    def trust_decisions(
        self, *, repository_id: int, pull_request: int
    ) -> tuple[TrustTransitionDecision, ...]:
        if isinstance(self.reviewer, ReviewPublisher):
            return self.reviewer.trust_decisions(
                repository_id=repository_id, pull_request=pull_request
            )
        return ()

    def has_unknown_trust_effect(
        self, *, repository_id: int, pull_request: int
    ) -> bool:
        if isinstance(self.reviewer, ReviewPublisher):
            return self.reviewer.has_unknown_trust_effect(
                repository_id=repository_id, pull_request=pull_request
            )
        return False

    def reconcile_unknown_trust_effect(
        self,
        *,
        repository_id: int,
        pull_request: int,
        reconciled_effect_sha256: str,
    ) -> None:
        if not isinstance(self.reviewer, ReviewPublisher):
            raise ReviewInputError(
                "unknown trust effect reconciliation requires a ReviewPublisher"
            )
        self.reviewer.reconcile_unknown_trust_effect(
            repository_id=repository_id,
            pull_request=pull_request,
            reconciled_effect_sha256=reconciled_effect_sha256,
        )

    def migrate_trust_boundary(
        self,
        *,
        repository_id: int,
        pull_request: int,
        target: AuthenticatedTrustSnapshot,
        migration: AuthenticatedTrustMigration,
        session_record: Any | None = None,
        inventory: Any | None = None,
        expected_transfer_attestation_sha256: str | None = None,
    ) -> TrustMigrationReceipt:
        if not isinstance(self.reviewer, ReviewPublisher):
            raise ReviewInputError("trust migration requires a ReviewPublisher")
        resolved_session = session_record
        if resolved_session is None and self.session_ledger is not None:
            enrolled = self.reviewer.trust_snapshot(
                repository_id=repository_id, pull_request=pull_request
            )
            if enrolled is not None:
                loaded = self.session_ledger.load(enrolled.to_session_identity())
                if loaded.record is not None:
                    resolved_session = loaded.record
        return self.reviewer.migrate_trust_boundary(
            repository_id=repository_id,
            pull_request=pull_request,
            target=target,
            migration=migration,
            session_record=resolved_session,
            inventory=inventory,
            expected_transfer_attestation_sha256=expected_transfer_attestation_sha256,
        )

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
        publication_budget: EvidenceReadBudget | None = None,
        max_active_findings: int | None = None,
        confirmed_fixes: Sequence[ConfirmedFixEvidence] = (),
        human_overrides: Sequence[AuthorizedHumanOverride] = (),
        coverage_acceptance: AuthorizedCoverageAcceptance | None = None,
        authorized_rescan: AuthorizedRescan | None = None,
        enable_drift_tracking: bool = False,
        drift_descriptors: Sequence[FileDriftDescriptor] = (),
        host_outdated_threads: frozenset[str] = frozenset(),
        host_resolved_threads: frozenset[str] = frozenset(),
        observed_artifacts: Sequence[ObservedGitHubArtifact] = (),
        archive_chain: tuple[
            Sequence[ArchiveChainPartRef], Mapping[int, ObservedGitHubArtifact]
        ]
        | None = None,
        observed_trust: AuthenticatedTrustSnapshot | None = None,
    ) -> PublicationResult:
        # Diagnostic receipt only; never reused as publication or funding authority.
        self._last_publication_result = None
        reviewer = self.reviewer
        resume_thread_cleanup = (
            isinstance(reviewer, ReviewPublisher) and options.complete_inventory
        )
        broker = self.broker
        if not options.github_writes or not options.auto_review:
            return PublicationResult(status="disabled")
        if not isinstance(result, ReviewResult):
            raise ReviewInputError("review publication requires a validated result")
        if result.publication_selection is not None:
            raise ReviewInputError(
                "publication selection is inactive; native revision protocol required"
            )
        if options.operation_entry not in {"enabled", "disabled"}:
            raise GitHubPublicationError("operation entry mode is invalid")
        if isinstance(reviewer, ReviewPublisher):
            trust_adm = reviewer.check_trust_boundary(
                repository_id=repository_id,
                pull_request=pull_request,
                phase="before_admission",
                operation="review",
                observed=observed_trust,
                repository_slug=repository,
                head_sha=head_sha,
                has_prior_charges=bool(
                    self.session_ledger is not None or options.github_session_ledger
                ),
            )
            if trust_adm is not None and not trust_adm.allowed:
                raise GitHubPublicationError(
                    f"repository trust transition refused ({trust_adm.phase}): {trust_adm.reason_code}"
                )
            if (
                reviewer.response_loss_hold(
                    repository_id=repository_id,
                    pull_request=pull_request,
                    head_sha=head_sha,
                )
                is not None
            ):
                raise GitHubPublicationError(
                    "review publication held uncertain after response loss; reconcile before retrying"
                )
            for observed_item in observed_artifacts:
                obs_decision = reviewer.observe_artifact(
                    repository_id=repository_id,
                    pull_request=pull_request,
                    observed=observed_item,
                )
                if not obs_decision.authority_intact:
                    raise GitHubPublicationError(
                        f"review artifact integrity refused: {obs_decision.reason_code}"
                    )
            if archive_chain is not None:
                expected_refs, observed_map = archive_chain
                chain_decision = reviewer.verify_archive_chain(
                    repository_id=repository_id,
                    pull_request=pull_request,
                    expected_chain=expected_refs,
                    observed_comments=observed_map,
                )
                if not chain_decision.authority_intact:
                    raise GitHubPublicationError(
                        f"archive chain integrity refused: {chain_decision.reason_code}"
                    )
        prior_budget = (
            reviewer.finalizer.evidence_budget or reviewer.finalizer._operation_budget
            if isinstance(reviewer, ReviewPublisher)
            else None
        )
        if (
            publication_budget is not None
            and prior_budget is not None
            and publication_budget is not prior_budget
        ):
            raise GitHubPublicationError("publication budget identity mismatches")
        budget = publication_budget or prior_budget
        if budget is None and (
            self.http is not None or isinstance(broker, BrokerClient)
        ):
            budget = EvidenceReadBudget()
        if budget is not None:
            budget.check()
            if budget.ordinary_remaining() <= 0:
                raise GitHubPublicationError(
                    "publication shared request budget exhausted"
                )
            if isinstance(broker, BrokerClient):
                if (
                    broker.before_request is not None
                    and broker.before_request != budget.consume
                ):
                    raise GitHubPublicationError(
                        "publication broker budget identity mismatches"
                    )
                broker = copy(broker)
                broker.before_request = budget.consume

        def check_capability() -> CheckCapability:
            capability = self._check_capability(
                exchange_input, broker=broker, accounting_context=check_context
            )
            if budget is not None:
                budget.check()
            return capability

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
        check_context = None
        transaction_ledger: SessionLedger | None = None
        preflight_source_ledger: SessionLedger | None = None
        expected_configuration_digest: str | None = None
        expected_evidence_digest: str | None = None

        def load_transaction(
            source_ledger: SessionLedger,
            *,
            commit_analysis: bool = True,
            loaded: Any = None,
        ):
            """Translate durable transaction validation into a publication error."""

            if (
                expected_configuration_digest is None
                or expected_evidence_digest is None
            ):
                raise GitHubPublicationError(
                    "identity-bound review publication context is incomplete"
                )
            try:
                from .empty_original_admission import (
                    load_transaction as select_transaction,
                )

                return select_transaction(
                    source_ledger,
                    identity,
                    result,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    policy_digest=policy.digest(),
                    configuration_digest=expected_configuration_digest,
                    evidence_digest=expected_evidence_digest,
                    baseline=baseline,
                    commit_analysis=commit_analysis,
                    loaded=loaded,
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
                preflight_source_ledger = self.session_ledger
                transaction_ledger = preflight_source_ledger
                if budget is not None:
                    from .publication_recovery import publication_session_ledger

                    transaction_ledger = publication_session_ledger(
                        transaction_ledger, budget
                    )
                transaction_record = load_transaction(
                    transaction_ledger, commit_analysis=False
                )
                if (
                    isinstance(reviewer, ReviewPublisher)
                    and transaction_record.finding_lifecycle is not None
                ):
                    from review_sensei.session import SessionLoadResult

                    from .finding_lifecycle import restore_finding_lifecycle

                    restore_finding_lifecycle(
                        reviewer,
                        transaction_ledger,
                        identity,
                        loaded=SessionLoadResult(
                            status="ok", record=transaction_record
                        ),
                    )
                if (
                    transaction_record.transaction is not None
                    and transaction_record.transaction.phase == "publication_succeeded"
                    and not resume_thread_cleanup
                    and not options.github_session_ledger
                ):
                    return PublicationResult(
                        status="already_published",
                        diagnostic="transaction-publication-complete",
                    )
        exchange_input = oidc_token or broker.request_oidc_token()
        if budget is not None:
            budget.check()
        # The publication capability is obtained before the session enrollment
        # is recorded. Enrollment is a durable broker-side witness that this
        # pull request already has a session marker, so recording it for a run
        # that never reaches publication would leave the next run seeing a
        # known witness with no marker, and require authenticated recovery
        # after nothing worse than a transient broker failure.
        token = broker.exchange(exchange_input, capability="review_publish")
        if budget is not None:
            budget.check()
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
                broker,
                exchange_input,
                repository_id=repository_id,
                pull_request=pull_request,
                head_sha=head_sha,
            )
        if budget is not None:
            budget.check()
        # Token exchange may select a different durable authority than the
        # injected preflight ledger. Always consult that selector; identity
        # checks below decide whether the selected transaction must be reloaded.
        select_ledger = self._session_ledger_for_token
        ledger = select_ledger(
            session_token if session_token is not None else token,
            options=options,
            app_slug=app_slug,
            evidence_budget=budget,
        )
        from review_sensei.session import SessionRecord

        from .api_run_requests import ApiRunRequestTransport, ApiRunSessionLedger
        from .publication_recovery import _PaginationObservationHttp

        # The built-in publication wrapper may copy the same run-owned ledger
        # twice. Reuse only its exact owned root, never a lookalike record or a
        # checkpoint that is still in flight. This changes no native/CAS fence.
        same_run_snapshot = (
            type(ledger) is ApiRunSessionLedger
            and type(transaction_ledger) is ApiRunSessionLedger
            and type(ledger.caller) is ApiRunRequestTransport
            and ledger.caller is transaction_ledger.caller
            and ledger.evidence_budget is ledger.caller.budget_view
            and transaction_record is not None
            and transaction_record.identity() == identity
            and transaction_record.record_sha256 == ledger.caller._root
            and SessionRecord.from_dict(transaction_record.to_dict()).record_sha256
            == ledger.caller._root
            and transaction_record is ledger.caller._effect_record
            and ledger.caller._checkpoint is None
            and ledger.caller._checkpoint_operation is None
            and ledger.caller._active_operation is None
            and ledger.caller._staged_part_operation is None
            and getattr(ledger.load, "__func__", None) is ApiRunSessionLedger.load
            and getattr(transaction_ledger.load, "__func__", None)
            is ApiRunSessionLedger.load
        )
        same_selected_authority = ledger is transaction_ledger or (
            isinstance(ledger, GitHubIssueCommentSessionLedger)
            and isinstance(transaction_ledger, GitHubIssueCommentSessionLedger)
            and (
                type(ledger) is GitHubIssueCommentSessionLedger
                and type(transaction_ledger) is GitHubIssueCommentSessionLedger
                or same_run_snapshot
            )
            and type(ledger.http) is _PaginationObservationHttp
            and type(transaction_ledger.http) is _PaginationObservationHttp
            and ledger.evidence_budget is budget
            and transaction_ledger.evidence_budget is budget
            and ledger.http.transport is transaction_ledger.http.transport
            and vars(ledger.http) == vars(transaction_ledger.http)
            and (vars(ledger) | {"http": None})
            == (vars(transaction_ledger) | {"http": None})
        )
        if (
            same_selected_authority
            and preflight_source_ledger is not None
            and self.session_ledger is preflight_source_ledger
            and transaction_ledger is not None
            and getattr(select_ledger, "__self__", None) is self
            and getattr(select_ledger, "__func__", None)
            is GitHubApplication._session_ledger_for_token
        ):
            # The built-in selector returns this exact injected authority,
            # possibly with a fresh metering wrapper. Keep the already-metered
            # identity only after selection succeeds. Custom/token-bound
            # selectors are never covered by this optimization.
            ledger = transaction_ledger
        if ledger is not None:
            self._finding_lifecycle_contexts[(repository_id, pull_request)] = (
                ledger,
                identity,
                app_slug,
                token,
            )
        reviewer_had_prior_round = isinstance(reviewer, ReviewPublisher) and (
            (repository_id, pull_request) in reviewer._active_window_states
            or (repository_id, pull_request) in reviewer._drift_states
        )
        effective_marker_state = (
            "known"
            if session_state == "known"
            or (
                reviewer_had_prior_round
                and isinstance(ledger, GitHubIssueCommentSessionLedger)
            )
            else session_state
        )
        present_loaded = _require_present_marker(
            ledger, identity, effective_marker_state, reviewer=reviewer
        )
        effective_max_active = (
            max_active_findings
            if max_active_findings is not None
            else options.max_active_findings
        )
        effective_drift = enable_drift_tracking or options.enable_drift_tracking
        # Current run-owned admission already grants this exact session writer
        # and funds it through the original physical caller. Selecting the
        # lifecycle selects its purpose-specific storage, not another allowance.
        from .api_run_requests import ApiRunSessionLedger

        if isinstance(ledger, ApiRunSessionLedger) and (
            effective_max_active is not None or effective_drift
        ):
            ledger.enable_finding_lifecycle_writes = True
        lifecycle_document = None
        lifecycle_record = None
        if isinstance(reviewer, ReviewPublisher) and ledger is not None:
            from review_sensei.session import SessionLoadResult

            from .finding_lifecycle import restore_finding_lifecycle

            lifecycle_loaded = present_loaded
            if (
                lifecycle_loaded is None
                and ledger is transaction_ledger
                and transaction_record is not None
            ):
                lifecycle_loaded = SessionLoadResult(
                    status="ok", record=transaction_record
                )
            try:
                lifecycle_record, lifecycle_document = restore_finding_lifecycle(
                    reviewer, ledger, identity, loaded=lifecycle_loaded
                )
            except ReviewInputError as exc:
                raise GitHubPublicationError(
                    "finding inventory session ledger load failed: " + str(exc)
                ) from exc
        if (
            lifecycle_document is not None
            and operator_baseline_requested
            and result.transaction is None
            and current_key is None
        ):
            # Restoring findings does not admit an unbound broader-review result.
            # Return the existing original-context handoff before validating an
            # incomplete new configuration or reserving another review round.
            return PublicationResult(
                status="handoff", diagnostic="durable_baseline_recovery_required"
            )
        if (
            isinstance(reviewer, ReviewPublisher)
            and ledger is not None
            and result.transaction is None
        ):
            # An unbound "full" result can omit every caller-supplied hint of
            # prior work. Consult the independently selected ledger instead of
            # treating that omission as proof of an initial finding lifecycle.
            # Do this before prepare_session_round: refusal must not reserve,
            # abort, replace, re-enroll or charge another review round.
            finding_key = (repository_id, pull_request)
            drift_requested = (
                effective_drift
                or finding_key in reviewer._drift_states
                or bool(drift_descriptors)
                or bool(host_outdated_threads)
                or bool(host_resolved_threads)
            )
            window_requested = (
                effective_max_active is not None
                or finding_key in reviewer._active_window_states
                or bool(confirmed_fixes)
                or bool(human_overrides)
                or coverage_acceptance is not None
                or authorized_rescan is not None
            )
            selected_state_missing = (
                finding_key not in reviewer._drift_states
                if drift_requested
                else finding_key not in reviewer._active_window_states
            )
            if (drift_requested or window_requested) and selected_state_missing:
                loaded_findings = (
                    present_loaded
                    if present_loaded is not None
                    else ledger.load(identity)
                )
                if loaded_findings.status not in {"ok", "migrated", "missing"}:
                    raise GitHubPublicationError(
                        "finding inventory session ledger load failed: "
                        f"{loaded_findings.status}"
                    )
                prior_record = loaded_findings.record
                if prior_record is not None and (
                    prior_record.completed_initial_reviews > 0
                    or prior_record.completed_verification_rounds > 0
                    or prior_record.convergence_history is not None
                    or prior_record.transaction is not None
                    or prior_record.publication_staging is not None
                ):
                    return PublicationResult(
                        status="handoff",
                        diagnostic="durable_finding_inventory_recovery_required",
                    )
        if (
            isinstance(reviewer, ReviewPublisher)
            and ledger is not None
            and getattr(ledger, "enable_finding_lifecycle_writes", False)
            and (effective_max_active is not None or effective_drift)
            and result.transaction is None
        ):
            # A legacy unbound review still needs its real effective settings
            # before admission can create a durable lifecycle operation. Refuse
            # before reserving a round, not after publishing native effects.
            if configuration_context is None:
                raise GitHubPublicationError(
                    "finding lifecycle publication requires trusted configuration context"
                )
            ReviewTransaction.compute_configuration_digest(configuration_context)
        if (
            transaction_record is None
            and ledger is not None
            and isinstance(result, ReviewResult)
            and result.transaction is not None
        ):
            transaction_record = load_transaction(
                ledger, commit_analysis=False, loaded=present_loaded
            )
            transaction_ledger = ledger
            if (
                transaction_record.transaction is not None
                and transaction_record.transaction.phase == "publication_succeeded"
                and not resume_thread_cleanup
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
            transaction_record = load_transaction(ledger, commit_analysis=False)
            if (
                transaction_record.transaction is not None
                and transaction_record.transaction.phase == "publication_succeeded"
                and not resume_thread_cleanup
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
            if durable_transaction.phase != "analysis":
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
            and not resume_thread_cleanup
        ):
            return PublicationResult(
                status="already_published",
                diagnostic="transaction-publication-complete",
            )
        terminal_cleanup_owner: tuple[ReviewTransaction, int] | None = None
        if (
            resume_thread_cleanup
            and transaction_record is not None
            and transaction_record.transaction is not None
            and transaction_record.transaction.phase == "publication_succeeded"
        ):
            # Terminal publication remains authoritative in either entry mode.
            # Only the independently selected ledger can establish this owner.
            # Native root/index/prose and all mutable authority fences still run.
            terminal_cleanup_owner = (
                transaction_record.transaction,
                transaction_record.generation,
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
                if isinstance(ledger, GitHubIssueCommentSessionLedger) and isinstance(
                    reviewer, ReviewPublisher
                ):
                    for cid, citem in ledger._comments_by_id.items():
                        cbody = citem.get("body")
                        if (
                            isinstance(cbody, str)
                            and _marker_shaped(cbody)
                            and citem.get("edited_by_human") is True
                        ):
                            reviewer.observe_artifact(
                                repository_id=repository_id,
                                pull_request=pull_request,
                                observed=ObservedGitHubArtifact(
                                    artifact_kind="session_ledger_marker",
                                    artifact_id=str(cid),
                                    present=True,
                                    edited_by_human=True,
                                ),
                                identity=identity,
                                has_active_reservation=True,
                            )
                            raise GitHubPublicationError(
                                "session ledger marker was edited by a human"
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
                if isinstance(reviewer, ReviewPublisher) and isinstance(
                    preparation_error, (SessionLoadError, ReviewInputError)
                ):
                    err_text = str(preparation_error)
                    if (
                        isinstance(preparation_error, SessionLoadError)
                        and preparation_error.reason == SessionLoadReason.CONFLICT
                    ) or "session ledger load failed: conflict" in err_text:
                        reviewer.observe_artifact(
                            repository_id=repository_id,
                            pull_request=pull_request,
                            observed=ObservedGitHubArtifact(
                                artifact_kind="session_ledger_marker",
                                artifact_id=f"session:{repository_id}:{pull_request}",
                                present=True,
                                duplicate_count=2,
                            ),
                            identity=identity,
                        )
                    elif "session ledger load failed: expired" in err_text:
                        reviewer.observe_artifact(
                            repository_id=repository_id,
                            pull_request=pull_request,
                            observed=ObservedGitHubArtifact(
                                artifact_kind="session_ledger_marker",
                                artifact_id=f"session:{repository_id}:{pull_request}",
                                present=True,
                                stale_generation=True,
                            ),
                            identity=identity,
                        )
                    elif (
                        isinstance(preparation_error, SessionLoadError)
                        and preparation_error.reason
                        == SessionLoadReason.INTEGRITY_FAILED
                    ) or "session ledger load failed: integrity-failed" in err_text:
                        reviewer.observe_artifact(
                            repository_id=repository_id,
                            pull_request=pull_request,
                            observed=ObservedGitHubArtifact(
                                artifact_kind="session_ledger_marker",
                                artifact_id=f"session:{repository_id}:{pull_request}",
                                present=True,
                                edited_by_human=True,
                            ),
                            identity=identity,
                        )
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
                transaction_record = load_transaction(ledger, commit_analysis=False)
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
            and (
                (
                    transaction_record.transaction.result_sha256 is not None
                    and result.content_digest()
                    == transaction_record.transaction.result_sha256
                )
                or (
                    transaction_record.transaction.phase == "analysis"
                    and result.transaction.result_sha256
                    in {None, result.content_digest()}
                )
            )
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
        from review_sensei.quarantine_lineage import is_full_analysis_transaction

        quarantine_full_analysis = (
            validated_transaction_recovery
            and transaction_record is not None
            and baseline is None
            and current_key is not None
            and is_full_analysis_transaction(transaction_record, current_key)
            and result.coverage_mode == "full"
        )
        verification_transaction_recovery_required = (
            lifecycle_document is None
            and not quarantine_full_analysis
            and validated_transaction_recovery
            and transaction_record is not None
            and (
                transaction_record.completed_verification_rounds > 0
                or transaction_record.reserved_slot == "verification"
            )
            and (baseline is None or current_key is None)
        )
        if (
            lifecycle_document is not None
            and transaction_record is not None
            and transaction_record.transaction is not None
        ):
            assert (
                lifecycle_record is not None
                and lifecycle_record.finding_lifecycle is not None
            )
            retained_binding = lifecycle_record.finding_lifecycle["manifest"]["binding"]
            current_transaction = transaction_record.transaction
            if (
                retained_binding["policy_digest"] != current_transaction.policy_digest
                or retained_binding["configuration_digest"]
                != current_transaction.configuration_digest
            ):
                return PublicationResult(
                    status="handoff",
                    diagnostic="durable_finding_inventory_recovery_required",
                )
        authorized_dispositions: tuple[object, ...] = ()
        durable_baseline = baseline
        baseline_recovery_required = False
        if ledger is not None:
            from review_sensei.disposition import session_dispositions

            if prepared is not None:
                authorized_dispositions = session_dispositions(
                    prepared.record, ledger=ledger
                )
            elif transaction_record is not None:
                authorized_dispositions = session_dispositions(
                    transaction_record, ledger=ledger
                )
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
        transport = self.http
        # Retention fences its first POST by advancing the durable phase. Capture
        # the original live admission before that intentional transition.
        may_start_human_inventory = bool(
            transaction_record is not None
            and transaction_record.transaction is not None
            and transaction_record.transaction.phase
            in {"analysis", "publication_pending"}
            and isinstance(result, ReviewResult)
        )
        result_retained = False
        lifecycle_custody_lost = False
        publisher_started = False
        admission_recovery_held = False
        try:
            unwrapped_ledger = getattr(ledger, "ledger", ledger)
            retention_context_required = (
                validated_transaction_recovery
                and (
                    options.operation_entry == "enabled"
                    or options.github_session_ledger
                )
                and self.http is not None
            )
            retention_required = (
                retention_context_required and terminal_cleanup_owner is None
            )
            if self.http is not None and (
                retention_context_required or publication_budget is not None
            ):
                from .publication_recovery import publication_http

                if budget is None:
                    raise GitHubPublicationError("publication budget is unavailable")
                transport = publication_http(self.http, budget)
                if isinstance(reviewer, ReviewPublisher):
                    reviewer = copy(reviewer)
                    reviewer.http = transport
                    reviewer.finalizer = copy(reviewer.finalizer)
                    reviewer.finalizer.http = transport
                    reviewer.finalizer.evidence_budget = budget
                    reviewer.checks = copy(reviewer.checks)
                    reviewer.checks.http = transport
            if (
                transaction_record is not None
                and transaction_record.transaction is not None
                and transaction_record.transaction.phase == "analysis"
                and ledger is not None
            ):
                if retention_required or (
                    isinstance(unwrapped_ledger, GitHubIssueCommentSessionLedger)
                    and self.http is not None
                    and configuration_context is not None
                    and evidence_context is not None
                ):
                    from .publication_recovery import retain_checkpoint_review_result

                    if configuration_context is None or evidence_context is None:
                        raise GitHubPublicationError(
                            "publication retention context is incomplete"
                        )
                    pending_preview = replace(
                        transaction_record.transaction.with_result(
                            result.content_digest()
                        ),
                        generation=transaction_record.generation + 1,
                    )
                    retain_checkpoint_review_result(
                        http=transport,
                        token=token,
                        result=result,
                        identity=identity,
                        base_sha=base_sha,
                        head_sha=head_sha,
                        app_slug=app_slug,
                        pending_transaction=pending_preview,
                        configuration_context=configuration_context,
                        evidence_context=evidence_context,
                        policy=policy,
                    )
                    result_retained = True
                transaction_record = load_transaction(ledger, commit_analysis=True)
                if transaction_record.transaction is not None:
                    if (
                        transaction_record.transaction.result_sha256
                        != result.content_digest()
                    ):
                        raise GitHubPublicationError(
                            "review result digest does not match durable transaction"
                        )
                    result = replace(result, transaction=transaction_record.transaction)
            if retention_required:
                from review_sensei.session import SessionLoadResult

                from .publication_recovery import retain_review_result

                if (
                    ledger is None
                    or configuration_context is None
                    or evidence_context is None
                ):
                    raise GitHubPublicationError(
                        "publication retention context is incomplete"
                    )
                retain_review_result(
                    http=transport,
                    token=token,
                    result=result,
                    identity=identity,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    app_slug=app_slug,
                    ledger=ledger,
                    configuration_context=configuration_context,
                    evidence_context=evidence_context,
                    policy=policy,
                    already_retained=result_retained,
                    _loaded=(
                        SessionLoadResult(status="ok", record=transaction_record)
                        if transaction_record is not None
                        and isinstance(
                            unwrapped_ledger, GitHubIssueCommentSessionLedger
                        )
                        else None
                    ),
                )
                result_retained = True
                transaction_record = load_transaction(ledger)
                if transaction_record.transaction is not None:
                    result = replace(result, transaction=transaction_record.transaction)
            from .empty_original_admission import consume as empty_original_admission

            omit_empty_original = (
                validated_transaction_recovery
                and result_retained
                and empty_original_admission(self, ledger, transaction_record, result)
            )
            admitted_inventory_store = None
            if (
                not omit_empty_original
                and retention_context_required
                and ledger is not None
                and budget is not None
                and result.transaction is not None
                and isinstance(reviewer, ReviewPublisher)
            ):
                from review_sensei.session import SessionLoadResult

                from .default_human_inventory import (
                    HumanInventoryStore,
                    build_human_inventory_context,
                )
                from .default_human_reader import (
                    restore_admitted_document,
                    transaction_publication_digest,
                )

                transaction = result.transaction
                context = build_human_inventory_context(
                    http=transport,
                    token=token,
                    ledger=ledger,
                    identity=identity,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    app_slug=app_slug,
                    publication_digest=transaction_publication_digest(transaction),
                    policy_digest=transaction.policy_digest,
                    configuration_digest=transaction.configuration_digest,
                    evidence_digest=transaction.evidence_digest,
                    budget=budget,
                    _loaded=(
                        SessionLoadResult(status="ok", record=transaction_record)
                        if transaction_record is not None
                        and isinstance(
                            unwrapped_ledger, GitHubIssueCommentSessionLedger
                        )
                        else None
                    ),
                )
                admitted_inventory_store = HumanInventoryStore(
                    transport, token, ledger, context
                )
                # An independently selected, digest-matched empty original
                # contains no per-comment admission flags to reconstruct. The
                # authenticated lifecycle is still required: its prior hidden
                # obligations must not be inferred empty from this new result.
                empty_original_with_retained_lifecycle = (
                    validated_transaction_recovery
                    and not result.comments
                    and lifecycle_document is not None
                    and lifecycle_record is not None
                    and lifecycle_record.finding_lifecycle is not None
                    and transaction_record is not None
                    and transaction_record.transaction is not None
                    and transaction_record.finding_lifecycle
                    == lifecycle_record.finding_lifecycle
                    and transaction_record.transaction.result_sha256
                    == result.content_digest()
                )
                if (
                    not may_start_human_inventory
                    and not empty_original_with_retained_lifecycle
                ):
                    admission_recovery_held = True
                    from .lifecycle_admission import initial_check_effects

                    available_admission = admitted_inventory_store.read_available()
                    if available_admission is not None:
                        result = restore_admitted_document(
                            result, admitted_inventory_store, available_admission[1]
                        )
                    elif (
                        validated_transaction_recovery
                        and 0 < len(result.comments) <= 2
                        and lifecycle_document is not None
                        and lifecycle_record is not None
                        and lifecycle_record.finding_lifecycle is not None
                        and lifecycle_record.transaction is not None
                        and transaction_record is not None
                        and transaction_record.transaction is not None
                        and transaction_record.finding_lifecycle
                        == lifecycle_record.finding_lifecycle
                        and transaction_record.transaction.result_sha256
                        == result.content_digest()
                        and lifecycle_record.transaction.logical_identity_matches(
                            transaction_record.transaction
                        )
                        and lifecycle_record.transaction.result_sha256
                        == transaction_record.transaction.result_sha256
                        and result.transaction is not None
                        and result.transaction.logical_identity_matches(
                            transaction_record.transaction
                        )
                        and not lifecycle_document["unknown_trust_effect"]
                        and not lifecycle_document["native_receipts"]
                        and initial_check_effects(
                            lifecycle_document["effects"], repository
                        )
                    ):
                        from review_sensei.finding_lifecycle import (
                            decode_lifecycle_state,
                        )

                        from .lifecycle_admission import (
                            restore_original_lifecycle_admission,
                        )

                        result = restore_original_lifecycle_admission(
                            result,
                            decode_lifecycle_state(
                                lifecycle_document["lifecycle"]
                            ).state,
                            head_sha=head_sha,
                            base_sha=base_sha,
                        )
                    else:
                        raise ReviewInputError(
                            "complete publication admission evidence is missing; retain the original result and do not infer clean obligations"
                        )
                    admission_recovery_held = False
            prepared_publishable: PublishableReview | None = None
            if (
                operator_mode
                and ledger is not None
                and transaction_record is not None
                and transaction_record.transaction is not None
                and (
                    callable(getattr(reviewer, "prepare", None))
                    or not validated_transaction_recovery
                    or result.review_status == "partial"
                )
            ):
                prepare = getattr(reviewer, "prepare", None)
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
                    from review_sensei.publication_intent_fusion import record_progress

                    admitted_record = record_progress(
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
                        if (
                            not isinstance(reviewer, ReviewPublisher)
                            or not options.complete_inventory
                        ):
                            return PublicationResult(
                                status="already_published",
                                diagnostic="transaction-publication-complete",
                                transaction_id=admitted_record.transaction.transaction_id,
                                generation=admitted_record.generation,
                            )
                        # A terminal result may still have optional old-thread work.
                        # Reconcile the complete native report before resuming it.
                        no_progress = False
                        flags["no_progress"] = False
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
            publisher_has_prepare = callable(getattr(reviewer, "prepare", None))
            publisher_result = (
                prepared_publishable.result
                if prepared_publishable is not None and not publisher_has_prepare
                else result
            )
            review_policy = options.review_policy()
            if isinstance(reviewer, ReviewPublisher):
                from .check_accounting import source_context

                check_context = source_context(
                    self.http, result, transaction_record, identity
                )
                reviewer.checks._accounting_context = check_context
            capability = check_capability()
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
                "check_token": capability.token,
                "check_capability_failure": capability.failure,
                "qualification": qualification_state_for_publication(
                    provider=result.provider,
                    model=result.model,
                    provider_identity=_provider_identity_from_context(
                        configuration_context
                    ),
                ),
                "configuration_context": configuration_context,
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
            if (
                isinstance(reviewer, ReviewPublisher)
                or effective_max_active is not None
                or confirmed_fixes
                or human_overrides
                or coverage_acceptance is not None
                or authorized_rescan is not None
                or effective_drift
                or drift_descriptors
                or host_outdated_threads
                or host_resolved_threads
            ):
                publisher_arguments.update(
                    max_active_findings=effective_max_active,
                    confirmed_fixes=confirmed_fixes,
                    human_overrides=human_overrides,
                    coverage_acceptance=coverage_acceptance,
                    authorized_rescan=authorized_rescan,
                    enable_drift_tracking=effective_drift,
                    drift_descriptors=drift_descriptors,
                    host_outdated_threads=host_outdated_threads,
                    host_resolved_threads=host_resolved_threads,
                    **(
                        {"observed_trust": observed_trust}
                        if isinstance(reviewer, ReviewPublisher)
                        and observed_trust is not None
                        else {}
                    ),
                    **(
                        {
                            "observed_artifacts": observed_artifacts,
                            "archive_chain": archive_chain,
                        }
                        if not isinstance(reviewer, ReviewPublisher)
                        and (observed_artifacts or archive_chain is not None)
                        else {}
                    ),
                )
            if (
                not options.complete_inventory
                and admitted_inventory_store is not None
                and isinstance(reviewer, ReviewPublisher)
            ):
                # Preserve independently admitted flags even if the legacy
                # root subsequently refuses its smaller inventory capacity.
                # Later complete-profile recovery must not guess from raw JSON.
                from review_sensei.human_assessment import PendingHumanReview
                from review_sensei.human_inventory import inventory_result_document

                from .publication import finding_index_marker, review_result_digest

                admitted = prepared_publishable or reviewer.prepare(
                    result=publisher_result,
                    diff=diff,
                    head_sha=head_sha,
                    convergence_policy=convergence_policy,
                )
                assembled_admission = reviewer._assemble_review_body(
                    result=admitted.result,
                    analysis=reviewer._validate_locations(admitted.result, diff),
                    convergence_policy=policy,
                    read_facts=lambda: reviewer._conversation_resolution_facts(
                        token=token, repository=repository, branch=base_branch
                    ),
                    auto_approve=review_policy == "auto-approve",
                    repository_id=repository_id,
                    pull_request=pull_request,
                    head_sha=head_sha,
                    base_sha=base_sha,
                )
                human = PendingHumanReview.from_result(admitted.result, base_sha)
                admitted_inventory_store.stage(
                    {
                        "human_inventory": inventory_result_document(
                            human, result_digest=review_result_digest(admitted.result)
                        )
                        if human is not None
                        else None,
                        "finding_index": finding_index_marker(
                            repository_id=repository_id,
                            pull_request=pull_request,
                            head_sha=head_sha,
                            base_sha=base_sha,
                            result_digest=review_result_digest(admitted.result),
                            entries=assembled_admission.index_entries,
                            complete_inventory=True,
                        ),
                        "visible_prose": [],
                    },
                    allow_start=may_start_human_inventory,
                )
            if options.complete_inventory:
                publisher_arguments["complete_inventory"] = True
                if admitted_inventory_store is not None:
                    publisher_arguments["human_inventory_store"] = (
                        admitted_inventory_store
                    )
                    publisher_arguments["allow_human_inventory_start"] = (
                        may_start_human_inventory and prepared_publishable is not None
                    )
            if transaction_record is not None:
                from review_sensei.rescan import is_rescan_transaction

                if is_rescan_transaction(transaction_record):
                    publisher_arguments["explicit_reanalysis"] = True
            if (
                isinstance(reviewer, ReviewPublisher)
                and options.complete_inventory
                and result_retained
                and retention_context_required
                and ledger is not None
                and budget is not None
                and result.transaction is not None
                and configuration_context is not None
                and evidence_context is not None
            ):
                from .publication_recovery import confirm_retained_narrative

                # Called only after ordinary complete-frame fitting fails. Read
                # the existing exact carriers using this invocation's meter;
                # compaction never stages replacement parts or resets work.
                publisher_arguments["narrative_archive"] = lambda: (
                    confirm_retained_narrative(
                        http=transport,
                        token=token,
                        result=publisher_result,
                        identity=identity,
                        base_sha=base_sha,
                        head_sha=head_sha,
                        app_slug=app_slug,
                        ledger=ledger,
                        configuration_context=configuration_context,
                        evidence_context=evidence_context,
                        policy=policy,
                        budget=budget,
                    )
                )
            if publisher_has_prepare:
                publisher_arguments["prepared_review"] = prepared_publishable
            if retained_eligibility is not None:
                publisher_arguments.update(
                    retained_eligibility=retained_eligibility,
                    continuation_digest=continuation_digest,
                    source_guard=source_guard,
                )
            if isinstance(reviewer, ReviewPublisher):
                reviewer = copy(reviewer)
                if (
                    ledger is not None
                    and (
                        effective_max_active is not None
                        or effective_drift
                        or lifecycle_document is not None
                    )
                    and getattr(ledger, "enable_finding_lifecycle_writes", False)
                ):
                    from .finding_lifecycle import FindingLifecycleJournal

                    lifecycle_loaded = ledger.load(identity)
                    if lifecycle_loaded.record is None:
                        raise ReviewInputError(
                            "finding lifecycle has no selected session"
                        )
                    selected_reference = (
                        None
                        if lifecycle_record is None
                        else lifecycle_record.finding_lifecycle
                    )
                    if lifecycle_loaded.record.finding_lifecycle != selected_reference:
                        lifecycle_custody_lost = True
                        raise ReviewInputError(
                            "finding lifecycle selected state changed before publication"
                        )
                    if (
                        prepared is not None
                        and lifecycle_loaded.record.transaction is None
                        and lifecycle_loaded.record.record_sha256
                        != prepared.record.record_sha256
                    ):
                        lifecycle_custody_lost = True
                        raise ReviewInputError(
                            "finding lifecycle prepared root changed before publication"
                        )
                    lifecycle_owner = lifecycle_loaded.record.transaction
                    expected_lifecycle_owner = result.transaction
                    if (
                        (lifecycle_owner is None) != (expected_lifecycle_owner is None)
                        or (
                            lifecycle_owner is not None
                            and expected_lifecycle_owner is not None
                            and (
                                not lifecycle_owner.logical_identity_matches(
                                    expected_lifecycle_owner
                                )
                                or lifecycle_owner.phase
                                != expected_lifecycle_owner.phase
                                or lifecycle_owner.result_sha256
                                != expected_lifecycle_owner.result_sha256
                            )
                        )
                        or (
                            transaction_record is not None
                            and lifecycle_loaded.record.reservation_id
                            != transaction_record.reservation_id
                        )
                    ):
                        lifecycle_custody_lost = True
                        raise ReviewInputError(
                            "finding lifecycle transaction owner changed before publication"
                        )
                    if lifecycle_owner is None:
                        operation_id = lifecycle_loaded.record.reservation_id
                        if (
                            operation_id is None
                            or prepared is None
                            or prepared.reservation_id != operation_id
                        ):
                            raise ReviewInputError(
                                "finding lifecycle requires an admitted operation"
                            )
                        lifecycle_policy_digest = policy.digest()
                        lifecycle_configuration_digest = (
                            ReviewTransaction.compute_configuration_digest(
                                configuration_context or {}
                            )
                        )
                    else:
                        operation_id = lifecycle_owner.transaction_id
                        lifecycle_policy_digest = lifecycle_owner.policy_digest
                        lifecycle_configuration_digest = (
                            lifecycle_owner.configuration_digest
                        )
                    carried_document = lifecycle_document
                    reference = lifecycle_loaded.record.finding_lifecycle
                    if (
                        reference is not None
                        and reference["operation_id"] != operation_id
                    ):
                        if carried_document is not None and any(
                            e["status"] == "pending"
                            for e in carried_document["effects"]
                        ):
                            raise ReviewInputError(
                                "finding lifecycle prior effect requires reconciliation"
                            )
                        carried_document = None
                    reviewer._finding_lifecycle_journal = FindingLifecycleJournal(
                        reviewer,
                        ledger,
                        identity,
                        operation_id=operation_id,
                        base_sha=base_sha,
                        head_sha=head_sha,
                        policy_digest=lifecycle_policy_digest,
                        configuration_digest=lifecycle_configuration_digest,
                        document=carried_document,
                        app_slug=app_slug,
                        record=lifecycle_loaded.record,
                        review_token=token,
                        check_token=capability.token,
                    )

                def cleanup_capability() -> str:
                    # Supersession needs separate authority from publication.
                    # Keep issuance lazy and on the original caller envelope;
                    # a failed mint must never fall back to the review token.
                    if budget is None:
                        raise GitHubBrokerClientError(
                            "thread cleanup broker accounting is unavailable"
                        )
                    budget.check()
                    if isinstance(broker, BrokerClient) and (
                        broker.control is None or broker.control.budget is not budget
                    ):
                        raise GitHubBrokerClientError(
                            "thread cleanup broker accounting is unavailable"
                        )
                    cleanup_token = broker.exchange(
                        exchange_input, capability="thread_resolve"
                    )
                    budget.check()
                    return cleanup_token

                reviewer._thread_cleanup_token_provider = cleanup_capability
                if ledger is not None:
                    reviewer._defer_thread_cleanup = True
                    reviewer._confirmed_publication_only = (
                        terminal_cleanup_owner is not None
                    )
            publisher_started = True
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
                else reviewer.publish(**publisher_arguments)
            )
            # Keep the exact returned native receipt for truthful failure output
            # if subsequent durable bookkeeping cannot complete. Not authority.
            self._last_publication_result = publication
        except BaseException as publication_error:
            if lifecycle_custody_lost:
                # Another writer owns this selected root. Neither a diagnostic
                # check nor failure cleanup may mutate its effects/reservation.
                publication_error.add_note(
                    "finding lifecycle changed; competing state retained"
                )
                raise
            publication_error.add_note(
                "publication result retained"
                if result_retained
                else "publication result not retained"
            )
            from review_sensei.publication_intent_fusion import (
                outstanding as fusion_outstanding,
            )

            if (
                not publisher_started
                and not fusion_outstanding()
                and not admission_recovery_held
                and isinstance(reviewer, ReviewPublisher)
                and not isinstance(publication_error, (KeyboardInterrupt, SystemExit))
            ):
                try:
                    reviewer._complete_failed_check(
                        check_token=check_capability().token,
                        repository=repository,
                        head_sha=head_sha,
                        app_slug=app_slug,
                        policy=options.review_policy(),
                        transient=False,
                    )
                except Exception as check_error:
                    publication_error.add_note(
                        "failed publication check could not be updated: "
                        + type(check_error).__name__
                    )
            if (
                ledger is not None
                and isinstance(result, ReviewResult)
                and result.transaction is not None
            ):
                if terminal_cleanup_owner is None and not isinstance(
                    publication_error, (KeyboardInterrupt, SystemExit)
                ):
                    # A refused optional retry cannot re-complete or fail the
                    # independently authenticated terminal publication owner.
                    durable_transaction = (
                        transaction_record.transaction
                        if transaction_record is not None
                        else None
                    )
                    if (
                        durable_transaction is not None
                        and durable_transaction.phase != "analysis"
                    ):
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
            if terminal_cleanup_owner is not None:
                # No identical success PATCH: spend a fresh authority read instead.
                # A competing writer cannot lend a cached terminal grant to cleanup.
                fresh_terminal = load_transaction(ledger)
                if (
                    fresh_terminal.transaction != terminal_cleanup_owner[0]
                    or fresh_terminal.generation != terminal_cleanup_owner[1]
                ):
                    raise GitHubPublicationError(
                        "confirmed publication cleanup transaction changed"
                    )
            else:
                complete_review_publication(
                    ledger,
                    identity,
                    durable_transaction,
                    published=publication.status in success_statuses,
                )
        elif ledger is not None and prepared is not None:
            lifecycle_journal = getattr(reviewer, "_finding_lifecycle_journal", None)
            if lifecycle_journal is not None:
                prepared = lifecycle_journal.prepared_completion(prepared)
            complete_session_round(
                ledger,
                identity,
                prepared,
                published=publication.status == "published",
            )
        if isinstance(reviewer, ReviewPublisher):
            publication = reviewer._finish_thread_cleanup(publication)
            window_after = reviewer.active_window_state(
                repository_id=repository_id, pull_request=pull_request
            )
            if (
                window_after is not None
                and not window_after.approval_eligibility.approved
            ):
                flags["independently_approval_eligible"] = False
        if (
            isinstance(result, ReviewResult)
            and result.coverage_only_partial
            and publication.status in {"published", "already_published"}
            and type(publication.review_id) is int
            and publication.review_id > 0
        ):
            try:
                self._publish_human_file_request(
                    token=token,
                    repository=repository,
                    repository_id=repository_id,
                    pull_request=pull_request,
                    review_id=publication.review_id,
                    result=result,
                )
            except Exception:
                # The review is already published. A follow-up inventory comment
                # must not turn that success into a publication failure.
                pass
        completed = _with_shadow(
            publication,
            _shadow_observation(_shadow_state(prepared, flags)),
        )
        if isinstance(reviewer, ReviewPublisher) and not getattr(
            reviewer, "_check_accounting_unknown", True
        ):
            from .check_accounting import confirm_denied_publication

            try:
                confirm_denied_publication(
                    completed,
                    approval=getattr(reviewer, "_check_accounting_approval", None),
                    failure=getattr(reviewer, "_check_accounting_failure", None),
                    cleanup=getattr(reviewer, "_accounting_cleanup_result", None),
                    review_id=getattr(reviewer, "_check_accounting_review_id", None),
                    http=reviewer.http,
                    source_result=result,
                    repository=repository,
                    pull_request=pull_request,
                    head_sha=head_sha,
                )
            except Exception:
                try:
                    from .review_lifecycle_diagnostics import check_proof_hook_failed

                    check_proof_hook_failed("publication")
                except BaseException:
                    pass
        self._last_publication_result = completed
        return completed

    def _publish_human_file_request(
        self,
        *,
        token: str,
        repository: str,
        repository_id: int,
        pull_request: int,
        review_id: int,
        result: ReviewResult,
    ) -> None:
        """Post the digest-bound human file request after the owned review exists."""

        from review_sensei.bounded_evidence import EvidenceReadBudget

        from .human_file_review import HumanFileReviewPolicy, HumanFileReviewPublisher

        http = self.http
        api_url = getattr(http, "api_url", None)
        if not isinstance(api_url, str) or not api_url.startswith("https://"):
            raise ReviewInputError("human file request API URL is invalid")
        status, body = http.request("GET", "/user", token=token)
        user_id = body.get("id") if isinstance(body, dict) else None
        login = body.get("login") if isinstance(body, dict) else None
        if (
            status != 200
            or not isinstance(body, dict)
            or body.get("type") != "Bot"
            or isinstance(user_id, bool)
            or not isinstance(user_id, int)
            or user_id <= 0
            or not isinstance(login, str)
            or not login
        ):
            raise ReviewInputError("human file request requires the app user")
        from .caller_operation import caller_for_http

        caller = caller_for_http(http)
        budget = caller.budget_view if caller is not None else EvidenceReadBudget()
        publisher = HumanFileReviewPublisher(
            http=http,
            api_url=api_url,
            repository=repository,
            repository_id=repository_id,
            pull_request=pull_request,
            app_login=login,
            app_user_id=user_id,
            token=token,
            before_request=budget.consume,
            policy=HumanFileReviewPolicy(allow_confirmations=True),
        )
        publisher.request_review(review_id=review_id, result=result)

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
        current_action: object | None = None,
        actor_permission: str | None = None,
        finding_fingerprints: Sequence[str] | None = None,
        finding_inventory: Sequence[object] | None = None,
        prior_inventories: Sequence[tuple[str, Sequence[object]]] = (),
        media_publisher: object | None = None,
        review_result: object | None = None,
        request_comment_id: int | None = None,
        media_budget: EvidenceReadBudget | None = None,
        command_budget: EvidenceReadBudget | None = None,
        media_read_token: str | None = None,
        operation_entry: str = "disabled",
        app_id: int | None = None,
        run_id: str | None = None,
        run_attempt: str = "1",
        operation_store: object | None = None,
        operation_parts: object | None = None,
        reviews_policy: str | None = None,
        convergence_policy: ReviewConvergencePolicy | None = None,
        actor_id: int | None = None,
        observed_trust: AuthenticatedTrustSnapshot | None = None,
    ):
        """Apply a maintainer command through its applicable authority boundary.

        A local injected ledger retains the existing operator-facing command
        seam.  In contrast, a hosted mutation must be reconstructed from the
        broker's current GitHub attestation: workflow event actor metadata is
        only an untrusted routing hint and never authorizes a remote write.
        """

        from review_sensei.disposition import (
            FindingIdentity,
            MaintainerCommandResult,
            apply_session_command,
            authorized_maintainer,
            authorized_override,
            parse_maintainer_command,
        )

        typed_inventory = cast(Sequence[FindingIdentity] | None, finding_inventory)
        typed_priors = cast(
            Sequence[tuple[str, Sequence[FindingIdentity]]], prior_inventories
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
            isinstance(self.reviewer, ReviewPublisher)
            and parsed_command.action != "status"
        ):
            cmd_op = (
                "override"
                if (
                    parsed_command.action
                    in {
                        "accept-risk",
                        "dismiss",
                        "defer",
                        "accept-coverage-uncertainty",
                        "approve-media",
                        "reenroll",
                    }
                    or parsed_command.finding_reference
                    or parsed_command.finding_references
                )
                else "command"
            )
            cmd_trust = self.reviewer.check_trust_boundary(
                repository_id=repository_id,
                pull_request=pull_request,
                phase="before_admission",
                operation=cmd_op,
                observed=observed_trust,
                repository_slug=repository,
                head_sha=head_sha,
                actor_id=actor_id,
                actor_login=actor_login,
                actor_association=association,
                actor_permission=actor_permission,
                has_prior_charges=bool(
                    self.session_ledger is not None or options.github_session_ledger
                ),
            )
            if cmd_trust is not None and not cmd_trust.allowed:
                return MaintainerCommandResult(
                    action=parsed_command.action,
                    applied=False,
                    operator_paused=False,
                    summary=cmd_trust.reason_code,
                )
        from .run_authority import VerifiedRunAction

        current_mutation = isinstance(current_action, VerifiedRunAction)
        if current_action is not None and not current_mutation:
            raise GitHubPublicationError("current command authority is invalid")
        if (
            current_mutation
            and parsed_command.action != "status"
            and not options.github_writes
        ):
            return MaintainerCommandResult(
                action=parsed_command.action,
                applied=False,
                operator_paused=False,
                summary="writes_disabled",
            )
        if (
            parsed_command.action != "status"
            and options.github_session_ledger
            and self.session_ledger is not None
            and not current_mutation
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
        if command_budget is None and options.github_session_ledger:
            command_budget = media_budget or EvidenceReadBudget()
        command: MaintainerCommand | None = None
        session_grant: str | None = None
        broker_attestation: Mapping[str, object] | None = None
        if current_mutation:
            assert isinstance(current_action, VerifiedRunAction)
            actor = current_action.require(
                self.session_ledger,
                purpose="command",
                repository=repository,
                repository_id=repository_id,
                pull_request=pull_request,
                head_sha=head_sha,
                source_comment_id=source_comment_id or 0,
                body=body,
            )
            command = parse_maintainer_command(
                body,
                actor=actor["login"],
                head_sha=head_sha,
                command_id=str(source_comment_id),
            )
            token = current_action.token
            actor_login, actor_type, association = (
                actor["login"],
                actor["type"],
                actor["association"],
            )
            broker_attestation = {"association": association}
            command_exchange_input = oidc_token or self.broker.request_oidc_token()
        elif hosted_mutation:
            raise GitHubPublicationError(
                "hosted commands require the current metered command lifecycle"
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
                    evaluation_failed=command.action == "approve-media",
                )
        if command is None:
            raise GitHubPublicationError("maintainer command reconstruction failed")
        attested_association = association
        if hosted_mutation and isinstance(broker_attestation, Mapping):
            attested = broker_attestation.get("association")
            if isinstance(attested, str):
                attested_association = attested
        can_lookup_collaborator_permission = (
            actor_permission is None
            and isinstance(attested_association, str)
            and attested_association.upper() == "COLLABORATOR"
            and options.github_writes
            and self.http is not None
            and callable(getattr(self.http, "request", None))
            and command.action != "approve-media"
        )
        if not can_lookup_collaborator_permission and (
            command.action
            in {
                "approve-media",
                "accept-risk",
                "accept-coverage-uncertainty",
                "reenroll",
            }
            or command.finding_reference
            or command.finding_references
        ):
            if not authorized_override(
                association=attested_association, permission=actor_permission
            ):
                return MaintainerCommandResult(
                    action=command.action,
                    applied=False,
                    operator_paused=False,
                    summary="unauthorized",
                    evaluation_failed=command.action == "approve-media",
                )
        media_check_token = None
        media_check_prepared = False
        if (
            command.action == "approve-media"
            and hosted_mutation
            and media_budget is not None
            and operation_store is not None
        ):
            # The supported CLI reconciles exact owned publications afresh.
            # Its assessment must not be replaced by a cached approval boolean
            # which cannot revalidate source, permissions, threads or policy.
            return MaintainerCommandResult(
                action="approve-media",
                applied=False,
                operator_paused=False,
                summary="hosted media approval requires fresh publication reconciliation; cached operation assessments are unsupported",
                evaluation_failed=True,
            )
        if (
            command.action == "approve-media"
            and hosted_mutation
            and media_publisher is None
            and review_result is None
            and request_comment_id is None
            and media_budget is not None
            and media_read_token is not None
        ):
            from .hosted_media import prepare_hosted_media_context

            # The supported command must fund its check capability before any
            # media publication. Charge its caller POST in the original pool;
            # cold JWKS/tag fallback remains independently Worker-bounded.
            check_broker = self.broker
            if type(check_broker) is BrokerClient:
                control = check_broker.control
                if control is None or control.budget is not media_budget:
                    raise GitHubPublicationError(
                        "media command broker accounting is unavailable"
                    )
            # This capability is required here. Preserve broker diagnostics for
            # the CLI's existing sanitized failure reporter instead of silently
            # degrading to a missing optional review check.
            media_check_token = check_broker.exchange(
                command_exchange_input, capability="check_publish"
            )
            if not isinstance(media_check_token, str) or not media_check_token.strip():
                return MaintainerCommandResult(
                    action="approve-media",
                    applied=False,
                    operator_paused=False,
                    summary="media approval could not fund its check capability; retry the original media command",
                    evaluation_failed=True,
                )
            media_check_prepared = True

            context = prepare_hosted_media_context(
                application=self,
                options=options,
                http=self.http,
                budget=media_budget,
                session_token=token,
                read_token=media_read_token,
                repository=repository,
                repository_id=repository_id,
                pull_request=pull_request,
                head_sha=head_sha,
                source_comment_id=cast(int, source_comment_id),
                actor_login=command.actor,
                body=body,
                app_slug=app_slug,
                reviews_policy=reviews_policy or options.review_policy(),
            )
            media_publisher = context.publisher
            review_result = context.result
            request_comment_id = context.request_comment_id
        if (
            command.action == "approve-media"
            and operation_entry == "enabled"
            and operation_store is not None
        ):
            effective_policy = (
                reviews_policy
                if reviews_policy is not None
                else options.review_policy()
            )
            if effective_policy not in REVIEW_POLICY_MODES:
                raise GitHubPublicationError("review policy mode is invalid")
            auto_approve_enabled = effective_policy == "auto-approve"
            media_finalizer = getattr(media_publisher, "finalizer", None)
            if media_finalizer is not None:
                self._bind_finding_lifecycle_finalizer(
                    media_finalizer,
                    options=options,
                    repository=repository,
                    repository_id=repository_id,
                    pull_request=pull_request,
                    head_sha=head_sha,
                    app_slug=app_slug,
                    token=media_read_token or getattr(self.session_ledger, "token", ""),
                    evidence_budget=media_budget,
                )
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
                    evaluation_failed=True,
                )
            media_digest = sha256(
                f"{repository_id}:{pull_request}:{head_sha}".encode()
            ).hexdigest()
            extra_kwargs = {"enabled": False} if not auto_approve_enabled else {}

            def approve_media() -> object:
                confirmer(
                    request_comment_id=request_comment_id,
                    source_comment_id=source_comment_id,
                    result=review_result,
                )
                return finalizer(
                    request_comment_id=request_comment_id,
                    result=review_result,
                    **extra_kwargs,
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
            return self._finalize_media_command_result(
                assessment=assessment,
                review_result=review_result,
                media_publisher=media_publisher,
                options=options,
                effective_policy=effective_policy,
                oidc_token=oidc_token,
                repository=repository,
                head_sha=head_sha,
                app_slug=app_slug,
                evidence_budget=media_budget,
                prepared_check_token=media_check_token,
                check_prepared=media_check_prepared,
            )
        if command.action == "approve-media":
            effective_policy = (
                reviews_policy
                if reviews_policy is not None
                else options.review_policy()
            )
            if effective_policy not in REVIEW_POLICY_MODES:
                raise GitHubPublicationError("review policy mode is invalid")
            auto_approve_enabled = effective_policy == "auto-approve"
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
                    evaluation_failed=True,
                )
            extra_kwargs = {"enabled": False} if not auto_approve_enabled else {}
            assessment = approver(
                request_comment_id=request_comment_id,
                source_comment_id=source_comment_id,
                result=review_result,
                **extra_kwargs,
            )
            return self._finalize_media_command_result(
                assessment=assessment,
                review_result=review_result,
                media_publisher=media_publisher,
                options=options,
                effective_policy=effective_policy,
                oidc_token=oidc_token,
                repository=repository,
                head_sha=head_sha,
                app_slug=app_slug,
                evidence_budget=media_budget,
                prepared_check_token=media_check_token,
                check_prepared=media_check_prepared,
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
        # A non-hosted command may already hold a review_publish capability for
        # its ledger mutation. Reacquiring the same capability for refinalization
        # would spend another whole internal-dispatch liability and can starve
        # the check and approval that follow thread cleanup.
        prepared_review_capability: _PreparedReviewCapability | None = None
        ledger = (
            self.session_ledger
            if current_mutation
            or command.action == "status"
            and not options.github_session_ledger
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
                if capability == "review_publish":
                    # Both refinalization call sites require a truthy
                    # ``oidc_token`` and pass that same value, so redemption
                    # always sees the exact assertion this capability was
                    # minted for. A capability minted from a refreshed
                    # assertion is never redeemed and simply goes unused.
                    prepared_review_capability = _PreparedReviewCapability(
                        self.broker, exchange_input, token
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
                evidence_budget=command_budget,
            )
        if ledger is None:
            raise GitHubPublicationError("maintainer commands require a session ledger")
        lifecycle_record = lifecycle_document = None
        if isinstance(self.reviewer, ReviewPublisher):
            from .finding_lifecycle import restore_finding_lifecycle

            lifecycle_record, lifecycle_document = restore_finding_lifecycle(
                self.reviewer, ledger, identity
            )
        if (
            typed_inventory is None
            and not finding_fingerprints
            and isinstance(self.reviewer, ReviewPublisher)
        ):
            window_state = self.reviewer.active_window_state(
                repository_id=repository_id, pull_request=pull_request
            )
            if window_state is not None and window_state.head_sha == head_sha:
                displayed_ids = {
                    entry.obligation_id
                    for entry in window_state.display_active_findings.entries
                }
                typed_inventory = tuple(
                    FindingIdentity(
                        instance=finding_instance_fingerprint(ob.comment),
                        concern=ob.fingerprint,
                        overridable=ob.required,
                    )
                    for ob in window_state.authoritative_decision_inventory.obligations
                    if ob.obligation_id in displayed_ids
                    and ob.unresolved
                    and ob.comment is not None
                )
            else:
                drift_state = self.reviewer.drift_state(
                    repository_id=repository_id, pull_request=pull_request
                )
                if drift_state is not None and drift_state.head_sha == head_sha:
                    projected_drift = project_drift_review_result(
                        ReviewResult(
                            summary="drift-command-inventory",
                            comments=(),
                            provider="internal",
                            review_status=(
                                "complete" if drift_state.review_complete else "partial"
                            ),
                        ),
                        drift_state,
                    )
                    by_oid = {
                        ob.obligation_id: ob for ob in drift_state.inventory.obligations
                    }
                    drift_identities: list[FindingIdentity] = []
                    seen_drift_inst: set[str] = set()
                    for entry, p_comment in zip(
                        drift_state.active_entries,
                        projected_drift.comments,
                        strict=False,
                    ):
                        ob = by_oid.get(entry.obligation_id)
                        if ob is None or not ob.unresolved:
                            continue
                        inst = finding_instance_fingerprint(p_comment)
                        if inst not in seen_drift_inst:
                            seen_drift_inst.add(inst)
                            drift_identities.append(
                                FindingIdentity(
                                    instance=inst,
                                    concern=ob.criterion.original_fingerprint,
                                    overridable=ob.blocks_approval_now,
                                )
                            )
                    typed_inventory = tuple(drift_identities)

        grant_bound_ledger = getattr(ledger, "_broker", None) is not None
        if grant_bound_ledger and not callable(
            getattr(ledger, "initialize_with_mutation", None)
        ):
            raise GitHubPublicationError(
                "hosted maintainer mutations require atomic session initialization"
            )
        needs_elevated_permission_lookup = can_lookup_collaborator_permission and (
            command.action
            in {
                "accept-risk",
                "accept-coverage-uncertainty",
                "reenroll",
                "continue",
            }
            or bool(command.finding_reference)
            or bool(command.finding_references)
        )
        if (
            needs_elevated_permission_lookup
            and isinstance(token, str)
            and token.strip()
        ):
            from .commands import lookup_collaborator_permission

            try:
                actor_permission = lookup_collaborator_permission(
                    self.http,
                    token=token.strip(),
                    repository=repository,
                    actor=command.actor,
                )
            except Exception:
                actor_permission = None
        if (
            command.action
            in {
                "accept-risk",
                "accept-coverage-uncertainty",
                "reenroll",
            }
            or command.finding_reference
            or command.finding_references
        ):
            if not authorized_override(
                association=attested_association, permission=actor_permission
            ):
                return MaintainerCommandResult(
                    action=command.action,
                    applied=False,
                    operator_paused=False,
                    summary="unauthorized",
                )
        if isinstance(actor_permission, str) and actor_permission.strip():
            effective_actor_role: str | None = actor_permission.strip()
        elif isinstance(attested_association, str) and attested_association.upper() in {
            "OWNER",
            "MEMBER",
            "COLLABORATOR",
        }:
            effective_actor_role = (
                None
                if command.action in {"continue", "reenroll"}
                and (
                    attested_association.upper() == "COLLABORATOR"
                    or (hosted_mutation and attested_association.upper() != "OWNER")
                )
                else attested_association.upper()
            )
        else:
            effective_actor_role = None
        command_policy_digest = (
            convergence_policy or ReviewConvergencePolicy()
        ).digest()
        command_provenance = (
            f"comment:{source_comment_id}"
            if isinstance(source_comment_id, int)
            and not isinstance(source_comment_id, bool)
            and source_comment_id > 0
            else (
                f"command:{command.command_id}"
                if command.command_id
                else f"local:{command.action}:{head_sha[:12]}"
            )
        )
        derived_incomplete_coverage: tuple[str, ...] = ()
        if command.action in {
            "accept-coverage-uncertainty",
            "verify",
            "accept-risk",
            "dismiss",
            "defer",
        }:
            from review_sensei.disposition import incomplete_coverage_from_result

            coverage_source = (
                review_result if isinstance(review_result, ReviewResult) else None
            )
            if coverage_source is None:
                loader = getattr(ledger, "load_retained_checkpoint_result", None)
                if callable(loader):
                    try:
                        loaded_checkpoint = loader(identity, head_sha=head_sha)
                        if isinstance(loaded_checkpoint, ReviewResult):
                            coverage_source = loaded_checkpoint
                    except Exception:
                        coverage_source = None
            derived_incomplete_coverage = incomplete_coverage_from_result(
                coverage_source
            )
        command_now = datetime.now(timezone.utc)
        command_finalization_plan = None
        if current_action is not None:
            capacity_preview: dict[str, object] = {}
            pre_effect_refusal = guard_current_command_representability(
                current_action=current_action,
                ledger=ledger,
                identity=identity,
                command=command,
                head_sha=head_sha,
                writes_enabled=bool(options.github_writes),
                finding_fingerprints=finding_fingerprints,
                finding_inventory=typed_inventory,
                prior_inventories=typed_priors,
                actor_role=effective_actor_role,
                policy_digest=command_policy_digest,
                provenance=command_provenance,
                incomplete_coverage=derived_incomplete_coverage,
                capacity_preview=capacity_preview,
                now=command_now,
            )
            if pre_effect_refusal is not None:
                return pre_effect_refusal
            if (
                options.github_writes
                and oidc_token
                and command.action
                in {
                    "verify",
                    "accept-risk",
                    "dismiss",
                    "defer",
                    "accept-coverage-uncertainty",
                }
                and lifecycle_record is not None
                and lifecycle_record.finding_lifecycle is not None
            ):
                capacity_outcome = self._preflight_command_finalization(
                    command=command,
                    oidc_token=oidc_token,
                    capacity_preview=capacity_preview,
                    record=lifecycle_record,
                    document=lifecycle_document,
                    ledger=ledger,
                    read_token=token,
                    evidence_budget=command_budget,
                    repository=repository,
                    repository_id=repository_id,
                    pull_request=pull_request,
                    head_sha=head_sha,
                    app_slug=app_slug,
                    incomplete_coverage=derived_incomplete_coverage,
                    policy_digest=command_policy_digest,
                )
                if isinstance(capacity_outcome, MaintainerCommandResult):
                    return capacity_outcome
                command_finalization_plan = capacity_outcome
        if command.action == "reenroll" and isinstance(self.reviewer, ReviewPublisher):
            active_blocking = self.reviewer.active_blocking_obligations_count(
                repository_id=repository_id, pull_request=pull_request
            )
            has_uncertain = any(
                hrid == repository_id and hpr == pull_request
                for (hrid, hpr, _sha) in self.reviewer._response_loss_holds
            )
            if active_blocking > 0 or has_uncertain:
                reenroll_check = evaluate_operator_reenroll(
                    OperatorReenrollRequest(
                        identity=identity,
                        head_sha=head_sha,
                        operator_login=command.actor,
                        association=attested_association or "OWNER",
                        permission=actor_permission,
                        reason=command.reason or "maintainer reenroll request",
                        active_blocking_obligations=active_blocking,
                        has_uncertain_write=has_uncertain,
                    ),
                    ledger_status="missing",
                )
                return MaintainerCommandResult(
                    action="reenroll",
                    applied=False,
                    operator_paused=True,
                    summary=reenroll_check.reason_code,
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
                    now=command_now,
                    finding_fingerprints=finding_fingerprints,
                    finding_inventory=typed_inventory,
                    prior_inventories=typed_priors,
                    actor_role=effective_actor_role,
                    policy_digest=command_policy_digest,
                    provenance=command_provenance,
                    incomplete_coverage=derived_incomplete_coverage,
                )[1],
                encode=_command_bytes,
                decode=_command_from_bytes,
            )
            if (
                options.github_writes
                and oidc_token
                and isinstance(produced, MaintainerCommandResult)
            ):
                loaded = ledger.load(identity)
                refinalized = self._refinalize_command(
                    produced,
                    oidc_token=oidc_token,
                    repository=repository,
                    repository_id=repository_id,
                    pull_request=pull_request,
                    head_sha=head_sha,
                    app_slug=app_slug,
                    actor=command.actor,
                    record=loaded.record,
                    ledger=ledger,
                    evidence_budget=command_budget,
                    incomplete_coverage=derived_incomplete_coverage,
                    policy_digest=command_policy_digest,
                    _prepared_review_capability=prepared_review_capability,
                    _capacity_plan=command_finalization_plan,
                )
                if isinstance(refinalized, MaintainerCommandResult):
                    produced = refinalized
            return produced
        _record, result = apply_session_command(
            ledger,
            identity,
            command,
            now=command_now,
            finding_fingerprints=finding_fingerprints,
            finding_inventory=typed_inventory,
            prior_inventories=typed_priors,
            actor_role=effective_actor_role,
            policy_digest=command_policy_digest,
            provenance=command_provenance,
            incomplete_coverage=derived_incomplete_coverage,
        )
        if (
            options.github_writes
            and oidc_token
            and command.action
            in {
                "verify",
                "accept-risk",
                "dismiss",
                "defer",
                "accept-coverage-uncertainty",
            }
            and (result.applied or command.action == "verify")
        ):
            # The operation-entry branch above returns `produced`, so only one
            # of these two refinalize calls can run; redemption stays
            # idempotent if a future retry revisits this path.
            refinalized = self._refinalize_command(
                result,
                oidc_token=oidc_token,
                repository=repository,
                repository_id=repository_id,
                pull_request=pull_request,
                head_sha=head_sha,
                app_slug=app_slug,
                actor=command.actor,
                record=_record,
                ledger=ledger,
                evidence_budget=command_budget,
                incomplete_coverage=derived_incomplete_coverage,
                policy_digest=command_policy_digest,
                _prepared_review_capability=prepared_review_capability,
                _capacity_plan=command_finalization_plan,
            )
            if isinstance(refinalized, MaintainerCommandResult):
                result = refinalized
        return result

    def _check_capability(
        self,
        exchange_input: str,
        *,
        broker: BrokerClient | None = None,
        accounting_context=None,
    ) -> CheckCapability:
        """Acquire the separate gate capability without changing review eligibility."""
        from .check_accounting import broker_check_call

        selected = broker or self.broker
        failure = None
        token = None
        with broker_check_call(selected, accounting_context) as capture:
            try:
                token = selected.exchange(exchange_input, capability="check_publish")
            except GitHubHTTPTransientError as exc:
                failure = check_failure(
                    "capability", status=observed_http_status(exc), transient=True
                )
            except GitHubBrokerClientError as exc:
                failure = check_failure("capability", status=observed_http_status(exc))
        if failure is not None:
            return CheckCapability(failure=capture.bind(failure))
        if not isinstance(token, str) or not token.strip():
            return CheckCapability(failure=check_failure("capability"))
        return CheckCapability(token=token)

    def _check_capability_token(
        self, exchange_input: str, *, broker: BrokerClient | None = None
    ) -> str | None:
        return self._check_capability(exchange_input, broker=broker).token

    def _finalize_media_command_result(
        self,
        *,
        assessment: object,
        review_result: object,
        media_publisher: object | None,
        options: GitHubWriteOptions,
        effective_policy: str,
        oidc_token: str | None,
        repository: str,
        head_sha: str,
        app_slug: str,
        evidence_budget: EvidenceReadBudget | None = None,
        prepared_check_token: str | None = None,
        check_prepared: bool = False,
    ) -> object:
        """Reconcile the merge-gate check run after a human media confirmation."""

        from review_sensei.disposition import MaintainerCommandResult

        from .approval import (
            has_blocking_findings,
            has_human_adjudication_findings,
        )
        from .checks import ReviewCheckPublisher, check_outcome_for_result

        try:
            approved, blockers = _media_approval_fields(assessment)
        except GitHubPublicationError:
            return MaintainerCommandResult(
                action="approve-media",
                applied=False,
                operator_paused=False,
                summary="media approval assessment is unavailable or invalid",
                evaluation_failed=True,
            )
        non_coverage_structural_blockers = set(blockers) - {
            "auto-approval-disabled",
            "blocking-findings-open",
            "blocking-findings",
            "human-adjudication-open",
            "human-adjudication-findings",
        }
        mixed = getattr(assessment, "mixed", None)
        mixed_satisfied = (
            approved
            or bool(getattr(mixed, "approved", False))
            or (
                bool(blockers)
                and not non_coverage_structural_blockers
                and not getattr(assessment, "pending_ids", ())
            )
        )
        all_obligations_met = approved or (
            bool(blockers) and set(blockers) <= {"auto-approval-disabled"}
        )
        applied = (
            approved if effective_policy == "auto-approve" else all_obligations_met
        )
        base_summary = "media-approved" if applied else "media-approval-withheld"

        if not isinstance(review_result, ReviewResult) or not options.github_writes:
            return MaintainerCommandResult(
                action="approve-media",
                applied=applied,
                operator_paused=False,
                summary=base_summary,
            )

        http = (
            self.http
            if self.http is not None
            else getattr(media_publisher, "http", None)
        )
        if http is None:
            return MaintainerCommandResult(
                action="approve-media",
                applied=applied,
                operator_paused=False,
                summary=base_summary,
            )

        if evidence_budget is not None and not check_prepared:
            return MaintainerCommandResult(
                action="approve-media",
                applied=applied,
                operator_paused=False,
                summary=(
                    f"{base_summary}; gate re-evaluation failed: "
                    "bounded media finalization requires its prepaid check capability"
                ),
                evaluation_failed=True,
            )

        if evidence_budget is not None:
            from .media_budget import MediaCheckFinalizationHttp

            http = MediaCheckFinalizationHttp(
                http, evidence_budget, repository=repository, head_sha=head_sha
            )

        check_token = prepared_check_token
        if not check_prepared:
            try:
                exchange_input = oidc_token or self.broker.request_oidc_token()
                check_token = self._check_capability_token(exchange_input)
            except Exception:
                check_token = None

        if not check_token:
            return MaintainerCommandResult(
                action="approve-media",
                applied=applied,
                operator_paused=False,
                summary=(
                    f"{base_summary}; gate re-evaluation failed: "
                    "check capability unavailable; retry media approval "
                    "to complete check update"
                ),
                evaluation_failed=True,
            )

        current_findings = getattr(assessment, "current_findings", None)
        if current_findings is not None:
            from review_sensei.mixed_coverage import CurrentFindingFacts

            if (
                not isinstance(current_findings, CurrentFindingFacts)
                or current_findings.result_digest != review_result.content_digest()
            ):
                return MaintainerCommandResult(
                    action="approve-media",
                    applied=False,
                    operator_paused=False,
                    summary="media approval current finding evidence is invalid",
                    evaluation_failed=True,
                )
        req_fixes = (
            "blocking-findings-open" in blockers
            or "blocking-findings" in blockers
            or (
                current_findings.has_blocking_findings
                if current_findings is not None
                else has_blocking_findings(review_result)
            )
        )
        req_decisions = (
            "human-adjudication-open" in blockers
            or "human-adjudication-findings" in blockers
            or (
                current_findings.has_human_adjudication_findings
                if current_findings is not None
                else has_human_adjudication_findings(review_result)
            )
        )
        outcome = check_outcome_for_result(
            review_result,
            policy=effective_policy,
            required_fixes=req_fixes,
            required_decisions=req_decisions,
            mixed_coverage_satisfied=mixed_satisfied
            and not non_coverage_structural_blockers,
        )
        try:
            check_result = ReviewCheckPublisher(http=http).complete(
                token=check_token,
                repository=repository,
                head_sha=head_sha,
                outcome=outcome,
                app_slug=app_slug,
            )
            if check_result.status not in {"created", "updated", "published"}:
                return MaintainerCommandResult(
                    action="approve-media",
                    applied=applied,
                    operator_paused=False,
                    summary=(
                        f"{base_summary}; gate re-evaluation failed, "
                        "retry media approval to complete check update"
                    ),
                    evaluation_failed=True,
                )
        except Exception:
            return MaintainerCommandResult(
                action="approve-media",
                applied=applied,
                operator_paused=False,
                summary=(
                    f"{base_summary}; gate re-evaluation failed, "
                    "retry media approval to complete check update"
                ),
                evaluation_failed=True,
            )

        return MaintainerCommandResult(
            action="approve-media",
            applied=applied,
            operator_paused=False,
            summary=base_summary,
        )

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
        evidence_budget: EvidenceReadBudget | None = None,
    ) -> SessionLedger | None:
        if self.session_ledger is not None and not prefer_remote:
            if evidence_budget is not None:
                from .publication_recovery import publication_session_ledger

                return publication_session_ledger(self.session_ledger, evidence_budget)
            return self.session_ledger
        if not options.github_session_ledger:
            return None
        from .session_ledger import GitHubIssueCommentSessionLedger

        if self.http is None:
            raise GitHubPublicationError("GitHub session ledger requires HTTP")
        ledger: SessionLedger = GitHubIssueCommentSessionLedger(
            self.http,
            token=token,
            app_slug=app_slug,
            broker=broker,
            session_grant=session_grant,
            session_attestation=session_attestation,
            head_sha=head_sha,
            actions_token_provider=actions_token_provider,
            evidence_budget=evidence_budget,
            enable_disposition_snapshot_writes=self.enable_disposition_snapshot_writes,
            enable_finding_lifecycle_writes=(
                options.max_active_findings is not None or options.enable_drift_tracking
            ),
        )
        if evidence_budget is not None:
            from .publication_recovery import publication_session_ledger

            ledger = publication_session_ledger(ledger, evidence_budget)
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
        artifact: RecoveryArtifact | None,
        diff: str,
        app_slug: str,
        now=None,
        convergence_policy: ReviewConvergencePolicy | None = None,
        configuration_context: Mapping[str, object] | None = None,
        expected_result_sha256: str | None = None,
        edited_by_human: bool = False,
    ) -> PublicationResult:
        """Publish a retained result without invoking a model or writing learnings."""

        if artifact is None:
            if isinstance(self.reviewer, ReviewPublisher):
                self.reviewer.observe_artifact(
                    repository_id=repository_id,
                    pull_request=pull_request,
                    observed=ObservedGitHubArtifact(
                        artifact_kind="retained_result_part",
                        artifact_id=f"recovery:{repository_id}:{pull_request}:{head_sha}",
                        present=False,
                        duplicate_count=0,
                    ),
                )
            raise ReviewInputError(
                "recovery artifact is missing",
                diagnostic="recovery_artifact_missing",
            )
        try:
            artifact.validate(
                repository=repository,
                pull_request_number=pull_request,
                base_sha=base_sha,
                head_sha=head_sha,
                now=now,
            )
        except ReviewInputError:
            if isinstance(self.reviewer, ReviewPublisher):
                self.reviewer.observe_artifact(
                    repository_id=repository_id,
                    pull_request=pull_request,
                    observed=ObservedGitHubArtifact(
                        artifact_kind="retained_result_part",
                        artifact_id=f"recovery:{repository_id}:{pull_request}:{head_sha}",
                        present=True,
                        edited_by_human=True,
                    ),
                )
            raise
        if isinstance(self.reviewer, ReviewPublisher):
            part_decision = self.reviewer.observe_artifact(
                repository_id=repository_id,
                pull_request=pull_request,
                observed=ObservedGitHubArtifact(
                    artifact_kind="retained_result_part",
                    artifact_id=f"recovery:{repository_id}:{pull_request}:{head_sha}",
                    present=True,
                    edited_by_human=edited_by_human,
                    expected_sha256=(expected_result_sha256 or artifact.result_sha256),
                    observed_sha256=artifact.result_sha256,
                ),
            )
            if not part_decision.authority_intact:
                raise ReviewInputError(
                    f"retained recovery artifact refused: {part_decision.reason_code}",
                    diagnostic="recovery_artifact_tampered",
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
        # Recovery is an ordinary publication consumer. Artifact bytes cannot
        # replace independently selected session authority or an original
        # admitted transaction, and never create a fresh inference allowance.
        return self.publish_review(
            options=options,
            oidc_token=oidc_token,
            repository=repository,
            repository_id=repository_id,
            pull_request=pull_request,
            head_sha=head_sha,
            base_branch=base_branch,
            base_sha=base_sha,
            result=result,
            diff=diff,
            app_slug=app_slug,
            convergence_policy=convergence_policy,
            configuration_context=configuration_context,
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
        if isinstance(self.replier, ConversationPublisher):
            self._bind_finding_lifecycle_finalizer(
                self.replier.finalizer,
                options=options,
                repository=repository,
                pull_request=pull_request,
                head_sha=head_sha,
                app_slug=app_slug,
                token=token,
                evidence_budget=getattr(self.session_ledger, "evidence_budget", None),
            )
        publish = getattr(self.replier, "publish_current", self.replier.publish)
        return publish(
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

    def _observe_quarantine_command_finalization(self, run, authority, target):
        """Read-only hypothetical finalizer work; not a completed capacity plan.

        The distinct observation contract is deliberately rejected by ordinary
        admission and execution. A guarded command still needs its complete
        physical/checkpoint/terminal proof before its first business effect.
        """
        from copy import copy

        from review_sensei.disposition import session_dispositions

        from .finding_lifecycle import FindingLifecycleJournal
        from .quarantine_command_finalization import (
            prepare,
            require_finalizer_copy_source,
        )
        from .quarantine_lifecycle_transfer import sizing_snapshot

        preparation = prepare(run, authority, target, self)
        record, document = sizing_snapshot(target, run)
        finalizer = getattr(self.reviewer, "finalizer", None)
        require_finalizer_copy_source(finalizer)
        budget = run.caller.budget_view
        if (
            type(finalizer) is not ReviewApprovalFinalizer
            or not isinstance(self.broker, BrokerClient)
            or self.broker.control is None
            or self.broker.control.budget is not budget
            or record.finding_lifecycle is None
        ):
            raise ReviewInputError("quarantine finalizer sizing source is unavailable")
        existing = finalizer.evidence_budget or finalizer._operation_budget
        if existing is not None and existing is not budget:
            raise ReviewInputError("quarantine finalizer sizing budget changed")
        finalizer = copy(finalizer)
        finalizer.evidence_budget = budget
        reference = record.finding_lifecycle
        binding = reference["manifest"]["binding"]
        journal = FindingLifecycleJournal(
            self.reviewer,
            run.ledger,
            record.identity(),
            operation_id=reference["operation_id"],
            base_sha=binding["base_sha"],
            head_sha=binding["head_sha"],
            policy_digest=binding["policy_digest"],
            configuration_digest=binding["configuration_digest"],
            app_slug="reviewsensei[bot]",
            record=record,
            document=document,
            review_token=run.ledger.token,
            native_budget=budget,
        )
        budget.check()
        check_token = self.broker.exchange(run.oidc, capability="check_publish")
        budget.check()
        with journal.transports(finalizer=finalizer):
            plan = finalizer.prepare_quarantine_command_capacity(
                preparation,
                token=run.ledger.token,
                check_token=check_token,
                repository=run.identity.repository,
                repository_id=run.identity.repository_id,
                pull_request=run.identity.pull_request,
                head_sha=run.head_sha,
                app_slug="reviewsensei[bot]",
                actor=authority.document["actor"]["login"],
                finding_ids=(),
                dispositions=session_dispositions(record, ledger=run.ledger),
                policy_digest=run.caller.accounting.configuration.digest,
                command_finalization_reserve=7,
            )
        return plan

    def _preflight_command_finalization(
        self,
        *,
        command,
        capacity_preview,
        record,
        document,
        ledger,
        read_token,
        evidence_budget,
        repository,
        repository_id,
        pull_request,
        head_sha,
        app_slug,
        incomplete_coverage,
        policy_digest,
        oidc_token=None,
    ):
        """Observe and price the entire command before its first business write.

        The preview root is sizing data only. The journal retains the actual
        selected root, original funding and authenticated retained document.
        Planning uses an existing read credential and never acquires a native
        mutation capability. Unsupported source paths fail closed here.
        """
        from review_sensei.disposition import (
            MaintainerCommandResult,
            decode_session_dispositions,
        )
        from review_sensei.session import SessionRecord

        from .finding_lifecycle import FindingLifecycleJournal

        preview_result = capacity_preview.get("result")
        if not isinstance(preview_result, MaintainerCommandResult):
            raise ReviewInputError("command capacity preview result is unavailable")
        if not preview_result.applied and command.action != "verify":
            return None
        try:
            preview_record = capacity_preview.get("record")
            details = capacity_preview.get("details")
            finalizer = getattr(self.reviewer, "finalizer", None)
            if (
                type(finalizer) is not ReviewApprovalFinalizer
                or not isinstance(preview_record, SessionRecord)
                or not isinstance(details, dict)
                or not isinstance(details.get("dispositions"), bytes)
                or not isinstance(read_token, str)
                or not read_token
                or evidence_budget is None
            ):
                raise ReviewInputError(
                    "command finalization capacity source is unavailable"
                )
            existing_budget = finalizer.evidence_budget or finalizer._operation_budget
            if existing_budget is not None and existing_budget is not evidence_budget:
                raise ReviewInputError(
                    "command finalization budget identity mismatches"
                )
            finalizer = copy(finalizer)
            finalizer.evidence_budget = evidence_budget
            reference = record.finding_lifecycle
            binding = reference["manifest"]["binding"]
            journal = FindingLifecycleJournal(
                self.reviewer,
                ledger,
                record.identity(),
                operation_id=reference["operation_id"],
                base_sha=binding["base_sha"],
                head_sha=binding["head_sha"],
                policy_digest=binding["policy_digest"],
                configuration_digest=binding["configuration_digest"],
                app_slug=app_slug,
                record=record,
                document=document,
                review_token=read_token,
                native_budget=evidence_budget,
            )
            if (
                not isinstance(oidc_token, str)
                or not oidc_token
                or not isinstance(self.broker, BrokerClient)
                or self.broker.control is None
                or self.broker.control.budget is not evidence_budget
            ):
                raise ReviewInputError(
                    "command check capability accounting is unavailable"
                )
            evidence_budget.check()
            check_token = self.broker.exchange(oidc_token, capability="check_publish")
            evidence_budget.check()
            prepared_check = _PreparedCheckCapability(
                self.broker, oidc_token, evidence_budget, check_token
            )
            with journal.transports(finalizer=finalizer):
                plan = finalizer.prepare_command_capacity(
                    token=read_token,
                    check_token=check_token,
                    repository=repository,
                    repository_id=repository_id,
                    pull_request=pull_request,
                    head_sha=head_sha,
                    app_slug=app_slug,
                    actor=command.actor,
                    finding_ids=tuple(
                        item.reference
                        for item in preview_result.receipts
                        if item.outcome in {"recorded", "already-recorded"}
                        and item.reference.startswith("RS-")
                    ),
                    dispositions=decode_session_dispositions(
                        json.loads(details["dispositions"])
                    ),
                    incomplete_coverage=incomplete_coverage,
                    policy_digest=policy_digest,
                    command_finalization_reserve=7,
                )
                journal.preflight_command_finalization(
                    plan,
                    preview_record=preview_record,
                    capacity_details=details,
                    http=finalizer.http,
                    budget=evidence_budget,
                )
        except Exception as error:
            # No dispositions, review-body update, cleanup, check or approval
            # has been attempted. Existing action audit/terminal obligations
            # remain on their original caller allocation.
            reason = " ".join(str(error).split())
            reason = reason.encode("utf-8")[:512].decode("utf-8", errors="ignore")
            return MaintainerCommandResult(
                action=command.action,
                applied=False,
                operator_paused=record.operator_paused,
                summary=(
                    "command finalization capacity unavailable before business writes: "
                    + reason
                ),
            )
        return _PreparedCommandFinalization(
            plan, prepared_check, plan.can_reuse_retained_lifecycle(preview_record)
        )

    def _refinalize_command(
        self,
        result: object,
        *,
        oidc_token: str,
        repository: str,
        repository_id: int,
        pull_request: int,
        head_sha: str,
        app_slug: str,
        actor: str,
        record: object,
        ledger: object | None = None,
        evidence_budget: EvidenceReadBudget | None = None,
        incomplete_coverage: Sequence[str] | None = None,
        policy_digest: str | None = None,
        # Private cross-call reuse: None, or the capability this operation's
        # broker exchange minted. Never a caller-supplied token.
        _prepared_review_capability: _PreparedReviewCapability | None = None,
        _capacity_plan=None,
        _quarantine_lifecycle_transfer: Any = None,
    ) -> object:
        """Re-check approval after a hosted override. A failed check leaves the ledger write."""

        from review_sensei.disposition import (
            MaintainerCommandResult,
            session_dispositions,
        )

        if not isinstance(result, MaintainerCommandResult):
            return result
        command_result: MaintainerCommandResult = result
        prepared_check = None
        reuse_lifecycle = False
        if _capacity_plan is not None:
            if type(_capacity_plan) is not _PreparedCommandFinalization:
                raise ReviewInputError("command finalization preparation is invalid")
            prepared_check = _capacity_plan.check_capability
            reuse_lifecycle = _capacity_plan.reuse_lifecycle
            if type(prepared_check) is not _PreparedCheckCapability:
                raise ReviewInputError("command prepared check capability is invalid")
            _capacity_plan = _capacity_plan.plan
        finalizer = getattr(self.reviewer, "finalizer", None)
        if evidence_budget is not None and isinstance(
            finalizer, ReviewApprovalFinalizer
        ):
            from copy import copy

            existing = (
                finalizer.evidence_budget
                if finalizer.evidence_budget is not None
                else finalizer._operation_budget
            )
            if existing is not None and existing is not evidence_budget:
                raise ReviewInputError(
                    "command finalization budget identity mismatches"
                )
            finalizer = copy(finalizer)
            finalizer.evidence_budget = evidence_budget
        refinalize = getattr(finalizer, "refinalize_with_dispositions", None)
        if not callable(refinalize):
            return command_result
        # Reserve caller requests only: one broker POST, two check requests,
        # and five approval reads. Worker internal fanout has its own service
        # allowance and cannot narrow this caller's cleanup capacity.
        command_finalization_reserve = (0 if prepared_check is not None else 1) + 2 + 5
        failed = (
            command_result.summary
            + "; recorded; gate re-evaluation failed, comment `@reviewsensei verify` to retry"
        )
        from .approval_progress import render_command_progress

        def command_token(budget: EvidenceReadBudget, capability: str) -> str:
            # Use only the original controlled command client. A finalizer
            # with a different or unavailable accounting envelope cannot mint
            # cleanup authority, and never falls back to the review token.
            budget.check()
            if isinstance(self.broker, BrokerClient) and (
                self.broker.control is None or self.broker.control.budget is not budget
            ):
                raise GitHubBrokerClientError(
                    "thread cleanup broker accounting is unavailable"
                )
            token = self.broker.exchange(oidc_token, capability=capability)
            budget.check()
            return token

        try:
            # Reuse only a review_publish capability minted by this operation.
            # The object binds the token to the minting broker and the exact
            # OIDC assertion, so another capability, a hosted session grant or
            # a caller-supplied string cannot reach this finalizer. Redemption
            # is idempotent for that same pair so an in-operation retry cannot
            # fail on a token it never sent.
            if _prepared_review_capability is not None:
                if not isinstance(
                    _prepared_review_capability, _PreparedReviewCapability
                ):
                    raise GitHubPublicationError(
                        "prepared review capability is invalid"
                    )
                review_token = _prepared_review_capability.redeem(
                    self.broker, oidc_token
                )
            else:
                review_token = self.broker.exchange(
                    oidc_token, capability="review_publish"
                )
            from review_sensei.session import SessionRecord

            dispositions = (
                session_dispositions(record, ledger=ledger)
                if isinstance(record, SessionRecord)
                else ()
            )
            finding_ids = tuple(
                item.reference
                for item in command_result.receipts
                if item.outcome in {"recorded", "already-recorded"}
                and item.reference.startswith("RS-")
            )
            if (
                isinstance(self.reviewer, ReviewPublisher)
                and ledger is not None
                and isinstance(record, SessionRecord)
                and record.finding_lifecycle is not None
                and getattr(ledger, "enable_finding_lifecycle_writes", False)
            ):
                from .finding_lifecycle import (
                    FindingLifecycleJournal,
                    restore_finding_lifecycle,
                )

                if reuse_lifecycle:
                    # Admission priced this exact same-caller transfer, not a
                    # second read. A failed custody check cannot fall back to
                    # unpriced reads or restore a competing writer's state.
                    selected_record, retained_journal = (
                        _capacity_plan.reuse_retained_lifecycle(
                            ledger, record, dispositions=dispositions
                        )
                    )
                else:
                    selected_record, retained_journal = restore_finding_lifecycle(
                        self.reviewer,
                        ledger,
                        record.identity(),
                        _retained=_quarantine_lifecycle_transfer,
                    )
                if (
                    selected_record is None
                    or selected_record.record_sha256 != record.record_sha256
                ):
                    raise ReviewInputError(
                        "finding lifecycle command authority changed"
                    )
                binding = cast(Mapping[str, Any], record.finding_lifecycle["manifest"])[
                    "binding"
                ]
                journal = FindingLifecycleJournal(
                    self.reviewer,
                    ledger,
                    record.identity(),
                    operation_id=record.finding_lifecycle["operation_id"],
                    base_sha=binding["base_sha"],
                    head_sha=binding["head_sha"],
                    policy_digest=binding["policy_digest"],
                    configuration_digest=binding["configuration_digest"],
                    app_slug=app_slug,
                    record=record,
                    document=retained_journal,
                    review_token=review_token,
                    native_budget=evidence_budget,
                )
                journal._armed = True
                original_refinalize = refinalize

                def refinalize(**arguments):
                    from .quarantine_command_checkpoint import (
                        phase as cold_checkpoint_phase,
                    )

                    with (
                        journal.transports(finalizer=finalizer),
                        cold_checkpoint_phase(journal),
                    ):
                        execute_refinalize = original_refinalize
                        if _capacity_plan is not None:
                            if prepared_check is None:
                                raise ReviewInputError(
                                    "command prepared check capability is absent"
                                )
                            from .command_finalization_plan import (
                                attach_execution_reads,
                            )

                            bounded_finalizer = attach_execution_reads(
                                _capacity_plan,
                                finalizer,
                                journal,
                                dispositions=dispositions,
                                check_token=_PreparedCheckCapability.redeem(
                                    prepared_check,
                                    self.broker,
                                    oidc_token,
                                    evidence_budget,
                                ),
                            )
                            execute_refinalize = (
                                bounded_finalizer.refinalize_with_dispositions
                            )
                        try:
                            return execute_refinalize(**arguments)
                        finally:
                            journal.finish()

            publication = refinalize(
                token=review_token,
                check_token=None,
                repository=repository,
                repository_id=repository_id,
                pull_request=pull_request,
                head_sha=head_sha,
                app_slug=app_slug,
                actor=actor,
                finding_ids=finding_ids,
                dispositions=dispositions,
                incomplete_coverage=incomplete_coverage,
                policy_digest=policy_digest,
                thread_cleanup_token_provider=lambda budget: command_token(
                    budget, "thread_resolve"
                ),
                check_token_provider=(
                    (
                        lambda budget: _PreparedCheckCapability.redeem(
                            prepared_check, self.broker, oidc_token, budget
                        )
                    )
                    if prepared_check is not None
                    else lambda budget: command_token(budget, "check_publish")
                ),
                command_finalization_reserve=command_finalization_reserve,
            )
        except Exception as exc:
            return render_command_progress(
                replace(command_result, summary=failed, evaluation_failed=True), exc
            )

        if getattr(
            publication, "status", None
        ) == "gate_reevaluation_failed" and getattr(
            publication, "diagnostic", None
        ) in {
            "command_body_capacity_exceeded",
            "command_body_capacity_unverifiable",
            "finding_lifecycle_verified_fixed_subset_capacity_unqualified",
        }:
            return render_command_progress(
                replace(
                    command_result,
                    summary=(
                        command_result.summary
                        + f"; gate re-evaluation blocked ({publication.diagnostic}); "
                        "decisions retained; retain the original review and ledger "
                        "for operator capacity/evidence repair"
                    ),
                    evaluation_failed=True,
                ),
                publication,
            )
        thread_status = getattr(publication, "thread_resolution", None)
        if thread_status:
            command_result = replace(
                command_result,
                evaluation_failed=command_result.evaluation_failed
                or thread_status == "unconfirmed",
                summary=command_result.summary
                + (
                    "; matching threads resolved"
                    if thread_status == "resolved"
                    else "; decision recorded; thread resolution unconfirmed, comment `@reviewsensei verify` to retry"
                ),
            )
        if getattr(publication, "status", None) == "gate_reevaluation_failed":
            return render_command_progress(
                replace(command_result, summary=failed, evaluation_failed=True),
                publication,
            )
        if getattr(publication, "status", None) in {"approved", "already_approved"}:
            suffix = (
                "; approval submitted (maintainer-coverage-exception)"
                if getattr(publication, "diagnostic", None)
                == "maintainer_coverage_exception"
                else "; approval submitted"
            )
            return render_command_progress(
                replace(command_result, summary=command_result.summary + suffix),
                publication,
            )
        progress = getattr(publication, "approval_progress", None)
        if (
            getattr(progress, "approval_state", None) == "not-observed"
            and getattr(publication, "diagnostic", None) == "approval_withheld"
        ):
            return render_command_progress(
                replace(
                    command_result,
                    evaluation_failed=True,
                    summary=command_result.summary
                    + "; approval outcome unconfirmed; reconcile the existing native "
                    "review, then comment `@reviewsensei verify` to retry",
                ),
                publication,
            )
        diagnostic = getattr(publication, "diagnostic", None)
        if diagnostic == "review_incomplete":
            return render_command_progress(
                replace(
                    command_result,
                    summary=(
                        command_result.summary
                        + "; approval withheld (review_incomplete); review evidence "
                        "is still incomplete: verify rechecks finding decisions, "
                        "not mixed media coverage. For unsupported-file coverage, "
                        "use the original media confirmation to validate the "
                        "current head/base and retained receipts. Other AI review "
                        "gaps require review continuation. Inspect the retained "
                        "review before choosing `@reviewsensei review remaining`: "
                        "it validates reusable text scope and refuses missing "
                        "or stale evidence. This check did not establish coverage "
                        "counts or the original media source comment ID."
                    ),
                ),
                publication,
            )
        if diagnostic:
            return render_command_progress(
                replace(
                    command_result,
                    summary=command_result.summary
                    + f"; approval withheld ({diagnostic})",
                ),
                publication,
            )
        return render_command_progress(command_result, publication)

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
        evidence_budget: EvidenceReadBudget | None = None,
        current_action: object | None = None,
        observed_trust: AuthenticatedTrustSnapshot | None = None,
    ) -> ReplyResult:
        """Authorize, generate, validate, and publish one mention reply."""

        if not options.github_writes or not options.mention_replies:
            return ReplyResult(status="disabled")
        if (
            isinstance(self.reviewer, ReviewPublisher)
            and type(repository_id) is int
            and repository_id > 0
        ):
            reply_trust = self.reviewer.check_trust_boundary(
                repository_id=repository_id,
                pull_request=pull_request,
                phase="before_admission",
                operation="reply",
                observed=observed_trust,
                repository_slug=repository,
                head_sha=expected_head_sha,
                has_prior_charges=bool(self.session_ledger is not None),
            )
            if reply_trust is not None and not reply_trust.allowed:
                raise GitHubPublicationError(
                    f"repository trust transition refused ({reply_trust.phase}): {reply_trust.reason_code}"
                )
        if current_action is not None:
            from .run_authority import VerifiedRunAction

            if not isinstance(current_action, VerifiedRunAction):
                raise ReviewInputError("current reply authority is invalid")
            document = current_action.document
            if (
                current_action.run.ledger is not self.session_ledger
                or document["purpose"] != "reply"
                or document["repository"] != repository
                or document["repository_id"] != repository_id
                or document["pull_request"] != pull_request
                or document["head_sha"] != expected_head_sha
                or document["source"]["id"] != source_comment_id
                or document["source"]["updated_at"] != source_updated_at
                or document["source"]["kind"]
                != ("review_comment" if source_kind == "inline" else "issue_comment")
            ):
                raise ReviewInputError("current reply purpose or source conflicts")
            current_action.fence()
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
        # Context preparation, authorization and operation-entry latency all
        # consume this caller's original provider-work deadline.
        work_tracker = ResourceBudgetTracker(budget or ResourceBudget.create())
        replier = self.replier
        broker = self.broker
        shared_evidence_budget = (
            evidence_budget
            or getattr(self.session_ledger, "evidence_budget", None)
            or EvidenceReadBudget()
        )
        if isinstance(replier, ConversationPublisher):
            from .publication_recovery import publication_http

            replier = copy(replier)
            replier.http = publication_http(replier.http, shared_evidence_budget)
            replier.finalizer = copy(replier.finalizer)
            replier.finalizer.http = replier.http
            replier.finalizer.evidence_budget = shared_evidence_budget
            replier.finalizer.human_inventory_read_token = read_token
            if isinstance(broker, BrokerClient):
                if (
                    broker.before_request is not None
                    and broker.before_request != shared_evidence_budget.consume
                ):
                    raise GitHubPublicationError(
                        "reply broker budget identity mismatches"
                    )
                broker = copy(broker)
                broker.before_request = shared_evidence_budget.consume
        operation_owner = copy(self)
        operation_owner.http = (
            replier.http if isinstance(replier, ConversationPublisher) else self.http
        )
        operation_owner.broker = broker
        operation_owner.replier = replier
        if isinstance(replier, ConversationPublisher):
            replier.finalizer.human_inventory_reader_factory = (
                operation_owner._human_inventory_reader
            )
        defer_generic_evidence = (
            isinstance(replier, ConversationPublisher) and operation_entry == "enabled"
        )
        prepared = replier.prepare_context(
            **({"defer_evidence": True} if defer_generic_evidence else {}),
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
        finding_consumer = None
        if isinstance(replier, ConversationPublisher):
            finding_consumer = operation_owner._bind_finding_lifecycle_finalizer(
                replier.finalizer,
                options=options,
                repository=repository,
                repository_id=repository_id,
                pull_request=pull_request,
                head_sha=prepared.head_sha,
                app_slug=app_slug,
                token=read_token,
                evidence_budget=shared_evidence_budget,
            )
        if (
            isinstance(replier, ConversationPublisher)
            and operation_entry == "disabled"
            and finding_consumer is not None
            and finding_consumer.enrolled
        ):
            existing = replier.existing_replied(
                token=read_token,
                repository=repository,
                pull_request=pull_request,
                prepared=prepared,
                app_slug=app_slug,
                producer_id=finding_consumer.authenticated_producer_id(),
            )
            if existing is not None:
                # Legacy delivery proves no structured model decision. Keep
                # approval withheld rather than infer again or guess from prose.
                return existing
        context_binding: _ConversationBaseBinding = (
            {
                "context_base_sha": prepared.context.base_sha,
                "context_base_ref": prepared.context.base_ref,
            }
            if isinstance(replier, ConversationPublisher)
            else {}
        )
        source_body = (
            prepared.context.messages[-1].body if prepared.context.messages else ""
        )
        from review_sensei.disposition import parse_maintainer_command

        command = parse_maintainer_command(source_body, actor="maintainer")
        if command is not None and len(command.finding_references) > 1:
            capability_token = broker.exchange(
                oidc_token or broker.request_oidc_token(),
                capability=(
                    "inline_reply"
                    if prepared.source_kind == "inline"
                    else "issue_reply"
                ),
            )
            return replier.publish(
                token=capability_token,
                repository=repository,
                pull_request=pull_request,
                source_comment_id=prepared.source_comment_id,
                source_updated_at=prepared.source_updated_at,
                head_sha=prepared.head_sha,
                reply=ConversationReply(
                    body=(
                        "This is a maintainer command, but this repository's "
                        "ReviewSensei caller workflow predates multi-finding "
                        "overrides, so it was not applied. Re-run setup to update "
                        "the caller, or post one override per comment."
                    )
                ),
                app_slug=app_slug,
                root_comment_id=prepared.root_comment_id,
                source_kind=prepared.source_kind,
                **context_binding,
            )
        capability_token = broker.exchange(
            oidc_token or broker.request_oidc_token(),
            capability=(
                "inline_reply" if prepared.source_kind == "inline" else "issue_reply"
            ),
        )
        # Validate human authority before the first visible processing mutation.
        # A malformed owned root is a held operation, not a generic reply.
        assessor = None
        human = None
        if isinstance(replier, ConversationPublisher):
            assessor = HumanAssessmentPublisher(
                http=replier.http,
                qualification_authority=replier.finalizer.qualification_authority,
                evidence_budget=shared_evidence_budget,
                human_inventory_reader_factory=operation_owner._human_inventory_reader,
                human_inventory_read_token=read_token,
            )
            if finding_consumer is not None:
                finding_consumer.bind(assessor.finalizer)
            human = assessor.prepare(
                token=read_token,
                repository=repository,
                pull_request=pull_request,
                prepared=prepared,
                app_slug=app_slug,
                work_budgets=work_budgets,
                work_tracker=work_tracker,
            )
        if human is None and defer_generic_evidence:
            # Generic conversation inference still receives its complete context.
            # Its fresh authorization checks also fence any change during the
            # read-only human-inventory lookup.
            full_context = replier.prepare_context(
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
            if not isinstance(full_context, PreparedConversation):
                return full_context
            if (
                full_context.context.base_sha,
                full_context.context.base_ref,
            ) != (prepared.context.base_sha, prepared.context.base_ref):
                return ReplyResult(status="skipped_stale_base")
            prepared = full_context
        # A large evidence operation prioritizes durable reply/decision work
        # within the same allowance over a transient processing indicator.
        reaction = (
            None
            if human is not None
            and (
                human.eligibility.human_inventory_reference is not None
                or (
                    human.eligibility.human_review is not None
                    and len(human.eligibility.human_review.findings)
                    > MAX_HUMAN_FINDINGS
                )
            )
            else replier.add_processing_reaction(
                token=capability_token,
                repository=repository,
                source_comment_id=prepared.source_comment_id,
                source_kind=prepared.source_kind,
            )
        )
        try:
            if isinstance(replier, ConversationPublisher):
                assert assessor is not None
                if human is not None:
                    assert human.eligibility.human_review is not None
                    inventory = human.eligibility.human_review
                    work_assessment = None
                    if human.evidence_diagnostic:
                        assessment = HumanAssessmentReply(
                            body=f"Human reassessment has insufficient current diff evidence (reason: `{human.evidence_diagnostic}`). No findings were cleared; approval requirements remain unchanged. After the required evidence is available within the context budget, rerun a full review for the current head and submit a new authorized mention.",
                            decisions=(),
                        )
                    elif human.evidence_bundle is not None and (
                        inventory.pending or operation_entry == "enabled"
                    ):
                        evidence_bundle = human.evidence_bundle
                        produced = operation_owner._through_operation(
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
                            encode=lambda value: _assessment_bytes(
                                value, replay_context=human.replay_context_digest
                            ),
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
                        produced_reply = operation_owner._through_operation(
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
                            encode=lambda value: _assessment_bytes(
                                value, replay_context=human.replay_context_digest
                            ),
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
                    # Stored results are bound to the original prepared bytes.
                    # Fresh work is validated by publish's validate_work/evidence
                    # checks and its live source/head/base/result fences instead.
                    if (
                        isinstance(assessment, _RecordedHumanAssessmentReply)
                        and assessment.replay_context != human.replay_context_digest
                    ):
                        raise ReviewInputError(
                            "human assessment replay authority changed; submit a new authorized mention"
                        )
                    accepted = not inventory.pending or any(
                        item.decision != "unresolved" for item in assessment.decisions
                    )
                    if work_assessment is not None:
                        accepted = accepted or work_assessment.inventory != inventory
                    review_policy = options.review_policy()
                    review_token = None
                    check_token = None
                    if accepted:
                        exchange_input = oidc_token or broker.request_oidc_token()
                        review_token = broker.exchange(
                            exchange_input,
                            capability="review_publish",
                        )
                        check_token = self._check_capability_token(
                            exchange_input, broker=broker
                        )
                    reply_outcome = assessor.publish(
                        token=capability_token,
                        review_token=review_token,
                        check_token=check_token,
                        reviews_policy=review_policy,
                        repository=repository,
                        pull_request=pull_request,
                        prepared=human,
                        reply=work_assessment
                        if work_assessment is not None
                        else assessment,
                        app_slug=app_slug,
                        recorded_replay=isinstance(
                            assessment, _RecordedHumanAssessmentReply
                        ),
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
                        retained = human.eligibility.with_fresh_human_review(
                            updated_inventory
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
                        completed_reply = replace(
                            reply_outcome,
                            assessment_status="broader_review_" + publication.status,
                            assessment_diagnostic=publication.diagnostic,
                            finalization_completion=None,
                        )
                        from .action_completion import (
                            reply_completion_from_finalization,
                            reply_source,
                        )

                        completion = reply_completion_from_finalization(
                            publication,
                            completed_reply,
                            reply_source(
                                repository=repository,
                                pull_request=pull_request,
                                head_sha=prepared.head_sha,
                                kind=prepared.source_kind,
                                source_id=prepared.source_comment_id,
                                updated_at=prepared.source_updated_at,
                                body=human.source_body,
                            ),
                            result_digest=work_assessment.discovery.result.content_digest(),
                        )
                        return replace(
                            completed_reply, finalization_completion=completion
                        )
                    return reply_outcome
            reply = operation_owner._through_operation(
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
                    json.dumps(
                        {
                            "kind": "reply-context-v2",
                            "repository_id": repository_id,
                            "pull_request": pull_request,
                            "head_sha": prepared.head_sha,
                            "base_sha": prepared.context.base_sha,
                            "base_ref": prepared.context.base_ref,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
                repository=repository,
                oidc_token=oidc_token,
                execute=lambda: ConversationService(reply_provider).reply(
                    prepared.context,
                    model=model,
                    tracker=work_tracker,
                ),
                encode=_conversation_bytes,
                decode=_conversation_from_bytes,
            )
            if not isinstance(reply, ConversationReply):
                raise ReviewInputError("operation entry refused: result_missing")
            publish = getattr(replier, "publish_current", replier.publish)
            resolution_evidence = None
            diff_context = prepared.context.diff_context
            if (
                reply.resolve
                and isinstance(replier, ConversationPublisher)
                and isinstance(diff_context, str)
                and diff_context
            ):
                resolution_evidence = sha256(diff_context.encode("utf-8")).hexdigest()
            if resolution_evidence is None:
                return publish(
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
                    **context_binding,
                )
            try:
                resolution_token = broker.exchange(
                    oidc_token or broker.request_oidc_token(),
                    capability="review_publish",
                )
            except (GitHubBrokerClientError, GitHubHTTPTransientError, OSError):
                resolution_token = None
            return publish(
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
                resolution_token=resolution_token,
                resolution_evidence=resolution_evidence,
                resolution_base_sha=prepared.context.base_sha,
                **context_binding,
            )
        finally:
            if reaction is not None:
                replier.remove_processing_reaction(
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
    *,
    reviewer: Any = None,
) -> Any:
    """Fail closed when the broker witnessed a marker the ledger cannot find."""

    if ledger is not None and session_state == "known":
        loaded = ledger.load(identity)
        rid = identity.repository_id
        pr = identity.pull_request
        can_record = (
            isinstance(reviewer, ReviewPublisher)
            and type(rid) is int
            and rid > 0
            and type(pr) is int
            and pr > 0
        )
        if loaded.status == "missing":
            if can_record:
                assert isinstance(rid, int)
                reviewer.observe_artifact(
                    repository_id=rid,
                    pull_request=pr,
                    observed=ObservedGitHubArtifact(
                        artifact_kind="session_ledger_marker",
                        artifact_id=f"session:{rid}:{pr}",
                        present=False,
                        duplicate_count=0,
                    ),
                    identity=identity,
                    enrollment_witness_present=True,
                )
            # The broker's authenticated witness says this session already
            # exists, so a missing marker means the comment was deleted. Fail
            # closed before any ledger call below can re-create it, and name
            # the recovery command so an operator does not have to infer it.
            raise GitHubPublicationError(
                "session ledger marker is missing; authenticated recovery is "
                "required: a maintainer must comment `@reviewsensei review reenroll` "
                "to re-establish this session"
            )
        if loaded.status == "conflict":
            if can_record:
                assert isinstance(rid, int)
                reviewer.observe_artifact(
                    repository_id=rid,
                    pull_request=pr,
                    observed=ObservedGitHubArtifact(
                        artifact_kind="session_ledger_marker",
                        artifact_id=f"session:{rid}:{pr}",
                        present=True,
                        duplicate_count=2,
                    ),
                    identity=identity,
                    enrollment_witness_present=True,
                )
            raise GitHubPublicationError(
                "session ledger marker conflict; duplicate session markers detected"
            )
        if loaded.status == "integrity-failed":
            if can_record:
                assert isinstance(rid, int)
                reviewer.observe_artifact(
                    repository_id=rid,
                    pull_request=pr,
                    observed=ObservedGitHubArtifact(
                        artifact_kind="session_ledger_marker",
                        artifact_id=f"session:{rid}:{pr}",
                        present=True,
                        edited_by_human=True,
                    ),
                    identity=identity,
                    enrollment_witness_present=True,
                )
            raise GitHubPublicationError(
                "session ledger marker integrity failed; authority refused"
            )
        if loaded.status == "expired":
            if can_record:
                assert isinstance(rid, int)
                reviewer.observe_artifact(
                    repository_id=rid,
                    pull_request=pr,
                    observed=ObservedGitHubArtifact(
                        artifact_kind="session_ledger_marker",
                        artifact_id=f"session:{rid}:{pr}",
                        present=True,
                        stale_generation=True,
                    ),
                    identity=identity,
                    enrollment_witness_present=True,
                )
            raise GitHubPublicationError(
                "session ledger marker expired; authenticated recovery is required"
            )
        if isinstance(ledger, GitHubIssueCommentSessionLedger):
            edited_cid: int | None = None
            for cid, citem in ledger._comments_by_id.items():
                cbody = citem.get("body")
                if (
                    isinstance(cbody, str)
                    and _marker_shaped(cbody)
                    and citem.get("edited_by_human") is True
                ):
                    edited_cid = cid
                    break
            if edited_cid is not None:
                if can_record:
                    assert isinstance(rid, int)
                    reviewer.observe_artifact(
                        repository_id=rid,
                        pull_request=pr,
                        observed=ObservedGitHubArtifact(
                            artifact_kind="session_ledger_marker",
                            artifact_id=str(edited_cid),
                            present=True,
                            edited_by_human=True,
                        ),
                        identity=identity,
                        enrollment_witness_present=True,
                    )
                raise GitHubPublicationError(
                    "session ledger marker was edited by a human; authority refused"
                )
        if can_record and loaded.record is not None:
            assert isinstance(rid, int)
            reviewer.observe_artifact(
                repository_id=rid,
                pull_request=pr,
                observed=ObservedGitHubArtifact(
                    artifact_kind="session_ledger_marker",
                    artifact_id=f"session:{rid}:{pr}",
                    present=True,
                    expected_sha256=loaded.record.record_sha256,
                    observed_sha256=loaded.record.record_sha256,
                ),
                identity=identity,
                enrollment_witness_present=True,
            )
        return loaded
    return None


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
    evidence_budget: EvidenceReadBudget | None = None,
    analysis_metadata_phases: bool = False,
) -> SessionLedger:
    raise ReviewInputError(
        "hosted phase ledger resolution is retired; use the current run-owned ledger"
    )


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
    assert_no_deferred_leakage(shadow)
    from .action_completion import rebind_publication_metadata

    updated = replace(result, shadow=dict(shadow))
    return replace(
        updated, finalization_completion=rebind_publication_metadata(result, updated)
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


from .empty_original_admission import install_application as _install_empty_original  # noqa: E402

_install_empty_original(GitHubApplication)
del _install_empty_original


from review_sensei.publication_intent_fusion import (  # noqa: E402
    install_application as _install_fusion,
)

_install_fusion(GitHubApplication)
del _install_fusion


from .materialized_publication_phase import (  # noqa: E402
    install_application as _install_materialized_phase,
)

_install_materialized_phase(GitHubApplication)
del _install_materialized_phase


from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
