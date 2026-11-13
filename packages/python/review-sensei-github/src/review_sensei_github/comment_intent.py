"""Proposal-only comment entries with G27 source-comment lifecycle revision guards."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from review_sensei.comment_intent import (
    ActionProposal,
    CommentObservation,
    PreparedComment,
    ResolvedActionFacts,
    _requires_explicit_command,
    current_source_refusal,
    plan_comment_action,
    prepare_intent_request,
    proposal,
)
from review_sensei.comment_intent_batch import (
    CommentBatchProposal,
    PreparedCommentBatch,
    ValidatedBatchExtraction,
    plan_comment_batch,
    prepare_intent_batch,
    terminal_batch,
)
from review_sensei.decision_provider import DecisionResponse
from review_sensei.disposition import parse_maintainer_command
from review_sensei.errors import ReviewInputError

from .source_comment_lifecycle import (
    SourceRevisionObservation,
    build_source_comment_descriptor,
    compare_source_comment_revisions,
)
from .trigger import (
    choose_head_sha,
    extract_requested_commit,
    issue_comment_requests_remaining,
    issue_comment_requests_rescan,
)


def source_descriptor_for_observation(
    observation: CommentObservation,
) -> dict[str, Any]:
    """Build a G27 ``(kind, id, updated_at, body_sha256)`` descriptor from an observation."""

    return build_source_comment_descriptor(
        kind=observation.surface,
        comment_id=observation.source_id,
        updated_at=observation.revision,
        body=observation.body,
    )


def verify_live_comment_source(
    *,
    bound_source: Mapping[str, Any],
    live_comment: Mapping[str, Any] | None = None,
    deleted: bool = False,
    phase: str = "unstarted_intent",
    event_delivery: Mapping[str, Any] | None = None,
) -> SourceRevisionObservation:
    """Verify via ``source_comment_lifecycle`` that a comment revision remains unmutated."""

    observed = compare_source_comment_revisions(
        bound_source,
        live_comment,
        deleted=deleted,
        phase=phase,
        event_delivery=event_delivery,
    )
    if observed.mutation_state != "unchanged" or observed.delivery_disposition not in {
        "current_revision",
        "duplicate_delivery",
    }:
        raise ReviewInputError(
            "source comment revision changed before execution "
            f"({observed.mutation_state}/{observed.delivery_disposition})"
        )
    return observed


def source_lifecycle_refusal(
    observation: CommentObservation,
    facts: ResolvedActionFacts,
    *,
    bound_source: Mapping[str, Any] | None = None,
    live_source: Mapping[str, Any] | None = None,
    source_deleted: bool = False,
    event_delivery: Mapping[str, Any] | None = None,
) -> ActionProposal | None:
    """Refuse mutated, restored-body, deleted, or stale comment deliveries before intent."""

    if (
        not source_deleted
        and bound_source is None
        and live_source is None
        and event_delivery is None
    ):
        return None
    anchor = (
        dict(bound_source)
        if bound_source is not None
        else source_descriptor_for_observation(observation)
    )
    candidate_live = (
        live_source
        if live_source is not None
        else (
            None if source_deleted else source_descriptor_for_observation(observation)
        )
    )
    compared = compare_source_comment_revisions(
        anchor,
        candidate_live,
        deleted=source_deleted,
        phase="before_admission",
        event_delivery=event_delivery,
    )
    if compared.mutation_state == "source_deleted":
        return proposal(observation, facts, "refused", "source-comment-deleted")
    if compared.mutation_state == "edit_restored_body":
        return proposal(observation, facts, "refused", "source-comment-edit-restored")
    if compared.mutation_state == "source_edited":
        return proposal(observation, facts, "refused", "source-comment-edited")
    if compared.delivery_disposition != "current_revision":
        return proposal(observation, facts, "refused", "source-comment-stale-delivery")
    return None


def _original_comment_proposal(
    observation: CommentObservation,
    facts: ResolvedActionFacts,
    *,
    bound_source: Mapping[str, Any] | None = None,
    live_source: Mapping[str, Any] | None = None,
    source_deleted: bool = False,
    event_delivery: Mapping[str, Any] | None = None,
) -> ActionProposal | None:
    lifecycle_veto = source_lifecycle_refusal(
        observation,
        facts,
        bound_source=bound_source,
        live_source=live_source,
        source_deleted=source_deleted,
        event_delivery=event_delivery,
    )
    if lifecycle_veto is not None:
        return lifecycle_veto
    refusal = current_source_refusal(observation, facts)
    if refusal:
        return refusal
    body = observation.body
    if observation.surface != "issue_comment":
        return None
    command = parse_maintainer_command(
        body, actor=observation.actor_login, head_sha=observation.scope.head_sha
    )
    action = command.action if command else None
    if issue_comment_requests_rescan(body):
        action = "rescan"
    elif issue_comment_requests_remaining(body):
        action = "remaining"
    if action in {"rescan", "remaining"}:
        try:
            choose_head_sha(
                {"head": {"sha": observation.scope.head_sha}},
                extract_requested_commit(body),
            )
        except ValueError:
            return proposal(
                observation, facts, "refused", "explicit-request-head-conflicts"
            )
    if action:
        return proposal(
            observation,
            facts,
            "explicit",
            "unchanged-explicit-route",
            action,
            requirements=("existing-literal-source-authorization",),
        )
    return None


def prepare_comment_proposal(
    observation: CommentObservation,
    facts: ResolvedActionFacts,
    *,
    bound_source: Mapping[str, Any] | None = None,
    live_source: Mapping[str, Any] | None = None,
    source_deleted: bool = False,
    event_delivery: Mapping[str, Any] | None = None,
) -> PreparedComment:
    original = _original_comment_proposal(
        observation,
        facts,
        bound_source=bound_source,
        live_source=live_source,
        source_deleted=source_deleted,
        event_delivery=event_delivery,
    )
    if original:
        return PreparedComment(proposal=original)
    if _requires_explicit_command(observation.body):
        return PreparedComment(
            proposal=proposal(
                observation, facts, "clarify", "explicit-command-required"
            )
        )
    return prepare_intent_request(observation, facts)


def complete_comment_proposal(
    observation: CommentObservation,
    facts: ResolvedActionFacts,
    response: DecisionResponse,
    *,
    bound_source: Mapping[str, Any] | None = None,
    live_source: Mapping[str, Any] | None = None,
    source_deleted: bool = False,
    event_delivery: Mapping[str, Any] | None = None,
) -> ActionProposal:
    prepared = prepare_comment_proposal(
        observation,
        facts,
        bound_source=bound_source,
        live_source=live_source,
        source_deleted=source_deleted,
        event_delivery=event_delivery,
    )
    if prepared.proposal:
        if prepared.proposal.status == "explicit":
            # Static commands finish during preparation. An extraction response
            # cannot be rebound to a different explicit source operation.
            return proposal(
                observation, facts, "refused", "completion-has-no-extraction-request"
            )
        return prepared.proposal
    assert prepared.request is not None
    return plan_comment_action(observation, facts, prepared.request, response)


def prepare_comment_batch(
    observation: CommentObservation,
    facts: ResolvedActionFacts,
    *,
    bound_source: Mapping[str, Any] | None = None,
    live_source: Mapping[str, Any] | None = None,
    source_deleted: bool = False,
    event_delivery: Mapping[str, Any] | None = None,
) -> PreparedCommentBatch:
    """Offline batch facade; explicit parsing sees the complete original only."""
    original = _original_comment_proposal(
        observation,
        facts,
        bound_source=bound_source,
        live_source=live_source,
        source_deleted=source_deleted,
        event_delivery=event_delivery,
    )
    if original:
        return PreparedCommentBatch(terminal=original)
    return prepare_intent_batch(observation, facts)


def complete_comment_batch(
    observation: CommentObservation,
    facts: ResolvedActionFacts,
    prepared: PreparedCommentBatch,
    extraction: ValidatedBatchExtraction | None = None,
    *,
    bound_source: Mapping[str, Any] | None = None,
    live_source: Mapping[str, Any] | None = None,
    source_deleted: bool = False,
    event_delivery: Mapping[str, Any] | None = None,
) -> CommentBatchProposal:
    """No existing command handler or broker accepts a proposal as admission."""
    lifecycle_veto = source_lifecycle_refusal(
        observation,
        facts,
        bound_source=bound_source,
        live_source=live_source,
        source_deleted=source_deleted,
        event_delivery=event_delivery,
    )
    if lifecycle_veto is not None:
        return terminal_batch(observation, facts, lifecycle_veto)
    current = prepare_comment_batch(observation, facts)
    if current != prepared:
        return terminal_batch(
            observation,
            facts,
            proposal(
                observation, facts, "refused", "comment-manifest-or-request-changed"
            ),
        )
    if current.terminal:
        return terminal_batch(observation, facts, current.terminal)
    return plan_comment_batch(observation, facts, prepared, extraction)


from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
