"""Explicit pending-only resolution roots; no ordinary/REST writer activation."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Callable, Protocol

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.presentation import assign_finding_identifiers
from review_sensei.reassessment_work import HumanAssessmentWork, validate_work

from .conversation import ReplyResult
from .eligibility_parts import (
    BoundPartitionedEligibilityReader,
    PartitionedEligibilityContext,
    _preflight,
    _visible_from_document,
)
from .errors import GitHubConversationError
from .prose_publication import MAX_RETAINED_PROSE_REVIEWS
from .publication import ReviewApprovalFinalizer

if TYPE_CHECKING:
    pass


@dataclass(frozen=True)
class PartitionedAssessmentPlan:
    """Host-owned original source/accepted queue observation; never a grant."""

    context: PartitionedEligibilityContext
    activation_generation: int
    source_digest: str
    authority_digest: str
    activation_dispatches: int
    replay_dispatches: int

    def __post_init__(self):
        if (
            type(self.context) is not PartitionedEligibilityContext
            or any(
                type(value) is not int or not 1 <= value <= 60
                for value in (self.activation_dispatches, self.replay_dispatches)
            )
            or type(self.activation_generation) is not int
            or not self.context.activation_generation
            < self.activation_generation
            <= 2_147_483_647
            or any(
                not isinstance(item, str)
                or len(item) != 64
                or any(char not in "0123456789abcdef" for char in item)
                for item in (self.source_digest, self.authority_digest)
            )
        ):
            raise ReviewInputError("partition assessment host plan is invalid")


@dataclass(frozen=True)
class PartitionedAssessmentActivation:
    plan: PartitionedAssessmentPlan
    body: str
    document_bytes: bytes
    assessment_bytes: bytes

    @property
    def assessments(self):
        return tuple(json.loads(self.assessment_bytes))


class PartitionedAssessmentHost(Protocol):
    plan: PartitionedAssessmentPlan
    accepted_context: PartitionedEligibilityContext | None

    def activate(
        self,
        request: PartitionedAssessmentActivation,
        *,
        verify: Callable[[PartitionedEligibilityContext], None],
    ) -> PartitionedEligibilityContext:
        """Serialize original accepted-operation/root activation and terminal ack.

        Hold before remote mutation. Bind exact accepted decision bytes to the
        owned source/queue and current generation. Authenticate the actual root
        readback, call publisher verification before terminal commit, then ack.
        Unknown writes/readbacks never authorize retry. Exact owned replay may
        read/verify but cannot call inference or issue another root. Retention,
        pause, original deadline/counters, and fresh-process witnesses remain
        host obligations; a plan/caller snapshot is never such a witness.
        """
        ...


def publish_partitioned_assessment(
    publisher, *, token, repository, pull_request, prepared, work, host, app_slug
):
    reader = publisher.finalizer.partitioned_reader
    plan = getattr(host, "plan", None)
    original = prepared.eligibility
    inventory = original.human_review
    if (
        not callable(getattr(host, "activate", None))
        or type(reader) is not BoundPartitionedEligibilityReader
        or type(plan) is not PartitionedAssessmentPlan
        or type(work) is not HumanAssessmentWork
        or inventory is None
        or original.partitioned_root is None
        or prepared.feedback is None
        or not prepared.feedback.target_ids
        or prepared.evidence_bundle is None
        or prepared.evidence_diagnostic
        or prepared.feedback.repository != repository
        or prepared.feedback.pull_request != pull_request
        or plan.source_digest != prepared.feedback.event_key
        or plan.authority_digest != prepared.authority_digest
        or plan.context != original.partitioned_root.context
        or prepared.feedback.base_sha != plan.context.binding["base_sha"]
        or prepared.feedback.head_sha != plan.context.binding["head_sha"]
        or dict(plan.context.binding) != dict(original.partitioned_root.context.binding)
        or plan.context.result_digest != original.result_digest
        or plan.context.visible_instances
        != original.partitioned_root.context.visible_instances
        or plan.context.budget is not reader.context.budget
        or plan.context.budget is not original.partitioned_root.context.budget
        or publisher.before_read != plan.context.budget.consume
    ):
        raise GitHubConversationError(
            "partition assessment original authority is invalid"
        )
    try:
        validate_work(
            work,
            pending=inventory,
            bundle=prepared.evidence_bundle,
            source_body=prepared.source_body,
            authority_digest=prepared.authority_digest,
            feedback=prepared.feedback,
        )
    except ReviewInputError as exc:
        raise GitHubConversationError(
            "partition assessment aggregate evidence is unsupported"
        ) from exc
    decisions = work.reply.decisions
    if (
        len({item.fingerprint for item in decisions}) != len(decisions)
        or not set(prepared.feedback.target_ids).issubset(
            {item.fingerprint for item in inventory.pending}
        )
        or not {item.fingerprint for item in decisions}.issubset(
            prepared.feedback.target_ids
        )
        or "<!-- reviewsensei:" in work.reply.body
    ):
        raise GitHubConversationError("partition assessment decisions are invalid")
    updated = work.inventory.apply(decisions)
    if not updated.pending or set(updated.resolved) <= set(inventory.resolved):
        raise GitHubConversationError(
            "partition assessment requires a partial resolution with pending findings"
        )
    refreshed = replace(
        original,
        human_review=updated,
        facts=replace(original.facts, has_human_adjudication_findings=True),
    )
    document = dict(
        refreshed.to_dict(), activation_generation=plan.activation_generation
    )
    _preflight(document, context=plan.context, inventory=updated)
    retained = reader.read(
        document["visible_prose"],
        expected_binding=dict(plan.context.binding, purpose="evidence"),
        budget=plan.context.budget,
    )
    receipts = _visible_from_document(retained, context=plan.context, inventory=updated)
    navigation = ", ".join(
        f"[part {item.index + 1}](https://github.com/{repository}/pull/{pull_request}#pullrequestreview-{item.review_id})"
        for item in receipts
    )
    aliases = assign_finding_identifiers(
        item.fingerprint for item in inventory.findings
    )
    resolved = ", ".join(
        f"`{aliases[item]}` (`{item}`)" for item in sorted(updated.resolved)
    )
    rationale = "\n".join(
        f"- `{item.fingerprint}`: {item.rationale}" for item in decisions
    )
    prefix = (
        f"{work.reply.body}\n\nCurrent pending findings: **{len(updated.pending)}**. Resolved finding IDs: {resolved}.\n\n"
        f"Every other visible finding ID in these historical reports remains pending: {navigation}. "
        "The linked reports retain every original concern and required path; their earlier pending labels are historical. Approval remains withheld.\n\n"
        f"{rationale}\n\n"
    )
    if "<!-- reviewsensei:" in prefix:
        raise GitHubConversationError("partition assessment contains a reserved marker")
    if len(prefix.encode()) > plan.context.framing_bytes:
        raise GitHubConversationError(
            "partition assessment visible framing exceeds allocation"
        )
    body = (
        prefix
        + "<!-- reviewsensei:eligibility:v1 "
        + base64.urlsafe_b64encode(canonical_bytes(document)).decode()
        + " -->"
    )
    if (
        len(body.encode()) + plan.context.future_lifecycle_bytes
        > plan.context.max_root_bytes
    ):
        raise GitHubConversationError(
            "partition assessment complete root exceeds allocation"
        )
    request = PartitionedAssessmentActivation(
        plan,
        body,
        canonical_bytes(document),
        canonical_bytes([item.to_dict() for item in decisions]),
    )
    accepted = host.accepted_context
    if accepted is not None and type(accepted) is not PartitionedEligibilityContext:
        raise GitHubConversationError("partition assessment replay context is invalid")

    def live(finalizer: ReviewApprovalFinalizer) -> None:
        if not publisher.source_fence(
            token=token,
            repository=repository,
            pull_request=pull_request,
            prepared=prepared,
            app_slug=app_slug,
        ):
            raise GitHubConversationError("partition assessment source changed")
        current = finalizer._preflight(
            token=token,
            repository=repository,
            pull_request=pull_request,
            head_sha=original.head_sha,
            app_slug=app_slug,
        )
        if (
            current.result is not None
            or current.app_authored
            or current.base_sha != inventory.base_sha
        ):
            raise GitHubConversationError("partition assessment current PR changed")

    if accepted is None:
        live(publisher.finalizer)
        current = publisher.finalizer.load_eligibility(
            token=token,
            repository=repository,
            pull_request=pull_request,
            head_sha=original.head_sha,
            app_slug=app_slug,
            require_valid=True,
        )
        if current != original:
            raise GitHubConversationError(
                "partition assessment latest authority changed"
            )
    scan = publisher.finalizer._last_review_scan
    if scan is None:
        raise GitHubConversationError(
            "partition assessment complete scan observation is missing"
        )
    calls, items, page_size = scan
    if accepted is None and items >= MAX_RETAINED_PROSE_REVIEWS:
        raise GitHubConversationError(
            "partition assessment retained review capacity is exhausted"
        )
    # The next root may require an additional EOF/page request. This planning
    # observation is not authority: verification still obtains every page fresh,
    # and unexpected concurrent growth remains a held/refused operation.
    extra_page = int(accepted is None and items % page_size == page_size - 1)
    plan.context.budget.preflight(
        (plan.activation_dispatches if accepted is None else plan.replay_dispatches)
        + calls
        + extra_page
        + len(prepared.feedback.sources)
        + 1
    )
    verified: list[tuple[PartitionedEligibilityContext, ReviewApprovalFinalizer]] = []

    def verify(context: PartitionedEligibilityContext) -> None:
        if (
            type(context) is not PartitionedEligibilityContext
            or context.activation_generation != plan.activation_generation
            or context.owned_root_id == plan.context.owned_root_id
        ):
            raise GitHubConversationError(
                "partition assessment activated context changed"
            )
        bound = reader._with_owned_context(context, expected_root_body=body)
        finalizer = ReviewApprovalFinalizer(
            http=publisher.http,
            evidence_budget=context.budget,
            qualification_authority=publisher.finalizer.qualification_authority,
            partitioned_reader=bound,
        )
        loaded = finalizer.load_eligibility(
            token=token,
            repository=repository,
            pull_request=pull_request,
            head_sha=original.head_sha,
            app_slug=app_slug,
            require_valid=True,
            expected_root_body=body,
        )
        if loaded is None or loaded.to_dict() != document:
            raise GitHubConversationError("partition assessment root readback changed")
        live(finalizer)
        verified.append((context, finalizer))

    context = host.activate(request, verify=verify)
    if (
        len(verified) != 1
        or context != verified[0][0]
        or context.budget is not plan.context.budget
    ):
        raise GitHubConversationError(
            "partition assessment terminal readback is missing"
        )
    publisher.finalizer = verified[0][1]
    plan.context.budget.check()
    return ReplyResult(
        status="already_replied" if accepted is not None else "replied",
        comment_id=context.owned_root_id,
        approval_status="approval_withheld",
    )
