"""Offline proposal entry. Not wired into caller, broker, reply or commands."""

import re

from review_sensei.comment_intent import (
    ActionProposal,
    CommentObservation,
    PreparedComment,
    ResolvedActionFacts,
    current_source_refusal,
    plan_comment_action,
    prepare_intent_request,
    proposal,
)
from review_sensei.decision_provider import DecisionResponse
from review_sensei.disposition import parse_maintainer_command

from .trigger import (
    choose_head_sha,
    extract_requested_commit,
    issue_comment_requests_remaining,
    issue_comment_requests_rescan,
)


def prepare_comment_proposal(
    observation: CommentObservation, facts: ResolvedActionFacts
) -> PreparedComment:
    refusal = current_source_refusal(observation, facts)
    if refusal:
        return PreparedComment(proposal=refusal)
    body = observation.body
    if observation.surface == "issue_comment":
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
                return PreparedComment(
                    proposal=proposal(
                        observation, facts, "refused", "explicit-request-head-conflicts"
                    )
                )
        if action:
            return PreparedComment(
                proposal=proposal(
                    observation,
                    facts,
                    "explicit",
                    "unchanged-explicit-route",
                    action,
                    requirements=("existing-literal-source-authorization",),
                )
            )
    # A malformed sharp command cannot acquire a permissive semantic fallback.
    if re.match(
        r"^\s*@(?:reviewsensei|sensei)\s+(?:"
        r"(?:override|accept-risk|dismiss|defer|approve-media|accept-coverage-uncertainty|reenroll|quarantine|verify|rescan)\b"
        r"|review\s+(?:remaining|continue|pause|status|reenroll|accept-coverage-uncertainty)\b"
        r"|i\s+reviewed\s+the\s+media\b"
        r"|(?:rs-)?[a-f0-9]{6,64}\b[^\r\n]*\bacceptable\s+risk\b)",
        body,
        re.I,
    ):
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
        return prepared.proposal
    assert prepared.request is not None
    return plan_comment_action(observation, facts, prepared.request, response)
