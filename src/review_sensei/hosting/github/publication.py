"""Exact-head-bound, idempotent App-authored review publication."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

from ...baseline import ReviewBaseline
from ...context import ReviewContextCacheKey, finding_lifecycle_for_comment
from ...convergence import (
    LEGACY_REVIEW_MODE,
    BlockerCandidate,
    ReviewConvergencePolicy,
)
from ...coverage import CoverageManifest
from ...diff import DiffAnalysis, analyze_diff
from ...errors import ReviewInputError
from ...models import ReviewComment, ReviewResult
from ...outcomes import PUBLIC_DIAGNOSTICS, RunOutcome, sanitize_diagnostic
from ...presentation import (
    escape_markdown_label,
    format_review_comment,
    format_review_summary,
)
from ...validation import validate_bounded_text
from ...verifier import CandidateFinding, PublishableReview, prepare_publishable_review
from .approval import (
    ReviewApprovalEligibility,
    approval_eligibility_from_result,
    approval_withheld_diagnostic,
    evaluate_approval_facts,
    has_blocking_findings,
)
from .checks import (
    REVIEW_POLICY_MODES,
    CheckOutcome,
    ReviewCheckPublisher,
    check_outcome_for_result,
    review_check_outcome_for_publication_failure,
)
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
APPROVAL_ELIGIBILITY_MARKER_PREFIX = "<!-- reviewsensei:eligibility:v1"
GIT_SHA_HEX = re.compile(r"^[a-f0-9]{40}$")
PUBLISHED_REVIEW_STATES = frozenset({"COMMENTED", "APPROVED", "CHANGES_REQUESTED"})
DISCUSSION_INSTRUCTION = (
    "To discuss this finding, reply with @sensei followed by your question."
)
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
              path
              line
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
_FINDING_MARKER_V2_RE = re.compile(
    r"<!-- reviewsensei:finding:v2 repo=(?P<repository_id>[1-9][0-9]*) "
    r"pr=(?P<pull_request>[1-9][0-9]*) head=(?P<head_sha>[a-f0-9]{40}) "
    r"base=(?P<base_sha>[a-f0-9]{40}) fingerprint=(?P<fingerprint>[a-f0-9]{64}) "
    r"state=(?P<state>new|still-present|fixed|outdated|uncertain) "
    r"blocking=(?P<blocking>true|false) -->"
)


def _publication_anchor(comment: ReviewComment, analysis: DiffAnalysis) -> str:
    if comment.side == "LEFT":
        if comment.line in analysis.deleted_lines.get(comment.path, frozenset()):
            return "left"
        return "summary"
    if comment.side == "FILE" and comment.path in analysis.changed_paths:
        return "file"
    if comment.side == "RIGHT":
        if comment.line in analysis.changed_lines.get(comment.path, frozenset()):
            return "right"
    return "summary"


def format_coverage_digest(coverage: CoverageManifest) -> str:
    counts: dict[str, int] = {}
    for entry in coverage.files:
        counts[entry.outcome] = counts.get(entry.outcome, 0) + 1
    parts = [
        f"{counts.get('reviewed', 0)} reviewed",
        f"{counts.get('partially-reviewed', 0)} partially-reviewed",
        f"{counts.get('excluded-by-policy', 0)} excluded-by-policy",
        f"{counts.get('unsupported', 0)} unsupported",
        f"{counts.get('budget-exhausted', 0)} budget-exhausted",
    ]
    enumeration = "complete" if coverage.enumeration_complete else "incomplete"
    return f"Coverage: {', '.join(parts)}. Enumeration {enumeration}."


def format_unanchored_findings(
    comments: tuple[ReviewComment, ...],
    *,
    heading: str = "## Findings without a publishable inline location",
) -> str:
    lines = [heading]
    for comment in comments:
        path = escape_markdown_label(comment.path)
        rendered = format_review_comment(comment)
        if "\n\n" in rendered:
            labels, body = rendered.split("\n\n", 1)
            lines.append(f"- `{path}`: {labels} {escape_markdown_label(body)}")
        else:
            lines.append(f"- `{path}`: {escape_markdown_label(comment.body)}")
    return "\n".join(lines)


def _with_discussion_instruction(text: str) -> str:
    return f"{text}\n\n{DISCUSSION_INSTRUCTION}"


def review_result_digest(result: ReviewResult) -> str:
    """Return the canonical digest that binds a decision to one review result."""

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
    digest = review_result_digest(result)
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
        f"head={head_sha} base={base_sha} result={review_result_digest(result)} "
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


def finding_blocking_from_body(body: object) -> bool | None:
    """Return the persisted blocking bit from a v1 or v2 finding marker."""

    if not isinstance(body, str):
        return None
    match = _FINDING_MARKER_V2_RE.search(body)
    if match is None:
        match = _FINDING_MARKER_RE.search(body)
    if match is None:
        return None
    return match.group("blocking") == "true"


@dataclass(frozen=True)
class PublishedFindingSuppression:
    """Indexes already-published findings for duplicate suppression."""

    by_fingerprint: Mapping[str, bool]
    by_location: Mapping[tuple[str, int], bool]


def _should_suppress_published_finding(
    comment: ReviewComment,
    fingerprint: str,
    suppression: PublishedFindingSuppression,
) -> bool:
    """Return whether an already-published root makes this inline comment redundant."""

    existing = suppression.by_fingerprint.get(fingerprint)
    if existing is not None:
        return existing == comment.blocks_approval
    if comment.line is not None:
        location = suppression.by_location.get((comment.path, comment.line))
        if location is not None:
            return location == comment.blocks_approval
    return False


def approval_marker(
    *, repository_id: int, pull_request: int, head_sha: str, base_sha: str
) -> str:
    """Return the idempotency marker for one final approval decision."""

    return (
        f"{APPROVAL_MARKER_PREFIX} repo={repository_id} pr={pull_request} "
        f"head={head_sha} base={base_sha} -->"
    )


_APPROVAL_ELIGIBILITY_RE = re.compile(
    re.escape(APPROVAL_ELIGIBILITY_MARKER_PREFIX)
    + r" (?P<payload>[A-Za-z0-9_-]+={0,2}) -->"
)


def approval_eligibility_marker(eligibility: ReviewApprovalEligibility) -> str:
    """Encode one eligibility document as a hidden, bounded review-body marker.

    The document is JSON, then base64url so it stays single-line and cannot
    close the HTML comment early. It is persisted beside the published review
    because the delayed finalization path (a reply-driven resolution) runs in a
    different process from the review that produced the evidence.
    """

    if not isinstance(eligibility, ReviewApprovalEligibility):
        raise GitHubPublicationError("approval eligibility is invalid")
    canonical = json.dumps(
        eligibility.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    payload = base64.urlsafe_b64encode(canonical.encode("utf-8")).decode("ascii")
    marker = f"{APPROVAL_ELIGIBILITY_MARKER_PREFIX} {payload} -->"
    try:
        validate_bounded_text(
            marker,
            MAX_PUBLISHED_REVIEW_BODY_BYTES,
            label="approval eligibility marker",
            allow_empty=False,
        )
    except ReviewInputError as exc:
        raise GitHubPublicationError(
            "approval eligibility exceeds the configured publication limit"
        ) from exc
    return marker


def approval_eligibility_from_body(body: object) -> ReviewApprovalEligibility | None:
    """Read back one persisted eligibility document, failing closed to ``None``.

    A missing, unreadable, or malformed document returns ``None``: every caller
    withholds approval rather than inferring eligibility from a partial read.
    """

    if not isinstance(body, str):
        return None
    match = _APPROVAL_ELIGIBILITY_RE.search(body)
    if match is None:
        return None
    # The marker carries base64url text, whose padding is re-added here so both
    # a padded and a stripped payload decode to the same document.
    payload = match.group("payload").rstrip("=")
    try:
        raw = base64.urlsafe_b64decode(
            (payload + "=" * (-len(payload) % 4)).encode("ascii")
        )
        document = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return None
    try:
        return ReviewApprovalEligibility.from_dict(document)
    except ReviewInputError:
        return None


def _keep_operator_inline_thread(
    policy: ReviewConvergencePolicy, comment: ReviewComment
) -> bool:
    """Return whether an operator-mode finding stays as an inline thread.

    Admitted blockers remain inline whenever automatic GitHub review events
    are enabled, including when ``auto_approve`` is False (ADR 0035
    comment-only). Non-blocking observations fold into the summary so
    required conversation resolution cannot turn optional notes into
    mechanical blockers. ``auto_approve`` is not part of this predicate.
    """

    return policy.automatic_github_review_events and comment.blocks_approval


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
    diagnostic: str | None = None
    shadow: Mapping[str, object] | None = None
    # A terminal no-progress handoff exposes the durable identity so an
    # at-least-once caller can distinguish persisted suppression from retryable
    # publication failure.
    transaction_id: str | None = None
    generation: int | None = None


_PUBLICATION_TO_RUN_STATUS = {
    "published": "reviewed",
    "approved": "reviewed",
    "changes_requested": "reviewed",
    "already_published": "already_published",
    "already_changes_requested": "already_published",
    "already_approved": "already_published",
    "skipped_stale_head": "skipped_stale",
    "skipped_stale_base": "skipped_stale",
    "skipped_pr_state": "skipped_policy",
    "skipped_repository_mismatch": "skipped_policy",
    "skipped_fork": "skipped_policy",
    "skipped_app_authored": "skipped_policy",
    "auto_approval_disabled": "skipped_policy",
    "disabled": "skipped_policy",
    "handoff": "action_required",
}

PUBLICATION_RESULT_STATUSES = frozenset(_PUBLICATION_TO_RUN_STATUS)

_PUBLICATION_DIAGNOSTIC_OVERRIDES = {
    "disabled": "writes_disabled",
    "skipped_fork": "fork_not_allowed",
}


def outcome_from_publication(
    result: PublicationResult,
    *,
    repository: str | None = None,
    pull_request_number: int | None = None,
    base_sha: str | None = None,
    head_sha: str | None = None,
    diagnostic: str | None = None,
) -> RunOutcome:
    """Project a GitHub publication result onto the public run-outcome contract."""

    status = _PUBLICATION_TO_RUN_STATUS.get(result.status, "publication_failed")
    token = diagnostic
    if token is None:
        token = getattr(result, "diagnostic", None)
    if token is None:
        candidate = _PUBLICATION_DIAGNOSTIC_OVERRIDES.get(result.status, result.status)
        if candidate in PUBLIC_DIAGNOSTICS:
            token = candidate
        elif status == "publication_failed":
            token = "publication_failed"
    return RunOutcome(
        status,
        repository=repository,
        pull_request_number=pull_request_number,
        base_sha=base_sha,
        head_sha=head_sha,
        diagnostic=sanitize_diagnostic(token),
    )


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


class ReviewApprovalFinalizer:
    """Converge an eligible exact-head PR to one App approval decision.

    This is deliberately independent of a provider result. It reads durable
    per-finding classifications from review-thread roots, so it can be invoked
    both after review publication and after an AI resolution mutation. The
    stable ReviewSensei check is the only imposed merge gate (ADR 0057): this
    finalizer never emits ``REQUEST_CHANGES``, and it emits ``APPROVE`` only
    from a trusted eligibility document whose exact head matches the reviewed
    head and whose live thread state is still clean.

    A concurrent blocking run still wins over an earlier approval: a newly
    visible App blocking root on the same head withholds the approval, and an
    existing ReviewSensei change request is never silently cleared while its
    blocking roots remain unresolved.
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
        eligibility: ReviewApprovalEligibility,
    ) -> PublicationResult:
        """Emit at most one exact-head APPROVE from trusted eligibility.

        The eligibility document is the only source of approval facts. A
        document for another head, or one whose static facts already withhold,
        is reported before any thread read so a withheld approval costs no
        mutations. The live thread scan is authoritative for the remaining
        blockers: an unresolved App blocking root on this exact head always
        withholds.
        """

        if not isinstance(eligibility, ReviewApprovalEligibility):
            raise GitHubPublicationError("approval eligibility is invalid")
        if not GIT_SHA_HEX.fullmatch(head_sha):
            raise GitHubPublicationError("review head sha is invalid")
        if eligibility.head_sha != head_sha:
            return PublicationResult(
                status="approval_withheld", diagnostic="approval_withheld"
            )
        # Evaluate the persisted facts with the thread state assumed clean so
        # every non-thread blocker is reported before any network read.
        static = evaluate_approval_facts(
            replace(eligibility.facts, has_open_review_threads=False)
        )
        if "auto-approval-disabled" in static.blockers:
            return PublicationResult(status="auto_approval_disabled")
        if not static.approved:
            return PublicationResult(
                status="approval_withheld",
                diagnostic=approval_withheld_diagnostic(static),
            )
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
        open_blocking = self._scan_blocking_threads(
            token=token,
            repository=repository,
            pull_request=pull_request,
            repository_id=preflight.repository_id,
            head_sha=head_sha,
            base_sha=preflight.base_sha,
            app_slug=app_slug,
        )
        # The thread scan can paginate, so bind the write to the same exact PR
        # identity immediately before emitting APPROVE.
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
        decision = eligibility.evaluate(
            app_authored=write_preflight.app_authored,
            has_open_review_threads=open_blocking,
        )
        if not decision.approved:
            return PublicationResult(
                status="approval_withheld",
                diagnostic=approval_withheld_diagnostic(decision),
            )
        reviews = self._load_head_reviews(
            token=token, repository=repository, pull_request=pull_request
        )
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
    ) -> bool:
        """Return whether this exact head still has an unresolved App blocking root."""

        owner, separator, name = repository.partition("/")
        if not separator or not owner or not name:
            raise GitHubPublicationError("review thread repository is invalid")
        after: str | None = None
        open_blocking = False
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
            has_next = page_info.get("hasNextPage")
            if not isinstance(has_next, bool):
                raise GitHubPublicationError("review thread response was invalid")
            if not has_next:
                return open_blocking
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

    def load_eligibility(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        head_sha: str,
        app_slug: str,
    ) -> ReviewApprovalEligibility | None:
        """Read the persisted eligibility document for one reviewed head.

        A delayed finalization (a resolved thread) has no access to the
        publishing process, so it re-reads the document that publication
        persisted beside this exact head. Only an App-authored review on this
        head can carry it, and a missing, malformed, or other-head document
        returns ``None`` so the caller withholds instead of trusting a claim it
        cannot verify.
        """

        if not GIT_SHA_HEX.fullmatch(head_sha):
            raise GitHubPublicationError("review head sha is invalid")
        if not isinstance(app_slug, str) or not app_slug.strip():
            raise GitHubPublicationError("review app slug is invalid")
        reviews = self._load_head_reviews(
            token=token, repository=repository, pull_request=pull_request
        )
        expected = app_slug.casefold()
        for review in reversed(reviews):
            if not isinstance(review, dict):
                continue
            if review.get("commit_id") != head_sha:
                continue
            user = review.get("user")
            login = user.get("login") if isinstance(user, dict) else None
            if not isinstance(login, str) or login.casefold() != expected:
                continue
            eligibility = approval_eligibility_from_body(review.get("body"))
            if eligibility is not None and eligibility.head_sha == head_sha:
                return eligibility
        return None

    def _reviews_have_app_head_state(
        self,
        reviews: list[Any],
        *,
        head_sha: str,
        app_slug: str,
        state: str,
        body: str | None = None,
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
        )


