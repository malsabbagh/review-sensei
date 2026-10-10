"""GitHub issue-comment port for the operation record and result parts.

Listing stops at 10 pages of 100 and requires a short final page. A lost POST
is not retried. Readback compares the canonical JSON payload byte for byte.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math

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
        self._http = http
        self._token = token
        self._list_path = http.repository_path(
            repository, f"/issues/{pull_request}/comments"
        )
        self._repository = repository
        self._app_user_id = app_user_id
        self.requests = 0

    def list_comments(self) -> tuple[OperationComment, ...]:
        collected: list[OperationComment] = []
        for page in range(1, 11):
            status, payload = self._request(
                "GET", f"{self._list_path}?per_page=100&page={page}"
            )
            if status != 200 or not isinstance(payload, list) or len(payload) > 100:
                raise ValueError("operation comment listing is unavailable")
            collected.extend(_view(item) for item in payload)
            if len(payload) < 100:
                return tuple(collected)
        raise ValueError("operation comment listing exceeded 10 pages")

    def create(self, body: str) -> OperationComment:
        bounded_comment(body)
        status, payload = self._request("POST", self._list_path, body={"body": body})
        comment_id = _created_id(status, payload)
        if comment_id is None:
            raise OperationWriteUnconfirmed(
                "operation comment create was not confirmed"
            )
        return self._readback(comment_id, body)

    def update(self, comment_id: int, body: str) -> OperationComment:
        bounded_comment(body)
        if (
            isinstance(comment_id, bool)
            or not isinstance(comment_id, int)
            or comment_id <= 0
        ):
            raise ValueError("invalid operation comment id")
        status, _payload = self._request(
            "PATCH", self._comment_path(comment_id), body={"body": body}
        )
        if status != 200:
            raise OperationWriteUnconfirmed(
                "operation comment update was not confirmed"
            )
        return self._readback(comment_id, body)

    def _readback(self, comment_id: int, body: str) -> OperationComment:
        status, payload = self._request("GET", self._comment_path(comment_id))
        if status != 200 or not isinstance(payload, dict):
            raise ValueError("operation comment readback failed")
        viewed = _view(payload)
        if comment_payload(viewed.body) != comment_payload(body):
            raise ValueError("operation comment readback failed")
        if viewed.user_id != self._app_user_id or viewed.user_type != "Bot":
            raise ValueError("operation comment author mismatch")
        return viewed

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
    ) -> tuple[int, object]:
        self.requests += 1
        return self._http.request(method, path, token=self._token, body=body)


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

    def write(self, *, event_key: str, result: bytes) -> str:
        if not isinstance(result, bytes) or not result:
            raise ValueError("result is empty")
        _parse_event_key(event_key)
        ensure_result_part_capacity(len(result))
        digest = hashlib.sha256(result).hexdigest()
        existing = self._parts(event_key)
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

    def _parts(self, event_key: str) -> list[dict[str, object]]:
        found: list[dict[str, object]] = []
        for comment in self._port.list_comments():
            if RESULT_COMMENT_MARKER not in comment.body:
                continue
            if comment.user_id != self._app_user_id or comment.user_type != "Bot":
                continue
            payload = json.loads(comment_payload(comment.body))
            if not isinstance(payload, dict) or payload.get("kind") != "result":
                raise ValueError("result part is invalid")
            if payload.get("event_key") != event_key:
                continue
            found.append(payload)
        return found


def _join(parts: list[dict[str, object]], payload_digest: str) -> bytes:
    if not parts:
        raise ValueError("result part is missing")
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
