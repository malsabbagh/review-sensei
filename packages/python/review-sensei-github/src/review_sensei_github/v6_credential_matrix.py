"""ReviewSensei v6 dual-credential endpoint authorization matrix.

Codifies the split between read-only ``GITHUB_TOKEN`` bulk scans and the
stateless ReviewSensei App broker (mutations plus a strictly enumerated
5-item ``O(1)`` authority-verification read allowlist).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from review_sensei.errors import ReviewInputError

from .errors import GitHubHTTPPermissionError


class CredentialKind(str, Enum):
    """Credential channel allowed to execute a GitHub API operation."""

    WORKFLOW_GITHUB_TOKEN = "workflow_github_token"
    APP_BROKER = "app_broker"


class GitHubEventKind(str, Enum):
    """GitHub Actions trigger events admitted by the v6 matrix."""

    PULL_REQUEST = "pull_request"
    ISSUE_COMMENT = "issue_comment"
    PULL_REQUEST_REVIEW_COMMENT = "pull_request_review_comment"
    WORKFLOW_DISPATCH = "workflow_dispatch"


class RepositoryVisibility(str, Enum):
    """Repository and pull-request head trust context."""

    PUBLIC = "public"
    PRIVATE = "private"
    FORK_PULL_REQUEST = "fork_pull_request"


class OperationKind(str, Enum):
    """Architectural class of a GitHub endpoint interaction."""

    BULK_READ = "bulk_read"
    BROKER_AUTHORITY_READ = "broker_authority_read"
    MUTATION = "mutation"


class V6CredentialPermissionError(GitHubHTTPPermissionError):
    """Raised when a workflow token lacks a required read-only scope (HTTP 403)."""

    error_category = "github_http_permission"

    def __init__(
        self,
        message: str,
        *,
        endpoint_id: str,
        credential: CredentialKind,
        missing_scopes: tuple[str, ...],
    ) -> None:
        missing_scope = ", ".join(missing_scopes) if missing_scopes else None
        super().__init__(
            message,
            http_status=403,
            failed_lookup=endpoint_id,
            endpoint_id=endpoint_id,
            credential_kind=credential.value,
            missing_scope=missing_scope,
        )
        self.credential = credential
        self.missing_scopes = missing_scopes


_ALLOWED_METHODS = frozenset({"GET", "POST", "PATCH", "PUT", "DELETE"})

WORKFLOW_READ_ONLY_SCOPES: frozenset[str] = frozenset(
    {
        "contents: read",
        "pull-requests: read",
        "issues: read",
        "checks: read",
        "actions: read",
    }
)

ALL_EVENTS: frozenset[GitHubEventKind] = frozenset(GitHubEventKind)
ALL_VISIBILITIES: frozenset[RepositoryVisibility] = frozenset(RepositoryVisibility)
SAME_REPO_VISIBILITIES: frozenset[RepositoryVisibility] = frozenset(
    {RepositoryVisibility.PUBLIC, RepositoryVisibility.PRIVATE}
)
MAINTAINER_COMMAND_EVENTS: frozenset[GitHubEventKind] = frozenset(
    {GitHubEventKind.ISSUE_COMMENT, GitHubEventKind.WORKFLOW_DISPATCH}
)

BROKER_APP_READ_ALLOWLIST: tuple[str, ...] = (
    "broker_app_identity",
    "broker_pr_binding_fence",
    "broker_collaborator_permission",
    "broker_active_writer_fence",
    "broker_effect_verification",
)
_BROKER_APP_READ_ALLOWLIST_SET = frozenset(BROKER_APP_READ_ALLOWLIST)


@dataclass(frozen=True)
class EndpointCredentialRule:
    """Immutable authorization policy for a single GitHub endpoint surface."""

    endpoint_id: str
    surface: str
    method: str
    operation_kind: OperationKind
    credential_kind: CredentialKind
    required_permissions: tuple[str, ...]
    supported_events: frozenset[GitHubEventKind]
    supported_visibilities: frozenset[RepositoryVisibility]
    allow_app_fallback: bool = False
    max_items_per_page: int | None = None
    rationale: str = ""

    def __post_init__(self) -> None:
        if (
            not isinstance(self.endpoint_id, str)
            or not self.endpoint_id
            or self.endpoint_id.strip() != self.endpoint_id
        ):
            raise ValueError(f"invalid endpoint_id: {self.endpoint_id!r}")
        if (
            not isinstance(self.surface, str)
            or not self.surface
            or self.surface.strip() != self.surface
        ):
            raise ValueError(
                f"invalid surface for {self.endpoint_id}: {self.surface!r}"
            )
        if self.method not in _ALLOWED_METHODS:
            raise ValueError(
                f"invalid HTTP method for {self.endpoint_id}: {self.method!r}"
            )
        if not isinstance(self.operation_kind, OperationKind):
            raise ValueError(f"invalid operation_kind: {self.operation_kind!r}")
        if not isinstance(self.credential_kind, CredentialKind):
            raise ValueError(f"invalid credential_kind: {self.credential_kind!r}")
        if not self.supported_events or not all(
            isinstance(item, GitHubEventKind) for item in self.supported_events
        ):
            raise ValueError(
                f"{self.endpoint_id} must declare at least one GitHubEventKind"
            )
        if not self.supported_visibilities or not all(
            isinstance(item, RepositoryVisibility)
            for item in self.supported_visibilities
        ):
            raise ValueError(
                f"{self.endpoint_id} must declare at least one RepositoryVisibility"
            )
        if self.max_items_per_page is not None and (
            isinstance(self.max_items_per_page, bool) or self.max_items_per_page <= 0
        ):
            raise ValueError(
                f"{self.endpoint_id} max_items_per_page must be positive or None"
            )
        if self.allow_app_fallback:
            raise ValueError(
                f"{self.endpoint_id} cannot enable allow_app_fallback in v6"
            )
        if any(
            not isinstance(scope, str) or not scope or scope.strip() != scope
            for scope in self.required_permissions
        ):
            raise ValueError(
                f"{self.endpoint_id} contains an invalid permission scope: "
                f"{self.required_permissions!r}"
            )
        if "contents: write" in self.required_permissions:
            raise ValueError(f"{self.endpoint_id} must never require 'contents: write'")
        if self.operation_kind == OperationKind.BULK_READ:
            if self.credential_kind != CredentialKind.WORKFLOW_GITHUB_TOKEN:
                raise ValueError(
                    f"bulk read {self.endpoint_id} must use workflow_github_token"
                )
            if not self.required_permissions:
                raise ValueError(
                    f"bulk read {self.endpoint_id} must declare read-only scopes"
                )
            invalid_scopes = set(self.required_permissions) - WORKFLOW_READ_ONLY_SCOPES
            if invalid_scopes:
                raise ValueError(
                    f"bulk read {self.endpoint_id} requires non-read-only scopes: "
                    f"{sorted(invalid_scopes)}"
                )
        else:
            if self.credential_kind != CredentialKind.APP_BROKER:
                raise ValueError(
                    f"{self.operation_kind.value} {self.endpoint_id} must use app_broker"
                )
            if (
                self.operation_kind == OperationKind.BROKER_AUTHORITY_READ
                and self.max_items_per_page is not None
            ):
                raise ValueError(
                    f"broker authority read {self.endpoint_id} must not declare "
                    "bulk pagination"
                )


def _bulk_read(
    endpoint_id: str,
    surface: str,
    *,
    method: str = "GET",
    required_permissions: tuple[str, ...],
    max_items_per_page: int | None = None,
    rationale: str,
) -> tuple[str, EndpointCredentialRule]:
    """Construct a read-only workflow ``GITHUB_TOKEN`` bulk-read rule."""

    return endpoint_id, EndpointCredentialRule(
        endpoint_id=endpoint_id,
        surface=surface,
        method=method,
        operation_kind=OperationKind.BULK_READ,
        credential_kind=CredentialKind.WORKFLOW_GITHUB_TOKEN,
        required_permissions=required_permissions,
        supported_events=ALL_EVENTS,
        supported_visibilities=ALL_VISIBILITIES,
        allow_app_fallback=False,
        max_items_per_page=max_items_per_page,
        rationale=rationale,
    )


def _broker_read(
    endpoint_id: str,
    surface: str,
    *,
    required_permissions: tuple[str, ...],
    supported_events: frozenset[GitHubEventKind] = ALL_EVENTS,
    supported_visibilities: frozenset[RepositoryVisibility] = SAME_REPO_VISIBILITIES,
    rationale: str,
) -> tuple[str, EndpointCredentialRule]:
    """Construct an O(1) broker App-read allowlist rule."""

    return endpoint_id, EndpointCredentialRule(
        endpoint_id=endpoint_id,
        surface=surface,
        method="GET",
        operation_kind=OperationKind.BROKER_AUTHORITY_READ,
        credential_kind=CredentialKind.APP_BROKER,
        required_permissions=required_permissions,
        supported_events=supported_events,
        supported_visibilities=supported_visibilities,
        allow_app_fallback=False,
        max_items_per_page=None,
        rationale=rationale,
    )


def _broker_mutation(
    endpoint_id: str,
    surface: str,
    *,
    method: str = "POST",
    required_permissions: tuple[str, ...],
    rationale: str,
    supported_events: frozenset[GitHubEventKind] = ALL_EVENTS
    - {GitHubEventKind.PULL_REQUEST_REVIEW_COMMENT},
) -> tuple[str, EndpointCredentialRule]:
    """Construct an App-broker mutation rule."""

    return endpoint_id, EndpointCredentialRule(
        endpoint_id=endpoint_id,
        surface=surface,
        method=method,
        operation_kind=OperationKind.MUTATION,
        credential_kind=CredentialKind.APP_BROKER,
        required_permissions=required_permissions,
        supported_events=supported_events,
        supported_visibilities=SAME_REPO_VISIBILITIES,
        allow_app_fallback=False,
        max_items_per_page=None,
        rationale=rationale,
    )


_MATRIX_ENTRIES: tuple[tuple[str, EndpointCredentialRule], ...] = (
    # Bulk reads: exclusively workflow GITHUB_TOKEN, zero App fallback.
    _bulk_read(
        "pr_metadata",
        "/repos/{owner}/{repo}/pulls/{pull_number}",
        required_permissions=("pull-requests: read", "contents: read"),
        rationale=(
            "Fetch PR head/base/state metadata via workflow GITHUB_TOKEN without "
            "spending App quota."
        ),
    ),
    _bulk_read(
        "pr_commits",
        "/repos/{owner}/{repo}/pulls/{pull_number}/commits",
        required_permissions=("pull-requests: read", "contents: read"),
        max_items_per_page=100,
        rationale="Paginate PR commit history via workflow GITHUB_TOKEN.",
    ),
    _bulk_read(
        "pr_diff",
        "/repos/{owner}/{repo}/pulls/{pull_number}",
        required_permissions=("pull-requests: read", "contents: read"),
        rationale=(
            "Fetch PR diff with diff media type via workflow GITHUB_TOKEN "
            "without burning App installation quota."
        ),
    ),
    _bulk_read(
        "pr_compare",
        "/repos/{owner}/{repo}/compare/{base}...{head}",
        required_permissions=("pull-requests: read", "contents: read"),
        rationale=(
            "Fetch immutable base...head compare summary or diff via workflow "
            "GITHUB_TOKEN without burning App installation quota."
        ),
    ),
    _bulk_read(
        "git_tree",
        "/repos/{owner}/{repo}/git/trees/{tree_sha}",
        required_permissions=("contents: read",),
        rationale=(
            "Fetch immutable commit tree snapshots via workflow GITHUB_TOKEN "
            "for complete oversized PR inventory and diff splitting."
        ),
    ),
    _bulk_read(
        "git_blob",
        "/repos/{owner}/{repo}/git/blobs/{file_sha}",
        required_permissions=("contents: read",),
        rationale=(
            "Fetch immutable Git blob objects via workflow GITHUB_TOKEN "
            "when splitting oversized PR diffs deterministically."
        ),
    ),
    _bulk_read(
        "git_commit",
        "/repos/{owner}/{repo}/git/commits/{commit_sha}",
        required_permissions=("contents: read",),
        rationale=(
            "Fetch immutable Git commit objects via workflow GITHUB_TOKEN "
            "to compute commit-graph merge base when compare responses fail or overflow."
        ),
    ),
    _bulk_read(
        "pr_files",
        "/repos/{owner}/{repo}/pulls/{pull_number}/files",
        required_permissions=("pull-requests: read", "contents: read"),
        max_items_per_page=100,
        rationale=(
            "Paginate changed PR files via workflow GITHUB_TOKEN (subject to "
            "GitHub's 3,000-file REST API cap)."
        ),
    ),
    _bulk_read(
        "pr_issue_comments",
        "/repos/{owner}/{repo}/issues/{pull_number}/comments",
        required_permissions=("issues: read", "pull-requests: read"),
        max_items_per_page=100,
        rationale=(
            "Paginate PR conversation comments at 100/page in deterministic "
            "ascending created order on GITHUB_TOKEN."
        ),
    ),
    _bulk_read(
        "pr_review_comments",
        "/repos/{owner}/{repo}/pulls/{pull_number}/comments",
        required_permissions=("pull-requests: read",),
        max_items_per_page=100,
        rationale="Paginate inline PR review comments at 100/page on GITHUB_TOKEN.",
    ),
    _bulk_read(
        "pr_reviews",
        "/repos/{owner}/{repo}/pulls/{pull_number}/reviews",
        required_permissions=("pull-requests: read",),
        max_items_per_page=100,
        rationale="Paginate PR review objects at 100/page on GITHUB_TOKEN.",
    ),
    _bulk_read(
        "graphql_review_threads",
        "/graphql",
        method="POST",
        required_permissions=("pull-requests: read",),
        max_items_per_page=100,
        rationale=(
            "Batch GraphQL reviewThreads (including originalCommit { oid }), "
            "reviews, and PR state/heads on GITHUB_TOKEN."
        ),
    ),
    _bulk_read(
        "check_runs_and_suites",
        "/repos/{owner}/{repo}/commits/{ref}/check-runs",
        required_permissions=("checks: read",),
        max_items_per_page=100,
        rationale=(
            "Inspect commit check-runs and check-suites via workflow "
            "GITHUB_TOKEN with read-only checks: read scope."
        ),
    ),
    _bulk_read(
        "workflow_runs_and_jobs",
        "/repos/{owner}/{repo}/actions/runs",
        required_permissions=("actions: read",),
        max_items_per_page=100,
        rationale=(
            "Inspect predecessor workflow runs and jobs via workflow "
            "GITHUB_TOKEN with read-only actions: read scope."
        ),
    ),
    # Narrow 5-item broker App-read allowlist: O(1) authority checks only.
    _broker_read(
        "broker_app_identity",
        "/app",
        required_permissions=(),
        rationale=(
            "Establish authoritative App/bot identity independent of caller claims."
        ),
    ),
    _broker_read(
        "broker_pr_binding_fence",
        "/repos/{owner}/{repo}/pulls/{pull_number}",
        required_permissions=("pull-requests: read",),
        rationale=(
            "Independently verify live PR state, head.sha, and base.sha "
            "immediately before applying a brokered write."
        ),
    ),
    _broker_read(
        "broker_collaborator_permission",
        "/repos/{owner}/{repo}/collaborators/{username}/permission",
        required_permissions=("metadata: read",),
        supported_events=MAINTAINER_COMMAND_EVENTS,
        supported_visibilities=ALL_VISIBILITIES,
        rationale=(
            "Verify maintainer permission on comment/dispatch commands where "
            "read-only GITHUB_TOKEN (especially on fork PRs) receives HTTP 403."
        ),
    ),
    _broker_read(
        "broker_active_writer_fence",
        "/repos/{owner}/{repo}/actions/runs/{run_id}",
        required_permissions=("actions: read",),
        rationale=(
            "Independently verify at the write boundary that the requesting "
            "OIDC workflow run remains the sole active writer."
        ),
    ),
    _broker_read(
        "broker_effect_verification",
        "/repos/{owner}/{repo}/pulls/{pull_number}/reviews/{review_id}",
        required_permissions=("pull-requests: read", "issues: read", "checks: read"),
        rationale=(
            "Bounded by-ID verification of a review, comment, or check run when "
            "a broker-dispatched write response was lost or ambiguous."
        ),
    ),
    # Mutations: exclusively behind the stateless App broker.
    _broker_mutation(
        "write_intent_comment",
        "/repos/{owner}/{repo}/issues/{pull_number}/comments",
        supported_events=ALL_EVENTS,
        required_permissions=("issues: write", "pull-requests: write"),
        rationale=(
            "Persist an immutable App-authored write intent record before "
            "dispatching a non-idempotent external write."
        ),
    ),
    _broker_mutation(
        "completion_receipt_comment",
        "/repos/{owner}/{repo}/issues/{pull_number}/comments",
        supported_events=ALL_EVENTS,
        required_permissions=("issues: write", "pull-requests: write"),
        rationale=(
            "Persist an immutable App-authored completion receipt record after "
            "confirming or reconciling a write effect."
        ),
    ),
    _broker_mutation(
        "advisory_progress_hint",
        "/repos/{owner}/{repo}/issues/{pull_number}/comments/{comment_id}",
        supported_events=ALL_EVENTS,
        method="PATCH",
        required_permissions=("issues: write", "pull-requests: write"),
        rationale=(
            "Update compact read-phase progress/stall hint only; never stores "
            "the sole copy of an unresolved intent, receipt, or review result."
        ),
    ),
    _broker_mutation(
        "issue_reply",
        "/repos/{owner}/{repo}/issues/{pull_number}/comments",
        supported_events=frozenset({GitHubEventKind.ISSUE_COMMENT}),
        required_permissions=("pull-requests: write",),
        rationale="Reply to a verified top-level PR mention.",
    ),
    _broker_mutation(
        "inline_reply",
        "/repos/{owner}/{repo}/pulls/{pull_number}/comments/{root_id}/replies",
        supported_events=frozenset({GitHubEventKind.PULL_REQUEST_REVIEW_COMMENT}),
        required_permissions=("pull-requests: write",),
        rationale="Reply to the verified review-comment root.",
    ),
    _broker_mutation(
        "pr_review_publish",
        "/repos/{owner}/{repo}/pulls/{pull_number}/reviews",
        required_permissions=("pull-requests: write",),
        rationale="Publish a validated PR review via the App broker.",
    ),
    _broker_mutation(
        "companion_prose_review_publish",
        "/repos/{owner}/{repo}/pulls/{pull_number}/reviews",
        required_permissions=("pull-requests: write",),
        rationale=(
            "Publish a verified companion COMMENT review carrying overflow "
            "finding prose on the PR."
        ),
    ),
    _broker_mutation(
        "review_thread_mutation",
        "/graphql",
        required_permissions=("pull-requests: write",),
        rationale=(
            "Resolve, unresolve, or reply to PR review threads via the App broker."
        ),
    ),
    _broker_mutation(
        "check_run_write",
        "/repos/{owner}/{repo}/check-runs",
        required_permissions=("checks: write",),
        rationale=(
            "Create or update ReviewSensei check runs (including "
            "paused-rate-limited) via the App broker."
        ),
    ),
    _broker_mutation(
        "pr_approval_publish",
        "/repos/{owner}/{repo}/pulls/{pull_number}/reviews",
        required_permissions=("pull-requests: write",),
        rationale="Publish an eligible PR approval review via the App broker.",
    ),
)

V6_ENDPOINT_CREDENTIAL_MATRIX: Mapping[str, EndpointCredentialRule] = MappingProxyType(
    dict(_MATRIX_ENTRIES)
)


def get_v6_endpoint_rule(endpoint_id: str) -> EndpointCredentialRule:
    """Return the registered rule for ``endpoint_id`` or fail closed."""

    if (
        not isinstance(endpoint_id, str)
        or endpoint_id not in V6_ENDPOINT_CREDENTIAL_MATRIX
    ):
        raise ReviewInputError(
            f"unknown v6 endpoint_id: {endpoint_id!r}",
            diagnostic="v6_unknown_endpoint",
        )
    return V6_ENDPOINT_CREDENTIAL_MATRIX[endpoint_id]


def authorize_v6_endpoint_credential(
    endpoint_id: str,
    credential: CredentialKind,
    *,
    event: GitHubEventKind,
    visibility: RepositoryVisibility,
    granted_scopes: Collection[str],
) -> EndpointCredentialRule:
    """Authorize a credential, event, visibility, and scopes for an endpoint.

    Fails closed when ``endpoint_id`` is unknown, forbids ``APP_BROKER`` on
    ordinary bulk reads, forbids ``WORKFLOW_GITHUB_TOKEN`` on broker reads or
    mutations, rejects unsupported events/visibilities, and raises a structured
    403 ``GitHubHTTPError`` when ``WORKFLOW_GITHUB_TOKEN`` lacks a required
    read-only scope.
    """

    rule = get_v6_endpoint_rule(endpoint_id)

    if not isinstance(credential, CredentialKind):
        raise ReviewInputError(
            f"unknown credential kind for endpoint {endpoint_id!r}: {credential!r}",
            diagnostic="v6_invalid_credential",
        )
    if not isinstance(event, GitHubEventKind):
        raise ReviewInputError(
            f"unknown event kind for endpoint {endpoint_id!r}: {event!r}",
            diagnostic="v6_invalid_event",
        )
    if not isinstance(visibility, RepositoryVisibility):
        raise ReviewInputError(
            f"unknown repository visibility for endpoint {endpoint_id!r}: {visibility!r}",
            diagnostic="v6_invalid_visibility",
        )

    if rule.operation_kind == OperationKind.BULK_READ:
        if credential == CredentialKind.APP_BROKER:
            raise ReviewInputError(
                f"App broker fallback is forbidden for bulk read endpoint "
                f"{endpoint_id!r}; bulk PR scans must use workflow_github_token",
                diagnostic="v6_app_fallback_forbidden",
            )
    elif credential == CredentialKind.WORKFLOW_GITHUB_TOKEN:
        raise ReviewInputError(
            f"workflow_github_token is not authorized for {rule.operation_kind.value} "
            f"endpoint {endpoint_id!r}; use app_broker",
            diagnostic="v6_workflow_token_forbidden",
        )

    if event not in rule.supported_events:
        raise ReviewInputError(
            f"endpoint {endpoint_id!r} is not supported for event {event.value!r}",
            diagnostic="v6_unsupported_event",
        )
    if visibility not in rule.supported_visibilities:
        raise ReviewInputError(
            f"endpoint {endpoint_id!r} is not supported for visibility "
            f"{visibility.value!r}",
            diagnostic="v6_unsupported_visibility",
        )

    if credential == CredentialKind.WORKFLOW_GITHUB_TOKEN:
        normalized_granted = frozenset(
            scope.strip() for scope in granted_scopes if isinstance(scope, str)
        )
        missing_scopes = tuple(
            scope
            for scope in rule.required_permissions
            if scope not in normalized_granted
        )
        if missing_scopes:
            missing_list = ", ".join(missing_scopes)
            raise V6CredentialPermissionError(
                f"permission denied (403) for endpoint {endpoint_id!r} using "
                f"{credential.value}: missing required scope(s): {missing_list}",
                endpoint_id=endpoint_id,
                credential=credential,
                missing_scopes=missing_scopes,
            )

    return rule


def validate_v6_matrix_least_privilege(
    matrix: Mapping[str, EndpointCredentialRule] = V6_ENDPOINT_CREDENTIAL_MATRIX,
) -> None:
    """Assert repository-wide least-privilege invariants over ``matrix``."""

    if len(matrix) != len(_MATRIX_ENTRIES) and matrix is V6_ENDPOINT_CREDENTIAL_MATRIX:
        raise ValueError("duplicate endpoint_id detected in v6 credential matrix")
    if not matrix:
        raise ValueError("v6 credential matrix cannot be empty")

    broker_reads: set[str] = set()
    for key, rule in matrix.items():
        if key != rule.endpoint_id:
            raise ValueError(
                f"matrix key {key!r} does not match rule.endpoint_id {rule.endpoint_id!r}"
            )
        if "contents: write" in rule.required_permissions:
            raise ValueError(f"{key!r} illegally requires 'contents: write'")
        if rule.allow_app_fallback:
            raise ValueError(f"{key!r} illegally enables allow_app_fallback")

        if rule.operation_kind == OperationKind.BULK_READ:
            if rule.credential_kind != CredentialKind.WORKFLOW_GITHUB_TOKEN:
                raise ValueError(f"bulk read {key!r} must use workflow_github_token")
            if not set(rule.required_permissions).issubset(WORKFLOW_READ_ONLY_SCOPES):
                raise ValueError(
                    f"bulk read {key!r} exceeds read-only scopes: "
                    f"{rule.required_permissions!r}"
                )
        elif rule.operation_kind == OperationKind.BROKER_AUTHORITY_READ:
            if rule.credential_kind != CredentialKind.APP_BROKER:
                raise ValueError(f"broker read {key!r} must use app_broker")
            if rule.max_items_per_page is not None:
                raise ValueError(
                    f"broker read {key!r} must not configure bulk pagination"
                )
            broker_reads.add(key)
        elif rule.operation_kind == OperationKind.MUTATION:
            if rule.credential_kind != CredentialKind.APP_BROKER:
                raise ValueError(f"mutation {key!r} must use app_broker")

        if rule.credential_kind == CredentialKind.WORKFLOW_GITHUB_TOKEN:
            if rule.operation_kind != OperationKind.BULK_READ:
                raise ValueError(
                    f"workflow_github_token cannot own {rule.operation_kind.value} {key!r}"
                )
            if not set(rule.required_permissions).issubset(WORKFLOW_READ_ONLY_SCOPES):
                raise ValueError(
                    f"workflow_github_token rule {key!r} requires non-read-only scope"
                )

    if len(BROKER_APP_READ_ALLOWLIST) != 5 or (
        len(_BROKER_APP_READ_ALLOWLIST_SET) != 5
    ):
        raise ValueError("BROKER_APP_READ_ALLOWLIST must contain exactly 5 entries")
    if broker_reads != _BROKER_APP_READ_ALLOWLIST_SET:
        raise ValueError(
            "BROKER_AUTHORITY_READ rules must match BROKER_APP_READ_ALLOWLIST exactly: "
            f"expected {sorted(_BROKER_APP_READ_ALLOWLIST_SET)}, got {sorted(broker_reads)}"
        )


validate_v6_matrix_least_privilege()
