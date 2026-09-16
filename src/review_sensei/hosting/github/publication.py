"""Exact-head-bound, idempotent App-authored review publication."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ...context import finding_lifecycle_for_comment
from ...diff import analyze_diff
from ...errors import ReviewInputError
from ...models import ReviewComment, ReviewResult
from ...presentation import format_review_comment, format_review_summary
from ...validation import validate_bounded_text
from ...verifier import CandidateFinding, prepare_publishable_review
from .approval import has_blocking_findings
from .errors import (
    GitHubHTTPError,
    GitHubHTTPTransientError,
    GitHubPublicationError,
    GitHubPublicationTransientError,
)
from .http import GitHubHttp

REVIEW_MARKER_PREFIX = "<!-- reviewsensei:review:v1"
FINDING_MARKER_PREFIX = "<!-- reviewsensei:finding:v1"
FINDING_MARKER_PREFIX_V2 = "<!-- reviewsensei:finding:v2"
APPROVAL_MARKER_PREFIX = "<!-- reviewsensei:approval:v1"
CHANGES_REQUESTED_MARKER_PREFIX = "<!-- reviewsensei:changes-requested:v1"
GIT_SHA_HEX = re.compile(r"^[a-f0-9]{40}$")
PUBLISHED_REVIEW_STATES = frozenset({"COMMENTED", "APPROVED", "CHANGES_REQUESTED"})
CHANGES_REQUESTED_BODY = "Blocking ReviewSensei findings remain unresolved."
CHANGES_REQUESTED_INLINE_POINTER = "See the inline ReviewSensei comments on this head."
DISCUSSION_INSTRUCTION = (
    "To discuss this finding, reply with @sensei followed by your question."
)
MAX_CHANGE_REQUEST_EXCERPTS = 8
MAX_CHANGE_REQUEST_EXCERPT_CHARS = 200
# GitHub's review body limit is independent of the provider-neutral summary
# profile. The formatted summary is checked against ReviewLimits first, then
# the complete body (including framing and the idempotency marker) is checked
# against this transport ceiling before any write.
MAX_PUBLISHED_REVIEW_BODY_BYTES = 65_536
MAX_REVIEW_THREAD_PAGES = 10

_REVIEW_THREADS_QUERY = """
query ReviewThreads($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $after) {
        nodes {
          isResolved
          comments(first: 1) {
            nodes {
              body
              author { login }
            }
          }
        }
        pageInfo {
          hasNextPage
          endCursor
        }
      }
    }
  }
}
""".strip()

_FINDING_MARKER_RE = re.compile(
    r"<!-- reviewsensei:finding:v1 repo=(?P<repository_id>[1-9][0-9]*) "
    r"pr=(?P<pull_request>[1-9][0-9]*) head=(?P<head_sha>[a-f0-9]{40}) "
    r"base=(?P<base_sha>[a-f0-9]{40}) result=(?P<result>[a-f0-9]{64}) "
    r"blocking=(?P<blocking>true|false) -->"
)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_FINDING_MARKER_V2_RE = re.compile(
    r"<!-- reviewsensei:finding:v2 repo=(?P<repository_id>[1-9][0-9]*) "
    r"pr=(?P<pull_request>[1-9][0-9]*) head=(?P<head_sha>[a-f0-9]{40}) "
    r"base=(?P<base_sha>[a-f0-9]{40}) fingerprint=(?P<fingerprint>[a-f0-9]{64}) "
    r"state=(?P<state>new|still-present|fixed|outdated|uncertain) "
    r"blocking=(?P<blocking>true|false) -->"
)


def _with_discussion_instruction(text: str) -> str:
    return f"{text}\n\n{DISCUSSION_INSTRUCTION}"


def _result_digest(result: ReviewResult) -> str:
    canonical = json.dumps(
        result.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def review_marker(
    *,
    repository_id: int,
    pull_request: int,
    head_sha: str,
    result: ReviewResult,
) -> str:
    digest = _result_digest(result)
    return (
        f"{review_identity_marker(repository_id=repository_id, pull_request=pull_request, head_sha=head_sha)} "
        f"result={digest} coverage={result.coverage_mode} -->"
    )


def review_identity_marker(
    *, repository_id: int, pull_request: int, head_sha: str
) -> str:
    """Return the per-head review-state reconciliation identity."""

    return (
        f"{REVIEW_MARKER_PREFIX} repo={repository_id} pr={pull_request} head={head_sha}"
    )


def finding_marker(
    *,
    repository_id: int,
    pull_request: int,
    head_sha: str,
    base_sha: str,
    result: ReviewResult,
    blocking: bool,
    fingerprint: str | None = None,
    state: str | None = None,
) -> str:
    """Bind one inline finding's approval classification to this review head."""

    if fingerprint is not None:
        lifecycle_state = state or "new"
        return (
            f"{FINDING_MARKER_PREFIX_V2} repo={repository_id} pr={pull_request} "
            f"head={head_sha} base={base_sha} fingerprint={fingerprint} "
            f"state={lifecycle_state} blocking={'true' if blocking else 'false'} -->"
        )
    return (
        f"{FINDING_MARKER_PREFIX} repo={repository_id} pr={pull_request} "
        f"head={head_sha} base={base_sha} result={_result_digest(result)} "
        f"blocking={'true' if blocking else 'false'} -->"
    )


def finding_fingerprint_from_body(body: object) -> str | None:
    """Return a stable finding fingerprint from an App-authored root, if present."""

    if not isinstance(body, str):
        return None
    match = _FINDING_MARKER_V2_RE.search(body)
    if match is None:
        return None
    return match.group("fingerprint")


def approval_marker(
    *, repository_id: int, pull_request: int, head_sha: str, base_sha: str
) -> str:
    """Return the idempotency marker for one final approval decision."""

    return (
        f"{APPROVAL_MARKER_PREFIX} repo={repository_id} pr={pull_request} "
        f"head={head_sha} base={base_sha} -->"
    )


def changes_requested_marker(
    *, repository_id: int, pull_request: int, head_sha: str, base_sha: str
) -> str:
    """Return the idempotency marker for one exact-head change request."""

    return (
        f"{CHANGES_REQUESTED_MARKER_PREFIX} repo={repository_id} "
        f"pr={pull_request} head={head_sha} base={base_sha} -->"
    )


