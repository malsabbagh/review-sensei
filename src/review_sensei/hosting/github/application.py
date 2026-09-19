"""Compose GitHub capabilities, publishers, and opt-ins for issue #64."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from ...convergence import (
    OPERATOR_REVIEW_MODES,
    BlockerCandidate,
    ReviewConvergencePolicy,
)
from ...conversation import ConversationService
from ...coverage import coverage_approval_state
from ...errors import ReviewInputError
from ...models import ReviewResult
from ...outcomes import RecoveryArtifact
from ...providers.base import ReviewProvider
from ...session import (
    SessionIdentity,
    SessionLedger,
    admission_diagnostic,
    complete_session_round,
    prepare_session_round,
    record_session_failed_attempt,
    session_reservation_id,
    should_skip_automation,
)
from ...verifier import CandidateFinding
from .approval import has_blocking_findings, has_human_adjudication_findings
from .broker_client import BrokerClient
from .conversation import (
    ConversationPublisher,
    PreparedConversation,
    ReplyResult,
)
from .errors import GitHubPublicationError
from .http import GitHubHttp
from .learning_pr import LearningPRPublisher, LearningPRResult
from .publication import PublicationResult, ReviewPublisher


@dataclass(frozen=True)
class GitHubWriteOptions:
    """GitHub publication switches with default-on approval safety gates."""

    auto_review: bool = False
    auto_approve: bool = True
    github_writes: bool = False
    learning_prs: bool = False
    mention_replies: bool = False
    upload_artifacts: bool = False
    github_session_ledger: bool = False


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
        continuation_rounds: int = 0,
        no_progress: bool = False,
    ) -> PublicationResult:
        if not options.github_writes or not options.auto_review:
            return PublicationResult(status="disabled")
        if base_branch is None or base_sha is None:
            raise GitHubPublicationError(
                "review publication requires expected base branch and sha"
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
        token = self.broker.exchange(
            oidc_token or self.broker.request_oidc_token(),
            capability="review_publish",
        )
        ledger = self._session_ledger_for_token(
            token, options=options, app_slug=app_slug
        )
        identity = SessionIdentity(
            repository=repository,
            pull_request=pull_request,
            repository_id=repository_id,
        )
        policy = (
            convergence_policy
            if isinstance(convergence_policy, ReviewConvergencePolicy)
            else ReviewConvergencePolicy()
        )
        prepared = None
        reservation = session_reservation_id(
            repository=repository,
            pull_request=pull_request,
            head_sha=head_sha,
            kind="publish",
        )
        if ledger is not None:
            if not isinstance(head_sha, str) or not head_sha.strip():
                raise GitHubPublicationError(
                    "session ledger requires a non-empty head_sha"
                )
            try:
                flags = _publication_round_flags(result, no_progress=no_progress)
                prepared = prepare_session_round(
                    ledger,
                    identity,
                    policy,
                    reservation_id=reservation,
                    continuation_rounds=continuation_rounds,
                    coverage_complete=flags["coverage_complete"],
                    independently_approval_eligible=flags[
                        "independently_approval_eligible"
                    ],
                    latest_head_reviewed=flags["latest_head_reviewed"],
                    no_progress=flags["no_progress"],
                )
                if should_skip_automation(prepared.decision, inference=False):
                    return PublicationResult(
                        status="handoff",
                        diagnostic=admission_diagnostic(prepared.decision),
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
        authorized_dispositions: tuple[object, ...] = ()
        if ledger is not None:
            from ...disposition import session_dispositions

            if prepared is not None:
                authorized_dispositions = session_dispositions(prepared.record)
        try:
            publication = self.reviewer.publish(
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
                auto_approve=options.auto_approve,
                candidates=candidates,
                snapshot=snapshot,
                snapshot_sha256=snapshot_sha256,
                evidence_policy=evidence_policy,
                convergence_policy=convergence_policy,
                blocker_candidates=blocker_candidates,
                input_blocker_candidates=input_blocker_candidates,
                authorized_dispositions=authorized_dispositions,
            )
        except BaseException as publication_error:
            if ledger is not None and policy.mode in OPERATOR_REVIEW_MODES:
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
                            ledger, identity, reservation_id=reservation
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
        if ledger is not None and prepared is not None:
            complete_session_round(
                ledger,
                identity,
                prepared,
                published=publication.status == "published",
            )
        return publication

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
    ):
        """Apply an authenticated maintainer command. Never exchanges when writes are off."""

        from ...disposition import (
            MaintainerCommandResult,
            apply_session_command,
            authorized_maintainer,
            parse_maintainer_command,
        )

        command = parse_maintainer_command(body, actor=actor_login, head_sha=head_sha)
        if command is None:
            return MaintainerCommandResult(
                action="status",
                applied=False,
                operator_paused=False,
                summary="not-a-command",
            )
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
            else:
                # Every hosted mutation is broker-authorized. The caller may
                # supply the OIDC assertion, but it is never treated as a
                # capability token or as proof of maintainer identity.
                exchange_input = oidc_token or self.broker.request_oidc_token()
                capability = "review_publish"
            token = self.broker.exchange(
                exchange_input,
                capability=capability,
            )
            ledger = self._session_ledger_for_token(
                token,
                options=options,
                prefer_remote=command.action == "status",
            )
        if ledger is None:
            raise GitHubPublicationError("maintainer commands require a session ledger")
        _record, result = apply_session_command(ledger, identity, command)
        return result

    def _session_ledger_for_token(
        self,
        token: str,
        *,
        options: GitHubWriteOptions,
        app_slug: str | None = None,
        prefer_remote: bool = False,
    ) -> SessionLedger | None:
        if self.session_ledger is not None and not prefer_remote:
            return self.session_ledger
        if not options.github_session_ledger:
            return None
        from .session_ledger import GitHubIssueCommentSessionLedger

        if self.http is None:
            raise GitHubPublicationError("GitHub session ledger requires HTTP")
        return GitHubIssueCommentSessionLedger(
            self.http, token=token, app_slug=app_slug
        )

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
        if result.review_status == "incomplete":
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
        token = self.broker.exchange(
            oidc_token or self.broker.request_oidc_token(),
            capability="review_publish",
        )
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
            auto_approve=options.auto_approve,
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
            auto_approve=options.auto_approve,
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
    ) -> ReplyResult:
        """Authorize, generate, validate, and publish one mention reply."""

        if not options.github_writes or not options.mention_replies:
            return ReplyResult(status="disabled")
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
            reply = ConversationService(reply_provider).reply(
                prepared.context,
                model=model,
            )
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
                auto_approve=options.auto_approve,
            )
        finally:
            self.replier.remove_processing_reaction(
                token=capability_token,
                repository=repository,
                source_comment_id=prepared.source_comment_id,
                source_kind=prepared.source_kind,
                reaction_id=reaction.reaction_id,
            )


def _publication_round_flags(
    result: ReviewResult, *, no_progress: bool = False
) -> dict[str, bool]:
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
        "no_progress": no_progress,
    }
