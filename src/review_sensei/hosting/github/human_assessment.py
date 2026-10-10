"""Refresh published eligibility after an authorized, evidence-backed human reply."""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from ...budgets import ReviewWorkBudgets
from ...errors import ReviewInputError
from ...evidence import EvidenceBundle, evidence_digest
from ...feedback import (
    MAX_FEEDBACK_BYTES,
    FeedbackAdmissionError,
    FeedbackReference,
    FeedbackSelection,
    FeedbackSelector,
    FeedbackSource,
    validate_feedback_context,
    validate_feedback_references,
)
from ...human_assessment import (
    HUMAN_ASSESSMENT_EVIDENCE_DIAGNOSTICS,
    MAX_HUMAN_SOURCE_BYTES,
    HumanAssessmentReply,
    validate_assessment_evidence,
)
from ...models import ConversationReply
from ...outcomes import ResourceBudgetTracker
from ...reassessment_work import HumanAssessmentWork, validate_work
from .approval import ReviewApprovalEligibility
from .conversation import (
    ConversationPublisher,
    PreparedConversation,
    ReplyResult,
    authorized_human_comment,
    has_standalone_sensei_mention,
)
from .errors import GitHubConversationError, GitHubPublicationError
from .http import GitHubHttp
from .publication import ReviewApprovalFinalizer, approval_eligibility_marker


class FeedbackBudgetHttp(GitHubHttp):
    """Operation-scoped transport: charge every request, including writes.

    The caller supplies one original budget callback. Inherited pagination and
    evidence retries dispatch through this request method, so each physical
    attempt is charged once. No counter, deadline, retry or activation allowance
    is created here. Storage-tail tickets are outside this adapter's scope.
    """

    def __init__(self, *, http: GitHubHttp, before_read: Callable[[], float]) -> None:
        if isinstance(http, FeedbackBudgetHttp) or not callable(before_read):
            raise FeedbackAdmissionError("feedback_read_budget_exhausted")
        super().__init__(api_url=http.api_url, timeout=getattr(http, "timeout", 30))
        self.transport = http
        self.before_read = before_read

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str,
        body: dict[str, object] | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[int, dict[str, Any] | list[Any] | None]:
        remaining = self.before_read()
        _require_feedback_timeout(remaining)
        if timeout_seconds is not None:
            if (
                isinstance(timeout_seconds, bool)
                or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds)
                or timeout_seconds <= 0
            ):
                raise FeedbackAdmissionError("feedback_read_budget_exhausted")
            remaining = min(remaining, timeout_seconds)
        started = time.monotonic()
        result = self.transport.request(
            method, path, token=token, body=body, timeout_seconds=remaining
        )
        if time.monotonic() - started >= remaining:
            # A late write may already exist remotely. Keep the original charge
            # and let the caller reconcile its exact marker; never retry fresh.
            raise FeedbackAdmissionError("feedback_read_budget_exhausted")
        return result


def _require_feedback_timeout(timeout: float) -> None:
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or not 0 < timeout <= 60
    ):
        raise FeedbackAdmissionError("feedback_read_budget_exhausted")


