"""Exact-head-bound, idempotent App-authored review publication."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Mapping, Sequence
from urllib.parse import quote

from review_sensei.baseline import ReviewBaseline
from review_sensei.bounded_evidence import MAX_PART_READS, EvidenceReadBudget
from review_sensei.context import ReviewContextCacheKey, finding_lifecycle_for_comment
from review_sensei.convergence import (
    LEGACY_REVIEW_MODE,
    BlockerCandidate,
    ReviewConvergencePolicy,
)
from review_sensei.coverage import CoverageManifest, coverage_approval_state
from review_sensei.diff import DiffAnalysis, analyze_diff
from review_sensei.errors import ReviewInputError
from review_sensei.human_assessment import (
    PendingHumanReview,
    finding_instance_fingerprint,
)
from review_sensei.models import ReviewComment, ReviewResult
from review_sensei.outcomes import PUBLIC_DIAGNOSTICS, RunOutcome, sanitize_diagnostic
from review_sensei.placement import (
    BODY_PLACEMENT,
    BODY_REASON_OVERSIZED,
    CONVERSATION_RESOLUTION_NOT_REQUIRED,
    CONVERSATION_RESOLUTION_REQUIRED,
    INLINE_PLACEMENT,
    HostPlacementFacts,
    placement_note,
    plan_finding_placement_detail,
)
from review_sensei.presentation import (
    ADVISORY_STATE,
    CHANGES_REQUIRED_STATE,
    INCOMPLETE_STATE,
    NO_REQUIRED_FIXES_STATE,
    OPTIONAL_FINDING,
    WITHHELD_STATE,
    FindingView,
    append_response_instructions,
    assign_finding_identifiers,
    build_finding_view,
    build_review_summary_view,
    render_body_findings,
    render_check_permission_warning,
    render_finding,
    render_review_summary,
)
from review_sensei.validation import validate_bounded_text
from review_sensei.verifier import (
    CandidateFinding,
    PublishableReview,
    prepare_publishable_review,
)

from .approval import (
    ReviewApprovalEligibility,
    approval_eligibility_from_result,
    approval_withheld_diagnostic,
    evaluate_approval_facts,
    has_blocking_findings,
    has_human_adjudication_findings,
    restrict_result_qualification,
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
    GitHubHTTPPaginationLimitError,
    GitHubHTTPTransientError,
    GitHubPublicationError,
    GitHubPublicationTransientError,
)
from .http import MAX_PAGINATION_PAGES, GitHubHttp

if TYPE_CHECKING:
    from .default_human_inventory import HumanInventoryReference, HumanInventoryStore
    from .eligibility_parts import (
        PartitionedEligibilityContext,
        PartitionReader,
        VisibleReader,
    )

REVIEW_MARKER_PREFIX = "<!-- reviewsensei:review:v1"
FINDING_MARKER_PREFIX = "<!-- reviewsensei:finding:v1"
FINDING_MARKER_PREFIX_V2 = "<!-- reviewsensei:finding:v2"
FINDING_INSTANCE_MARKER_PREFIX = "<!-- reviewsensei:instance:v1"
_FINDING_INSTANCE_RE = re.compile(
    re.escape(FINDING_INSTANCE_MARKER_PREFIX)
    + r" instance=(?P<instance>[a-f0-9]{64}) concern=(?P<concern>[a-f0-9]{64}) -->"
)
APPROVAL_MARKER_PREFIX = "<!-- reviewsensei:approval:v1"
APPROVAL_ELIGIBILITY_MARKER_PREFIX = "<!-- reviewsensei:eligibility:v1"
GIT_SHA_HEX = re.compile(r"^[a-f0-9]{40}$")
PUBLISHED_REVIEW_STATES = frozenset({"COMMENTED", "APPROVED", "CHANGES_REQUESTED"})
DISCUSSION_INSTRUCTION = (
    "To discuss a finding, reply in its thread with @reviewsensei and your question. "
    "For a body-only finding, use a PR comment naming its RS-id."
)
# GitHub's review body limit is independent of the provider-neutral summary
# profile. The formatted summary is checked against ReviewLimits first, then
# the complete body (including framing and the idempotency marker) is checked
# against this transport ceiling before any write.
MAX_PUBLISHED_REVIEW_BODY_BYTES = 65_536
MAX_REVIEW_THREAD_PAGES = 10
RESERVED_RECONCILIATION_READS = 4
ORDINARY_EVIDENCE_READ_LIMIT = MAX_PART_READS - RESERVED_RECONCILIATION_READS

_REVIEW_THREADS_QUERY = """
query ReviewThreads($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $after) {
        nodes {
          id
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
    r"blocking=(?P<blocking>true|false)(?: human=(?P<human>true|false))? -->"
)
FINDING_INDEX_MARKER_PREFIX = "<!-- reviewsensei:finding-index:v1"
MAX_INDEXED_FINDINGS = 250
LEGACY_MAX_INDEXED_FINDINGS = 64
_FINDING_INDEX_ENTRY_BYTES = 65
_FINDING_INDEX_BLOCKING = 0x01
_FINDING_INDEX_NEEDS_HUMAN = 0x02
_FINDING_INDEX_BODY = 0x04
_FINDING_INDEX_OPTIONAL = 0x08
FINDING_INDEX_LIMITATION = "Finding IDs cannot be overridden by comment on this review."
_FINDING_INDEX_RE = re.compile(
    r"<!-- reviewsensei:finding-index:v1 repo=(?P<repository_id>[1-9][0-9]*) "
    r"pr=(?P<pull_request>[1-9][0-9]*) head=(?P<head_sha>[a-f0-9]{40}) "
    r"base=(?P<base_sha>[a-f0-9]{40}) result=(?P<result>[a-f0-9]{64}) "
    r"entries=(?P<entries>[A-Za-z0-9_-]*) -->"
)
_OVERRIDE_PROVENANCE_RE = re.compile(
    r"<!-- reviewsensei:override-provenance:v1 actor=(?P<actor>[A-Za-z0-9-]{1,39}) "
    r"head=(?P<head_sha>[a-f0-9]{40}) ids=(?P<ids>RS-[0-9A-F]{6,16}(?:,RS-[0-9A-F]{6,16})*) -->"
)


@dataclass(frozen=True)
class IndexedFinding:
    """One displayed finding bound to the review that published its RS- ID."""

    instance: str
    lifecycle: str | None
    blocking: bool
    needs_human: bool
    body_placed: bool = False
    overridable: bool = True


@dataclass(frozen=True)
class FindingIndex:
    """Head-bound inventory of every displayed finding ID."""

    result_digest: str
    base_sha: str
    entries: tuple[IndexedFinding, ...]


class _FindingIndexError(GitHubPublicationError):
    def __init__(self, check: str, message: str) -> None:
        self.check = check
        super().__init__(message)


def finding_index_marker(
    *,
    repository_id: int,
    pull_request: int,
    head_sha: str,
    base_sha: str,
    result_digest: str,
    entries: Sequence[IndexedFinding],
    complete_inventory: bool = False,
) -> str:
    """Encode displayed finding identities. The prefix is not a v2 finding marker."""

    if not isinstance(complete_inventory, bool):
        raise GitHubPublicationError("complete inventory writer gate is invalid")
    if (
        type(repository_id) is not int
        or repository_id <= 0
        or type(pull_request) is not int
        or pull_request <= 0
        or any(
            not isinstance(value, str)
            or re.fullmatch(f"[a-f0-9]{{{width}}}", value) is None
            for value, width in ((head_sha, 40), (base_sha, 40), (result_digest, 64))
        )
    ):
        raise _FindingIndexError(
            "finding-index-identity", "finding index scope is invalid"
        )
    maximum = (
        MAX_INDEXED_FINDINGS if complete_inventory else LEGACY_MAX_INDEXED_FINDINGS
    )
    if len(entries) > maximum:
        raise _FindingIndexError(
            "finding-index-count",
            "finding index count exceeds the enabled writer limit",
        )
    payload = bytearray()
    seen: set[str] = set()
    for entry in entries:
        if (
            not isinstance(entry, IndexedFinding)
            or any(
                not isinstance(value, str)
                or re.fullmatch(r"[a-f0-9]{64}", value) is None
                for value in (entry.instance, entry.lifecycle)
            )
            or any(
                not isinstance(value, bool)
                for value in (
                    entry.blocking,
                    entry.needs_human,
                    entry.body_placed,
                    entry.overridable,
                )
            )
        ):
            raise _FindingIndexError(
                "finding-index-identity",
                "finding index identity or classification is invalid",
            )
        if entry.instance in seen:
            raise _FindingIndexError(
                "finding-index-identity", "finding index identity is duplicated"
            )
        seen.add(entry.instance)
        if entry.lifecycle is None:
            raise GitHubPublicationError("finding index lifecycle is missing")
        try:
            payload += bytes.fromhex(entry.instance)
            payload += bytes.fromhex(entry.lifecycle)
        except ValueError as exc:
            raise GitHubPublicationError("finding index identity is invalid") from exc
        flags = 0
        if entry.blocking:
            flags |= _FINDING_INDEX_BLOCKING
        if entry.needs_human:
            flags |= _FINDING_INDEX_NEEDS_HUMAN
        if entry.body_placed:
            flags |= _FINDING_INDEX_BODY
        if not entry.overridable:
            flags |= _FINDING_INDEX_OPTIONAL
        payload.append(flags)
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return (
        f"{FINDING_INDEX_MARKER_PREFIX} repo={repository_id} pr={pull_request} "
        f"head={head_sha} base={base_sha} result={result_digest} entries={encoded} -->"
    )


def parse_finding_index(
    body: object,
    *,
    repository_id: int,
    pull_request: int,
    head_sha: str,
) -> FindingIndex | None:
    """Read this review's finding index.

    A missing marker returns ``None`` so legacy reviews can use their fallback.
    A marker for another repo, pull request, or head is not this review's index.
    A marker that claims this head and does not decode fails closed.
    """

    if not isinstance(body, str) or FINDING_INDEX_MARKER_PREFIX not in body:
        return None
    if body.count(FINDING_INDEX_MARKER_PREFIX) != 1:
        raise ReviewInputError("current review inventory is incomplete")
    match = _FINDING_INDEX_RE.search(body)
    if match is None:
        raise ReviewInputError("current review inventory is incomplete")
    if (
        int(match.group("repository_id")) != repository_id
        or int(match.group("pull_request")) != pull_request
        or match.group("head_sha") != head_sha
    ):
        return None
    encoded = match.group("entries")
    if len(encoded) > (MAX_INDEXED_FINDINGS * _FINDING_INDEX_ENTRY_BYTES * 4 + 2) // 3:
        raise ReviewInputError("current review inventory is incomplete")
    try:
        raw = base64.urlsafe_b64decode(
            (encoded + "=" * (-len(encoded) % 4)).encode("ascii")
        )
    except (ValueError, binascii.Error) as exc:
        raise ReviewInputError("current review inventory is incomplete") from exc
    if base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != encoded:
        raise ReviewInputError("current review inventory is incomplete")
    if len(raw) % _FINDING_INDEX_ENTRY_BYTES:
        raise ReviewInputError("current review inventory is incomplete")
    count = len(raw) // _FINDING_INDEX_ENTRY_BYTES
    if count > MAX_INDEXED_FINDINGS:
        raise ReviewInputError("current review inventory is incomplete")
    entries: list[IndexedFinding] = []
    seen: set[str] = set()
    for offset in range(0, len(raw), _FINDING_INDEX_ENTRY_BYTES):
        chunk = raw[offset : offset + _FINDING_INDEX_ENTRY_BYTES]
        instance = chunk[:32].hex()
        lifecycle = chunk[32:64].hex()
        flags = chunk[64]
        if flags & ~0x0F or instance in seen:
            raise ReviewInputError("current review inventory is incomplete")
        seen.add(instance)
        entries.append(
            IndexedFinding(
                instance=instance,
                lifecycle=lifecycle,
                blocking=bool(flags & _FINDING_INDEX_BLOCKING),
                needs_human=bool(flags & _FINDING_INDEX_NEEDS_HUMAN),
                body_placed=bool(flags & _FINDING_INDEX_BODY),
                overridable=not bool(flags & _FINDING_INDEX_OPTIONAL),
            )
        )
    return FindingIndex(
        result_digest=match.group("result"),
        base_sha=match.group("base_sha"),
        entries=tuple(entries),
    )


def override_provenance_marker(
    *, actor: str, head_sha: str, finding_ids: Sequence[str]
) -> str:
    """Record who overrode which displayed IDs on this head."""

    if not re.fullmatch(r"[A-Za-z0-9-]{1,39}", actor):
        raise ReviewInputError("maintainer actor is invalid")
    if not finding_ids or any(
        not re.fullmatch(r"RS-[0-9A-F]{6,16}", item) for item in finding_ids
    ):
        raise ReviewInputError("override provenance ids are invalid")
    return (
        "<!-- reviewsensei:override-provenance:v1 "
        f"actor={actor} head={head_sha} ids={','.join(finding_ids)} -->"
    )


def _reject_conflicting_instances(comments: Sequence[ReviewComment]) -> None:
    """Reject one instance whose approval classification is not unique.

    Preparation must not keep whichever comment arrived first. The instance is
    path, line, side, and explanation; blocking and human assessment are the
    classification.
    """

    seen: dict[str, tuple[bool, bool]] = {}
    for comment in comments:
        classification = (bool(comment.blocks_approval), bool(comment.needs_human))
        instance = finding_instance_fingerprint(comment)
        previous = seen.get(instance)
        if previous is not None and previous != classification:
            raise GitHubPublicationError("conflicting finding classifications")
        seen[instance] = classification


def _publication_anchor(comment: ReviewComment, analysis: DiffAnalysis) -> str:
    if comment.path in analysis.binary_paths:
        return "summary"
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


def _placement_needs_host_facts(
    comment: ReviewComment,
    *,
    anchor: str,
    policy: ReviewConvergencePolicy,
    advisory: bool,
) -> bool:
    """Return whether conversation-resolution facts can change this placement.

    Required findings stay inline and unanchored findings stay in the body
    regardless of branch rules, so host facts are only read when an optional,
    anchored finding could actually become a thread.
    """

    if advisory or comment.needs_human or comment.blocks_approval:
        return False
    if anchor not in ("left", "right"):
        return False
    return policy.inline_advisory_threads


def _coverage_line(result: ReviewResult) -> str:
    if result.coverage is not None:
        line = format_coverage_digest(result.coverage).rstrip(".")
    else:
        line = "Coverage: complete within the configured review scope"
    if result.coverage_mode != "full":
        line = f"{line}. Coverage mode: {result.coverage_mode}"
    return line


def _review_summary_state(
    result: ReviewResult,
    *,
    policy: ReviewConvergencePolicy,
    auto_approve: bool,
) -> tuple[str, str | None]:
    """Classify the published summary without overstating enforcement.

    A summary never claims an approval: the approval itself is a separate
    review operation whose result is only knowable afterwards, so a clean
    eligible review states that no required fixes were found and leaves the
    approval claim to the approval review.
    """

    if result.persistence_status == "capacity-exceeded":
        return (
            WITHHELD_STATE,
            "analysis completed, but complete baseline persistence exceeded its capacity; published findings remain actionable and approval requires a durable complete baseline",
        )
    if not policy.automatic_github_review_events:
        return ADVISORY_STATE, None
    if has_blocking_findings(result):
        return CHANGES_REQUIRED_STATE, None
    incomplete_reason = _incomplete_reason(result)
    if incomplete_reason is not None:
        return INCOMPLETE_STATE, incomplete_reason
    if has_human_adjudication_findings(result):
        # A finding labeled for human assessment is not a proven defect, but
        # it is also not a clean review: the summary must not claim that no
        # required fixes were found while a maintainer decision is pending.
        return WITHHELD_STATE, "a required human assessment is pending"
    if not _coverage_complete(result):
        return WITHHELD_STATE, "coverage was not complete for every changed file"
    if auto_approve:
        return NO_REQUIRED_FIXES_STATE, None
    return WITHHELD_STATE, "automatic approval is disabled for this repository"


def _incomplete_reason(result: ReviewResult) -> str | None:
    if result.coverage_only_partial and result.coverage is not None:
        from review_sensei.human_file_review import human_confirmation_reason

        return human_confirmation_reason(result.coverage)
    if result.review_status != "complete":
        return f"the review reported status {result.review_status}"
    coverage = result.coverage
    if coverage is not None and not coverage.enumeration_complete:
        return "coverage enumeration was incomplete"
    return None


def _coverage_complete(result: ReviewResult) -> bool:
    """Whether coverage supports an approval decision for every changed file."""

    return (
        result.coverage is None
        or coverage_approval_state(result.coverage) == "reviewed"
    )


def _approval_eligible(result: ReviewResult) -> bool:
    """Whether this result may converge to an automatic APPROVE.

    The criteria match the independently computed round flags: a complete
    review, no blocking findings, no pending human assessment, and complete
    coverage. A partial or failed analysis must never be published as if a
    maintainer had approved it.
    """

    return (
        _incomplete_reason(result) is None
        and not has_blocking_findings(result)
        and not has_human_adjudication_findings(result)
        and _coverage_complete(result)
    )


def _review_limitations(
    result: ReviewResult, views: Sequence[FindingView]
) -> tuple[str, ...]:
    """Operational limitations stay separate from code defects."""

    limitations: list[str] = []
    reason = _incomplete_reason(result)
    if reason is not None:
        limitations.append(f"Incomplete analysis: {reason}.")
    coverage = result.coverage
    if coverage is not None:
        exhausted = sum(
            1 for entry in coverage.files if entry.outcome == "budget-exhausted"
        )
        if exhausted:
            limitations.append(
                f"Per-run resource limits were reached for {exhausted} file(s); "
                "those files were not fully reviewed."
            )
    for view in views:
        if view.needs_human:
            limitations.append(
                f"Required human assessment: {view.identifier} "
                "is labeled for human assessment rather than asserted as a proven defect."
            )
    return tuple(limitations)


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


def _with_discussion_instruction(text: str) -> str:
    return append_response_instructions(f"{text}\n\n{DISCUSSION_INSTRUCTION}")


def _framed_review_body(
    summary: str,
    *,
    review_marker: str,
    index_marker: str | None,
    eligibility_marker: str,
) -> str:
    body = f"{_with_discussion_instruction(summary)}\n\n{review_marker}"
    if index_marker:
        body = f"{body}\n\n{index_marker}"
    return f"{body}\n\n{eligibility_marker}"


def _framed_body_fits(
    summary: str,
    *,
    review_marker: str,
    index_marker: str | None,
    eligibility_marker: str,
    summary_limit: int | None,
) -> bool:
    if summary_limit is not None and len(summary.encode("utf-8")) > summary_limit:
        return False
    framed = _framed_review_body(
        summary,
        review_marker=review_marker,
        index_marker=index_marker,
        eligibility_marker=eligibility_marker,
    )
    return len(framed.encode("utf-8")) <= MAX_PUBLISHED_REVIEW_BODY_BYTES


class PublicationLimitError(GitHubPublicationError):
    """A publication ceiling failed. The message names the failing check."""

    def __init__(self, check: str) -> None:
        self.check = check
        messages = {
            "published-summary": (
                "published review summary exceeds the configured publication limit"
            ),
            "framed-review-body": (
                "framed review body exceeds the configured publication limit"
            ),
            "formatted-comment": (
                "formatted comment exceeds the configured publication limit"
            ),
            "finding-index": (
                "finding index metadata exceeds the configured publication limit"
            ),
            "finding-index-count": (
                "finding index count exceeds the enabled writer limit; "
                "complete inventory publication requires compatible readers"
            ),
            "finding-index-identity": "finding index identity or classification is invalid",
        }
        super().__init__(
            messages.get(
                check, "formatted review exceeds the configured publication limit"
            )
        )


def _index_marker_for_budget(
    entries: Sequence[IndexedFinding],
    *,
    summary: str,
    review_marker: str,
    eligibility_marker: str,
    repository_id: int,
    pull_request: int,
    head_sha: str,
    base_sha: str,
    result_digest: str,
    summary_limit: int | None = None,
) -> tuple[str, str]:
    """Return the finding index. It is not dropped to make a body fit."""

    try:
        marker = finding_index_marker(
            repository_id=repository_id,
            pull_request=pull_request,
            head_sha=head_sha,
            base_sha=base_sha,
            result_digest=result_digest,
            entries=entries,
        )
    except GitHubPublicationError as exc:
        raise PublicationLimitError("finding-index") from exc
    if not _framed_body_fits(
        summary,
        review_marker=review_marker,
        index_marker=marker,
        eligibility_marker=eligibility_marker,
        summary_limit=summary_limit,
    ):
        summary_over = (
            summary_limit is not None and len(summary.encode("utf-8")) > summary_limit
        )
        if summary_over:
            raise PublicationLimitError("published-summary")
        minimum = _framed_review_body(
            "",
            review_marker=review_marker,
            index_marker=marker,
            eligibility_marker=eligibility_marker,
        )
        if len(minimum.encode("utf-8")) > MAX_PUBLISHED_REVIEW_BODY_BYTES:
            raise PublicationLimitError("finding-index")
        raise PublicationLimitError("framed-review-body")
    return marker, summary


_SEVERITY_DROP_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


@dataclass(frozen=True)
class BodyExplanation:
    """One body explanation that may be omitted only when it is optional."""

    instance: str
    severity: str | None
    obligatory: bool
    view: FindingView


@dataclass(frozen=True)
class AssembledReview:
    """Placement-final finding inventory before the framed body is fitted."""

    inline_comments: tuple[tuple[ReviewComment, str, str, str], ...]
    index_entries: tuple[IndexedFinding, ...]
    head_sha: str
    state: str
    reason: str | None
    overview: str
    coverage_line: str | None
    required_views: tuple[FindingView, ...]
    optional_views: tuple[FindingView, ...]
    limitations: tuple[str, ...]
    placement_note: str | None
    body_explanations: tuple[BodyExplanation, ...]
    inline_blocker_instances: tuple[str, ...]
    requests_changes: bool
    compact_references: bool = False


def _render_assembled_summary(
    assembled: AssembledReview,
    *,
    omitted_identifiers: frozenset[str],
    kept_explanations: Sequence[BodyExplanation],
    suffix: str = "",
) -> str:
    def marked(view: FindingView) -> FindingView:
        if view.identifier in omitted_identifiers:
            return replace(view, explanation_omitted=True)
        return view

    limitations = assembled.limitations
    if assembled.compact_references:
        repeated_human_guidance = {
            f"Required human assessment: {view.identifier} "
            "is labeled for human assessment rather than asserted as a proven defect."
            for view in (*assembled.required_views, *assembled.optional_views)
            if view.needs_human
        }
        limitations = tuple(
            item for item in limitations if item not in repeated_human_guidance
        )
        if repeated_human_guidance:
            limitations += (
                "Every required decision listed above needs human assessment; "
                "these findings are not asserted as proven defects.",
            )
    summary = render_review_summary(
        build_review_summary_view(
            state=assembled.state,
            head_sha=assembled.head_sha,
            overview=assembled.overview,
            coverage_line=assembled.coverage_line,
            required=tuple(marked(view) for view in assembled.required_views),
            optional=tuple(marked(view) for view in assembled.optional_views),
            limitations=limitations,
            placement_note=assembled.placement_note,
            reason=assembled.reason,
            omitted_explanations=len(omitted_identifiers),
        ),
        compact_references=assembled.compact_references,
    )
    prose = tuple(
        item.view
        for item in kept_explanations
        if item.view.identifier not in omitted_identifiers and item.view.detail
    )
    if prose:
        summary = f"{summary}\n\n{render_body_findings(prose)}"
    if suffix:
        summary = f"{summary}\n\n{suffix}"
    return summary


def fit_assembled_review(
    assembled: AssembledReview,
    *,
    review_marker: str,
    eligibility_marker: str,
    repository_id: int,
    pull_request: int,
    head_sha: str,
    base_sha: str,
    result_digest: str,
    summary_limit: int,
    suffix: str = "",
    complete_inventory: bool = False,
    reserved_bytes: int = 0,
    preserve_explanations: bool = False,
) -> tuple[str, str]:
    """Fit the framed body without dropping obligations or the finding index.

    Optional, non-blocking, non-human body prose is removed lowest severity
    first. Blocking and human-assessment explanations stay. The omission count
    is part of the measured body. If the body still cannot fit, publication
    is refused and the error names the failing check. Human inventory callers
    preserve every explanation and reserve the future disposition frame.
    """

    if (
        isinstance(reserved_bytes, bool)
        or not isinstance(reserved_bytes, int)
        or not 0 <= reserved_bytes <= MAX_PUBLISHED_REVIEW_BODY_BYTES
    ):
        raise GitHubPublicationError("publication capacity reserve is invalid")
    try:
        index_marker = finding_index_marker(
            repository_id=repository_id,
            pull_request=pull_request,
            head_sha=head_sha,
            base_sha=base_sha,
            result_digest=result_digest,
            entries=assembled.index_entries,
            complete_inventory=complete_inventory,
        )
    except _FindingIndexError as exc:
        raise PublicationLimitError(exc.check) from exc
    except GitHubPublicationError as exc:
        raise PublicationLimitError("finding-index") from exc
    optional = [
        item
        for item in assembled.body_explanations
        if not item.obligatory and not complete_inventory and not preserve_explanations
    ]
    obligatory = [
        item
        for item in assembled.body_explanations
        if item.obligatory or complete_inventory or preserve_explanations
    ]
    optional.sort(
        key=lambda item: (
            _SEVERITY_DROP_RANK.get((item.severity or "").strip().lower(), -1),
            item.instance,
        )
    )
    omitted: list[str] = []
    while True:
        omitted_ids = frozenset(omitted)
        kept = [*obligatory, *optional]
        summary = _render_assembled_summary(
            assembled,
            omitted_identifiers=omitted_ids,
            kept_explanations=kept,
            suffix=suffix,
        )
        summary_over = len(summary.encode("utf-8")) > summary_limit
        framed = _framed_review_body(
            summary,
            review_marker=review_marker,
            index_marker=index_marker,
            eligibility_marker=eligibility_marker,
        )
        framed_over = (
            len(framed.encode("utf-8")) + reserved_bytes
            > MAX_PUBLISHED_REVIEW_BODY_BYTES
        )
        if not summary_over and not framed_over:
            return summary, index_marker
        if not optional:
            if summary_over:
                raise PublicationLimitError("published-summary")
            minimum = _framed_review_body(
                "",
                review_marker=review_marker,
                index_marker=index_marker,
                eligibility_marker=eligibility_marker,
            )
            if (
                len(minimum.encode("utf-8")) + reserved_bytes
                > MAX_PUBLISHED_REVIEW_BODY_BYTES
            ):
                raise PublicationLimitError("finding-index")
            raise PublicationLimitError("framed-review-body")
        dropped = optional.pop(0)
        omitted.append(dropped.view.identifier)


def _disposition_frame_reserve(
    eligibility: ReviewApprovalEligibility,
    entries: Sequence[IndexedFinding],
) -> int:
    """Reserve the full resolution receipt and one additional maximum command marker.

    A legacy inventory's own decoded limit does not bound its complete review
    frame. Full resolution can grow the eligibility marker after publication.
    Additional historical provenance remains subject to the ordinary body cap;
    it is never erased or treated as evidence of a different head.
    """
    overridable = tuple(entry for entry in entries if entry.overridable)
    if not overridable:
        return 0
    human = eligibility.human_review
    future = replace(
        eligibility,
        human_review=(
            replace(
                human,
                resolved=tuple(item.fingerprint for item in human.findings),
            )
            if human is not None
            else None
        ),
        facts=replace(
            eligibility.facts,
            app_authored=False,
            check_published=False,
            has_blocking_findings=False,
            has_human_adjudication_findings=False,
        ),
    )
    identifiers = assign_finding_identifiers(entry.instance for entry in entries)
    # The command accepts at most ten IDs. Use the longest assigned IDs and
    # longest valid actor, without inventing a persisted provenance record.
    command_ids = tuple(
        sorted(
            (identifiers[entry.instance] for entry in overridable),
            key=lambda identifier: (-len(identifier), identifier),
        )[:10]
    )
    provenance = override_provenance_marker(
        actor="a" * 39, head_sha=eligibility.head_sha, finding_ids=command_ids
    )
    growth = max(
        0,
        len(approval_eligibility_marker(future).encode("utf-8"))
        - len(approval_eligibility_marker(eligibility).encode("utf-8")),
    )
    return growth + len(provenance.encode("utf-8")) + 2


def _published_summary_byte_limit(result: ReviewResult) -> int:
    """Account for finding prose separately from the validated input summary.

    Body-placed findings retain their full text. Charging that text to the
    overview's limit imposes a second, accidental inventory cap. The final
    framed body, including authority and discussion markers, still has to fit
    the host bound. Narrower input-summary limits and prose validation remain.
    """
    return min(
        MAX_PUBLISHED_REVIEW_BODY_BYTES,
        result.limits.max_summary_bytes
        + sum(len(comment.body.encode("utf-8")) for comment in result.comments),
    )


def review_result_digest(result: ReviewResult) -> str:
    """Bind content and the logical attempt, excluding mutable recovery state.

    Unbound results retain their historical canonical hash. Transaction-bound
    results use an explicit projection: transaction_id already binds repository,
    snapshot, policy, configuration, evidence and reservation; content_digest
    binds the frozen result fields. Rebinding publication phase/generation must
    not create a second review identity after a lost POST acknowledgement.
    """

    canonical = json.dumps(
        {
            "domain": "reviewsensei:transaction-publication:v1",
            "transaction_id": result.transaction.transaction_id,
            "result_content_sha256": result.content_digest(),
        }
        if result.transaction is not None
        else result.to_dict(),
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
    continuation_digest: str | None = None,
) -> str:
    digest = review_result_digest(result)
    return (
        f"{review_identity_marker(repository_id=repository_id, pull_request=pull_request, head_sha=head_sha)} "
        f"result={digest} coverage={result.coverage_mode}"
        + (f" continuation={continuation_digest}" if continuation_digest else "")
        + " -->"
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
    needs_human: bool = False,
) -> str:
    """Bind one inline finding's approval classification to this review head."""

    if fingerprint is not None:
        lifecycle_state = state or "new"
        human = " human=true" if needs_human else ""
        return (
            f"{FINDING_MARKER_PREFIX_V2} repo={repository_id} pr={pull_request} "
            f"head={head_sha} base={base_sha} fingerprint={fingerprint} "
            f"state={lifecycle_state} blocking={'true' if blocking else 'false'}"
            f"{human} -->"
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


def finding_instance_marker(comment: ReviewComment) -> str:
    """Bind display/target identity separately from the unchanged concern marker."""
    return (
        f"{FINDING_INSTANCE_MARKER_PREFIX} instance={finding_instance_fingerprint(comment)} "
        f"concern={finding_lifecycle_for_comment(comment).fingerprint} -->"
    )


def finding_instance_from_body(body: object) -> str | None:
    if not isinstance(body, str) or body.count(FINDING_INSTANCE_MARKER_PREFIX) != 1:
        return None
    match = _FINDING_INSTANCE_RE.search(body)
    if match is None or match.group("concern") != finding_fingerprint_from_body(body):
        return None
    return match.group("instance")


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


def finding_publication_scope(
    body: object,
) -> tuple[int, int, str, str, bool, bool | None] | None:
    """Return repo, pull request, head, base, blocking, and human classification.

    ``None`` means the body has no recognizable finding marker. A parsed marker
    for another head is still returned so the caller can tell that scope apart
    from an unreadable root.
    """

    if not isinstance(body, str):
        return None
    match = _FINDING_MARKER_V2_RE.search(body)
    if match is None:
        match = _FINDING_MARKER_RE.search(body)
    if match is None:
        return None
    human = match.groupdict().get("human")
    return (
        int(match.group("repository_id")),
        int(match.group("pull_request")),
        match.group("head_sha"),
        match.group("base_sha"),
        match.group("blocking") == "true",
        None if human is None else human == "true",
    )


@dataclass(frozen=True)
class PublishedInstance:
    """One open or historical App thread bound to a publication scope."""

    blocking: bool
    needs_human: bool | None
    head_sha: str
    base_sha: str
    resolved: bool
    thread_id: str | None = None
    concern: str | None = None


@dataclass(frozen=True)
class PublishedFindingSuppression:
    """Indexes already-published findings for duplicate suppression."""

    by_fingerprint: Mapping[str, bool]
    by_location: Mapping[tuple[str, int], bool]
    by_instance: Mapping[str, PublishedInstance] = field(default_factory=dict)
    superseded_threads: tuple[tuple[str, str], ...] = ()


def _should_suppress_published_finding(
    comment: ReviewComment,
    fingerprint: str,
    suppression: PublishedFindingSuppression,
    *,
    instance: str | None = None,
    head_sha: str | None = None,
    base_sha: str | None = None,
) -> bool:
    """Reuse an open same-head thread only when scope and classification match.

    Instance identity does not include the head. An older head, a resolved
    thread, or a changed blocking or human-assessment classification must not
    suppress a current obligation. A concern fingerprint or path/line alone
    does not prove this explanation.
    """

    if instance is None or head_sha is None or base_sha is None:
        return False
    existing = suppression.by_instance.get(instance)
    if existing is None or existing.resolved:
        return False
    if existing.head_sha != head_sha or existing.base_sha != base_sha:
        return False
    if existing.blocking != comment.blocks_approval:
        return False
    if existing.needs_human is None:
        return not comment.needs_human
    return existing.needs_human == bool(comment.needs_human)


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


def approval_eligibility_from_body(
    body: object,
    *,
    human_inventory_reader: Callable[[HumanInventoryReference], PendingHumanReview]
    | None = None,
    partition_reader: PartitionReader | None = None,
    expected_context: PartitionedEligibilityContext | None = None,
    visible_reader: VisibleReader | None = None,
) -> ReviewApprovalEligibility | None:
    """Read back one persisted eligibility document, failing closed to ``None``.

    A missing, unreadable, or malformed document returns ``None``: every caller
    withholds approval rather than inferring eligibility from a partial read.
    """

    if not isinstance(body, str) or body.count(APPROVAL_ELIGIBILITY_MARKER_PREFIX) != 1:
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
        return ReviewApprovalEligibility.from_dict(
            document,
            human_inventory_reader=human_inventory_reader,
            partition_reader=partition_reader,
            expected_context=expected_context,
            visible_reader=visible_reader,
        )
    except ReviewInputError:
        return None


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
    thread_resolution: str | None = None
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
    "publication_incomplete": "action_required",
}

PUBLICATION_RESULT_STATUSES = frozenset(_PUBLICATION_TO_RUN_STATUS)

_PUBLICATION_DIAGNOSTIC_OVERRIDES = {
    "disabled": "writes_disabled",
    "skipped_fork": "fork_not_allowed",
    "publication_incomplete": "publication_ambiguous",
}


def outcome_from_publication(
    result: PublicationResult,
    *,
    repository: str | None = None,
    pull_request_number: int | None = None,
    base_sha: str | None = None,
    head_sha: str | None = None,
    diagnostic: str | None = None,
    review_result: ReviewResult | None = None,
) -> RunOutcome:
    """Project a GitHub publication result onto the public run-outcome contract."""

    status = _PUBLICATION_TO_RUN_STATUS.get(result.status, "publication_failed")
    if (
        status in {"reviewed", "already_published"}
        and review_result is not None
        and (
            review_result.review_status != "complete"
            or not _coverage_complete(review_result)
        )
    ):
        status = "partial"
    token = diagnostic
    if token is None:
        token = getattr(result, "diagnostic", None)
    if token is None:
        candidate = _PUBLICATION_DIAGNOSTIC_OVERRIDES.get(result.status, result.status)
        if candidate in PUBLIC_DIAGNOSTICS:
            token = candidate
        elif status == "publication_failed":
            token = "publication_failed"
        elif status == "partial":
            token = "partial_coverage"
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


class _BudgetedReviewPagination(GitHubHttp):
    """Use the existing adaptive pager with caller-owned dispatch accounting."""

    def __init__(self, http: GitHubHttp, budget: EvidenceReadBudget) -> None:
        self.transport = http
        self.budget = budget

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str,
        body: dict[str, object] | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[int, dict[str, Any] | list[Any] | None]:
        try:
            if getattr(self.transport, "publication_budget", None) is self.budget:
                result = self.transport.request(
                    method,
                    path,
                    token=token,
                    body=body,
                    timeout_seconds=timeout_seconds,
                )
                self.budget.check()
                return result
            remaining = self.budget.consume()
            result = self.transport.request(
                method,
                path,
                token=token,
                body=body,
                timeout_seconds=min(remaining, timeout_seconds)
                if timeout_seconds is not None
                else remaining,
            )
            self.budget.check()
            return result
        except ReviewInputError:
            raise GitHubHTTPPaginationLimitError(
                "review scan shared budget exhausted"
            ) from None


class _ScopedHumanInventoryReader:
    """Bind one lazy reader to independently supplied operation scope and budget."""

    def __init__(
        self,
        factory: Callable[..., Callable[[HumanInventoryReference], PendingHumanReview]],
        *,
        token: str,
        repository: str,
        pull_request: int,
        head_sha: str,
        app_slug: str,
        budget: EvidenceReadBudget,
    ) -> None:
        self.factory = factory
        self.scope = {
            "token": token,
            "repository": repository,
            "pull_request": pull_request,
            "head_sha": head_sha,
            "app_slug": app_slug,
            "budget": budget,
        }
        self.budget = budget
        self.reader: Callable[[HumanInventoryReference], PendingHumanReview] | None = (
            None
        )

    def _reader(self) -> Callable[[HumanInventoryReference], PendingHumanReview]:
        if self.reader is None:
            self.budget.check()
            reader = self.factory(**self.scope)
            if not callable(reader):
                raise ReviewInputError(
                    "human inventory reader factory returned invalid reader"
                )
            self.reader = reader
        return self.reader

    def __call__(self, reference: HumanInventoryReference) -> PendingHumanReview:
        self.budget.check()
        inventory = self._reader()(reference)
        self.budget.check()
        return inventory

    def validate_root(self, review: dict[str, Any], reviews: list[Any]) -> None:
        self.budget.check()
        validate = getattr(self._reader(), "validate_root", None)
        if callable(validate):
            validate(review, reviews)
        self.budget.check()

    def validate_metadata(self, review: dict[str, Any], reviews: list[Any]) -> None:
        """Keep root authority fresh; older readers retain their full validator."""
        self.budget.check()
        reader = self._reader()
        validate = getattr(reader, "validate_metadata", None)
        if not callable(validate):
            validate = getattr(reader, "validate_root", None)
        if callable(validate):
            validate(review, reviews)
        self.budget.check()


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

    def __init__(
        self,
        *,
        http: GitHubHttp,
        evidence_budget: EvidenceReadBudget | None = None,
        human_inventory_reader: Callable[[HumanInventoryReference], PendingHumanReview]
        | None = None,
        human_inventory_reader_factory: Callable[
            ..., Callable[[HumanInventoryReference], PendingHumanReview]
        ]
        | None = None,
        human_inventory_read_token: str | None = None,
    ) -> None:
        if (
            human_inventory_reader is not None
            and human_inventory_reader_factory is not None
        ):
            raise GitHubPublicationError(
                "human inventory reader configuration is ambiguous"
            )
        if human_inventory_read_token is not None and (
            not isinstance(human_inventory_read_token, str)
            or not human_inventory_read_token.strip()
        ):
            raise GitHubPublicationError("human inventory read credential is invalid")
        self.http = http
        self.evidence_budget = evidence_budget
        self.human_inventory_reader = human_inventory_reader
        self.human_inventory_reader_factory = human_inventory_reader_factory
        # An explicitly supplied metadata credential is independent of the
        # capability used for review/check mutations. Never persist this value.
        self.human_inventory_read_token = human_inventory_read_token
        self._operation_budget: EvidenceReadBudget | None = None
        self._human_inventory_scope: tuple[str, int, str, str, str] | None = None
        self._scoped_human_inventory_reader: _ScopedHumanInventoryReader | None = None

    @contextmanager
    def _read_operation(self) -> Iterator[None]:
        owns_budget = self.evidence_budget is None and self._operation_budget is None
        if owns_budget:
            self._operation_budget = EvidenceReadBudget()
        try:
            yield
        finally:
            if owns_budget:
                self._operation_budget = None

    def _human_reader_for_scope(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        head_sha: str,
        app_slug: str,
    ) -> Callable[[HumanInventoryReference], PendingHumanReview] | None:
        if self.human_inventory_reader is not None:
            return self.human_inventory_reader
        factory = self.human_inventory_reader_factory
        if factory is None:
            return None
        reader_token = (
            token
            if self.human_inventory_read_token is None
            else self.human_inventory_read_token
        )
        # A scoped reader retains its transport credential. Reuse only within
        # that credential context; keep the cache discriminator opaque and
        # process-local rather than retaining another raw token in its key.
        scope = (
            repository,
            pull_request,
            head_sha,
            app_slug.casefold(),
            hashlib.sha256(reader_token.encode("utf-8")).hexdigest(),
        )
        budget = self.evidence_budget or self._operation_budget
        cached = self._scoped_human_inventory_reader
        if (
            cached is not None
            and self._human_inventory_scope == scope
            and (budget is None or cached.budget is budget)
        ):
            return cached
        reader = _ScopedHumanInventoryReader(
            factory,
            token=reader_token,
            repository=repository,
            pull_request=pull_request,
            head_sha=head_sha,
            app_slug=app_slug,
            budget=budget or EvidenceReadBudget(),
        )
        self._human_inventory_scope = scope
        self._scoped_human_inventory_reader = reader
        return reader

    def _review_pages(
        self, *, token: str, repository: str, pull_request: int
    ) -> list[Any]:
        # Legacy scans acquire one finite read budget. Partition consumers must
        # supply the same object used for parts/readback/fences; no phase reset.
        budget = self.evidence_budget or self._operation_budget or EvidenceReadBudget()
        return _BudgetedReviewPagination(self.http, budget).paginate(
            path=self.http.repository_path(
                repository, f"/pulls/{pull_request}/reviews"
            ),
            token=token,
            page_sizes=(100, 50, 25, 5, 1),
            max_requests=64,
            timeout_seconds=60,
        )

    def finalize(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        head_sha: str,
        app_slug: str,
        eligibility: ReviewApprovalEligibility,
        require_persisted: bool = False,
        honored_instances: frozenset[str] | None = None,
        assume_inline_unresolved: frozenset[str] | None = None,
        partition_reader: PartitionReader | None = None,
        visible_reader: VisibleReader | None = None,
    ) -> PublicationResult:
        """Emit at most one exact-head APPROVE from trusted eligibility.

        The eligibility document is the only source of approval facts. A
        document for another head, or one whose static facts already withhold,
        is reported before any thread read so a withheld approval costs no
        mutations. The live thread scan is authoritative for the remaining
        blockers: an unresolved App blocking root on this exact head always
        withholds.
        """

        owns_operation_budget = (
            self.evidence_budget is None and self._operation_budget is None
        )
        if owns_operation_budget:
            self._operation_budget = EvidenceReadBudget()
        try:
            if not isinstance(eligibility, ReviewApprovalEligibility):
                raise GitHubPublicationError("approval eligibility is invalid")
            if not GIT_SHA_HEX.fullmatch(head_sha):
                raise GitHubPublicationError("review head sha is invalid")
            if eligibility.head_sha != head_sha:
                return PublicationResult(
                    status="approval_withheld", diagnostic="approval_withheld"
                )
            # Body-placed blockers stay in the persisted flag. Inline blockers are
            # not settled by that flag: a caller that just published them says they
            # are unresolved, and a later finalizer requires a resolved thread.
            honored = honored_instances or frozenset()
            unresolved_inline = frozenset(assume_inline_unresolved or ()) - honored
            static = evaluate_approval_facts(
                replace(
                    eligibility.facts,
                    has_open_review_threads=False,
                    has_blocking_findings=(
                        eligibility.facts.has_blocking_findings
                        or bool(unresolved_inline)
                    ),
                )
            )
            if "auto-approval-disabled" in static.blockers:
                return PublicationResult(status="auto_approval_disabled")
            if not static.approved:
                return PublicationResult(
                    status="approval_withheld",
                    diagnostic=approval_withheld_diagnostic(static),
                )
            if unresolved_inline:
                return PublicationResult(
                    status="approval_withheld", diagnostic="required_fixes_open"
                )
            if eligibility.partitioned_root is not None:
                root = eligibility.partitioned_root
                if (
                    self.evidence_budget is None
                    or root.context.budget is not self.evidence_budget
                    or partition_reader is None
                    or visible_reader is None
                ):
                    return PublicationResult(
                        status="approval_withheld", diagnostic="approval_withheld"
                    )
                try:
                    self.evidence_budget.check()
                except ReviewInputError as exc:
                    raise GitHubPublicationError(
                        "review decision reconciliation failed"
                    ) from exc
                try:
                    root.to_document(eligibility)
                except ReviewInputError:
                    return PublicationResult(
                        status="approval_withheld", diagnostic="approval_withheld"
                    )
                if (
                    eligibility.facts.qualification != "qualified"
                    or eligibility.human_review is None
                    or bool(eligibility.human_review.pending)
                    or root.context.binding.get("repository") != repository
                    or root.context.binding.get("head_sha") != head_sha
                    or eligibility.human_review.base_sha
                    != root.context.binding.get("base_sha")
                ):
                    return PublicationResult(
                        status="approval_withheld", diagnostic="approval_withheld"
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
            assert (
                preflight.repository_id is not None and preflight.base_sha is not None
            )
            if (
                eligibility.human_review is not None
                and eligibility.human_review.base_sha != preflight.base_sha
            ):
                return PublicationResult(status="skipped_stale_base")
            open_blocking, resolved_instances = self._scan_blocking_threads(
                token=token,
                repository=repository,
                pull_request=pull_request,
                repository_id=preflight.repository_id,
                head_sha=head_sha,
                base_sha=preflight.base_sha,
                app_slug=app_slug,
                honored_instances=honored,
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
            if not self._inline_obligations_resolved(
                reviews,
                repository_id=preflight.repository_id,
                pull_request=pull_request,
                head_sha=head_sha,
                app_slug=app_slug,
                resolved_instances=resolved_instances,
                honored_instances=honored,
                eligibility=eligibility,
            ):
                return PublicationResult(
                    status="approval_withheld", diagnostic="required_fixes_open"
                )
            if eligibility.partitioned_root is not None:
                assert partition_reader is not None and visible_reader is not None
                part_budget = eligibility.partitioned_root.context.budget
                try:
                    persisted = self._eligibility_from_reviews(
                        reviews,
                        head_sha=head_sha,
                        app_slug=app_slug,
                        partition_reader=partition_reader,
                        expected_context=eligibility.partitioned_root.context,
                        visible_reader=visible_reader,
                        token=token,
                        repository=repository,
                        pull_request=pull_request,
                    )
                    if (
                        persisted is None
                        and part_budget.calls >= ORDINARY_EVIDENCE_READ_LIMIT
                    ):
                        part_budget.consume()
                    part_budget.check()
                except ReviewInputError as exc:
                    raise GitHubPublicationError(
                        "review decision reconciliation failed"
                    ) from exc
                if (
                    persisted is None
                    or replace(persisted, partitioned_root=eligibility.partitioned_root)
                    != eligibility
                    or persisted.to_dict() != eligibility.to_dict()
                ):
                    return PublicationResult(
                        status="approval_withheld", diagnostic="approval_withheld"
                    )
            else:
                persisted = self._eligibility_from_reviews(
                    reviews,
                    head_sha=head_sha,
                    app_slug=app_slug,
                    token=token,
                    repository=repository,
                    pull_request=pull_request,
                )
                if persisted != eligibility and (
                    require_persisted
                    or eligibility.human_inventory_reference is not None
                    or persisted is not None
                ):
                    return PublicationResult(
                        status="approval_withheld", diagnostic="approval_withheld"
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
        finally:
            if owns_operation_budget:
                self._operation_budget = None

    def refinalize_with_dispositions(
        self,
        *,
        token: str,
        check_token: str | None,
        repository: str,
        repository_id: int,
        pull_request: int,
        head_sha: str,
        app_slug: str,
        actor: str,
        finding_ids: Sequence[str],
        dispositions: Sequence[Any],
        reviews_policy: str = "auto-approve",
    ) -> PublicationResult:
        """Re-evaluate one published head after an override, without a provider call.

        Adjusted eligibility is updated in the original review before approval.
        A failed patch does not submit an approval.
        """
        owns_operation_budget = (
            self.evidence_budget is None and self._operation_budget is None
        )
        if owns_operation_budget:
            self._operation_budget = EvidenceReadBudget()
        try:
            from copy import copy

            from .publication_recovery import publication_http

            budget = self.evidence_budget or self._operation_budget
            assert budget is not None
            operation = copy(self)
            operation.http = publication_http(self.http, budget)
            return operation._refinalize_with_dispositions_impl(
                token=token,
                check_token=check_token,
                repository=repository,
                repository_id=repository_id,
                pull_request=pull_request,
                head_sha=head_sha,
                app_slug=app_slug,
                actor=actor,
                finding_ids=finding_ids,
                dispositions=dispositions,
                reviews_policy=reviews_policy,
            )
        finally:
            if owns_operation_budget:
                self._operation_budget = None

    @staticmethod
    def _command_root_unchanged(reviews, chosen, expected_body, head_sha, app_slug):
        candidates = [
            review
            for review in reviews
            if isinstance(review, dict)
            and review.get("commit_id") == head_sha
            and isinstance(review.get("user"), dict)
            and isinstance(review["user"].get("login"), str)
            and review["user"]["login"].casefold() == app_slug.casefold()
            and type(review.get("id")) is int
            and isinstance(review.get("body"), str)
            and APPROVAL_ELIGIBILITY_MARKER_PREFIX in review["body"]
        ]
        if not candidates:
            return False
        current = max(candidates, key=lambda review: review["id"])
        return (
            current["id"] == chosen["id"]
            and current["body"] == expected_body
            and current.get("user") == chosen.get("user")
            and current.get("state") == chosen.get("state")
        )

    def _refinalize_with_dispositions_impl(
        self,
        *,
        token: str,
        check_token: str | None,
        repository: str,
        repository_id: int,
        pull_request: int,
        head_sha: str,
        app_slug: str,
        actor: str,
        finding_ids: Sequence[str],
        dispositions: Sequence[Any],
        reviews_policy: str = "auto-approve",
    ) -> PublicationResult:
        from .checks import ReviewCheckPublisher, review_check_outcome

        reviews = self._load_head_reviews(
            token=token, repository=repository, pull_request=pull_request
        )
        expected = app_slug.casefold()
        chosen: dict[str, Any] | None = None
        for review in reviews:
            if not isinstance(review, dict) or review.get("commit_id") != head_sha:
                continue
            user = review.get("user")
            login = user.get("login") if isinstance(user, dict) else None
            if not isinstance(login, str) or login.casefold() != expected:
                continue
            review_id = review.get("id")
            body = review.get("body")
            if (
                isinstance(review_id, bool)
                or not isinstance(review_id, int)
                or not isinstance(body, str)
            ):
                continue
            if APPROVAL_ELIGIBILITY_MARKER_PREFIX not in body:
                continue
            if chosen is None or review_id >= int(chosen["id"]):
                chosen = review
        if chosen is None:
            return PublicationResult(
                status="approval_withheld", diagnostic="finding_index_missing"
            )
        body = chosen["body"]
        assert isinstance(body, str)
        eligibility = self._eligibility_from_reviews(
            reviews,
            head_sha=head_sha,
            app_slug=app_slug,
            token=token,
            repository=repository,
            pull_request=pull_request,
        )
        if eligibility is None or eligibility.head_sha != head_sha:
            return PublicationResult(
                status="approval_withheld", diagnostic="finding_index_missing"
            )
        if eligibility.partitioned_root is not None:
            return PublicationResult(
                status="approval_withheld", diagnostic="approval_withheld"
            )
        try:
            index = self.finding_index_from_reviews(
                reviews,
                repository_id=repository_id,
                pull_request=pull_request,
                head_sha=head_sha,
                app_slug=app_slug,
                eligibility=eligibility,
            )
        except (ReviewInputError, GitHubPublicationError):
            return PublicationResult(
                status="gate_reevaluation_failed",
                diagnostic="gate_reevaluation_failed",
            )
        if index is None or index.result_digest != eligibility.result_digest:
            return PublicationResult(
                status="approval_withheld", diagnostic="finding_index_missing"
            )
        active = []
        for item in dispositions:
            item_head = getattr(item, "head_sha", None)
            fingerprint = getattr(item, "fingerprint", None)
            honors = getattr(item, "honors", None)
            if not isinstance(fingerprint, str) or not callable(honors):
                continue
            if honors(fingerprint, head_sha=head_sha):
                active.append(fingerprint)
            elif item_head == head_sha:
                active.append(fingerprint)
        honored = tuple(
            entry
            for entry in index.entries
            if entry.instance in active
            or (entry.lifecycle is not None and entry.lifecycle in active)
        )
        honored_instances = frozenset(entry.instance for entry in honored)
        has_blocking = any(
            entry.blocking
            and entry.body_placed
            and entry.instance not in honored_instances
            for entry in index.entries
        )
        human = eligibility.human_review
        if human is not None:
            identities = {item.fingerprint for item in human.findings}
            resolved = set(human.resolved)
            for entry in honored:
                if entry.needs_human and entry.instance in identities:
                    resolved.add(entry.instance)
            human = replace(human, resolved=tuple(sorted(resolved)))
            has_human = bool(human.pending)
        else:
            has_human = eligibility.facts.has_human_adjudication_findings
        adjusted = replace(
            eligibility,
            human_review=human,
            facts=replace(
                eligibility.facts,
                has_blocking_findings=has_blocking,
                has_human_adjudication_findings=has_human,
            ),
        )
        marker_match = _APPROVAL_ELIGIBILITY_RE.search(body)
        if marker_match is None:
            return PublicationResult(
                status="gate_reevaluation_failed",
                diagnostic="gate_reevaluation_failed",
            )
        new_body = (
            body[: marker_match.start()]
            + approval_eligibility_marker(adjusted)
            + body[marker_match.end() :]
        )
        if finding_ids:
            provenance = override_provenance_marker(
                actor=actor, head_sha=head_sha, finding_ids=finding_ids
            )
            if provenance not in new_body:
                new_body = f"{new_body}\n\n{provenance}"
        compacted = False
        if len(new_body.encode("utf-8")) > MAX_PUBLISHED_REVIEW_BODY_BYTES:
            from .command_capacity import compact_command_references
            from .publication_recovery import publication_http

            budget = self.evidence_budget or self._operation_budget
            assert budget is not None
            capacity_http = publication_http(self.http, budget)
            try:
                status, producer = capacity_http.request(
                    "GET", "/users/" + quote(app_slug, safe=""), token=token
                )
                user = chosen.get("user")
                if (
                    chosen.get("state") not in PUBLISHED_REVIEW_STATES
                    or body.count(FINDING_INDEX_MARKER_PREFIX) != 1
                    or parse_finding_index(
                        body,
                        repository_id=repository_id,
                        pull_request=pull_request,
                        head_sha=head_sha,
                    )
                    != index
                ):
                    raise ReviewInputError("command capacity original index conflicts")
                if (
                    status != 200
                    or not isinstance(producer, dict)
                    or type(producer.get("id")) is not int
                    or producer["id"] <= 0
                    or producer.get("type") != "Bot"
                    or not isinstance(producer.get("login"), str)
                    or producer["login"].casefold() != app_slug.casefold()
                    or not isinstance(user, dict)
                    or type(user.get("id")) is not int
                    or user.get("id") != producer["id"]
                    or user.get("type") != "Bot"
                ):
                    raise ReviewInputError("command capacity root producer conflicts")
                inline = capacity_http.paginate(
                    path=capacity_http.repository_path(
                        repository,
                        f"/pulls/{pull_request}/reviews/{chosen['id']}/comments",
                    ),
                    token=token,
                )
                new_body = compact_command_references(
                    new_body,
                    eligibility=eligibility,
                    index=index,
                    inline=inline,
                    review_id=chosen["id"],
                    producer_id=producer["id"],
                    app_slug=app_slug,
                    pull_request_url=capacity_http.api_url
                    + capacity_http.repository_path(
                        repository, f"/pulls/{pull_request}"
                    ),
                )
                compacted = True
            except (ReviewInputError, GitHubHTTPError, GitHubPublicationError):
                return PublicationResult(
                    status="gate_reevaluation_failed",
                    diagnostic="command_body_capacity_unverifiable",
                )
        live = self._preflight(
            token=token,
            repository=repository,
            pull_request=pull_request,
            head_sha=head_sha,
            app_slug=app_slug,
        )
        if live.result is not None:
            return live.result
        if live.base_sha != index.base_sha or live.repository_id != repository_id:
            return PublicationResult(status="skipped_stale_base")
        if (
            self.load_eligibility(
                token=token,
                repository=repository,
                pull_request=pull_request,
                head_sha=head_sha,
                app_slug=app_slug,
            )
            != eligibility
        ):
            return PublicationResult(
                status="approval_withheld", diagnostic="approval_withheld"
            )
        if compacted:
            fresh = self._load_head_reviews(
                token=token, repository=repository, pull_request=pull_request
            )
            if not self._command_root_unchanged(
                fresh, chosen, body, head_sha, app_slug
            ):
                return PublicationResult(
                    status="approval_withheld", diagnostic="approval_withheld"
                )
        if len(new_body.encode("utf-8")) > MAX_PUBLISHED_REVIEW_BODY_BYTES:
            return PublicationResult(
                status="gate_reevaluation_failed",
                diagnostic="command_body_capacity_exceeded",
            )
        if new_body != body:
            try:
                status, _payload = self.http.request(
                    "PUT",
                    self.http.repository_path(
                        repository, f"/pulls/{pull_request}/reviews/{chosen['id']}"
                    ),
                    token=token,
                    body={"body": new_body},
                )
            except (GitHubHTTPError, GitHubHTTPTransientError):
                if not compacted:
                    return PublicationResult(
                        status="gate_reevaluation_failed",
                        diagnostic="gate_reevaluation_failed",
                    )
                # An uncertain PUT is never reissued. Only exact owned readback
                # of the full intended body permits this operation to continue.
                try:
                    observed = self._load_head_reviews(
                        token=token, repository=repository, pull_request=pull_request
                    )
                except (GitHubHTTPError, GitHubPublicationError, ReviewInputError):
                    observed = []
                if not self._command_root_unchanged(
                    observed, chosen, new_body, head_sha, app_slug
                ):
                    return PublicationResult(
                        status="gate_reevaluation_failed",
                        diagnostic="command_body_capacity_unverifiable",
                    )
                status = 200
            if status != 200:
                return PublicationResult(
                    status="gate_reevaluation_failed",
                    diagnostic="gate_reevaluation_failed",
                )
        if (
            self.load_eligibility(
                token=token,
                repository=repository,
                pull_request=pull_request,
                head_sha=head_sha,
                app_slug=app_slug,
            )
            != adjusted
        ):
            return PublicationResult(
                status="gate_reevaluation_failed", diagnostic="gate_reevaluation_failed"
            )
        if compacted:
            readback = self._load_head_reviews(
                token=token, repository=repository, pull_request=pull_request
            )
            if not self._command_root_unchanged(
                readback, chosen, new_body, head_sha, app_slug
            ):
                return PublicationResult(
                    status="gate_reevaluation_failed",
                    diagnostic="command_body_capacity_unverifiable",
                )
        check_failed = False
        if check_token:
            try:
                ReviewCheckPublisher(http=self.http).complete(
                    token=check_token,
                    repository=repository,
                    head_sha=head_sha,
                    app_slug=app_slug,
                    outcome=review_check_outcome(
                        policy=reviews_policy,
                        review_status=adjusted.facts.review_status,
                        required_fixes=has_blocking,
                        required_decisions=has_human,
                    ),
                )
            except (
                GitHubHTTPError,
                GitHubHTTPTransientError,
                GitHubPublicationError,
                ReviewInputError,
            ):
                check_failed = True
        resolution = self.reconcile_decided_threads(
            token=token,
            repository=repository,
            repository_id=repository_id,
            pull_request=pull_request,
            head_sha=head_sha,
            app_slug=app_slug,
            eligibility=adjusted,
            index=index,
            reviews=reviews,
            honored_instances=honored_instances,
        )
        try:
            finalized = self.finalize(
                token=token,
                repository=repository,
                pull_request=pull_request,
                head_sha=head_sha,
                app_slug=app_slug,
                eligibility=adjusted,
                require_persisted=True,
                honored_instances=honored_instances,
            )
        except (GitHubHTTPError, GitHubPublicationError):
            finalized = PublicationResult(
                status="approval_withheld", diagnostic="approval_withheld"
            )
        thread_res = (
            "unconfirmed"
            if resolution.unconfirmed
            else "resolved"
            if resolution.resolved
            else None
        )
        if check_failed:
            return PublicationResult(
                status="gate_reevaluation_failed",
                diagnostic="check_reconciliation_failed",
                review_id=finalized.review_id,
                thread_resolution=thread_res,
            )
        return replace(
            finalized,
            thread_resolution=thread_res,
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
            return self._review_pages(
                token=token, repository=repository, pull_request=pull_request
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

    def finding_index_from_reviews(
        self,
        reviews: Sequence[Any],
        *,
        repository_id: int,
        pull_request: int,
        head_sha: str,
        app_slug: str,
        eligibility: ReviewApprovalEligibility | None = None,
    ) -> FindingIndex | None:
        """Find the original inventory bound to the latest eligibility result.

        Assessment and approval receipts need not repeat the index. They cannot
        erase an earlier inventory, or reveal an inventory for another result.
        """
        if eligibility is None:
            eligibility = self._eligibility_from_reviews(
                list(reviews),
                head_sha=head_sha,
                app_slug=app_slug,
                require_valid=True,
            )
        indexes = []
        for review in reversed(reviews):
            if (
                not isinstance(review, dict)
                or review.get("commit_id") != head_sha
                or str(review.get("user", {}).get("login", "")).casefold()
                != app_slug.casefold()
            ):
                continue
            body = review.get("body")
            if not isinstance(body, str) or FINDING_INDEX_MARKER_PREFIX not in body:
                continue
            if body.count(FINDING_INDEX_MARKER_PREFIX) != 1:
                raise GitHubPublicationError("finding index is ambiguous")
            index = parse_finding_index(
                body,
                repository_id=repository_id,
                pull_request=pull_request,
                head_sha=head_sha,
            )
            if index is None:
                raise GitHubPublicationError("finding index scope is invalid")
            indexes.append(index)
        if not indexes:
            return None
        if eligibility is None:
            # Without eligibility there is no permission to change the inventory.
            return indexes[0]
        matching = [
            item for item in indexes if item.result_digest == eligibility.result_digest
        ]
        if not matching or any(item != matching[0] for item in matching):
            raise GitHubPublicationError(
                "finding index does not match current eligibility"
            )
        return matching[0]

    def reconcile_decided_threads(
        self,
        *,
        token: str,
        repository: str,
        repository_id: int,
        pull_request: int,
        head_sha: str,
        app_slug: str,
        eligibility: ReviewApprovalEligibility,
        index: FindingIndex | None,
        reviews: Sequence[Any],
        honored_instances: frozenset[str] = frozenset(),
    ):
        """Retry durable override, assessment and addressed decisions without AI."""
        from .thread_resolution import (
            FindingThreadResolver,
            ThreadResolutionResult,
            resolution_records,
        )

        if index is None:
            return ThreadResolutionResult()
        try:
            durable_records = resolution_records(reviews, app_slug)
        except GitHubPublicationError:
            return ThreadResolutionResult(
                unconfirmed=tuple(
                    entry.instance for entry in index.entries if not entry.body_placed
                )
            )
        records = tuple(
            record
            for record in durable_records
            if (
                record["repo"],
                record["pr"],
                record["head"],
                record["base"],
                record["result"],
            )
            == (
                repository_id,
                pull_request,
                head_sha,
                index.base_sha,
                eligibility.result_digest,
            )
        )
        assessed = (
            set(eligibility.human_review.resolved)
            if eligibility.human_review
            else set()
        )
        addressed = {record["instance"] for record in records}
        instances = frozenset(
            entry.instance
            for entry in index.entries
            if not entry.body_placed
            and (
                entry.instance in honored_instances
                or entry.instance in addressed
                or (entry.needs_human and entry.instance in assessed)
            )
        )
        if not instances:
            return ThreadResolutionResult()

        def fence() -> bool:
            live_reviews = self._load_head_reviews(
                token=token, repository=repository, pull_request=pull_request
            )
            current = self._eligibility_from_reviews(
                live_reviews,
                head_sha=head_sha,
                app_slug=app_slug,
                require_valid=True,
                token=token,
                repository=repository,
                pull_request=pull_request,
            )
            current_index = self.finding_index_from_reviews(
                live_reviews,
                repository_id=repository_id,
                pull_request=pull_request,
                head_sha=head_sha,
                app_slug=app_slug,
                eligibility=current,
            )
            live_records = resolution_records(live_reviews, app_slug)
            if current_index != index or any(
                record not in live_records for record in records
            ):
                return False
            live = self._preflight(
                token=token,
                repository=repository,
                pull_request=pull_request,
                head_sha=head_sha,
                app_slug=app_slug,
            )
            return (
                current == eligibility
                and live.result is None
                and live.repository_id == repository_id
                and live.base_sha == index.base_sha
                and not live.app_authored
            )

        try:
            return FindingThreadResolver(self.http).resolve(
                token=token,
                repository=repository,
                repository_id=repository_id,
                pull_request=pull_request,
                head_sha=head_sha,
                base_sha=index.base_sha,
                app_slug=app_slug,
                instances=instances,
                records=records,
                fence=fence,
            )
        except (GitHubHTTPError, GitHubPublicationError, ReviewInputError):
            return ThreadResolutionResult(unconfirmed=tuple(sorted(instances)))

    def finalize_recorded_decisions(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        head_sha: str,
        app_slug: str,
        eligibility: ReviewApprovalEligibility,
        check_token: str | None = None,
        reviews_policy: str = "auto-approve",
        partition_reader: PartitionReader | None = None,
        visible_reader: VisibleReader | None = None,
    ) -> PublicationResult:
        """Keep a recorded decision successful when a later host operation fails."""
        with self._read_operation():
            from .checks import ReviewCheckPublisher, review_check_outcome

            auto_approve_enabled = (
                reviews_policy == "auto-approve" and eligibility.facts.enabled
            )
            resolution = None
            has_open_inline_fixes = False
            try:
                live = self._preflight(
                    token=token,
                    repository=repository,
                    pull_request=pull_request,
                    head_sha=head_sha,
                    app_slug=app_slug,
                )
                if live.result is not None:
                    return live.result
                if live.app_authored:
                    return PublicationResult(status="skipped_app_authored")
                if (
                    eligibility.human_review is not None
                    and eligibility.human_review.base_sha != live.base_sha
                ):
                    return PublicationResult(status="skipped_stale_base")
                assert live.repository_id is not None and live.base_sha is not None
                reviews = self._load_head_reviews(
                    token=token, repository=repository, pull_request=pull_request
                )
                if (
                    eligibility.partitioned_root is None
                    and self._eligibility_from_reviews(
                        reviews,
                        head_sha=head_sha,
                        app_slug=app_slug,
                        token=token,
                        repository=repository,
                        pull_request=pull_request,
                    )
                    != eligibility
                ):
                    return PublicationResult(
                        status="approval_withheld", diagnostic="approval_withheld"
                    )
                index = self.finding_index_from_reviews(
                    reviews,
                    repository_id=live.repository_id,
                    pull_request=pull_request,
                    head_sha=head_sha,
                    app_slug=app_slug,
                    eligibility=eligibility,
                )
                resolution = self.reconcile_decided_threads(
                    token=token,
                    repository=repository,
                    repository_id=live.repository_id,
                    pull_request=pull_request,
                    head_sha=head_sha,
                    app_slug=app_slug,
                    eligibility=eligibility,
                    index=index,
                    reviews=reviews,
                )
                if (
                    check_token
                    and not auto_approve_enabled
                    and index is not None
                    and any(
                        entry.blocking and not entry.body_placed
                        for entry in index.entries
                    )
                ):
                    open_blocking, resolved_instances = self._scan_blocking_threads(
                        token=token,
                        repository=repository,
                        pull_request=pull_request,
                        repository_id=live.repository_id,
                        head_sha=head_sha,
                        base_sha=live.base_sha,
                        app_slug=app_slug,
                        honored_instances=frozenset(),
                    )
                    has_open_inline_fixes = (
                        open_blocking
                        or not self._inline_obligations_resolved(
                            reviews,
                            repository_id=live.repository_id,
                            pull_request=pull_request,
                            head_sha=head_sha,
                            app_slug=app_slug,
                            resolved_instances=resolved_instances,
                            honored_instances=frozenset(),
                            eligibility=eligibility,
                        )
                    )
                if reviews_policy != "auto-approve":
                    result = PublicationResult(status="auto_approval_disabled")
                else:
                    result = self.finalize(
                        token=token,
                        repository=repository,
                        pull_request=pull_request,
                        head_sha=head_sha,
                        app_slug=app_slug,
                        eligibility=eligibility,
                        require_persisted=True,
                        partition_reader=partition_reader,
                        visible_reader=visible_reader,
                    )
            except (GitHubHTTPError, GitHubPublicationError, ReviewInputError):
                result = PublicationResult(
                    status="approval_withheld", diagnostic="approval_withheld"
                )
            if result.status.startswith("skipped_"):
                return result
            check_failed = False
            if check_token:
                has_open_fixes = (
                    eligibility.facts.has_blocking_findings
                    or has_open_inline_fixes
                    or (
                        result.status == "approval_withheld"
                        and result.diagnostic == "required_fixes_open"
                    )
                )
                has_open_decisions = (
                    eligibility.facts.has_human_adjudication_findings
                    or bool(
                        eligibility.human_review and eligibility.human_review.pending
                    )
                )
                try:
                    ReviewCheckPublisher(http=self.http).complete(
                        token=check_token,
                        repository=repository,
                        head_sha=head_sha,
                        app_slug=app_slug,
                        outcome=review_check_outcome(
                            policy=reviews_policy,
                            review_status=eligibility.facts.review_status,
                            required_fixes=has_open_fixes,
                            required_decisions=has_open_decisions,
                        ),
                    )
                except (
                    GitHubHTTPError,
                    GitHubHTTPTransientError,
                    GitHubPublicationError,
                    ReviewInputError,
                ):
                    check_failed = True
            return replace(
                result,
                diagnostic="check_reconciliation_failed"
                if check_failed
                else result.diagnostic,
                thread_resolution=(
                    "unconfirmed"
                    if resolution is None or resolution.unconfirmed
                    else "resolved"
                    if resolution.resolved
                    else None
                ),
            )

    def _inline_obligations_resolved(
        self,
        reviews: Sequence[Any],
        *,
        repository_id: int,
        pull_request: int,
        head_sha: str,
        app_slug: str,
        resolved_instances: frozenset[str],
        honored_instances: frozenset[str],
        eligibility: ReviewApprovalEligibility | None = None,
    ) -> bool:
        """Require every unhonored inline blocker to have a resolved thread.

        A missing index is a legacy review and keeps the thread scan's answer.
        An index that does not decode withholds. A missing thread is not treated
        as resolved. Manual closure does not clear a human-assessment obligation;
        that obligation stays on the eligibility facts.
        """

        try:
            index = self.finding_index_from_reviews(
                reviews,
                repository_id=repository_id,
                pull_request=pull_request,
                head_sha=head_sha,
                app_slug=app_slug,
                eligibility=eligibility,
            )
        except (ReviewInputError, GitHubPublicationError):
            return False
        if index is None:
            # Compatibility only for histories without any finding index.
            return True
        required = {
            entry.instance
            for entry in index.entries
            if entry.blocking
            and not entry.body_placed
            and entry.instance not in honored_instances
        }
        return required <= resolved_instances

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
        honored_instances: frozenset[str] | None = None,
    ) -> tuple[bool, frozenset[str]]:
        """Return unresolved blocking state and instances positively resolved.

        An override names an instance. That thread stays open on GitHub, and its
        marker still says blocking, but it no longer withholds approval. A
        marker that belongs to another head is ignored. An unreadable App root
        withholds.
        """

        owner, separator, name = repository.partition("/")
        if not separator or not owner or not name:
            raise GitHubPublicationError("review thread repository is invalid")
        after: str | None = None
        open_blocking = False
        resolved_instances: set[str] = set()
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
                if (
                    not isinstance(roots, list)
                    or len(roots) != 1
                    or not isinstance(roots[0], dict)
                ):
                    if node["isResolved"]:
                        continue
                    raise GitHubPublicationError("review thread response was invalid")
                root = roots[0]
                author = root.get("author")
                if not isinstance(author, dict) or author.get("login") != app_slug:
                    continue
                body = root.get("body")
                instance = finding_instance_from_body(body)
                scope = finding_publication_scope(body)
                if scope is not None:
                    (
                        repo_id,
                        pr_number,
                        marker_head,
                        marker_base,
                        blocking_bit,
                        _human,
                    ) = scope
                    this_head = (
                        repo_id == repository_id
                        and pr_number == pull_request
                        and marker_head == head_sha
                        and marker_base == base_sha
                    )
                    if not this_head:
                        continue
                    if node["isResolved"]:
                        if blocking_bit and instance is not None:
                            resolved_instances.add(instance)
                        continue
                    if (
                        honored_instances
                        and instance is not None
                        and instance in honored_instances
                    ):
                        continue
                    if blocking_bit:
                        open_blocking = True
                    continue
                if node["isResolved"]:
                    continue
                if (
                    honored_instances
                    and instance is not None
                    and instance in honored_instances
                ):
                    continue
                blocking = finding_blocks_approval(
                    body=body,
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
                return open_blocking, frozenset(resolved_instances)
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
        require_valid: bool = False,
        validate_visible_prose: bool = True,
        partition_reader: PartitionReader | None = None,
        expected_context: PartitionedEligibilityContext | None = None,
        visible_reader: VisibleReader | None = None,
    ) -> ReviewApprovalEligibility | None:
        """Read the persisted eligibility document for one reviewed head.

        A delayed finalization (a resolved thread) has no access to the
        publishing process, so it re-reads the document that publication
        persisted beside this exact head. Only an App-authored review on this
        head can carry it, and a missing, malformed, or other-head document
        returns ``None`` so the caller withholds instead of trusting a claim it
        cannot verify.

        Non-authoritative receipt paging may explicitly request metadata-only
        validation for v5. Preparation and all authority/finalization paths
        retain the default full visible-prose validation.
        """

        if not isinstance(validate_visible_prose, bool):
            raise GitHubPublicationError("eligibility prose validation mode is invalid")
        with self._read_operation():
            if not GIT_SHA_HEX.fullmatch(head_sha):
                raise GitHubPublicationError("review head sha is invalid")
            if not isinstance(app_slug, str) or not app_slug.strip():
                raise GitHubPublicationError("review app slug is invalid")
            if (
                expected_context is not None
                and expected_context.budget is not self.evidence_budget
            ):
                raise GitHubPublicationError(
                    "partition eligibility scan requires the original shared budget"
                )
            reviews = self._load_head_reviews(
                token=token, repository=repository, pull_request=pull_request
            )
            return self._eligibility_from_reviews(
                reviews,
                head_sha=head_sha,
                app_slug=app_slug,
                require_valid=require_valid,
                validate_visible_prose=validate_visible_prose,
                partition_reader=partition_reader,
                expected_context=expected_context,
                visible_reader=visible_reader,
                token=token,
                repository=repository,
                pull_request=pull_request,
            )

    def _eligibility_from_reviews(
        self,
        reviews: list[Any],
        *,
        head_sha: str,
        app_slug: str,
        require_valid: bool = False,
        validate_visible_prose: bool = True,
        partition_reader: PartitionReader | None = None,
        expected_context: PartitionedEligibilityContext | None = None,
        visible_reader: VisibleReader | None = None,
        token: str | None = None,
        repository: str | None = None,
        pull_request: int | None = None,
    ) -> ReviewApprovalEligibility | None:
        human_reader = self.human_inventory_reader
        if token is not None and repository is not None and pull_request is not None:
            human_reader = self._human_reader_for_scope(
                token=token,
                repository=repository,
                pull_request=pull_request,
                head_sha=head_sha,
                app_slug=app_slug,
            )
        expected = app_slug.casefold()
        for review in reversed(reviews):
            if not isinstance(review, dict) or review.get("commit_id") != head_sha:
                continue
            user = review.get("user")
            login = user.get("login") if isinstance(user, dict) else None
            if not isinstance(login, str) or login.casefold() != expected:
                continue
            body = review.get("body")
            if (
                not isinstance(body, str)
                or APPROVAL_ELIGIBILITY_MARKER_PREFIX not in body
            ):
                continue
            # A malformed newer App record cannot reveal an older clean one.
            if expected_context is not None and (
                type(review.get("id")) is not int
                or review.get("id") != expected_context.owned_root_id
                or not isinstance(user, dict)
                or type(user.get("id")) is not int
                or user.get("type") != "Bot"
                or f"github-bot:{user.get('id')}"
                != expected_context.binding["producer"]
                or review.get("state") not in PUBLISHED_REVIEW_STATES
            ):
                if require_valid:
                    raise GitHubPublicationError(
                        "latest partition eligibility root ownership conflicts"
                    )
                return None
            eligibility = approval_eligibility_from_body(
                body,
                human_inventory_reader=human_reader,
                partition_reader=partition_reader,
                expected_context=expected_context,
                visible_reader=visible_reader,
            )
            if eligibility is not None and eligibility.head_sha == head_sha:
                if eligibility.human_inventory_reference is not None:
                    validate = (
                        getattr(human_reader, "validate_metadata", None)
                        if not validate_visible_prose
                        else None
                    )
                    if not callable(validate):
                        validate = getattr(human_reader, "validate_root", None)
                    if callable(validate):
                        try:
                            validate(review, reviews)
                        except ReviewInputError as exc:
                            if require_valid:
                                raise GitHubPublicationError(
                                    "latest human inventory eligibility root conflicts"
                                ) from exc
                            return None
                return eligibility
            if require_valid:
                raise GitHubPublicationError(
                    "latest review eligibility is invalid; rerun a full review for the current head before reassessment"
                )
            return None
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
            reviews = self._review_pages(
                token=token, repository=repository, pull_request=pull_request
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
        than raised: the review itself is still published, and an otherwise
        eligible review may still be approved even though the merge gate is
        unavailable.
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

        A gate that cannot be written must not suppress the review itself. The
        ``check_permission`` diagnostic tells the operator enforcement is not
        in place; an otherwise eligible review may still be approved. A pending
        conclusion left behind by a transient failure is overwritten by the
        next run for the same head.
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
        base_sha: str,
        app_slug: str,
        check_token: str | None,
        result: ReviewResult,
        policy: str,
        approval_enabled: bool,
        qualification: str,
        retained_eligibility: ReviewApprovalEligibility | None = None,
        persisted_eligibility: ReviewApprovalEligibility | None = None,
    ) -> PublicationResult:
        """Re-assert one already-published result's gate and approval decision.

        The gate is published for every reviewed head, including one whose
        review already exists, so an interrupted earlier run cannot leave a
        pending conclusion standing for this result. App-authored pull requests
        never reach this helper: the preflight reports them before publication.
        The reported diagnostic is the one a fresh publication would report, so
        a repeated run explains its withholding the same way.
        """

        persisted = persisted_eligibility
        if persisted is not None and persisted.result_digest != review_result_digest(
            result
        ):
            return PublicationResult(
                status="already_published", diagnostic="approval_withheld"
            )
        replay_eligibility = persisted or approval_eligibility_from_result(
            result,
            head_sha=head_sha,
            base_sha=base_sha,
            enabled=approval_enabled,
            app_authored=False,
            qualification=qualification,
            retained_eligibility=retained_eligibility,
        )
        replay_eligibility = replace(
            replay_eligibility,
            facts=replace(
                replay_eligibility.facts,
                enabled=approval_enabled and replay_eligibility.facts.enabled,
                qualification=qualification
                if qualification not in ("qualified", "not-required")
                else replay_eligibility.facts.qualification,
            ),
        )
        check_diagnostic = self._check_state_or_diagnostic(
            check_token=check_token,
            repository=repository,
            head_sha=head_sha,
            app_slug=app_slug,
            outcome=check_outcome_for_result(
                replace(
                    result,
                    review_status=replay_eligibility.facts.review_status,
                ),
                policy=policy,
                required_fixes=replay_eligibility.facts.has_blocking_findings,
                required_decisions=replay_eligibility.facts.has_human_adjudication_findings,
            ),
        )
        finalized = self.finalizer.finalize(
            token=token,
            repository=repository,
            pull_request=pull_request,
            head_sha=head_sha,
            app_slug=app_slug,
            eligibility=replay_eligibility,
            require_persisted=persisted is not None,
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
        retained_eligibility: ReviewApprovalEligibility | None = None,
        continuation_digest: str | None = None,
        explicit_reanalysis: bool = False,
        source_guard: Callable[[], bool] | None = None,
        complete_inventory: bool = False,
        human_inventory_store: HumanInventoryStore | None = None,
        allow_human_inventory_start: bool = False,
    ) -> PublicationResult:
        owns_operation_budget = (
            self.finalizer.evidence_budget is None
            and self.finalizer._operation_budget is None
        )
        if owns_operation_budget:
            self.finalizer._operation_budget = EvidenceReadBudget()
        try:
            return self._publish_impl(
                token=token,
                repository=repository,
                repository_id=repository_id,
                pull_request=pull_request,
                head_sha=head_sha,
                base_branch=base_branch,
                base_sha=base_sha,
                result=result,
                diff=diff,
                app_slug=app_slug,
                auto_approve=auto_approve,
                reviews_policy=reviews_policy,
                check_token=check_token,
                qualification=qualification,
                candidates=candidates,
                snapshot=snapshot,
                snapshot_sha256=snapshot_sha256,
                evidence_policy=evidence_policy,
                convergence_policy=convergence_policy,
                _allow_retired_legacy_policy=allow_retired_legacy_policy,
                blocker_candidates=blocker_candidates,
                input_blocker_candidates=input_blocker_candidates,
                baseline=baseline,
                current_key=current_key,
                changed_paths=changed_paths,
                related_paths=related_paths,
                evidence_confirmed_concerns=evidence_confirmed_concerns,
                authorized_dispositions=authorized_dispositions,
                prepared_review=prepared_review,
                retained_eligibility=retained_eligibility,
                continuation_digest=continuation_digest,
                explicit_reanalysis=explicit_reanalysis,
                source_guard=source_guard,
                complete_inventory=complete_inventory,
                human_inventory_store=human_inventory_store,
                allow_human_inventory_start=allow_human_inventory_start,
            )
        finally:
            if owns_operation_budget:
                self.finalizer._operation_budget = None

    def _publish_impl(
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
        _allow_retired_legacy_policy: bool = False,
        blocker_candidates: Sequence[BlockerCandidate] | None = None,
        input_blocker_candidates: Sequence[BlockerCandidate] | None = None,
        baseline: ReviewBaseline | None = None,
        current_key: ReviewContextCacheKey | None = None,
        changed_paths: Sequence[str] | None = None,
        related_paths: Sequence[str] = (),
        evidence_confirmed_concerns: Sequence[str] = (),
        authorized_dispositions: Sequence[object] = (),
        prepared_review: PublishableReview | None = None,
        retained_eligibility: ReviewApprovalEligibility | None = None,
        continuation_digest: str | None = None,
        explicit_reanalysis: bool = False,
        source_guard: Callable[[], bool] | None = None,
        complete_inventory: bool = False,
        human_inventory_store: HumanInventoryStore | None = None,
        allow_human_inventory_start: bool = False,
    ) -> PublicationResult:
        if not isinstance(complete_inventory, bool):
            raise GitHubPublicationError("complete inventory writer gate is invalid")
        if not isinstance(allow_human_inventory_start, bool):
            raise GitHubPublicationError("human inventory start authority is invalid")
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
        qualification = restrict_result_qualification(result, qualification)
        _reject_conflicting_instances(result.comments)
        if retained_eligibility is not None:
            if (
                not isinstance(retained_eligibility, ReviewApprovalEligibility)
                or not isinstance(continuation_digest, str)
                or re.fullmatch(r"[a-f0-9]{64}", continuation_digest) is None
                or not callable(source_guard)
            ):
                raise GitHubPublicationError(
                    "broader review source authority is invalid"
                )
            if not source_guard():
                return PublicationResult(status="skipped_edited_source")
        elif continuation_digest is not None or source_guard is not None:
            raise GitHubPublicationError("broader review retained authority is missing")
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
        if not isinstance(_allow_retired_legacy_policy, bool):
            raise GitHubPublicationError(
                "review legacy policy opt-in must be a boolean"
            )
        if (
            convergence_policy.mode == LEGACY_REVIEW_MODE
            and not _allow_retired_legacy_policy
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
        # Validate the complete inventory before checks or review mutations.
        # Failure must not publish an unreassessable human-review record.
        prospective_eligibility = approval_eligibility_from_result(
            result,
            head_sha=head_sha,
            base_sha=base_sha,
            enabled=approval_enabled,
            app_authored=False,
            qualification=qualification,
            retained_eligibility=retained_eligibility,
        )
        # Only legacy single-piece capacity may select the new default store.
        # A fixed-width placeholder qualifies every authority/body allocation
        # before the first inventory, check, or review write. No reference or
        # caller-supplied hash can authorize a new staging attempt.
        needs_human_inventory = False
        stage_human_inventory = complete_inventory and human_inventory_store is not None
        human_inventory = prospective_eligibility.human_review
        if stage_human_inventory:
            from .default_human_inventory import (
                HumanInventoryReference,
                HumanInventoryStore,
            )

            if not isinstance(human_inventory_store, HumanInventoryStore):
                raise GitHubPublicationError("human inventory store is invalid")
            context = human_inventory_store.context
            if (
                context.repository != repository
                or context.repository_id != repository_id
                or context.pull_request != pull_request
                or context.base_sha != base_sha
                or context.head_sha != head_sha
                or context.app_slug.casefold() != app_slug.casefold()
                or context.original_content_digest != result.content_digest()
                or context.publication_digest != review_result_digest(result)
                or context.budget
                is not (
                    self.finalizer.evidence_budget or self.finalizer._operation_budget
                )
                or result.transaction is None
                or context.transaction_id != result.transaction.transaction_id
            ):
                raise GitHubPublicationError(
                    "human inventory store authority mismatches publication"
                )
        if human_inventory is not None:
            try:
                human_inventory.to_dict()
            except ReviewInputError:
                if not stage_human_inventory:
                    raise
                prospective_eligibility = replace(
                    prospective_eligibility,
                    human_inventory_reference=HumanInventoryReference(
                        context.event_key, "0" * 64
                    ),
                )
                needs_human_inventory = True
        prospective_eligibility.to_dict()
        # Automatic approval also requires an eligible review: a partial or
        # failed analysis, a pending human assessment, or incomplete coverage
        # must never converge to APPROVE on the maintainer's behalf. Eligibility
        # is evaluated once, on the admitted artifact that is actually
        # published, so the summary state and the approval operation can never
        # disagree. The finding review event and the finalizer's gate
        # maintenance still follow the operator's opt-in, so a blocking finding
        # keeps requesting changes on an ineligible review.
        approval_eligible = _approval_eligible(result) and not (
            retained_eligibility is not None
            and (
                prospective_eligibility.facts.has_blocking_findings
                or prospective_eligibility.facts.has_human_adjudication_findings
            )
        )
        auto_approval = auto_approve and approval_eligible
        analysis = self._validate_locations(result, diff)
        marker = review_marker(
            repository_id=repository_id,
            pull_request=pull_request,
            head_sha=head_sha,
            result=result,
            continuation_digest=continuation_digest,
        )
        identity_marker = review_identity_marker(
            repository_id=repository_id,
            pull_request=pull_request,
            head_sha=head_sha,
        )
        complete_review: AssembledReview | None = None
        preflight_review: AssembledReview | None = None
        if complete_inventory or human_inventory is not None:
            # Qualify the complete framed representation before reconciliation
            # can update a check or finalize an already published result. False
            # reserves the longer check/author boolean spelling; body placement
            # determines the same blocking fact used by the actual writer.
            preflight_review = self._assemble_review_body(
                result=result,
                analysis=analysis,
                convergence_policy=convergence_policy,
                read_facts=lambda: self._conversation_resolution_facts(
                    token=token, repository=repository, branch=base_branch
                ),
                auto_approve=auto_approval,
                repository_id=repository_id,
                pull_request=pull_request,
                head_sha=head_sha,
                base_sha=base_sha,
            )
            if needs_human_inventory:
                preflight_review = replace(preflight_review, compact_references=True)
            body_blocking = any(
                entry.blocking and entry.body_placed
                for entry in preflight_review.index_entries
            ) or bool(
                retained_eligibility is not None
                and retained_eligibility.facts.has_blocking_findings
            )
            preflight_document = replace(
                prospective_eligibility,
                facts=replace(
                    prospective_eligibility.facts,
                    app_authored=False,
                    check_published=False,
                    has_blocking_findings=body_blocking,
                ),
            )
            reserve = _disposition_frame_reserve(
                preflight_document, preflight_review.index_entries
            )
            for compact in (False, True):
                if compact:
                    preflight_review = replace(
                        preflight_review, compact_references=True
                    )
                try:
                    fit_assembled_review(
                        preflight_review,
                        review_marker=marker,
                        eligibility_marker=approval_eligibility_marker(
                            preflight_document
                        ),
                        repository_id=repository_id,
                        pull_request=pull_request,
                        head_sha=head_sha,
                        base_sha=base_sha,
                        result_digest=review_result_digest(result),
                        summary_limit=_published_summary_byte_limit(result),
                        suffix=(
                            "Earlier findings remain in their assessment inventory; all "
                            "approval requirements still apply."
                            if retained_eligibility is not None
                            else ""
                        ),
                        complete_inventory=complete_inventory,
                        reserved_bytes=reserve,
                        preserve_explanations=human_inventory is not None,
                    )
                except PublicationLimitError:
                    if preflight_review.compact_references:
                        raise
                else:
                    break
            if complete_inventory:
                complete_review = preflight_review
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
            identity_states, result_states, persisted_eligibility = (
                self._published_state_index(
                    token=token,
                    repository=repository,
                    pull_request=pull_request,
                    head_sha=head_sha,
                    identity_marker=identity_marker,
                    result_marker=marker,
                    app_slug=app_slug,
                    repository_id=repository_id if complete_inventory else None,
                    complete_review=complete_review,
                    base_sha=base_sha,
                    result_digest=review_result_digest(result),
                )
            )
            if (
                complete_inventory
                and identity_states
                and not result_states
                and not explicit_reanalysis
                and not has_blocking_findings(result)
            ):
                raise GitHubPublicationError(
                    "existing review does not prove complete publication of this result"
                )
            if explicit_reanalysis and not result_states:
                # Only the application can bind this to a consumed durable
                # rescan receipt. The latest publication must still be
                # incomplete; ordinary per-head reconciliation remains below.
                if (
                    result.transaction is None
                    or persisted_eligibility is None
                    or (
                        persisted_eligibility.facts.review_status == "complete"
                        and persisted_eligibility.facts.coverage_blocker is None
                    )
                    or "APPROVED" in identity_states
                ):
                    return PublicationResult(status="already_published")
            elif retained_eligibility is not None:
                if result_states:
                    current = persisted_eligibility
                    if (
                        current is None
                        or replace(
                            current,
                            facts=replace(
                                current.facts,
                                check_published=prospective_eligibility.facts.check_published,
                            ),
                        )
                        != prospective_eligibility
                    ):
                        return PublicationResult(status="skipped_stale_head")
                    return self._finalize_published_result(
                        token=token,
                        repository=repository,
                        pull_request=pull_request,
                        head_sha=head_sha,
                        base_sha=base_sha,
                        app_slug=app_slug,
                        check_token=check_token,
                        result=result,
                        policy=effective_policy,
                        approval_enabled=approval_enabled,
                        qualification=qualification,
                        retained_eligibility=retained_eligibility,
                        persisted_eligibility=persisted_eligibility,
                    )
            elif has_blocking_findings(result):
                if result_states:
                    if not preflight.app_authored:
                        return self._finalize_published_result(
                            token=token,
                            repository=repository,
                            pull_request=pull_request,
                            head_sha=head_sha,
                            base_sha=base_sha,
                            app_slug=app_slug,
                            check_token=check_token,
                            result=result,
                            policy=effective_policy,
                            approval_enabled=approval_enabled,
                            qualification=qualification,
                            persisted_eligibility=persisted_eligibility,
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
                            base_sha=base_sha,
                            app_slug=app_slug,
                            check_token=check_token,
                            result=result,
                            policy=effective_policy,
                            approval_enabled=approval_enabled,
                            qualification=qualification,
                            persisted_eligibility=persisted_eligibility,
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
        continuation_review: AssembledReview | None = None
        continuation_suffix = (
            "Earlier findings remain in their assessment inventory; all "
            "approval requirements still apply."
        )
        complete_suppression: PublishedFindingSuppression | None = None
        if retained_eligibility is not None:
            assert source_guard is not None
            if not source_guard():
                return PublicationResult(status="skipped_edited_source")
            current = self.finalizer.load_eligibility(
                token=token,
                repository=repository,
                pull_request=pull_request,
                head_sha=head_sha,
                app_slug=app_slug,
                require_valid=True,
            )
            if current != retained_eligibility:
                return PublicationResult(status="skipped_stale_head")
            continuation_review = preflight_review or self._assemble_review_body(
                result=result,
                analysis=analysis,
                convergence_policy=convergence_policy,
                read_facts=lambda: self._conversation_resolution_facts(
                    token=token, repository=repository, branch=base_branch
                ),
                auto_approve=auto_approval,
                repository_id=repository_id,
                pull_request=pull_request,
                head_sha=head_sha,
                base_sha=base_sha,
            )
            # False is the longer JSON boolean. Preflight the whole framed
            # authority before the first check or review mutation.
            preflight_eligibility = approval_eligibility_marker(
                replace(
                    prospective_eligibility,
                    facts=replace(
                        prospective_eligibility.facts,
                        check_published=False,
                        has_blocking_findings=(
                            any(
                                entry.blocking and entry.body_placed
                                for entry in continuation_review.index_entries
                            )
                            or retained_eligibility.facts.has_blocking_findings
                        ),
                    ),
                )
            )
            fit_assembled_review(
                continuation_review,
                review_marker=marker,
                eligibility_marker=preflight_eligibility,
                repository_id=repository_id,
                pull_request=pull_request,
                head_sha=head_sha,
                base_sha=base_sha,
                result_digest=review_result_digest(result),
                summary_limit=_published_summary_byte_limit(result),
                suffix=continuation_suffix,
                complete_inventory=complete_inventory,
                reserved_bytes=_disposition_frame_reserve(
                    prospective_eligibility, continuation_review.index_entries
                ),
                preserve_explanations=human_inventory is not None,
            )
        if complete_review is not None and complete_review.inline_comments:
            complete_suppression = self._published_finding_suppression(
                token=token,
                repository=repository,
                pull_request=pull_request,
                app_slug=app_slug,
                repository_id=repository_id,
                head_sha=head_sha,
                base_sha=base_sha,
            )
            if any(
                _should_suppress_published_finding(
                    comment,
                    fingerprint,
                    complete_suppression,
                    instance=finding_instance_fingerprint(comment),
                    head_sha=head_sha,
                    base_sha=base_sha,
                )
                for comment, fingerprint, _, _ in complete_review.inline_comments
            ):
                raise GitHubPublicationError(
                    "complete inline publication requires reconciliation of retained explanations"
                )
        if stage_human_inventory:
            from review_sensei.human_inventory import inventory_result_document

            assert human_inventory_store is not None
            assert complete_review is not None
            # The full index preserves runtime admission even for a small or
            # clean review. Raw ReviewResult retention omits those flags, so
            # absence of a human inventory alone must never mean clean recovery.
            reference = human_inventory_store.stage(
                {
                    "human_inventory": inventory_result_document(
                        human_inventory, result_digest=review_result_digest(result)
                    )
                    if human_inventory is not None
                    else None,
                    "finding_index": finding_index_marker(
                        repository_id=repository_id,
                        pull_request=pull_request,
                        head_sha=head_sha,
                        base_sha=base_sha,
                        result_digest=review_result_digest(result),
                        entries=complete_review.index_entries,
                        complete_inventory=True,
                    ),
                    "visible_prose": [],
                },
                allow_start=allow_human_inventory_start,
            )
            if needs_human_inventory:
                prospective_eligibility = replace(
                    prospective_eligibility, human_inventory_reference=reference
                )
            # Staging is nonauthoritative and may span multiple requests. Fence
            # the actual PR again before check/root mutations; retained parts
            # remain evidence if that original head changed during staging.
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
                    outcome=check_outcome_for_result(
                        replace(
                            result,
                            review_status=prospective_eligibility.facts.review_status,
                        ),
                        policy=effective_policy,
                        required_fixes=prospective_eligibility.facts.has_blocking_findings,
                        required_decisions=prospective_eligibility.facts.has_human_adjudication_findings,
                    ),
                )
        eligibility = approval_eligibility_from_result(
            result,
            head_sha=head_sha,
            base_sha=base_sha,
            enabled=approval_enabled,
            app_authored=write_preflight.app_authored,
            qualification=qualification,
            check_published=check_diagnostic is None,
            retained_eligibility=retained_eligibility,
        )
        if needs_human_inventory:
            eligibility = replace(
                eligibility,
                human_inventory_reference=prospective_eligibility.human_inventory_reference,
            )
        try:
            assembled = (
                continuation_review
                or preflight_review
                or self._assemble_review_body(
                    result=result,
                    analysis=analysis,
                    convergence_policy=convergence_policy,
                    read_facts=lambda: self._conversation_resolution_facts(
                        token=token, repository=repository, branch=base_branch
                    ),
                    auto_approve=auto_approval,
                    repository_id=repository_id,
                    pull_request=pull_request,
                    head_sha=head_sha,
                    base_sha=base_sha,
                )
            )
            body_blocking = any(
                entry.blocking and entry.body_placed
                for entry in assembled.index_entries
            )
            if retained_eligibility is not None:
                # A continuation does not restate earlier findings. Their
                # persisted blocking obligation stays in force.
                body_blocking = (
                    body_blocking or retained_eligibility.facts.has_blocking_findings
                )
            eligibility = replace(
                eligibility,
                facts=replace(eligibility.facts, has_blocking_findings=body_blocking),
            )
            eligibility_marker = approval_eligibility_marker(eligibility)
            summary, index_marker = fit_assembled_review(
                assembled,
                review_marker=marker,
                eligibility_marker=eligibility_marker,
                repository_id=repository_id,
                pull_request=pull_request,
                head_sha=head_sha,
                base_sha=base_sha,
                result_digest=review_result_digest(result),
                summary_limit=_published_summary_byte_limit(result),
                suffix=continuation_suffix if continuation_review is not None else "",
                complete_inventory=complete_inventory,
                reserved_bytes=_disposition_frame_reserve(
                    eligibility, assembled.index_entries
                ),
                preserve_explanations=human_inventory is not None,
            )
            prepared_comments = assembled.inline_comments

            def body_for_summary(review_summary: str) -> str:
                return _framed_review_body(
                    review_summary,
                    review_marker=marker,
                    index_marker=index_marker,
                    eligibility_marker=eligibility_marker,
                )

            body = body_for_summary(summary)
            if check_diagnostic == "check_permission":
                for concise in (False, True):
                    warned_summary = (
                        f"{render_check_permission_warning(concise=concise)}\n\n"
                        f"{summary}"
                    )
                    if len(
                        warned_summary.encode("utf-8")
                    ) > _published_summary_byte_limit(result):
                        continue
                    warned_body = body_for_summary(warned_summary)
                    if len(
                        warned_body.encode("utf-8")
                    ) <= MAX_PUBLISHED_REVIEW_BODY_BYTES - _disposition_frame_reserve(
                        eligibility, assembled.index_entries
                    ):
                        body = warned_body
                        break
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
        suppression = complete_suppression or PublishedFindingSuppression({}, {})
        if prepared_comments and complete_suppression is None:
            suppression = self._published_finding_suppression(
                token=token,
                repository=repository,
                pull_request=pull_request,
                app_slug=app_slug,
                repository_id=repository_id,
                head_sha=head_sha,
                base_sha=base_sha,
            )
        comments = []
        for comment, fingerprint, comment_body, anchor in prepared_comments:
            if _should_suppress_published_finding(
                comment,
                fingerprint,
                suppression,
                instance=finding_instance_fingerprint(comment),
                head_sha=head_sha,
                base_sha=base_sha,
            ):
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
        # auto-approve submits GitHub's Changes requested review when this
        # head has required fixes, then approves a later eligible head. Both
        # auto-approve and blocking fail the ReviewSensei check when required
        # fixes remain and return action_required when human decisions remain;
        # advisory never fails the check. App-authored pull requests stay
        # comments because GitHub rejects REQUEST_CHANGES from the pull
        # request author.
        retained_blocking = (
            retained_eligibility is not None
            and retained_eligibility.facts.has_blocking_findings
        )
        if (
            approval_enabled
            and (assembled.requests_changes or retained_blocking)
            and not write_preflight.app_authored
        ):
            event, published_state = "REQUEST_CHANGES", "CHANGES_REQUESTED"
        else:
            event, published_state = "COMMENT", "COMMENTED"
        path = self.http.repository_path(
            repository,
            f"/pulls/{pull_request}/reviews",
        )
        if retained_eligibility is not None:
            assert source_guard is not None
            if not source_guard():
                return PublicationResult(status="skipped_edited_source")
            if (
                self.finalizer.load_eligibility(
                    token=token,
                    repository=repository,
                    pull_request=pull_request,
                    head_sha=head_sha,
                    app_slug=app_slug,
                    require_valid=True,
                )
                != retained_eligibility
            ):
                return PublicationResult(status="skipped_stale_head")
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
                marker=marker
                if complete_inventory or retained_eligibility is not None
                else identity_marker,
                app_slug=app_slug,
                expected_state=published_state,
                complete_review=assembled if complete_inventory else None,
                repository_id=repository_id,
                base_sha=base_sha,
                result_digest=review_result_digest(result),
            ):
                return self._finalize_after_publication(
                    token=token,
                    repository=repository,
                    pull_request=pull_request,
                    head_sha=head_sha,
                    app_slug=app_slug,
                    eligibility=eligibility,
                    require_persisted=retained_eligibility is not None,
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
                "review publication failed temporarily (review-create transport failure)"
            ) from exc
        if status == 200 and isinstance(payload, dict):
            review_id = payload.get("id")
            if isinstance(review_id, int):
                self._supersede_replaced_threads(
                    token=token,
                    threads=suppression.superseded_threads,
                    confirmed_review_id=review_id,
                    repository=repository,
                    repository_id=repository_id,
                    pull_request=pull_request,
                    head_sha=head_sha,
                    base_sha=base_sha,
                    app_slug=app_slug,
                    eligibility=eligibility,
                    replacements=prepared_comments,
                )
                return self._finalize_after_publication(
                    token=token,
                    repository=repository,
                    pull_request=pull_request,
                    head_sha=head_sha,
                    app_slug=app_slug,
                    eligibility=eligibility,
                    require_persisted=retained_eligibility is not None,
                    check_diagnostic=check_diagnostic,
                    status="published",
                    review_id=review_id,
                    assume_inline_unresolved=frozenset(
                        assembled.inline_blocker_instances
                    ),
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
            # An HTTP 422 is ambiguous. It is not evidence that a line was an
            # invalid inline location. Reconcile the exact review, then the
            # expected finding identities. One existing thread is not the review.
            if not self._reconcile_marker(
                token=token,
                repository=repository,
                pull_request=pull_request,
                head_sha=head_sha,
                marker=marker,
                app_slug=app_slug,
                expected_state=published_state,
                complete_review=assembled if complete_inventory else None,
                repository_id=repository_id,
                base_sha=base_sha,
                result_digest=review_result_digest(result),
            ):
                self._complete_failed_check(
                    check_token=check_token,
                    repository=repository,
                    head_sha=head_sha,
                    app_slug=app_slug,
                    policy=effective_policy,
                    transient=False,
                )
                raise GitHubPublicationError(
                    "review publication was rejected; intended review is absent"
                )
            if not self._inline_findings_persisted(
                token=token,
                repository=repository,
                pull_request=pull_request,
                app_slug=app_slug,
                repository_id=repository_id,
                head_sha=head_sha,
                base_sha=base_sha,
                instances=tuple(
                    finding_instance_fingerprint(comment)
                    for comment, _fingerprint, _body, _anchor in prepared_comments
                ),
            ):
                return PublicationResult(
                    status="publication_incomplete",
                    diagnostic="publication_ambiguous",
                )
            return self._finalize_after_publication(
                token=token,
                repository=repository,
                pull_request=pull_request,
                head_sha=head_sha,
                app_slug=app_slug,
                eligibility=eligibility,
                require_persisted=retained_eligibility is not None,
                check_diagnostic=check_diagnostic,
                status="already_published",
            )
        if status == 409 or status == 429 or status >= 500:
            if self._reconcile_marker(
                token=token,
                repository=repository,
                pull_request=pull_request,
                head_sha=head_sha,
                marker=marker
                if complete_inventory or retained_eligibility is not None
                else identity_marker,
                app_slug=app_slug,
                expected_state=published_state,
                complete_review=assembled if complete_inventory else None,
                repository_id=repository_id,
                base_sha=base_sha,
                result_digest=review_result_digest(result),
            ):
                return self._finalize_after_publication(
                    token=token,
                    repository=repository,
                    pull_request=pull_request,
                    head_sha=head_sha,
                    app_slug=app_slug,
                    eligibility=eligibility,
                    require_persisted=retained_eligibility is not None,
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
                f"review publication failed temporarily (review-create HTTP {status})"
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
        require_persisted: bool = False,
        assume_inline_unresolved: frozenset[str] | None = None,
    ) -> PublicationResult:
        """Finalize approval for one published head and report its withholding.

        The persisted eligibility document is the only approval input, so an
        app-authored pull request withholds here as well: the finalizer reports
        it before any network read. A ``check_diagnostic`` wins over the
        finalizer's reason because an unpublished gate is an enforcement gap
        that must remain visible, even when approval succeeds or is withheld for
        another eligibility reason.
        """

        finalized = self.finalizer.finalize(
            token=token,
            repository=repository,
            pull_request=pull_request,
            head_sha=head_sha,
            app_slug=app_slug,
            eligibility=eligibility,
            require_persisted=require_persisted,
            assume_inline_unresolved=assume_inline_unresolved,
        )
        diagnostic = check_diagnostic
        if diagnostic is None and finalized.status == "approval_withheld":
            diagnostic = finalized.diagnostic
        return PublicationResult(
            status=status,
            review_id=review_id,
            diagnostic=diagnostic,
        )

    def _inline_findings_persisted(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        app_slug: str,
        repository_id: int,
        head_sha: str,
        base_sha: str,
        instances: Sequence[str],
    ) -> bool:
        """Return whether each expected inline instance is already on this head.

        Failure or an incomplete thread read is not confirmation. A 422 is not
        rewritten into an invalid-location error here.
        """

        if not instances:
            return True
        try:
            found = self._published_finding_suppression(
                token=token,
                repository=repository,
                pull_request=pull_request,
                app_slug=app_slug,
                repository_id=repository_id,
                head_sha=head_sha,
                base_sha=base_sha,
            )
        except (GitHubPublicationError, GitHubPublicationTransientError):
            return False
        return all(instance in found.by_instance for instance in instances)

    def _supersede_replaced_threads(
        self,
        *,
        token: str,
        threads: Sequence[tuple[str, str]],
        confirmed_review_id: int,
        repository: str,
        repository_id: int,
        pull_request: int,
        head_sha: str,
        base_sha: str,
        app_slug: str,
        eligibility: ReviewApprovalEligibility,
        replacements: Sequence[tuple[ReviewComment, str, str, str]],
    ) -> None:
        from .thread_resolution import FindingThreadResolver

        replacement_concerns = {item[1] for item in replacements}
        matching = {
            thread: concern
            for thread, concern in threads
            if concern in replacement_concerns
        }
        if (
            not matching
            or type(confirmed_review_id) is not int
            or confirmed_review_id < 1
        ):
            return
        try:

            def fence() -> bool:
                if not self._inline_findings_persisted(
                    token=token,
                    repository=repository,
                    pull_request=pull_request,
                    app_slug=app_slug,
                    repository_id=repository_id,
                    head_sha=head_sha,
                    base_sha=base_sha,
                    instances=tuple(
                        finding_instance_fingerprint(item[0]) for item in replacements
                    ),
                ):
                    return False
                current = self.finalizer.load_eligibility(
                    token=token,
                    repository=repository,
                    pull_request=pull_request,
                    head_sha=head_sha,
                    app_slug=app_slug,
                )
                live = self.finalizer._preflight(
                    token=token,
                    repository=repository,
                    pull_request=pull_request,
                    head_sha=head_sha,
                    app_slug=app_slug,
                )
                return (
                    current == eligibility
                    and live.result is None
                    and live.repository_id == repository_id
                    and live.base_sha == base_sha
                    and not live.app_authored
                )

            FindingThreadResolver(self.http).resolve(
                token=token,
                repository=repository,
                repository_id=repository_id,
                pull_request=pull_request,
                head_sha=head_sha,
                base_sha=base_sha,
                app_slug=app_slug,
                instances=frozenset(),
                supersede=matching,
                fence=fence,
            )
        except (GitHubHTTPError, GitHubPublicationError):
            return

    def _published_finding_suppression(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        app_slug: str,
        repository_id: int,
        head_sha: str,
        base_sha: str,
    ) -> PublishedFindingSuppression:
        """Return already-published finding indexes for duplicate suppression.

        Only an open App thread whose marker matches this repository, pull
        request, head, base, instance, and approval classification can suppress
        a new comment. An older head is recorded for supersession and does not
        suppress the current head.
        """

        owner, separator, name = repository.partition("/")
        if not separator or not owner or not name:
            raise GitHubPublicationError("review thread repository is invalid")
        by_fingerprint: dict[str, bool] = {}
        by_location: dict[tuple[str, int], bool] = {}
        by_instance: dict[str, PublishedInstance] = {}
        superseded: list[tuple[str, str]] = []
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
                    "review thread reconciliation is unavailable; publication "
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
                resolved = node.get("isResolved")
                if not isinstance(resolved, bool):
                    raise GitHubPublicationError("review thread response was invalid")
                body = root.get("body")
                scope = finding_publication_scope(body)
                if scope is None:
                    continue
                repo_id, pr_number, marker_head, marker_base, blocking, needs_human = (
                    scope
                )
                thread_id = node.get("id")
                if thread_id is not None and not isinstance(thread_id, str):
                    raise GitHubPublicationError("review thread response was invalid")
                fingerprint = finding_fingerprint_from_body(body)
                instance = finding_instance_from_body(body)
                same_pull = repo_id == repository_id and pr_number == pull_request
                if (
                    same_pull
                    and marker_head != head_sha
                    and isinstance(thread_id, str)
                    and thread_id
                    and not resolved
                    and fingerprint is not None
                ):
                    superseded.append((thread_id, fingerprint))
                if (
                    same_pull
                    and marker_head == head_sha
                    and marker_base == base_sha
                    and instance is not None
                ):
                    by_instance[instance] = PublishedInstance(
                        blocking=blocking,
                        needs_human=needs_human,
                        head_sha=marker_head,
                        base_sha=marker_base,
                        resolved=resolved,
                        thread_id=thread_id if isinstance(thread_id, str) else None,
                        concern=fingerprint,
                    )
                if fingerprint is not None and marker_head == head_sha:
                    by_fingerprint[fingerprint] = blocking
            has_next = page_info.get("hasNextPage")
            if not isinstance(has_next, bool):
                raise GitHubPublicationError("review thread response was invalid")
            if not has_next:
                return PublishedFindingSuppression(
                    by_fingerprint,
                    by_location,
                    by_instance,
                    superseded_threads=tuple(superseded),
                )
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

    def _assemble_review_body(
        self,
        *,
        result: ReviewResult,
        analysis: DiffAnalysis,
        convergence_policy: ReviewConvergencePolicy,
        read_facts: Callable[[], HostPlacementFacts],
        auto_approve: bool,
        repository_id: int,
        pull_request: int,
        head_sha: str,
        base_sha: str,
    ) -> AssembledReview:
        """Plan placement, including oversized-inline fallback, before the index.

        Every displayed finding gets one stable ID from its instance
        fingerprint. Conflicting classifications for that instance are rejected
        before publication. Fixed and outdated findings do not open threads.
        Inline findings are POSTed as threads; body findings keep their
        explanation unless optional prose must be removed for the body ceiling.
        """

        advisory = not convergence_policy.automatic_github_review_events
        lifecycle_by_fingerprint = {
            item.fingerprint: item.state for item in result.finding_lifecycles
        }
        fingerprints = [
            finding_lifecycle_for_comment(comment).fingerprint
            for comment in result.comments
        ]
        instances = [
            finding_instance_fingerprint(comment) for comment in result.comments
        ]
        classifications: dict[str, tuple[bool, bool]] = {}
        for comment, instance in zip(result.comments, instances, strict=True):
            classification = (
                bool(comment.blocks_approval),
                bool(comment.needs_human),
            )
            previous = classifications.get(instance)
            if previous is not None and previous != classification:
                raise GitHubPublicationError("conflicting finding classifications")
            classifications[instance] = classification
        identifiers = assign_finding_identifiers(instances)
        prepared_comments: list[tuple[ReviewComment, str, str, str]] = []
        body_reasons: list[str] = []
        all_views: list[FindingView] = []
        index_entries: list[IndexedFinding] = []
        explanations: list[BodyExplanation] = []
        inline_blockers: list[str] = []
        facts: HostPlacementFacts | None = None
        seen_instances: set[str] = set()
        requests_changes = False
        for comment, fingerprint, instance in zip(
            result.comments, fingerprints, instances, strict=True
        ):
            state = lifecycle_by_fingerprint.get(fingerprint, "new")
            if state in {"fixed", "outdated"}:
                continue
            if comment.blocks_approval:
                requests_changes = True
            if instance in seen_instances:
                continue
            seen_instances.add(instance)
            anchor = _publication_anchor(comment, analysis)
            if facts is None and _placement_needs_host_facts(
                comment,
                anchor=anchor,
                policy=convergence_policy,
                advisory=advisory,
            ):
                facts = read_facts()
            placement, body_reason = plan_finding_placement_detail(
                comment,
                anchor=anchor,
                facts=facts or HostPlacementFacts(),
                policy=convergence_policy,
                advisory=advisory,
            )
            view = build_finding_view(
                comment,
                fingerprint=fingerprint,
                identifier=identifiers[instance],
                lifecycle_state=state,
                placement=placement,
                advisory=advisory,
            )
            # Concern history cannot prove this exact instance was explained.
            view = replace(view, repeat=True)
            if placement == INLINE_PLACEMENT:
                comment_body = (
                    f"{_with_discussion_instruction(render_finding(view))}\n\n"
                    f"{finding_instance_marker(comment)}\n\n"
                    f"{finding_marker(repository_id=repository_id, pull_request=pull_request, head_sha=head_sha, base_sha=base_sha, result=result, blocking=comment.blocks_approval, fingerprint=fingerprint, state=state, needs_human=comment.needs_human)}"
                )
                if (
                    len(comment_body.encode("utf-8"))
                    > result.limits.max_comment_body_bytes
                ):
                    placement = BODY_PLACEMENT
                    body_reason = BODY_REASON_OVERSIZED
                    view = replace(view, placement=BODY_PLACEMENT)
                else:
                    prepared_comments.append(
                        (comment, fingerprint, comment_body, anchor)
                    )
                    if comment.blocks_approval:
                        inline_blockers.append(instance)
            all_views.append(view)
            overridable = bool(comment.blocks_approval or comment.needs_human)
            index_entries.append(
                IndexedFinding(
                    instance=instance,
                    lifecycle=fingerprint,
                    blocking=bool(comment.blocks_approval),
                    needs_human=bool(comment.needs_human),
                    body_placed=placement == BODY_PLACEMENT,
                    overridable=overridable,
                )
            )
            if placement == BODY_PLACEMENT:
                if body_reason is not None:
                    body_reasons.append(body_reason)
                explanations.append(
                    BodyExplanation(
                        instance=instance,
                        severity=comment.severity,
                        obligatory=bool(comment.blocks_approval or comment.needs_human),
                        view=view,
                    )
                )
        state, reason = _review_summary_state(
            result, policy=convergence_policy, auto_approve=auto_approve
        )
        return AssembledReview(
            inline_comments=tuple(prepared_comments),
            index_entries=tuple(index_entries),
            head_sha=head_sha,
            state=state,
            reason=reason,
            overview=result.summary,
            coverage_line=_coverage_line(result),
            required_views=tuple(
                view for view in all_views if view.requirement != OPTIONAL_FINDING
            ),
            optional_views=tuple(
                view for view in all_views if view.requirement == OPTIONAL_FINDING
            ),
            limitations=_review_limitations(result, all_views),
            placement_note=placement_note(body_reasons) if explanations else None,
            body_explanations=tuple(explanations),
            inline_blocker_instances=tuple(inline_blockers),
            requests_changes=requests_changes,
        )

    def _conversation_resolution_facts(
        self, *, token: str, repository: str, branch: str
    ) -> HostPlacementFacts:
        """Read branch conversation-resolution requirements through the host.

        A positive requirement from either branch rules or branch protection is
        conclusive. Any unreadable or unparseable surface leaves the state
        unknown, which never permits optional inline threads.
        """

        required = False
        unknown = False
        try:
            status, payload = self.http.request(
                "GET",
                self.http.repository_path(repository, f"/rules/branches/{branch}"),
                token=token,
            )
        except GitHubHTTPError:
            unknown = True
        else:
            if status == 200 and isinstance(payload, list):
                for rule in payload:
                    if not isinstance(rule, dict) or rule.get("type") != "pull_request":
                        continue
                    parameters = rule.get("parameters")
                    if not isinstance(parameters, dict):
                        continue
                    if parameters.get("required_review_thread_resolution") is True:
                        required = True
            elif status == 404:
                # Repositories without rulesets answer 404 for this endpoint.
                unknown = True
            else:
                unknown = True
        if required:
            return HostPlacementFacts(
                conversation_resolution=CONVERSATION_RESOLUTION_REQUIRED
            )
        try:
            status, payload = self.http.request(
                "GET",
                self.http.repository_path(repository, f"/branches/{branch}/protection"),
                token=token,
            )
        except GitHubHTTPError:
            unknown = True
        else:
            if status == 404:
                # No classic branch protection is configured.
                pass
            elif status == 200 and isinstance(payload, dict):
                conversation = payload.get("required_conversation_resolution")
                if isinstance(conversation, dict) and (
                    conversation.get("enabled") is True
                ):
                    required = True
                elif conversation is not None and not isinstance(conversation, dict):
                    unknown = True
            else:
                unknown = True
        if required:
            return HostPlacementFacts(
                conversation_resolution=CONVERSATION_RESOLUTION_REQUIRED
            )
        if unknown:
            return HostPlacementFacts()
        return HostPlacementFacts(
            conversation_resolution=CONVERSATION_RESOLUTION_NOT_REQUIRED
        )

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
        repository_id: int | None = None,
        complete_review: AssembledReview | None = None,
        base_sha: str | None = None,
        result_digest: str | None = None,
    ) -> tuple[frozenset[str], frozenset[str], ReviewApprovalEligibility | None]:
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
                    if complete_review is not None:
                        self._verify_complete_review(
                            token=token,
                            repository=repository,
                            repository_id=repository_id,
                            pull_request=pull_request,
                            head_sha=head_sha,
                            app_slug=app_slug,
                            review=review,
                            assembled=complete_review,
                            base_sha=base_sha,
                            result_digest=result_digest,
                        )
        eligibility = self.finalizer._eligibility_from_reviews(
            payload,
            head_sha=head_sha,
            app_slug=app_slug,
            require_valid=True,
            token=token,
            repository=repository,
            pull_request=pull_request,
        )
        if repository_id is not None:
            # A complete-inventory replay must validate the retained inventory
            # before the already-published path updates checks or finalizes.
            try:
                self.finalizer.finding_index_from_reviews(
                    payload,
                    repository_id=repository_id,
                    pull_request=pull_request,
                    head_sha=head_sha,
                    app_slug=app_slug,
                    eligibility=eligibility,
                )
            except ReviewInputError:
                raise GitHubPublicationError(
                    "persisted finding index is invalid"
                ) from None
        return frozenset(identity_states), frozenset(result_states), eligibility

    def _verify_complete_review(
        self,
        *,
        token: str,
        repository: str,
        repository_id: int | None,
        pull_request: int,
        head_sha: str,
        app_slug: str,
        review: Mapping[str, Any],
        assembled: AssembledReview,
        base_sha: str | None,
        result_digest: str | None,
    ) -> None:
        """Never relabel an omitted legacy explanation as complete on replay."""
        if repository_id is None:
            raise GitHubPublicationError("complete publication scope is unavailable")
        index = parse_finding_index(
            review["body"],
            repository_id=repository_id,
            pull_request=pull_request,
            head_sha=head_sha,
        )
        user = review.get("user")
        if (
            index is None
            or index.base_sha != base_sha
            or index.result_digest != result_digest
            or index.entries != assembled.index_entries
            or not isinstance(user, dict)
            or user.get("type") != "Bot"
        ):
            raise GitHubPublicationError(
                "complete publication inventory differs from retained review"
            )
        if any(
            render_finding(item.view) not in review["body"]
            for item in assembled.body_explanations
        ):
            raise GitHubPublicationError(
                "complete publication prose is missing from retained review"
            )
        if not assembled.inline_comments:
            return
        review_id = review.get("id")
        if type(review_id) is not int or review_id <= 0:
            raise GitHubPublicationError(
                "complete publication review identity is invalid"
            )
        expected = {
            finding_instance_fingerprint(comment): body
            for comment, _, body, _ in assembled.inline_comments
        }
        observed: set[str] = set()
        comments = self.http.paginate(
            path=self.http.repository_path(
                repository, f"/pulls/{pull_request}/reviews/{review_id}/comments"
            ),
            token=token,
            max_requests=MAX_PAGINATION_PAGES,
            timeout_seconds=60,
        )
        for comment in comments:
            user = comment.get("user") if isinstance(comment, dict) else None
            body = comment.get("body") if isinstance(comment, dict) else None
            instance = finding_instance_from_body(body)
            if (
                not isinstance(comment, dict)
                or type(comment.get("id")) is not int
                or comment["id"] <= 0
                or type(comment.get("pull_request_review_id")) is not int
                or comment["pull_request_review_id"] != review_id
                or not isinstance(user, dict)
                or user.get("type") != "Bot"
                or not isinstance(user.get("login"), str)
                or user["login"].casefold() != app_slug.casefold()
                or instance not in expected
                or instance in observed
                or body != expected[instance]
            ):
                raise GitHubPublicationError(
                    "complete publication inline prose is incomplete"
                )
            observed.add(instance)
        if observed != set(expected):
            raise GitHubPublicationError(
                "complete publication inline prose is incomplete"
            )

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
        identity_states, _, _ = self._published_state_index(
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
        complete_review: AssembledReview | None = None,
        repository_id: int | None = None,
        base_sha: str | None = None,
        result_digest: str | None = None,
    ) -> bool:
        try:
            if complete_review is not None:
                _, states, _ = self._published_state_index(
                    token=token,
                    repository=repository,
                    pull_request=pull_request,
                    head_sha=head_sha,
                    identity_marker=marker,
                    result_marker=marker,
                    app_slug=app_slug,
                    repository_id=repository_id,
                    complete_review=complete_review,
                    base_sha=base_sha,
                    result_digest=result_digest,
                )
                return expected_state in states
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