def _change_request_excerpt(body: object) -> str:
    """Return one bounded, marker-stripped blocking-root excerpt."""

    if not isinstance(body, str) or not body.strip():
        return ""
    text = _HTML_COMMENT_RE.sub("", body)
    text = text.replace(DISCUSSION_INSTRUCTION, "")
    text = " ".join(text.split())
    if len(text) > MAX_CHANGE_REQUEST_EXCERPT_CHARS:
        text = text[: MAX_CHANGE_REQUEST_EXCERPT_CHARS - 1].rstrip() + "…"
    return text


def changes_requested_review_body(*, excerpts: tuple[str, ...], marker: str) -> str:
    """Return one bounded exact-head change-request review body."""

    parts = [CHANGES_REQUESTED_BODY, ""]
    if excerpts:
        parts.extend(f"- {excerpt}" for excerpt in excerpts)
    else:
        parts.append(CHANGES_REQUESTED_INLINE_POINTER)
    parts.extend(["", marker])
    body = "\n".join(parts)
    try:
        validate_bounded_text(
            body,
            MAX_PUBLISHED_REVIEW_BODY_BYTES,
            label="published change-request body",
            allow_empty=False,
        )
    except ReviewInputError as exc:
        raise GitHubPublicationError(
            "formatted review exceeds the configured publication limit"
        ) from exc
    return body


def finding_review_event(
    *, auto_approve: bool, result: ReviewResult
) -> tuple[str, str]:
    """Return the GitHub review event and state for one finding publication."""

    if auto_approve and has_blocking_findings(result):
        return "REQUEST_CHANGES", "CHANGES_REQUESTED"
    return "COMMENT", "COMMENTED"


def finding_blocks_approval(
    *,
    body: object,
    repository_id: int,
    pull_request: int,
    head_sha: str,
    base_sha: str,
) -> bool | None:
    """Return an App finding's persisted blocking decision, if recognizable.

    The v1 marker is the durable contract. The narrow legacy fallback keeps
    existing ReviewSensei findings usable while installations roll forward.
    Unknown App-authored roots fail closed rather than being silently ignored.
    """

    if not isinstance(body, str):
        return None
    match = _FINDING_MARKER_V2_RE.search(body)
    if match is None:
        match = _FINDING_MARKER_RE.search(body)
    if match is not None:
        values = match.groupdict()
        if (
            int(values["repository_id"]) == repository_id
            and int(values["pull_request"]) == pull_request
            and values["head_sha"] == head_sha
            and values["base_sha"] == base_sha
        ):
            return values["blocking"] == "true"
        return None
    if body.startswith("[🚫 Blocking]"):
        return True
    if body.startswith("[💬 Non-blocking]"):
        return False
    return None


def finding_declares_blocking(body: object) -> bool:
    """Return whether a root is a recognized blocking finding for a callback."""

    if not isinstance(body, str):
        return False
    match = _FINDING_MARKER_V2_RE.search(body)
    if match is None:
        match = _FINDING_MARKER_RE.search(body)
    if match is not None:
        return match.group("blocking") == "true"
    return body.startswith("[🚫 Blocking]")


@dataclass(frozen=True)
class PublicationResult:
    status: str
    review_id: int | None = None


@dataclass(frozen=True)
class _PreflightDecision:
    result: PublicationResult | None = None
    app_authored: bool = False


@dataclass(frozen=True)
class _FinalizationPreflight:
    result: PublicationResult | None = None
    repository_id: int | None = None
    base_sha: str | None = None
    app_authored: bool = False


@dataclass(frozen=True)
class _BlockingThreadScan:
    open_blocking: bool
    excerpts: tuple[str, ...] = ()


