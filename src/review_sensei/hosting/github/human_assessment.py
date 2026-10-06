"""Refresh published eligibility after an authorized, evidence-backed human reply."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any

from ...errors import ReviewInputError
from ...human_assessment import (
    MAX_HUMAN_SOURCE_BYTES,
    HumanAssessmentReply,
    validate_assessment_evidence,
)
from ...models import ConversationReply
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


@dataclass(frozen=True)
class PreparedHumanAssessment:
    conversation: PreparedConversation
    eligibility: ReviewApprovalEligibility
    source_body: str
    source_actor: str


class HumanAssessmentPublisher:
    """Keep reply authority, inference evidence and review authority distinct."""

    def __init__(self, *, http: GitHubHttp) -> None:
        self.http = http
        self.conversation = ConversationPublisher(http=http)
        self.finalizer = ReviewApprovalFinalizer(http=http)

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
        if (
            not isinstance(body, str)
            or len(body.encode("utf-8")) > MAX_HUMAN_SOURCE_BYTES
        ):
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
        # Inline comment hunks can belong to a historical commit. For human
        # reassessment always read the current PR diff rather than that hunk.
        diff, _paths = self.conversation._load_diff_context(
            token=token,
            repository=repository,
            pull_request=pull_request,
            source=source,
            source_kind="issue",
        )
        if not diff and eligibility.human_review.pending:
            raise GitHubConversationError(
                "human reassessment current diff is missing; rerun a full review before reassessment"
            )
        context = replace(prepared.context, diff_context=diff)
        return PreparedHumanAssessment(
            conversation=replace(prepared, context=context),
            eligibility=eligibility,
            source_body=source["body"],
            source_actor=source["user"]["login"],
        )

    def publish(
        self,
        *,
        token: str,
        review_token: str | None,
        repository: str,
        pull_request: int,
        prepared: PreparedHumanAssessment,
        reply: HumanAssessmentReply,
        app_slug: str,
    ) -> ReplyResult:
        conversation = prepared.conversation
        original = prepared.eligibility
        inventory = original.human_review
        if inventory is None or not isinstance(reply, HumanAssessmentReply):
            raise GitHubConversationError("human assessment evidence is missing")
        try:
            updated_inventory = inventory.apply(reply.decisions)
        except ReviewInputError as exc:
            raise GitHubConversationError(
                "human assessment identities are invalid"
            ) from exc
        source = self._source(
            token=token,
            repository=repository,
            pull_request=pull_request,
            prepared=conversation,
            app_slug=app_slug,
        )
        if (
            source is None
            or source["body"] != prepared.source_body
            or source["user"]["login"] != prepared.source_actor
        ):
            return ReplyResult(status="skipped_edited_source")
        if "<!-- reviewsensei:" in reply.body:
            raise GitHubConversationError(
                "human assessment reply contains a reserved marker"
            )
        findings = {item.fingerprint: item for item in inventory.pending}
        try:
            for decision in reply.decisions:
                validate_assessment_evidence(
                    decision,
                    findings[decision.fingerprint],
                    source_body=prepared.source_body,
                    diff_context=conversation.context.diff_context or "",
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
        if current == refreshed and (
            updated_inventory != inventory or not inventory.pending
        ):
            # A previous attempt durably reassessed but failed before approval.
            # Re-enter the existing exact-head finalizer without another model
            # decision or COMMENT. A newer/conflicting result still wins.
            assert review_token is not None
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
                else "already_replied"
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
        if (
            outcome.status not in {"replied", "already_replied"}
            or updated_inventory == inventory
        ):
            return outcome
        assert review_token is not None
        # The App reply is not an authority record. Recheck human authorization,
        # exact base/head and latest result immediately before recording the
        # refreshed eligibility. Partial decisions keep the other concerns open.
        source = self._source(
            token=token,
            repository=repository,
            pull_request=pull_request,
            prepared=conversation,
            app_slug=app_slug,
        )
        if (
            source is None
            or source["body"] != prepared.source_body
            or source["user"]["login"] != prepared.source_actor
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
        if current != original:
            return ReplyResult(
                status="skipped_stale_head", comment_id=outcome.comment_id
            )
        evidence_digest = hashlib.sha256(
            json.dumps(
                {
                    "decisions": [item.to_dict() for item in reply.decisions],
                    "source_body_sha256": hashlib.sha256(
                        prepared.source_body.encode()
                    ).hexdigest(),
                    "source_actor": prepared.source_actor,
                    "diff_sha256": hashlib.sha256(
                        (conversation.context.diff_context or "").encode()
                    ).hexdigest(),
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
        self.finalizer.finalize(
            token=review_token,
            repository=repository,
            pull_request=pull_request,
            head_sha=conversation.head_sha,
            app_slug=app_slug,
            eligibility=refreshed,
            require_persisted=True,
        )
        return outcome
