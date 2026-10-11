"""Author fence, readback, and result-part comments for the operation record."""

from __future__ import annotations

import hashlib
import unittest

from review_sensei.hosting.github.operation_comments import (
    MAX_RESULT_PARTS,
    GitHubIssueCommentOperationPort,
    ResultPartStore,
    ensure_result_part_capacity,
)
from review_sensei.hosting.github.operation_host import (
    RESULT_COMMENT_MARKER,
    OperationComment,
    OperationWriteUnconfirmed,
    bounded_comment,
)


def _event_key() -> str:
    return f"{'a' * 64}\t{'b' * 64}"


class FakeHttp:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.pages: dict[int, list[dict[str, object]]] = {1: []}
        self.created: list[dict[str, object]] = []
        self.next_id = 1
        self.lose_post = False
        self.readback_body: str | None = None

    def repository_path(self, repository: str, suffix: str = "") -> str:
        return f"/repos/{repository}{suffix}"

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str,
        body: dict[str, object] | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[int, dict[str, object] | list[object] | None]:
        del token, timeout_seconds
        self.calls.append(method)
        if method == "GET" and "per_page=100" in path:
            page = int(path.rsplit("page=", 1)[1])
            return 200, list(self.pages.get(page, []))
        if method == "POST":
            if self.lose_post:
                return 500, None
            comment_id = self.next_id
            self.next_id += 1
            assert body is not None
            stored = {
                "id": comment_id,
                "body": body["body"],
                "user": {"id": 42, "type": "Bot"},
            }
            self.created.append(stored)
            return 201, stored
        if method == "GET":
            comment_id = int(path.rstrip("/").rsplit("/", 1)[1])
            stored = next(item for item in self.created if item["id"] == comment_id)
            if self.readback_body is not None:
                return 200, {**stored, "body": self.readback_body}
            return 200, stored
        if method == "PATCH":
            assert body is not None
            comment_id = int(path.rstrip("/").rsplit("/", 1)[1])
            stored = next(item for item in self.created if item["id"] == comment_id)
            stored["body"] = body["body"]
            return 200, stored
        raise AssertionError(method)


class PortTests(unittest.TestCase):
    def test_listing_requires_a_short_page(self):
        http = FakeHttp()
        http.pages = {
            page: [
                {
                    "id": page * 100 + offset,
                    "body": "",
                    "user": {"id": 42, "type": "Bot"},
                }
                for offset in range(100)
            ]
            for page in range(1, 11)
        }
        port = GitHubIssueCommentOperationPort(
            http,
            token="token",
            repository="acme/widgets",
            pull_request=7,
            app_user_id=42,
        )
        with self.assertRaisesRegex(ValueError, "exceeded 10 pages"):
            port.list_comments()
        self.assertEqual(http.calls, ["GET"] * 10)

    def test_a_lost_post_is_not_retried(self):
        http = FakeHttp()
        http.lose_post = True
        port = GitHubIssueCommentOperationPort(
            http,
            token="token",
            repository="acme/widgets",
            pull_request=7,
            app_user_id=42,
        )
        with self.assertRaises(OperationWriteUnconfirmed):
            port.create("<!-- review-sensei-operation -->\n```json\n{}\n```\n")
        self.assertEqual(http.calls, ["POST"])

    def test_readback_mismatch_refuses(self):
        http = FakeHttp()
        http.readback_body = (
            '<!-- review-sensei-operation -->\n```json\n{"no":1}\n```\n'
        )
        port = GitHubIssueCommentOperationPort(
            http,
            token="token",
            repository="acme/widgets",
            pull_request=7,
            app_user_id=42,
        )
        body = "<!-- review-sensei-operation -->\n```json\n{}\n```\n"
        with self.assertRaisesRegex(ValueError, "readback failed"):
            port.create(body)
        self.assertEqual(http.calls, ["POST", "GET"])

    def test_oversize_comment_is_refused_before_post(self):
        http = FakeHttp()
        port = GitHubIssueCommentOperationPort(
            http,
            token="token",
            repository="acme/widgets",
            pull_request=7,
            app_user_id=42,
        )
        with self.assertRaisesRegex(ValueError, "exceeds the GitHub bound"):
            port.create("x" * 60_001)
        self.assertEqual(http.calls, [])
        with self.assertRaisesRegex(ValueError, "exceeds the GitHub bound"):
            bounded_comment("x" * 60_001)


class ResultPartTests(unittest.TestCase):
    def test_parts_round_trip_and_reject_a_prompt(self):
        comments: dict[int, OperationComment] = {}
        next_id = {"n": 1}

        class Port:
            requests = 0

            def list_comments(self) -> tuple[OperationComment, ...]:
                return tuple(comments.values())

            def create(self, body: str) -> OperationComment:
                comment = OperationComment(next_id["n"], body, 42, "Bot")
                comments[comment.comment_id] = comment
                next_id["n"] += 1
                return comment

            def update(self, comment_id: int, body: str) -> OperationComment:
                raise AssertionError("result parts are not updated")

        store = ResultPartStore(Port(), app_user_id=42)
        result = b"validated-result"
        digest = store.write(event_key=_event_key(), result=result)
        self.assertEqual(digest, hashlib.sha256(result).hexdigest())
        joined = "\n".join(comment.body for comment in comments.values())
        self.assertNotIn("SECRET PROMPT", joined)
        self.assertIn(RESULT_COMMENT_MARKER, joined)
        reloaded = ResultPartStore(Port(), app_user_id=42)
        self.assertEqual(
            reloaded.read(event_key=_event_key(), payload_digest=digest),
            result,
        )

    def test_a_foreign_part_is_ignored_and_a_duplicate_refuses(self):
        body_result = b"validated-result"
        digest = hashlib.sha256(body_result).hexdigest()
        from review_sensei.hosting.github.operation_host import render_marked_comment

        part = render_marked_comment(
            RESULT_COMMENT_MARKER,
            {
                "count": 1,
                "data": "dmFsaWRhdGVkLXJlc3VsdA==",
                "event_key": _event_key(),
                "index": 0,
                "kind": "result",
                "result_sha256": digest,
            },
        )
        comments = [
            OperationComment(1, part, 99, "User"),
            OperationComment(2, part, 42, "Bot"),
            OperationComment(3, part, 42, "Bot"),
        ]

        class Port:
            requests = 0

            def list_comments(self) -> tuple[OperationComment, ...]:
                return tuple(comments)

            def create(self, body: str) -> OperationComment:
                raise AssertionError(body)

            def update(self, comment_id: int, body: str) -> OperationComment:
                raise AssertionError(body)

        store = ResultPartStore(Port(), app_user_id=42)
        with self.assertRaisesRegex(ValueError, "result part"):
            store.read(event_key=_event_key(), payload_digest=digest)

    def test_capacity_refuses_before_a_provider_call(self):
        self.assertLessEqual(ensure_result_part_capacity(1_048_576), MAX_RESULT_PARTS)
        with self.assertRaisesRegex(ValueError, "result size is invalid"):
            ensure_result_part_capacity(1_048_577)
        import review_sensei.hosting.github.operation_comments as parts

        original = parts._RESULT_CHUNK
        parts._RESULT_CHUNK = 10
        try:
            with self.assertRaisesRegex(ValueError, "part cap"):
                parts.ensure_result_part_capacity(MAX_RESULT_PARTS * 10 + 1)
        finally:
            parts._RESULT_CHUNK = original
