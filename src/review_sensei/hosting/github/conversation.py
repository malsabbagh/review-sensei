"""Authorized, bounded GitHub mention-reply publication."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from ...errors import LearningLoadError, ReviewInputError
from ...learnings import MAX_LEARNING_FILE_BYTES, MAX_LEARNING_FILES, LearningStore
from ...models import (
    ConversationContext,
    ConversationFinding,
    ConversationMessage,
    ConversationReply,
    LearningEntry,
)
from ...validation import validate_repository_path
from .errors import (
    GitHubConversationError,
    GitHubConversationTransientError,
    GitHubHTTPError,
    GitHubHTTPTransientError,
)
from .http import GitHubHttp

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
MENTION_PATTERN = re.compile(r"(?i)(?:^|\s)@sensei(?:$|\s|[.,!?])")
AUTHORIZED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
GIT_SHA_HEX = re.compile(r"^[a-f0-9]{40}$")
MARKER_PREFIX = "<!-- reviewsensei:reply:v1"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bounded_text(value: object, maximum: int) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.encode("utf-8", errors="strict")
    if len(raw) <= maximum:
        return value
    suffix = b"\n[truncated]"
    clipped = raw[: maximum - len(suffix)]
    while clipped:
        try:
            return clipped.decode("utf-8", errors="strict") + suffix.decode("ascii")
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    return suffix.decode("ascii").lstrip()


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
    """Publish an App reply to an authorized explicit @sensei mention."""

    def __init__(self, *, http: GitHubHttp) -> None:
        self.http = http

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
            diff_context, changed_paths = self._load_diff_context(
                token=token,
                repository=repository,
                pull_request=pull_request,
                source=source,
                source_kind=source_kind,
            )
            prior_findings = self._prior_findings(
                comments=review_comments,
                app_slug=app_slug,
                head_sha=head_sha,
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

    def _load_diff_context(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        source: dict[str, Any],
        source_kind: str,
    ) -> tuple[str | None, tuple[str, ...]]:
        source_path = source.get("path")
        source_hunk = source.get("diff_hunk")
        if source_kind == "inline" and isinstance(source_path, str):
            validate_repository_path(source_path, label="conversation diff path")
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
        used = 0
        for item in files:
            if not isinstance(item, dict):
                continue
            filename = item.get("filename")
            patch = item.get("patch")
            if not isinstance(filename, str):
                continue
            validate_repository_path(filename, label="conversation diff path")
            paths.append(filename)
            if not isinstance(patch, str) or not patch.strip():
                continue
            part = f"path={filename}\n{patch}"
            remaining = MAX_CONTEXT_DIFF_BYTES - used
            if remaining < 1:
                break
            bounded = _bounded_text(part, remaining)
            if bounded is None:
                continue
            parts.append(bounded)
            used += len(bounded.encode("utf-8")) + 1
        return ("\n".join(parts) or None), tuple(dict.fromkeys(paths))

    @staticmethod
    def _prior_findings(
        *,
        comments: list[dict[str, Any]],
        app_slug: str,
        head_sha: str,
    ) -> tuple[ConversationFinding, ...]:
        findings: list[ConversationFinding] = []
        for item in comments:
            user = item.get("user")
            if (
                not isinstance(user, dict)
                or user.get("login") != app_slug
                or item.get("commit_id") != head_sha
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
        directory = ".github/review-sensei/learnings"
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
        if len(files) > MAX_LEARNING_FILES:
            raise GitHubConversationError("conversation contains too many learnings")
        entries: list[LearningEntry] = []
        for item in sorted(files, key=lambda value: str(value.get("path", ""))):
            path = item.get("path")
            if item.get("type") != "file" or not isinstance(path, str):
                continue
            validate_repository_path(path, label="conversation learning path")
            if not path.startswith(directory + "/") or not path.endswith(".json"):
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

        existing = self._find_existing(
            token=token,
            repository=repository,
            pull_request=pull_request,
            marker=marker,
            app_slug=app_slug,
            source_kind=source_kind,
        )
        if existing is not None:
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

        body = f"{reply.body}\n\n{marker}"
        if len(body.encode("utf-8")) > MAX_REPLY_BYTES:
            raise GitHubConversationError("reply body exceeds the configured limit")
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
            )
            if existing_after is not None:
                return ReplyResult(status="already_replied", comment_id=existing_after)
            raise GitHubConversationTransientError(
                "reply publication failed temporarily"
            ) from exc
        if status == 201 and isinstance(created, dict):
            comment_id = created.get("id")
            if isinstance(comment_id, int):
                return ReplyResult(status="replied", comment_id=comment_id)
        if status == 422 or status == 409 or status == 429 or status >= 500:
            existing_after = self._find_existing(
                token=token,
                repository=repository,
                pull_request=pull_request,
                marker=marker,
                app_slug=app_slug,
                source_kind=source_kind,
            )
            if existing_after is not None:
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

    def _find_existing(
        self,
        *,
        token: str,
        repository: str,
        pull_request: int,
        marker: str,
        app_slug: str,
        source_kind: str = "inline",
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
            payload = self.http.paginate(path=path, token=token)
        except GitHubHTTPTransientError as exc:
            raise GitHubConversationTransientError(
                "reply reconciliation failed temporarily"
            ) from exc
        except GitHubHTTPError as exc:
            raise GitHubConversationError("reply reconciliation failed") from exc
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
                number = comment.get("id")
                if isinstance(number, int):
                    return number
        return None
