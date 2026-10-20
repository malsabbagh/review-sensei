"""Authorized, bounded GitHub mention-reply publication."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Sequence
from urllib.parse import quote

from review_sensei.errors import LearningLoadError, ReviewInputError
from review_sensei.feedback import FeedbackSelector
from review_sensei.learnings import (
    MAX_LEARNING_FILE_BYTES,
    MAX_LEARNING_FILES,
    LearningStore,
)
from review_sensei.models import (
    ConversationContext,
    ConversationFinding,
    ConversationMessage,
    ConversationReply,
    LearningEntry,
)
from review_sensei.presentation import append_response_instructions
from review_sensei.validation import validate_repository_path

from .errors import (
    GitHubConversationError,
    GitHubConversationTransientError,
    GitHubHTTPError,
    GitHubHTTPTransientError,
    GitHubPublicationError,
)
from .http import GitHubHttp
from .pending import PendingReplyPage, pending_reply_pages
from .publication import (
    ReviewApprovalFinalizer,
    finding_declares_blocking,
    finding_instance_from_body,
    finding_publication_scope,
)
from .qualification import TrustedOpenRouterQualification
from .reply_budget import bounded_reply_publication

MAX_THREAD_MESSAGES = 20
MAX_REPLY_BYTES = 16 * 1024
MAX_CONTEXT_MESSAGE_BYTES = 1024
MAX_CONTEXT_AUTHOR_BYTES = 128
MAX_CONTEXT_TIMESTAMP_BYTES = 128
MAX_CONTEXT_PR_TITLE_BYTES = 2 * 1024
MAX_CONTEXT_PR_BODY_BYTES = 4 * 1024
MAX_CONTEXT_DIFF_BYTES = 12 * 1024
MAX_CONTEXT_FINDING_BYTES = 512
MAX_CONTEXT_LEARNINGS_BYTES = 6 * 1024
CONVERSATION_COMMENT_PAGE_SIZES = (100, 50, 25, 5, 1)
MAX_REVIEW_THREAD_PAGES = 10
# Preserve Unicode whitespace as a conversation boundary for both handles.
# Only canonical letter matching is ASCII-scoped; commands use ASCII separators.
MENTION_PATTERN = re.compile(
    r"(?i)(?:^|\s)(?:@sensei|(?a:@reviewsensei))(?:$|\s|[.,!?])"
)
AUTHORIZED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
GIT_SHA_HEX = re.compile(r"^[a-f0-9]{40}$")
DIFF_HUNK_HEADER = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)
MARKER_PREFIX = "<!-- reviewsensei:reply:v1"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bounded_text(value: object, maximum: int) -> str | None:
    """Return non-empty text whose UTF-8 encoding is at most ``maximum`` bytes."""

    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.encode("utf-8", errors="strict")
    if len(raw) <= maximum:
        return value
    suffix = b"\n[truncated]"
    if maximum <= len(suffix):
        clipped = raw[:maximum]
        while clipped:
            try:
                return clipped.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                clipped = clipped[:-1]
        return None
    clipped = raw[: maximum - len(suffix)]
    while clipped:
        try:
            return clipped.decode("utf-8", errors="strict") + suffix.decode("ascii")
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    return suffix.decode("ascii").lstrip()


def _parse_diff_hunk_header(value: str) -> tuple[int, int, int, int] | None:
    """Parse the unified-diff header from a hunk block or header line."""

    first_line = value.splitlines()[0] if value else ""
    match = DIFF_HUNK_HEADER.match(first_line)
    if match is None:
        return None
    return (
        int(match.group("old_start")),
        int(match.group("old_count") or "1"),
        int(match.group("new_start")),
        int(match.group("new_count") or "1"),
    )


def _diff_ranges_overlap(
    left_start: int,
    left_count: int,
    right_start: int,
    right_count: int,
) -> bool:
    left_end = left_start + max(left_count, 1)
    right_end = right_start + max(right_count, 1)
    return left_start < right_end and right_start < left_end


def _diff_hunks_overlap(
    left: tuple[int, int, int, int], right: tuple[int, int, int, int]
) -> bool:
    return _diff_ranges_overlap(left[0], left[1], right[0], right[1]) or (
        _diff_ranges_overlap(left[2], left[3], right[2], right[3])
    )


def _remove_emitted_priority_hunks(
    patch: str, emitted: set[str], *, filename: str
) -> str:
    priority_ranges = [
        parsed
        for hunk in emitted
        if (parsed := _parse_diff_hunk_header(hunk)) is not None
    ]
    if not priority_ranges:
        return patch
    lines = patch.splitlines(keepends=True)
    patch_hunks: list[tuple[int, tuple[int, int, int, int]]] = []
    for index, line in enumerate(lines):
        parsed = _parse_diff_hunk_header(line)
        if parsed is not None:
            patch_hunks.append((index, parsed))
    if not patch_hunks:
        return patch
    preamble = lines[: patch_hunks[0][0]]
    kept_hunks: list[list[str]] = []
    removed = False
    for position, (start, patch_range) in enumerate(patch_hunks):
        end = (
            patch_hunks[position + 1][0]
            if position + 1 < len(patch_hunks)
            else len(lines)
        )
        if any(
            _diff_hunks_overlap(patch_range, priority) for priority in priority_ranges
        ):
            removed = True
            continue
        kept_hunks.append(lines[start:end])
    if not removed:
        return patch
    if not kept_hunks:
        return ""
    if not preamble:
        preamble = [f"--- a/{filename}\n", f"+++ b/{filename}\n"]
    return "".join(preamble + [line for hunk in kept_hunks for line in hunk])


def has_standalone_sensei_mention(body: object) -> bool:
    return isinstance(body, str) and MENTION_PATTERN.search(body) is not None


def authorized_human_comment(source: dict[str, Any], *, app_slug: str) -> bool:
    """Accept only human repository collaborators and never the App itself."""

    user = source.get("user")
    association = source.get("author_association")
    if not isinstance(user, dict) or not isinstance(association, str):
        return False
    login = user.get("login")
    user_type = user.get("type")
    return (
        isinstance(login, str)
        and login != app_slug
        and user_type != "Bot"
        and association.upper() in AUTHORIZED_ASSOCIATIONS
    )


def reply_marker(
    *,
    source_comment_id: int,
    source_updated_digest: str,
    pull_request: int,
    head_sha: str,
) -> str:
    return (
        f"{MARKER_PREFIX} comment={source_comment_id} "
        f"updated={source_updated_digest} pr={pull_request} head={head_sha} -->"
    )


@dataclass(frozen=True)
class ReplyResult:
    status: str
    comment_id: int | None = None
    resolved: bool = False
    # Source receipt and approval finalization are separate outcomes. Ordinary
    # conversations do not finalize approval and leave these fields unset.
    approval_status: str | None = None
    approval_diagnostic: str | None = None
    assessment_status: str | None = None
    assessment_diagnostic: str | None = None
    resolution_status: str | None = None


@dataclass(frozen=True)
class HumanAssessmentDiffContext:
    """Whole current patches for every required path, or a safe diagnostic."""

    diff_context: str | None
    diagnostic: str | None = None

    def __post_init__(self) -> None:
        from review_sensei.human_assessment import HUMAN_ASSESSMENT_EVIDENCE_DIAGNOSTICS

        if self.diagnostic is not None and (
            self.diagnostic not in HUMAN_ASSESSMENT_EVIDENCE_DIAGNOSTICS
            or self.diff_context is not None
        ):
            raise GitHubConversationError(
                "human assessment evidence diagnostic is invalid"
            )


@dataclass(frozen=True)
class PreparedConversation:
    """Authorized, exact-head conversation data ready for provider execution."""

    context: ConversationContext
    source_kind: str
    source_comment_id: int
    source_updated_at: str
    head_sha: str
    root_comment_id: int


@dataclass(frozen=True)
class ProcessingReaction:
    """One App-authored processing reaction that must be removed."""

    reaction_id: int


class ConversationPublisher:
    """Publish an App reply to an authorized explicit @reviewsensei or @sensei mention."""

    def __init__(
        self,
        *,
        http: GitHubHttp,
        qualification_authority: TrustedOpenRouterQualification | None = None,
    ) -> None:
        self.http = http
        self.finalizer = ReviewApprovalFinalizer(
            http=http, qualification_authority=qualification_authority
        )

    @staticmethod
    def _reaction_path(*, source_kind: str, source_comment_id: int) -> str:
        if source_kind not in {"inline", "issue"}:
            raise GitHubConversationError("reply source kind is invalid")
        if (
            isinstance(source_comment_id, bool)
            or not isinstance(source_comment_id, int)
            or source_comment_id < 1
        ):
            raise GitHubConversationError("reply source comment id is invalid")
        resource = "pulls" if source_kind == "inline" else "issues"
        return f"/{resource}/comments/{source_comment_id}/reactions"

    def add_processing_reaction(
        self,
        *,
        token: str,
        repository: str,
        source_comment_id: int,
        source_kind: str,
    ) -> ProcessingReaction:
        """Add the ephemeral eyes reaction after mention authorization."""

        path = self._reaction_path(
            source_kind=source_kind,
            source_comment_id=source_comment_id,
        )
        try:
            status, payload = self.http.request(
                "POST",
                self.http.repository_path(repository, path),
                token=token,
                body={"content": "eyes"},
            )
        except GitHubHTTPTransientError as exc:
            raise GitHubConversationTransientError(
                "processing reaction failed temporarily"
            ) from exc
        if status in {200, 201} and isinstance(payload, dict):
            reaction_id = payload.get("id")
            if (
                isinstance(reaction_id, int)
                and not isinstance(reaction_id, bool)
                and reaction_id > 0
            ):
                return ProcessingReaction(reaction_id=reaction_id)
        if status == 404:
            raise GitHubConversationError("processing reaction target was not found")
        if status == 403:
            raise GitHubConversationError("processing reaction lacks permission")
        if status == 429 or status >= 500:
            raise GitHubConversationTransientError(
                "processing reaction failed temporarily"
            )
        raise GitHubConversationError("processing reaction was rejected")

    def remove_processing_reaction(
        self,
        *,
        token: str,
        repository: str,
        source_comment_id: int,
        source_kind: str,
        reaction_id: int,
    ) -> None:
        """Remove an App-authored processing reaction after a terminal outcome."""

        path = self._reaction_path(
            source_kind=source_kind,
            source_comment_id=source_comment_id,
        )
        if (
            isinstance(reaction_id, bool)
            or not isinstance(reaction_id, int)
            or reaction_id < 1
        ):
            raise GitHubConversationError("processing reaction id is invalid")
        try:
            status, _ = self.http.request(
                "DELETE",
                self.http.repository_path(repository, f"{path}/{reaction_id}"),
                token=token,
            )
        except GitHubHTTPTransientError as exc:
            raise GitHubConversationTransientError(
                "processing reaction cleanup failed temporarily"
            ) from exc
        if status in {204, 404}:
            return
        if status == 403:
            raise GitHubConversationError(
                "processing reaction cleanup lacks permission"
            )
        if status == 429 or status >= 500:
            raise GitHubConversationTransientError(
                "processing reaction cleanup failed temporarily"
            )
        raise GitHubConversationError("processing reaction cleanup was rejected")

    def _require_comment_association(
        self,
        *,
        comment: dict[str, Any],
        repository: str,
        pull_request: int,
        source_kind: str,
    ) -> None:
        field = "pull_request_url" if source_kind == "inline" else "issue_url"
        suffix = (
            f"/pulls/{pull_request}"
            if source_kind == "inline"
            else f"/issues/{pull_request}"
        )
        expected = self.http.api_url + self.http.repository_path(repository, suffix)
        if comment.get(field) != expected:
            raise GitHubConversationError("reply source association was invalid")

    def prepare_context(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        source_comment_id: int,
        source_updated_at: str,
        app_slug: str,
        root_comment_id: int | None = None,
        source_kind: str = "inline",
        expected_head_sha: str | None = None,
    ) -> PreparedConversation | ReplyResult:
        """Authorize and bound a mention before any provider invocation."""
        return self._prepare_context(
            token=token,
            repository=repository,
            pull_request=pull_request,
            source_comment_id=source_comment_id,
            source_updated_at=source_updated_at,
            app_slug=app_slug,
            root_comment_id=root_comment_id,
            source_kind=source_kind,
            expected_head_sha=expected_head_sha,
        )

    def prepare_selected_assessment_context(
        self,
        *,
        token: str,
        selector: FeedbackSelector,
        app_slug: str,
        root_comment_id: int | None = None,
    ) -> PreparedConversation | ReplyResult:
        """Authorize the trigger for explicit selected-feedback preparation.

        This is not ordinary discussion context. The caller must subsequently
        use ``prepare_feedback`` with this exact selector: it obtains the full
        retained concern inventory, complete selected-source closure and current
        required review evidence. None of those are inferred from chat history.
        Generic conversation diff/learnings/history are not assessment inputs.
        Every request uses this publisher's original transport/budget.
        """
        if not isinstance(selector, FeedbackSelector) or not selector.target_ids:
            raise GitHubConversationError("selected assessment selector is required")
        return self._prepare_context(
            token=token,
            repository=selector.repository,
            pull_request=selector.pull_request,
            source_comment_id=selector.trigger.comment_id,
            source_updated_at=selector.trigger.updated_at,
            app_slug=app_slug,
            root_comment_id=root_comment_id,
            source_kind=selector.trigger.kind,
            expected_head_sha=selector.head_sha,
            assessment_selector=selector,
        )

    def _prepare_context(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        source_comment_id: int,
        source_updated_at: str,
        app_slug: str,
        root_comment_id: int | None = None,
        source_kind: str = "inline",
        expected_head_sha: str | None = None,
        assessment_selector: FeedbackSelector | None = None,
    ) -> PreparedConversation | ReplyResult:

        if source_kind not in {"inline", "issue"}:
            raise GitHubConversationError("reply source kind is invalid")
        if (
            isinstance(pull_request, bool)
            or not isinstance(pull_request, int)
            or pull_request < 1
            or isinstance(source_comment_id, bool)
            or not isinstance(source_comment_id, int)
            or source_comment_id < 1
        ):
            raise GitHubConversationError("conversation identifiers are invalid")
        if not isinstance(source_updated_at, str) or not source_updated_at.strip():
            raise GitHubConversationError("reply source update time is invalid")
        if expected_head_sha is not None and not GIT_SHA_HEX.fullmatch(
            expected_head_sha
        ):
            raise GitHubConversationError("reply head sha is invalid")

        source_path = (
            f"/pulls/comments/{source_comment_id}"
            if source_kind == "inline"
            else f"/issues/comments/{source_comment_id}"
        )
        status, source = self.http.request(
            "GET",
            self.http.repository_path(repository, source_path),
            token=token,
        )
        if status == 404:
            return ReplyResult(status="skipped_source_gone")
        if status < 200 or status >= 300 or not isinstance(source, dict):
            raise GitHubConversationError("reply source lookup failed")
        if assessment_selector is not None and (
            type(source.get("id")) is not int or source["id"] != source_comment_id
        ):
            raise GitHubConversationError("selected assessment source identity changed")
        self._require_comment_association(
            comment=source,
            repository=repository,
            pull_request=pull_request,
            source_kind=source_kind,
        )
        if not has_standalone_sensei_mention(source.get("body")):
            return ReplyResult(status="skipped_no_mention")
        if not authorized_human_comment(source, app_slug=app_slug):
            return ReplyResult(status="skipped_unauthorized")
        current_updated_at = source.get("updated_at")
        if not isinstance(current_updated_at, str) or _digest(
            current_updated_at
        ) != _digest(source_updated_at):
            return ReplyResult(status="skipped_edited_source")

        status, pr = self.http.request(
            "GET",
            self.http.repository_path(repository, f"/pulls/{pull_request}"),
            token=token,
        )
        if status < 200 or status >= 300 or not isinstance(pr, dict):
            raise GitHubConversationError("reply PR preflight failed")
        if pr.get("state") != "open" or pr.get("draft") is True:
            return ReplyResult(status="skipped_pr_state")
        head = pr.get("head")
        if not isinstance(head, dict) or not isinstance(head.get("sha"), str):
            raise GitHubConversationError("reply preflight head was invalid")
        head_sha = head["sha"]
        if not GIT_SHA_HEX.fullmatch(head_sha):
            raise GitHubConversationError("reply preflight head was invalid")
        if expected_head_sha is not None and head_sha != expected_head_sha:
            return ReplyResult(status="skipped_stale_head")
        head_repo = head.get("repo")
        base = pr.get("base")
        if not isinstance(head_repo, dict) or not isinstance(base, dict):
            raise GitHubConversationError("reply preflight repository was invalid")
        base_repo = base.get("repo")
        if not isinstance(base_repo, dict):
            raise GitHubConversationError("reply preflight repository was invalid")
        head_fork = head_repo.get("fork")
        base_fork = base_repo.get("fork")
        if not isinstance(head_fork, bool) or not isinstance(base_fork, bool):
            raise GitHubConversationError("reply preflight repository was invalid")
        if (
            head_fork
            or head_repo.get("full_name") != repository
            or base_fork
            or base_repo.get("full_name") != repository
        ):
            return ReplyResult(status="skipped_fork")

        resolved_root = source_comment_id
        if source_kind == "inline":
            in_reply_to = source.get("in_reply_to_id")
            if in_reply_to is not None:
                if (
                    isinstance(in_reply_to, bool)
                    or not isinstance(in_reply_to, int)
                    or in_reply_to < 1
                ):
                    raise GitHubConversationError("reply root comment is invalid")
                resolved_root = in_reply_to
            if root_comment_id is not None and root_comment_id != resolved_root:
                raise GitHubConversationError(
                    "reply root comment does not match source"
                )
            if resolved_root != source_comment_id:
                status, root = self.http.request(
                    "GET",
                    self.http.repository_path(
                        repository,
                        f"/pulls/comments/{resolved_root}",
                    ),
                    token=token,
                )
                if status != 200 or not isinstance(root, dict):
                    raise GitHubConversationError(
                        "reply root comment could not be resolved"
                    )
                if root.get("in_reply_to_id") is not None:
                    raise GitHubConversationError("reply root comment is not a root")
                self._require_comment_association(
                    comment=root,
                    repository=repository,
                    pull_request=pull_request,
                    source_kind="inline",
                )

        if assessment_selector is not None:
            base_sha = base.get("sha")
            if base_sha != assessment_selector.base_sha:
                return ReplyResult(status="skipped_stale_base")
            try:
                context = ConversationContext(
                    messages=tuple(
                        self._select_thread_messages(
                            candidates=[],
                            source=source,
                            source_comment_id=source_comment_id,
                            root_comment_id=resolved_root,
                            source_kind=source_kind,
                        )
                    ),
                    pull_request_number=pull_request,
                    head_sha=head_sha,
                    base_sha=base_sha,
                )
            except (ValueError, UnicodeError) as exc:
                raise GitHubConversationError(
                    "conversation context was invalid"
                ) from exc
            return PreparedConversation(
                context=context,
                source_kind=source_kind,
                source_comment_id=source_comment_id,
                source_updated_at=current_updated_at,
                head_sha=head_sha,
                root_comment_id=resolved_root,
            )

        try:
            thread_candidates = self._load_comments(
                token=token,
                repository=repository,
                pull_request=pull_request,
                source_kind=source_kind,
            )
            messages = self._select_thread_messages(
                candidates=thread_candidates,
                source=source,
                source_comment_id=source_comment_id,
                root_comment_id=resolved_root,
                source_kind=source_kind,
            )
            review_comments = (
                thread_candidates
                if source_kind == "inline"
                else self._load_comments(
                    token=token,
                    repository=repository,
                    pull_request=pull_request,
                    source_kind="inline",
                )
            )
            prior_findings = self._prior_findings(
                comments=review_comments,
                app_slug=app_slug,
                head_sha=head_sha,
            )
            priority_hunks = (
                self._prioritized_finding_hunks(
                    comments=review_comments,
                    app_slug=app_slug,
                    head_sha=head_sha,
                )
                if source_kind == "issue"
                else ()
            )
            diff_context, changed_paths = self._load_diff_context(
                token=token,
                repository=repository,
                pull_request=pull_request,
                source=source,
                source_kind=source_kind,
                priority_hunks=priority_hunks,
            )
            base_sha = base.get("sha")
            if not isinstance(base_sha, str) or not GIT_SHA_HEX.fullmatch(base_sha):
                raise GitHubConversationError("reply preflight base was invalid")
            learnings = self._load_base_learnings(
                token=token,
                repository=repository,
                base_sha=base_sha,
                changed_paths=changed_paths,
            )
        except (LearningLoadError, ReviewInputError, UnicodeError) as exc:
            raise GitHubConversationError("conversation context was invalid") from exc
        try:
            context = ConversationContext(
                messages=tuple(messages),
                pull_request_number=pull_request,
                head_sha=head_sha,
                pull_request_title=_bounded_text(
                    pr.get("title"), MAX_CONTEXT_PR_TITLE_BYTES
                ),
                pull_request_body=_bounded_text(
                    pr.get("body"), MAX_CONTEXT_PR_BODY_BYTES
                ),
                base_ref=_bounded_text(base.get("ref"), 512),
                base_sha=base_sha,
                head_ref=_bounded_text(head.get("ref"), 512),
                diff_context=diff_context,
                prior_findings=prior_findings,
                learnings=learnings,
            )
        except ValueError as exc:
            raise GitHubConversationError("conversation context was invalid") from exc
        return PreparedConversation(
            context=context,
            source_kind=source_kind,
            source_comment_id=source_comment_id,
            source_updated_at=current_updated_at,
            head_sha=head_sha,
            root_comment_id=resolved_root,
        )

    def _load_comments(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        source_kind: str,
    ) -> list[dict[str, Any]]:
        path = (
            f"/pulls/{pull_request}/comments"
            if source_kind == "inline"
            else f"/issues/{pull_request}/comments"
        )
        try:
            payload = self.http.paginate(
                path=self.http.repository_path(repository, path),
                token=token,
                page_sizes=CONVERSATION_COMMENT_PAGE_SIZES,
            )
        except GitHubHTTPError as exc:
            raise GitHubConversationError("conversation context lookup failed") from exc
        return [item for item in payload if isinstance(item, dict)]

    def _select_thread_messages(
        self,
        *,
        candidates: list[dict[str, Any]],
        source: dict[str, Any],
        source_comment_id: int,
        root_comment_id: int,
        source_kind: str,
    ) -> list[ConversationMessage]:
        if source_kind == "inline":
            by_id = {
                item["id"]: item
                for item in candidates
                if isinstance(item.get("id"), int)
            }
            wanted: set[int] = {root_comment_id}
            changed = True
            while changed:
                changed = False
                for item in candidates:
                    item_id = item.get("id")
                    parent_id = item.get("in_reply_to_id")
                    if (
                        isinstance(item_id, int)
                        and isinstance(parent_id, int)
                        and parent_id in wanted
                        and item_id not in wanted
                    ):
                        wanted.add(item_id)
                        changed = True
            selected = [
                item
                for item in candidates
                if isinstance(item.get("id"), int) and item["id"] in wanted
            ]
            if source_comment_id not in by_id:
                selected.append(source)
        else:
            selected = candidates
        if len(selected) > MAX_THREAD_MESSAGES:
            selected = selected[-MAX_THREAD_MESSAGES:]
        if not any(item.get("id") == source_comment_id for item in selected):
            selected = [source, *selected[: MAX_THREAD_MESSAGES - 1]]

        messages: list[ConversationMessage] = []
        for item in selected:
            body = item.get("body")
            user = item.get("user")
            author = user.get("login") if isinstance(user, dict) else None
            created_at = item.get("created_at")
            if not isinstance(body, str) or not body.strip():
                continue
            if not isinstance(author, str) or not author.strip():
                author = "unknown"
            if not isinstance(created_at, str) or not created_at.strip():
                created_at = "unknown"
            try:
                messages.append(
                    ConversationMessage(
                        author=_bounded_text(author, MAX_CONTEXT_AUTHOR_BYTES)
                        or "unknown",
                        body=_bounded_text(body, MAX_CONTEXT_MESSAGE_BYTES)
                        or "[empty]",
                        created_at=_bounded_text(
                            created_at, MAX_CONTEXT_TIMESTAMP_BYTES
                        )
                        or "unknown",
                    )
                )
            except ValueError as exc:
                raise GitHubConversationError(
                    "conversation context exceeded a configured limit"
                ) from exc
        if not messages:
            raise GitHubConversationError("conversation context was empty")
        return messages

    def load_human_assessment_diff(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        pending_paths: Sequence[str],
    ) -> HumanAssessmentDiffContext:
        """Admit required current file patches first, without truncating them.

        The inventory has no authoritative hunk locations, so a fragment of a
        required patch cannot represent complete evidence. Unrelated files may
        use only the remaining budget. This leaves ordinary conversations and
        their historical inline-hunk selection unchanged.
        """
        from review_sensei.human_assessment import MAX_HUMAN_FINDINGS

        if isinstance(pending_paths, str) or len(pending_paths) > MAX_HUMAN_FINDINGS:
            raise GitHubConversationError("human assessment paths are invalid")
        try:
            for path in pending_paths:
                validate_repository_path(
                    path, label="human assessment path", allow_glob_chars=True
                )
        except ReviewInputError as exc:
            raise GitHubConversationError("human assessment paths are invalid") from exc
        required = sorted(set(pending_paths))
        if not required:
            return HumanAssessmentDiffContext(None)
        try:
            files = self.http.paginate(
                path=self.http.repository_path(
                    repository, f"/pulls/{pull_request}/files"
                ),
                token=token,
            )
        except GitHubHTTPError as exc:
            raise GitHubConversationError(
                "human assessment diff lookup failed"
            ) from exc
        required_patches: dict[str, str | None] = {}
        optional: list[tuple[str, str]] = []
        for item in files:
            if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
                continue
            filename = item["filename"]
            validate_repository_path(
                filename, label="conversation diff path", allow_glob_chars=True
            )
            raw_patch = item.get("patch")
            patch = (
                raw_patch if isinstance(raw_patch, str) and raw_patch.strip() else None
            )
            if filename in required:
                if filename in required_patches and required_patches[filename] != patch:
                    return HumanAssessmentDiffContext(
                        None, "human_assessment_evidence_conflicting_patch"
                    )
                required_patches[filename] = patch
            elif patch is not None:
                optional.append((filename, patch))
        if any(required_patches.get(path) is None for path in required):
            return HumanAssessmentDiffContext(
                None, "human_assessment_evidence_missing_patch"
            )
        parts: list[str] = []
        used = 0
        for path in required:
            part = f"path={path}\n{required_patches[path]}"
            size = len(part.encode("utf-8")) + bool(parts)
            if used + size > MAX_CONTEXT_DIFF_BYTES:
                return HumanAssessmentDiffContext(
                    None, "human_assessment_evidence_budget_exhausted"
                )
            parts.append(part)
            used += size
        for path, patch in optional:
            remaining = MAX_CONTEXT_DIFF_BYTES - used - 1
            if remaining < 1:
                break
            header = f"path={path}\n"
            if remaining <= len(header.encode("utf-8")):
                continue
            optional_part = _bounded_text(header + patch, remaining)
            if optional_part:
                parts.append(optional_part)
                used += len(optional_part.encode("utf-8")) + 1
        return HumanAssessmentDiffContext("\n".join(parts))

    def load_review_evidence(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        base_sha: str,
        head_sha: str,
        required_paths: Sequence[str],
        timeout_seconds: float = 120,
        include_all_changed: bool = False,
        before_read=None,
    ):
        """Load exact evidence with shared acquisition accounting and fences.

        The integrated host must pass its original before_read guard; partition,
        feedback and evidence phases must not replenish one another's allowance.
        Transport failures retain pending obligations without partial authority.
        """
        from review_sensei.evidence import EvidenceBundle, EvidenceSnapshot

        if isinstance(required_paths, str) or len(required_paths) > 64:
            raise GitHubConversationError("human assessment paths are invalid")
        for path in required_paths:
            validate_repository_path(
                path, label="human assessment path", allow_glob_chars=True
            )
        snapshot = EvidenceSnapshot(repository, pull_request, base_sha, head_sha)
        try:
            return self.http.load_review_evidence(
                token=token,
                snapshot=snapshot,
                required_paths=tuple(required_paths),
                include_all_changed=include_all_changed,
                timeout_seconds=min(60, timeout_seconds),
                before_read=before_read,
            )
        except GitHubHTTPError:
            return EvidenceBundle(snapshot, (), enumeration_complete=False)

    def _load_diff_context(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        source: dict[str, Any],
        source_kind: str,
        priority_hunks: Sequence[tuple[str, str]] = (),
    ) -> tuple[str | None, tuple[str, ...]]:
        source_path = source.get("path")
        source_hunk = source.get("diff_hunk")
        if source_kind == "inline" and isinstance(source_path, str):
            validate_repository_path(
                source_path, label="conversation diff path", allow_glob_chars=True
            )
            if isinstance(source_hunk, str) and source_hunk.strip():
                bounded = _bounded_text(
                    f"path={source_path}\n{source_hunk}", MAX_CONTEXT_DIFF_BYTES
                )
                return bounded, (source_path,)

        try:
            files = self.http.paginate(
                path=self.http.repository_path(
                    repository, f"/pulls/{pull_request}/files"
                ),
                token=token,
            )
        except GitHubHTTPError as exc:
            raise GitHubConversationError("conversation diff lookup failed") from exc
        parts: list[str] = []
        paths: list[str] = []
        file_patches: list[tuple[str, str]] = []
        used = 0

        def append_part(
            part: str, *, allow_truncation: bool
        ) -> tuple[str | None, bool]:
            nonlocal used
            separator_bytes = 1 if parts else 0
            remaining = MAX_CONTEXT_DIFF_BYTES - used - separator_bytes
            if remaining < 1:
                return None, True
            if not allow_truncation and len(part.encode("utf-8")) > remaining:
                return None, False
            bounded = _bounded_text(part, remaining)
            if bounded is None:
                return None, False
            bounded_bytes = len(bounded.encode("utf-8"))
            if bounded_bytes > remaining:
                return None, False
            parts.append(bounded)
            used += separator_bytes + bounded_bytes
            return bounded, False

        for item in files:
            if not isinstance(item, dict):
                continue
            filename = item.get("filename")
            patch = item.get("patch")
            if not isinstance(filename, str):
                continue
            validate_repository_path(
                filename, label="conversation diff path", allow_glob_chars=True
            )
            paths.append(filename)
            if not isinstance(patch, str) or not patch.strip():
                continue
            file_patches.append((filename, patch))

        emitted_priority_hunks: dict[str, set[str]] = {}
        seen_priority: set[tuple[str, str]] = set()
        for filename, diff_hunk in priority_hunks:
            try:
                validate_repository_path(
                    filename, label="conversation diff path", allow_glob_chars=True
                )
            except ReviewInputError:
                continue
            if not isinstance(diff_hunk, str) or not diff_hunk.strip():
                continue
            priority = (filename, diff_hunk)
            if priority in seen_priority:
                continue
            seen_priority.add(priority)
            part = f"path={filename}\n{diff_hunk}"
            bounded, budget_exhausted = append_part(part, allow_truncation=False)
            if budget_exhausted:
                break
            if bounded is None:
                continue
            if bounded == part:
                emitted_priority_hunks.setdefault(filename, set()).add(diff_hunk)

        for filename, patch in file_patches:
            remaining_patch = _remove_emitted_priority_hunks(
                patch,
                emitted_priority_hunks.get(filename, set()),
                filename=filename,
            )
            if not remaining_patch.strip():
                continue
            part = f"path={filename}\n{remaining_patch}"
            _, budget_exhausted = append_part(part, allow_truncation=True)
            if budget_exhausted:
                break
        return ("\n".join(parts) or None), tuple(dict.fromkeys(paths))

    @classmethod
    def _prioritized_finding_hunks(
        cls,
        *,
        comments: list[dict[str, Any]],
        app_slug: str,
        head_sha: str,
    ) -> tuple[tuple[str, str], ...]:
        """Return newest current-head App hunks for issue-level context first."""

        current = sorted(
            (
                item
                for item in comments
                if cls._is_current_app_finding(
                    item,
                    app_slug=app_slug,
                    head_sha=head_sha,
                )
            ),
            key=cls._comment_created_at,
        )[-20:]
        hunks: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in reversed(current):
            path = item.get("path")
            diff_hunk = item.get("diff_hunk")
            if not isinstance(path, str) or not path:
                continue
            try:
                validate_repository_path(
                    path, label="conversation finding path", allow_glob_chars=True
                )
            except ReviewInputError:
                continue
            if not isinstance(diff_hunk, str) or not diff_hunk.strip():
                continue
            value = (path, diff_hunk)
            if value in seen:
                continue
            seen.add(value)
            hunks.append(value)
        return tuple(hunks)

    @staticmethod
    def _comment_created_at(item: dict[str, Any]) -> str:
        created_at = item.get("created_at")
        return created_at if isinstance(created_at, str) else ""

    @staticmethod
    def _is_current_app_finding(
        item: dict[str, Any], *, app_slug: str, head_sha: str
    ) -> bool:
        user = item.get("user")
        return (
            isinstance(user, dict)
            and user.get("login") == app_slug
            and item.get("commit_id") == head_sha
        )

    @staticmethod
    def _prior_findings(
        *,
        comments: list[dict[str, Any]],
        app_slug: str,
        head_sha: str,
    ) -> tuple[ConversationFinding, ...]:
        findings: list[ConversationFinding] = []
        for item in comments:
            if not ConversationPublisher._is_current_app_finding(
                item,
                app_slug=app_slug,
                head_sha=head_sha,
            ):
                continue
            body = _bounded_text(item.get("body"), MAX_CONTEXT_FINDING_BYTES)
            if body is None:
                continue
            path = item.get("path")
            line = item.get("line")
            findings.append(
                ConversationFinding(
                    body=body,
                    path=path if isinstance(path, str) else None,
                    line=line
                    if isinstance(line, int) and not isinstance(line, bool)
                    else None,
                )
            )
        return tuple(findings[-20:])

    def _load_base_learnings(
        self,
        *,
        token: str,
        repository: str,
        base_sha: str,
        changed_paths: tuple[str, ...],
    ) -> tuple[LearningEntry, ...]:
        directory = validate_repository_path(
            ".github/review-sensei/learnings",
            label="conversation learning directory",
        )
        status, payload = self.http.request(
            "GET",
            self.http.repository_path(
                repository,
                f"/contents/{directory}?ref={quote(base_sha, safe='')}",
            ),
            token=token,
        )
        if status == 404:
            return ()
        if status < 200 or status >= 300 or not isinstance(payload, list):
            raise GitHubConversationError("conversation learning lookup failed")
        files = [item for item in payload if isinstance(item, dict)]
        directory_parts = tuple(directory.split("/"))
        learning_files: list[dict[str, Any]] = []
        for item in files:
            path = item.get("path")
            if item.get("type") != "file" or not isinstance(path, str):
                continue
            validate_repository_path(path, label="conversation learning path")
            path_parts = tuple(path.split("/"))
            if path_parts[: len(directory_parts)] != directory_parts:
                # Entries outside the configured directory are malformed Contents
                # responses; fail closed instead of treating them as docs.
                raise GitHubConversationError(
                    "conversation learning path was outside configured directory"
                )
            relative_parts = path_parts[len(directory_parts) :]
            if len(relative_parts) != 1:
                raise GitHubConversationError("conversation learning path was invalid")
            # Direct-child documentation is intentionally never fetched as learning content.
            if not path.endswith(".json"):
                continue
            learning_files.append(item)
        if len(learning_files) > MAX_LEARNING_FILES:
            raise GitHubConversationError("conversation contains too many learnings")
        entries: list[LearningEntry] = []
        for item in sorted(
            learning_files, key=lambda value: str(value.get("path", ""))
        ):
            path = item.get("path")
            if not isinstance(path, str):
                raise GitHubConversationError("conversation learning path was invalid")
            size = item.get("size")
            if isinstance(size, int) and size > MAX_LEARNING_FILE_BYTES:
                raise GitHubConversationError(
                    "conversation learning file was oversized"
                )
            file_status, file_payload = self.http.request(
                "GET",
                self.http.repository_path(
                    repository,
                    f"/contents/{quote(path, safe='/')}?ref={quote(base_sha, safe='')}",
                ),
                token=token,
            )
            if (
                file_status < 200
                or file_status >= 300
                or not isinstance(file_payload, dict)
                or file_payload.get("encoding") != "base64"
                or not isinstance(file_payload.get("content"), str)
            ):
                raise GitHubConversationError("conversation learning file was invalid")
            try:
                raw = base64.b64decode(
                    "".join(file_payload["content"].split()), validate=True
                )
                if len(raw) > MAX_LEARNING_FILE_BYTES:
                    raise ValueError("oversized")
                value = json.loads(raw.decode("utf-8", errors="strict"))
                if not isinstance(value, dict):
                    raise ValueError("invalid root")
                entry = LearningEntry.from_dict(value)
            except (
                binascii.Error,
                UnicodeDecodeError,
                json.JSONDecodeError,
                KeyError,
                TypeError,
                ValueError,
                ReviewInputError,
            ) as exc:
                raise GitHubConversationError(
                    "conversation learning file was invalid"
                ) from exc
            if entry.status == "active":
                entries.append(entry)
        applicable = LearningStore(entries).for_paths(changed_paths)
        selected: list[LearningEntry] = []
        used = 0
        for entry in applicable:
            size = len(
                json.dumps(
                    entry.to_prompt_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            if used + size > MAX_CONTEXT_LEARNINGS_BYTES:
                break
            selected.append(entry)
            used += size
        return tuple(selected)

    def _can_record_resolution(
        self,
        *,
        reply: ConversationReply,
        root_is_app_authored: bool,
        root_body: object,
        resolution_evidence: str | None,
    ) -> bool:
        return bool(
            reply.resolve
            and root_is_app_authored
            and finding_instance_from_body(root_body) is not None
            and isinstance(resolution_evidence, str)
            and re.fullmatch(r"[a-f0-9]{64}", resolution_evidence)
        )

    def publish(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        source_comment_id: int,
        source_updated_at: str,
        head_sha: str,
        reply: ConversationReply,
        app_slug: str,
        root_comment_id: int,
        source_kind: str = "inline",
        pending_page: PendingReplyPage | None = None,
        resolution_evidence: str | None = None,
        resolution_base_sha: str | None = None,
        resolution_token: str | None = None,
    ) -> ReplyResult:
        return self._publish(
            token=token,
            repository=repository,
            pull_request=pull_request,
            source_comment_id=source_comment_id,
            source_updated_at=source_updated_at,
            head_sha=head_sha,
            reply=reply,
            app_slug=app_slug,
            root_comment_id=root_comment_id,
            source_kind=source_kind,
            pending_page=pending_page,
            resolution_evidence=resolution_evidence,
            resolution_base_sha=resolution_base_sha,
            resolution_token=resolution_token,
        )

    def _publish(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        source_comment_id: int,
        source_updated_at: str,
        head_sha: str,
        reply: ConversationReply,
        app_slug: str,
        root_comment_id: int,
        source_kind: str = "inline",
        pending_page: PendingReplyPage | None = None,
        resolution_evidence: str | None = None,
        resolution_base_sha: str | None = None,
        resolution_token: str | None = None,
        complete_report_readback: bool = False,
    ) -> ReplyResult:
        if not GIT_SHA_HEX.fullmatch(head_sha):
            raise GitHubConversationError("reply head sha is invalid")
        if not isinstance(reply, ConversationReply):
            raise GitHubConversationError("reply is invalid")
        marker = reply_marker(
            source_comment_id=source_comment_id,
            source_updated_digest=_digest(source_updated_at),
            pull_request=pull_request,
            head_sha=head_sha,
        )
        if pending_page is not None:
            if not isinstance(pending_page, PendingReplyPage):
                raise GitHubConversationError("pending reply page is invalid")
            marker = marker[:-4] + (
                f" report={pending_page.report_digest} "
                f"part={pending_page.index + 1}/{pending_page.count} "
                f"content={_digest(append_response_instructions(pending_page.body))} -->"
            )
        body = (
            f"{append_response_instructions(pending_page.body if pending_page else reply.body)}"
            f"\n\n{marker}"
        )
        if len(body.encode("utf-8")) > MAX_REPLY_BYTES:
            raise GitHubConversationError("reply body exceeds the configured limit")
        if source_kind not in {"inline", "issue"}:
            raise GitHubConversationError("reply source kind is invalid")
        source_path = (
            f"/pulls/comments/{source_comment_id}"
            if source_kind == "inline"
            else f"/issues/comments/{source_comment_id}"
        )
        status, source = self.http.request(
            "GET",
            self.http.repository_path(
                repository,
                source_path,
            ),
            token=token,
        )
        if status == 404:
            return ReplyResult(status="skipped_source_gone")
        if status < 200 or status >= 300 or not isinstance(source, dict):
            raise GitHubConversationError("reply source lookup failed")
        self._require_comment_association(
            comment=source,
            repository=repository,
            pull_request=pull_request,
            source_kind=source_kind,
        )
        if not has_standalone_sensei_mention(source.get("body")):
            return ReplyResult(status="skipped_no_mention")
        if not authorized_human_comment(source, app_slug=app_slug):
            return ReplyResult(status="skipped_unauthorized")
        updated_at = source.get("updated_at")
        if not isinstance(updated_at, str) or _digest(updated_at) != _digest(
            source_updated_at
        ):
            return ReplyResult(status="skipped_edited_source")
        in_reply_to = source.get("in_reply_to_id")
        if source_kind == "issue":
            resolved_root = None
        else:
            authoritative_root = (
                source_comment_id if not isinstance(in_reply_to, int) else in_reply_to
            )
            if root_comment_id != authoritative_root:
                raise GitHubConversationError(
                    "reply root comment does not match source"
                )
            resolved_root = authoritative_root
        root_is_app_authored = False
        root_is_blocking_finding = False
        root_body: object = None
        if source_kind == "inline" and resolved_root != source_comment_id:
            status, root = self.http.request(
                "GET",
                self.http.repository_path(
                    repository,
                    f"/pulls/comments/{resolved_root}",
                ),
                token=token,
            )
            if status != 200 or not isinstance(root, dict):
                raise GitHubConversationError(
                    "reply root comment could not be resolved"
                )
            if root.get("in_reply_to_id") is not None:
                raise GitHubConversationError("reply root comment is not a root")
            self._require_comment_association(
                comment=root,
                repository=repository,
                pull_request=pull_request,
                source_kind="inline",
            )
            root_user = root.get("user")
            root_is_app_authored = (
                isinstance(root_user, dict)
                and isinstance(root_user.get("login"), str)
                and root_user["login"].casefold() == app_slug.casefold()
                and root_user.get("type") == "Bot"
            )
            root_body = root.get("body")
            root_is_blocking_finding = finding_declares_blocking(root_body)

        existing = self._find_existing(
            token=token,
            repository=repository,
            pull_request=pull_request,
            marker=marker,
            app_slug=app_slug,
            source_kind=source_kind,
            expected_body=body if pending_page is not None else None,
        )
        if existing is not None:
            if self._can_record_resolution(
                reply=reply,
                root_is_app_authored=root_is_app_authored,
                root_body=root_body,
                resolution_evidence=resolution_evidence,
            ):
                resolution = self._resolve_and_finalize(
                    token=resolution_token,
                    repository=repository,
                    pull_request=pull_request,
                    root_comment_id=resolved_root,
                    head_sha=head_sha,
                    app_slug=app_slug,
                    root_is_blocking_finding=root_is_blocking_finding,
                    source_comment_id=source_comment_id,
                    source_updated_at=source_updated_at,
                    evidence_sha256=resolution_evidence,
                    base_sha=resolution_base_sha,
                )
                return ReplyResult(
                    status="already_replied_and_resolved"
                    if resolution.resolved
                    else "already_replied",
                    comment_id=existing,
                    resolved=resolution.resolved,
                    resolution_status=resolution.resolution_status,
                    approval_status=resolution.approval_status,
                    approval_diagnostic=resolution.approval_diagnostic,
                )
            return ReplyResult(status="already_replied", comment_id=existing)

        status, pr = self.http.request(
            "GET",
            self.http.repository_path(repository, f"/pulls/{pull_request}"),
            token=token,
        )
        if status < 200 or status >= 300 or not isinstance(pr, dict):
            raise GitHubConversationError("reply PR preflight failed")
        if pr.get("state") != "open" or pr.get("draft") is True:
            return ReplyResult(status="skipped_pr_state")
        head = pr.get("head")
        if not isinstance(head, dict) or head.get("sha") != head_sha:
            return ReplyResult(status="skipped_stale_head")
        head_repo = head.get("repo")
        if not isinstance(head_repo, dict):
            raise GitHubConversationError("reply preflight head repository was invalid")
        base = pr.get("base")
        if not isinstance(base, dict):
            raise GitHubConversationError("reply preflight base was invalid")
        base_repo = base.get("repo")
        if not isinstance(base_repo, dict):
            raise GitHubConversationError("reply preflight repository was invalid")
        head_fork = head_repo.get("fork")
        base_fork = base_repo.get("fork")
        if not isinstance(head_fork, bool) or not isinstance(base_fork, bool):
            raise GitHubConversationError("reply preflight repository was invalid")
        if (
            head_fork
            or head_repo.get("full_name") != repository
            or base_fork
            or base_repo.get("full_name") != repository
        ):
            return ReplyResult(status="skipped_fork")

        request_body: dict[str, object] = {"body": body}
        target = (
            f"/pulls/{pull_request}/comments/{resolved_root}/replies"
            if source_kind == "inline"
            else f"/issues/{pull_request}/comments"
        )
        try:
            status, created = self.http.request(
                "POST",
                self.http.repository_path(repository, target),
                token=token,
                body=request_body,
            )
        except GitHubHTTPTransientError as exc:
            existing_after = self._find_existing(
                token=token,
                repository=repository,
                pull_request=pull_request,
                marker=marker,
                app_slug=app_slug,
                source_kind=source_kind,
                expected_body=body if pending_page is not None else None,
            )
            if existing_after is not None:
                return ReplyResult(status="already_replied", comment_id=existing_after)
            raise GitHubConversationTransientError(
                "reply publication failed temporarily"
            ) from exc
        if status == 201 and isinstance(created, dict):
            comment_id = created.get("id")
            if type(comment_id) is int and comment_id > 0:
                if pending_page is not None:
                    confirmed = (
                        self._owned_pending_page(
                            token=token,
                            repository=repository,
                            pull_request=pull_request,
                            comment_id=comment_id,
                            app_slug=app_slug,
                            source_kind=source_kind,
                            expected_body=body,
                        )
                        if complete_report_readback
                        else self._find_existing(
                            token=token,
                            repository=repository,
                            pull_request=pull_request,
                            marker=marker,
                            app_slug=app_slug,
                            source_kind=source_kind,
                            expected_body=body,
                        )
                    )
                    if confirmed != comment_id:
                        raise GitHubConversationTransientError(
                            "pending reply page readback was not confirmed"
                        )
                if self._can_record_resolution(
                    reply=reply,
                    root_is_app_authored=root_is_app_authored,
                    root_body=root_body,
                    resolution_evidence=resolution_evidence,
                ):
                    resolution = self._resolve_and_finalize(
                        token=resolution_token,
                        repository=repository,
                        pull_request=pull_request,
                        root_comment_id=resolved_root,
                        head_sha=head_sha,
                        app_slug=app_slug,
                        root_is_blocking_finding=root_is_blocking_finding,
                        source_comment_id=source_comment_id,
                        source_updated_at=source_updated_at,
                        evidence_sha256=resolution_evidence,
                        base_sha=resolution_base_sha,
                    )
                    return ReplyResult(
                        status="replied_and_resolved"
                        if resolution.resolved
                        else "replied",
                        comment_id=comment_id,
                        resolved=resolution.resolved,
                        resolution_status=resolution.resolution_status,
                        approval_status=resolution.approval_status,
                        approval_diagnostic=resolution.approval_diagnostic,
                    )
                return ReplyResult(status="replied", comment_id=comment_id)
        if status == 422 or status == 409 or status == 429 or status >= 500:
            existing_after = self._find_existing(
                token=token,
                repository=repository,
                pull_request=pull_request,
                marker=marker,
                app_slug=app_slug,
                source_kind=source_kind,
                expected_body=body if pending_page is not None else None,
            )
            if existing_after is not None:
                if self._can_record_resolution(
                    reply=reply,
                    root_is_app_authored=root_is_app_authored,
                    root_body=root_body,
                    resolution_evidence=resolution_evidence,
                ):
                    resolution = self._resolve_and_finalize(
                        token=resolution_token,
                        repository=repository,
                        pull_request=pull_request,
                        root_comment_id=resolved_root,
                        head_sha=head_sha,
                        app_slug=app_slug,
                        root_is_blocking_finding=root_is_blocking_finding,
                        source_comment_id=source_comment_id,
                        source_updated_at=source_updated_at,
                        evidence_sha256=resolution_evidence,
                        base_sha=resolution_base_sha,
                    )
                    return ReplyResult(
                        status="already_replied_and_resolved"
                        if resolution.resolved
                        else "already_replied",
                        comment_id=existing_after,
                        resolved=resolution.resolved,
                        resolution_status=resolution.resolution_status,
                        approval_status=resolution.approval_status,
                        approval_diagnostic=resolution.approval_diagnostic,
                    )
                return ReplyResult(status="already_replied", comment_id=existing_after)
            if status == 422:
                raise GitHubConversationError("reply publication was rejected")
            raise GitHubConversationTransientError(
                "reply publication failed temporarily"
            )
        if status == 404:
            raise GitHubConversationError("reply target was not found")
        if status == 403:
            raise GitHubConversationError("reply publication lacks permission")
        if status == 429 or status >= 500:
            raise GitHubConversationTransientError(
                "reply publication failed temporarily"
            )
        raise GitHubConversationError("reply publication was rejected")

    @bounded_reply_publication
    def publish_pending(
        self,
        *,
        eligibility,
        fence: Callable[[], str | None],
        authority_digest: str = "",
        diagnostic: str | None = None,
        decision_reasons=None,
        assessment_digest: str = "",
        layout: str = "legacy",
        **arguments,
    ) -> ReplyResult:
        """Confirm every bounded page before allowing caller authority updates.

        Use the same transport and caller fence for the entire report. A retry
        reconciles exact App-authored bodies under head/source/report-bound
        markers; neither another source nor a changed result can reuse pages.
        No per-page execution or feedback allowance is created here.
        """
        if not callable(fence):
            raise GitHubConversationError("pending reply fence is missing")
        reply = arguments.get("reply")
        if not isinstance(reply, ConversationReply):
            raise GitHubConversationError("reply is invalid")
        pages = pending_reply_pages(
            body=reply.body,
            head_sha=arguments["head_sha"],
            eligibility=eligibility,
            authority_digest=authority_digest,
            diagnostic=diagnostic,
            decision_reasons=decision_reasons,
            assessment_digest=assessment_digest,
            layout=layout,
        )
        failure = fence()
        if failure is not None:
            return ReplyResult(
                status=failure, assessment_status="pending_delivery_incomplete"
            )
        previous = self._completed_pending_report(
            token=arguments["token"],
            repository=arguments["repository"],
            pull_request=arguments["pull_request"],
            source_kind=arguments.get("source_kind", "inline"),
            source_comment_id=arguments["source_comment_id"],
            source_updated_at=arguments["source_updated_at"],
            head_sha=arguments["head_sha"],
            report_digest=pages[0].report_digest,
            pages=pages,
            app_slug=arguments["app_slug"],
        )
        if previous is not None:
            failure = fence()
            return ReplyResult(
                status=failure or "already_replied",
                comment_id=previous,
                assessment_status="pending_delivery_incomplete" if failure else None,
            )
        # Resolution is a distinct operation. Never resolve/finalize during a
        # partial receipt, even when ordinary conversation inference asked to.
        page_arguments = {**arguments, "reply": ConversationReply(reply.body, False)}
        last = ReplyResult(status="pending_delivery_incomplete")
        created = False
        for page in pages:
            failure = fence()
            if failure is not None:
                return ReplyResult(
                    status=failure,
                    comment_id=last.comment_id,
                    assessment_status="pending_delivery_incomplete",
                )
            last = (
                self._publish(
                    **page_arguments, pending_page=page, complete_report_readback=True
                )
                if layout == "shared-guidance-v1"
                else self.publish(**page_arguments, pending_page=page)
            )
            if last.status not in {"replied", "already_replied"}:
                return ReplyResult(
                    status=last.status,
                    comment_id=last.comment_id,
                    assessment_status="pending_delivery_incomplete",
                )
            created = created or last.status == "replied"
        if layout == "shared-guidance-v1":
            # The compact opt-in report is accepted only as a complete owned
            # report. A later page POST cannot hide edits/removal of an earlier
            # page that already passed its individual readback.
            complete = self._completed_pending_report(
                token=arguments["token"],
                repository=arguments["repository"],
                pull_request=arguments["pull_request"],
                source_kind=arguments.get("source_kind", "inline"),
                source_comment_id=arguments["source_comment_id"],
                source_updated_at=arguments["source_updated_at"],
                head_sha=arguments["head_sha"],
                report_digest=pages[0].report_digest,
                pages=pages,
                app_slug=arguments["app_slug"],
            )
            if complete is None:
                raise GitHubConversationError("pending report readback is incomplete")
        failure = fence()
        if failure is not None:
            return ReplyResult(
                status=failure,
                comment_id=last.comment_id,
                assessment_status="pending_delivery_incomplete",
            )
        return ReplyResult(
            status="replied" if created else "already_replied",
            comment_id=last.comment_id,
        )

    def _owned_pending_page(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        comment_id: int,
        app_slug: str,
        source_kind: str,
        expected_body: str,
    ) -> int | None:
        """Read the known successful POST id before the final whole-report scan.

        This private path never replaces negative lookup or ambiguous-POST
        reconciliation. Only the enclosing compact report transaction uses it;
        that transaction still scans every report page/duplicate/conflict before
        its last live source/PR/latest-authority fence.
        """
        suffix = "pulls" if source_kind == "inline" else "issues"
        status, comment = self.http.request(
            "GET",
            self.http.repository_path(repository, f"/{suffix}/comments/{comment_id}"),
            token=token,
        )
        if status != 200 or not isinstance(comment, dict):
            raise GitHubConversationError("pending reply page readback unavailable")
        self._require_comment_association(
            comment=comment,
            repository=repository,
            pull_request=pull_request,
            source_kind=source_kind,
        )
        user = comment.get("user")
        if (
            type(comment.get("id")) is not int
            or comment["id"] != comment_id
            or comment.get("body") != expected_body
            or not isinstance(user, dict)
            or user.get("login") != app_slug
            or user.get("type") != "Bot"
        ):
            raise GitHubConversationError("pending reply page readback conflicts")
        return comment_id

    def _completed_pending_report(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        source_kind: str,
        source_comment_id: int,
        source_updated_at: str,
        head_sha: str,
        report_digest: str,
        pages: tuple[PendingReplyPage, ...],
        app_slug: str,
    ) -> int | None:
        """Reconcile complete earlier delivery for the same authoritative state."""
        marker = (
            reply_marker(
                source_comment_id=source_comment_id,
                source_updated_digest=_digest(source_updated_at),
                pull_request=pull_request,
                head_sha=head_sha,
            )[:-4]
            + f" report={report_digest} part="
        )
        path = self.http.repository_path(
            repository,
            f"/pulls/{pull_request}/comments"
            if source_kind == "inline"
            else f"/issues/{pull_request}/comments",
        )
        comments = self.http.paginate(
            path=path, token=token, page_sizes=CONVERSATION_COMMENT_PAGE_SIZES
        )
        found: dict[int, int] = {}
        counts: set[int] = set()
        for comment in comments:
            if not isinstance(comment, dict):
                continue
            user, body = comment.get("user"), comment.get("body")
            if (
                not isinstance(user, dict)
                or user.get("login") != app_slug
                or not isinstance(body, str)
                or marker not in body
            ):
                continue
            content, separator, tail = body.rpartition("\n\n" + marker)
            parsed = re.fullmatch(r"(\d+)/(\d+) content=([a-f0-9]{64}) -->", tail)
            if (
                not separator
                or parsed is None
                or user.get("type") != "Bot"
                or len(body.encode("utf-8")) > MAX_REPLY_BYTES
                or type(comment.get("id")) is not int
                or comment["id"] < 1
            ):
                raise GitHubConversationError("pending reply receipt conflicts")
            index, count = int(parsed[1]), int(parsed[2])
            if (
                not 1 <= index <= count == len(pages)
                or index in found
                or comment["id"] in found.values()
                or _digest(content) != parsed[3]
            ):
                raise GitHubConversationError("pending reply receipt conflicts")
            expected_content = append_response_instructions(pages[index - 1].body)
            if content != expected_content:
                raise GitHubConversationError("pending reply receipt conflicts")
            found[index] = comment["id"]
            counts.add(count)
        if len(counts) > 1:
            raise GitHubConversationError("pending reply receipt conflicts")
        if counts and set(found) == set(range(1, next(iter(counts)) + 1)):
            return found[max(found)]
        return None

    @bounded_reply_publication
    def publish_current(self, **arguments) -> ReplyResult:
        """Ordinary replies also show trusted current-head pending state."""
        eligibility = self.finalizer.load_eligibility(
            token=arguments["token"],
            repository=arguments["repository"],
            pull_request=arguments["pull_request"],
            head_sha=arguments["head_sha"],
            app_slug=arguments["app_slug"],
        )

        def fence() -> str | None:
            source_kind = arguments.get("source_kind", "inline")
            suffix = "pulls" if source_kind == "inline" else "issues"
            status, source = self.http.request(
                "GET",
                self.http.repository_path(
                    arguments["repository"],
                    f"/{suffix}/comments/{arguments['source_comment_id']}",
                ),
                token=arguments["token"],
            )
            if status == 404:
                return "skipped_source_gone"
            if status != 200 or not isinstance(source, dict):
                raise GitHubConversationError("reply source lookup failed")
            self._require_comment_association(
                comment=source,
                repository=arguments["repository"],
                pull_request=arguments["pull_request"],
                source_kind=source_kind,
            )
            if not authorized_human_comment(source, app_slug=arguments["app_slug"]):
                return "skipped_unauthorized"
            if not has_standalone_sensei_mention(source.get("body")):
                return "skipped_no_mention"
            if source.get("updated_at") != arguments["source_updated_at"]:
                return "skipped_edited_source"
            current = self.finalizer.load_eligibility(
                token=arguments["token"],
                repository=arguments["repository"],
                pull_request=arguments["pull_request"],
                head_sha=arguments["head_sha"],
                app_slug=arguments["app_slug"],
            )
            if current != eligibility:
                return "skipped_stale_head"
            preflight = self.finalizer._preflight(
                token=arguments["token"],
                repository=arguments["repository"],
                pull_request=arguments["pull_request"],
                head_sha=arguments["head_sha"],
                app_slug=arguments["app_slug"],
            )
            return preflight.result.status if preflight.result is not None else None

        outcome = self.publish_pending(
            eligibility=eligibility, fence=fence, **arguments
        )
        # Keep the existing resolver/finalizer, but enter only after a complete
        # pending receipt. Its own live head/thread/eligibility guards still run.
        if arguments["reply"].resolve and outcome.status in {
            "replied",
            "already_replied",
        }:
            last_page = pending_reply_pages(
                body=arguments["reply"].body,
                head_sha=arguments["head_sha"],
                eligibility=eligibility,
            )[-1]
            resolved = self.publish(**arguments, pending_page=last_page)
            return resolved
        return outcome

    def _resolve_and_finalize(
        self,
        *,
        token: str | None,
        repository: str,
        pull_request: int,
        root_comment_id: int | None,
        head_sha: str,
        app_slug: str,
        root_is_blocking_finding: bool,
        source_comment_id: int | None = None,
        source_updated_at: str | None = None,
        evidence_sha256: str | None = None,
        base_sha: str | None = None,
    ) -> ReplyResult:
        """Confirm durable addressed authority before mutation; retain partial success."""
        from .thread_resolution import (
            FindingThreadResolver,
            digest,
            resolution_marker,
            resolution_records,
        )

        result = ReplyResult(
            status="replied",
            resolution_status="unconfirmed",
            approval_status="approval_withheld",
            approval_diagnostic="approval_withheld",
        )
        if not token:
            return result
        try:
            if (
                type(root_comment_id) is not int
                or type(source_comment_id) is not int
                or not isinstance(base_sha, str)
                or not GIT_SHA_HEX.fullmatch(base_sha)
                or not isinstance(evidence_sha256, str)
                or re.fullmatch(r"[a-f0-9]{64}", evidence_sha256) is None
            ):
                return result
            status, root = self.http.request(
                "GET",
                self.http.repository_path(
                    repository, f"/pulls/comments/{root_comment_id}"
                ),
                token=token,
            )
            if (
                status != 200
                or not isinstance(root, dict)
                or str(root.get("user", {}).get("login", "")).casefold()
                != app_slug.casefold()
                or root.get("user", {}).get("type") != "Bot"
            ):
                return result
            scope = finding_publication_scope(root.get("body"))
            instance = finding_instance_from_body(root.get("body"))
            if (
                scope is None
                or scope[1:4] != (pull_request, head_sha, base_sha)
                or instance is None
            ):
                return result
            status, source = self.http.request(
                "GET",
                self.http.repository_path(
                    repository, f"/pulls/comments/{source_comment_id}"
                ),
                token=token,
            )
            if (
                status != 200
                or not isinstance(source, dict)
                or source.get("updated_at") != source_updated_at
            ):
                return result
            reviews = self.finalizer._load_head_reviews(
                token=token, repository=repository, pull_request=pull_request
            )
            eligibility = self.finalizer._eligibility_from_reviews(
                reviews, head_sha=head_sha, app_slug=app_slug, require_valid=True
            )
            if eligibility is None:
                return result
            index = self.finalizer.finding_index_from_reviews(
                reviews,
                repository_id=scope[0],
                pull_request=pull_request,
                head_sha=head_sha,
                app_slug=app_slug,
                eligibility=eligibility,
            )
            if (
                index is None
                or index.base_sha != base_sha
                or not any(
                    entry.instance == instance and not entry.body_placed
                    for entry in index.entries
                )
            ):
                return result
            record = {
                "repo": scope[0],
                "pr": pull_request,
                "head": head_sha,
                "base": base_sha,
                "result": eligibility.result_digest,
                "instance": instance,
                "root": root_comment_id,
                "source": source_comment_id,
                "updated": source_updated_at,
                "actor": source.get("user", {}).get("login"),
                "source_digest": digest(source.get("body", "")),
                "evidence": evidence_sha256,
            }
            resolver = FindingThreadResolver(self.http)
            if not resolver.source_valid(
                token=token, repository=repository, record=record, app_slug=app_slug
            ):
                return result
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
            if (
                current != eligibility
                or live.result is not None
                or live.app_authored
                or live.repository_id != scope[0]
                or live.base_sha != base_sha
            ):
                return result
            if record not in resolution_records(reviews, app_slug):
                # The full record is independent of reply paging/size and is never omitted.
                body = (
                    "Recorded addressed finding; thread resolution and approval are checked separately.\n\n"
                    + resolution_marker(record)
                )
                try:
                    self.http.request(
                        "POST",
                        self.http.repository_path(
                            repository, f"/pulls/{pull_request}/reviews"
                        ),
                        token=token,
                        body={"commit_id": head_sha, "event": "COMMENT", "body": body},
                    )
                except GitHubHTTPError:
                    pass  # Ambiguous writes grant nothing until the exact record reads back.
                reviews = self.finalizer._load_head_reviews(
                    token=token, repository=repository, pull_request=pull_request
                )
                if record not in resolution_records(reviews, app_slug):
                    return result
            resolution = self.finalizer.reconcile_decided_threads(
                token=token,
                repository=repository,
                repository_id=scope[0],
                pull_request=pull_request,
                head_sha=head_sha,
                app_slug=app_slug,
                eligibility=eligibility,
                index=index,
                reviews=reviews,
            )
            resolved = (
                instance in resolution.resolved
                and instance not in resolution.unconfirmed
            )
            result = ReplyResult(
                status="replied",
                resolved=resolved,
                resolution_status="resolved" if resolved else "unconfirmed",
                approval_status="approval_withheld",
                approval_diagnostic="approval_withheld",
            )
            finalized = self.finalizer.finalize(
                token=token,
                repository=repository,
                pull_request=pull_request,
                head_sha=head_sha,
                app_slug=app_slug,
                eligibility=eligibility,
                require_persisted=True,
            )
            return ReplyResult(
                status="replied",
                resolved=resolved,
                resolution_status=result.resolution_status,
                approval_status=finalized.status,
                approval_diagnostic=finalized.diagnostic,
            )
        except (
            GitHubHTTPError,
            GitHubPublicationError,
            GitHubConversationError,
            ReviewInputError,
            AttributeError,
            TypeError,
        ):
            return result

    def _find_existing(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        marker: str,
        app_slug: str,
        source_kind: str = "inline",
        expected_body: str | None = None,
    ) -> int | None:
        path = self.http.repository_path(
            repository,
            f"/pulls/{pull_request}/comments"
            if source_kind == "inline"
            else f"/issues/{pull_request}/comments",
        )
        # A failed reconciliation is not evidence that no reply exists. Let
        # the bounded transport error propagate so publication fails closed.
        try:
            payload = self.http.paginate(
                path=path,
                token=token,
                page_sizes=CONVERSATION_COMMENT_PAGE_SIZES,
            )
        except GitHubHTTPTransientError as exc:
            raise GitHubConversationTransientError(
                "reply reconciliation failed temporarily"
            ) from exc
        except GitHubHTTPError as exc:
            raise GitHubConversationError("reply reconciliation failed") from exc
        found: list[int] = []
        for comment in payload:
            if not isinstance(comment, dict):
                continue
            body = comment.get("body")
            user = comment.get("user")
            if (
                isinstance(body, str)
                and marker in body
                and isinstance(user, dict)
                and user.get("login") == app_slug
            ):
                if expected_body is not None and (
                    body != expected_body or user.get("type") != "Bot"
                ):
                    raise GitHubConversationError("pending reply page conflicts")
                number = comment.get("id")
                if type(number) is int and number > 0:
                    if expected_body is None:
                        return number
                    found.append(number)
        if len(found) > 1:
            raise GitHubConversationError("pending reply page is duplicated")
        return found[0] if found else None