@dataclass(frozen=True)
class PreparedHumanAssessment:
    conversation: PreparedConversation
    eligibility: ReviewApprovalEligibility
    source_body: str
    source_actor: str
    evidence_diagnostic: str | None = None
    evidence_bundle: EvidenceBundle | None = None
    source_metadata_digest: str | None = None
    feedback: FeedbackSelection | None = None

    @property
    def authority_digest(self) -> str:
        authority: dict[str, object] = {
            "repository": self.evidence_bundle.snapshot.repository
            if self.evidence_bundle is not None
            else None,
            "head": self.conversation.head_sha,
            "eligibility": self.eligibility.to_dict(),
            "source_kind": self.conversation.source_kind,
            "source_id": self.conversation.source_comment_id,
            "source_updated_at": self.conversation.source_updated_at,
            "source_body_sha256": hashlib.sha256(self.source_body.encode()).hexdigest(),
            "source_actor": self.source_actor,
            "source_metadata_digest": self.source_metadata_digest,
        }
        if self.feedback is not None:
            authority["feedback_digest"] = self.feedback.digest
        return evidence_digest(authority)

    def __post_init__(self) -> None:
        if self.feedback is not None:
            feedback = self.feedback
            trigger = FeedbackReference(
                self.conversation.source_kind,
                self.conversation.source_comment_id,
                self.conversation.source_updated_at,
            )
            if (
                not isinstance(feedback, FeedbackSelection)
                or feedback.trigger != trigger
                or feedback.base_sha != self.conversation.context.base_sha
                or feedback.head_sha != self.conversation.head_sha
                or self.eligibility.human_review is None
                or feedback.base_sha != self.eligibility.human_review.base_sha
            ):
                raise FeedbackAdmissionError("feedback_snapshot_invalid")
            original = next(
                item for item in feedback.sources if item.reference == trigger
            )
            if (
                original.body != self.source_body
                or original.author != self.source_actor
                or (
                    trigger.kind == "inline"
                    and original.root_comment_id != self.conversation.root_comment_id
                )
            ):
                raise FeedbackAdmissionError("feedback_source_changed")
        if self.evidence_bundle is not None and (
            self.evidence_bundle.snapshot.head_sha != self.conversation.head_sha
            or self.evidence_bundle.snapshot.base_sha
            != self.conversation.context.base_sha
            or self.conversation.context.diff_context is not None
            or (
                self.feedback is not None
                and (
                    self.feedback.repository != self.evidence_bundle.snapshot.repository
                    or self.feedback.pull_request
                    != self.evidence_bundle.snapshot.pull_request
                )
            )
        ):
            raise GitHubConversationError(
                "human assessment aggregate snapshot is invalid"
            )
        if self.evidence_diagnostic is not None and (
            self.evidence_diagnostic not in HUMAN_ASSESSMENT_EVIDENCE_DIAGNOSTICS
            or self.conversation.context.diff_context is not None
        ):
            raise GitHubConversationError(
                "human assessment evidence diagnostic is invalid"
            )


