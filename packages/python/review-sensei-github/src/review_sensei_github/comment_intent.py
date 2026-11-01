"""Offline proposal entries. Not wired into caller, broker, replies or commands."""

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

from .trigger import (
    choose_head_sha,
    extract_requested_commit,
    issue_comment_requests_remaining,
    issue_comment_requests_rescan,
)


def _original_comment_proposal(
    observation: CommentObservation, facts: ResolvedActionFacts
) -> ActionProposal | None:
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
    observation: CommentObservation, facts: ResolvedActionFacts
) -> PreparedComment:
    original = _original_comment_proposal(observation, facts)
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
) -> ActionProposal:
    prepared = prepare_comment_proposal(observation, facts)
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
    observation: CommentObservation, facts: ResolvedActionFacts
) -> PreparedCommentBatch:
    """Offline batch facade; explicit parsing sees the complete original only."""
    original = _original_comment_proposal(observation, facts)
    if original:
        return PreparedCommentBatch(terminal=original)
    return prepare_intent_batch(observation, facts)


def complete_comment_batch(
    observation: CommentObservation,
    facts: ResolvedActionFacts,
    prepared: PreparedCommentBatch,
    extraction: ValidatedBatchExtraction | None = None,
) -> CommentBatchProposal:
    """No existing command handler or broker accepts a proposal as admission."""
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
