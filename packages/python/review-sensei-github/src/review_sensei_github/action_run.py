"""One authenticated process for an Actions command or mention reply.

Policy checkout, dependency installation and artifact upload are infrastructure.
Every business request made here uses the selected caller-run transport.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.convergence import resolve_review_convergence_policy
from review_sensei.disposition import (
    FindingIdentity,
    command_exit_code,
    parse_maintainer_command,
)
from review_sensei.errors import ReviewInputError

from .application import GitHubApplication, GitHubWriteOptions
from .conversation import ConversationPublisher
from .learning_pr import LearningPRPublisher
from .publication import ReviewPublisher, parse_finding_index
from .review_run import ActionRunLedger, ReviewRun, bootstrap
from .run_authority import VerifiedRunAction


def event_source(args: Any, *, purpose: str) -> dict[str, object]:
    """The runner event selects a source; live Worker reads authenticate it."""
    path = Path(os.environ.get("GITHUB_EVENT_PATH", ""))
    with path.open("rb") as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ReviewInputError("action event exceeds its byte bound")
    value = json.loads(raw)
    comment = value.get("comment") if isinstance(value, dict) else None
    pull = (
        value.get("issue") or value.get("pull_request")
        if isinstance(value, dict)
        else None
    )
    if (
        not isinstance(comment, dict)
        or not isinstance(pull, dict)
        or pull.get("number") != args.pull_request
        or comment.get("id") != args.source_comment_id
        or not isinstance(comment.get("body"), str)
        or not isinstance(comment.get("updated_at"), str)
    ):
        raise ReviewInputError("action source does not match the original event")
    if purpose == "command" and comment["body"] != args.comment_body:
        raise ReviewInputError("command body differs from the original source")
    if purpose == "reply" and comment["updated_at"] != args.source_updated_at:
        raise ReviewInputError("reply source version differs from the original event")
    return {
        "kind": "review_comment"
        if os.environ.get("GITHUB_EVENT_NAME") == "pull_request_review_comment"
        else "issue_comment",
        "id": comment["id"],
        "updated_at": comment["updated_at"],
        "body_sha256": hashlib.sha256(comment["body"].encode()).hexdigest(),
    }


def application(run: ReviewRun) -> GitHubApplication:
    app = GitHubApplication(
        broker=run.broker,
        http=run.http,
        reviewer=ReviewPublisher(http=run.http),
        replier=ConversationPublisher(http=run.http),
        learner=LearningPRPublisher(http=run.http),
        session_ledger=run.ledger,
        enable_disposition_snapshot_writes=True,
    )
    app.reviewer.finalizer.evidence_budget = run.caller.budget_view
    app.replier.finalizer.evidence_budget = run.caller.budget_view
    return app


def current_inventory(run: ReviewRun) -> tuple[FindingIdentity, ...]:
    """Read the newest current App review's current index; no old fallback."""
    assert run.identity.repository_id is not None
    reviews = run.http.paginate(
        path=run.http.repository_path(
            run.identity.repository, f"/pulls/{run.identity.pull_request}/reviews"
        ),
        token=run.ledger.token,
    )
    owned = [
        r
        for r in reviews
        if isinstance(r, dict)
        and isinstance(r.get("user"), dict)
        and r["user"].get("type") == "Bot"
        and r["user"].get("id") == run.ledger._evidence_author_id
        and r.get("commit_id") == run.head_sha
        and type(r.get("id")) is int
    ]
    if not owned:
        raise ReviewInputError("current command review inventory is unavailable")
    selected = max(owned, key=lambda r: r["id"])
    index = parse_finding_index(
        selected.get("body"),
        repository_id=run.identity.repository_id,
        pull_request=run.identity.pull_request,
        head_sha=run.head_sha,
    )
    if index is None:
        raise ReviewInputError(
            "current command requires a complete current review index"
        )
    return tuple(
        FindingIdentity(
            instance=i.instance,
            concern=i.lifecycle,
            blocking=i.blocking,
            needs_human=i.needs_human,
            overridable=i.overridable,
        )
        for i in index.entries
    )


def _options(run: ReviewRun) -> GitHubWriteOptions:
    return GitHubWriteOptions(
        github_writes=True,
        github_session_ledger=True,
        reviews_policy=run.plan.policy.reviews,
    )