def prepare_publication_review(
    *,
    result: ReviewResult,
    diff: str,
    head_sha: str,
    candidates: Sequence[CandidateFinding] | None = None,
    snapshot: Mapping[str, str] | None = None,
    snapshot_sha256: str | None = None,
    evidence_policy: str = "legacy",
    convergence_policy: ReviewConvergencePolicy | None = None,
    blocker_candidates: Sequence[BlockerCandidate] | None = None,
    input_blocker_candidates: Sequence[BlockerCandidate] | None = None,
    baseline: ReviewBaseline | None = None,
    current_key: ReviewContextCacheKey | None = None,
    changed_paths: Sequence[str] | None = None,
    related_paths: Sequence[str] = (),
    evidence_confirmed_concerns: Sequence[str] = (),
    authorized_dispositions: Sequence[object] = (),
) -> PublishableReview:
    """Prepare one publication artifact independently of the HTTP publisher.

    The application uses this same admission routine for publisher adapters
    that expose only ``publish``. Keeping preparation here ensures operator
    F3 admission is never skipped merely because an adapter lacks the
    optional ``ReviewPublisher.prepare`` convenience method.
    """

    try:
        analysis = analyze_diff(diff)
        prepared = prepare_publishable_review(
            result,
            candidates=candidates,
            snapshot=snapshot,
            snapshot_sha256=snapshot_sha256,
            evidence_policy=evidence_policy,
            changed_lines=analysis.changed_lines,
            deleted_lines=analysis.deleted_lines,
            convergence_policy=convergence_policy,
            blocker_candidates=blocker_candidates,
            input_blocker_candidates=input_blocker_candidates,
            baseline=baseline,
            current_key=current_key,
            changed_paths=changed_paths,
            related_paths=related_paths,
            evidence_confirmed_concerns=evidence_confirmed_concerns,
            authorized_dispositions=authorized_dispositions,
            current_head_sha=head_sha,
        )
        return replace(
            prepared,
            diff_sha256=hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        )
    except ReviewInputError as exc:
        raise GitHubPublicationError("review evidence verification failed") from exc