class ReviewApprovalFinalizer:
    """Converge an eligible exact-head PR to one App approval or change request.

    This is deliberately independent of a provider result. It reads durable
    per-finding classifications from review-thread roots, so it can be invoked
    both after review publication and after an AI resolution mutation. On one
    exact head, unresolved blocking findings always win over a concurrent or
    earlier APPROVE: the later blocking execution still emits REQUEST_CHANGES.
    A later APPROVE wins only after blocking ReviewSensei roots are resolved.
    """

    def __init__(self, *, http: GitHubHttp) -> None:
        self.http = http

    def finalize(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        head_sha: str,
        app_slug: str,
        enabled: bool = True,
        known_blocking_finding: bool = False,
    ) -> PublicationResult:
        if not isinstance(enabled, bool):
            raise GitHubPublicationError("review auto_approve must be a boolean")
        if not isinstance(known_blocking_finding, bool):
            raise GitHubPublicationError("known blocking finding is invalid")
        if not enabled:
            return PublicationResult(status="auto_approval_disabled")
        if not GIT_SHA_HEX.fullmatch(head_sha):
            raise GitHubPublicationError("review head sha is invalid")
        preflight = self._preflight(
            token=token,
            repository=repository,
            pull_request=pull_request,
            head_sha=head_sha,
            app_slug=app_slug,
        )
        if preflight.result is not None:
            return preflight.result
        if preflight.app_authored:
            return PublicationResult(status="skipped_app_authored")
        assert preflight.repository_id is not None and preflight.base_sha is not None
        excerpts: tuple[str, ...] = ()
        if known_blocking_finding:
            open_blocking = True
        else:
            scan = self._scan_blocking_threads(
                token=token,
                repository=repository,
                pull_request=pull_request,
                repository_id=preflight.repository_id,
                head_sha=head_sha,
                base_sha=preflight.base_sha,
                app_slug=app_slug,
            )
            open_blocking = scan.open_blocking
            excerpts = scan.excerpts
        # The thread scan can paginate, so bind the write to the same exact PR
        # identity immediately before emitting APPROVE or REQUEST_CHANGES.
        write_preflight = self._preflight(
            token=token,
            repository=repository,
            pull_request=pull_request,
            head_sha=head_sha,
            app_slug=app_slug,
        )
        if write_preflight.result is not None:
            return write_preflight.result
        if write_preflight.app_authored:
            return PublicationResult(status="skipped_app_authored")
        if (
            write_preflight.repository_id != preflight.repository_id
            or write_preflight.base_sha != preflight.base_sha
        ):
            return PublicationResult(status="skipped_stale_base")
        if open_blocking:
            marker = changes_requested_marker(
                repository_id=preflight.repository_id,
                pull_request=pull_request,
                head_sha=head_sha,
                base_sha=preflight.base_sha,
            )
            reviews = self._load_head_reviews(
                token=token, repository=repository, pull_request=pull_request
            )
            if self._reviews_have_app_head_state(
                reviews,
                head_sha=head_sha,
                app_slug=app_slug,
                state="CHANGES_REQUESTED",
                body_contains=marker,
            ):
                return PublicationResult(status="already_changes_requested")
            return self._post_head_review(
                token=token,
                repository=repository,
                pull_request=pull_request,
                head_sha=head_sha,
                body=changes_requested_review_body(excerpts=excerpts, marker=marker),
                event="REQUEST_CHANGES",
                already=lambda: self._is_already_changes_requested(
                    token=token,
                    repository=repository,
                    pull_request=pull_request,
                    head_sha=head_sha,
                    marker=marker,
                    app_slug=app_slug,
                ),
                success_status="changes_requested",
                already_status="already_changes_requested",
                rejected="change-request publication was rejected",
                permission="change-request publication lacks permission",
                transient="change-request publication failed temporarily",
            )
        # Recheck reviews immediately before APPROVE so a change request that
        # landed during the first thread sweep is not dismissed.
        reviews = self._load_head_reviews(
            token=token, repository=repository, pull_request=pull_request
        )
        kept = self._keep_existing_change_request(
            token=token,
            repository=repository,
            pull_request=pull_request,
            head_sha=head_sha,
            app_slug=app_slug,
            repository_id=preflight.repository_id,
            base_sha=preflight.base_sha,
            reviews=reviews,
        )
        if kept is not None:
            return kept
        if self._reviews_have_app_head_state(
            reviews,
            head_sha=head_sha,
            app_slug=app_slug,
            state="CHANGES_REQUESTED",
        ):
            # A concurrent blocking run may POST REQUEST_CHANGES during the
            # second sweep. Re-read immediately before APPROVE and keep that
            # change request when newly visible roots are still open.
            reviews = self._load_head_reviews(
                token=token, repository=repository, pull_request=pull_request
            )
            kept = self._keep_existing_change_request(
                token=token,
                repository=repository,
                pull_request=pull_request,
                head_sha=head_sha,
                app_slug=app_slug,
                repository_id=preflight.repository_id,
                base_sha=preflight.base_sha,
                reviews=reviews,
            )
            if kept is not None:
                return kept
        marker = approval_marker(
            repository_id=preflight.repository_id,
            pull_request=pull_request,
            head_sha=head_sha,
            base_sha=preflight.base_sha,
        )
        if self._reviews_have_app_head_state(
            reviews,
            head_sha=head_sha,
            app_slug=app_slug,
            state="APPROVED",
            body=marker,
        ):
            return PublicationResult(status="already_approved")
        return self._post_head_review(
            token=token,
            repository=repository,
            pull_request=pull_request,
            head_sha=head_sha,
            body=marker,
            event="APPROVE",
            already=lambda: self._is_already_approved(
                token=token,
                repository=repository,
                pull_request=pull_request,
                head_sha=head_sha,
                marker=marker,
                app_slug=app_slug,
            ),
            success_status="approved",
            already_status="already_approved",
            rejected="approval finalization was rejected",
            permission="approval finalization lacks permission",
            transient="approval finalization failed temporarily",
        )

    def _keep_existing_change_request(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        head_sha: str,
        app_slug: str,
        repository_id: int,
        base_sha: str,
        reviews: list[Any],
    ) -> PublicationResult | None:
        if not self._reviews_have_app_head_state(
            reviews,
            head_sha=head_sha,
            app_slug=app_slug,
            state="CHANGES_REQUESTED",
        ):
            return None
        open_blocking = self._scan_blocking_threads(
            token=token,
            repository=repository,
            pull_request=pull_request,
            repository_id=repository_id,
            head_sha=head_sha,
            base_sha=base_sha,
            app_slug=app_slug,
        ).open_blocking
        write_preflight = self._preflight(
            token=token,
            repository=repository,
            pull_request=pull_request,
            head_sha=head_sha,
            app_slug=app_slug,
        )
        if write_preflight.result is not None:
            return write_preflight.result
        if write_preflight.app_authored:
            return PublicationResult(status="skipped_app_authored")
        if (
            write_preflight.repository_id != repository_id
            or write_preflight.base_sha != base_sha
        ):
            return PublicationResult(status="skipped_stale_base")
        if open_blocking:
            return PublicationResult(status="already_changes_requested")
        return None

    def _post_head_review(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        head_sha: str,
        body: str,
        event: str,
        already: Callable[[], bool],
        success_status: str,
        already_status: str,
        rejected: str,
        permission: str,
        transient: str,
    ) -> PublicationResult:
        path = self.http.repository_path(repository, f"/pulls/{pull_request}/reviews")
        try:
            status, payload = self.http.request(
                "POST",
                path,
                token=token,
                body={"body": body, "event": event, "commit_id": head_sha},
            )
        except GitHubHTTPTransientError as exc:
            if already():
                return PublicationResult(status=already_status)
            raise GitHubPublicationTransientError(transient) from exc
        if status == 200 and isinstance(payload, dict):
            review_id = payload.get("id")
            if isinstance(review_id, int):
                return PublicationResult(status=success_status, review_id=review_id)
        if status in {409, 422, 429} or status >= 500:
            if already():
                return PublicationResult(status=already_status)
            if status == 422:
                raise GitHubPublicationError(rejected)
            raise GitHubPublicationTransientError(transient)
        if status == 404:
            raise GitHubPublicationError("review target was not found")
        if status == 403:
            raise GitHubPublicationError(permission)
        raise GitHubPublicationError(rejected)

    def _load_head_reviews(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
    ) -> list[Any]:
        try:
            return self.http.paginate(
                path=self.http.repository_path(
                    repository, f"/pulls/{pull_request}/reviews"
                ),
                token=token,
            )
        except GitHubHTTPTransientError as exc:
            raise GitHubPublicationTransientError(
                "review decision reconciliation failed temporarily"
            ) from exc
        except GitHubHTTPError as exc:
            raise GitHubPublicationError(
                "review decision reconciliation failed"
            ) from exc

    def _preflight(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        head_sha: str,
        app_slug: str,
    ) -> _FinalizationPreflight:
        status, payload = self.http.request(
            "GET",
            self.http.repository_path(repository, f"/pulls/{pull_request}"),
            token=token,
        )
        if status == 404:
            raise GitHubPublicationError("review target was not found")
        if status < 200 or status >= 300 or not isinstance(payload, dict):
            raise GitHubPublicationError("approval finalization preflight failed")
        if payload.get("state") != "open" or payload.get("draft") is True:
            return _FinalizationPreflight(
                result=PublicationResult(status="skipped_pr_state")
            )
        head = payload.get("head")
        base = payload.get("base")
        if not isinstance(head, dict) or not isinstance(base, dict):
            raise GitHubPublicationError("approval finalization preflight was invalid")
        if head.get("sha") != head_sha:
            return _FinalizationPreflight(
                result=PublicationResult(status="skipped_stale_head")
            )
        head_repo = head.get("repo")
        base_repo = base.get("repo")
        if not isinstance(head_repo, dict) or not isinstance(base_repo, dict):
            raise GitHubPublicationError("approval finalization preflight was invalid")
        if (
            head_repo.get("fork") is not False
            or base_repo.get("fork") is not False
            or head_repo.get("full_name") != repository
            or base_repo.get("full_name") != repository
        ):
            return _FinalizationPreflight(
                result=PublicationResult(status="skipped_fork")
            )
        repository_id = base_repo.get("id")
        base_sha = base.get("sha")
        author = payload.get("user")
        if (
            isinstance(repository_id, bool)
            or not isinstance(repository_id, int)
            or repository_id < 1
            or not isinstance(base_sha, str)
            or not GIT_SHA_HEX.fullmatch(base_sha)
            or not isinstance(author, dict)
            or not isinstance(author.get("login"), str)
            or not author["login"].strip()
        ):
            raise GitHubPublicationError("approval finalization preflight was invalid")
        return _FinalizationPreflight(
            repository_id=repository_id,
            base_sha=base_sha,
            app_authored=author["login"] == app_slug,
        )

    def _has_open_blocking_findings(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        repository_id: int,
        head_sha: str,
        base_sha: str,
        app_slug: str,
    ) -> bool:
        return self._scan_blocking_threads(
            token=token,
            repository=repository,
            pull_request=pull_request,
            repository_id=repository_id,
            head_sha=head_sha,
            base_sha=base_sha,
            app_slug=app_slug,
        ).open_blocking

    def _scan_blocking_threads(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        repository_id: int,
        head_sha: str,
        base_sha: str,
        app_slug: str,
    ) -> _BlockingThreadScan:
        owner, separator, name = repository.partition("/")
        if not separator or not owner or not name:
            raise GitHubPublicationError("review thread repository is invalid")
        after: str | None = None
        open_blocking = False
        excerpts: list[str] = []
        for _ in range(MAX_REVIEW_THREAD_PAGES):
            try:
                status, payload = self.http.request(
                    "POST",
                    "/graphql",
                    token=token,
                    body={
                        "operationName": "ReviewThreads",
                        "query": _REVIEW_THREADS_QUERY,
                        "variables": {
                            "owner": owner,
                            "name": name,
                            "number": pull_request,
                            "after": after,
                        },
                    },
                )
            except GitHubHTTPTransientError as exc:
                raise GitHubPublicationTransientError(
                    "review thread lookup failed temporarily"
                ) from exc
            except GitHubHTTPError as exc:
                raise GitHubPublicationError("review thread lookup failed") from exc
            if status == 429 or status >= 500:
                raise GitHubPublicationTransientError(
                    "review thread lookup failed temporarily"
                )
            if status < 200 or status >= 300 or not isinstance(payload, dict):
                raise GitHubPublicationError("review thread lookup failed")
            if payload.get("errors") not in (None, []):
                raise GitHubPublicationError("review thread lookup failed")
            try:
                threads = payload["data"]["repository"]["pullRequest"]["reviewThreads"]
                nodes = threads["nodes"]
                page_info = threads["pageInfo"]
            except (KeyError, TypeError) as exc:
                raise GitHubPublicationError(
                    "review thread response was invalid"
                ) from exc
            if not isinstance(nodes, list) or not isinstance(page_info, dict):
                raise GitHubPublicationError("review thread response was invalid")
            for node in nodes:
                if not isinstance(node, dict) or not isinstance(
                    node.get("isResolved"), bool
                ):
                    raise GitHubPublicationError("review thread response was invalid")
                comments = node.get("comments")
                roots = comments.get("nodes") if isinstance(comments, dict) else None
                if node["isResolved"]:
                    continue
                if (
                    not isinstance(roots, list)
                    or len(roots) != 1
                    or not isinstance(roots[0], dict)
                ):
                    raise GitHubPublicationError("review thread response was invalid")
                root = roots[0]
                author = root.get("author")
                if not isinstance(author, dict) or author.get("login") != app_slug:
                    continue
                blocking = finding_blocks_approval(
                    body=root.get("body"),
                    repository_id=repository_id,
                    pull_request=pull_request,
                    head_sha=head_sha,
                    base_sha=base_sha,
                )
                if blocking is not False:
                    open_blocking = True
                    excerpt = _change_request_excerpt(root.get("body"))
                    if excerpt and len(excerpts) < MAX_CHANGE_REQUEST_EXCERPTS:
                        excerpts.append(excerpt)
            has_next = page_info.get("hasNextPage")
            if not isinstance(has_next, bool):
                raise GitHubPublicationError("review thread response was invalid")
            if not has_next:
                return _BlockingThreadScan(
                    open_blocking=open_blocking, excerpts=tuple(excerpts)
                )
            if open_blocking and len(excerpts) >= MAX_CHANGE_REQUEST_EXCERPTS:
                return _BlockingThreadScan(open_blocking=True, excerpts=tuple(excerpts))
            after = page_info.get("endCursor")
            if not isinstance(after, str) or not after:
                raise GitHubPublicationError("review thread response was invalid")
        raise GitHubPublicationError(
            "review thread pagination exceeded configured limit"
        )

    def _is_already_approved(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        head_sha: str,
        marker: str,
        app_slug: str,
    ) -> bool:
        return self._has_app_head_review(
            token=token,
            repository=repository,
            pull_request=pull_request,
            head_sha=head_sha,
            app_slug=app_slug,
            state="APPROVED",
            body=marker,
            transient="approval reconciliation failed temporarily",
            failed="approval reconciliation failed",
        )

    def _is_already_changes_requested(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        head_sha: str,
        marker: str,
        app_slug: str,
    ) -> bool:
        return self._has_app_head_review(
            token=token,
            repository=repository,
            pull_request=pull_request,
            head_sha=head_sha,
            app_slug=app_slug,
            state="CHANGES_REQUESTED",
            body_contains=marker,
            transient="change-request reconciliation failed temporarily",
            failed="change-request reconciliation failed",
        )

    def _reviews_have_app_head_state(
        self,
        reviews: list[Any],
        *,
        head_sha: str,
        app_slug: str,
        state: str,
        body: str | None = None,
        body_contains: str | None = None,
    ) -> bool:
        for review in reviews:
            if not isinstance(review, dict):
                continue
            user = review.get("user")
            review_body = review.get("body")
            if (
                review.get("commit_id") == head_sha
                and review.get("state") == state
                and isinstance(user, dict)
                and user.get("login") == app_slug
                and (body is None or review_body == body)
                and (
                    body_contains is None
                    or (isinstance(review_body, str) and body_contains in review_body)
                )
            ):
                return True
        return False

    def _has_app_head_review(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        head_sha: str,
        app_slug: str,
        state: str,
        body: str | None = None,
        body_contains: str | None = None,
        transient: str,
        failed: str,
    ) -> bool:
        try:
            reviews = self.http.paginate(
                path=self.http.repository_path(
                    repository, f"/pulls/{pull_request}/reviews"
                ),
                token=token,
            )
        except GitHubHTTPTransientError as exc:
            raise GitHubPublicationTransientError(transient) from exc
        except GitHubHTTPError as exc:
            raise GitHubPublicationError(failed) from exc
        return self._reviews_have_app_head_state(
            reviews,
            head_sha=head_sha,
            app_slug=app_slug,
            state=state,
            body=body,
            body_contains=body_contains,
        )


