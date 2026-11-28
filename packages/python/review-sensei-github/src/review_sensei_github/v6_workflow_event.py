"""Bind the installed v6 callable to GitHub's event and live read-only PR evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeGuard
from urllib.request import HTTPRedirectHandler, Request, build_opener

from review_sensei.configuration import ProductConfiguration
from review_sensei.conversation import ConversationService
from review_sensei.disposition import parse_maintainer_command
from review_sensei.errors import ReviewInputError, ReviewSenseiError
from review_sensei.hosted import HostedPlan, plan_hosted_execution
from review_sensei.outcomes import ResourceBudget, ResourceBudgetTracker
from review_sensei.presentation import escape_markdown_text
from review_sensei.providers.registry import default_registry

from .conversation import (
    ConversationPublisher,
    PreparedConversation,
    authorized_human_comment,
    has_standalone_sensei_mention,
)
from .errors import GitHubConversationError, GitHubHTTPError
from .http import GitHubHttp
from .trigger import issue_comment_requests_remaining, resolve_issue_comment
from .v6_credential_matrix import GitHubEventKind
from .v6_fresh_analysis import (
    V6FreshAnalysisError,
    _load_trusted_configuration,
    filtered_hosted_environ,
    v6_provider_settings,
)
from .v6_recovery_ledger import canonical_sha256

COMMENT_EVENTS = {
    GitHubEventKind.ISSUE_COMMENT,
    GitHubEventKind.PULL_REQUEST_REVIEW_COMMENT,
}
_SHA = re.compile("[0-9a-f]{40}")


def refuse(reason: str) -> ReviewInputError:
    return ReviewInputError(reason, diagnostic=reason)


def _positive(value: Any) -> TypeGuard[int]:
    return type(value) is int and value > 0


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise refuse("v6_event_binding_invalid")
    return value


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def workflow_reader(api_url: str, repository: str) -> GitHubHttp:
    """Constrain all context preparation, including pagination, to same-repo GETs."""
    prefix = api_url.rstrip("/") + "/repos/" + repository + "/"
    opener = build_opener(_NoRedirect())

    def read(req: Request, *, timeout: float, **kwargs: Any):
        if req.get_method() != "GET" or not req.full_url.startswith(prefix):
            raise refuse("v6_untrusted_context_request")
        return opener.open(req, timeout=timeout)

    return GitHubHttp(api_url=api_url, opener=read)


@dataclass(frozen=True)
class WorkflowOperation:
    head_sha: str
    operation: str
    verify: Callable[[], None]
    source_id: int | None = None
    root_id: int | None = None
    source_digest: str | None = None
    conversation: Callable[[], PreparedConversation] | None = None


def resolve_workflow_event(
    *,
    event: GitHubEventKind,
    payload: Mapping[str, Any],
    repository: str,
    repository_id: int,
    pull_request: int,
    expected_head: str,
    ref: str,
    http: GitHubHttp,
    token: str,
) -> WorkflowOperation:
    repository_info = _object(payload.get("repository"))
    if (
        repository_info.get("full_name") != repository
        or type(repository_info.get("id")) is not int
        or repository_info["id"] != repository_id
        or repository_info.get("fork") is not False
    ):
        raise refuse("v6_event_binding_invalid")
    if event in {GitHubEventKind.ISSUE_COMMENT, GitHubEventKind.WORKFLOW_DISPATCH}:
        default_branch = repository_info.get("default_branch")
        if not default_branch or ref != f"refs/heads/{default_branch}":
            raise refuse("v6_untrusted_caller_ref")
    elif ref != f"refs/pull/{pull_request}/merge":
        raise refuse("v6_untrusted_caller_ref")

    def live_pr() -> dict[str, Any]:
        status, data = http.request(
            "GET",
            http.repository_path(repository, f"/pulls/{pull_request}"),
            token=token,
        )
        if (
            status != 200
            or not isinstance(data, dict)
            or type(data.get("number")) is not int
            or data["number"] != pull_request
        ):
            raise refuse("v6_pr_metadata_invalid")
        if data.get("state") != "open" or data.get("draft") is not False:
            raise refuse("v6_pr_not_reviewable")
        for side in ("head", "base"):
            identity = _object(_object(data.get(side)).get("repo"))
            if (
                identity.get("fork") is not False
                or type(identity.get("id")) is not int
                or identity["id"] != repository_id
                or identity.get("full_name") != repository
            ):
                raise refuse("v6_untrusted_head_repository")
        return data

    def verify_maintainer() -> None:
        sender = _object(payload.get("sender"))
        login = sender.get("login")
        if (
            sender.get("type") != "User"
            or not _positive(sender.get("id"))
            or not isinstance(login, str)
            or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", login)
            or os.environ.get("GITHUB_ACTOR") != login
            or os.environ.get("GITHUB_ACTOR_ID") != str(sender["id"])
            or os.environ.get("GITHUB_TRIGGERING_ACTOR") != login
        ):
            raise refuse("v6_rescan_actor_invalid")
        status, permission = http.request(
            "GET",
            http.repository_path(repository, f"/collaborators/{login}/permission"),
            token=token,
        )
        if (
            status != 200
            or not isinstance(permission, dict)
            or permission.get("permission") not in {"admin", "maintain", "write"}
            or _object(permission.get("user")).get("id") != sender["id"]
        ):
            raise refuse("v6_rescan_permission_unverified")

    if event is GitHubEventKind.WORKFLOW_DISPATCH:
        verify_maintainer()
        inputs = _object(payload.get("inputs"))
        if (
            str(inputs.get("pull_request_number")) != str(pull_request)
            or inputs.get("operation", "review") != "review"
            or inputs.get("head_repository") not in (None, "", repository)
        ):
            raise refuse("v6_dispatch_binding_invalid")
    elif event is GitHubEventKind.ISSUE_COMMENT:
        issue = _object(payload.get("issue"))
        if issue.get("number") != pull_request or not isinstance(
            issue.get("pull_request"), dict
        ):
            raise refuse("v6_comment_outside_pr")
    else:
        event_pr = _object(payload.get("pull_request"))
        event_head = _object(event_pr.get("head"))
        if (
            event_pr.get("number") != pull_request
            or event_head.get("sha") != expected_head
            or _object(event_head.get("repo")).get("id") != repository_id
        ):
            raise refuse("v6_event_binding_invalid")
        if event is GitHubEventKind.PULL_REQUEST and payload.get("action") not in {
            "opened",
            "reopened",
            "synchronize",
            "ready_for_review",
        }:
            raise refuse("v6_event_binding_invalid")
    pull = live_pr()
    head = _object(pull.get("head")).get("sha")
    if (
        not isinstance(head, str)
        or not _SHA.fullmatch(head)
        or (expected_head and expected_head != head)
    ):
        raise refuse("v6_pr_head_mismatch")

    def verify_head() -> None:
        if live_pr()["head"]["sha"] != head:
            raise refuse("v6_pr_head_mismatch")

    if event not in COMMENT_EVENTS:
        if (
            event is GitHubEventKind.WORKFLOW_DISPATCH
            and str(inputs.get("review_remaining", "false")).lower() != "false"
        ):
            raise refuse("v6_remaining_requires_retained_coverage")

        def verify_review() -> None:
            verify_head()
            if event is GitHubEventKind.WORKFLOW_DISPATCH:
                verify_maintainer()

        return WorkflowOperation(head, "review", verify_review)
    comment = _object(payload.get("comment"))
    author = _object(comment.get("user"))
    actor_id = author.get("id")
    source_id, updated_at, body = (
        comment.get("id"),
        comment.get("updated_at"),
        comment.get("body"),
    )
    if (
        payload.get("action") != "created"
        or not _positive(source_id)
        or not _positive(actor_id)
        or _object(payload.get("sender")).get("id") != actor_id
        or author.get("type") != "User"
        or not authorized_human_comment(comment, app_slug="reviewsensei[bot]")
        or not isinstance(updated_at, str)
        or not updated_at
        or not isinstance(body, str)
        or not has_standalone_sensei_mention(body)
    ):
        raise refuse("v6_mention_unauthorized")
    inline = event is GitHubEventKind.PULL_REQUEST_REVIEW_COMMENT
    root = comment.get("in_reply_to_id", source_id) if inline else source_id
    if not _positive(root):
        raise refuse("v6_invalid_thread_root")
    kind = "inline" if inline else "issue"
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    digest = canonical_sha256(
        [
            repository_id,
            pull_request,
            kind,
            source_id,
            root,
            actor_id,
            updated_at,
            body_hash,
        ]
    )
    publisher = ConversationPublisher(http=http)

    def prepare(*, defer: bool) -> PreparedConversation:
        try:
            result = publisher.prepare_context(
                token=token,
                repository=repository,
                repository_id=repository_id,
                pull_request=pull_request,
                source_kind=kind,
                source_comment_id=source_id,
                source_updated_at=updated_at,
                source_body_sha256=body_hash,
                source_actor_id=actor_id,
                root_comment_id=root,
                app_slug="reviewsensei[bot]",
                expected_head_sha=head,
                defer_evidence=defer,
            )
        except (GitHubConversationError, GitHubHTTPError) as exc:
            raise refuse("v6_mention_binding_invalid") from exc
        if not isinstance(result, PreparedConversation):
            raise refuse("v6_mention_" + result.status)
        return result

    def verify_source() -> None:
        verify_head()
        prepare(defer=True)

    prepare(defer=True)
    if not inline:
        try:
            route = resolve_issue_comment(body, pull).operation
        except ValueError as exc:
            raise refuse("v6_mention_review_target_invalid") from exc
        if route == "review":
            if issue_comment_requests_remaining(body):
                raise refuse("v6_remaining_requires_retained_coverage")
            if comment.get("created_at") != updated_at:
                raise refuse("v6_mention_edited_rescan")
            verify_maintainer()

            def verify_rescan() -> None:
                verify_source()
                verify_maintainer()

            return WorkflowOperation(head, route, verify_rescan, source_id)
        if route == "command":
            raise refuse("v6_maintainer_command_requires_migration")
    if parse_maintainer_command(body, actor=author["login"], head_sha=head) is not None:
        raise refuse("v6_maintainer_command_requires_migration")
    return WorkflowOperation(
        head,
        kind + "_reply",
        verify_source,
        source_id,
        root,
        digest,
        lambda: prepare(defer=False),
    )


def analyze_mention(
    operation: WorkflowOperation, configuration: ProductConfiguration
) -> tuple[tuple[dict[str, Any], ...], tuple[str, ...]]:
    """Prepare authorized live context only when the PR ledger requests a fresh result."""
    assert operation.conversation is not None and operation.source_digest is not None
    prepared = operation.conversation()
    _, settings = v6_provider_settings(configuration)
    tracker = ResourceBudgetTracker(
        ResourceBudget.create(
            max_provider_calls=configuration.advanced.resources.max_provider_calls
            or ResourceBudget.create().max_provider_calls
        )
    )
    try:
        reply = ConversationService(default_registry().create(settings)).reply(
            prepared.context,
            model=settings.model,
            tracker=tracker,
        )
    except ReviewSenseiError as exc:
        raise V6FreshAnalysisError(
            getattr(exc, "diagnostic", None) or "v6_mention_analysis_failed",
            provider_calls=tracker.provider_calls,
        ) from exc
    text = escape_markdown_text(reply.body)
    if reply.resolve and operation.operation == "inline_reply":
        text += "\n\nThis thread remains open for a maintainer to resolve."
    return (
        {
            "schema": "reviewsensei:v6:mention-reply:v1",
            "kind": operation.operation,
            "source": operation.source_digest,
            "source_id": operation.source_id,
            "root_id": operation.root_id,
            "body": text,
            "provider_calls": tracker.provider_calls,
        },
    ), (operation.source_digest,)


def read_workflow_event(
    path: Path,
    *,
    event: GitHubEventKind,
    repository: str,
    repository_id: int,
    pull_request: int,
    expected_head: str,
    api_url: str,
    token: str,
) -> WorkflowOperation:
    if (
        os.environ.get("GITHUB_EVENT_NAME") != event.value
        or os.environ.get("GITHUB_REPOSITORY") != repository
        or os.environ.get("GITHUB_REPOSITORY_ID") != str(repository_id)
    ):
        raise refuse("v6_actions_context_mismatch")
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise refuse("v6_event_unavailable_or_oversized")
    return resolve_workflow_event(
        event=event,
        payload=_object(json.loads(path.read_bytes())),
        repository=repository,
        repository_id=repository_id,
        pull_request=pull_request,
        expected_head=expected_head,
        ref=os.environ.get("GITHUB_REF", ""),
        http=workflow_reader(api_url, repository),
        token=token,
    )


def workflow_plan_digest(plan: HostedPlan) -> str:
    return canonical_sha256(
        [
            plan.backend,
            plan.model,
            plan.runner_kind,
            plan.base_url,
            plan.allow_custom_endpoint,
            plan.credential_env,
            plan.upstream_provider,
        ]
    )


def plan_workflow() -> None:
    """Bind the configured provider and runner kind to a verified PR head."""
    env = os.environ
    event = GitHubEventKind(env["EVENT_KIND"])
    operation = read_workflow_event(
        Path(env["GITHUB_EVENT_PATH"]),
        event=event,
        repository=env["REPOSITORY"],
        repository_id=int(env["REPOSITORY_ID"]),
        pull_request=int(env["PULL_REQUEST"]),
        expected_head=env["HEAD_SHA"],
        api_url=env["GITHUB_API_URL"],
        token=env["GITHUB_TOKEN"],
    )
    config = _load_trusted_configuration(Path(env["POLICY_ROOT"]))
    plan = plan_hosted_execution(config, environ=filtered_hosted_environ())
    if plan.credential_required and plan.credential_env not in {
        "OLLAMA_API_KEY",
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
    }:
        raise refuse("v6_undeclared_provider_credential")
    if plan.runner_kind not in {"hosted", "local"}:
        raise refuse("v6_unsupported_runner_kind")
    enabled = (
        event != GitHubEventKind.PULL_REQUEST or config.github.automatic_reviews
    ) and (event not in COMMENT_EVENTS or config.github.mentions)
    with Path(env["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        print("head_sha=" + operation.head_sha, file=output)
        print("plan_sha=" + workflow_plan_digest(plan), file=output)
        print(
            "credential="
            + ((plan.credential_env or "") if plan.credential_required else ""),
            file=output,
        )
        print("runner_kind=" + plan.runner_kind, file=output)
        print("enabled=" + str(enabled).lower(), file=output)


if __name__ == "__main__":
    plan_workflow()