class ReviewPublisher:
    """Publish one validated review as App-authored inline comments and summary."""

    def __init__(self, *, http: GitHubHttp) -> None:
        self.http = http
        self.finalizer = ReviewApprovalFinalizer(http=http)
        self.checks = ReviewCheckPublisher(http=http)

    def prepare(
        self,
        *,
        result: ReviewResult,
        diff: str,
        head_sha: str,
        candidates: Sequence[CandidateFinding] | None = None,
        snapshot: Mapping[str, str] | None = None,
        snapshot_sha256: str | None = None,
        evidence_policy: str = "legacy",
        convergence_policy: ReviewConvergencePolicy | None = None,
        blocker_candidates: Sequence[BlockerCandidate] | None = None,
        input_blocker_candidates: Sequence[BlockerCandidate] | None = None,
        baseline: ReviewBaseline | None = None,
        current_key: ReviewContextCacheKey | None = None,
        changed_paths: Sequence[str] | None = None,
        related_paths: Sequence[str] = (),
        evidence_confirmed_concerns: Sequence[str] = (),
        authorized_dispositions: Sequence[object] = (),
    ) -> PublishableReview:
        """Admit the exact result that a later call to :meth:`publish` emits."""
        return prepare_publication_review(
            result=result,
            diff=diff,
            head_sha=head_sha,
            candidates=candidates,
            snapshot=snapshot,
            snapshot_sha256=snapshot_sha256,
            evidence_policy=evidence_policy,
            convergence_policy=convergence_policy,
            blocker_candidates=blocker_candidates,
            input_blocker_candidates=input_blocker_candidates,
            baseline=baseline,
            current_key=current_key,
            changed_paths=changed_paths,
            related_paths=related_paths,
            evidence_confirmed_concerns=evidence_confirmed_concerns,
            authorized_dispositions=authorized_dispositions,
        )

    def _publish_check_state(
        self,
        *,
        check_token: str | None,
        repository: str,
        head_sha: str,
        app_slug: str,
        outcome: CheckOutcome | None,
    ) -> str | None:
        """Write one gate state for a reviewed head and report an unusable capability.

        ``outcome=None`` publishes the pending state. A missing or unauthorized
        check capability is reported as a ``check_permission`` diagnostic rather
        than raised: the review itself is still published, and the withheld
        approval tells the operator that enforcement is not in place.
        """

        if check_token is None:
            return "check_permission"
        if outcome is None:
            result = self.checks.start(
                token=check_token,
                repository=repository,
                head_sha=head_sha,
                app_slug=app_slug,
            )
        else:
            result = self.checks.complete(
                token=check_token,
                repository=repository,
                head_sha=head_sha,
                app_slug=app_slug,
                outcome=outcome,
            )
        if result.status == "check_permission_denied":
            return "check_permission"
        return None

    def _check_state_or_diagnostic(
        self,
        *,
        check_token: str | None,
        repository: str,
        head_sha: str,
        app_slug: str,
        outcome: CheckOutcome | None,
    ) -> str | None:
        """Write one gate state, reporting an unusable capability as a diagnostic.

        A gate that cannot be written must not suppress the review itself: the
        review content is what the operator needs, and the withheld approval
        plus the ``check_permission`` diagnostic tell them enforcement is not in
        place instead of implying it. A pending conclusion left behind by a
        transient failure is overwritten by the next run for the same head.
        """

        try:
            return self._publish_check_state(
                check_token=check_token,
                repository=repository,
                head_sha=head_sha,
                app_slug=app_slug,
                outcome=outcome,
            )
        except (GitHubHTTPError, GitHubPublicationError):
            return "check_permission"

    def _complete_failed_check(
        self,
        *,
        check_token: str | None,
        repository: str,
        head_sha: str,
        app_slug: str,
        policy: str,
        transient: bool,
    ) -> None:
        """Publish the non-passing gate state for one failed publication attempt.

        A failure of this write is not raised: the caller is already failing
        with the publication error, and the next attempt for the same head
        republishes the same head-bound gate state.
        """

        self._check_state_or_diagnostic(
            check_token=check_token,
            repository=repository,
            head_sha=head_sha,
            app_slug=app_slug,
            outcome=review_check_outcome_for_publication_failure(
                policy=policy, transient=transient
            ),
        )

    def _finalize_published_result(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        head_sha: str,
        app_slug: str,
        check_token: str | None,
        result: ReviewResult,
        policy: str,
        approval_enabled: bool,
        qualification: str,
    ) -> PublicationResult:
        """Re-assert one already-published result's gate and approval decision.

        The gate is published for every reviewed head, including one whose
        review already exists, so an interrupted earlier run cannot leave a
        pending conclusion standing for this result. App-authored pull requests
        never reach this helper: the preflight reports them before publication.
        The reported diagnostic is the one a fresh publication would report, so
        a repeated run explains its withholding the same way.
        """

        check_diagnostic = self._check_state_or_diagnostic(
            check_token=check_token,
            repository=repository,
            head_sha=head_sha,
            app_slug=app_slug,
            outcome=check_outcome_for_result(result, policy=policy),
        )
        finalized = self.finalizer.finalize(
            token=token,
            repository=repository,
            pull_request=pull_request,
            head_sha=head_sha,
            app_slug=app_slug,
            eligibility=approval_eligibility_from_result(
                result,
                head_sha=head_sha,
                enabled=approval_enabled,
                app_authored=False,
                qualification=qualification,
                check_published=check_diagnostic is None,
            ),
        )
        diagnostic = check_diagnostic
        if diagnostic is None and finalized.status == "approval_withheld":
            diagnostic = finalized.diagnostic
        return PublicationResult(status="already_published", diagnostic=diagnostic)

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
        reviews_policy: str = "auto-approve",
        check_token: str | None = None,
        qualification: str = "not-required",
        candidates: Sequence[CandidateFinding] | None = None,
        snapshot: Mapping[str, str] | None = None,
        snapshot_sha256: str | None = None,
        evidence_policy: str = "legacy",
        convergence_policy: ReviewConvergencePolicy | None = None,
        allow_retired_legacy_policy: bool = False,
        blocker_candidates: Sequence[BlockerCandidate] | None = None,
        input_blocker_candidates: Sequence[BlockerCandidate] | None = None,
        baseline: ReviewBaseline | None = None,
        current_key: ReviewContextCacheKey | None = None,
        changed_paths: Sequence[str] | None = None,
        related_paths: Sequence[str] = (),
        evidence_confirmed_concerns: Sequence[str] = (),
        authorized_dispositions: Sequence[object] = (),
        prepared_review: PublishableReview | None = None,
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
        # Omitted policies use the same deterministic default as preparation;
        # runtime configuration is resolved by the application, not from
        # ambient environment variables at this publication boundary. The
        # historical legacy policy is retired (ADR 0055): a low-level embedder
        # or replay that still needs it must ask for it explicitly, so a
        # future caller cannot re-enable legacy by passing the policy alone.
        if convergence_policy is None:
            convergence_policy = ReviewConvergencePolicy()
        elif not isinstance(convergence_policy, ReviewConvergencePolicy):
            raise GitHubPublicationError("review convergence policy is invalid")
        if not isinstance(allow_retired_legacy_policy, bool):
            raise GitHubPublicationError(
                "review legacy policy opt-in must be a boolean"
            )
        if (
            convergence_policy.mode == LEGACY_REVIEW_MODE
            and not allow_retired_legacy_policy
        ):
            raise GitHubPublicationError(
                "the legacy review mode is retired and cannot be published "
                "without allow_retired_legacy_policy=True"
            )
        # Operator modes may only withhold GitHub review events. The
        # conjunction cannot promote auto_approve=False to REQUEST_CHANGES
        # or APPROVE, including when REVIEWSENSEI_REVIEW_MODE is merge-focused.
        auto_approve = (
            auto_approve and convergence_policy.automatic_github_review_events
        )
        if reviews_policy not in REVIEW_POLICY_MODES:
            raise GitHubPublicationError("review policy mode is invalid")
        # An operator mode that withholds GitHub review events is advisory by
        # construction, whatever the configured policy says.
        effective_policy = (
            reviews_policy
            if convergence_policy.automatic_github_review_events
            else "advisory"
        )
        approval_enabled = auto_approve and effective_policy == "auto-approve"
        if prepared_review is not None:
            if not isinstance(prepared_review, PublishableReview):
                raise GitHubPublicationError("prepared review is invalid")
            if prepared_review.result.content_digest() != result.content_digest():
                raise GitHubPublicationError("prepared review does not match result")
            rebound = self.prepare(
                result=result,
                diff=diff,
                head_sha=head_sha,
                candidates=candidates,
                snapshot=snapshot,
                snapshot_sha256=snapshot_sha256,
                evidence_policy=evidence_policy,
                convergence_policy=convergence_policy,
                blocker_candidates=blocker_candidates,
                input_blocker_candidates=input_blocker_candidates,
                baseline=baseline,
                current_key=current_key,
                changed_paths=changed_paths,
                related_paths=related_paths,
                evidence_confirmed_concerns=evidence_confirmed_concerns,
                authorized_dispositions=authorized_dispositions,
            )
            if rebound != prepared_review:
                raise GitHubPublicationError(
                    "prepared review does not match publication context"
                )
            prepared = rebound
        else:
            prepared = self.prepare(
                result=result,
                diff=diff,
                head_sha=head_sha,
                candidates=candidates,
                snapshot=snapshot,
                snapshot_sha256=snapshot_sha256,
                evidence_policy=evidence_policy,
                convergence_policy=convergence_policy,
                blocker_candidates=blocker_candidates,
                input_blocker_candidates=input_blocker_candidates,
                baseline=baseline,
                current_key=current_key,
                changed_paths=changed_paths,
                related_paths=related_paths,
                evidence_confirmed_concerns=evidence_confirmed_concerns,
                authorized_dispositions=authorized_dispositions,
            )
        result = prepared.result
        analysis = self._validate_locations(result, diff)
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
                        return self._finalize_published_result(
                            token=token,
                            repository=repository,
                            pull_request=pull_request,
                            head_sha=head_sha,
                            app_slug=app_slug,
                            check_token=check_token,
                            result=result,
                            policy=effective_policy,
                            approval_enabled=approval_enabled,
                            qualification=qualification,
                        )
                    return PublicationResult(status="already_published")
            else:
                if "APPROVED" in identity_states:
                    return PublicationResult(status="already_published")
                if identity_states & {"COMMENTED", "CHANGES_REQUESTED"}:
                    if not preflight.app_authored:
                        return self._finalize_published_result(
                            token=token,
                            repository=repository,
                            pull_request=pull_request,
                            head_sha=head_sha,
                            app_slug=app_slug,
                            check_token=check_token,
                            result=result,
                            policy=effective_policy,
                            approval_enabled=approval_enabled,
                            qualification=qualification,
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
        # An old success on this head must not stand in for the review that is
        # about to be published, so the pending gate state is written first and
        # concluded before the review body is composed. The persisted
        # eligibility document therefore agrees with the diagnostic this run
        # reports; a failed publication overrides the conclusion below. An
        # App-authored pull request receives no gate: its review is
        # informational and this run never approves it.
        if write_preflight.app_authored:
            check_diagnostic: str | None = "app_authored"
        else:
            check_diagnostic = self._check_state_or_diagnostic(
                check_token=check_token,
                repository=repository,
                head_sha=head_sha,
                app_slug=app_slug,
                outcome=None,
            )
            if check_diagnostic is None:
                check_diagnostic = self._check_state_or_diagnostic(
                    check_token=check_token,
                    repository=repository,
                    head_sha=head_sha,
                    app_slug=app_slug,
                    outcome=check_outcome_for_result(result, policy=effective_policy),
                )
        eligibility = approval_eligibility_from_result(
            result,
            head_sha=head_sha,
            enabled=approval_enabled,
            app_authored=write_preflight.app_authored,
            qualification=qualification,
            check_published=check_diagnostic is None,
        )
        try:
            summary = format_review_summary(result.summary, result.comments)
            if result.coverage_mode != "full":
                summary = f"{summary}\n\nCoverage mode: {result.coverage_mode}."
            if result.coverage is not None:
                summary = f"{summary}\n\n{format_coverage_digest(result.coverage)}"
            lifecycle_by_fingerprint = {
                item.fingerprint: item.state for item in result.finding_lifecycles
            }
            prepared_comments: list[tuple[ReviewComment, str, str, str]] = []
            unanchored: list[ReviewComment] = []
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
                anchor = _publication_anchor(comment, analysis)
                if anchor == "summary":
                    unanchored.append(comment)
                    continue
                prepared_comments.append(
                    (comment, lifecycle.fingerprint, comment_body, anchor)
                )
            advisory_folded: list[ReviewComment] = []
            if not convergence_policy.inline_advisory_threads:
                kept_inline: list[tuple[ReviewComment, str, str, str]] = []
                for entry in prepared_comments:
                    if _keep_operator_inline_thread(convergence_policy, entry[0]):
                        kept_inline.append(entry)
                    else:
                        advisory_folded.append(entry[0])
                prepared_comments = kept_inline
            # The batch create-review input type defines no file subject type:
            # a live probe on a COMMENT review rejects a file-level entry with
            # HTTP 422 ("Field is not defined on DraftPullRequestReviewComment",
            # "0.position Expected value to not be null"), and an isolating
            # probe with identical coordinates rejects only the entry carrying
            # subject_type ("Field is not defined on
            # DraftPullRequestReviewThread") while the same entry without it
            # returns HTTP 200 COMMENTED, so a finding anchored to the file
            # itself is always retained in the summary rather than published as
            # an inline thread.
            file_level = [entry[0] for entry in prepared_comments if entry[3] == "file"]
            if file_level:
                unanchored.extend(file_level)
                prepared_comments = [
                    entry for entry in prepared_comments if entry[3] != "file"
                ]
            if unanchored:
                summary = (
                    f"{summary}\n\n{format_unanchored_findings(tuple(unanchored))}"
                )
            if advisory_folded:
                heading = (
                    "## Review observations"
                    if not convergence_policy.automatic_github_review_events
                    else "## Advisory observations"
                )
                summary = (
                    f"{summary}\n\n"
                    f"{format_unanchored_findings(tuple(advisory_folded), heading=heading)}"
                )
            validate_bounded_text(
                summary,
                result.limits.max_summary_bytes,
                label="published review summary",
                allow_empty=False,
            )
            body = (
                f"{_with_discussion_instruction(summary)}\n\n{marker}\n\n"
                f"{approval_eligibility_marker(eligibility)}"
            )
            validate_bounded_text(
                body,
                MAX_PUBLISHED_REVIEW_BODY_BYTES,
                label="published review body",
                allow_empty=False,
            )
        except ReviewInputError as exc:
            raise GitHubPublicationError(
                "formatted review exceeds the configured publication limit"
            ) from exc
        suppression = PublishedFindingSuppression({}, {})
        if prepared_comments:
            try:
                suppression = self._published_finding_suppression(
                    token=token,
                    repository=repository,
                    pull_request=pull_request,
                    app_slug=app_slug,
                )
            except GitHubPublicationError as exc:
                # Installations without GraphQL thread access can still publish;
                # duplicate suppression is skipped rather than blocking every
                # review when the entitlement surface is unavailable.
                if "was not permitted" not in str(exc):
                    raise
        comments = []
        for comment, fingerprint, comment_body, anchor in prepared_comments:
            if _should_suppress_published_finding(comment, fingerprint, suppression):
                continue
            comment_payload: dict[str, object] = {
                "path": comment.path,
                "body": comment_body,
            }
            if anchor == "left":
                comment_payload["line"] = comment.line
                comment_payload["side"] = "LEFT"
            else:
                comment_payload["line"] = comment.line
                comment_payload["side"] = "RIGHT"
            comments.append(comment_payload)
        # The stable ReviewSensei check run is the only imposed merge gate
        # (ADR 0057). The review itself is always published as a COMMENT so a
        # persistent REQUEST_CHANGES cannot become a second, stale gate, and an
        # empty inline payload can never misrepresent a same-head re-review.
        event, published_state = "COMMENT", "COMMENTED"
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
                return self._finalize_after_publication(
                    token=token,
                    repository=repository,
                    pull_request=pull_request,
                    head_sha=head_sha,
                    app_slug=app_slug,
                    eligibility=eligibility,
                    check_diagnostic=check_diagnostic,
                    status="already_published",
                )
            self._complete_failed_check(
                check_token=check_token,
                repository=repository,
                head_sha=head_sha,
                app_slug=app_slug,
                policy=effective_policy,
                transient=True,
            )
            raise GitHubPublicationTransientError(
                "review publication failed temporarily"
            ) from exc
        if status == 200 and isinstance(payload, dict):
            review_id = payload.get("id")
            if isinstance(review_id, int):
                return self._finalize_after_publication(
                    token=token,
                    repository=repository,
                    pull_request=pull_request,
                    head_sha=head_sha,
                    app_slug=app_slug,
                    eligibility=eligibility,
                    check_diagnostic=check_diagnostic,
                    status="published",
                    review_id=review_id,
                )
        if status == 404:
            self._complete_failed_check(
                check_token=check_token,
                repository=repository,
                head_sha=head_sha,
                app_slug=app_slug,
                policy=effective_policy,
                transient=False,
            )
            raise GitHubPublicationError("review target was not found")
        if status == 403:
            self._complete_failed_check(
                check_token=check_token,
                repository=repository,
                head_sha=head_sha,
                app_slug=app_slug,
                policy=effective_policy,
                transient=False,
            )
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
                return self._finalize_after_publication(
                    token=token,
                    repository=repository,
                    pull_request=pull_request,
                    head_sha=head_sha,
                    app_slug=app_slug,
                    eligibility=eligibility,
                    check_diagnostic=check_diagnostic,
                    status="already_published",
                )
            self._complete_failed_check(
                check_token=check_token,
                repository=repository,
                head_sha=head_sha,
                app_slug=app_slug,
                policy=effective_policy,
                transient=False,
            )
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
                return self._finalize_after_publication(
                    token=token,
                    repository=repository,
                    pull_request=pull_request,
                    head_sha=head_sha,
                    app_slug=app_slug,
                    eligibility=eligibility,
                    check_diagnostic=check_diagnostic,
                    status="already_published",
                )
            self._complete_failed_check(
                check_token=check_token,
                repository=repository,
                head_sha=head_sha,
                app_slug=app_slug,
                policy=effective_policy,
                transient=True,
            )
            raise GitHubPublicationTransientError(
                "review publication failed temporarily"
            )
        self._complete_failed_check(
            check_token=check_token,
            repository=repository,
            head_sha=head_sha,
            app_slug=app_slug,
            policy=effective_policy,
            transient=False,
        )
        raise GitHubPublicationError("review publication was rejected")

    def _finalize_after_publication(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        head_sha: str,
        app_slug: str,
        eligibility: ReviewApprovalEligibility,
        check_diagnostic: str | None,
        status: str,
        review_id: int | None = None,
    ) -> PublicationResult:
        """Finalize approval for one published head and report its withholding.

        The persisted eligibility document is the only approval input, so an
        app-authored pull request withholds here as well: the finalizer reports
        it before any network read. A ``check_diagnostic`` wins over the
        finalizer's reason because an unpublished gate is an enforcement gap
        that must be reported rather than folded into a quieter explanation.
        """

        finalized = self.finalizer.finalize(
            token=token,
            repository=repository,
            pull_request=pull_request,
            head_sha=head_sha,
            app_slug=app_slug,
            eligibility=eligibility,
        )
        diagnostic = check_diagnostic
        if diagnostic is None and finalized.status == "approval_withheld":
            diagnostic = finalized.diagnostic
        return PublicationResult(
            status=status,
            review_id=review_id,
            diagnostic=diagnostic,
        )

    def _published_finding_suppression(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        app_slug: str,
    ) -> PublishedFindingSuppression:
        """Return already-published finding indexes for duplicate suppression.

        Human comments are ignored. v2 markers index by fingerprint; legacy v1
        markers index by inline path and line until installations roll forward.
        Suppression applies only when the persisted blocking bit matches the
        current comment, so a reclassification still publishes an update.
        """

        owner, separator, name = repository.partition("/")
        if not separator or not owner or not name:
            raise GitHubPublicationError("review thread repository is invalid")
        by_fingerprint: dict[str, bool] = {}
        by_location: dict[tuple[str, int], bool] = {}
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
                body = root.get("body")
                blocking = finding_blocking_from_body(body)
                if blocking is None:
                    continue
                fingerprint = finding_fingerprint_from_body(body)
                if fingerprint is not None:
                    by_fingerprint[fingerprint] = blocking
                    continue
                path = root.get("path")
                line = root.get("line")
                if (
                    isinstance(path, str)
                    and isinstance(line, int)
                    and line > 0
                    and _FINDING_MARKER_RE.search(body if isinstance(body, str) else "")
                ):
                    by_location[(path, line)] = blocking
            has_next = page_info.get("hasNextPage")
            if not isinstance(has_next, bool):
                raise GitHubPublicationError("review thread response was invalid")
            if not has_next:
                return PublishedFindingSuppression(by_fingerprint, by_location)
            after = page_info.get("endCursor")
            if not isinstance(after, str) or not after:
                raise GitHubPublicationError("review thread response was invalid")
        raise GitHubPublicationError(
            "review thread pagination exceeded configured limit"
        )

    def _validate_locations(self, result: ReviewResult, diff: str) -> DiffAnalysis:
        try:
            analysis = analyze_diff(diff)
        except ReviewInputError as exc:
            raise GitHubPublicationError("review diff failed validation") from exc
        return analysis

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