class HumanAssessmentPublisher:
    """Keep reply authority, inference evidence and review authority distinct."""

    def __init__(
        self, *, http: GitHubHttp, before_read: Callable[[], float] | None = None
    ) -> None:
        if before_read is not None:
            http = FeedbackBudgetHttp(http=http, before_read=before_read)
        self.before_read = (
            http.before_read if isinstance(http, FeedbackBudgetHttp) else None
        )
        self.http = http
        self.conversation = ConversationPublisher(http=http)
        self.finalizer = ReviewApprovalFinalizer(http=http)

    @staticmethod
    def _source_metadata_digest(source: dict[str, Any]) -> str:
        user = source["user"]
        return evidence_digest(
            {
                "comment_id": source.get("id"),
                "author_id": user.get("id"),
                "author": user.get("login"),
                "author_type": user.get("type"),
                "association": source["author_association"].upper(),
                "inline_parent_id": source.get("in_reply_to_id"),
            }
        )

    def source_matches(
        self, source: dict[str, Any] | None, prepared: PreparedHumanAssessment
    ) -> bool:
        """Fence complete body and actor metadata, including immutable account ID."""
        return (
            source is not None
            and source["body"] == prepared.source_body
            and source["user"]["login"] == prepared.source_actor
            and self._source_metadata_digest(source) == prepared.source_metadata_digest
        )

    def source_fence(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        prepared: PreparedHumanAssessment,
        app_slug: str,
    ) -> bool:
        """One complete source fence, sharing preparation/publication transport."""
        if prepared.feedback is not None:
            if (
                self.before_read is None
                or prepared.feedback.repository != repository
                or prepared.feedback.pull_request != pull_request
            ):
                raise FeedbackAdmissionError("feedback_snapshot_invalid")
            self.revalidate_feedback(
                token=token,
                feedback=prepared.feedback,
                app_slug=app_slug,
                before_read=self.before_read,
            )
            return True
        source = self._source(
            token=token,
            repository=repository,
            pull_request=pull_request,
            prepared=prepared.conversation,
            app_slug=app_slug,
        )
        return self.source_matches(source, prepared)

    def _feedback_source(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        reference: FeedbackReference,
        trigger: FeedbackReference,
        app_slug: str,
        before_read: Callable[[], float],
    ) -> FeedbackSource:
        suffix = "pulls" if reference.kind == "inline" else "issues"
        if self.before_read is not None:
            if before_read is not self.before_read:
                raise FeedbackAdmissionError("feedback_read_budget_exhausted")
            # The operation transport charges this dispatch, not its source caller.
            timeout = None
        else:
            timeout = before_read()
            _require_feedback_timeout(timeout)
        started = time.monotonic()
        status, source = self.http.request(
            "GET",
            self.http.repository_path(
                repository, f"/{suffix}/comments/{reference.comment_id}"
            ),
            token=token,
            timeout_seconds=timeout,
        )
        if timeout is not None and time.monotonic() - started >= timeout:
            raise FeedbackAdmissionError("feedback_read_budget_exhausted")
        if status == 404:
            raise FeedbackAdmissionError("feedback_source_missing")
        if status != 200 or not isinstance(source, dict):
            raise FeedbackAdmissionError("feedback_source_lookup_failed")
        if type(source.get("id")) is not int or source["id"] != reference.comment_id:
            raise FeedbackAdmissionError("feedback_source_identity_invalid")
        try:
            self.conversation._require_comment_association(
                comment=source,
                repository=repository,
                pull_request=pull_request,
                source_kind=reference.kind,
            )
        except GitHubConversationError as exc:
            raise FeedbackAdmissionError("feedback_source_association_invalid") from exc
        if (
            not authorized_human_comment(source, app_slug=app_slug)
            or source["user"].get("type") != "User"
        ):
            raise FeedbackAdmissionError("feedback_source_unauthorized")
        if source.get("updated_at") != reference.updated_at:
            raise FeedbackAdmissionError("feedback_source_changed")
        if reference == trigger and not has_standalone_sensei_mention(
            source.get("body")
        ):
            raise FeedbackAdmissionError("feedback_trigger_mention_missing")
        body = source.get("body")
        if not isinstance(body, str):
            raise FeedbackAdmissionError("feedback_source_invalid")
        return FeedbackSource(
            reference=reference,
            author=source["user"]["login"],
            author_id=source["user"].get("id"),
            association=source["author_association"].upper(),
            body=body,
            root_comment_id=source.get("in_reply_to_id", reference.comment_id)
            if reference.kind == "inline"
            else None,
        )

    def load_feedback(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        prepared: PreparedConversation,
        app_slug: str,
        references: tuple[FeedbackReference, ...],
        before_read: Callable[[], float],
        target_ids: tuple[str, ...] = (),
    ) -> FeedbackSelection:
        """Admit an explicit trusted selection; never enumerate or parse chat.

        The caller must validate target IDs against the exact pending inventory
        and bind this selection to the durable operation before inference.
        Ordinary conversation/CLI callers do not opt in implicitly.
        before_read charges each physical read attempt against the caller's
        shared A/D/E operation envelope and returns its remaining timeout.
        """
        trigger = FeedbackReference(
            prepared.source_kind, prepared.source_comment_id, prepared.source_updated_at
        )
        validate_feedback_references(references, trigger=trigger)
        if prepared.context.base_sha is None:
            raise FeedbackAdmissionError("feedback_snapshot_invalid")
        validate_feedback_context(
            repository=repository,
            pull_request=pull_request,
            base_sha=prepared.context.base_sha,
            head_sha=prepared.head_sha,
            target_ids=target_ids,
        )
        guard = before_read
        sources = []
        total_bytes = 0
        for reference in references:
            source = self._feedback_source(
                token=token,
                repository=repository,
                pull_request=pull_request,
                reference=reference,
                trigger=trigger,
                app_slug=app_slug,
                before_read=guard,
            )
            if (
                reference == trigger
                and reference.kind == "inline"
                and source.root_comment_id != prepared.root_comment_id
            ):
                raise FeedbackAdmissionError("feedback_source_association_invalid")
            total_bytes += source.body_bytes
            if total_bytes > MAX_FEEDBACK_BYTES:
                raise FeedbackAdmissionError("feedback_total_oversized")
            sources.append(source)
        return FeedbackSelection(
            repository=repository,
            pull_request=pull_request,
            base_sha=prepared.context.base_sha,
            head_sha=prepared.head_sha,
            trigger=trigger,
            sources=tuple(sources),
            target_ids=target_ids,
        )

    def load_selected_feedback(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        prepared: PreparedConversation,
        app_slug: str,
        selector: FeedbackSelector | None = None,
    ) -> FeedbackSelection:
        """Complete single trigger by default, explicit operator tuple otherwise.

        This explicit rich API requires an operation-bound transport. It never
        selects sources/targets from the bounded conversation archive or prose.
        """
        if self.before_read is None:
            raise FeedbackAdmissionError("feedback_read_budget_exhausted")
        trigger = FeedbackReference(
            prepared.source_kind, prepared.source_comment_id, prepared.source_updated_at
        )
        if selector is None:
            if prepared.context.base_sha is None:
                raise FeedbackAdmissionError("feedback_snapshot_invalid")
            selector = FeedbackSelector(
                repository,
                pull_request,
                prepared.context.base_sha,
                prepared.head_sha,
                trigger,
                (trigger,),
            )
        if (
            not isinstance(selector, FeedbackSelector)
            or selector.repository != repository
            or selector.pull_request != pull_request
            or selector.base_sha != prepared.context.base_sha
            or selector.head_sha != prepared.head_sha
            or selector.trigger != trigger
        ):
            raise FeedbackAdmissionError("feedback_snapshot_invalid")
        return self.load_feedback(
            token=token,
            repository=repository,
            pull_request=pull_request,
            prepared=prepared,
            app_slug=app_slug,
            references=selector.references,
            target_ids=selector.target_ids,
            before_read=self.before_read,
        )

    def prepare_feedback(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        prepared: PreparedConversation,
        app_slug: str,
        selector: FeedbackSelector | None = None,
        work_budgets: ReviewWorkBudgets | None = None,
        work_tracker: ResourceBudgetTracker | None = None,
    ) -> PreparedHumanAssessment | None:
        """Opt-in rich preparation; legacy prepare remains the default route."""
        feedback = self.load_selected_feedback(
            token=token,
            repository=repository,
            pull_request=pull_request,
            prepared=prepared,
            app_slug=app_slug,
            selector=selector,
        )
        return self.prepare(
            token=token,
            repository=repository,
            pull_request=pull_request,
            prepared=prepared,
            app_slug=app_slug,
            work_budgets=work_budgets,
            work_tracker=work_tracker,
            feedback=feedback,
        )

    def revalidate_feedback(
        self,
        *,
        token: str,
        feedback: FeedbackSelection,
        app_slug: str,
        before_read: Callable[[], float],
    ) -> None:
        """Fence every selected comment before publication/receipt activation.

        GitHub has no atomic multi-comment read. The caller must also fence the
        exact current PR snapshot/latest inventory before activating authority.
        Reuse the same before_read accounting/deadline as admission and parts;
        constructing a fresh allowance for this fence is unsupported.
        """
        guard = before_read
        for original in feedback.sources:
            current = self._feedback_source(
                token=token,
                repository=feedback.repository,
                pull_request=feedback.pull_request,
                reference=original.reference,
                trigger=feedback.trigger,
                app_slug=app_slug,
                before_read=guard,
            )
            if current != original:
                raise FeedbackAdmissionError("feedback_source_changed")

    def _source(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        prepared: PreparedConversation,
        app_slug: str,
    ) -> dict[str, Any] | None:
        suffix = "pulls" if prepared.source_kind == "inline" else "issues"
        status, source = self.http.request(
            "GET",
            self.http.repository_path(
                repository, f"/{suffix}/comments/{prepared.source_comment_id}"
            ),
            token=token,
        )
        if status == 404:
            return None
        if status != 200 or not isinstance(source, dict):
            raise GitHubConversationError("human assessment source lookup failed")
        if (
            type(source.get("id")) is not int
            or source["id"] != prepared.source_comment_id
        ):
            return None
        self.conversation._require_comment_association(
            comment=source,
            repository=repository,
            pull_request=pull_request,
            source_kind=prepared.source_kind,
        )
        if (
            not authorized_human_comment(source, app_slug=app_slug)
            or source["user"].get("type") != "User"
            or not has_standalone_sensei_mention(source.get("body"))
            or source.get("updated_at") != prepared.source_updated_at
        ):
            return None
        body = source.get("body")
        if not isinstance(body, str):
            return None
        try:
            if len(body.encode("utf-8")) > MAX_HUMAN_SOURCE_BYTES:
                return None
        except UnicodeError:
            return None
        return source

    def prepare(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        prepared: PreparedConversation,
        app_slug: str,
        work_budgets: ReviewWorkBudgets | None = None,
        work_tracker: ResourceBudgetTracker | None = None,
        feedback: FeedbackSelection | None = None,
    ) -> PreparedHumanAssessment | None:
        eligibility = self.finalizer.load_eligibility(
            token=token,
            repository=repository,
            pull_request=pull_request,
            head_sha=prepared.head_sha,
            app_slug=app_slug,
            require_valid=True,
        )
        if eligibility is None:
            return None
        if eligibility.human_review is None:
            if eligibility.facts.has_human_adjudication_findings:
                # Public prose/short RS identifiers do not bind a complete
                # inventory to the result digest. Never reconstruct authority
                # from a chat reply, or expose this as ordinary conversation.
                raise GitHubConversationError(
                    "human-review inventory is missing; rerun a full review for the current head before reassessment"
                )
            return None
        preflight = self.finalizer._preflight(
            token=token,
            repository=repository,
            pull_request=pull_request,
            head_sha=prepared.head_sha,
            app_slug=app_slug,
        )
        if (
            preflight.result is not None
            or preflight.app_authored
            or preflight.base_sha != eligibility.human_review.base_sha
            or prepared.context.base_sha != preflight.base_sha
        ):
            raise GitHubConversationError(
                "human reassessment requires the current open PR and exact reviewed base/head; rerun a full review if the base changed"
            )
        if feedback is not None:
            if (
                self.before_read is None
                or not isinstance(feedback, FeedbackSelection)
                or feedback.repository != repository
                or feedback.pull_request != pull_request
                or feedback.base_sha != prepared.context.base_sha
                or feedback.head_sha != prepared.head_sha
                or feedback.trigger
                != FeedbackReference(
                    prepared.source_kind,
                    prepared.source_comment_id,
                    prepared.source_updated_at,
                )
            ):
                raise FeedbackAdmissionError("feedback_snapshot_invalid")
            self.revalidate_feedback(
                token=token,
                feedback=feedback,
                app_slug=app_slug,
                before_read=self.before_read,
            )
            original = next(
                item for item in feedback.sources if item.reference == feedback.trigger
            )
            source: dict[str, Any] | None = {
                "id": original.reference.comment_id,
                "body": original.body,
                "user": {
                    "id": original.author_id,
                    "login": original.author,
                    "type": "User",
                },
                "author_association": original.association,
            }
            if (
                original.reference.kind == "inline"
                and original.root_comment_id != original.reference.comment_id
            ):
                assert source is not None
                source["in_reply_to_id"] = original.root_comment_id
        else:
            source = self._source(
                token=token,
                repository=repository,
                pull_request=pull_request,
                prepared=prepared,
                app_slug=app_slug,
            )
        if source is None:
            raise GitHubConversationError(
                "human reassessment source changed or is unauthorized; submit a new authorized mention"
            )
        # Pending inventory paths are authoritative; historical inline hunks
        # and unrelated file order must not consume their evidence budget.
        if (
            work_budgets is not None and work_budgets.mode == "unified"
        ) or eligibility.human_review.requires_batched_reassessment:
            bundle = self.conversation.load_review_evidence(
                token=token,
                repository=repository,
                pull_request=pull_request,
                base_sha=eligibility.human_review.base_sha,
                head_sha=prepared.head_sha,
                required_paths=tuple(
                    sorted(
                        {
                            path
                            for item in eligibility.human_review.pending
                            for path in item.evidence_paths
                        }
                    )
                ),
                timeout_seconds=work_tracker.remaining_seconds()
                if work_tracker is not None
                else 120,
                include_all_changed=True,
            )
            return PreparedHumanAssessment(
                conversation=replace(
                    prepared, context=replace(prepared.context, diff_context=None)
                ),
                eligibility=eligibility,
                source_body=source["body"],
                source_actor=source["user"]["login"],
                source_metadata_digest=self._source_metadata_digest(source),
                evidence_bundle=bundle,
                feedback=feedback,
            )
        if any(item.required_paths for item in eligibility.human_review.pending):
            raise GitHubConversationError(
                "cross-file human review inventory requires the unified work mechanism"
            )
        selected = self.conversation.load_human_assessment_diff(
            token=token,
            repository=repository,
            pull_request=pull_request,
            pending_paths=tuple(item.path for item in eligibility.human_review.pending),
        )
        diff = selected.diff_context
        context = replace(prepared.context, diff_context=diff)
        return PreparedHumanAssessment(
            conversation=replace(prepared, context=context),
            eligibility=eligibility,
            source_body=source["body"],
            source_actor=source["user"]["login"],
            source_metadata_digest=self._source_metadata_digest(source),
            evidence_diagnostic=selected.diagnostic,
            feedback=feedback,
        )

    def publish(
        self,
        *,
        token: str,
        review_token: str | None,
        repository: str,
        pull_request: int,
        prepared: PreparedHumanAssessment,
        reply: HumanAssessmentReply | HumanAssessmentWork,
        app_slug: str,
    ) -> ReplyResult:
        conversation = prepared.conversation
        if prepared.feedback is not None and self.before_read is None:
            raise FeedbackAdmissionError("feedback_read_budget_exhausted")
        original = prepared.eligibility
        inventory = original.human_review
        work = reply if isinstance(reply, HumanAssessmentWork) else None
        if work is not None:
            if inventory is None or prepared.evidence_bundle is None:
                raise GitHubConversationError(
                    "human assessment aggregate evidence is missing"
                )
            if (
                prepared.evidence_bundle.snapshot.repository != repository
                or prepared.evidence_bundle.snapshot.pull_request != pull_request
            ):
                raise GitHubConversationError(
                    "human assessment aggregate repository is invalid"
                )
            try:
                validate_work(
                    work,
                    pending=inventory,
                    bundle=prepared.evidence_bundle,
                    source_body=prepared.source_body,
                    authority_digest=prepared.authority_digest,
                    **(
                        {"feedback": prepared.feedback}
                        if prepared.feedback is not None
                        else {}
                    ),
                )
            except ReviewInputError as exc:
                raise GitHubConversationError(
                    "human assessment aggregate evidence is unsupported"
                ) from exc
            reply = work.reply
        if inventory is None or not isinstance(reply, HumanAssessmentReply):
            raise GitHubConversationError("human assessment evidence is missing")
        if prepared.feedback is not None and prepared.feedback.target_ids:
            targets = set(prepared.feedback.target_ids)
            if not targets.issubset(
                {item.fingerprint for item in inventory.pending}
            ) or not {item.fingerprint for item in reply.decisions}.issubset(targets):
                raise FeedbackAdmissionError("feedback_targets_invalid")
        if prepared.evidence_diagnostic and any(
            item.decision != "unresolved" for item in reply.decisions
        ):
            raise GitHubConversationError("human assessment evidence is insufficient")
        try:
            updated_inventory = (
                work.inventory if work is not None else inventory
            ).apply(reply.decisions)
        except ReviewInputError as exc:
            raise GitHubConversationError(
                "human assessment identities are invalid"
            ) from exc
        if not self.source_fence(
            token=token,
            repository=repository,
            pull_request=pull_request,
            prepared=prepared,
            app_slug=app_slug,
        ):
            return ReplyResult(status="skipped_edited_source")
        if "<!-- reviewsensei:" in reply.body:
            raise GitHubConversationError(
                "human assessment reply contains a reserved marker"
            )
        findings = {item.fingerprint: item for item in inventory.pending}
        try:
            for decision in reply.decisions if work is None else ():
                validate_assessment_evidence(
                    decision,
                    findings[decision.fingerprint],
                    source_body=prepared.source_body,
                    diff_context=conversation.context.diff_context or "",
                    **(
                        {"feedback": prepared.feedback}
                        if prepared.feedback is not None
                        else {}
                    ),
                )
        except ReviewInputError as exc:
            raise GitHubConversationError(
                "human assessment evidence is unsupported"
            ) from exc
        if (
            updated_inventory != inventory or not inventory.pending
        ) and not review_token:
            raise GitHubConversationError(
                "human assessment review capability is missing"
            )
        current = self.finalizer.load_eligibility(
            token=token,
            repository=repository,
            pull_request=pull_request,
            head_sha=conversation.head_sha,
            app_slug=app_slug,
        )
        refreshed = replace(
            original,
            human_review=updated_inventory,
            facts=replace(
                original.facts,
                has_human_adjudication_findings=bool(updated_inventory.pending),
            ),
        )
        if current not in (original, refreshed):
            return ReplyResult(status="skipped_stale_head")
        preflight = self.finalizer._preflight(
            token=token,
            repository=repository,
            pull_request=pull_request,
            head_sha=conversation.head_sha,
            app_slug=app_slug,
        )
        if preflight.result is not None:
            return ReplyResult(status=preflight.result.status)
        if preflight.app_authored or preflight.base_sha != inventory.base_sha:
            return ReplyResult(status="skipped_stale_base")
        resuming = current == refreshed and (
            updated_inventory != inventory or not inventory.pending
        )
        outcome = self.conversation.publish(
            token=token,
            repository=repository,
            pull_request=pull_request,
            source_comment_id=conversation.source_comment_id,
            source_updated_at=conversation.source_updated_at,
            head_sha=conversation.head_sha,
            reply=ConversationReply(body=reply.body, resolve=False),
            app_slug=app_slug,
            root_comment_id=conversation.root_comment_id,
            source_kind=conversation.source_kind,
        )
        if prepared.evidence_diagnostic:
            outcome = replace(
                outcome,
                assessment_status="insufficient_evidence",
                assessment_diagnostic=prepared.evidence_diagnostic,
            )
        if outcome.status not in {"replied", "already_replied"} or (
            updated_inventory == inventory and inventory.pending
        ):
            return outcome
        assert review_token is not None
        # The App reply is not an authority record. Recheck human authorization,
        # exact base/head and latest result immediately before recording the
        # refreshed eligibility. Partial decisions keep the other concerns open.
        if not self.source_fence(
            token=token,
            repository=repository,
            pull_request=pull_request,
            prepared=prepared,
            app_slug=app_slug,
        ):
            return ReplyResult(
                status="skipped_edited_source", comment_id=outcome.comment_id
            )
        preflight = self.finalizer._preflight(
            token=review_token,
            repository=repository,
            pull_request=pull_request,
            head_sha=conversation.head_sha,
            app_slug=app_slug,
        )
        if preflight.result is not None:
            return ReplyResult(
                status=preflight.result.status, comment_id=outcome.comment_id
            )
        if preflight.app_authored or preflight.base_sha != inventory.base_sha:
            return ReplyResult(
                status="skipped_stale_base", comment_id=outcome.comment_id
            )
        current = self.finalizer.load_eligibility(
            token=review_token,
            repository=repository,
            pull_request=pull_request,
            head_sha=conversation.head_sha,
            app_slug=app_slug,
        )
        if current != (refreshed if resuming else original):
            return ReplyResult(
                status="skipped_stale_head", comment_id=outcome.comment_id
            )
        if resuming:
            # A previous attempt durably reassessed but failed before approval.
            # The conversation publisher has now reconciled this source's
            # reply (or acknowledged a new mention). Re-enter the exact-head
            # finalizer without another assessment record or model decision.
            finalized = self.finalizer.finalize(
                token=review_token,
                repository=repository,
                pull_request=pull_request,
                head_sha=conversation.head_sha,
                app_slug=app_slug,
                eligibility=refreshed,
                require_persisted=True,
            )
            return ReplyResult(
                status=finalized.status
                if finalized.status.startswith("skipped_")
                else outcome.status,
                comment_id=outcome.comment_id,
                approval_status=finalized.status,
                approval_diagnostic=finalized.diagnostic,
            )
        evidence_digest = hashlib.sha256(
            json.dumps(
                {
                    "decisions": [item.to_dict() for item in reply.decisions],
                    "source_body_sha256": hashlib.sha256(
                        prepared.source_body.encode()
                    ).hexdigest(),
                    "source_actor": prepared.source_actor,
                    "source_metadata_digest": prepared.source_metadata_digest,
                    **(
                        {
                            "feedback_digest": prepared.feedback.digest,
                            "feedback_event_key": prepared.feedback.event_key,
                        }
                        if prepared.feedback is not None
                        else {}
                    ),
                    "diff_sha256": prepared.evidence_bundle.digest
                    if work is not None and prepared.evidence_bundle is not None
                    else hashlib.sha256(
                        (conversation.context.diff_context or "").encode()
                    ).hexdigest(),
                    "plan_id": work.execution.plan.plan_id
                    if work is not None
                    else None,
                    "batch_evidence": [
                        {
                            "batch_id": batch.batch.batch_id,
                            "evidence_ids": batch.batch.evidence_ids,
                            "requirement_ids": batch.batch.requirement_ids,
                        }
                        for batch in work.execution.completed
                    ]
                    if work is not None
                    else [],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        marker = (
            f"<!-- reviewsensei:human-review:v1 source={conversation.source_comment_id} kind={conversation.source_kind} "
            f"evidence={evidence_digest} "
            f"updated={hashlib.sha256(conversation.source_updated_at.encode()).hexdigest()} "
            f"head={conversation.head_sha} base={inventory.base_sha} result={original.result_digest} -->"
        )
        body = (
            f"Reassessed human-review findings for `{conversation.head_sha}`. "
            f"{len(updated_inventory.resolved)} addressed or safely dismissed findings; "
            f"{len(updated_inventory.pending)} still require human assessment. "
            "All other review, coverage, qualification and approval requirements are preserved.\n\n"
            f"{marker}\n\n{approval_eligibility_marker(refreshed)}"
        )
        status, payload = self.http.request(
            "POST",
            self.http.repository_path(repository, f"/pulls/{pull_request}/reviews"),
            token=review_token,
            body={"commit_id": conversation.head_sha, "event": "COMMENT", "body": body},
        )
        if (
            status not in {200, 201}
            or not isinstance(payload, dict)
            or type(payload.get("id")) is not int
        ):
            raise GitHubPublicationError(
                "human assessment publication was not confirmed"
            )
        # Re-read durable eligibility inside finalization, including after the
        # thread scan, so a newer same-head human finding wins over this result.
        finalized = self.finalizer.finalize(
            token=review_token,
            repository=repository,
            pull_request=pull_request,
            head_sha=conversation.head_sha,
            app_slug=app_slug,
            eligibility=refreshed,
            require_persisted=True,
        )
        return replace(
            outcome,
            approval_status=finalized.status,
            approval_diagnostic=finalized.diagnostic,
        )
