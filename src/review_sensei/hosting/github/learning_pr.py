"""Publish validated learning proposals as one safe draft pull request.

The publisher deliberately keeps the durable identity in a small, signed-by-
content (hash-bound) marker and commit trailer.  GitHub and model transports
remain outside the review domain; this module only accepts ``LearningProposal``
values and verifies every object it is about to reconcile.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import NoReturn, cast
from urllib.parse import quote

from ...models import LearningEntry, LearningProposal
from .errors import (
    GitHubHTTPResponseTooLargeError,
    GitHubHTTPTransientError,
    GitHubLearningProposalError,
    GitHubLearningProposalTransientError,
)
from .http import MAX_PAGINATION_PAGES, GitHubHttp

LEARNING_DIRECTORY = ".github/review-sensei/learnings"
GIT_SHA_HEX = re.compile(r"^[a-f0-9]{40}$")
SHA256_HEX = re.compile(r"^[a-f0-9]{64}$")
GITHUB_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)
BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9._/-]{1,255}$")
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_V2_MARKER = re.compile(
    r"^<!-- reviewsensei:learning:v2 repo=(?P<repo>[1-9][0-9]{0,19}) pr=(?P<pr>[1-9][0-9]{0,19}) "
    r"batch=(?P<batch>[a-f0-9]{64}) head=(?P<head>[a-f0-9]{40}) -->$"
)
_V1_MARKER = re.compile(
    r"^<!-- reviewsensei:learning:v1 pr=(?P<pr>[1-9][0-9]{0,19}) "
    r"batch=(?P<batch>[a-f0-9]{64}) -->$"
)
_PROVENANCE = re.compile(
    r"^reviewsensei-learning:v2 repo=(?P<repo>[1-9][0-9]{0,19}) pr=(?P<pr>[1-9][0-9]{0,19}) "
    r"base=(?P<base>[a-f0-9]{40}) batch=(?P<batch>[a-f0-9]{64}) "
    r"head=(?P<head>[a-f0-9]{40}) title=(?P<title>[a-f0-9]{64}) "
    r"body=(?P<body>[a-f0-9]{64})$"
)
_LEGACY_BRANCH = re.compile(
    r"^review-sensei/learnings/pr-(?P<pr>[1-9][0-9]{0,19})-(?P<batch>[a-f0-9]{16})$"
)
_GENERATION_BRANCH = re.compile(
    r"^review-sensei/learnings/pr-(?P<pr>[1-9][0-9]{0,19})-g-(?P<batch>[a-f0-9]{16})(?:-(?P<suffix>[2-9]|10))?$"
)
_STABLE_BRANCH = re.compile(r"^review-sensei/learnings/pr-(?P<pr>[1-9][0-9]{0,19})$")

MAX_TITLE_CHARS = 256
MAX_TITLE_BYTES = 256
MAX_BODY_BYTES = 65_536
MAX_DISPLAY_FIELD_CHARS = 1_024
MAX_SCOPE_FIELD_CHARS = 256
MAX_GENERATION_COLLISIONS = 10
MAX_RECONCILIATION_ATTEMPTS = 2
MAX_COMPARE_FILES = 300
MAX_DECIMAL_ID_DIGITS = 20
MAX_DISCOVERED_PULL_REQUESTS = MAX_PAGINATION_PAGES * 100
PULL_REQUEST_PAGE_SIZES = (20, 10, 5, 1)


def proposal_digest(proposal: LearningProposal) -> str:
    """Return the SHA-256 of a proposal's canonical JSON representation."""

    canonical = json.dumps(
        proposal.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _normalized_proposals(
    proposals: Sequence[LearningProposal],
) -> tuple[LearningProposal, ...]:
    if isinstance(proposals, (str, bytes)) or not isinstance(proposals, Sequence):
        raise GitHubLearningProposalError("learning proposals must be a sequence")
    if not proposals:
        raise GitHubLearningProposalError("learning proposal batch must not be empty")
    by_digest: dict[str, LearningProposal] = {}
    for proposal in proposals:
        if not isinstance(proposal, LearningProposal):
            raise GitHubLearningProposalError(
                "learning proposal batch contains an invalid proposal"
            )
        digest = proposal_digest(proposal)
        old = by_digest.setdefault(digest, proposal)
        if old != proposal:
            raise GitHubLearningProposalError("learning proposal digest conflict")
    return tuple(by_digest[key] for key in sorted(by_digest))


def batch_digest(proposals: Sequence[LearningProposal]) -> str:
    """Return an order-independent digest for a deduplicated proposal batch."""

    normalized = _normalized_proposals(proposals)
    if len(normalized) == 1:
        return proposal_digest(normalized[0])
    canonical = json.dumps(
        [proposal.to_dict() for proposal in normalized],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def learning_path(proposal: LearningProposal) -> str:
    return f"{LEARNING_DIRECTORY}/sensei-{proposal_digest(proposal)[:16]}.json"


def _proposal_content(proposal: LearningProposal) -> str:
    digest = proposal_digest(proposal)
    return (
        json.dumps(
            proposal.to_entry(id=f"sensei-{digest[:16]}").to_dict(),
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sanitize_display_text(value: object, *, limit: int) -> str:
    """Normalize untrusted display text into one bounded Markdown-safe line."""

    if not isinstance(value, str) or not isinstance(limit, int) or limit <= 0:
        return ""
    normalized = unicodedata.normalize("NFC", value)
    cleaned: list[str] = []
    for character in normalized:
        category = unicodedata.category(character)
        if character in {"\r", "\n", "\t"} or category.startswith("C"):
            cleaned.append(" ")
        elif character == "\\":
            cleaned.append("\\\\")
        elif character in {"`", "[", "]"}:
            cleaned.append("\\" + character)
        else:
            cleaned.append(character)
    text = " ".join("".join(cleaned).split()).strip()
    if len(text) > limit:
        text = text[: max(1, limit - 1)].rstrip() + "…"
    return text


def _canonical_source_title(value: object) -> str:
    return _sanitize_display_text(value, limit=MAX_DISPLAY_FIELD_CHARS) or (
        "source pull request"
    )


def _truncate_bytes(value: str, limit: int) -> str:
    if not isinstance(limit, int) or limit <= 0:
        return ""
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    suffix = "…".encode("utf-8")
    if limit <= len(suffix):
        return ""
    return encoded[: max(0, limit - len(suffix))].decode("utf-8", errors="ignore") + "…"


def _unsanitize_display_text(value: str) -> str:
    """Reverse only the Markdown escapes emitted by the metadata renderer."""

    sentinel = "\x00"
    return (
        value.replace("\\\\", sentinel)
        .replace("\\`", "`")
        .replace("\\[", "[")
        .replace("\\]", "]")
        .replace(sentinel, "\\")
    )


def _marker_line(
    *, repository_id: int, pull_request: int, batch: str, head_sha: str
) -> str:
    return (
        f"<!-- reviewsensei:learning:v2 repo={repository_id} pr={pull_request} "
        f"batch={batch} head={head_sha} -->"
    )


@dataclass(frozen=True)
class LearningPRResult:
    status: str
    pull_request_number: int | None = None


@dataclass(frozen=True)
class _Marker:
    repository_id: int
    pull_request: int
    batch: str
    head_sha: str
    version: int


@dataclass(frozen=True)
class _Provenance:
    repository_id: int
    pull_request: int
    base_sha: str
    batch: str
    head_sha: str
    title_sha: str
    body_sha: str
    parent_shas: tuple[str, ...]


@dataclass(frozen=True)
class _Source:
    title: str
    html_url: str
    head_sha: str
    base_sha: str
    base_branch: str


@dataclass(frozen=True)
class _Authority:
    source: _Source
    latest_base_sha: str


@dataclass(frozen=True)
class _Candidate:
    number: int | None
    branch: str
    state: str
    merged: bool
    commit_sha: str
    proposals: tuple[LearningProposal, ...]
    provenance: _Provenance
    title: str
    body: str
    created_at: datetime
    merged_at: str | None = None
    metadata_pending: bool = False


def _parse_marker_line(body: str) -> _Marker | None:
    if not isinstance(body, str):
        return None
    if "\r" in body:
        return None
    lines = [line for line in body.splitlines() if line.strip()]
    if not lines:
        return None
    match = _V2_MARKER.fullmatch(lines[-1])
    if match is not None and len(lines) >= 1:
        return _Marker(
            repository_id=int(match["repo"]),
            pull_request=int(match["pr"]),
            batch=match["batch"],
            head_sha=match["head"],
            version=2,
        )
    match = _V1_MARKER.fullmatch(lines[-1])
    if match is not None:
        return _Marker(
            repository_id=0,
            pull_request=int(match["pr"]),
            batch=match["batch"],
            head_sha="",
            version=1,
        )
    return None


def _has_marker(body: object) -> bool:
    return isinstance(body, str) and any(
        "reviewsensei:learning:v" in line for line in body.splitlines()
    )


def _branch_for_pr(branch: object, pull_request: int) -> bool:
    if not isinstance(branch, str):
        return False
    for pattern in (_STABLE_BRANCH, _GENERATION_BRANCH, _LEGACY_BRANCH):
        match = pattern.fullmatch(branch)
        if match is not None and int(match["pr"]) == pull_request:
            return True
    return False


def _is_sha(value: object) -> bool:
    return isinstance(value, str) and GIT_SHA_HEX.fullmatch(value) is not None


def _parse_github_timestamp(value: object) -> datetime | None:
    """Parse the bounded RFC3339 timestamps returned by GitHub."""

    if not isinstance(value, str) or GITHUB_TIMESTAMP.fullmatch(value) is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc)


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and SHA256_HEX.fullmatch(value) is not None


def _repository_id_matches(value: object, expected: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value == expected


def _parse_provenance(message: object) -> tuple[str, dict[str, str]] | None:
    if not isinstance(message, str):
        return None
    if "\r" in message:
        return None
    lines = message.splitlines()
    if (
        len(lines) != 3
        or lines[0] != "chore: propose ReviewSensei learnings"
        or lines[1] != ""
    ):
        return None
    match = _PROVENANCE.fullmatch(lines[2])
    return (lines[2], match.groupdict()) if match is not None else None


def _decode_base64(payload: object) -> str:
    if not isinstance(payload, dict) or payload.get("encoding") != "base64":
        raise GitHubLearningProposalError("learning content response was invalid")
    encoded = payload.get("content")
    if not isinstance(encoded, str):
        raise GitHubLearningProposalError("learning content response was invalid")
    try:
        return base64.b64decode("".join(encoded.split()), validate=True).decode(
            "utf-8", errors="strict"
        )
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise GitHubLearningProposalError(
            "learning content response was invalid"
        ) from exc


def _raise_response(status: int, message: str) -> NoReturn:
    """Map bounded GitHub status failures without exposing response bodies."""

    if status == 429 or status >= 500:
        raise GitHubLearningProposalTransientError(message)
    raise GitHubLearningProposalError(message)


class LearningPRPublisher:
    """Reconcile one ReviewSensei-owned draft PR for each source PR."""

    def __init__(self, *, http: GitHubHttp) -> None:
        self.http = http

    def propose(
        self,
        *,
        token: str,
        repository: str,
        repository_id: int,
        pull_request: int,
        head_sha: str,
        base_branch: str,
        base_sha: str,
        proposal: LearningProposal,
    ) -> LearningPRResult:
        """Compatibility wrapper for a single proposal."""

        return self.propose_batch(
            token=token,
            repository=repository,
            repository_id=repository_id,
            pull_request=pull_request,
            head_sha=head_sha,
            base_branch=base_branch,
            base_sha=base_sha,
            proposals=(proposal,),
        )

    def propose_batch(
        self,
        *,
        token: str,
        repository: str,
        repository_id: int,
        pull_request: int,
        head_sha: str,
        base_branch: str,
        base_sha: str,
        proposals: Sequence[LearningProposal],
    ) -> LearningPRResult:
        """Create, refresh, or safely reuse one draft learning PR."""

        self._validate_inputs(
            repository=repository,
            repository_id=repository_id,
            pull_request=pull_request,
            head_sha=head_sha,
            base_branch=base_branch,
            base_sha=base_sha,
        )
        normalized = _normalized_proposals(proposals)
        self._validate_path_collisions(normalized)
        for _attempt in range(MAX_RECONCILIATION_ATTEMPTS):
            outcome = self._propose_attempt(
                token=token,
                repository=repository,
                repository_id=repository_id,
                pull_request=pull_request,
                head_sha=head_sha,
                base_branch=base_branch,
                caller_base_sha=base_sha,
                proposals=normalized,
            )
            if outcome is not None:
                return outcome
        raise GitHubLearningProposalTransientError(
            "default branch advanced during learning publication"
        )

    @staticmethod
    def _validate_inputs(
        *,
        repository: str,
        repository_id: int,
        pull_request: int,
        head_sha: str,
        base_branch: str,
        base_sha: str,
    ) -> None:
        if (
            not isinstance(repository, str)
            or REPOSITORY_PATTERN.fullmatch(repository) is None
        ):
            raise GitHubLearningProposalError("learning repository is invalid")
        if (
            isinstance(repository_id, bool)
            or not isinstance(repository_id, int)
            or repository_id < 1
            or len(str(repository_id)) > MAX_DECIMAL_ID_DIGITS
        ):
            raise GitHubLearningProposalError("source repository is invalid")
        if (
            isinstance(pull_request, bool)
            or not isinstance(pull_request, int)
            or pull_request < 1
            or len(str(pull_request)) > MAX_DECIMAL_ID_DIGITS
        ):
            raise GitHubLearningProposalError("source pull request is invalid")
        if not _is_sha(head_sha) or not _is_sha(base_sha):
            raise GitHubLearningProposalError("learning source SHA is invalid")
        if (
            not isinstance(base_branch, str)
            or not base_branch.strip()
            or BRANCH_PATTERN.fullmatch(base_branch) is None
            or base_branch.startswith((".", "/"))
            or base_branch.endswith((".", "/"))
            or ".." in base_branch
            or "//" in base_branch
        ):
            raise GitHubLearningProposalError("learning base branch is invalid")
        stable_branch = f"review-sensei/learnings/pr-{pull_request}"
        if len(stable_branch.encode("ascii")) > 255:
            raise GitHubLearningProposalError("learning branch identity is invalid")

    @staticmethod
    def _validate_path_collisions(proposals: Sequence[LearningProposal]) -> None:
        paths: dict[str, str] = {}
        for proposal in proposals:
            path = learning_path(proposal)
            digest = proposal_digest(proposal)
            old = paths.setdefault(path, digest)
            if old != digest:
                raise GitHubLearningProposalError("learning path collision")

    def _propose_attempt(
        self,
        *,
        token: str,
        repository: str,
        repository_id: int,
        pull_request: int,
        head_sha: str,
        base_branch: str,
        caller_base_sha: str,
        proposals: tuple[LearningProposal, ...],
    ) -> LearningPRResult | None:
        authority = self._read_authority(
            token=token,
            repository=repository,
            repository_id=repository_id,
            pull_request=pull_request,
            head_sha=head_sha,
            base_branch=base_branch,
            caller_base_sha=caller_base_sha,
        )
        if isinstance(authority, LearningPRResult):
            return authority

        open_candidates = self._discover_candidates(
            token=token,
            repository=repository,
            repository_id=repository_id,
            pull_request=pull_request,
            base_branch=authority.source.base_branch,
            include_closed=False,
        )
        if len(open_candidates) > 1:
            raise GitHubLearningProposalError(
                "multiple open learning PRs are ambiguous"
            )
        if open_candidates:
            return self._refresh_open_candidate(
                token=token,
                repository=repository,
                repository_id=repository_id,
                pull_request=pull_request,
                head_sha=head_sha,
                caller_base_sha=caller_base_sha,
                authority=authority,
                candidate=open_candidates[0],
                proposals=proposals,
            )

        candidates = self._discover_candidates(
            token=token,
            repository=repository,
            repository_id=repository_id,
            pull_request=pull_request,
            base_branch=authority.source.base_branch,
            include_closed=True,
        )
        late_open = [candidate for candidate in candidates if candidate.state == "open"]
        if len(late_open) > 1:
            raise GitHubLearningProposalError(
                "multiple open learning PRs are ambiguous"
            )
        if late_open:
            return self._refresh_open_candidate(
                token=token,
                repository=repository,
                repository_id=repository_id,
                pull_request=pull_request,
                head_sha=head_sha,
                caller_base_sha=caller_base_sha,
                authority=authority,
                candidate=late_open[0],
                proposals=proposals,
            )
        closed_unmerged = [
            candidate
            for candidate in candidates
            if candidate.state == "closed" and not candidate.merged
        ]
        if len(closed_unmerged) > 1:
            raise GitHubLearningProposalError(
                "multiple closed-unmerged learning PRs are ambiguous"
            )
        if closed_unmerged:
            return LearningPRResult(status="skipped_closed_pull_request")
        merged = [
            candidate
            for candidate in candidates
            if candidate.state == "closed" and candidate.merged
        ]
        merged.sort(
            key=lambda candidate: (candidate.merged_at or "", candidate.number or 0),
            reverse=True,
        )
        pending = self._pending_against_base(
            token=token,
            repository=repository,
            base_sha=authority.latest_base_sha,
            proposals=proposals,
        )
        if not pending:
            return LearningPRResult(status="skipped_identical")
        branch = self._select_generation_branch(
            token=token,
            repository=repository,
            repository_id=repository_id,
            pull_request=pull_request,
            batch=batch_digest(pending),
            latest_base_sha=authority.latest_base_sha,
            merged=bool(merged),
            merged_branches={candidate.branch for candidate in merged},
        )
        title, body = self._render_metadata(
            repository=repository,
            pull_request=pull_request,
            source=authority.source,
            head_sha=head_sha,
            proposals=pending,
            repository_id=repository_id,
        )
        final = self._read_authority(
            token=token,
            repository=repository,
            repository_id=repository_id,
            pull_request=pull_request,
            head_sha=head_sha,
            base_branch=base_branch,
            caller_base_sha=caller_base_sha,
        )
        if isinstance(final, LearningPRResult):
            return final
        if final.latest_base_sha != authority.latest_base_sha:
            return None
        # The source PR title is part of the generated metadata.  If the
        # authority reread observes a rename after rendering, retry the whole
        # attempt so the commit and pull request are born with the current
        # title rather than creating a stale artifact whose rename history
        # predates its creation boundary.
        if final.source != authority.source:
            return None
        commit_sha = self._recover_orphan_commit(
            token=token,
            repository=repository,
            repository_id=repository_id,
            pull_request=pull_request,
            branch=branch,
            base_sha=authority.latest_base_sha,
            head_sha=head_sha,
            batch=batch_digest(pending),
            title=title,
            body=body,
            proposals=pending,
        )
        if commit_sha is None:
            commit_sha = self._create_learning_commit(
                token=token,
                repository=repository,
                repository_id=repository_id,
                pull_request=pull_request,
                base_sha=authority.latest_base_sha,
                head_sha=head_sha,
                batch=batch_digest(pending),
                title=title,
                body=body,
                proposals=pending,
                parents=(authority.latest_base_sha,),
            )
            self._create_or_recover_ref(
                token=token,
                repository=repository,
                branch=branch,
                commit_sha=commit_sha,
            )
        final_after_ref = self._read_authority(
            token=token,
            repository=repository,
            repository_id=repository_id,
            pull_request=pull_request,
            head_sha=head_sha,
            base_branch=base_branch,
            caller_base_sha=caller_base_sha,
        )
        if isinstance(final_after_ref, LearningPRResult):
            return final_after_ref
        if final_after_ref.latest_base_sha != authority.latest_base_sha:
            return None
        if (
            self._read_ref(token=token, repository=repository, branch=branch)
            != commit_sha
        ):
            raise GitHubLearningProposalTransientError(
                "learning branch changed before pull-request creation"
            )
        return self._create_learning_pull_request(
            token=token,
            repository=repository,
            repository_id=repository_id,
            branch=branch,
            base_branch=authority.source.base_branch,
            title=title,
            body=body,
        )

    def _read_authority(
        self,
        *,
        token: str,
        repository: str,
        repository_id: int,
        pull_request: int,
        head_sha: str,
        base_branch: str,
        caller_base_sha: str,
    ) -> _Authority | LearningPRResult:
        status, repo_payload = self._request(
            "GET", self.http.repository_path(repository), token=token
        )
        if status == 404:
            raise GitHubLearningProposalError("learning repository was not found")
        if status < 200 or status >= 300 or not isinstance(repo_payload, dict):
            _raise_response(status, "learning repository lookup failed")
        if (
            not _repository_id_matches(repo_payload.get("id"), repository_id)
            or repo_payload.get("full_name") != repository
            or repo_payload.get("fork") is not False
            or repo_payload.get("default_branch") != base_branch
        ):
            return LearningPRResult(status="skipped_repository_mismatch")

        status, source_payload = self._request(
            "GET",
            self.http.repository_path(repository, f"/pulls/{pull_request}"),
            token=token,
        )
        if status == 404:
            raise GitHubLearningProposalError("learning source was not found")
        if status < 200 or status >= 300 or not isinstance(source_payload, dict):
            _raise_response(status, "learning source preflight failed")
        if (
            source_payload.get("state") != "open"
            or source_payload.get("draft") is not False
        ):
            return LearningPRResult(status="skipped_pr_state")
        head = source_payload.get("head")
        base = source_payload.get("base")
        if not isinstance(head, dict) or not isinstance(base, dict):
            raise GitHubLearningProposalError("learning source binding was invalid")
        if head.get("sha") != head_sha:
            return LearningPRResult(status="skipped_stale_source")
        if base.get("ref") != base_branch:
            return LearningPRResult(status="skipped_stale_base")
        head_repo = head.get("repo")
        base_repo = base.get("repo")
        if not isinstance(head_repo, dict) or not isinstance(base_repo, dict):
            raise GitHubLearningProposalError("learning source repository was invalid")
        if (
            head_repo.get("full_name") != repository
            or head_repo.get("fork") is not False
            or not _repository_id_matches(head_repo.get("id"), repository_id)
        ):
            return LearningPRResult(status="skipped_fork")
        if (
            not _repository_id_matches(base_repo.get("id"), repository_id)
            or base_repo.get("full_name") != repository
            or base_repo.get("fork") is not False
        ):
            return LearningPRResult(status="skipped_repository_mismatch")
        source_base_sha = cast(str | None, base.get("sha"))
        if not _is_sha(source_base_sha):
            raise GitHubLearningProposalError("learning source base SHA was invalid")

        status, ref_payload = self._request(
            "GET",
            self.http.repository_path(
                repository, f"/git/ref/heads/{quote(base_branch, safe='/')}"
            ),
            token=token,
        )
        if status < 200 or status >= 300 or not isinstance(ref_payload, dict):
            _raise_response(status, "learning default branch lookup failed")
        ref_object = ref_payload.get("object")
        latest_sha = (
            cast(str | None, ref_object.get("sha"))
            if isinstance(ref_object, dict)
            else None
        )
        if not _is_sha(latest_sha):
            raise GitHubLearningProposalError("learning default branch SHA was invalid")
        # The caller may be stale, and GitHub may have advanced after the
        # source event.  A source base that is neither caller nor latest is
        # an ambiguous replay and must not write.
        if source_base_sha not in {caller_base_sha, latest_sha}:
            return LearningPRResult(status="skipped_stale_base")
        title = source_payload.get("title")
        if not isinstance(title, str) or not title.strip():
            title = f"pull request #{pull_request}"
        html_url = source_payload.get("html_url")
        if not isinstance(html_url, str) or not html_url.startswith("https://"):
            html_url = f"https://github.com/{repository}/pull/{pull_request}"
        return _Authority(
            source=_Source(
                title=title,
                html_url=html_url,
                head_sha=head_sha,
                base_sha=cast(str, source_base_sha),
                base_branch=base_branch,
            ),
            latest_base_sha=cast(str, latest_sha),
        )

    def _discover_candidates(
        self,
        *,
        token: str,
        repository: str,
        repository_id: int,
        pull_request: int,
        base_branch: str,
        include_closed: bool,
    ) -> list[_Candidate]:
        candidates: list[_Candidate] = []
        for state in ("all",) if include_closed else ("open",):
            items = self._list_pull_requests(
                token=token, repository=repository, state=state
            )
            for raw_item in items:
                item = self._validate_pull_request_summary(raw_item)
                head = item.get("head")
                branch = head.get("ref") if isinstance(head, dict) else None
                body = item.get("body")
                marker = _parse_marker_line(body) if isinstance(body, str) else None
                related = (
                    _branch_for_pr(branch, pull_request)
                    or (marker is not None and marker.pull_request == pull_request)
                    or _has_marker(body)
                )
                if not related:
                    continue
                if not isinstance(branch, str) or not _branch_for_pr(
                    branch, pull_request
                ):
                    # A marker claiming this source PR on another branch is
                    # an ownership collision, not an invitation to mutate.
                    if marker is not None and marker.pull_request == pull_request:
                        raise GitHubLearningProposalError(
                            "learning PR branch identity is invalid"
                        )
                    continue
                candidate = self._load_candidate(
                    token=token,
                    repository=repository,
                    repository_id=repository_id,
                    pull_request=pull_request,
                    base_branch=base_branch,
                    item=item,
                    branch=branch,
                )
                if candidate is not None:
                    candidates.append(candidate)
        return candidates

    def _list_pull_requests(
        self, *, token: str, repository: str, state: str
    ) -> list[object]:
        path = self.http.repository_path(repository, "/pulls")
        all_items: list[object] = []
        page_size_index = 0
        while len(all_items) < MAX_DISCOVERED_PULL_REQUESTS:
            page_size = PULL_REQUEST_PAGE_SIZES[page_size_index]
            page = len(all_items) // page_size + 1
            try:
                status, payload = self._request(
                    "GET",
                    f"{path}?state={state}&per_page={page_size}&page={page}",
                    token=token,
                )
            except GitHubHTTPResponseTooLargeError:
                if page_size_index + 1 >= len(PULL_REQUEST_PAGE_SIZES):
                    raise
                page_size_index += 1
                next_page_size = PULL_REQUEST_PAGE_SIZES[page_size_index]
                if len(all_items) % next_page_size:
                    raise GitHubLearningProposalError(
                        "learning PR pagination offset was invalid"
                    )
                continue
            if status < 200 or status >= 300 or not isinstance(payload, list):
                _raise_response(status, "learning PR lookup failed")
            if len(payload) > page_size:
                raise GitHubLearningProposalError(
                    "learning PR page exceeded its requested size"
                )
            all_items.extend(payload)
            if len(payload) < page_size:
                return all_items
        raise GitHubLearningProposalError("learning PR pagination exceeded its limit")

    @staticmethod
    def _validate_pull_request_summary(item: object) -> dict[str, object]:
        """Require enough list-payload identity to make discovery fail closed."""

        if not isinstance(item, dict):
            raise GitHubLearningProposalError("learning PR list entry was incomplete")
        number = item.get("number")
        head = item.get("head")
        base = item.get("base")
        body = item.get("body")
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number < 1
            or item.get("state") not in {"open", "closed"}
            or not isinstance(item.get("draft"), bool)
            or "body" not in item
            or (body is not None and not isinstance(body, str))
            or not isinstance(head, dict)
            or not isinstance(head.get("ref"), str)
            or not head.get("ref")
            or not isinstance(base, dict)
            or not isinstance(base.get("ref"), str)
            or not base.get("ref")
        ):
            raise GitHubLearningProposalError("learning PR list entry was incomplete")
        return cast(dict[str, object], item)

    def _load_candidate(
        self,
        *,
        token: str,
        repository: str,
        repository_id: int,
        pull_request: int,
        base_branch: str,
        item: dict[str, object],
        branch: str,
    ) -> _Candidate | None:
        number = item.get("number")
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise GitHubLearningProposalError("learning PR identity was invalid")
        status, detail = self._request(
            "GET",
            self.http.repository_path(repository, f"/pulls/{number}"),
            token=token,
        )
        if status < 200 or status >= 300 or not isinstance(detail, dict):
            _raise_response(status, "learning PR detail lookup failed")
        detail_head_summary = detail.get("head")
        detail_branch = (
            detail_head_summary.get("ref")
            if isinstance(detail_head_summary, dict)
            else None
        )
        if detail_branch != branch:
            raise GitHubLearningProposalError("learning PR head branch changed")
        state = detail.get("state")
        if state not in {"open", "closed"}:
            raise GitHubLearningProposalError("learning PR state was invalid")
        created_at = _parse_github_timestamp(detail.get("created_at"))
        if created_at is None:
            raise GitHubLearningProposalError(
                "learning PR creation timestamp was invalid"
            )
        if state == "open" and detail.get("draft") is not True:
            raise GitHubLearningProposalError("learning PR must remain a draft")
        detail_base = detail.get("base")
        if not isinstance(detail_base, dict) or detail_base.get("ref") != base_branch:
            raise GitHubLearningProposalError("learning PR base branch changed")
        detail_base_repo = detail_base.get("repo")
        if not isinstance(detail_base_repo, dict) or (
            not _repository_id_matches(detail_base_repo.get("id"), repository_id)
            or detail_base_repo.get("full_name") != repository
            or detail_base_repo.get("fork") is not False
        ):
            raise GitHubLearningProposalError(
                "learning PR base repository is not owned"
            )
        detail_head = detail.get("head")
        if not isinstance(detail_head, dict):
            raise GitHubLearningProposalError("learning PR head was invalid")
        head_repo = detail_head.get("repo")
        if state == "open" and not isinstance(head_repo, dict):
            raise GitHubLearningProposalError(
                "open learning PR head repository was deleted"
            )
        if isinstance(head_repo, dict) and (
            head_repo.get("full_name") != repository
            or head_repo.get("fork") is not False
            or not _repository_id_matches(head_repo.get("id"), repository_id)
        ):
            raise GitHubLearningProposalError(
                "learning PR head repository is not owned"
            )
        commit_sha = detail_head.get("sha")
        if not _is_sha(commit_sha):
            raise GitHubLearningProposalError("learning PR head SHA was invalid")
        body = detail.get("body")
        if not isinstance(body, str):
            raise GitHubLearningProposalError("learning PR body was invalid")
        marker = _parse_marker_line(body)
        if marker is None:
            raise GitHubLearningProposalError("legacy or malformed learning PR marker")
        if marker.version == 1:
            legacy = _LEGACY_BRANCH.fullmatch(branch)
            if legacy is None or legacy["batch"] != marker.batch[:16]:
                raise GitHubLearningProposalError(
                    "legacy learning PR branch identity is invalid"
                )
            ref_sha = self._read_ref(token=token, repository=repository, branch=branch)
            if state == "open" and ref_sha != commit_sha:
                raise GitHubLearningProposalError(
                    "open learning PR branch is missing or changed"
                )
            if state == "closed" and ref_sha is not None and ref_sha != commit_sha:
                raise GitHubLearningProposalError(
                    "closed learning PR branch tip changed"
                )
            provenance, proposals = self._read_legacy_candidate(
                token=token,
                repository=repository,
                repository_id=repository_id,
                pull_request=pull_request,
                commit_sha=cast(str, commit_sha),
                marker=marker,
            )
            expected_body = self._render_legacy_body(proposals, marker)
            if (
                body != expected_body
                or detail.get("title") != "Propose ReviewSensei learnings"
            ):
                raise GitHubLearningProposalError(
                    "legacy learning PR metadata was edited"
                )
            return _Candidate(
                number=number,
                branch=branch,
                state=state,
                merged=detail.get("merged") is True,
                commit_sha=cast(str, commit_sha),
                proposals=proposals,
                provenance=provenance,
                title=cast(str, detail.get("title"))
                if isinstance(detail.get("title"), str)
                else "",
                body=body,
                created_at=created_at,
                merged_at=(
                    cast(str, detail.get("merged_at"))
                    if isinstance(detail.get("merged_at"), str)
                    else None
                ),
            )
        if marker.version != 2:
            raise GitHubLearningProposalError("learning PR marker version was invalid")
        if marker.repository_id != repository_id or marker.pull_request != pull_request:
            raise GitHubLearningProposalError("learning PR marker identity changed")
        ref_sha = self._read_ref(token=token, repository=repository, branch=branch)
        if state == "open" and ref_sha != commit_sha:
            raise GitHubLearningProposalError(
                "open learning PR branch is missing or changed"
            )
        if state == "closed" and ref_sha is not None and ref_sha != commit_sha:
            raise GitHubLearningProposalError("closed learning PR branch tip changed")
        commit, provenance, proposals = self._read_candidate_commit(
            token=token,
            repository=repository,
            repository_id=repository_id,
            pull_request=pull_request,
            commit_sha=cast(str, commit_sha),
            marker=None,
        )
        if provenance.base_sha != commit["base_sha"]:
            raise GitHubLearningProposalError("learning commit provenance was invalid")
        title = detail.get("title")
        if not isinstance(title, str):
            raise GitHubLearningProposalError(
                "learning PR metadata was edited outside ReviewSensei"
            )
        metadata_pending = False
        if provenance.batch != marker.batch or provenance.head_sha != marker.head_sha:
            # A process may have advanced the branch before it patched the PR
            # metadata.  Recover only the exact transition from the prior
            # canonical commit: the old marker and byte hashes must match the
            # first parent, and no user text may be overwritten.
            if state != "open" or not provenance.parent_shas:
                raise GitHubLearningProposalError(
                    "learning PR metadata was edited outside ReviewSensei"
                )
            previous_commit, previous_provenance, _previous_proposals = (
                self._read_candidate_commit(
                    token=token,
                    repository=repository,
                    repository_id=repository_id,
                    pull_request=pull_request,
                    commit_sha=provenance.parent_shas[0],
                    marker=None,
                )
            )
            if (
                previous_provenance.batch != marker.batch
                or previous_provenance.head_sha != marker.head_sha
                or previous_provenance.title_sha != _sha256_text(title)
                or previous_provenance.body_sha != _sha256_text(body)
                or previous_provenance.base_sha != previous_commit["base_sha"]
            ):
                raise GitHubLearningProposalError(
                    "learning PR metadata was edited outside ReviewSensei"
                )
            metadata_pending = True
        elif (
            _sha256_text(body) != provenance.body_sha
            or _sha256_text(title) != provenance.title_sha
        ):
            raise GitHubLearningProposalError(
                "learning PR metadata was edited outside ReviewSensei"
            )
        merged = detail.get("merged") is True
        return _Candidate(
            number=number,
            branch=branch,
            state=state,
            merged=merged,
            commit_sha=cast(str, commit_sha),
            proposals=proposals,
            provenance=provenance,
            title=title,
            body=body,
            created_at=created_at,
            merged_at=(
                cast(str, detail.get("merged_at"))
                if isinstance(detail.get("merged_at"), str)
                else None
            ),
            metadata_pending=metadata_pending,
        )

    def _read_legacy_candidate(
        self,
        *,
        token: str,
        repository: str,
        repository_id: int,
        pull_request: int,
        commit_sha: str,
        marker: _Marker,
    ) -> tuple[_Provenance, tuple[LearningProposal, ...]]:
        if not _is_sha(commit_sha):
            raise GitHubLearningProposalError(
                "legacy learning commit binding was invalid"
            )
        status, payload = self._request(
            "GET",
            self.http.repository_path(repository, f"/git/commits/{commit_sha}"),
            token=token,
        )
        if status < 200 or status >= 300 or not isinstance(payload, dict):
            _raise_response(status, "legacy learning commit lookup failed")
        payload_sha = payload.get("sha")
        if not _is_sha(payload_sha) or payload_sha != commit_sha:
            raise GitHubLearningProposalError(
                "legacy learning commit identity was invalid"
            )
        parents = payload.get("parents")
        parent_sha = (
            parents[0].get("sha")
            if isinstance(parents, list)
            and len(parents) == 1
            and isinstance(parents[0], dict)
            else None
        )
        if (
            not isinstance(parents, list)
            or len(parents) != 1
            or not _is_sha(parent_sha)
        ):
            raise GitHubLearningProposalError(
                "legacy learning commit parent was invalid"
            )
        base_sha = cast(str, parent_sha)
        tree = payload.get("tree")
        tree_sha = tree.get("sha") if isinstance(tree, dict) else None
        if not _is_sha(tree_sha):
            raise GitHubLearningProposalError("legacy learning commit tree was invalid")
        proposals = self._read_candidate_files(
            token=token,
            repository=repository,
            base_sha=base_sha,
            tip_tree_sha=cast(str, tree_sha),
            commit_sha=commit_sha,
            allow_legacy_ascii=True,
        )
        if not proposals or batch_digest(proposals) != marker.batch:
            raise GitHubLearningProposalError(
                "legacy learning batch provenance was invalid"
            )
        return (
            _Provenance(
                repository_id=repository_id,
                pull_request=pull_request,
                base_sha=base_sha,
                batch=marker.batch,
                head_sha="",
                title_sha="",
                body_sha="",
                parent_shas=(base_sha,),
            ),
            proposals,
        )

    def _read_candidate_commit(
        self,
        *,
        token: str,
        repository: str,
        repository_id: int,
        pull_request: int,
        commit_sha: str,
        marker: _Marker | None,
    ) -> tuple[dict[str, str], _Provenance, tuple[LearningProposal, ...]]:
        status, payload = self._request(
            "GET",
            self.http.repository_path(repository, f"/git/commits/{commit_sha}"),
            token=token,
        )
        if status < 200 or status >= 300 or not isinstance(payload, dict):
            _raise_response(status, "learning commit lookup failed")
        payload_sha = payload.get("sha")
        if not _is_sha(payload_sha) or payload_sha != commit_sha:
            raise GitHubLearningProposalError("learning commit identity was invalid")
        parsed = _parse_provenance(payload.get("message"))
        if parsed is None:
            raise GitHubLearningProposalError("learning commit provenance was missing")
        _line, values = parsed
        provenance = _Provenance(
            repository_id=int(values["repo"]),
            pull_request=int(values["pr"]),
            base_sha=values["base"],
            batch=values["batch"],
            head_sha=values["head"],
            title_sha=values["title"],
            body_sha=values["body"],
            parent_shas=tuple(
                cast(str, parent.get("sha"))
                for parent in cast(list[object], payload.get("parents", []))
                if isinstance(parent, dict) and isinstance(parent.get("sha"), str)
            ),
        )
        if (
            provenance.repository_id != repository_id
            or provenance.pull_request != pull_request
            or (
                marker is not None
                and (
                    provenance.batch != marker.batch
                    or provenance.head_sha != marker.head_sha
                )
            )
            or len(provenance.parent_shas) not in {1, 2}
            or not _is_sha(provenance.base_sha)
            or any(not _is_sha(parent) for parent in provenance.parent_shas)
        ):
            raise GitHubLearningProposalError(
                "learning commit provenance identity is invalid"
            )
        parents = payload.get("parents")
        if not isinstance(parents, list) or len(parents) != len(provenance.parent_shas):
            raise GitHubLearningProposalError("learning commit parents were invalid")
        if (
            len(provenance.parent_shas) == 2
            and provenance.parent_shas[1] != provenance.base_sha
        ):
            raise GitHubLearningProposalError(
                "learning refresh commit base parent was invalid"
            )
        if (
            len(provenance.parent_shas) == 2
            or provenance.parent_shas[0] != provenance.base_sha
        ):
            self._validate_refresh_parent(
                token=token,
                repository=repository,
                repository_id=repository_id,
                pull_request=pull_request,
                parent_sha=provenance.parent_shas[0],
            )
        tree = payload.get("tree")
        tree_sha = cast(str | None, tree.get("sha")) if isinstance(tree, dict) else None
        if not _is_sha(tree_sha):
            raise GitHubLearningProposalError("learning commit tree was invalid")
        proposals = self._read_candidate_files(
            token=token,
            repository=repository,
            base_sha=provenance.base_sha,
            tip_tree_sha=cast(str, tree_sha),
            commit_sha=commit_sha,
        )
        if not proposals or batch_digest(proposals) != provenance.batch:
            raise GitHubLearningProposalError(
                "learning commit batch provenance was invalid"
            )
        return (
            {"base_sha": provenance.base_sha, "tree_sha": cast(str, tree_sha)},
            provenance,
            proposals,
        )

    def _validate_refresh_parent(
        self,
        *,
        token: str,
        repository: str,
        repository_id: int,
        pull_request: int,
        parent_sha: str,
    ) -> None:
        status, payload = self._request(
            "GET",
            self.http.repository_path(repository, f"/git/commits/{parent_sha}"),
            token=token,
        )
        if status < 200 or status >= 300 or not isinstance(payload, dict):
            _raise_response(status, "learning refresh parent lookup failed")
        payload_sha = payload.get("sha")
        if not _is_sha(payload_sha) or payload_sha != parent_sha:
            raise GitHubLearningProposalError(
                "learning refresh parent identity was invalid"
            )
        parsed = _parse_provenance(payload.get("message"))
        if parsed is None:
            # A v2 refresh may be the first write that upgrades a proven v1
            # branch.  The legacy parent has no trailer, so prove its exact
            # historical commit shape and canonical tree instead.
            parents = payload.get("parents")
            tree = payload.get("tree")
            parent_values = (
                [parent.get("sha") for parent in parents if isinstance(parent, dict)]
                if isinstance(parents, list)
                else []
            )
            tree_sha = tree.get("sha") if isinstance(tree, dict) else None
            if (
                payload.get("message") == "chore: propose ReviewSensei learnings"
                and len(parent_values) == 1
                and _is_sha(parent_values[0])
                and _is_sha(tree_sha)
            ):
                self._read_candidate_files(
                    token=token,
                    repository=repository,
                    base_sha=cast(str, parent_values[0]),
                    tip_tree_sha=cast(str, tree_sha),
                    commit_sha=parent_sha,
                    allow_legacy_ascii=True,
                )
                return
            raise GitHubLearningProposalError(
                "learning refresh parent provenance was invalid"
            )
        _line, values = parsed
        if (
            int(values["repo"]) != repository_id
            or int(values["pr"]) != pull_request
            or not _is_sha(values["base"])
            or not _is_sha(values["head"])
        ):
            raise GitHubLearningProposalError(
                "learning refresh parent provenance was invalid"
            )
        parents = payload.get("parents")
        if not isinstance(parents, list) or len(parents) not in {1, 2}:
            raise GitHubLearningProposalError(
                "learning refresh parent provenance was invalid"
            )
        parent_values = [
            parent.get("sha") for parent in parents if isinstance(parent, dict)
        ]
        if len(parent_values) != len(parents) or any(
            not _is_sha(value) for value in parent_values
        ):
            raise GitHubLearningProposalError(
                "learning refresh parent provenance was invalid"
            )
        if len(parent_values) == 2 and parent_values[1] != values["base"]:
            raise GitHubLearningProposalError(
                "learning refresh parent provenance was invalid"
            )
        tree = payload.get("tree")
        tree_sha = tree.get("sha") if isinstance(tree, dict) else None
        if not _is_sha(tree_sha):
            raise GitHubLearningProposalError(
                "learning refresh parent tree was invalid"
            )
        proposals = self._read_candidate_files(
            token=token,
            repository=repository,
            base_sha=values["base"],
            tip_tree_sha=cast(str, tree_sha),
            commit_sha=parent_sha,
        )
        if not proposals or batch_digest(proposals) != values["batch"]:
            raise GitHubLearningProposalError(
                "learning refresh parent batch provenance was invalid"
            )

    def _read_candidate_files(
        self,
        *,
        token: str,
        repository: str,
        base_sha: str,
        tip_tree_sha: str,
        commit_sha: str,
        allow_legacy_ascii: bool = False,
    ) -> tuple[LearningProposal, ...]:
        base_tree_sha = self._commit_tree_sha(
            token=token, repository=repository, commit_sha=base_sha
        )
        base_tree = self._read_tree(
            token=token, repository=repository, tree_sha=base_tree_sha
        )
        tip_tree = self._read_tree(
            token=token, repository=repository, tree_sha=tip_tree_sha
        )
        base_files = self._tree_files(base_tree)
        tip_files = self._tree_files(tip_tree)
        all_paths = set(base_files) | set(tip_files)
        changed: set[str] = set()
        for path in all_paths:
            if base_files.get(path) != tip_files.get(path):
                changed.add(path)
        if len(changed) > MAX_COMPARE_FILES:
            raise GitHubLearningProposalError(
                "learning branch changes exceed the safety limit"
            )
        if any(not path.startswith(f"{LEARNING_DIRECTORY}/") for path in changed):
            raise GitHubLearningProposalError(
                "learning branch changed an unmanaged path"
            )
        status, comparison = self._request(
            "GET",
            self.http.repository_path(
                repository, f"/compare/{base_sha}...{commit_sha}"
            ),
            token=token,
        )
        if status < 200 or status >= 300 or not isinstance(comparison, dict):
            _raise_response(status, "learning branch comparison failed")
        files = comparison.get("files")
        if not isinstance(files, list) or len(files) > MAX_COMPARE_FILES:
            raise GitHubLearningProposalError(
                "learning branch comparison was incomplete"
            )
        compared: set[str] = set()
        for entry in files:
            if not isinstance(entry, dict) or not isinstance(
                entry.get("filename"), str
            ):
                raise GitHubLearningProposalError(
                    "learning branch comparison was invalid"
                )
            filename = entry["filename"]
            if filename in compared:
                raise GitHubLearningProposalError(
                    "learning branch comparison contained a duplicate file"
                )
            if (
                entry.get("status") not in {"added", "modified"}
                or filename not in changed
            ):
                raise GitHubLearningProposalError(
                    "learning branch contains an unsafe change"
                )
            if "previous_filename" in entry:
                raise GitHubLearningProposalError("learning branch contains a rename")
            compared.add(filename)
        if compared != changed:
            raise GitHubLearningProposalError(
                "learning branch comparison was incomplete"
            )
        proposals: list[LearningProposal] = []
        for path in sorted(changed):
            blob_sha = tip_files.get(path)
            if not _is_sha(blob_sha):
                raise GitHubLearningProposalError(
                    "learning branch deleted a managed file"
                )
            status, blob = self._request(
                "GET",
                self.http.repository_path(repository, f"/git/blobs/{blob_sha}"),
                token=token,
            )
            content = _decode_base64(blob) if status >= 200 and status < 300 else None
            if content is None:
                _raise_response(status, "learning branch blob lookup failed")
            try:
                raw = json.loads(content)
                entry = LearningEntry.from_dict(raw)
            except Exception as exc:
                raise GitHubLearningProposalError(
                    "learning branch learning entry was invalid"
                ) from exc
            expected_id = path.rsplit("/", 1)[-1].removesuffix(".json")
            proposal = LearningProposal(
                title=entry.title,
                rule=entry.rule,
                scope=entry.scope,
                rationale=entry.rationale,
                category=entry.category,
            )
            canonical_content = _proposal_content(proposal)
            legacy_content = (
                json.dumps(
                    proposal.to_entry(id=expected_id).to_dict(),
                    indent=2,
                )
                + "\n"
            )
            if (
                entry.id != expected_id
                or entry.status != "active"
                or entry.source is not None
                or (
                    content != canonical_content
                    and not (allow_legacy_ascii and content == legacy_content)
                )
                or learning_path(proposal) != path
            ):
                raise GitHubLearningProposalError(
                    "learning branch learning entry was not canonical"
                )
            proposals.append(proposal)
        return tuple(proposals)

    def _commit_tree_sha(self, *, token: str, repository: str, commit_sha: str) -> str:
        status, payload = self._request(
            "GET",
            self.http.repository_path(repository, f"/git/commits/{commit_sha}"),
            token=token,
        )
        if status < 200 or status >= 300 or not isinstance(payload, dict):
            _raise_response(status, "learning commit tree lookup failed")
        payload_sha = payload.get("sha")
        if not _is_sha(payload_sha) or payload_sha != commit_sha:
            raise GitHubLearningProposalError("learning commit identity was invalid")
        tree = payload.get("tree")
        tree_sha = cast(str | None, tree.get("sha")) if isinstance(tree, dict) else None
        if not _is_sha(tree_sha):
            raise GitHubLearningProposalError("learning commit tree was invalid")
        return cast(str, tree_sha)

    def _read_tree(
        self, *, token: str, repository: str, tree_sha: str
    ) -> list[dict[str, object]]:
        status, payload = self._request(
            "GET",
            self.http.repository_path(repository, f"/git/trees/{tree_sha}?recursive=1"),
            token=token,
        )
        if (
            status < 200
            or status >= 300
            or not isinstance(payload, dict)
            or payload.get("truncated") is not False
            or not isinstance(payload.get("tree"), list)
        ):
            _raise_response(status, "learning tree was incomplete")
        result: list[dict[str, object]] = []
        for item in cast(list[object], payload["tree"]):
            if not isinstance(item, dict):
                raise GitHubLearningProposalError("learning tree entry was invalid")
            result.append(item)
        return result

    @staticmethod
    def _tree_files(tree: Sequence[dict[str, object]]) -> dict[str, str]:
        files: dict[str, str] = {}
        for item in tree:
            path = item.get("path")
            if not isinstance(path, str):
                raise GitHubLearningProposalError("learning tree path was invalid")
            entry_type = item.get("type")
            mode = item.get("mode")
            sha = item.get("sha")
            if not isinstance(entry_type, str) or not isinstance(mode, str):
                raise GitHubLearningProposalError("learning tree entry was invalid")
            if entry_type == "tree":
                if path.startswith(f"{LEARNING_DIRECTORY}/"):
                    raise GitHubLearningProposalError(
                        "learning tree entry was not a regular file"
                    )
                # Recursive Git trees include directory nodes.  Their SHA
                # changes whenever a child changes, so only their descendants
                # participate in the managed-file delta.
                continue
            if entry_type == "blob" and mode == "100644":
                if not _is_sha(sha):
                    raise GitHubLearningProposalError(
                        "learning tree blob SHA was invalid"
                    )
                value = cast(str, sha)
            else:
                if not _is_sha(sha):
                    raise GitHubLearningProposalError(
                        "learning tree entry SHA was invalid"
                    )
                # Retain non-regular entries in the comparison map.  They are
                # harmless when unchanged, but a changed unmanaged tree,
                # submodule, or executable must not disappear from the delta.
                value = f"@{entry_type}:{mode}:{sha}"
            if path in files:
                raise GitHubLearningProposalError(
                    "learning tree contained duplicate paths"
                )
            files[path] = value
        return files

    def _pending_against_base(
        self,
        *,
        token: str,
        repository: str,
        base_sha: str,
        proposals: Sequence[LearningProposal],
    ) -> tuple[LearningProposal, ...]:
        pending: list[LearningProposal] = []
        for proposal in proposals:
            path = learning_path(proposal)
            status, payload = self._request(
                "GET",
                self.http.repository_path(
                    repository,
                    f"/contents/{quote(path, safe='/')}?ref={quote(base_sha, safe='')}",
                ),
                token=token,
            )
            if status == 404:
                pending.append(proposal)
                continue
            if status < 200 or status >= 300:
                _raise_response(status, "learning base file lookup failed")
            content = _decode_base64(payload)
            if content != _proposal_content(proposal):
                raise GitHubLearningProposalError(
                    "learning base file conflicts with the proposal"
                )
        return tuple(pending)

    def _recover_orphan_commit(
        self,
        *,
        token: str,
        repository: str,
        repository_id: int,
        pull_request: int,
        branch: str,
        base_sha: str,
        head_sha: str,
        batch: str,
        title: str,
        body: str,
        proposals: Sequence[LearningProposal],
    ) -> str | None:
        """Reuse a proven final commit left behind before PR creation."""

        ref_sha = self._read_ref(token=token, repository=repository, branch=branch)
        if ref_sha is None:
            return None
        marker = _Marker(
            repository_id=repository_id,
            pull_request=pull_request,
            batch=batch,
            head_sha=head_sha,
            version=2,
        )
        commit, provenance, actual = self._read_candidate_commit(
            token=token,
            repository=repository,
            repository_id=repository_id,
            pull_request=pull_request,
            commit_sha=ref_sha,
            marker=marker,
        )
        if (
            provenance.base_sha != base_sha
            or provenance.head_sha != head_sha
            or provenance.batch != batch
            or provenance.parent_shas != (base_sha,)
            or _sha256_text(title) != provenance.title_sha
            or _sha256_text(body) != provenance.body_sha
            or _normalized_proposals(actual) != _normalized_proposals(proposals)
            or commit["base_sha"] != base_sha
        ):
            raise GitHubLearningProposalError(
                "learning branch orphan provenance was invalid"
            )
        return ref_sha

    def _select_generation_branch(
        self,
        *,
        token: str,
        repository: str,
        repository_id: int,
        pull_request: int,
        batch: str,
        latest_base_sha: str,
        merged: bool,
        merged_branches: set[str] | frozenset[str] = frozenset(),
    ) -> str:
        stable = f"review-sensei/learnings/pr-{pull_request}"
        stable_sha = self._read_ref(token=token, repository=repository, branch=stable)
        generation_names = [
            f"review-sensei/learnings/pr-{pull_request}-g-{batch[:16]}"
        ] + [
            f"review-sensei/learnings/pr-{pull_request}-g-{batch[:16]}-{suffix}"
            for suffix in range(2, MAX_GENERATION_COLLISIONS + 1)
        ]
        if stable_sha is None:
            if not merged:
                return stable
            # A crash can leave any bounded generation ref behind even when the
            # merged stable ref was later deleted.  Recover exactly one proven
            # orphan before creating a second stable branch.
            orphan_matches: list[str] = []
            for orphan_branch in generation_names:
                if orphan_branch in merged_branches:
                    continue
                orphan_sha = self._read_ref(
                    token=token, repository=repository, branch=orphan_branch
                )
                if orphan_sha is None:
                    continue
                status, commit = self._request(
                    "GET",
                    self.http.repository_path(repository, f"/git/commits/{orphan_sha}"),
                    token=token,
                )
                if status < 200 or status >= 300 or not isinstance(commit, dict):
                    _raise_response(
                        status, "learning generation branch commit lookup failed"
                    )
                parsed = _parse_provenance(commit.get("message"))
                if parsed is None:
                    raise GitHubLearningProposalError(
                        "learning generation branch is not canonical"
                    )
                _line, values = parsed
                if (
                    int(values["repo"]) != repository_id
                    or int(values["pr"]) != pull_request
                    or values["base"] != latest_base_sha
                    or values["batch"] != batch
                ):
                    raise GitHubLearningProposalError(
                        "learning generation branch collides with another artifact"
                    )
                orphan_matches.append(orphan_branch)
            if len(orphan_matches) > 1:
                raise GitHubLearningProposalError(
                    "multiple learning generation orphans are ambiguous"
                )
            return orphan_matches[0] if orphan_matches else stable
        names = [stable] if not merged else generation_names
        for branch in names:
            if branch in merged_branches:
                continue
            status, ref_payload = self._request(
                "GET",
                self.http.repository_path(
                    repository, f"/git/ref/heads/{quote(branch, safe='/')}"
                ),
                token=token,
            )
            if status == 404:
                return branch
            if status < 200 or status >= 300 or not isinstance(ref_payload, dict):
                _raise_response(status, "learning branch lookup failed")
            obj = ref_payload.get("object")
            commit_sha = obj.get("sha") if isinstance(obj, dict) else None
            if not _is_sha(commit_sha):
                raise GitHubLearningProposalError(
                    "learning branch reference was invalid"
                )
            status, commit = self._request(
                "GET",
                self.http.repository_path(repository, f"/git/commits/{commit_sha}"),
                token=token,
            )
            if status < 200 or status >= 300 or not isinstance(commit, dict):
                _raise_response(status, "learning branch commit lookup failed")
            parsed = _parse_provenance(commit.get("message"))
            if parsed is None:
                raise GitHubLearningProposalError(
                    "learning generation branch is not canonical"
                )
            _line, values = parsed
            if (
                int(values["repo"]) == repository_id
                and int(values["pr"]) == pull_request
                and values["base"] == latest_base_sha
                and values["batch"] == batch
            ):
                return branch
            if not merged:
                raise GitHubLearningProposalTransientError(
                    "learning stable branch changed during reconciliation"
                )
        raise GitHubLearningProposalError(
            "learning generation branch collisions exceeded the limit"
        )

    def _create_learning_commit(
        self,
        *,
        token: str,
        repository: str,
        repository_id: int,
        pull_request: int,
        base_sha: str,
        head_sha: str,
        batch: str,
        title: str,
        body: str,
        proposals: Sequence[LearningProposal],
        parents: tuple[str, ...],
    ) -> str:
        if not parents or any(not _is_sha(parent) for parent in parents):
            raise GitHubLearningProposalError("learning commit parents were invalid")
        if len(body.encode("utf-8")) > MAX_BODY_BYTES:
            raise GitHubLearningProposalError(
                "learning PR body exceeds the safety limit"
            )
        status, base_commit = self._request(
            "GET",
            self.http.repository_path(repository, f"/git/commits/{base_sha}"),
            token=token,
        )
        if status < 200 or status >= 300 or not isinstance(base_commit, dict):
            _raise_response(status, "learning base commit was invalid")
        base_payload_sha = base_commit.get("sha")
        if not _is_sha(base_payload_sha) or base_payload_sha != base_sha:
            raise GitHubLearningProposalError(
                "learning base commit identity was invalid"
            )
        base_tree = base_commit.get("tree")
        base_tree_sha = base_tree.get("sha") if isinstance(base_tree, dict) else None
        if not _is_sha(base_tree_sha):
            raise GitHubLearningProposalError("learning base tree was invalid")
        entries: list[dict[str, object]] = []
        for proposal in proposals:
            status, blob = self._request(
                "POST",
                self.http.repository_path(repository, "/git/blobs"),
                token=token,
                body={"content": _proposal_content(proposal), "encoding": "utf-8"},
            )
            if (
                status < 200
                or status >= 300
                or not isinstance(blob, dict)
                or not _is_sha(blob.get("sha"))
            ):
                _raise_response(status, "learning blob creation was rejected")
            entries.append(
                {
                    "path": learning_path(proposal),
                    "mode": "100644",
                    "type": "blob",
                    "sha": blob["sha"],
                }
            )
        status, tree = self._request(
            "POST",
            self.http.repository_path(repository, "/git/trees"),
            token=token,
            body={"base_tree": base_tree_sha, "tree": entries},
        )
        if (
            status < 200
            or status >= 300
            or not isinstance(tree, dict)
            or not _is_sha(tree.get("sha"))
        ):
            _raise_response(status, "learning tree creation was rejected")
        message = (
            "chore: propose ReviewSensei learnings\n\n"
            f"reviewsensei-learning:v2 repo={repository_id} pr={pull_request} "
            f"base={base_sha} batch={batch} head={head_sha} "
            f"title={_sha256_text(title)} body={_sha256_text(body)}"
        )
        status, commit = self._request(
            "POST",
            self.http.repository_path(repository, "/git/commits"),
            token=token,
            body={"message": message, "tree": tree["sha"], "parents": list(parents)},
        )
        if (
            status < 200
            or status >= 300
            or not isinstance(commit, dict)
            or not _is_sha(commit.get("sha"))
        ):
            _raise_response(status, "learning commit creation was rejected")
        return cast(str, commit["sha"])

    def _create_or_recover_ref(
        self, *, token: str, repository: str, branch: str, commit_sha: str
    ) -> None:
        try:
            status, _payload = self._request(
                "POST",
                self.http.repository_path(repository, "/git/refs"),
                token=token,
                body={"ref": f"refs/heads/{branch}", "sha": commit_sha},
            )
        except GitHubLearningProposalTransientError as exc:
            if (
                self._read_ref(token=token, repository=repository, branch=branch)
                == commit_sha
            ):
                return
            raise GitHubLearningProposalTransientError(
                "learning branch creation was ambiguous"
            ) from exc
        if 200 <= status < 300:
            return
        current = self._read_ref(token=token, repository=repository, branch=branch)
        if current == commit_sha:
            return
        if status in {409, 422, 429} or status >= 500:
            raise GitHubLearningProposalTransientError(
                "learning branch creation was ambiguous"
            )
        raise GitHubLearningProposalError("learning branch creation was rejected")

    def _read_ref(self, *, token: str, repository: str, branch: str) -> str | None:
        status, payload = self._request(
            "GET",
            self.http.repository_path(
                repository, f"/git/ref/heads/{quote(branch, safe='/')}"
            ),
            token=token,
        )
        if status == 404:
            return None
        if status < 200 or status >= 300 or not isinstance(payload, dict):
            _raise_response(status, "learning branch lookup failed")
        obj = payload.get("object")
        sha = obj.get("sha") if isinstance(obj, dict) else None
        if not _is_sha(sha):
            raise GitHubLearningProposalError("learning branch reference was invalid")
        return sha

    def _refresh_open_candidate(
        self,
        *,
        token: str,
        repository: str,
        repository_id: int,
        pull_request: int,
        head_sha: str,
        caller_base_sha: str,
        authority: _Authority,
        candidate: _Candidate,
        proposals: tuple[LearningProposal, ...],
    ) -> LearningPRResult | None:
        if candidate.state != "open":
            return LearningPRResult(
                status="skipped_closed_pull_request",
                pull_request_number=candidate.number,
            )
        # The initial open-list read and detail read are not an atomic
        # snapshot.  Revalidate the candidate immediately before creating any
        # Git objects so a close/merge race cannot mutate its branch.
        latest_candidate = self._load_candidate(
            token=token,
            repository=repository,
            repository_id=repository_id,
            pull_request=pull_request,
            base_branch=authority.source.base_branch,
            item={"number": candidate.number},
            branch=candidate.branch,
        )
        if latest_candidate is None or latest_candidate.state != "open":
            return LearningPRResult(
                status="skipped_closed_pull_request",
                pull_request_number=candidate.number,
            )
        if (
            latest_candidate.commit_sha != candidate.commit_sha
            or latest_candidate.title != candidate.title
            or latest_candidate.body != candidate.body
        ):
            raise GitHubLearningProposalTransientError(
                "learning PR changed during reconciliation"
            )
        candidate = latest_candidate
        self._assert_canonical_candidate_metadata(
            token=token,
            repository=repository,
            repository_id=repository_id,
            pull_request=pull_request,
            authority=authority,
            candidate=candidate,
        )
        merged: dict[str, LearningProposal] = {
            proposal_digest(proposal): proposal for proposal in candidate.proposals
        }
        for proposal in proposals:
            merged[proposal_digest(proposal)] = proposal
        union = tuple(merged[key] for key in sorted(merged))
        self._validate_path_collisions(union)
        pending = self._pending_against_base(
            token=token,
            repository=repository,
            base_sha=authority.latest_base_sha,
            proposals=union,
        )
        if not pending:
            return LearningPRResult(
                status="skipped_identical",
                pull_request_number=candidate.number,
            )
        title, body = self._render_metadata(
            repository=repository,
            pull_request=pull_request,
            source=authority.source,
            head_sha=head_sha,
            proposals=pending,
            repository_id=repository_id,
        )
        materialized = (
            batch_digest(pending) == candidate.provenance.batch
            and head_sha == candidate.provenance.head_sha
            and _sha256_text(title) == candidate.provenance.title_sha
            and _sha256_text(body) == candidate.provenance.body_sha
        )
        if (
            not candidate.metadata_pending
            and authority.latest_base_sha == candidate.provenance.base_sha
            and materialized
        ):
            return LearningPRResult(
                status="skipped_pull_request_exists",
                pull_request_number=candidate.number,
            )
        latest = self._read_authority(
            token=token,
            repository=repository,
            repository_id=repository_id,
            pull_request=pull_request,
            head_sha=head_sha,
            base_branch=authority.source.base_branch,
            caller_base_sha=caller_base_sha,
        )
        if isinstance(latest, LearningPRResult):
            return latest
        if latest.latest_base_sha != authority.latest_base_sha:
            return None
        current_ref = self._read_ref(
            token=token, repository=repository, branch=candidate.branch
        )
        if current_ref != candidate.commit_sha:
            raise GitHubLearningProposalTransientError(
                "learning PR changed during reconciliation"
            )
        before_objects = self._revalidate_open_candidate_detail(
            token=token,
            repository=repository,
            repository_id=repository_id,
            base_branch=authority.source.base_branch,
            candidate=candidate,
        )
        if before_objects is not None:
            return before_objects
        parents = (
            (candidate.commit_sha, authority.latest_base_sha)
            if authority.latest_base_sha != candidate.provenance.base_sha
            else (candidate.commit_sha,)
        )
        commit_sha = (
            candidate.commit_sha
            if candidate.metadata_pending and materialized
            else self._create_learning_commit(
                token=token,
                repository=repository,
                repository_id=repository_id,
                pull_request=pull_request,
                base_sha=authority.latest_base_sha,
                head_sha=head_sha,
                batch=batch_digest(pending),
                title=title,
                body=body,
                proposals=pending,
                parents=parents,
            )
        )
        if (
            self._read_ref(token=token, repository=repository, branch=candidate.branch)
            != current_ref
        ):
            raise GitHubLearningProposalTransientError(
                "learning PR branch changed during reconciliation"
            )
        before_ref = self._revalidate_open_candidate_detail(
            token=token,
            repository=repository,
            repository_id=repository_id,
            base_branch=authority.source.base_branch,
            candidate=candidate,
        )
        if before_ref is not None:
            return before_ref
        if commit_sha != current_ref:
            try:
                status, _payload = self._request(
                    "PATCH",
                    self.http.repository_path(
                        repository,
                        f"/git/refs/heads/{quote(candidate.branch, safe='/')}",
                    ),
                    token=token,
                    body={"sha": commit_sha, "force": False},
                )
            except GitHubLearningProposalTransientError as exc:
                if (
                    self._read_ref(
                        token=token, repository=repository, branch=candidate.branch
                    )
                    == commit_sha
                ):
                    status = 200
                else:
                    raise GitHubLearningProposalTransientError(
                        "learning PR branch update was ambiguous"
                    ) from exc
            if not 200 <= status < 300:
                if (
                    self._read_ref(
                        token=token, repository=repository, branch=candidate.branch
                    )
                    == commit_sha
                ):
                    status = 200
                elif status in {409, 422, 429} or status >= 500:
                    raise GitHubLearningProposalTransientError(
                        "learning PR branch update was ambiguous"
                    )
                else:
                    raise GitHubLearningProposalError(
                        "learning PR branch update was rejected"
                    )
        before_patch = self._read_authority(
            token=token,
            repository=repository,
            repository_id=repository_id,
            pull_request=pull_request,
            head_sha=head_sha,
            base_branch=authority.source.base_branch,
            caller_base_sha=caller_base_sha,
        )
        if isinstance(before_patch, LearningPRResult):
            raise GitHubLearningProposalTransientError(
                "source changed before learning PR metadata update"
            )
        if before_patch.latest_base_sha != authority.latest_base_sha:
            raise GitHubLearningProposalTransientError(
                "default branch changed before learning PR metadata update"
            )
        if (
            self._read_ref(token=token, repository=repository, branch=candidate.branch)
            != commit_sha
        ):
            raise GitHubLearningProposalTransientError(
                "learning PR branch changed before metadata update"
            )
        self._patch_learning_pull_request(
            token=token,
            repository=repository,
            repository_id=repository_id,
            pull_request_number=candidate.number,
            branch=candidate.branch,
            old_head=candidate.commit_sha,
            new_head=commit_sha,
            base_branch=authority.source.base_branch,
            old_title=candidate.title,
            old_body=candidate.body,
            title=title,
            body=body,
        )
        return LearningPRResult(status="updated", pull_request_number=candidate.number)

    def _revalidate_open_candidate_detail(
        self,
        *,
        token: str,
        repository: str,
        repository_id: int,
        base_branch: str,
        candidate: _Candidate,
    ) -> LearningPRResult | None:
        """Check the mutable PR state immediately around Git mutations."""

        if candidate.number is None:
            raise GitHubLearningProposalError("learning PR identity was invalid")
        status, detail = self._request(
            "GET",
            self.http.repository_path(repository, f"/pulls/{candidate.number}"),
            token=token,
        )
        if status == 404:
            raise GitHubLearningProposalError(
                "learning PR disappeared during reconciliation"
            )
        if status < 200 or status >= 300 or not isinstance(detail, dict):
            _raise_response(status, "learning PR revalidation failed")
        if detail.get("state") != "open":
            if detail.get("state") == "closed":
                return LearningPRResult(
                    status="skipped_closed_pull_request",
                    pull_request_number=candidate.number,
                )
            raise GitHubLearningProposalError("learning PR state was invalid")
        created_at = _parse_github_timestamp(detail.get("created_at"))
        if created_at is None or created_at != candidate.created_at:
            raise GitHubLearningProposalError("learning PR creation timestamp changed")
        if detail.get("draft") is not True:
            raise GitHubLearningProposalError("learning PR must remain a draft")
        head = detail.get("head")
        base = detail.get("base")
        if not isinstance(head, dict) or not isinstance(base, dict):
            raise GitHubLearningProposalError("learning PR binding was invalid")
        if (
            head.get("ref") != candidate.branch
            or head.get("sha") != candidate.commit_sha
        ):
            raise GitHubLearningProposalTransientError(
                "learning PR changed during reconciliation"
            )
        head_repo = head.get("repo")
        base_repo = base.get("repo")
        if (
            not isinstance(head_repo, dict)
            or head_repo.get("full_name") != repository
            or head_repo.get("fork") is not False
            or not _repository_id_matches(head_repo.get("id"), repository_id)
            or not isinstance(base_repo, dict)
            or base_repo.get("full_name") != repository
            or base_repo.get("fork") is not False
            or not _repository_id_matches(base_repo.get("id"), repository_id)
            or base.get("ref") != base_branch
        ):
            raise GitHubLearningProposalError("learning PR binding changed")
        if (
            detail.get("title") != candidate.title
            or detail.get("body") != candidate.body
        ):
            raise GitHubLearningProposalTransientError(
                "learning PR metadata changed during reconciliation"
            )
        return None

    def _assert_canonical_candidate_metadata(
        self,
        *,
        token: str,
        repository: str,
        repository_id: int,
        pull_request: int,
        authority: _Authority,
        candidate: _Candidate,
    ) -> None:
        """Prove the old PR metadata is a rendering owned by ReviewSensei.

        Commit trailers bind byte hashes, but a manually authored commit could
        copy those hashes for arbitrary text.  Before a refresh can overwrite
        title/body, re-render the exact prior generation from its marker,
        prior title envelope, and canonical proposal files.  The current
        source title is rendered only after this prior state is proven.
        """

        marker = _parse_marker_line(candidate.body)
        if marker is None:
            raise GitHubLearningProposalError(
                "learning PR metadata was edited outside ReviewSensei"
            )
        if marker.version == 1:
            expected_title = "Propose ReviewSensei learnings"
            expected_body = self._render_legacy_body(candidate.proposals, marker)
        else:
            prior_provenance = candidate.provenance
            prior_proposals = candidate.proposals
            if candidate.metadata_pending:
                if not prior_provenance.parent_shas:
                    raise GitHubLearningProposalError(
                        "learning PR metadata transition was invalid"
                    )
                _prior_commit, prior_provenance, prior_proposals = (
                    self._read_candidate_commit(
                        token=token,
                        repository=repository,
                        repository_id=repository_id,
                        pull_request=pull_request,
                        commit_sha=prior_provenance.parent_shas[0],
                        marker=None,
                    )
                )
            title_prefix = f"ReviewSensei learnings from #{pull_request}: "
            if not candidate.title.startswith(title_prefix):
                raise GitHubLearningProposalError(
                    "learning PR metadata was edited outside ReviewSensei"
                )
            title_suffix = _unsanitize_display_text(
                candidate.title[len(title_prefix) :]
            )
            if not title_suffix:
                raise GitHubLearningProposalError(
                    "learning PR metadata was edited outside ReviewSensei"
                )
            source_title_prefix = "Source title: "
            body_source_titles = [
                _unsanitize_display_text(line[len(source_title_prefix) :])
                for line in candidate.body.splitlines()
                if line.startswith(source_title_prefix)
            ]
            if len(body_source_titles) > 1:
                raise GitHubLearningProposalError(
                    "learning PR metadata was edited outside ReviewSensei"
                )
            if body_source_titles:
                if not body_source_titles[0]:
                    raise GitHubLearningProposalError(
                        "learning PR metadata was edited outside ReviewSensei"
                    )
                if body_source_titles[0] != _canonical_source_title(
                    authority.source.title
                ) and not self._source_title_transition_proven(
                    token=token,
                    repository=repository,
                    pull_request=pull_request,
                    old_title=body_source_titles[0],
                    current_title=_canonical_source_title(authority.source.title),
                    candidate_created_at=candidate.created_at,
                ):
                    raise GitHubLearningProposalError(
                        "learning PR metadata was edited outside ReviewSensei"
                    )
                prior_source = replace(authority.source, title=body_source_titles[0])
                include_source_title = True
            else:
                # v2 bodies emitted before the source-title line was added are
                # accepted only while their title still matches today's source
                # title.  This preserves safe no-op compatibility without
                # allowing an unprovable title transition.
                if title_suffix != _canonical_source_title(authority.source.title):
                    raise GitHubLearningProposalError(
                        "learning PR metadata was edited outside ReviewSensei"
                    )
                prior_source = authority.source
                include_source_title = False
            expected_title, expected_body = self._render_metadata(
                repository=repository,
                pull_request=pull_request,
                source=prior_source,
                head_sha=prior_provenance.head_sha,
                proposals=prior_proposals,
                repository_id=repository_id,
                include_source_title=include_source_title,
            )
        if candidate.title != expected_title or candidate.body != expected_body:
            raise GitHubLearningProposalError(
                "learning PR metadata was edited outside ReviewSensei"
            )

    def _source_title_transition_proven(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        old_title: str,
        current_title: str,
        candidate_created_at: datetime,
    ) -> bool:
        """Require an authoritative GitHub rename event for title drift."""

        if old_title == current_title:
            return True
        path = self.http.repository_path(repository, f"/issues/{pull_request}/timeline")
        cursor = old_title
        last_event_at: datetime | None = None
        for page in range(1, MAX_PAGINATION_PAGES + 1):
            status, payload = self._request(
                "GET",
                f"{path}?per_page=100&page={page}",
                token=token,
            )
            if status < 200 or status >= 300 or not isinstance(payload, list):
                _raise_response(status, "learning source title history lookup failed")
            for event in payload:
                if not isinstance(event, dict):
                    raise GitHubLearningProposalError(
                        "learning source title history was invalid"
                    )
                if event.get("event") != "renamed":
                    continue
                rename = event.get("rename")
                if not isinstance(rename, dict):
                    raise GitHubLearningProposalError(
                        "learning source title history was invalid"
                    )
                old_value = rename.get("from")
                new_value = rename.get("to")
                if (
                    not isinstance(old_value, str)
                    or not old_value.strip()
                    or not isinstance(new_value, str)
                    or not new_value.strip()
                ):
                    raise GitHubLearningProposalError(
                        "learning source title history was invalid"
                    )
                event_at = _parse_github_timestamp(event.get("created_at"))
                if event_at is None:
                    raise GitHubLearningProposalError(
                        "learning source title history was invalid"
                    )
                if last_event_at is not None and event_at <= last_event_at:
                    raise GitHubLearningProposalError(
                        "learning source title history was not chronological"
                    )
                last_event_at = event_at
                if event_at <= candidate_created_at:
                    continue
                old_event_title = _canonical_source_title(old_value)
                new_event_title = _canonical_source_title(new_value)
                # Timeline events are consumed in authoritative chronological
                # order.  Ignore unrelated issue renames, but only advance the
                # candidate title when the event starts at the title reached
                # by the preceding source-title transition.
                if old_event_title == cursor:
                    cursor = new_event_title
            if len(payload) < 100:
                return cursor == current_title
        raise GitHubLearningProposalError(
            "learning source title history exceeded its limit"
        )

    def _render_metadata(
        self,
        *,
        repository: str,
        pull_request: int,
        source: _Source,
        head_sha: str,
        proposals: Sequence[LearningProposal],
        repository_id: int,
        include_source_title: bool = True,
    ) -> tuple[str, str]:
        source_title = _canonical_source_title(source.title)
        title = _truncate_bytes(
            f"ReviewSensei learnings from #{pull_request}: {source_title}",
            MAX_TITLE_BYTES,
        )
        if len(title) > MAX_TITLE_CHARS:
            title = title[:MAX_TITLE_CHARS]
        lines = [
            f"ReviewSensei proposed learnings for [#{pull_request}](https://github.com/{repository}/pull/{pull_request}).",
            "",
            *([f"Source title: {source_title}", ""] if include_source_title else []),
            f"Latest reviewed source head: `{head_sha}`",
            f"Proposal count: {len(proposals)}",
            "",
            "These proposals are unapproved and require maintainer review and merge.",
            "",
            "## Proposed learnings",
        ]
        for index, proposal in enumerate(proposals, 1):
            scope = ", ".join(
                _sanitize_display_text(pattern, limit=MAX_SCOPE_FIELD_CHARS)
                for pattern in proposal.scope
            )
            item_lines = [
                "",
                f"{index}. Title: {_sanitize_display_text(proposal.title, limit=MAX_DISPLAY_FIELD_CHARS)}",
                f"   Rule: {_sanitize_display_text(proposal.rule, limit=MAX_DISPLAY_FIELD_CHARS)}",
            ]
            if proposal.rationale is not None:
                item_lines.append(
                    f"   Rationale: {_sanitize_display_text(proposal.rationale, limit=MAX_DISPLAY_FIELD_CHARS)}"
                )
            if proposal.category is not None:
                item_lines.append(
                    f"   Category: {_sanitize_display_text(proposal.category, limit=MAX_DISPLAY_FIELD_CHARS)}"
                )
            item_lines.append(f"   Scope: {scope}")
            lines.extend(item_lines)
        batch = batch_digest(proposals)
        lines.extend(
            [
                "",
                _marker_line(
                    repository_id=repository_id,
                    pull_request=pull_request,
                    batch=batch,
                    head_sha=head_sha,
                ),
            ]
        )
        body = "\n".join(lines)
        if len(body.encode("utf-8")) > MAX_BODY_BYTES:
            raise GitHubLearningProposalError(
                "learning PR metadata exceeds the safety limit"
            )
        return title, body

    @staticmethod
    def _render_legacy_body(
        proposals: Sequence[LearningProposal], marker: _Marker
    ) -> str:
        lines = [
            "Proposed ReviewSensei learning files:",
            *[f"- `{learning_path(proposal)}`" for proposal in proposals],
            "",
            f"<!-- reviewsensei:learning:v1 pr={marker.pull_request} batch={marker.batch} -->",
        ]
        return "\n".join(lines)

    def _create_learning_pull_request(
        self,
        *,
        token: str,
        repository: str,
        repository_id: int,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> LearningPRResult:
        try:
            status, payload = self._request(
                "POST",
                self.http.repository_path(repository, "/pulls"),
                token=token,
                body={
                    "title": title,
                    "head": branch,
                    "base": base_branch,
                    "draft": True,
                    "body": body,
                },
            )
        except GitHubLearningProposalTransientError:
            existing = self._find_open_by_marker(
                token=token,
                repository=repository,
                repository_id=repository_id,
                branch=branch,
                base_branch=base_branch,
                title=title,
                body=body,
            )
            if existing is not None:
                return LearningPRResult(
                    status="skipped_pull_request_exists", pull_request_number=existing
                )
            raise
        if 200 <= status < 300 and isinstance(payload, dict):
            number = payload.get("number")
            if not isinstance(number, bool) and isinstance(number, int) and number > 0:
                return LearningPRResult(status="created", pull_request_number=number)
            raise GitHubLearningProposalError("learning PR response was invalid")
        existing = self._find_open_by_marker(
            token=token,
            repository=repository,
            repository_id=repository_id,
            branch=branch,
            base_branch=base_branch,
            title=title,
            body=body,
        )
        if existing is not None:
            return LearningPRResult(
                status="skipped_pull_request_exists", pull_request_number=existing
            )
        if status in {409, 422, 429} or status >= 500:
            raise GitHubLearningProposalTransientError(
                "learning PR creation was ambiguous"
            )
        raise GitHubLearningProposalError("learning PR creation was rejected")

    def _find_open_by_marker(
        self,
        *,
        token: str,
        repository: str,
        repository_id: int,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> int | None:
        marker = _parse_marker_line(body)
        if marker is None:
            raise GitHubLearningProposalError("learning PR marker was invalid")
        matches: list[int] = []
        for raw_item in self._list_pull_requests(
            token=token, repository=repository, state="open"
        ):
            item = self._validate_pull_request_summary(raw_item)
            head = item.get("head")
            if not isinstance(head, dict) or head.get("ref") != branch:
                continue
            if item.get("body") != body:
                continue
            number = item.get("number")
            if isinstance(number, bool) or not isinstance(number, int) or number < 1:
                raise GitHubLearningProposalError("learning PR identity was invalid")
            status, detail = self._request(
                "GET",
                self.http.repository_path(repository, f"/pulls/{number}"),
                token=token,
            )
            if status < 200 or status >= 300 or not isinstance(detail, dict):
                _raise_response(status, "learning PR detail lookup failed")
            head_detail = detail.get("head")
            base_detail = detail.get("base")
            head_repo = (
                head_detail.get("repo") if isinstance(head_detail, dict) else None
            )
            base_repo = (
                base_detail.get("repo") if isinstance(base_detail, dict) else None
            )
            if (
                detail.get("state") != "open"
                or detail.get("draft") is not True
                or detail.get("title") != title
                or detail.get("body") != body
                or not isinstance(head_detail, dict)
                or head_detail.get("ref") != branch
                or not _is_sha(head_detail.get("sha"))
                or not isinstance(base_detail, dict)
                or base_detail.get("ref") != base_branch
                or not isinstance(head_repo, dict)
                or not _repository_id_matches(head_repo.get("id"), repository_id)
                or head_repo.get("full_name") != repository
                or head_repo.get("fork") is not False
                or not isinstance(base_repo, dict)
                or not _repository_id_matches(base_repo.get("id"), repository_id)
                or base_repo.get("full_name") != repository
                or base_repo.get("fork") is not False
            ):
                raise GitHubLearningProposalError(
                    "learning PR recovery candidate was not owned"
                )
            matches.append(number)
        if len(matches) > 1:
            raise GitHubLearningProposalError(
                "multiple learning PR recovery candidates are ambiguous"
            )
        return matches[0] if matches else None

    def _patch_learning_pull_request(
        self,
        *,
        token: str,
        repository: str,
        repository_id: int,
        pull_request_number: int | None,
        branch: str,
        old_head: str,
        new_head: str,
        base_branch: str,
        old_title: str,
        old_body: str,
        title: str,
        body: str,
    ) -> None:
        if pull_request_number is None:
            raise GitHubLearningProposalError("learning PR number was missing")
        status, detail = self._request(
            "GET",
            self.http.repository_path(repository, f"/pulls/{pull_request_number}"),
            token=token,
        )
        if status < 200 or status >= 300 or not isinstance(detail, dict):
            _raise_response(status, "learning PR detail lookup failed")
        head = detail.get("head")
        base = detail.get("base")
        head_repo = head.get("repo") if isinstance(head, dict) else None
        base_repo = base.get("repo") if isinstance(base, dict) else None
        if (
            detail.get("state") != "open"
            or detail.get("draft") is not True
            or not isinstance(head, dict)
            or head.get("ref") != branch
            or not (head.get("sha") == old_head or head.get("sha") == new_head)
            or not isinstance(head_repo, dict)
            or not _repository_id_matches(head_repo.get("id"), repository_id)
            or head_repo.get("full_name") != repository
            or head_repo.get("fork") is not False
            or not isinstance(base, dict)
            or base.get("ref") != base_branch
            or not isinstance(base_repo, dict)
            or not _repository_id_matches(base_repo.get("id"), repository_id)
            or base_repo.get("full_name") != repository
            or base_repo.get("fork") is not False
            or detail.get("title") != old_title
            or detail.get("body") != old_body
        ):
            raise GitHubLearningProposalError(
                "learning PR changed outside ReviewSensei"
            )
        patch_status, _payload = self._request(
            "PATCH",
            self.http.repository_path(repository, f"/pulls/{pull_request_number}"),
            token=token,
            body={
                "title": title,
                "body": body,
                "base": base_branch,
                "draft": True,
            },
        )
        if 200 <= patch_status < 300:
            return
        recovery_status, after = self._request(
            "GET",
            self.http.repository_path(repository, f"/pulls/{pull_request_number}"),
            token=token,
        )
        after_head = after.get("head") if isinstance(after, dict) else None
        after_base = after.get("base") if isinstance(after, dict) else None
        after_head_repo = (
            after_head.get("repo") if isinstance(after_head, dict) else None
        )
        after_base_repo = (
            after_base.get("repo") if isinstance(after_base, dict) else None
        )
        if (
            recovery_status >= 200
            and recovery_status < 300
            and isinstance(after, dict)
            and after.get("state") == "open"
            and after.get("draft") is True
            and after.get("title") == title
            and after.get("body") == body
            and isinstance(after_head, dict)
            and after_head.get("ref") == branch
            and after_head.get("sha") == new_head
            and isinstance(after_head_repo, dict)
            and _repository_id_matches(after_head_repo.get("id"), repository_id)
            and after_head_repo.get("full_name") == repository
            and after_head_repo.get("fork") is False
            and isinstance(after_base, dict)
            and after_base.get("ref") == base_branch
            and isinstance(after_base_repo, dict)
            and _repository_id_matches(after_base_repo.get("id"), repository_id)
            and after_base_repo.get("full_name") == repository
            and after_base_repo.get("fork") is False
        ):
            return
        if (
            patch_status in {409, 422, 429}
            or patch_status >= 500
            or recovery_status in {409, 422, 429}
            or recovery_status >= 500
        ):
            raise GitHubLearningProposalTransientError(
                "learning PR metadata update was ambiguous"
            )
        raise GitHubLearningProposalError("learning PR metadata update was rejected")

    def _request(
        self,
        method: str,
        path: str,
        *,
        token: str,
        body: dict[str, object] | None = None,
    ) -> tuple[int, dict[str, object] | list[object] | None]:
        try:
            return self.http.request(method, path, token=token, body=body)
        except GitHubHTTPTransientError as exc:
            raise GitHubLearningProposalTransientError(
                "learning GitHub request failed temporarily"
            ) from exc