def execute_command(run: ReviewRun, args: Any) -> int:
    assert run.identity.repository_id is not None
    authority = VerifiedRunAction(run)
    if not isinstance(run.ledger, ActionRunLedger):
        raise ReviewInputError("command requires its current action ledger")
    run.ledger.business_guard = authority.fence
    source = authority.document["source"]
    actor = authority.document["actor"]
    command = parse_maintainer_command(
        args.comment_body,
        actor=actor["login"],
        head_sha=run.head_sha,
        command_id=str(source["id"]),
    )
    if command is None:
        raise ReviewInputError("authenticated source is not a supported command")
    key = hashlib.sha256(
        canonical_bytes(
            {
                "purpose": "command",
                "repository_id": run.identity.repository_id,
                "pull_request": run.identity.pull_request,
                "head_sha": run.head_sha,
                "source": source,
                "configuration": run.caller.accounting.configuration.digest,
            }
        )
    ).hexdigest()
    caller = run.caller
    intent = caller.admit_action(
        run.ledger,
        run.identity,
        purpose="command",
        source=source,
        head_sha=run.head_sha,
        operation_key=key,
    )
    inventory = (
        current_inventory(run)
        if command.finding_reference or command.finding_references
        else None
    )
    app = application(run)
    from .commands import lookup_collaborator_permission

    permission = None
    if command.action in {
        "accept-risk",
        "accept-coverage-uncertainty",
        "continue",
        "reenroll",
        "dismiss",
        "defer",
        "approve-media",
    }:
        permission = lookup_collaborator_permission(
            run.http,
            token=run.ledger.token,
            repository=run.identity.repository,
            actor=actor["login"],
        )
    authority.fence()
    boundary = caller.begin_action_effect(run.ledger, run.identity, intent)
    result = app.apply_maintainer_command(
        options=_options(run),
        oidc_token=run.oidc,
        repository=run.identity.repository,
        repository_id=run.identity.repository_id,
        pull_request=run.identity.pull_request,
        head_sha=run.head_sha,
        body=args.comment_body,
        actor_login=actor["login"],
        actor_type=actor["type"],
        association=actor["association"],
        app_slug=args.app_slug,
        source_comment_id=args.source_comment_id,
        current_action=authority,
        finding_inventory=inventory,
        actor_permission=permission,
        command_budget=caller.budget_view,
        media_budget=caller.budget_view,
        media_read_token=run.ledger.token,
        operation_entry="enabled",
        app_id=run.ledger.authenticated_app_id,
        run_id=str(caller.run.run_id),
        run_attempt=str(caller.attempt),
        reviews_policy=run.plan.policy.reviews,
        convergence_policy=resolve_review_convergence_policy(),
    )
    if result.evaluation_failed:
        raise ReviewInputError(
            "command effect or required finalization remains unconfirmed: "
            + result.summary
        )
    # Receipt publication is part of this same action and must be confirmed.
    if result.receipts:
        from .action_receipt import publish_command_receipt

        publish_command_receipt(run, authority, args.comment_body, result)
    outcome = (
        {"kind": "applied", "reason": None}
        if result.applied
        else {
            "kind": "explicit_noop",
            "reason": "status" if command.action == "status" else "no_change",
        }
    )
    proof = caller.settle_action_effect(
        run.ledger,
        run.identity,
        boundary,
        outcome=outcome,
        result_document=asdict(result),
    )
    caller.begin_terminal("action-completion")
    caller.close_action(run.ledger, run.identity, proof)
    print(result.summary)
    return command_exit_code(result)


def run_action(argv: Sequence[str], *, purpose: str) -> int:
    parser = argparse.ArgumentParser(prog="review-sensei-github " + purpose + "-run")
    parser.add_argument("--policy-commit", required=True)
    parser.add_argument("--run-directory", type=Path, required=True)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    options = parser.parse_args(list(argv))
    arguments = (
        options.arguments[1:] if options.arguments[:1] == ["--"] else options.arguments
    )
    from .commands import github_parser

    args = github_parser().parse_args([purpose, *arguments])
    if not args.allow_write:
        raise ReviewInputError("current Actions routes require --allow-write")
    if (
        getattr(args, "session_attestation", None) is not None
        or getattr(args, "oidc_token", None) is not None
    ):
        raise ReviewInputError(
            "current action authority is obtained inside its lifecycle"
        )
    if getattr(args, "session_ledger", None) is not None:
        raise ReviewInputError("current Actions routes require their native run ledger")
    source = event_source(args, purpose=purpose)
    args.provider = getattr(args, "provider", None)
    args.model = getattr(args, "model", None)
    args.base_sha = None
    if not args.head_sha:
        args.head_sha = None
    args.rescan = args.review_remaining = False
    run = bootstrap(
        args,
        run_directory=options.run_directory,
        policy_commit=options.policy_commit,
        purpose=purpose,
        source=source,
    )
    if purpose == "command":
        return execute_command(run, args)
    from .action_reply import execute_reply

    return execute_reply(run, args)