class ReviewPublisher:
    """Publish one validated review as App-authored inline comments and summary."""

    def __init__(self, *, http: GitHubHttp) -> None:
        self.http = http
        self.finalizer = ReviewApprovalFinalizer(http=http)

    def publish(
        self,
        *,
        token: str,
        repository: str,
        repository_id: int,
        pull_request: int,
        head_sha: str,
        base_branch: str,
        base_sha: str,
        result: ReviewResult,
        diff: str,
        app_slug: str,
        auto_approve: bool = True,
        candidates: Sequence[CandidateFinding] | None = None,
        snapshot: Mapping[str, str] | None = None,
        snapshot_sha256: str | None = None,
        evidence_policy: str = "legacy",
    ) -> PublicationResult:
        if not isinstance(auto_approve, bool):
            raise GitHubPublicationError("review auto_approve must be a boolean")
        if (
            isinstance(repository_id, bool)
            or not isinstance(repository_id, int)
            or repository_id <= 0
            or isinstance(pull_request, bool)
            or not isinstance(pull_request, int)
            or pull_request <= 0
        ):
            raise GitHubPublicationError("review identity is invalid")
        if not GIT_SHA_HEX.fullmatch(head_sha):
            raise GitHubPublicationError("review head sha is invalid")
        if not isinstance(base_branch, str) or not base_branch.strip():
            raise GitHubPublicationError("review base branch is invalid")
        if not GIT_SHA_HEX.fullmatch(base_sha):
            raise GitHubPublicationError("review base sha is invalid")
        if not isinstance(result, ReviewResult):
            raise GitHubPublicationError("review result is invalid")
        try:
            analysis = analyze_diff(diff)
        except ReviewInputError as exc:
            raise GitHubPublicationError("review diff failed validation") from exc
        try:
            prepared = prepare_publishable_review(
                result,
                candidates=candidates,
                snapshot=snapshot,
                snapshot_sha256=snapshot_sha256,
                evidence_policy=evidence_policy,
                changed_lines=analysis.changed_lines,
            )
        except ReviewInputError as exc:
            raise GitHubPublicationError("review evidence verification failed") from exc
        result = prepared.result
        self._validate_locations(result, diff)
        marker = review_marker(
            repository_id=repository_id,
            pull_request=pull_request,
            head_sha=head_sha,
            result=result,
        )
        identity_marker = review_identity_marker(
            repository_id=repository_id,
            pull_request=pull_request,
            head_sha=head_sha,
        )
        preflight = self._preflight_pr(
            token=token,
            repository=repository,
            repository_id=repository_id,
            pull_request=pull_request,
            head_sha=head_sha,
            base_branch=base_branch,
            base_sha=base_sha,
            app_slug=app_slug,
        )
        if preflight.result is not None:
            return preflight.result
        # Reconcile before POST so retries never duplicate the same findings.
        # A later execution may still publish blocking comments after another
        # run approved the same head, and a later clean run may promote
        # CHANGES_REQUESTED to APPROVED only after blocking roots resolve.
        # Any pagination/transport failure is an uncertainty and therefore
        # fails closed instead of being treated as "no marker".
        try:
            identity_states, result_states = self._published_state_index(
                token=token,
                repository=repository,
                pull_request=pull_request,
                head_sha=head_sha,
                identity_marker=identity_marker,
                result_marker=marker,
                app_slug=app_slug,
            )
            if has_blocking_findings(result):
                if result_states:
                    if not preflight.app_authored:
                        self.finalizer.finalize(
                            token=token,
                            repository=repository,
                            pull_request=pull_request,
                            head_sha=head_sha,
                            app_slug=app_slug,
                            enabled=auto_approve,
                            known_blocking_finding=True,
                        )
                    return PublicationResult(status="already_published")
            else:
                if "APPROVED" in identity_states:
                    return PublicationResult(status="already_published")
                if identity_states & {"COMMENTED", "CHANGES_REQUESTED"}:
                    if not preflight.app_authored:
                        self.finalizer.finalize(
                            token=token,
                            repository=repository,
                            pull_request=pull_request,
                            head_sha=head_sha,
                            app_slug=app_slug,
                            enabled=auto_approve,
                            known_blocking_finding=False,
                        )
                    return PublicationResult(status="already_published")
        except GitHubHTTPTransientError as exc:
            raise GitHubPublicationTransientError(
                "review reconciliation failed temporarily"
            ) from exc
        except GitHubHTTPError as exc:
            raise GitHubPublicationError("review reconciliation failed") from exc

        # Marker pagination can span multiple network requests. Re-read the
        # authoritative PR immediately before publication so a changed head,
        # base, repository, or state fails closed instead of accepting an
        # outdated review.
        write_preflight = self._preflight_pr(
            token=token,
            repository=repository,
            repository_id=repository_id,
            pull_request=pull_request,
            head_sha=head_sha,
            base_branch=base_branch,
            base_sha=base_sha,
            app_slug=app_slug,
        )
        if write_preflight.result is not None:
            return write_preflight.result
        try:
            summary = format_review_summary(result.summary, result.comments)
            if result.coverage_mode != "full":
                summary = f"{summary}\n\nCoverage mode: {result.coverage_mode}."
            validate_bounded_text(
                summary,
                result.limits.max_summary_bytes,
                label="published review summary",
                allow_empty=False,
            )
            body = f"{_with_discussion_instruction(summary)}\n\n{marker}"
            validate_bounded_text(
                body,
                MAX_PUBLISHED_REVIEW_BODY_BYTES,
                label="published review body",
                allow_empty=False,
            )
            lifecycle_by_fingerprint = {
                item.fingerprint: item.state for item in result.finding_lifecycles
            }
            prepared_comments: list[tuple[ReviewComment, str, str]] = []
            for comment in result.comments:
                lifecycle = finding_lifecycle_for_comment(comment)
                state = lifecycle_by_fingerprint.get(lifecycle.fingerprint, "new")
                comment_body = (
                    f"{_with_discussion_instruction(format_review_comment(comment))}\n\n"
                    f"{finding_marker(repository_id=repository_id, pull_request=pull_request, head_sha=head_sha, base_sha=base_sha, result=result, blocking=comment.blocks_approval, fingerprint=lifecycle.fingerprint, state=state)}"
                )
                validate_bounded_text(
                    comment_body,
                    result.limits.max_comment_body_bytes,
                    label="published comment body",
                    allow_empty=False,
                )
                prepared_comments.append((comment, lifecycle.fingerprint, comment_body))
        except ReviewInputError as exc:
            raise GitHubPublicationError(
                "formatted review exceeds the configured publication limit"
            ) from exc
        published_fingerprints: frozenset[str] = frozenset()
        if prepared_comments:
            published_fingerprints = self._published_finding_fingerprints(
                token=token,
                repository=repository,
                pull_request=pull_request,
                app_slug=app_slug,
            )
        comments = []
        for comment, fingerprint, comment_body in prepared_comments:
            if fingerprint in published_fingerprints:
                continue
            comments.append(
                {
                    "path": comment.path,
                    "line": comment.line,
                    "side": "RIGHT",
                    "body": comment_body,
                }
            )
        # Blocking findings request changes on this exact head. The shared
        # finalizer remains the sole APPROVE writer, and it re-asserts
        # REQUEST_CHANGES when a later execution still sees unresolved
        # blocking ReviewSensei roots.
        event, published_state = finding_review_event(
            auto_approve=auto_approve, result=result
        )
        path = self.http.repository_path(
            repository,
            f"/pulls/{pull_request}/reviews",
        )
        try:
            status, payload = self.http.request(
                "POST",
                path,
                token=token,
                body={
                    "body": body,
                    "event": event,
                    "commit_id": head_sha,
                    "comments": comments,
                },
            )
        except GitHubHTTPTransientError as exc:
            if self._reconcile_marker(
                token=token,
                repository=repository,
                pull_request=pull_request,
                head_sha=head_sha,
                marker=identity_marker,
                app_slug=app_slug,
                expected_state=published_state,
            ):
                self._handle_after_findings(
                    token=token,
                    repository=repository,
                    pull_request=pull_request,
                    head_sha=head_sha,
                    app_slug=app_slug,
                    auto_approve=auto_approve,
                    result=result,
                    app_authored=write_preflight.app_authored,
                    finding_event=event,
                )
                return PublicationResult(status="already_published")
            raise GitHubPublicationTransientError(
                "review publication failed temporarily"
            ) from exc
        if status == 200 and isinstance(payload, dict):
            review_id = payload.get("id")
            if isinstance(review_id, int):
                self._handle_after_findings(
                    token=token,
                    repository=repository,
                    pull_request=pull_request,
                    head_sha=head_sha,
                    app_slug=app_slug,
                    auto_approve=auto_approve,
                    result=result,
                    app_authored=write_preflight.app_authored,
                    finding_event=event,
                )
                return PublicationResult(status="published", review_id=review_id)
        if status == 404:
            raise GitHubPublicationError("review target was not found")
        if status == 403:
            raise GitHubPublicationError("review publication lacks permission")
        if status == 422:
            # Ambiguous failure: reconcile the marker before deciding.
            if self._reconcile_marker(
                token=token,
                repository=repository,
                pull_request=pull_request,
                head_sha=head_sha,
                marker=identity_marker,
                app_slug=app_slug,
                expected_state=published_state,
            ):
                self._handle_after_findings(
                    token=token,
                    repository=repository,
                    pull_request=pull_request,
                    head_sha=head_sha,
                    app_slug=app_slug,
                    auto_approve=auto_approve,
                    result=result,
                    app_authored=write_preflight.app_authored,
                    finding_event=event,
                )
                return PublicationResult(status="already_published")
            raise GitHubPublicationError("review publication was rejected")
        if status == 409 or status == 429 or status >= 500:
            if self._reconcile_marker(
                token=token,
                repository=repository,
                pull_request=pull_request,
                head_sha=head_sha,
                marker=identity_marker,
                app_slug=app_slug,
                expected_state=published_state,
            ):
                self._handle_after_findings(
                    token=token,
                    repository=repository,
                    pull_request=pull_request,
                    head_sha=head_sha,
                    app_slug=app_slug,
                    auto_approve=auto_approve,
                    result=result,
                    app_authored=write_preflight.app_authored,
                    finding_event=event,
                )
                return PublicationResult(status="already_published")
            raise GitHubPublicationTransientError(
                "review publication failed temporarily"
            )
        raise GitHubPublicationError("review publication was rejected")

    def _handle_after_findings(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        head_sha: str,
        app_slug: str,
        auto_approve: bool,
        result: ReviewResult,
        app_authored: bool,
        finding_event: str,
    ) -> None:
        # App-authored PRs stay on the skipped_app_authored preflight path:
        # never APPROVE or REQUEST_CHANGES from the finalizer.
        # A finding review that already submitted REQUEST_CHANGES must not
        # post a second identical event; a later execution or resolved-root
        # pass is what may APPROVE.
        if app_authored or finding_event == "REQUEST_CHANGES":
            return
        self.finalizer.finalize(
            token=token,
            repository=repository,
            pull_request=pull_request,
            head_sha=head_sha,
            app_slug=app_slug,
            enabled=auto_approve,
            known_blocking_finding=has_blocking_findings(result),
        )

    def _published_finding_fingerprints(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        app_slug: str,
    ) -> frozenset[str]:
        """Return App-authored finding fingerprints already on this pull request.

        Human comments are ignored. Existing ReviewSensei roots keep their
        discussion identity across heads so a moved or rephrased finding does
        not open a duplicate thread. This lookup never resolves or edits a
        thread.
        """

        owner, separator, name = repository.partition("/")
        if not separator or not owner or not name:
            raise GitHubPublicationError("review thread repository is invalid")
        fingerprints: set[str] = set()
        after: str | None = None
        for _ in range(MAX_REVIEW_THREAD_PAGES):
            try:
                status, payload = self.http.request(
                    "POST",
                    "/graphql",
                    token=token,
                    body={
                        "operationName": "ReviewThreads",
                        "query": _REVIEW_THREADS_QUERY,
                        "variables": {
                            "owner": owner,
                            "name": name,
                            "number": pull_request,
                            "after": after,
                        },
                    },
                )
            except GitHubHTTPTransientError as exc:
                raise GitHubPublicationTransientError(
                    "review thread lookup failed temporarily"
                ) from exc
            except GitHubHTTPError as exc:
                raise GitHubPublicationError("review thread lookup failed") from exc
            if status == 429 or status >= 500:
                raise GitHubPublicationTransientError(
                    "review thread lookup failed temporarily"
                )
            if status in (401, 403):
                # Duplicate suppression depends on the GraphQL surface for every
                # publication carrying inline findings, so a missing scope or an
                # enterprise policy blocking /graphql is reported distinctly from
                # a malformed response instead of as a generic failure.
                raise GitHubPublicationError(
                    "review thread lookup was not permitted; publication "
                    "requires GraphQL read access to review threads"
                )
            if status < 200 or status >= 300 or not isinstance(payload, dict):
                raise GitHubPublicationError("review thread lookup failed")
            if payload.get("errors") not in (None, []):
                raise GitHubPublicationError("review thread lookup failed")
            try:
                threads = payload["data"]["repository"]["pullRequest"]["reviewThreads"]
                nodes = threads["nodes"]
                page_info = threads["pageInfo"]
            except (KeyError, TypeError) as exc:
                raise GitHubPublicationError(
                    "review thread response was invalid"
                ) from exc
            if not isinstance(nodes, list) or not isinstance(page_info, dict):
                raise GitHubPublicationError("review thread response was invalid")
            for node in nodes:
                if not isinstance(node, dict):
                    raise GitHubPublicationError("review thread response was invalid")
                comments = node.get("comments")
                if not isinstance(comments, dict):
                    # An unreadable thread may already carry a finding marker,
                    # so skipping it would risk publishing a duplicate.
                    raise GitHubPublicationError("review thread response was invalid")
                roots = comments.get("nodes")
                if not isinstance(roots, list):
                    raise GitHubPublicationError("review thread response was invalid")
                if not roots:
                    continue
                if not isinstance(roots[0], dict):
                    raise GitHubPublicationError("review thread response was invalid")
                root = roots[0]
                author = root.get("author")
                if not isinstance(author, dict):
                    # A root whose author cannot be read may still be an
                    # App-authored finding, so skipping it would risk opening a
                    # duplicate thread for a fingerprint already published.
                    raise GitHubPublicationError("review thread response was invalid")
                login = author.get("login")
                if login is not None and not isinstance(login, str):
                    raise GitHubPublicationError("review thread response was invalid")
                # GitHub logins are case-insensitive, so a configured slug that
                # differs only in case must still match its own findings.
                if login is None or login.casefold() != app_slug.casefold():
                    continue
                fingerprint = finding_fingerprint_from_body(root.get("body"))
                if fingerprint is not None:
                    fingerprints.add(fingerprint)
            has_next = page_info.get("hasNextPage")
            if not isinstance(has_next, bool):
                raise GitHubPublicationError("review thread response was invalid")
            if not has_next:
                return frozenset(fingerprints)
            after = page_info.get("endCursor")
            if not isinstance(after, str) or not after:
                raise GitHubPublicationError("review thread response was invalid")
        raise GitHubPublicationError(
            "review thread pagination exceeded configured limit"
        )

    def _validate_locations(self, result: ReviewResult, diff: str) -> None:
        try:
            analysis = analyze_diff(diff)
        except ReviewInputError as exc:
            raise GitHubPublicationError("review diff failed validation") from exc
        allowed = analysis.changed_lines
        for comment in result.comments:
            lines = allowed.get(comment.path)
            if not lines or comment.line not in lines:
                raise GitHubPublicationError("review comment targets an unchanged line")

    def _preflight_pr(
        self,
        *,
        token: str,
        repository: str,
        repository_id: int,
        pull_request: int,
        head_sha: str,
        base_branch: str,
        base_sha: str,
        app_slug: str,
    ) -> _PreflightDecision:
        path = self.http.repository_path(repository, f"/pulls/{pull_request}")
        status, payload = self.http.request("GET", path, token=token)
        if status == 404:
            raise GitHubPublicationError("review target was not found")
        if status < 200 or status >= 300:
            raise GitHubPublicationError("review preflight failed")
        if not isinstance(payload, dict):
            raise GitHubPublicationError("review preflight response was invalid")
        if payload.get("state") != "open" or payload.get("draft") is True:
            return _PreflightDecision(
                result=PublicationResult(status="skipped_pr_state")
            )
        head = payload.get("head")
        if not isinstance(head, dict):
            raise GitHubPublicationError("review preflight head was invalid")
        if head.get("sha") != head_sha:
            return _PreflightDecision(
                result=PublicationResult(status="skipped_stale_head")
            )
        head_repository = head.get("repo")
        if not isinstance(head_repository, dict):
            raise GitHubPublicationError("review preflight head repository was invalid")
        head_fork = head_repository.get("fork")
        if not isinstance(head_fork, bool):
            raise GitHubPublicationError("review preflight head repository was invalid")
        if head_fork or head_repository.get("full_name") != repository:
            return _PreflightDecision(result=PublicationResult(status="skipped_fork"))
        base = payload.get("base")
        if not isinstance(base, dict):
            raise GitHubPublicationError("review preflight base was invalid")
        repository_info = base.get("repo")
        if not isinstance(repository_info, dict):
            raise GitHubPublicationError("review preflight repository was invalid")
        base_id = repository_info.get("id")
        if isinstance(base_id, bool) or not isinstance(base_id, int) or base_id <= 0:
            raise GitHubPublicationError("review preflight repository was invalid")
        base_fork = repository_info.get("fork")
        if not isinstance(base_fork, bool):
            raise GitHubPublicationError("review preflight repository was invalid")
        if base_id != repository_id:
            return _PreflightDecision(
                result=PublicationResult(status="skipped_repository_mismatch")
            )
        if repository_info.get("full_name") != repository:
            return _PreflightDecision(
                result=PublicationResult(status="skipped_repository_mismatch")
            )
        if base_fork:
            return _PreflightDecision(result=PublicationResult(status="skipped_fork"))
        actual_base_branch = base.get("ref")
        actual_base_sha = base.get("sha")
        if not isinstance(actual_base_branch, str) or not GIT_SHA_HEX.fullmatch(
            actual_base_sha if isinstance(actual_base_sha, str) else ""
        ):
            raise GitHubPublicationError("review preflight base binding was invalid")
        if actual_base_branch != base_branch or actual_base_sha != base_sha:
            return _PreflightDecision(
                result=PublicationResult(status="skipped_stale_base")
            )
        author = payload.get("user")
        if not isinstance(author, dict):
            raise GitHubPublicationError("review preflight author was invalid")
        author_login = author.get("login")
        if not isinstance(author_login, str) or not author_login.strip():
            raise GitHubPublicationError("review preflight author was invalid")
        return _PreflightDecision(app_authored=author_login == app_slug)

    def _published_state_index(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        head_sha: str,
        identity_marker: str,
        result_marker: str,
        app_slug: str,
    ) -> tuple[frozenset[str], frozenset[str]]:
        path = self.http.repository_path(repository, f"/pulls/{pull_request}/reviews")
        payload = self.http.paginate(path=path, token=token)
        identity_states: set[str] = set()
        result_states: set[str] = set()
        for review in payload:
            if not isinstance(review, dict):
                continue
            body = review.get("body")
            commit_id = review.get("commit_id")
            user = review.get("user")
            if (
                isinstance(body, str)
                and commit_id == head_sha
                and isinstance(user, dict)
                and user.get("login") == app_slug
            ):
                state = review.get("state")
                if state not in PUBLISHED_REVIEW_STATES:
                    if identity_marker in body or result_marker in body:
                        raise GitHubPublicationError(
                            "review reconciliation response was invalid"
                        )
                    continue
                if identity_marker in body:
                    identity_states.add(state)
                if result_marker in body:
                    result_states.add(state)
        return frozenset(identity_states), frozenset(result_states)

    def _published_states(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        head_sha: str,
        marker: str,
        app_slug: str,
    ) -> frozenset[str]:
        identity_states, _ = self._published_state_index(
            token=token,
            repository=repository,
            pull_request=pull_request,
            head_sha=head_sha,
            identity_marker=marker,
            result_marker=marker,
            app_slug=app_slug,
        )
        return identity_states

    def _has_open_review_threads(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
    ) -> bool:
        """Return whether any existing pull-request review thread is unresolved.

        GitHub's REST review-comment endpoint does not expose thread resolution
        state. The narrow GraphQL query asks only for that state, keeps the
        response bounded, and fails closed when the approval preflight cannot
        establish a complete answer.
        """

        owner, separator, name = repository.partition("/")
        if not separator or not owner or not name:
            raise GitHubPublicationError("review thread repository is invalid")
        after: str | None = None
        for _ in range(MAX_REVIEW_THREAD_PAGES):
            variables: dict[str, object] = {
                "owner": owner,
                "name": name,
                "number": pull_request,
                "after": after,
            }
            try:
                status, payload = self.http.request(
                    "POST",
                    "/graphql",
                    token=token,
                    body={
                        "operationName": "ReviewThreads",
                        "query": _REVIEW_THREADS_QUERY,
                        "variables": variables,
                    },
                )
            except GitHubHTTPTransientError as exc:
                raise GitHubPublicationTransientError(
                    "review thread lookup failed temporarily"
                ) from exc
            except GitHubHTTPError as exc:
                raise GitHubPublicationError("review thread lookup failed") from exc
            if status == 429 or status >= 500:
                raise GitHubPublicationTransientError(
                    "review thread lookup failed temporarily"
                )
            if status < 200 or status >= 300 or not isinstance(payload, dict):
                raise GitHubPublicationError("review thread lookup failed")
            if "errors" in payload:
                errors = payload["errors"]
                if not isinstance(errors, list) or errors:
                    raise GitHubPublicationError("review thread lookup failed")
            data = payload.get("data")
            if not isinstance(data, dict):
                raise GitHubPublicationError("review thread response was invalid")
            repository_data = data.get("repository")
            if not isinstance(repository_data, dict):
                raise GitHubPublicationError("review thread response was invalid")
            pull_request_data = repository_data.get("pullRequest")
            if not isinstance(pull_request_data, dict):
                raise GitHubPublicationError("review thread response was invalid")
            threads = pull_request_data.get("reviewThreads")
            if not isinstance(threads, dict):
                raise GitHubPublicationError("review thread response was invalid")
            nodes = threads.get("nodes")
            if not isinstance(nodes, list):
                raise GitHubPublicationError("review thread response was invalid")
            for node in nodes:
                if not isinstance(node, dict) or not isinstance(
                    node.get("isResolved"), bool
                ):
                    raise GitHubPublicationError("review thread response was invalid")
                if not node["isResolved"]:
                    return True
            page_info = threads.get("pageInfo")
            if not isinstance(page_info, dict) or not isinstance(
                page_info.get("hasNextPage"), bool
            ):
                raise GitHubPublicationError("review thread response was invalid")
            if not page_info["hasNextPage"]:
                return False
            end_cursor = page_info.get("endCursor")
            if not isinstance(end_cursor, str) or not end_cursor:
                raise GitHubPublicationError("review thread response was invalid")
            after = end_cursor
        raise GitHubPublicationError(
            "review thread pagination exceeded configured limit"
        )

    def _reconcile_marker(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        head_sha: str,
        marker: str,
        app_slug: str,
        expected_state: str,
    ) -> bool:
        try:
            return expected_state in self._published_states(
                token=token,
                repository=repository,
                pull_request=pull_request,
                head_sha=head_sha,
                marker=marker,
                app_slug=app_slug,
            )
        except GitHubHTTPTransientError as exc:
            raise GitHubPublicationTransientError(
                "review reconciliation failed temporarily"
            ) from exc
        except GitHubHTTPError as exc:
            raise GitHubPublicationError("review reconciliation failed") from exc

    def http_get(self, path: str, *, token: str) -> tuple[int, Any]:
        return self.http.request("GET", path, token=token)
