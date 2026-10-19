"""Author fence, readback, and result-part comments for the operation record."""

from __future__ import annotations

import hashlib
import unittest
from urllib.parse import parse_qs, urlparse

from review_sensei_github.operation_comments import (
    MAX_RESULT_PARTS,
    GitHubIssueCommentOperationPort,
    ResultPartStore,
    ensure_result_part_capacity,
)
from review_sensei_github.operation_host import (
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
            # A mismatched echo is not a read-back. The port then GETs, and
            # that GET returns the same mismatched body.
            if self.readback_body is not None:
                return 201, {**stored, "body": self.readback_body}
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

    def test_real_oversized_pages_reduce_size_without_losing_items_or_refilling_budget(
        self,
    ):
        from review_sensei_github.http import GitHubHttp
        from review_sensei_github.publication_recovery import publication_http

        from review_sensei.bounded_evidence import EvidenceReadBudget
        from tests.fake_github_http import json_response

        comments = [
            {
                "id": index + 1,
                "body": str(index) + "x" * 40_000,
                "user": {"id": 42, "type": "Bot"},
            }
            for index in range(17)
        ]
        calls = []

        def open_page(request, timeout):
            query = parse_qs(urlparse(request.full_url).query)
            size, page = int(query["per_page"][0]), int(query["page"][0])
            calls.append((size, page, timeout))
            offset = (page - 1) * size
            return json_response(comments[offset : offset + size])

        budget = EvidenceReadBudget()
        port = GitHubIssueCommentOperationPort(
            publication_http(GitHubHttp(opener=open_page), budget),
            token="synthetic",
            repository="acme/widgets",
            pull_request=7,
            app_user_id=42,
        )
        observed = port.list_comments()
        self.assertEqual([item.comment_id for item in observed], list(range(1, 18)))
        self.assertEqual(
            [item.body for item in observed], [item["body"] for item in comments]
        )
        self.assertEqual(
            [item[:2] for item in calls],
            [(100, 1), (25, 1), (5, 1), (5, 2), (5, 3), (5, 4)],
        )
        self.assertEqual(port.requests, budget.calls)
        self.assertEqual(port.requests, len(calls))
        self.assertTrue(all(0 < timeout <= 60 for _, _, timeout in calls))
        # A new scan must see new data while avoiding already disproved sizes.
        # The hint survives the reply wrapper without bypassing its own gate.
        from review_sensei_github.reply_budget import ReplyPublicationHttp

        comments.append(
            {"id": 18, "body": "new item", "user": {"id": 42, "type": "Bot"}}
        )
        wrapped = ReplyPublicationHttp(port._http)
        second = wrapped.paginate(
            path="/repos/acme/widgets/issues/7/comments",
            token="synthetic",
            page_sizes=(100, 50, 25, 5, 1),
            max_requests=10,
        )
        self.assertEqual([item["id"] for item in second], list(range(1, 19)))
        self.assertEqual([item[0] for item in calls[6:]], [5, 5, 5, 5])
        self.assertEqual(wrapped.requests, 4)
        self.assertEqual(budget.calls, len(calls))

    def test_oversized_individual_item_refuses_at_one_item_without_partial_inventory(
        self,
    ):
        from review_sensei_github.errors import GitHubHTTPResponseTooLargeError
        from review_sensei_github.http import GitHubHttp

        from tests.fake_github_http import json_response

        calls = []

        def open_page(request, timeout):
            calls.append(request.full_url)
            return json_response(
                [{"id": 1, "body": "x" * 600_000, "user": {"id": 42, "type": "Bot"}}]
            )

        port = GitHubIssueCommentOperationPort(
            GitHubHttp(opener=open_page),
            token="synthetic",
            repository="acme/widgets",
            pull_request=7,
            app_user_id=42,
        )
        with self.assertRaises(GitHubHTTPResponseTooLargeError):
            port.list_comments()
        self.assertEqual(len(calls), 4)
        self.assertIn("per_page=1&", calls[-1])

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
        from review_sensei_github.operation_host import render_marked_comment

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
        import review_sensei_github.operation_comments as parts

        original = parts._RESULT_CHUNK
        parts._RESULT_CHUNK = 10
        try:
            with self.assertRaisesRegex(ValueError, "part cap"):
                parts.ensure_result_part_capacity(MAX_RESULT_PARTS * 10 + 1)
        finally:
            parts._RESULT_CHUNK = original
