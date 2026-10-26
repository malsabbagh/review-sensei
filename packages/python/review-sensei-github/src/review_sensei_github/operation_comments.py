"""GitHub issue-comment port for the operation record and result parts.

Listing uses at most 10 dispatches, adaptive bounded pages and a short final page. A lost POST
is not retried. Readback compares the canonical JSON payload byte for byte.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import time
from typing import Any

from .errors import GitHubHTTPResponseTooLargeError
from .http import GitHubHttp
from .operation_host import (
    _COMMENT_BOUND,
    RESULT_COMMENT_MARKER,
    OperationComment,
    OperationCommentPort,
    OperationWriteUnconfirmed,
    _parse_event_key,
    bounded_comment,
    comment_payload,
    render_marked_comment,
)

MAX_RESULT_PARTS = 32
_MAX_RESULT_BYTES = 1_048_576


def _result_overhead() -> int:
    body = render_marked_comment(
        RESULT_COMMENT_MARKER,
        {
            "count": MAX_RESULT_PARTS,
            "data": "",
            "event_key": f"{'a' * 64}\t{'b' * 64}",
            "index": MAX_RESULT_PARTS - 1,
            "kind": "result",
            "result_sha256": "c" * 64,
        },
    )
    return len(body.encode("utf-8"))


_RESULT_CHUNK = ((_COMMENT_BOUND - _result_overhead()) // 4) * 3


def ensure_result_part_capacity(byte_count: int) -> int:
    """Refuse before a provider call when the result cannot fit the part cap."""

    if (
        isinstance(byte_count, bool)
        or not isinstance(byte_count, int)
        or not 1 <= byte_count <= _MAX_RESULT_BYTES
    ):
        raise ValueError("result size is invalid")
    parts = math.ceil(byte_count / _RESULT_CHUNK)
    if parts > MAX_RESULT_PARTS:
        raise ValueError("result exceeds the part cap")
    return parts


class GitHubIssueCommentOperationPort:
    """Issue comments on one pull request, with a physical request counter."""

    def __init__(
        self,
        http: GitHubHttp,
        *,
        token: str,
        repository: str,
        pull_request: int,
        app_user_id: int,
        strict_association: bool = False,
    ) -> None:
        if (
            isinstance(pull_request, bool)
            or not isinstance(pull_request, int)
            or pull_request <= 0
        ):
            raise ValueError("invalid pull request")
        if (
            isinstance(app_user_id, bool)
            or not isinstance(app_user_id, int)
            or app_user_id <= 0
        ):
            raise ValueError("invalid app user id")
        self._unconfirmed_write: Any = None
        self._http = http
        self._token = token
        self._list_path = http.repository_path(
            repository, f"/issues/{pull_request}/comments"
        )
        self._repository = repository
        self._app_user_id = app_user_id
        self._issue_url = (
            http.api_url.rstrip("/")
            + http.repository_path(repository, f"/issues/{pull_request}")
            if strict_association
            else None
        )
        self._strict_association = strict_association
        self.requests = 0
        self._primed: list[object] | None = None

    def prime(self, comments: list[object]) -> None:
        """Use one comment list already read, instead of reading it again."""

        self._primed = comments

    def list_comments(self) -> tuple[OperationComment, ...]:
        from .caller_operation import http_read_operation

        with http_read_operation(
            self._http, "operation-comments", retained=self._unconfirmed_write
        ):
            return self._list_comments()

    def _list_comments(self) -> tuple[OperationComment, ...]:
        primed = self._primed
        if primed is not None:
            self._primed = None
            return tuple(self._view(item) for item in primed)
        collected: list[OperationComment] = []
        page_sizes = (100, 50, 20, 10, 5, 1)
        size_index = 0
        deadline = time.monotonic() + 60
        for _ in range(10):
            hint = (
                self._http.pagination_page_size(self._list_path, len(collected))
                if isinstance(self._http, GitHubHttp)
                else 100
            )
            size_index = next(
                (
                    index
                    for index in range(size_index, len(page_sizes))
                    if page_sizes[index] <= hint
                    and len(collected) % page_sizes[index] == 0
                ),
                size_index,
            )
            page_size = page_sizes[size_index]
            if len(collected) % page_size:
                raise ValueError("operation comment listing offset conflicts")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("operation comment listing deadline exceeded")
            page = len(collected) // page_size + 1
            try:
                status, payload = self._request(
                    "GET",
                    f"{self._list_path}?per_page={page_size}&page={page}",
                    timeout_seconds=remaining,
                )
            except GitHubHTTPResponseTooLargeError:
                if size_index + 1 == len(page_sizes):
                    raise
                size_index += 1
                while len(collected) % page_sizes[size_index]:
                    size_index += 1
                continue
            if (
                status != 200
                or not isinstance(payload, list)
                or len(payload) > page_size
            ):
                raise ValueError("operation comment listing is unavailable")
            collected.extend(self._view(item) for item in payload)
            identifiers = [item.comment_id for item in collected]
            if len(set(identifiers)) != len(identifiers):
                raise ValueError(
                    "operation comment listing contains duplicate identities"
                )
            if len(payload) < page_size:
                return tuple(collected)
        raise ValueError("operation comment listing exceeded 10 pages or requests")

    def create(self, body: str) -> OperationComment:
        return self._write("create", body, lambda: self._create(body))

    def _write(self, kind: str, body: str, write: Any) -> OperationComment:
        from .caller_operation import http_write_operation

        if self._unconfirmed_write is not None:
            raise OperationWriteUnconfirmed("operation comment write is unresolved")
        bounded_comment(body)
        key = kind + ":" + hashlib.sha256(body.encode("utf-8")).hexdigest()
        with http_write_operation(self._http, key) as allocation:
            try:
                viewed = write()
            except BaseException:
                self._unconfirmed_write = allocation
                raise
            if allocation is not None:
                allocation.confirmed = True
            return viewed

    def _create(self, body: str) -> OperationComment:
        bounded_comment(body)
        status, payload = self._request("POST", self._list_path, body={"body": body})
        accepted = self._accepted_write(status, payload, body, created=True)
        if accepted is not None:
            return accepted
        comment_id = _created_id(status, payload)
        if comment_id is None:
            raise OperationWriteUnconfirmed(
                "operation comment create was not confirmed"
            )
        return self._readback(comment_id, body)

    def update(self, comment_id: int, body: str) -> OperationComment:
        return self._write("update", body, lambda: self._update(comment_id, body))

    def _update(self, comment_id: int, body: str) -> OperationComment:
        bounded_comment(body)
        if (
            isinstance(comment_id, bool)
            or not isinstance(comment_id, int)
            or comment_id <= 0
        ):
            raise ValueError("invalid operation comment id")
        status, payload = self._request(
            "PATCH", self._comment_path(comment_id), body={"body": body}
        )
        accepted = self._accepted_write(status, payload, body, created=False)
        if accepted is not None:
            return accepted
        if status != 200:
            raise OperationWriteUnconfirmed(
                "operation comment update was not confirmed"
            )
        return self._readback(comment_id, body)

    def _accepted_write(
        self,
        status: int,
        payload: object,
        body: str,
        *,
        created: bool,
    ) -> OperationComment | None:
        """Use GitHub's write response as the read-back when it echoes the body."""

        if status != (201 if created else 200) or not isinstance(payload, dict):
            return None
        try:
            viewed = self._view(payload)
        except ValueError:
            return None
        if comment_payload(viewed.body) != comment_payload(body):
            return None
        if viewed.user_id != self._app_user_id or viewed.user_type != "Bot":
            return None
        return viewed

    def _readback(self, comment_id: int, body: str) -> OperationComment:
        status, payload = self._request("GET", self._comment_path(comment_id))
        if status != 200 or not isinstance(payload, dict):
            raise ValueError("operation comment readback failed")
        viewed = self._view(payload)
        if comment_payload(viewed.body) != comment_payload(body):
            raise ValueError("operation comment readback failed")
        if viewed.user_id != self._app_user_id or viewed.user_type != "Bot":
            raise ValueError("operation comment author mismatch")
        return viewed

    def _view(self, payload: object) -> OperationComment:
        if self._strict_association and (
            not isinstance(payload, dict) or payload.get("issue_url") != self._issue_url
        ):
            raise ValueError("operation comment pull request association mismatch")
        return _view(payload)

    def _comment_path(self, comment_id: int) -> str:
        return self._http.repository_path(
            self._repository, f"/issues/comments/{comment_id}"
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[int, object]:
        self.requests += 1
        return self._http.request(
            method, path, token=self._token, body=body, timeout_seconds=timeout_seconds
        )


class ResultPartStore:
    """App-authored parts of one validated review result.

    A prompt or provider transcript is not added here. Callers pass the
    validated result bytes only.
    """

    def __init__(self, port: OperationCommentPort, *, app_user_id: int) -> None:
        if (
            isinstance(app_user_id, bool)
            or not isinstance(app_user_id, int)
            or app_user_id <= 0
        ):
            raise ValueError("invalid app user id")
        self._port = port
        self._app_user_id = app_user_id

    def write(
        self,
        *,
        event_key: str,
        result: bytes,
        _check_existing: bool = True,
    ) -> str:
        if not isinstance(result, bytes) or not result:
            raise ValueError("result is empty")
        _parse_event_key(event_key)
        ensure_result_part_capacity(len(result))
        digest = hashlib.sha256(result).hexdigest()
        existing = self._parts(event_key) if _check_existing else []
        if existing:
            joined = _join(existing, digest)
            if hashlib.sha256(joined).hexdigest() != digest:
                raise ValueError("result part digest mismatch")
            return digest
        count = math.ceil(len(result) / _RESULT_CHUNK)
        for index in range(count):
            chunk = result[index * _RESULT_CHUNK : (index + 1) * _RESULT_CHUNK]
            body = bounded_comment(
                render_marked_comment(
                    RESULT_COMMENT_MARKER,
                    {
                        "count": count,
                        "data": base64.b64encode(chunk).decode("ascii"),
                        "event_key": event_key,
                        "index": index,
                        "kind": "result",
                        "result_sha256": digest,
                    },
                )
            )
            echoed = self._port.create(body)
            if comment_payload(echoed.body) != comment_payload(body):
                raise ValueError("operation comment readback failed")
            if echoed.user_id != self._app_user_id or echoed.user_type != "Bot":
                raise ValueError("operation comment author mismatch")
        return digest

    def read(self, *, event_key: str, payload_digest: str) -> bytes:
        if not isinstance(payload_digest, str) or len(payload_digest) != 64:
            raise ValueError("result digest is invalid")
        _parse_event_key(event_key)
        return _join(self._parts(event_key), payload_digest)

    def read_available(self, *, event_key: str) -> tuple[str, bytes] | None:
        """Read a complete owned payload; its digest alone conveys no authority.

        Publication recovery independently verifies the returned validated result
        against its exact durable transaction and trusted context.
        """

        _parse_event_key(event_key)
        parts = self._parts(event_key)
        if not parts:
            return None
        digest = parts[0].get("result_sha256")
        if not isinstance(digest, str):
            raise ValueError("result digest is invalid")
        return digest, _join(parts, digest)

    def _parts(self, event_key: str) -> list[dict[str, object]]:
        found: list[dict[str, object]] = []
        identities: set[int] = set()
        for comment in self._port.list_comments():
            if RESULT_COMMENT_MARKER not in comment.body:
                continue
            if comment.user_id != self._app_user_id or comment.user_type != "Bot":
                continue
            bounded_comment(comment.body)
            payload = json.loads(comment_payload(comment.body))
            if not isinstance(payload, dict) or payload.get("kind") != "result":
                raise ValueError("result part is invalid")
            if payload.get("event_key") != event_key:
                continue
            if comment.comment_id in identities:
                raise ValueError("result part identity is duplicated")
            identities.add(comment.comment_id)
            found.append(payload)
        return found


def _join(parts: list[dict[str, object]], payload_digest: str) -> bytes:
    if not parts:
        raise ValueError("result part is missing")
    if any(
        type(part.get("count")) is not int
        or not isinstance(part.get("result_sha256"), str)
        for part in parts
    ):
        raise ValueError("result part is invalid")
    counts = {part.get("count") for part in parts}
    digests = {part.get("result_sha256") for part in parts}
    if len(counts) != 1 or len(digests) != 1 or payload_digest not in digests:
        raise ValueError("result part digest mismatch")
    count = counts.pop()
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or not 1 <= count <= MAX_RESULT_PARTS
    ):
        raise ValueError("result part is invalid")
    if len(parts) != count:
        raise ValueError("result part is missing")
    ordered: dict[int, bytes] = {}
    for part in parts:
        if set(part) != {
            "count",
            "data",
            "event_key",
            "index",
            "kind",
            "result_sha256",
        }:
            raise ValueError("result part is invalid")
        index = part.get("index")
        data = part.get("data")
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or not isinstance(data, str)
            or not 0 <= index < count
            or index in ordered
        ):
            raise ValueError("result part is invalid")
        try:
            ordered[index] = base64.b64decode(data.encode("ascii"), validate=True)
        except (ValueError, UnicodeError) as exc:
            raise ValueError("result part is invalid") from exc
    if len(ordered) != count:
        raise ValueError("result part is missing")
    joined = b"".join(ordered[index] for index in range(count))
    ensure_result_part_capacity(len(joined))
    if hashlib.sha256(joined).hexdigest() != payload_digest:
        raise ValueError("result part digest mismatch")
    return joined


def _created_id(status: int, payload: object) -> int | None:
    if status != 201 or not isinstance(payload, dict):
        return None
    comment_id = payload.get("id")
    if (
        isinstance(comment_id, bool)
        or not isinstance(comment_id, int)
        or comment_id <= 0
    ):
        return None
    return comment_id


def _view(payload: object) -> OperationComment:
    if not isinstance(payload, dict):
        raise ValueError("operation comment is invalid")
    comment_id = payload.get("id")
    body = payload.get("body")
    user = payload.get("user")
    if (
        isinstance(comment_id, bool)
        or not isinstance(comment_id, int)
        or comment_id <= 0
        or not isinstance(body, str)
        or not isinstance(user, dict)
    ):
        raise ValueError("operation comment is invalid")
    user_id = user.get("id")
    user_type = user.get("type")
    if (
        isinstance(user_id, bool)
        or not isinstance(user_id, int)
        or user_id <= 0
        or not isinstance(user_type, str)
    ):
        raise ValueError("operation comment author is missing")
    return OperationComment(
        comment_id=comment_id,
        body=body,
        user_id=user_id,
        user_type=user_type,
    )
