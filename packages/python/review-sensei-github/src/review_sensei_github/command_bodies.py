"""Hosted command bodies. Loaded with the engine CLI globals, not imported."""

import argparse
from typing import Any

from review_sensei.baseline import admission_context_from_document
from review_sensei.configuration import customization_directories
from review_sensei.outcomes import (
    diagnostic_for_recovery_error,
    load_recovery_artifact,
)
from review_sensei.service import ReviewRun


def _resolve_hosted_openrouter_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review-sensei resolve-hosted-openrouter",
        description=(
            "Resolve and validate hosted OpenRouter model and upstream "
            "from workflow environment variables."
        ),
    )
    parser.add_argument(
        "action",
        choices=("model", "upstream", "both"),
        help="Emit resolved model, upstream, or both as a tab-separated line.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=_package_version(),
        help="Print the installed review-sensei-github version and exit",
    )
    return parser


def _host_plan_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review-sensei host-plan",
        description=(
            "Resolve the trusted hosted backend, runner requirement, and GitHub "
            "policy without provider or GitHub calls."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        help=(
            "Configuration file to resolve; defaults to .reviewsensei.yml when "
            "present, then packaged defaults."
        ),
    )
    parser.add_argument("--provider", help="Backend override (inference.backend)")
    parser.add_argument("--model", help="Model override (inference.model)")
    parser.add_argument(
        "--github-output",
        type=Path,
        help=(
            "Write the plan as Actions environment-file lines to this path. "
            "The workflow passes its own $GITHUB_OUTPUT file; the command never "
            "reads that variable itself."
        ),
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _github_parser() -> argparse.ArgumentParser:
    from review_sensei.configuration import REVIEW_POLICIES

    parser = argparse.ArgumentParser(
        prog="review-sensei github",
        description="Run GitHub publication seams with write opt-ins.",
    )
    _add_allow_custom_endpoint_argument(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    review = subparsers.add_parser(
        "review",
        aliases=["publish"],
        help="Publish a validated review result",
    )
    publication_source = review.add_mutually_exclusive_group()
    publication_source.add_argument("--result", type=Path)
    publication_source.add_argument(
        "--recover-retained",
        action="store_true",
        help="Recover the original transaction-bound result from owned PR parts without inference; trusted contexts and a session ledger remain required.",
    )
    review.add_argument(
        "--configuration-context",
        type=Path,
        help=(
            "Trusted configuration-context JSON emitted alongside an "
            "identity-bound analysis result"
        ),
    )
    review.add_argument(
        "--reconstruct-contexts",
        action="store_true",
        help="Explicit publication-only recovery: reconstruct the original canonical caller contexts from frozen trusted source and the authenticated committed transaction; never invoke inference.",
    )
    review.add_argument(
        "--trusted-context-root",
        type=Path,
        help="Independently authenticated original policy checkout for --reconstruct-contexts; changed or unavailable context refuses.",
    )
    review.add_argument(
        "--evidence-context",
        type=Path,
        help="Optional trusted evidence-context JSON for an identity-bound result",
    )
    review.add_argument(
        "--admission-context",
        type=Path,
        help=(
            "Trusted admission-context JSON (prior baseline and current review "
            "context cache key) emitted by the analysis that produced this "
            "result; required to publish a verification round, which cannot "
            "reconstruct either input from the durable transaction alone"
        ),
    )
    review.add_argument("--diff", type=Path, required=True)
    review.add_argument("--repository", required=True)
    review.add_argument("--repository-id", type=int, required=True)
    review.add_argument("--pull-request", type=int, required=True)
    review.add_argument("--head-sha", required=True)
    review.add_argument("--base-branch")
    review.add_argument("--base-sha")
    review.add_argument("--app-slug", default="reviewsensei[bot]")
    review.add_argument("--oidc-token")
    review.add_argument(
        "--outcome",
        type=Path,
        help="Write the structured run-outcome JSON for this publication",
    )
    publication_source.add_argument(
        "--recover-from",
        type=Path,
        help="Publish a retained recovery artifact without invoking a model. Ambient REVIEWSENSEI_REVIEW_MODE does not apply; an explicit operator --review-mode is refused because serialized results drop admission state.",
    )
    review.add_argument(
        "--allow-write",
        action="store_true",
        help="Acknowledge that GitHub writes are enabled for this invocation.",
    )
    review.add_argument(
        "--enable-review",
        action="store_true",
        help="Opt into review publication for this invocation.",
    )
    review.add_argument(
        "--enable-auto-approve",
        dest="enable_auto_approve",
        action="store_true",
        default=True,
        help="Approve eligible complete reviews (the default).",
    )
    review.add_argument(
        "--no-auto-approve",
        dest="enable_auto_approve",
        action="store_false",
        help=(
            "Enforce required fixes through the ReviewSensei check without "
            "emitting an approval, unless --reviews-policy says otherwise."
        ),
    )
    review.add_argument(
        "--reviews-policy",
        choices=list(REVIEW_POLICIES),
        default=None,
        help=(
            "Canonical github.reviews mode: auto-approve (default), blocking, "
            "or advisory. Takes precedence over the approval boolean."
        ),
    )
    review.add_argument(
        "--config", type=Path, help="Trusted publication configuration file"
    )
    review.add_argument(
        "--publication-inventory",
        choices=("legacy", "complete"),
        default=None,
        help="Enable complete larger inventories only after all readers support them; default comes from trusted configuration.",
    )
    review.add_argument(
        "--operation-entry",
        choices=("enabled", "disabled"),
        default=None,
        help="Preserve the configured validated-result retention policy.",
    )
    review.add_argument(
        "--enable-learning-prs",
        action="store_true",
        help="Opt into deterministic draft learning-PR publication.",
    )
    review.add_argument(
        "--review-mode",
        help=(
            "Review-convergence mode: merge-focused (default), advisory, or "
            "strict. Legacy settings must be migrated before execution. "
            "Operator modes apply trusted blocker admission before GitHub review "
            "events. Recovery artifacts cannot be republished under operator modes."
        ),
    )
    review.add_argument(
        "--session-ledger",
        type=Path,
        help=(
            "Local directory for the issue #136 durable session ledger. "
            "Operator modes reserve/commit a counted round around publication "
            "and refuse unadmitted REQUEST_CHANGES (C5)."
        ),
    )
    review.add_argument(
        "--github-session-ledger",
        action="store_true",
        help=(
            "Persist the C3 session ledger as one GitHub issue comment on the "
            "source pull request. Operator modes keep rounds, baselines, and "
            "dispositions durable."
        ),
    )
    command = subparsers.add_parser(
        "command",
        help="Apply an authenticated @reviewsensei maintainer command to the session ledger",
    )
    command.add_argument("--comment-body", required=True)
    command.add_argument("--actor", required=True)
    command.add_argument("--actor-type", default="User")
    command.add_argument("--association", required=True)
    command.add_argument("--repository", required=True)
    command.add_argument(
        "--repository-id",
        type=int,
        help="GitHub repository database ID required for the hosted session ledger",
    )
    command.add_argument("--pull-request", type=int, required=True)
    command.add_argument("--head-sha")
    command.add_argument(
        "--source-comment-id",
        type=int,
        help="GitHub issue-comment ID required for a hosted command mutation",
    )
    command.add_argument("--app-slug", default="reviewsensei[bot]")
    command.add_argument(
        "--session-ledger",
        type=Path,
        help=(
            "Local operator directory for the durable session ledger; hosted "
            "webhook commands use GitHubApplication and broker authorization."
        ),
    )
    command.add_argument(
        "--github-session-ledger",
        action="store_true",
        help="Use the broker-attested GitHub issue-comment session ledger.",
    )
    command.add_argument(
        "--session-attestation",
        type=Path,
        help="Broker-bound hosted command attestation JSON.",
    )
    command.add_argument(
        "--oidc-token",
        help="Optional GitHub Actions OIDC assertion for the hosted ledger.",
    )
    command.add_argument(
        "--allow-write",
        action="store_true",
        help=(
            "Required for mutating session commands and when exchanging a "
            "GitHub capability; read-only status does not require it."
        ),
    )
    command.add_argument(
        "--config",
        type=Path,
        help="Path to trusted ReviewSensei configuration",
    )
    command.add_argument(
        "--review-mode",
        help=("Review-convergence mode: merge-focused (default), advisory, or strict."),
    )

    reply = subparsers.add_parser("reply", help="Generate or publish a mention reply")
    reply.add_argument("--reply", type=Path)
    _add_work_recovery_arguments(reply)
    reply.add_argument(
        "--enable-broader-review",
        action="store_true",
        help="Allow one bounded full discovery continuation under trusted unified review policy.",
    )
    reply.add_argument(
        "--config", type=Path, help="Path to trusted ReviewSensei configuration"
    )
    reply.add_argument(
        "--generate",
        action="store_true",
        help="Build bounded GitHub context, invoke the provider, and publish the reply.",
    )
    reply.add_argument("--repository", required=True)
    reply.add_argument("--repository-id", type=int)
    reply.add_argument("--pull-request", type=int, required=True)
    reply.add_argument("--source-comment-id", type=int, required=True)
    reply.add_argument("--source-updated-at", required=True)
    reply.add_argument("--head-sha")
    reply.add_argument(
        "--source-kind",
        choices=("inline", "issue"),
        default="inline",
    )
    reply.add_argument("--app-slug", default="reviewsensei[bot]")
    reply.add_argument("--root-comment-id", type=int, default=0)
    reply.add_argument("--oidc-token")
    reply.add_argument("--github-token-env", default="GITHUB_TOKEN")
    reply.add_argument(
        "--profile",
        default=None,
        help=(
            "Named provider profile (local-private, fast-triage, deep-verification, "
            "openrouter-sonnet, openrouter-gpt)"
        ),
    )
    reply.add_argument(
        "--provider", default=os.getenv("REVIEWSENSEI_PROVIDER", "ollama")
    )
    reply.add_argument("--base-url", default=_default_ollama_base_url())
    _add_allow_custom_endpoint_argument(reply, default=argparse.SUPPRESS)
    reply.add_argument("--model", default=_default_ollama_model())
    reply.add_argument("--api-key-env", default="OLLAMA_API_KEY")
    reply.add_argument(
        "--timeout-seconds",
        type=_positive_float,
        default=os.getenv("OLLAMA_TIMEOUT_SECONDS", "900"),
    )
    reply.add_argument(
        "--allow-unqualified-profile",
        action="store_true",
        help=(
            "Authorize live provider use with an unqualified profile before "
            "qualification evidence exists"
        ),
    )
    reply.add_argument(
        "--allow-write",
        action="store_true",
        help="Acknowledge that GitHub writes may be enabled for this invocation.",
    )
    reply.add_argument(
        "--enable-reply",
        action="store_true",
        help="Enable mention reply publication for this invocation.",
    )
    return parser


def _require_accepted_result(
    result: object,
    *,
    repository: str,
    pull_request: int,
    app_slug: str,
    operation_entry: str | None = None,
    publication_http: Any = None,
    checkpoint: dict[str, Any] | None = None,
) -> list[Any] | None:
    """Refuse publication when an operation record accepted different bytes."""

    if not os.environ.get("GITHUB_RUN_ID", "").strip():
        return None
    configuration = load_configuration(None)
    if (operation_entry or configuration.github.operation_entry) != "enabled":
        return None
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not isinstance(token, str) or not token.strip():
        raise ReviewInputError("operation record is unavailable")
    from review_sensei_github.errors import GitHubHTTPError
    from review_sensei_github.http import MAX_PAGINATION_PAGES, GitHubHttp
    from review_sensei_github.operation_entry import accepted_payload_matches

    http = publication_http if publication_http is not None else GitHubHttp()
    comments_path = http.repository_path(repository, f"/issues/{pull_request}/comments")
    try:
        comments = http.paginate(
            path=comments_path,
            token=token.strip(),
            page_sizes=(100, 50, 20, 10, 5, 1),
            max_requests=MAX_PAGINATION_PAGES,
            timeout_seconds=60,
        )
    except GitHubHTTPError:
        # A missing record permits the legacy path only after a complete read.
        # A full final page, rejected page or bounded transport failure cannot
        # establish that no accepted operation exists beyond the observed prefix.
        raise ReviewInputError(
            "operation record inventory is incomplete or unavailable"
        ) from None
    bodies: list[str] = []
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        user = comment.get("user")
        login = user.get("login") if isinstance(user, dict) else None
        body = comment.get("body")
        if (
            isinstance(login, str)
            and login.lower() == app_slug.lower()
            and isinstance(body, str)
        ):
            bodies.append(body)
    digest = hashlib.sha256(
        json.dumps(
            result.to_dict(),  # type: ignore[attr-defined]
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    match = accepted_payload_matches(tuple(bodies), digest)
    if match is False and checkpoint is not None:
        from review_sensei.session import load_review_transaction_for_publication

        # Analysis accepts bytes before the durable checkpoint adds its
        # transaction envelope. Only an independently validated durable
        # transaction permits that one normalization; retained part hashes do
        # not constitute an operation acceptance proof.
        checkpoint_ledger = checkpoint.get("ledger")
        ledger_http = getattr(checkpoint_ledger, "http", None)
        primed_map = getattr(ledger_http, "primed_issue_comments", None)
        primed = False
        if (
            isinstance(primed_map, dict)
            and getattr(checkpoint_ledger, "token", None) == token.strip()
            and comments_path not in primed_map
        ):
            primed_map[comments_path] = list(comments)
            primed = True
        try:
            load_review_transaction_for_publication(result=result, **checkpoint)
        finally:
            if primed and isinstance(primed_map, dict):
                primed_map.pop(comments_path, None)
        original = result.to_dict()
        original.pop("transaction", None)
        original_digest = hashlib.sha256(
            json.dumps(original, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        match = accepted_payload_matches(tuple(bodies), original_digest)
    if match is False:
        raise ReviewInputError(
            "published result does not match the accepted operation record"
        )
    return list(comments)


def _identity_from_indexed(item: Any) -> Any:
    from review_sensei.disposition import FindingIdentity

    return FindingIdentity(
        instance=item.instance,
        concern=item.lifecycle,
        blocking=bool(item.blocking),
        needs_human=bool(item.needs_human),
        overridable=bool(item.overridable),
    )


class OverrideFacts:
    """Permission plus the current head's finding inventory and older heads."""

    def __init__(self, permission, inventory, prior_heads) -> None:
        self.permission = permission
        self.inventory = inventory
        self.prior_heads = prior_heads


def _legacy_inventory(
    http: object,
    *,
    token: str,
    repository: str,
    pull_request: int,
    review: dict[str, Any],
    empty_discussion: bool = False,
    repository_id: int | None = None,
    app_slug: str | None = None,
    before_read=None,
):
    """Eligibility needs-human IDs, plus inline instance/lifecycle pairs."""

    from review_sensei_github.errors import GitHubHTTPError
    from review_sensei_github.http import MAX_PAGINATION_PAGES
    from review_sensei_github.publication import (
        APPROVAL_ELIGIBILITY_MARKER_PREFIX,
        IndexedFinding,
        approval_eligibility_from_body,
        finding_blocking_from_body,
        finding_fingerprint_from_body,
        finding_instance_from_body,
        finding_publication_scope,
    )

    body = review.get("body")
    entries: list[IndexedFinding] = []
    seen: set[str] = set()
    if isinstance(body, str) and APPROVAL_ELIGIBILITY_MARKER_PREFIX in body:
        eligibility = approval_eligibility_from_body(body)
        if eligibility is None or eligibility.partitioned_root is not None:
            raise ReviewInputError("current review inventory is incomplete")
        human = None if eligibility is None else eligibility.human_review
        if human is not None:
            for item in human.findings:
                if item.fingerprint in seen:
                    continue
                seen.add(item.fingerprint)
                entries.append(
                    IndexedFinding(
                        instance=item.fingerprint,
                        lifecycle=None,
                        blocking=False,
                        needs_human=True,
                        body_placed=True,
                        overridable=True,
                    )
                )
    review_id = review.get("id")
    if isinstance(review_id, bool) or not isinstance(review_id, int):
        return tuple(entries)
    try:
        comments = http.paginate(  # type: ignore[attr-defined]
            path=http.repository_path(  # type: ignore[attr-defined]
                repository, f"/pulls/{pull_request}/reviews/{review_id}/comments"
            ),
            token=token,
            max_requests=MAX_PAGINATION_PAGES,
            timeout_seconds=60,
            before_read=before_read,
        )
    except GitHubHTTPError as exc:
        raise ReviewInputError("current review inventory is incomplete") from exc
    if not isinstance(comments, list):
        raise ReviewInputError("current review inventory is incomplete")
    comment_ids: set[int] = set()
    root_base: str | None = None
    for comment in comments:
        if not isinstance(comment, dict):
            if empty_discussion:
                raise ReviewInputError("current review inventory is incomplete")
            continue
        comment_body = comment.get("body")
        if empty_discussion:
            user = comment.get("user")
            comment_id = comment.get("id")
            if (
                type(comment_id) is not int
                or comment_id < 1
                or comment_id in comment_ids
                or type(comment.get("pull_request_review_id")) is not int
                or comment["pull_request_review_id"] != review_id
                or not isinstance(user, dict)
                or user.get("type") != "Bot"
                or not isinstance(user.get("login"), str)
                or user["login"].casefold() != (app_slug or "").casefold()
                or not isinstance(comment_body, str)
            ):
                raise ReviewInputError("current review inventory is incomplete")
            comment_ids.add(comment_id)
            parent = comment.get("in_reply_to_id")
            if parent is not None:
                if type(parent) is not int or parent < 1:
                    raise ReviewInputError("current review inventory is incomplete")
                # A positively owned inline reply is discussion, not a new
                # top-level finding, regardless of IDs quoted in its prose.
                continue
        instance = finding_instance_from_body(comment_body)
        lifecycle = finding_fingerprint_from_body(comment_body)
        scope = finding_publication_scope(comment_body) if empty_discussion else None
        if empty_discussion:
            if (
                instance is None
                or lifecycle is None
                or instance in seen
                or scope is None
                or scope[:3] != (repository_id, pull_request, review.get("commit_id"))
                or (root_base is not None and root_base != scope[3])
                or comment_body.count("<!-- reviewsensei:finding:") != 1
            ):
                # Unknown, malformed or conflicting top-level roots must not
                # expose an older clean inventory as if this were only replies.
                raise ReviewInputError("current review inventory is incomplete")
            root_base = scope[3]
        if not isinstance(instance, str) or instance in seen or lifecycle is None:
            continue
        seen.add(instance)
        blocking = finding_blocking_from_body(comment_body) is True
        entries.append(
            IndexedFinding(
                instance=instance,
                lifecycle=lifecycle,
                blocking=blocking,
                needs_human=scope[-1] is True if scope is not None else False,
                body_placed=False,
                overridable=True,
            )
        )
    return tuple(entries)


def _collaborator_permission(
    http: object,
    *,
    token: str,
    repository: str,
    actor: str,
) -> str:
    """Read live collaborator permission for a repository actor."""

    if not re.fullmatch(r"[A-Za-z0-9-]{1,39}", actor):
        raise ReviewInputError("maintainer actor is invalid")
    permission_status, permission_body = http.request(  # type: ignore[attr-defined]
        "GET",
        http.repository_path(  # type: ignore[attr-defined]
            repository, f"/collaborators/{actor}/permission"
        ),
        token=token,
    )
    permission = (
        permission_body.get("permission") if isinstance(permission_body, dict) else None
    )
    if permission_status != 200 or not isinstance(permission, str):
        raise ReviewInputError("collaborator permission is unavailable")
    # GitHub projects the built-in maintain role onto legacy permission=write.
    # Only that exact pair supplies maintain authority; ordinary write and
    # custom roles retain their legacy permission and the existing denial.
    if permission == "write" and permission_body.get("role_name") == "maintain":
        permission = "maintain"
    return permission


def _override_facts(
    http: object,
    *,
    token: str,
    repository: str,
    repository_id: int,
    pull_request: int,
    head_sha: str,
    actor: str,
    app_slug: str,
) -> OverrideFacts:
    """Read collaborator permission and the current App review inventory.

    The command does not accept a caller-supplied review file. Identities come
    from the latest App review on this head. Older heads are returned only so
    a receipt can name the commit that last displayed an unknown ID.
    """

    from time import monotonic

    from review_sensei_github.errors import GitHubHTTPError
    from review_sensei_github.http import MAX_PAGINATION_PAGES
    from review_sensei_github.operation_entry import finding_inventory_for_head

    permission = _collaborator_permission(
        http,
        token=token,
        repository=repository,
        actor=actor,
    )
    try:
        reviews = http.paginate(  # type: ignore[attr-defined]
            path=http.repository_path(  # type: ignore[attr-defined]
                repository, f"/pulls/{pull_request}/reviews"
            ),
            token=token,
            max_requests=MAX_PAGINATION_PAGES,
            timeout_seconds=60,
        )
    except GitHubHTTPError as exc:
        raise ReviewInputError("current review fingerprints are unavailable") from exc
    if not isinstance(reviews, list):
        raise ReviewInputError("current review fingerprints are unavailable")
    latest: dict[str, Any] | None = None
    latest_legacy = None
    prior: dict[str, dict[str, Any]] = {}
    legacy_requests = 0
    legacy_deadline = monotonic() + 60

    def before_legacy_read() -> float:
        nonlocal legacy_requests
        remaining = legacy_deadline - monotonic()
        if legacy_requests >= MAX_PAGINATION_PAGES or remaining <= 0:
            raise ReviewInputError("current review inventory is incomplete")
        legacy_requests += 1
        return min(60, remaining)

    # Inspect only newer candidates, with one shared bounded metadata walk.
    # An authoritative current-head record ends discussion-container fallback.
    for review in sorted(
        reviews,
        key=lambda item: (
            item.get("id", -1)
            if isinstance(item, dict) and type(item.get("id")) is int
            else -1
        ),
        reverse=True,
    ):
        if not isinstance(review, dict):
            continue
        user = review.get("user")
        login = user.get("login") if isinstance(user, dict) else None
        if not isinstance(login, str) or login.lower() != app_slug.lower():
            continue
        review_id = review.get("id")
        commit_id = review.get("commit_id")
        if (
            isinstance(review_id, bool)
            or not isinstance(review_id, int)
            or not isinstance(commit_id, str)
        ):
            continue
        if commit_id == head_sha:
            if latest is not None:
                continue
            if review.get("state") == "COMMENTED" and review.get("body") in (None, ""):
                candidate = _legacy_inventory(
                    http,
                    token=token,
                    repository=repository,
                    pull_request=pull_request,
                    review=review,
                    empty_discussion=True,
                    repository_id=repository_id,
                    app_slug=app_slug,
                    before_read=before_legacy_read,
                )
                if not candidate:
                    continue
                latest_legacy = candidate
            latest = review
            continue
        previous = prior.get(commit_id)
        if previous is None or review_id >= int(previous["id"]):
            prior[commit_id] = review
    inventory: tuple[Any, ...] = ()
    if latest_legacy is not None:
        inventory = latest_legacy
    elif latest is not None and isinstance(latest.get("body"), str):
        index = finding_inventory_for_head(
            latest["body"],
            repository_id=repository_id,
            pull_request=pull_request,
            head_sha=head_sha,
        )
        if index is not None:
            inventory = index.entries
        else:
            inventory = _legacy_inventory(
                http,
                token=token,
                repository=repository,
                pull_request=pull_request,
                review=latest,
                before_read=before_legacy_read,
            )
    prior_heads: list[tuple[str, tuple[Any, ...]]] = []
    for commit_id, review in prior.items():
        if not isinstance(review.get("body"), str):
            continue
        try:
            index = finding_inventory_for_head(
                review["body"],
                repository_id=repository_id,
                pull_request=pull_request,
                head_sha=commit_id,
            )
        except ReviewInputError:
            continue
        if index is not None:
            prior_heads.append((commit_id, index.entries))
    return OverrideFacts(permission, inventory, tuple(prior_heads))


def _bind_hosted_command_attestation(
    attestation: dict[str, object],
    *,
    repository: str,
    repository_id: int,
    pull_request: int,
    head_sha: str,
    source_comment_id: int,
) -> None:
    """Reject a hosted command attestation that disagrees with this invocation.

    The broker re-checks this binding against the OIDC claims; checking it here
    fails a mismatched attestation before any credential is exchanged.

    `run_id` is intentionally not compared: the CLI has no trustworthy run
    identity of its own, so the broker is the only place that can bind it.
    """

    if (
        attestation.get("version") != 1
        or attestation.get("operation") != "command"
        or attestation.get("repository") != repository
        or attestation.get("repository_id") != repository_id
        or attestation.get("pull_request") != pull_request
        or attestation.get("head_sha") != head_sha
        or attestation.get("source_comment_id") != source_comment_id
        or attestation.get("concurrency_group")
        != f"reviewsensei-session-{repository_id}-{pull_request}"
    ):
        raise ReviewInputError(
            "session attestation does not match the hosted command identity"
        )


def _write_step_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not isinstance(path, str) or not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text if text.endswith("\n") else text + "\n")
    except OSError:
        return


def _is_app_receipt_comment(
    comment: object,
    *,
    app_slug: str,
    expected_user_id: int | None = None,
) -> bool:
    if not isinstance(comment, dict):
        return False
    user = comment.get("user")
    if not isinstance(user, dict):
        return False
    login = user.get("login")
    user_type = user.get("type")
    if not isinstance(login, str) or login != app_slug or user_type != "Bot":
        return False
    if expected_user_id is not None:
        user_id = user.get("id")
        if type(user_id) is not int or user_id != expected_user_id:
            return False
    return True


def _has_terminal_receipt_marker(body: object, marker: str) -> bool:
    if not isinstance(body, str):
        return False
    stripped = body.rstrip()
    if not stripped:
        return False
    terminal_line = stripped.rsplit("\n", 1)[-1].strip()
    return terminal_line == marker


def _post_command_receipt(
    http: object,
    broker: object,
    *,
    oidc_token: str | None,
    repository: str,
    pull_request: int,
    source_comment_id: int,
    comment_body: str,
    actor: str,
    receipts: tuple[object, ...],
    ledger_written: bool,
    app_slug: str = "reviewsensei[bot]",
    expected_app_user_id: int | None = None,
) -> None:
    """Post or reconcile one App-owned receipt comment with readback verification."""

    if (
        broker is None
        or not receipts
        or not isinstance(source_comment_id, int)
        or source_comment_id <= 0
    ):
        return
    import hashlib

    from review_sensei.presentation import render_command_receipt
    from review_sensei_github.http import MAX_PAGINATION_PAGES

    digest = hashlib.sha256(comment_body.encode("utf-8")).hexdigest()
    text = render_command_receipt(
        actor=actor,
        source_comment_id=source_comment_id,
        command_digest=digest,
        receipts=receipts,
        ledger_written=ledger_written,
    )
    marker = (
        "<!-- reviewsensei:command-receipt:v1 "
        f"source={source_comment_id} digest={digest} -->"
    )
    try:
        token = broker.exchange(  # type: ignore[attr-defined]
            oidc_token or broker.request_oidc_token(),  # type: ignore[attr-defined]
            capability="issue_reply",
        )
        if not isinstance(token, str) or not token.strip():
            raise ReviewInputError("issue reply capability is unavailable")
        clean_token = token.strip()
        comments = http.paginate(  # type: ignore[attr-defined]
            path=http.repository_path(  # type: ignore[attr-defined]
                repository, f"/issues/{pull_request}/comments"
            ),
            token=clean_token,
            max_requests=MAX_PAGINATION_PAGES,
            timeout_seconds=60,
        )
        existing_comment_id: int | None = None
        for comment in comments:
            if not _is_app_receipt_comment(
                comment,
                app_slug=app_slug,
                expected_user_id=expected_app_user_id,
            ):
                continue
            body_text = comment.get("body")
            if not _has_terminal_receipt_marker(body_text, marker):
                continue
            if isinstance(body_text, str) and body_text.strip() == text.strip():
                return
            cid = comment.get("id")
            if type(cid) is int and cid > 0:
                existing_comment_id = cid
                break
        if existing_comment_id is not None:
            status, payload = http.request(  # type: ignore[attr-defined]
                "PATCH",
                http.repository_path(  # type: ignore[attr-defined]
                    repository, f"/issues/comments/{existing_comment_id}"
                ),
                token=clean_token,
                body={"body": text},
            )
        else:
            status, payload = http.request(  # type: ignore[attr-defined]
                "POST",
                http.repository_path(  # type: ignore[attr-defined]
                    repository, f"/issues/{pull_request}/comments"
                ),
                token=clean_token,
                body={"body": text},
            )
        if status not in {200, 201}:
            _write_step_summary(text)
            return
        if isinstance(payload, dict) and payload.get("body") is not None:
            if str(payload.get("body") or "").strip() != text.strip() or (
                payload.get("user") is not None
                and not _is_app_receipt_comment(
                    payload,
                    app_slug=app_slug,
                    expected_user_id=expected_app_user_id,
                )
            ):
                _write_step_summary(text)
    except Exception:
        _write_step_summary(text)


def _run_github(args: argparse.Namespace, *, argv: list[str]) -> int:
    from review_sensei.convergence import (
        OPERATOR_REVIEW_MODES,
        ReviewConvergencePolicy,
        resolve_review_convergence_policy,
        resolve_review_mode,
    )
    from review_sensei.models import ConversationReply, ReviewResult
    from review_sensei.session import resolve_local_session_ledger
    from review_sensei_github import (
        BrokerClient,
        ConversationPublisher,
        GitHubApplication,
        GitHubHttp,
        GitHubWriteOptions,
        LearningPRPublisher,
        LearningPRResult,
        ReviewPublisher,
    )
    from review_sensei_github.errors import (
        GitHubPublicationError,
        GitHubPublicationTransientError,
    )
    from review_sensei_github.publication import outcome_from_publication

    if args.command == "command":
        from review_sensei.disposition import (
            apply_session_command,
            authorized_maintainer,
            authorized_override,
            parse_maintainer_command,
        )
        from review_sensei.session import SessionIdentity

        parsed = parse_maintainer_command(
            args.comment_body,
            actor=args.actor,
            head_sha=getattr(args, "head_sha", None),
        )
        if parsed is None:
            raise ReviewInputError("comment is not a supported maintainer command")
        if not authorized_maintainer(
            login=args.actor,
            user_type=args.actor_type,
            association=args.association,
            app_slug=args.app_slug,
        ):
            raise ReviewInputError("maintainer command is unauthorized")
        if (
            not getattr(args, "github_session_ledger", False)
            and (
                parsed.action
                in {
                    "approve-media",
                    "accept-risk",
                    "accept-coverage-uncertainty",
                    "reenroll",
                }
                or parsed.finding_reference
                or parsed.finding_references
            )
            and not authorized_override(association=args.association)
        ):
            raise ReviewInputError("maintainer command is unauthorized")
        if parsed.action != "status" and not args.allow_write:
            raise ReviewInputError("github writes require --allow-write")
        if getattr(args, "github_session_ledger", False):
            if args.session_ledger is not None:
                raise ReviewInputError(
                    "hosted maintainer commands cannot use --session-ledger"
                )
            if (
                isinstance(args.repository_id, bool)
                or not isinstance(args.repository_id, int)
                or args.repository_id <= 0
            ):
                raise ReviewInputError(
                    "hosted maintainer commands require --repository-id"
                )
            if not isinstance(args.head_sha, str) or not re.fullmatch(
                r"[a-f0-9]{40}", args.head_sha
            ):
                raise ReviewInputError("hosted maintainer commands require --head-sha")
            source_comment_id = getattr(args, "source_comment_id", None)
            session_attestation = None
            if parsed.action != "status":
                if (
                    isinstance(source_comment_id, bool)
                    or not isinstance(source_comment_id, int)
                    or source_comment_id <= 0
                ):
                    raise ReviewInputError(
                        "hosted command mutations require --source-comment-id"
                    )
                attestation_path = getattr(args, "session_attestation", None)
                if attestation_path is None:
                    raise ReviewInputError(
                        "hosted command mutations require --session-attestation"
                    )
                try:
                    loaded_attestation = json.loads(
                        read_bounded_utf8(
                            attestation_path,
                            maximum=16 * 1024,
                            label="session attestation",
                        )
                    )
                except json.JSONDecodeError as exc:
                    raise ReviewInputError(
                        "session attestation is invalid JSON"
                    ) from exc
                if not isinstance(loaded_attestation, dict):
                    raise ReviewInputError("session attestation must be an object")
                _bind_hosted_command_attestation(
                    loaded_attestation,
                    repository=args.repository,
                    repository_id=args.repository_id,
                    pull_request=args.pull_request,
                    head_sha=args.head_sha,
                    source_comment_id=source_comment_id,
                )
                session_attestation = loaded_attestation
            http = GitHubHttp()
            media_budget = None
            media_read_token = None
            command_broker = None
            if parsed.action == "approve-media":
                from review_sensei.bounded_evidence import EvidenceReadBudget
                from review_sensei_github.broker_control import BrokerDispatchAccounting
                from review_sensei_github.media_budget import media_http
                from review_sensei_github.publication_recovery import publication_http

                http = media_http(http)
                media_budget = EvidenceReadBudget()
                command_broker = BrokerClient(
                    control=BrokerDispatchAccounting(
                        media_budget,
                        hashlib.sha256(
                            f"media:{args.repository_id}:{args.pull_request}:{args.head_sha}:{source_comment_id}".encode()
                        ).hexdigest(),
                        10,
                        1,
                    )
                )
            actor_permission = None
            finding_fingerprints = None
            finding_inventory = None
            prior_inventories = ()
            if (
                parsed.action == "approve-media"
                or parsed.finding_reference
                or parsed.finding_references
            ):
                read_token = os.environ.get("GITHUB_TOKEN") or os.environ.get(
                    "GH_TOKEN"
                )
                if not isinstance(read_token, str) or not read_token.strip():
                    raise ReviewInputError(
                        "maintainer override requires GitHub metadata access"
                    )
                if parsed.action == "approve-media":
                    media_read_token = read_token.strip()
                    actor_permission = _collaborator_permission(
                        publication_http(http, media_budget),
                        token=media_read_token,
                        repository=args.repository,
                        actor=args.actor,
                    )
                else:
                    facts = _override_facts(
                        http,
                        token=read_token.strip(),
                        repository=args.repository,
                        repository_id=args.repository_id,
                        pull_request=args.pull_request,
                        head_sha=args.head_sha,
                        actor=args.actor,
                        app_slug=args.app_slug,
                    )
                    actor_permission = facts.permission
                    finding_inventory = tuple(
                        _identity_from_indexed(item) for item in facts.inventory
                    )
                    prior_inventories = tuple(
                        (head, tuple(_identity_from_indexed(item) for item in entries))
                        for head, entries in facts.prior_heads
                    )
            elif parsed.action in {
                "accept-risk",
                "accept-coverage-uncertainty",
                "reenroll",
                "continue",
                "dismiss",
                "defer",
            }:
                read_token = os.environ.get("GITHUB_TOKEN") or os.environ.get(
                    "GH_TOKEN"
                )
                if isinstance(read_token, str) and read_token.strip():
                    try:
                        actor_permission = _collaborator_permission(
                            http,
                            token=read_token.strip(),
                            repository=args.repository,
                            actor=args.actor,
                        )
                    except ReviewInputError:
                        if parsed.action in {
                            "accept-risk",
                            "accept-coverage-uncertainty",
                            "reenroll",
                        }:
                            raise
                elif parsed.action in {"accept-risk", "accept-coverage-uncertainty"}:
                    raise ReviewInputError(
                        "maintainer override requires GitHub metadata access"
                    )
            application = GitHubApplication(
                broker=command_broker if command_broker is not None else BrokerClient(),
                http=http,
                reviewer=ReviewPublisher(http=http),
                learner=LearningPRPublisher(http=http),
                replier=ConversationPublisher(http=http),
            )
            command_configuration = load_configuration(getattr(args, "config", None))
            explicit_mode = getattr(args, "review_mode", None)
            if (
                explicit_mode is None
                and not os.environ.get("REVIEWSENSEI_REVIEW_MODE", "").strip()
                and command_configuration.github.reviews == "advisory"
            ):
                explicit_mode = "advisory"
            command_convergence_policy = resolve_review_convergence_policy(
                mode=explicit_mode
            )
            from review_sensei.disposition import OverrideRejected

            try:
                result = application.apply_maintainer_command(
                    options=GitHubWriteOptions(
                        github_writes=args.allow_write,
                        github_session_ledger=True,
                    ),
                    oidc_token=getattr(args, "oidc_token", None),
                    repository=args.repository,
                    repository_id=args.repository_id,
                    pull_request=args.pull_request,
                    head_sha=args.head_sha,
                    body=args.comment_body,
                    actor_login=args.actor,
                    actor_type=args.actor_type,
                    association=args.association,
                    app_slug=args.app_slug,
                    source_comment_id=source_comment_id,
                    session_attestation=session_attestation,
                    actor_permission=actor_permission,
                    finding_fingerprints=finding_fingerprints,
                    finding_inventory=finding_inventory,
                    prior_inventories=prior_inventories,
                    media_budget=media_budget,
                    media_read_token=media_read_token,
                    reviews_policy=command_configuration.github.reviews,
                    convergence_policy=command_convergence_policy,
                    operation_entry=command_configuration.github.operation_entry,
                    app_id=_actions_app_id(),
                    run_id=os.environ.get("GITHUB_RUN_ID", "").strip() or None,
                    run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT", "1").strip()
                    or "1",
                )
            except OverrideRejected as exc:
                _post_command_receipt(
                    http,
                    getattr(application, "broker", None),
                    oidc_token=getattr(args, "oidc_token", None),
                    repository=args.repository,
                    pull_request=args.pull_request,
                    source_comment_id=source_comment_id,
                    comment_body=args.comment_body,
                    actor=args.actor,
                    receipts=exc.receipts,
                    ledger_written=False,
                    app_slug=args.app_slug,
                )
                raise
            print(result.summary)
            _post_command_receipt(
                http,
                getattr(application, "broker", None),
                oidc_token=getattr(args, "oidc_token", None),
                repository=args.repository,
                pull_request=args.pull_request,
                source_comment_id=source_comment_id,
                comment_body=args.comment_body,
                actor=args.actor,
                receipts=tuple(getattr(result, "receipts", ()) or ()),
                ledger_written=bool(getattr(result, "applied", False)),
                app_slug=args.app_slug,
            )
            from review_sensei.disposition import command_exit_code

            return command_exit_code(result)
        for flag, value in (
            ("--repository-id", getattr(args, "repository_id", None)),
            ("--source-comment-id", getattr(args, "source_comment_id", None)),
            ("--session-attestation", getattr(args, "session_attestation", None)),
            ("--oidc-token", getattr(args, "oidc_token", None)),
        ):
            if value is not None:
                raise ReviewInputError(
                    f"{flag} requires --github-session-ledger for maintainer commands"
                )
        ledger = resolve_local_session_ledger(getattr(args, "session_ledger", None))
        if ledger is None:
            raise ReviewInputError("maintainer commands require a session ledger")
        session_identity = SessionIdentity(
            repository=args.repository, pull_request=args.pull_request
        )
        local_configuration = load_configuration(getattr(args, "config", None))
        local_explicit_mode = getattr(args, "review_mode", None)
        if (
            local_explicit_mode is None
            and not os.environ.get("REVIEWSENSEI_REVIEW_MODE", "").strip()
            and local_configuration.github.reviews == "advisory"
        ):
            local_explicit_mode = "advisory"
        local_convergence_policy = resolve_review_convergence_policy(
            mode=local_explicit_mode
        )
        from review_sensei.disposition import incomplete_coverage_from_result

        local_incomplete_coverage: tuple[str, ...] = ()
        loader = getattr(ledger, "load_retained_checkpoint_result", None)
        if callable(loader):
            try:
                loaded_checkpoint = loader(session_identity, head_sha=parsed.head_sha)
                local_incomplete_coverage = incomplete_coverage_from_result(
                    loaded_checkpoint
                )
            except Exception:
                local_incomplete_coverage = ()
        local_assoc = (
            args.association.strip().upper()
            if isinstance(args.association, str)
            else ""
        )
        local_actor_role = (
            None
            if parsed.action in {"continue", "reenroll"}
            and local_assoc == "COLLABORATOR"
            else (
                local_assoc
                if local_assoc in {"OWNER", "MEMBER", "COLLABORATOR"}
                else None
            )
        )
        _record, command_result = apply_session_command(
            ledger,
            session_identity,
            parsed,
            actor_role=local_actor_role,
            policy_digest=local_convergence_policy.digest(),
            incomplete_coverage=local_incomplete_coverage,
        )
        print(command_result.summary)
        from review_sensei.disposition import command_exit_code

        return command_exit_code(command_result)

    if not args.allow_write:
        raise ReviewInputError("github writes require --allow-write")
    broker = BrokerClient()
    http = GitHubHttp()
    session_ledger = None
    if args.command in {"review", "publish"}:
        if (
            getattr(args, "session_ledger", None) is not None
            or getattr(args, "github_session_ledger", False)
        ) and (
            not getattr(args, "repository", None)
            or getattr(args, "pull_request", None) is None
        ):
            raise ReviewInputError(
                "session ledger requires --repository and --pull-request"
            )
        session_ledger = resolve_local_session_ledger(
            getattr(args, "session_ledger", None)
        )
    application = GitHubApplication(
        broker=broker,
        http=http,
        reviewer=ReviewPublisher(http=http),
        learner=LearningPRPublisher(http=http),
        replier=ConversationPublisher(http=http),
        session_ledger=session_ledger,
    )
    if args.command in {"review", "publish"}:
        diff = read_bounded_utf8(args.diff, maximum=1_048_576, label="diff")
        if args.recover_from and (
            args.reconstruct_contexts or args.trusted_context_root is not None
        ):
            raise ReviewInputError(
                "legacy artifact recovery does not reconstruct contexts; use --result or --recover-retained"
            )
        if args.enable_review and (not args.base_branch or not args.base_sha):
            raise ReviewInputError(
                "review publication requires --base-branch and --base-sha"
            )
        identity = {
            "repository": args.repository,
            "pull_request_number": args.pull_request,
            "base_sha": args.base_sha,
            "head_sha": args.head_sha,
        }
        if (
            args.recover_from or args.recover_retained or args.reconstruct_contexts
        ) and args.enable_learning_prs:
            outcome = RunOutcome(
                "publication_failed",
                diagnostic="recovery_learning_prs_refused",
                **identity,
            )
            emit_host_outcome(outcome, output_path=args.outcome)
            print(
                "review-sensei: publication recovery cannot write learning pull requests",
                file=sys.stderr,
            )
            print(outcome.status)
            return run_outcome_exit_code(outcome.status)
        explicit_mode = getattr(args, "review_mode", None)
        if args.recover_from:
            if (
                explicit_mode is not None
                and resolve_review_mode(explicit_mode) in OPERATOR_REVIEW_MODES
            ):
                convergence_policy = resolve_review_convergence_policy(
                    mode=explicit_mode
                )
            else:
                convergence_policy = ReviewConvergencePolicy()
        else:
            convergence_policy = resolve_review_convergence_policy(mode=explicit_mode)
        if args.recover_from:
            if any(
                value is not None
                for value in (
                    args.config,
                    args.publication_inventory,
                    args.operation_entry,
                )
            ):
                raise ReviewInputError(
                    "legacy --recover-from does not accept publication profile settings; "
                    "use --result or identity-bound --recover-retained"
                )
            try:
                artifact = load_recovery_artifact(args.recover_from)
                artifact.validate(
                    repository=args.repository,
                    pull_request_number=args.pull_request,
                    base_sha=args.base_sha,
                    head_sha=args.head_sha,
                )
                review_publication = application.recover_review(
                    options=GitHubWriteOptions(
                        github_writes=True,
                        auto_review=args.enable_review,
                        auto_approve=args.enable_auto_approve,
                        reviews_policy=getattr(args, "reviews_policy", None),
                    ),
                    oidc_token=args.oidc_token,
                    repository=args.repository,
                    repository_id=args.repository_id,
                    pull_request=args.pull_request,
                    head_sha=args.head_sha,
                    base_branch=args.base_branch,
                    base_sha=args.base_sha,
                    artifact=artifact,
                    diff=diff,
                    app_slug=args.app_slug,
                    convergence_policy=convergence_policy,
                )
            except (GitHubPublicationTransientError, GitHubPublicationError) as exc:
                diagnostic = (
                    "publication_ambiguous"
                    if isinstance(exc, GitHubPublicationTransientError)
                    else "publication_failed"
                )
                outcome = RunOutcome(
                    "publication_failed",
                    diagnostic=diagnostic,
                    **identity,
                )
                emit_host_outcome(outcome, output_path=args.outcome)
                print(f"review-sensei: {exc}", file=sys.stderr)
                print(outcome.status)
                return run_outcome_exit_code(outcome.status)
            except ReviewInputError as exc:
                outcome = RunOutcome(
                    "publication_failed",
                    diagnostic=diagnostic_for_recovery_error(exc),
                    **identity,
                )
                emit_host_outcome(outcome, output_path=args.outcome)
                print(f"review-sensei: {exc}", file=sys.stderr)
                print(outcome.status)
                return run_outcome_exit_code(outcome.status)
            outcome = outcome_from_publication(
                review_publication,
                review_result=ReviewResult.from_dict(artifact.result),
                **identity,
            )
            emit_host_outcome(outcome, output_path=args.outcome)
            print(review_publication.status)
            return run_outcome_exit_code(outcome.status)
        if args.result is None and not args.recover_retained:
            outcome = RunOutcome(
                "publication_failed",
                diagnostic="publication_failed",
                **identity,
            )
            emit_host_outcome(outcome, output_path=args.outcome)
            print(
                "review-sensei: review publication requires --result, --recover-from or --recover-retained",
                file=sys.stderr,
            )
            print(outcome.status)
            return run_outcome_exit_code(outcome.status)
        publication_configuration = load_configuration(args.config)
        publication_inventory = (
            args.publication_inventory
            or publication_configuration.github.publication_inventory
        )
        operation_entry = (
            args.operation_entry or publication_configuration.github.operation_entry
        )
        from review_sensei.bounded_evidence import EvidenceReadBudget

        publication_budget = EvidenceReadBudget()
        configuration_context = (
            _load_context_document(
                args.configuration_context,
                label="configuration context",
            )
            if args.configuration_context is not None
            else None
        )
        evidence_context = (
            _load_context_document(args.evidence_context, label="evidence context")
            if args.evidence_context is not None
            else None
        )
        admission_baseline = None
        admission_current_key = None
        if args.trusted_context_root is not None and not args.reconstruct_contexts:
            raise ReviewInputError(
                "trusted context root requires --reconstruct-contexts"
            )
        if args.reconstruct_contexts and (
            args.trusted_context_root is None
            or any(
                value is not None
                for value in (
                    args.configuration_context,
                    args.evidence_context,
                    args.admission_context,
                )
            )
        ):
            raise ReviewInputError(
                "context reconstruction requires original trusted source and cannot replace supplied context documents"
            )
        if args.recover_retained or args.reconstruct_contexts:
            if not args.github_session_ledger and operation_entry != "enabled":
                raise ReviewInputError(
                    "retained result recovery requires operation_entry: enabled"
                )
            if not args.enable_review or not args.base_sha:
                raise ReviewInputError(
                    "retained result recovery requires review publication and exact base SHA"
                )
            metadata_token = os.environ.get("GITHUB_TOKEN") or os.environ.get(
                "GH_TOKEN"
            )
            if not isinstance(metadata_token, str) or not metadata_token.strip():
                raise ReviewInputError(
                    "retained result recovery requires a metadata token"
                )
            recovery_ledger = session_ledger
            if recovery_ledger is None and args.github_session_ledger:
                from review_sensei_github.session_ledger import (
                    GitHubIssueCommentSessionLedger,
                )

                recovery_ledger = GitHubIssueCommentSessionLedger(
                    http,
                    token=metadata_token.strip(),
                    app_slug=args.app_slug,
                    evidence_budget=publication_budget,
                )
            if recovery_ledger is None:
                raise ReviewInputError(
                    "retained result recovery requires a session ledger"
                )
            from contextlib import nullcontext

            from review_sensei.session import SessionIdentity
            from review_sensei_github.session_ledger import (
                GitHubIssueCommentSessionLedger,
            )

            recovery_read: Any = nullcontext(None)
            if (
                args.reconstruct_contexts
                and args.recover_retained
                and isinstance(recovery_ledger, GitHubIssueCommentSessionLedger)
            ):
                from review_sensei_github.publication_recovery import (
                    publication_recovery_read_phase,
                )

                recovery_read = publication_recovery_read_phase(
                    http=http,
                    token=metadata_token.strip(),
                    ledger=recovery_ledger,
                    identity=SessionIdentity(
                        args.repository,
                        args.pull_request,
                        repository_id=args.repository_id,
                    ),
                    base_sha=args.base_sha,
                    head_sha=args.head_sha,
                    app_slug=args.app_slug,
                    budget=publication_budget,
                )
            with recovery_read as recovery_phase:
                if recovery_phase is not None:
                    recovery_ledger = recovery_phase.ledger
                if args.reconstruct_contexts:
                    from review_sensei_github.publication_context_recovery import (
                        reconstruct_publication_contexts,
                    )
                    from review_sensei_github.publication_recovery import (
                        publication_session_ledger,
                    )

                    reconstructed = reconstruct_publication_contexts(
                        source_root=args.trusted_context_root,
                        diff=diff,
                        ledger=(
                            recovery_phase.ledger
                            if recovery_phase is not None
                            else publication_session_ledger(
                                recovery_ledger, publication_budget
                            )
                        ),
                        identity=SessionIdentity(
                            args.repository,
                            args.pull_request,
                            repository_id=args.repository_id,
                        ),
                        base_sha=args.base_sha,
                        head_sha=args.head_sha,
                        policy=convergence_policy,
                    )
                    configuration_context = reconstructed.configuration
                    evidence_context = reconstructed.evidence
                    admission_baseline = reconstructed.baseline
                    admission_current_key = reconstructed.current_key
                if configuration_context is None or evidence_context is None:
                    raise ReviewInputError(
                        "retained result recovery requires trusted configuration and evidence contexts"
                    )
                if args.recover_retained:
                    recovery_options = (
                        {"recovery_phase": recovery_phase}
                        if recovery_phase is not None
                        else {}
                    )
                    result = application.recover_retained_review_result(
                        token=metadata_token.strip(),
                        repository=args.repository,
                        repository_id=args.repository_id,
                        pull_request=args.pull_request,
                        base_sha=args.base_sha,
                        head_sha=args.head_sha,
                        app_slug=args.app_slug,
                        ledger=recovery_ledger,
                        configuration_context=configuration_context,
                        evidence_context=evidence_context,
                        convergence_policy=convergence_policy,
                        budget=publication_budget,
                        **recovery_options,
                    )
        if not args.recover_retained:
            result = ReviewResult.from_dict(
                json.loads(
                    read_bounded_utf8(args.result, maximum=2_097_152, label="result")
                )
            )
        if args.admission_context is not None:
            if configuration_context is None:
                # The identity-bound transaction is what makes the admission
                # inputs meaningful; accepting them without it would let a
                # hosted caller bypass F3 admission with caller-supplied state.
                raise ReviewInputError(
                    "admission context requires an identity-bound configuration context"
                )
            try:
                admission_baseline, admission_current_key = (
                    admission_context_from_document(
                        _load_context_document(
                            args.admission_context, label="admission context"
                        )
                    )
                )
            except ReviewInputError as exc:
                raise ReviewInputError(f"admission context is invalid: {exc}") from exc
        acceptance_checkpoint = None
        acceptance_http = None
        if os.environ.get("GITHUB_RUN_ID", "").strip() and operation_entry == "enabled":
            from review_sensei_github.publication_recovery import publication_http

            acceptance_http = publication_http(http, publication_budget)
            if result.transaction is not None and configuration_context is not None:
                from review_sensei.models import ReviewTransaction
                from review_sensei.session import SessionIdentity

                acceptance_ledger = session_ledger
                if args.recover_retained or args.reconstruct_contexts:
                    acceptance_ledger = recovery_ledger
                if acceptance_ledger is None and args.github_session_ledger:
                    from review_sensei_github.publication_recovery import (
                        publication_session_ledger,
                    )
                    from review_sensei_github.session_ledger import (
                        GitHubIssueCommentSessionLedger,
                    )

                    read_token = os.environ.get("GITHUB_TOKEN") or os.environ.get(
                        "GH_TOKEN"
                    )
                    if not isinstance(read_token, str) or not read_token.strip():
                        raise ReviewInputError("operation record is unavailable")
                    acceptance_ledger = publication_session_ledger(
                        GitHubIssueCommentSessionLedger(
                            http,
                            token=read_token.strip(),
                            app_slug=args.app_slug,
                            evidence_budget=publication_budget,
                        ),
                        publication_budget,
                    )
                if acceptance_ledger is not None:
                    from review_sensei_github.publication_recovery import (
                        publication_session_ledger,
                    )

                    acceptance_ledger = publication_session_ledger(
                        acceptance_ledger, publication_budget
                    )
                    acceptance_checkpoint = dict(
                        ledger=acceptance_ledger,
                        identity=SessionIdentity(
                            repository=args.repository,
                            pull_request=args.pull_request,
                            repository_id=args.repository_id,
                        ),
                        base_sha=args.base_sha,
                        head_sha=args.head_sha,
                        policy_digest=convergence_policy.digest(),
                        configuration_digest=ReviewTransaction.compute_configuration_digest(
                            configuration_context
                        ),
                        evidence_digest=ReviewTransaction.compute_evidence_digest(
                            evidence_context
                            if evidence_context is not None
                            else {"evidence_policy": "legacy", "snapshot_sha256": None}
                        ),
                    )
        accepted_comments = _require_accepted_result(
            result,
            repository=args.repository,
            pull_request=args.pull_request,
            app_slug=args.app_slug,
            operation_entry=operation_entry,
            publication_http=acceptance_http,
            checkpoint=acceptance_checkpoint,
        )
        primed_comments_path: str | None = None
        if (
            accepted_comments is not None
            and getattr(args, "github_session_ledger", False)
            and session_ledger is None
        ):
            comments_path = http.repository_path(
                args.repository, f"/issues/{args.pull_request}/comments"
            )
            if comments_path not in http.primed_issue_comments:
                http.primed_issue_comments[comments_path] = list(accepted_comments)
                primed_comments_path = comments_path
        try:
            review_outcome = application.publish_review(
                options=GitHubWriteOptions(
                    github_writes=True,
                    auto_review=args.enable_review,
                    auto_approve=args.enable_auto_approve,
                    reviews_policy=getattr(args, "reviews_policy", None),
                    github_session_ledger=getattr(args, "github_session_ledger", False),
                    complete_inventory=publication_inventory == "complete",
                    operation_entry=operation_entry,
                ),
                oidc_token=args.oidc_token,
                repository=args.repository,
                repository_id=args.repository_id,
                pull_request=args.pull_request,
                head_sha=args.head_sha,
                base_branch=args.base_branch,
                base_sha=args.base_sha,
                result=result,
                diff=diff,
                app_slug=args.app_slug,
                convergence_policy=convergence_policy,
                configuration_context=configuration_context,
                evidence_context=evidence_context,
                baseline=admission_baseline,
                current_key=admission_current_key,
                publication_budget=publication_budget,
            )
        except GitHubPublicationTransientError as exc:
            outcome = RunOutcome(
                "publication_failed",
                diagnostic="publication_ambiguous",
                **identity,
            )
            emit_host_outcome(outcome, output_path=args.outcome)
            print(f"review-sensei: {exc}", file=sys.stderr)
            print(outcome.status)
            return run_outcome_exit_code(outcome.status)
        except GitHubPublicationError as exc:
            outcome = RunOutcome(
                "publication_failed",
                diagnostic="publication_failed",
                **identity,
            )
            emit_host_outcome(outcome, output_path=args.outcome)
            print(f"review-sensei: {exc}", file=sys.stderr)
            print(outcome.status)
            return run_outcome_exit_code(outcome.status)
        finally:
            if primed_comments_path is not None:
                http.primed_issue_comments.pop(primed_comments_path, None)
        learning_outcomes: tuple[LearningPRResult, ...] = ()
        review_allows_learning = not args.enable_review or review_outcome.status in {
            "published",
            "already_published",
        }
        if args.enable_learning_prs and review_allows_learning:
            if not args.base_branch or not args.base_sha:
                raise ReviewInputError(
                    "learning PR publication requires --base-branch and --base-sha"
                )
            learning_outcomes = application.publish_learning_proposals(
                options=GitHubWriteOptions(
                    github_writes=True,
                    learning_prs=True,
                ),
                oidc_token=args.oidc_token,
                repository=args.repository,
                repository_id=args.repository_id,
                pull_request=args.pull_request,
                head_sha=args.head_sha,
                base_branch=args.base_branch,
                base_sha=args.base_sha,
                result=result,
            )
        outcome = outcome_from_publication(
            review_outcome, review_result=result, **identity
        )
        emit_host_outcome(outcome, output_path=args.outcome)
        if getattr(review_outcome, "thread_resolution", None) == "unconfirmed":
            print(
                "review-sensei: old-thread cleanup is unconfirmed or incomplete; "
                "use --result with the exact original result and trusted contexts "
                "for this head and base to continue cleanup; "
                "no inference is needed.",
                file=sys.stderr,
            )
        statuses = [review_outcome.status]
        statuses.extend(item.status for item in learning_outcomes)
        print(" ".join(statuses))
        return run_outcome_exit_code(outcome.status)
    if args.generate:
        if not args.enable_reply:
            print("disabled")
            return 0
        read_token = os.getenv(args.github_token_env)
        if not read_token:
            raise ReviewInputError(
                f"GitHub read token environment variable {args.github_token_env} is unavailable"
            )
        _validate_live_profile_gates(args, argv)
        provider_settings, _ = resolve_review_inference(args, argv)
        provider = default_registry().create(provider_settings)
        work_configuration = load_configuration(getattr(args, "config", None))
        work_recovery = _work_recovery_from_cli(args, work_configuration)
        broader_service = None
        if getattr(args, "enable_broader_review", False):
            if (
                work_configuration.advanced.review_work.mode != "unified"
                or not work_configuration.github.automatic_reviews
                or not work_configuration.github.writes
            ):
                raise ReviewInputError(
                    "broader review requires unified mode and trusted full-review/write policy"
                )
            import review_sensei
            from review_sensei.stages import (
                category_catalog_for_configured_stages,
                load_stages_from_dir,
            )

            custom = customization_directories(work_configuration)
            stages = load_stages_from_dir(
                custom.stages or Path(review_sensei.__file__).parent / "default_stages",
                category_catalog=category_catalog_for_configured_stages(
                    custom.categories
                ),
            )
            full_provider, stage_providers = bind_stage_providers(
                registry=default_registry(), settings=provider_settings, stages=stages
            )
            broader_service = ReviewService(
                full_provider,
                stages=stages,
                stage_providers=stage_providers,
                work_budgets=work_configuration.advanced.review_work,
            )
        reply_budget = _configured_resource_budget(
            ResourceBudget.create(), work_configuration
        )
        reply_outcome = application.generate_and_publish_reply(
            options=GitHubWriteOptions(
                github_writes=True,
                mention_replies=True,
                auto_review=broader_service is not None,
                reviews_policy=work_configuration.github.reviews,
                upload_artifacts=work_configuration.github.artifacts == "diagnostics",
            ),
            oidc_token=args.oidc_token,
            read_token=read_token,
            repository=args.repository,
            pull_request=args.pull_request,
            source_comment_id=args.source_comment_id,
            source_updated_at=args.source_updated_at,
            expected_head_sha=args.head_sha or None,
            reply_provider=provider,
            model=args.model,
            app_slug=args.app_slug,
            root_comment_id=args.root_comment_id or None,
            source_kind=args.source_kind,
            work_budgets=work_configuration.advanced.review_work,
            budget=reply_budget,
            work_recovery=work_recovery,
            broader_service=broader_service,
            operation_entry=work_configuration.github.operation_entry,
            repository_id=getattr(args, "repository_id", None),
            app_id=_actions_app_id(),
            run_id=os.environ.get("GITHUB_RUN_ID", "").strip() or None,
            run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT", "1").strip() or "1",
        )
        print(reply_outcome.status)
        assessment_status = getattr(reply_outcome, "assessment_status", None)
        if assessment_status is not None:
            assessment_diagnostic = getattr(
                reply_outcome, "assessment_diagnostic", None
            )
            print(
                f"review-sensei: assessment={assessment_status}"
                + (
                    f" diagnostic={assessment_diagnostic}"
                    if assessment_diagnostic
                    else ""
                ),
                file=sys.stderr,
            )
        resolution_status = getattr(reply_outcome, "resolution_status", None)
        if resolution_status is not None:
            print(
                f"review-sensei: thread_resolution={resolution_status}; comment @reviewsensei verify to retry",
                file=sys.stderr,
            )
        approval_status = getattr(reply_outcome, "approval_status", None)
        if approval_status is not None:
            reply_approval_diagnostic = getattr(
                reply_outcome, "approval_diagnostic", None
            )
            print(
                f"review-sensei: approval={approval_status}"
                + (
                    f" diagnostic={reply_approval_diagnostic}"
                    if reply_approval_diagnostic
                    else ""
                ),
                file=sys.stderr,
            )
        return 0
    if args.reply is None or not args.head_sha:
        raise ReviewInputError(
            "publishing a reply requires --reply and --head-sha unless --generate is used"
        )
    reply = ConversationReply.from_dict(
        json.loads(read_bounded_utf8(args.reply, maximum=16 * 1024, label="reply"))
    )
    reply_outcome = application.publish_reply(
        options=GitHubWriteOptions(
            github_writes=True,
            mention_replies=args.enable_reply,
        ),
        oidc_token=args.oidc_token,
        repository=args.repository,
        pull_request=args.pull_request,
        source_comment_id=args.source_comment_id,
        source_updated_at=args.source_updated_at,
        head_sha=args.head_sha,
        reply=reply,
        app_slug=args.app_slug,
        root_comment_id=args.root_comment_id,
        source_kind=args.source_kind,
    )
    print(reply_outcome.status)
    return 0


def _run_host_plan_command(arguments: list[str]) -> int:
    args = _host_plan_parser().parse_args(arguments)
    try:
        from review_sensei.hosted import (
            hosted_plan_outputs,
            load_and_plan_hosted,
            render_hosted_plan,
            write_github_outputs,
        )

        plan = load_and_plan_hosted(
            args.config,
            cli_provider=args.provider,
            cli_model=args.model,
        )
        if args.github_output is not None:
            write_github_outputs(args.github_output, hosted_plan_outputs(plan))
        sys.stdout.write(render_hosted_plan(plan, as_json=args.as_json))
        return 0
    except (OSError, ValueError, TypeError, ReviewSenseiError) as exc:
        _print_offline_error(exc)
        return 2


def _report_broker_failure(error: BaseException, args: argparse.Namespace) -> bool:
    """Supplement broker failures without changing exits or wrapper semantics."""
    from review_sensei.broker_diagnostics import exception_broker_diagnostic
    from review_sensei_github.errors import GitHubHTTPTransientError

    diagnostic = exception_broker_diagnostic(error)
    if diagnostic is None:
        return False
    transient = isinstance(error, GitHubHTTPTransientError)
    token = "broker_temporarily_unavailable" if transient else "broker_rejected"
    print(f"review-sensei: {diagnostic.message()}", file=sys.stderr)
    print(f"review-sensei: reason={token}", file=sys.stderr)
    try:
        emit_host_outcome(
            RunOutcome(
                status="action_required",
                diagnostic=token,
                stage_summary=diagnostic.metadata(),
            ),
            output_path=getattr(args, "outcome", None),
        )
    except (OSError, ValueError, ReviewSenseiError):
        # Optional output must never replace the original failure or its exit.
        print("review-sensei: broker diagnostic output unavailable", file=sys.stderr)
    return True


def _actions_app_id() -> int | None:
    """GitHub App id for an Actions run, when the event or env provides one."""

    raw = os.environ.get("REVIEWSENSEI_APP_ID", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    event_path = os.environ.get("GITHUB_EVENT_PATH", "").strip()
    if not event_path:
        return None
    try:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    installation = event.get("installation") if isinstance(event, dict) else None
    app_id = installation.get("app_id") if isinstance(installation, dict) else None
    if isinstance(app_id, int) and not isinstance(app_id, bool) and app_id > 0:
        return app_id
    return None


def _run_review_provider(
    service: object, request: object, **kwargs: object
) -> ReviewRun:
    """Call the provider, or replay an accepted Actions attempt.

    The operation entry runs only for a hosted session ledger when the policy
    is enabled and this process has an Actions run id and App id. Every other
    caller keeps the existing provider call.
    """

    hosted = kwargs.pop("hosted_session_ledger", False)
    operation_entry = kwargs.pop("operation_entry", "disabled")
    ledger = kwargs.pop("ledger", None)
    repository = kwargs.pop("repository", None)
    repository_id = kwargs.pop("repository_id", None)
    pull_request = kwargs.pop("pull_request", None)
    analysis_reservation = kwargs.pop("analysis_reservation_id", None)
    if analysis_reservation is not None and (
        not isinstance(analysis_reservation, str) or not analysis_reservation.strip()
    ):
        raise ReviewInputError("analysis reservation id must be a non-empty string")
    adjust = kwargs.pop("adjust", None)
    run_id = os.environ.get("GITHUB_RUN_ID", "").strip()
    app_id = _actions_app_id()
    head_sha = kwargs.get("trusted_head_sha")

    def produced() -> ReviewRun:
        run = service.run(request, **kwargs)  # type: ignore[attr-defined]
        admission = getattr(run, "context_admission", None)
        if admission:
            print(
                "review-sensei: context-admission "
                + json.dumps(admission, ensure_ascii=True, sort_keys=True),
                file=sys.stderr,
            )
        if callable(adjust):
            run = adjust(run)
        if not isinstance(run, ReviewRun):
            raise ReviewInputError("review provider returned an invalid result")
        return run

    if (
        hosted is not True
        or operation_entry != "enabled"
        or ledger is None
        or not run_id
        or app_id is None
        or not isinstance(repository, str)
        or not isinstance(repository_id, int)
        or isinstance(pull_request, bool)
        or not isinstance(pull_request, int)
        or not isinstance(head_sha, str)
        or len(head_sha) != 40
    ):
        return produced()
    from review_sensei.models import ReviewResult
    from review_sensei.outcomes import RunOutcome
    from review_sensei.session import session_reservation_id
    from review_sensei_github.operation_entry import (
        HostedProviderResult,
        analysis_payload,
        run_actions_analysis,
    )
    from review_sensei_github.operation_host import OperationRefusal

    reservation = analysis_reservation or session_reservation_id(
        repository=repository,
        pull_request=pull_request,
        head_sha=head_sha,
        kind="publish",
    )
    admitted = run_actions_analysis(
        http=getattr(ledger, "http"),
        token=getattr(ledger, "token"),
        repository=repository,
        repository_id=repository_id,
        pull_request=pull_request,
        run_id=run_id,
        run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT", "1").strip() or "1",
        app_id=app_id,
        reservation_id=reservation,
        attestation_digest=hashlib.sha256(
            f"{repository_id}:{pull_request}:{head_sha}".encode()
        ).hexdigest(),
        execute=produced,
        encode=analysis_payload,
    )
    if isinstance(admitted, OperationRefusal):
        raise ReviewInputError(f"operation entry refused: {admitted.reason}")
    if not isinstance(admitted, HostedProviderResult):
        raise ReviewInputError("operation entry refused: invalid")
    if admitted.fresh is not None:
        if not isinstance(admitted.fresh, ReviewRun):
            raise ReviewInputError("operation entry refused: invalid")
        return admitted.fresh
    if admitted.replay_payload is None:
        raise ReviewInputError("operation entry refused: result_missing")
    result = ReviewResult.from_dict(json.loads(admitted.replay_payload))
    status, diagnostic = service._run_outcome_for_result(result)  # type: ignore[attr-defined]
    return ReviewRun(
        outcome=RunOutcome(status, diagnostic=diagnostic),
        result=result,
    )


run_hosted_provider = _run_review_provider
report_broker_failure = _report_broker_failure
run_github = _run_github
run_host_plan = _run_host_plan_command
resolve_hosted_openrouter_parser = _resolve_hosted_openrouter_parser
