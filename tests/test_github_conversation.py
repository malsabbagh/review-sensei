import base64
import json
import unittest
from urllib.error import URLError

from review_sensei.conversation import ConversationService
from review_sensei.hosting.github import (
    ConversationPublisher,
    GitHubConversationError,
    GitHubConversationTransientError,
    GitHubPublicationError,
)
from review_sensei.hosting.github.conversation import (
    CONVERSATION_COMMENT_PAGE_SIZES,
    MAX_CONTEXT_DIFF_BYTES,
    PreparedConversation,
    _bounded_text,
    _remove_emitted_priority_hunks,
    authorized_human_comment,
    has_standalone_sensei_mention,
    reply_marker,
)
from review_sensei.models import ConversationReply

try:
    from fake_github_http import FakeHTTPResponse, json_response, make_http
except ModuleNotFoundError:
    from tests.fake_github_http import FakeHTTPResponse, json_response, make_http

INLINE_URL = "https://api.github.test/repos/owner/repo/pulls/1"
ISSUE_URL = "https://api.github.test/repos/owner/repo/issues/1"


def pr_payload(head_sha, *, fork=False, state="open", draft=False):
    return {
        "state": state,
        "draft": draft,
        "user": {"login": "alice", "type": "User"},
        "title": "Bounded PR title",
        "body": "Bounded PR body",
        "head": {
            "sha": head_sha,
            "ref": "feature/context",
            "repo": {"full_name": "owner/repo", "fork": fork},
        },
        "base": {
            "sha": "a" * 40,
            "ref": "main",
            "repo": {"id": 1, "full_name": "owner/repo", "fork": False},
        },
    }


class ConversationPublisherTests(unittest.TestCase):
    def test_processing_reaction_uses_source_comment_endpoint_and_is_removed(self):
        for source_kind, endpoint in (
            ("inline", "/pulls/comments/10/reactions"),
            ("issue", "/issues/comments/10/reactions"),
        ):
            with self.subTest(source_kind=source_kind):
                http, calls = make_http(
                    [json_response({"id": 99}, 201), FakeHTTPResponse(b"", 204)]
                )
                publisher = ConversationPublisher(http=http)
                reaction = publisher.add_processing_reaction(
                    token="token",
                    repository="owner/repo",
                    source_comment_id=10,
                    source_kind=source_kind,
                )
                publisher.remove_processing_reaction(
                    token="token",
                    repository="owner/repo",
                    source_comment_id=10,
                    source_kind=source_kind,
                    reaction_id=reaction.reaction_id,
                )

                self.assertEqual([call[0] for call in calls], ["POST", "DELETE"])
                self.assertTrue(calls[0][1].endswith(endpoint))
                self.assertEqual(
                    __import__("json").loads(calls[0][2].decode("utf-8")),
                    {"content": "eyes"},
                )
                self.assertTrue(calls[1][1].endswith(f"{endpoint}/99"))

    def test_processing_reaction_reuses_existing_and_tolerates_missing_cleanup(self):
        http, calls = make_http(
            [json_response({"id": 99}, 200), json_response({}, 404)]
        )
        publisher = ConversationPublisher(http=http)
        reaction = publisher.add_processing_reaction(
            token="token",
            repository="owner/repo",
            source_comment_id=10,
            source_kind="issue",
        )
        publisher.remove_processing_reaction(
            token="token",
            repository="owner/repo",
            source_comment_id=10,
            source_kind="issue",
            reaction_id=reaction.reaction_id,
        )
        self.assertEqual(len(calls), 2)

    def test_processing_reaction_rejects_invalid_inputs_before_request(self):
        http, calls = make_http([])
        publisher = ConversationPublisher(http=http)
        for source_kind, source_comment_id in (("unknown", 1), ("issue", 0)):
            with (
                self.subTest(
                    source_kind=source_kind,
                    source_comment_id=source_comment_id,
                ),
                self.assertRaises(GitHubConversationError),
            ):
                publisher.add_processing_reaction(
                    token="token",
                    repository="owner/repo",
                    source_comment_id=source_comment_id,
                    source_kind=source_kind,
                )
        with self.assertRaises(GitHubConversationError):
            publisher.remove_processing_reaction(
                token="token",
                repository="owner/repo",
                source_comment_id=1,
                source_kind="inline",
                reaction_id=0,
            )
        self.assertEqual(calls, [])

    def test_processing_reaction_maps_github_failures(self):
        add_cases = (
            (json_response({}, 404), GitHubConversationError),
            (json_response({}, 403), GitHubConversationError),
            (json_response({}, 429), GitHubConversationTransientError),
            (json_response({}, 422), GitHubConversationError),
            (URLError("timed out"), GitHubConversationTransientError),
            (json_response({"id": True}, 201), GitHubConversationError),
        )
        for response, error in add_cases:
            with self.subTest(operation="add", error=error.__name__):
                http, _ = make_http(response)
                with self.assertRaises(error):
                    ConversationPublisher(http=http).add_processing_reaction(
                        token="token",
                        repository="owner/repo",
                        source_comment_id=10,
                        source_kind="inline",
                    )

        remove_cases = (
            (json_response({}, 403), GitHubConversationError),
            (json_response({}, 500), GitHubConversationTransientError),
            (json_response({}, 422), GitHubConversationError),
            (URLError("timed out"), GitHubConversationTransientError),
        )
        for response, error in remove_cases:
            with self.subTest(operation="remove", error=error.__name__):
                http, _ = make_http(response)
                with self.assertRaises(error):
                    ConversationPublisher(http=http).remove_processing_reaction(
                        token="token",
                        repository="owner/repo",
                        source_comment_id=10,
                        source_kind="inline",
                        reaction_id=99,
                    )

    def test_prepare_issue_context_uses_bounded_issue_thread(self):
        updated = "2026-08-19T00:00:00Z"
        head = "b" * 40
        source = {
            "id": 10,
            "issue_url": ISSUE_URL,
            "body": "@sensei please explain",
            "user": {"login": "alice", "type": "User"},
            "author_association": "OWNER",
            "updated_at": updated,
            "created_at": updated,
        }
        responses = [
            json_response(source),
            json_response(pr_payload(head)),
            json_response([source, {"id": 11, "body": "context"}]),
            json_response([]),
            json_response([]),
            json_response({"message": "not found"}, 404),
        ]
        http, calls = make_http(responses)
        prepared = ConversationPublisher(http=http).prepare_context(
            token="token",
            repository="owner/repo",
            pull_request=1,
            source_comment_id=10,
            source_updated_at=updated,
            expected_head_sha=head,
            app_slug="review-sensei[bot]",
            source_kind="issue",
        )
        self.assertIsInstance(prepared, PreparedConversation)
        self.assertEqual(prepared.source_kind, "issue")
        self.assertEqual(len(prepared.context.messages), 2)
        self.assertEqual(len(calls), 6)

    def test_prepare_issue_context_prioritizes_current_head_finding_hunks(self):
        updated = "2026-08-19T00:00:00Z"
        head = "b" * 40
        source = {
            "id": 10,
            "issue_url": ISSUE_URL,
            "body": "@sensei verify the remaining finding",
            "user": {"login": "alice", "type": "User"},
            "author_association": "OWNER",
            "updated_at": updated,
            "created_at": updated,
        }
        target_hunk = "@@ -40,2 +40,4 @@\n-old\n+new\n+guard\n+return"
        current_finding = {
            "id": 11,
            "body": "Current exact-head finding",
            "user": {"login": "review-sensei[bot]", "type": "Bot"},
            "created_at": updated,
            "commit_id": head,
            "path": "src/target.py",
            "line": 42,
            "diff_hunk": target_hunk,
        }
        stale_finding = {
            "id": 12,
            "body": "Stale finding",
            "user": {"login": "review-sensei[bot]", "type": "Bot"},
            "created_at": updated,
            "commit_id": "c" * 40,
            "path": "src/stale.py",
            "line": 7,
            "diff_hunk": "@@ -1 +1 @@\n-old-stale\n+new-stale",
        }
        responses = [
            json_response(source),
            json_response(pr_payload(head)),
            json_response([source]),
            json_response([current_finding, stale_finding]),
            json_response(
                [
                    {
                        "filename": "docs/large.md",
                        "patch": "x" * (13 * 1024),
                    },
                    {
                        "filename": "src/target.py",
                        "patch": "@@ -40,2 +40,4 @@\n-old\n+new\n+guard\n+return",
                    },
                ]
            ),
            json_response({"message": "not found"}, 404),
        ]
        http, calls = make_http(responses)

        prepared = ConversationPublisher(http=http).prepare_context(
            token="token",
            repository="owner/repo",
            pull_request=1,
            source_comment_id=10,
            source_updated_at=updated,
            expected_head_sha=head,
            app_slug="review-sensei[bot]",
            source_kind="issue",
        )

        self.assertIsInstance(prepared, PreparedConversation)
        self.assertTrue(prepared.context.diff_context.startswith("path=src/target.py"))
        self.assertIn("path=src/target.py", prepared.context.diff_context)
        self.assertIn(target_hunk, prepared.context.diff_context)
        self.assertNotIn("new-stale", prepared.context.diff_context)
        self.assertEqual(len(calls), 6)

    def test_issue_diff_context_keeps_uncovered_hunks_and_deduplicates_priority(self):
        target_hunk = "@@ -40,2 +40,4 @@\n-old\n+new\n+guard\n+return"
        helper_hunk = "@@ -100,1 +100,2 @@\n+helper\n+return helper()"
        target_patch = (
            "diff --git a/src/target.py b/src/target.py\n"
            "index abc..def 100644\n"
            "--- a/src/target.py\n+++ b/src/target.py\n"
            "@@ -38,8 +38,10 @@\n context-before\n"
            "-old\n+new\n+guard\n+return\n context-after"
        )
        http, calls = make_http(
            [
                json_response(
                    [
                        {
                            "filename": "src/target.py",
                            "patch": f"{target_patch}\n{helper_hunk}",
                        }
                    ]
                )
            ]
        )

        diff_context, changed_paths = ConversationPublisher(
            http=http
        )._load_diff_context(
            token="token",
            repository="owner/repo",
            pull_request=1,
            source={},
            source_kind="issue",
            priority_hunks=[
                ("src/target.py", target_hunk),
                ("src/target.py", target_hunk),
            ],
        )

        self.assertIsNotNone(diff_context)
        self.assertEqual(diff_context.count(target_hunk), 1)
        self.assertIn(helper_hunk, diff_context)
        self.assertNotIn("context-before", diff_context)
        self.assertIn("--- a/src/target.py", diff_context)
        self.assertIn("+++ b/src/target.py", diff_context)
        self.assertEqual(changed_paths, ("src/target.py",))
        self.assertEqual(len(calls), 1)

    def test_remove_emitted_priority_hunks_keeps_preamble_and_disjoint_hunks(self):
        priority_hunk = "@@ -40,2 +40,4 @@\n-old\n+new\n+guard\n+return"
        patch = (
            "diff --git a/src/target.py b/src/target.py\n"
            "index abc..def 100644\n"
            "--- a/src/target.py\n+++ b/src/target.py\n"
            "@@ -38,8 +38,10 @@\n context-before\n"
            "-old\n+new\n+guard\n+return\n context-after\n"
            "@@ -100,1 +100,2 @@\n+helper\n+return helper()\n"
        )

        residual = _remove_emitted_priority_hunks(
            patch,
            {priority_hunk},
            filename="src/target.py",
        )

        self.assertTrue(residual.startswith("diff --git a/src/target.py"))
        self.assertIn("--- a/src/target.py", residual)
        self.assertIn("@@ -100,1 +100,2 @@", residual)
        self.assertNotIn("context-before", residual)

    def test_remove_emitted_priority_hunks_reconstructs_headers_without_preamble(self):
        priority_hunk = "@@ -40,2 +40,4 @@\n-old\n+new\n+guard\n+return"
        patch = (
            "@@ -38,8 +38,10 @@\n context-before\n"
            "-old\n+new\n+guard\n+return\n context-after\n"
            "@@ -100,1 +100,2 @@\n+helper\n+return helper()\n"
        )

        residual = _remove_emitted_priority_hunks(
            patch,
            {priority_hunk},
            filename="src/target.py",
        )

        self.assertTrue(residual.startswith("--- a/src/target.py\n+++ b/src/target.py"))
        self.assertIn("@@ -100,1 +100,2 @@", residual)
        self.assertNotIn("context-before", residual)

    def test_bounded_text_never_exceeds_small_byte_budget(self):
        for maximum in range(1, len(b"\n[truncated]")):
            bounded = _bounded_text("é" * 100, maximum)
            self.assertTrue(bounded is None or len(bounded.encode("utf-8")) <= maximum)

    def test_general_diff_truncation_stays_within_byte_budget(self):
        http, _ = make_http(
            [
                json_response(
                    [
                        {
                            "filename": "src/large.py",
                            "patch": "@@ -1 +1 @@\n" + "é" * MAX_CONTEXT_DIFF_BYTES,
                        }
                    ]
                )
            ]
        )

        diff_context, _ = ConversationPublisher(http=http)._load_diff_context(
            token="token",
            repository="owner/repo",
            pull_request=1,
            source={},
            source_kind="issue",
        )

        self.assertIsNotNone(diff_context)
        self.assertLessEqual(len(diff_context.encode("utf-8")), MAX_CONTEXT_DIFF_BYTES)
        self.assertIn("[truncated]", diff_context)

    def test_oversized_priority_hunk_does_not_starve_later_context(
        self,
    ):
        http, _ = make_http(
            [
                json_response(
                    [
                        {
                            "filename": "src/target.py",
                            "patch": "@@ -1 +1 @@\n-old\n+new",
                        },
                        {
                            "filename": "src/other.py",
                            "patch": "@@ -1 +1 @@\n-old\n+new",
                        },
                    ]
                )
            ]
        )
        oversized_hunk = "@@ -1 +1 @@\n" + "x" * (MAX_CONTEXT_DIFF_BYTES * 2)
        target_hunk = "@@ -1 +1 @@\n-old\n+new"

        diff_context, _ = ConversationPublisher(http=http)._load_diff_context(
            token="token",
            repository="owner/repo",
            pull_request=1,
            source={},
            source_kind="issue",
            priority_hunks=[
                ("src/oversized.py", oversized_hunk),
                ("src/target.py", target_hunk),
            ],
        )

        self.assertIsNotNone(diff_context)
        self.assertLessEqual(len(diff_context.encode("utf-8")), MAX_CONTEXT_DIFF_BYTES)
        self.assertTrue(diff_context.startswith("path=src/target.py"))
        self.assertIn("path=src/other.py", diff_context)
        self.assertNotIn("path=src/oversized.py", diff_context)

    def test_prioritized_finding_hunks_sort_current_comments_by_created_at(self):
        head = "b" * 40
        comments = [
            {
                "user": {"login": "review-sensei[bot]"},
                "commit_id": head,
                "created_at": "2026-08-19T00:02:00Z",
                "path": "src/newer.py",
                "diff_hunk": "@@ -2 +2 @@\n+newer",
            },
            {
                "user": {"login": "review-sensei[bot]"},
                "commit_id": head,
                "created_at": "2026-08-19T00:01:00Z",
                "path": "src/older.py",
                "diff_hunk": "@@ -1 +1 @@\n+older",
            },
        ]

        hunks = ConversationPublisher._prioritized_finding_hunks(
            comments=comments,
            app_slug="review-sensei[bot]",
            head_sha=head,
        )

        self.assertEqual(hunks[0][0], "src/newer.py")
        self.assertEqual(hunks[1][0], "src/older.py")

    def test_prioritized_finding_hunks_skip_invalid_paths(self):
        head = "b" * 40
        comments = [
            {
                "user": {"login": "review-sensei[bot]"},
                "commit_id": head,
                "created_at": "2026-08-19T00:02:00Z",
                "path": "../invalid.py",
                "diff_hunk": "@@ -1 +1 @@\n+invalid",
            },
            {
                "user": {"login": "review-sensei[bot]"},
                "commit_id": head,
                "created_at": "2026-08-19T00:01:00Z",
                "path": "src/valid.py",
                "diff_hunk": "@@ -1 +1 @@\n+valid",
            },
        ]

        hunks = ConversationPublisher._prioritized_finding_hunks(
            comments=comments,
            app_slug="review-sensei[bot]",
            head_sha=head,
        )

        self.assertEqual(hunks, (("src/valid.py", "@@ -1 +1 @@\n+valid"),))

    def test_prepare_inline_child_resolves_and_validates_root(self):
        updated = "2026-08-19T00:00:00Z"
        head = "b" * 40
        source = {
            "id": 10,
            "pull_request_url": INLINE_URL,
            "body": "@sensei please explain",
            "user": {"login": "alice", "type": "User"},
            "author_association": "MEMBER",
            "updated_at": updated,
            "created_at": updated,
            "in_reply_to_id": 9,
            "path": "src/app.py",
            "diff_hunk": "@@ -1 +1 @@\n-old\n+new",
        }
        root = {
            "id": 9,
            "pull_request_url": INLINE_URL,
            "body": "Original comment",
            "user": {"login": "alice", "type": "User"},
            "created_at": updated,
            "in_reply_to_id": None,
        }
        responses = [
            json_response(source),
            json_response(pr_payload(head)),
            json_response(root),
            json_response([root, source]),
            json_response({"message": "not found"}, 404),
        ]
        http, calls = make_http(responses)
        prepared = ConversationPublisher(http=http).prepare_context(
            token="token",
            repository="owner/repo",
            pull_request=1,
            source_comment_id=10,
            source_updated_at=updated,
            expected_head_sha=head,
            app_slug="review-sensei[bot]",
            source_kind="inline",
        )
        self.assertIsInstance(prepared, PreparedConversation)
        self.assertEqual(prepared.root_comment_id, 9)
        self.assertEqual(len(prepared.context.messages), 2)
        self.assertEqual(len(calls), 5)

    def test_prepare_context_authorizes_before_provider_and_bounds_thread(self):
        updated = "2026-08-19T00:00:00Z"
        head = "b" * 40
        responses = [
            json_response(
                {
                    "id": 10,
                    "pull_request_url": INLINE_URL,
                    "body": "please @sensei explain",
                    "user": {"login": "alice", "type": "User"},
                    "author_association": "MEMBER",
                    "updated_at": updated,
                    "created_at": updated,
                    "in_reply_to_id": None,
                    "path": "src/app.py",
                    "diff_hunk": "@@ -1 +1 @@\n-old\n+new",
                }
            ),
            json_response(pr_payload(head)),
            json_response(
                [
                    {
                        "id": 10,
                        "pull_request_url": INLINE_URL,
                        "body": "please @sensei explain",
                        "user": {"login": "alice", "type": "User"},
                        "created_at": updated,
                        "in_reply_to_id": None,
                    },
                    {
                        "id": 11,
                        "body": "Earlier context",
                        "user": {"login": "bob", "type": "User"},
                        "created_at": updated,
                        "in_reply_to_id": 10,
                    },
                ]
            ),
            json_response({"message": "not found"}, 404),
        ]
        http, calls = make_http(responses)
        prepared = ConversationPublisher(http=http).prepare_context(
            token="token",
            repository="owner/repo",
            pull_request=1,
            source_comment_id=10,
            source_updated_at=updated,
            app_slug="review-sensei[bot]",
            source_kind="inline",
        )
        self.assertIsInstance(prepared, PreparedConversation)
        self.assertEqual(prepared.head_sha, head)
        self.assertEqual(prepared.root_comment_id, 10)
        self.assertEqual(len(prepared.context.messages), 2)
        self.assertEqual(len(calls), 4)

    def test_prepare_context_rejects_comment_from_another_pull_request(self):
        updated = "2026-08-19T00:00:00Z"
        http, calls = make_http(
            json_response(
                {
                    "id": 10,
                    "pull_request_url": (
                        "https://api.github.test/repos/owner/repo/pulls/2"
                    ),
                    "body": "@sensei please explain",
                    "user": {"login": "alice", "type": "User"},
                    "author_association": "MEMBER",
                    "updated_at": updated,
                }
            )
        )

        with self.assertRaises(GitHubConversationError):
            ConversationPublisher(http=http).prepare_context(
                token="token",
                repository="owner/repo",
                pull_request=1,
                source_comment_id=10,
                source_updated_at=updated,
                app_slug="review-sensei[bot]",
                source_kind="inline",
            )

        self.assertEqual(len(calls), 1)

    def test_prepare_context_rejects_root_from_another_pull_request(self):
        updated = "2026-08-19T00:00:00Z"
        head = "b" * 40
        source = {
            "id": 10,
            "pull_request_url": INLINE_URL,
            "body": "@sensei please explain",
            "user": {"login": "alice", "type": "User"},
            "author_association": "MEMBER",
            "updated_at": updated,
            "created_at": updated,
            "in_reply_to_id": 9,
        }
        root = {
            "id": 9,
            "pull_request_url": "https://api.github.test/repos/owner/repo/pulls/2",
            "body": "Original comment",
            "user": {"login": "alice", "type": "User"},
            "created_at": updated,
            "in_reply_to_id": None,
        }
        http, calls = make_http(
            [
                json_response(source),
                json_response(pr_payload(head)),
                json_response(root),
            ]
        )

        with self.assertRaises(GitHubConversationError):
            ConversationPublisher(http=http).prepare_context(
                token="token",
                repository="owner/repo",
                pull_request=1,
                source_comment_id=10,
                source_updated_at=updated,
                expected_head_sha=head,
                app_slug="review-sensei[bot]",
                source_kind="inline",
            )

        self.assertEqual(len(calls), 3)

    def test_prepare_context_rejects_caller_selected_inline_root(self):
        updated = "2026-08-19T00:00:00Z"
        head = "b" * 40
        source = {
            "id": 10,
            "pull_request_url": INLINE_URL,
            "body": "@sensei please explain",
            "user": {"login": "alice", "type": "User"},
            "author_association": "MEMBER",
            "updated_at": updated,
            "created_at": updated,
            "in_reply_to_id": 9,
        }
        http, calls = make_http(
            [json_response(source), json_response(pr_payload(head))]
        )

        with self.assertRaises(GitHubConversationError):
            ConversationPublisher(http=http).prepare_context(
                token="token",
                repository="owner/repo",
                pull_request=1,
                source_comment_id=10,
                source_updated_at=updated,
                expected_head_sha=head,
                app_slug="review-sensei[bot]",
                root_comment_id=8,
                source_kind="inline",
            )

        self.assertEqual(len(calls), 2)

    def test_prepare_context_includes_bounded_pr_diff_findings_and_base_learnings(self):
        updated = "2026-08-19T00:00:00Z"
        head = "b" * 40
        source = {
            "id": 10,
            "pull_request_url": INLINE_URL,
            "body": "@sensei explain this change",
            "user": {"login": "alice", "type": "User"},
            "author_association": "MEMBER",
            "updated_at": updated,
            "created_at": updated,
            "in_reply_to_id": None,
            "path": "src/app.py",
            "diff_hunk": "@@ -1 +1 @@\n-old\n+new",
        }
        prior = {
            "id": 9,
            "body": "Prior exact-head finding",
            "user": {"login": "review-sensei[bot]", "type": "Bot"},
            "created_at": updated,
            "commit_id": head,
            "path": "src/app.py",
            "line": 2,
        }
        learning = {
            "id": "python.error-contract",
            "title": "Stable errors",
            "rule": "Keep public errors stable.",
            "scope": ["src/**"],
            "status": "active",
        }
        encoded = base64.b64encode(
            (json.dumps(learning) + "\n").encode("utf-8")
        ).decode("ascii")
        responses = [
            json_response(source),
            json_response(pr_payload(head)),
            json_response([prior, source]),
            json_response(
                [
                    {
                        "type": "file",
                        "path": ".github/review-sensei/learnings/rule.json",
                        "size": len(encoded),
                    }
                ]
            ),
            json_response({"content": encoded, "encoding": "base64"}),
        ]
        http, calls = make_http(responses)

        prepared = ConversationPublisher(http=http).prepare_context(
            token="token",
            repository="owner/repo",
            pull_request=1,
            source_comment_id=10,
            source_updated_at=updated,
            expected_head_sha=head,
            app_slug="review-sensei[bot]",
            source_kind="inline",
        )

        self.assertIsInstance(prepared, PreparedConversation)
        context = prepared.context
        self.assertEqual(context.pull_request_title, "Bounded PR title")
        self.assertEqual(context.pull_request_body, "Bounded PR body")
        self.assertEqual(context.base_ref, "main")
        self.assertEqual(context.base_sha, "a" * 40)
        self.assertEqual(context.head_ref, "feature/context")
        self.assertIn("+new", context.diff_context)
        self.assertEqual(context.prior_findings[0].body, "Prior exact-head finding")
        self.assertEqual(context.learnings[0].id, "python.error-contract")
        prompt = ConversationService._render_prompt(context)
        self.assertIn("untrusted-pr-metadata", prompt)
        self.assertIn("untrusted-diff-context", prompt)
        self.assertIn("trusted-base-learnings", prompt)
        self.assertEqual(len(calls), 5)

    def test_prepare_context_ignores_non_json_learning_files(self):
        updated = "2026-08-19T00:00:00Z"
        head = "b" * 40
        source = {
            "id": 10,
            "pull_request_url": INLINE_URL,
            "body": "@sensei explain this change",
            "user": {"login": "alice", "type": "User"},
            "author_association": "MEMBER",
            "updated_at": updated,
            "created_at": updated,
            "in_reply_to_id": None,
            "path": "src/app.py",
            "diff_hunk": "@@ -1 +1 @@\n-old\n+new",
        }
        responses = [
            json_response(source),
            json_response(pr_payload(head)),
            json_response([source]),
            json_response(
                [
                    {
                        "type": "file",
                        "path": ".github/review-sensei/learnings/README.md",
                        "size": 712,
                    }
                ]
            ),
        ]
        http, calls = make_http(responses)

        prepared = ConversationPublisher(http=http).prepare_context(
            token="token",
            repository="owner/repo",
            pull_request=1,
            source_comment_id=10,
            source_updated_at=updated,
            expected_head_sha=head,
            app_slug="review-sensei[bot]",
            source_kind="inline",
        )

        self.assertIsInstance(prepared, PreparedConversation)
        self.assertEqual(prepared.context.learnings, ())
        self.assertEqual(len(calls), 4)
        self.assertTrue(all("README.md" not in call[1] for call in calls))

    def test_base_learnings_rejects_json_path_anomalies(self):
        for path in (
            ".github/review-sensei/other.json",
            ".github/review-sensei/learnings-extra/file.json",
            ".github/review-sensei/learnings-extra/file.md",
            ".github/review-sensei/learnings/nested/bad.json",
        ):
            with self.subTest(path=path):
                http, calls = make_http(
                    json_response([{"type": "file", "path": path, "size": 1}])
                )
                with self.assertRaises(GitHubConversationError):
                    ConversationPublisher(http=http)._load_base_learnings(
                        token="token",
                        repository="owner/repo",
                        base_sha="a" * 40,
                        changed_paths=("src/app.py",),
                    )
                self.assertEqual(len(calls), 1)

    def test_base_learnings_counts_only_json_files(self):
        directory = [
            {
                "type": "file",
                "path": ".github/review-sensei/learnings/README.md",
                "size": 712,
            },
            *[
                {
                    "type": "file",
                    "path": f".github/review-sensei/learnings/rule-{index}.json",
                    "size": 1,
                }
                for index in range(100)
            ],
        ]
        encoded_files = []
        for index in range(100):
            learning = {
                "id": f"python.error-contract-{index}",
                "title": "Stable errors",
                "rule": "Keep public errors stable.",
                "scope": ["src/**"],
                "status": "active",
            }
            encoded_files.append(
                base64.b64encode((json.dumps(learning) + "\n").encode("utf-8")).decode(
                    "ascii"
                )
            )
        responses = [
            json_response(directory),
            *[
                json_response({"content": encoded, "encoding": "base64"})
                for encoded in encoded_files
            ],
        ]
        http, calls = make_http(responses)

        entries = ConversationPublisher(http=http)._load_base_learnings(
            token="token",
            repository="owner/repo",
            base_sha="a" * 40,
            changed_paths=("src/app.py",),
        )

        self.assertGreater(len(entries), 0)
        self.assertEqual(len(calls), 101)

    def test_prepare_context_rejects_unauthorized_before_thread_fetch(self):
        http, calls = make_http(
            json_response(
                {
                    "id": 10,
                    "pull_request_url": INLINE_URL,
                    "body": "please @sensei explain",
                    "user": {"login": "alice", "type": "User"},
                    "author_association": "CONTRIBUTOR",
                    "updated_at": "updated",
                }
            )
        )
        outcome = ConversationPublisher(http=http).prepare_context(
            token="token",
            repository="owner/repo",
            pull_request=1,
            source_comment_id=10,
            source_updated_at="updated",
            app_slug="review-sensei[bot]",
        )
        self.assertEqual(outcome.status, "skipped_unauthorized")
        self.assertEqual(len(calls), 1)

    def test_mention_and_authorization_helpers_are_strict(self):
        self.assertTrue(has_standalone_sensei_mention("please @sensei help"))
        self.assertTrue(has_standalone_sensei_mention("please @SENSEI help"))
        self.assertFalse(has_standalone_sensei_mention("@senseiish"))
        self.assertFalse(has_standalone_sensei_mention(None))
        self.assertTrue(
            authorized_human_comment(
                {
                    "user": {"login": "alice", "type": "User"},
                    "author_association": "member",
                },
                app_slug="review-sensei[bot]",
            )
        )
        self.assertFalse(
            authorized_human_comment(
                {
                    "user": {"login": "Review-Sensei[Bot]", "type": "Bot"},
                    "author_association": "OWNER",
                },
                app_slug="review-sensei[bot]",
            )
        )
        self.assertFalse(authorized_human_comment({}, app_slug="review-sensei[bot]"))
        self.assertIn(
            "head=" + ("a" * 40),
            reply_marker(
                source_comment_id=1,
                source_updated_digest="digest",
                pull_request=2,
                head_sha="a" * 40,
            ),
        )

    def test_source_gone_no_mention_and_unauthorized_do_not_write(self):
        for source, expected in (
            ({"pull_request_url": INLINE_URL}, "skipped_no_mention"),
            (
                {
                    "body": "@sensei help",
                    "pull_request_url": INLINE_URL,
                    "user": {"login": "alice", "type": "User"},
                    "author_association": "CONTRIBUTOR",
                },
                "skipped_unauthorized",
            ),
        ):
            http, calls = make_http(json_response(source))
            outcome = ConversationPublisher(http=http).publish(
                token="token",
                repository="owner/repo",
                pull_request=1,
                source_comment_id=10,
                source_updated_at="updated",
                head_sha="b" * 40,
                reply=ConversationReply.from_dict({"body": "Thanks."}),
                app_slug="review-sensei[bot]",
                root_comment_id=10,
            )
            self.assertEqual(outcome.status, expected)
            self.assertEqual(len(calls), 1)

        http, calls = make_http(json_response({}, 404))
        outcome = ConversationPublisher(http=http).publish(
            token="token",
            repository="owner/repo",
            pull_request=1,
            source_comment_id=10,
            source_updated_at="updated",
            head_sha="b" * 40,
            reply=ConversationReply.from_dict({"body": "Thanks."}),
            app_slug="review-sensei[bot]",
            root_comment_id=10,
        )
        self.assertEqual(outcome.status, "skipped_source_gone")
        self.assertEqual(len(calls), 1)

    def test_issue_reply_uses_issue_comments_without_root_lookup(self):
        updated = "2026-08-19T00:00:00Z"
        head = "b" * 40
        responses = [
            json_response(
                {
                    "id": 10,
                    "issue_url": ISSUE_URL,
                    "body": "@sensei please explain",
                    "user": {"login": "alice", "type": "User"},
                    "author_association": "MEMBER",
                    "updated_at": updated,
                }
            ),
            json_response([]),
            json_response(pr_payload(head)),
            json_response({"id": 12}, 201),
        ]
        http, calls = make_http(responses)
        outcome = ConversationPublisher(http=http).publish(
            token="token",
            repository="owner/repo",
            pull_request=1,
            source_comment_id=10,
            source_updated_at=updated,
            head_sha=head,
            reply=ConversationReply.from_dict({"body": "Thanks.", "resolve": True}),
            app_slug="review-sensei[bot]",
            root_comment_id=10,
            source_kind="issue",
        )
        self.assertEqual(outcome.status, "replied")
        self.assertFalse(outcome.resolved)
        self.assertTrue(calls[-1][1].endswith("/repos/owner/repo/issues/1/comments"))

    def test_invalid_reply_inputs_fail_before_github_request(self):
        http, calls = make_http([])
        publisher = ConversationPublisher(http=http)
        kwargs = {
            "token": "token",
            "repository": "owner/repo",
            "pull_request": 1,
            "source_comment_id": 10,
            "source_updated_at": "updated",
            "head_sha": "b" * 40,
            "reply": ConversationReply.from_dict({"body": "Thanks."}),
            "app_slug": "review-sensei[bot]",
            "root_comment_id": 10,
        }
        with self.assertRaises(GitHubConversationError):
            publisher.publish(**{**kwargs, "head_sha": "not-a-sha"})
        with self.assertRaises(GitHubConversationError):
            publisher.publish(**{**kwargs, "source_kind": "unknown"})
        with self.assertRaises(GitHubConversationError):
            publisher.publish(**{**kwargs, "reply": object()})
        self.assertEqual(calls, [])

        http, calls = make_http(json_response({}, 500))
        with self.assertRaises(GitHubConversationError):
            ConversationPublisher(http=http).publish(**kwargs)
        self.assertEqual(len(calls), 1)

        http, calls = make_http(json_response([], 200))
        with self.assertRaises(GitHubConversationError):
            ConversationPublisher(http=http).publish(**kwargs)
        self.assertEqual(len(calls), 1)

    def test_reply_publishes_with_marker_on_valid_source(self):
        head = "b" * 40
        updated = "2026-08-19T00:00:00Z"
        responses = [
            json_response(
                {
                    "id": 10,
                    "pull_request_url": INLINE_URL,
                    "body": "@sensei please explain",
                    "user": {"login": "alice", "type": "User"},
                    "author_association": "MEMBER",
                    "updated_at": updated,
                    "in_reply_to_id": None,
                    "path": "src/app.py",
                    "line": 2,
                }
            ),
            json_response([]),
            json_response(pr_payload(head)),
            json_response({"id": 12}, 201),
        ]
        http, calls = make_http(responses)
        outcome = ConversationPublisher(http=http).publish(
            token="token",
            repository="owner/repo",
            pull_request=1,
            source_comment_id=10,
            source_updated_at=updated,
            head_sha=head,
            reply=ConversationReply.from_dict({"body": "Thanks."}),
            app_slug="review-sensei[bot]",
            root_comment_id=10,
        )
        self.assertEqual(outcome.status, "replied")
        self.assertEqual(outcome.comment_id, 12)
        post_body = __import__("json").loads(calls[-1][2].decode("utf-8"))
        self.assertIn("<!-- reviewsensei:reply:v1", post_body["body"])
        self.assertEqual(set(post_body), {"body"})
        self.assertTrue(calls[-1][1].endswith("/pulls/1/comments/10/replies"))

    def test_ai_resolution_resolves_review_sensei_inline_thread(self):
        head = "b" * 40
        updated = "2026-08-19T00:00:00Z"
        responses = [
            json_response(
                {
                    "id": 11,
                    "pull_request_url": INLINE_URL,
                    "body": "@sensei addressed this",
                    "user": {"login": "alice", "type": "User"},
                    "author_association": "MEMBER",
                    "updated_at": updated,
                    "in_reply_to_id": 10,
                    "path": "src/app.py",
                    "diff_hunk": "@@ -1 +1 @@\n-old\n+new",
                }
            ),
            json_response(
                {
                    "id": 10,
                    "pull_request_url": INLINE_URL,
                    "body": "[🚫 Blocking] The original finding",
                    "user": {"login": "Review-Sensei[Bot]", "type": "Bot"},
                    "author_association": "OWNER",
                    "in_reply_to_id": None,
                }
            ),
            json_response([]),
            json_response(pr_payload(head)),
            json_response({"id": 12}, 201),
            json_response(pr_payload(head)),
            json_response(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "nodes": [
                                        {
                                            "id": "PRRT_thread",
                                            "isResolved": False,
                                            "comments": {"nodes": [{"databaseId": 10}]},
                                        }
                                    ],
                                    "pageInfo": {
                                        "hasNextPage": False,
                                        "endCursor": None,
                                    },
                                }
                            }
                        }
                    }
                }
            ),
            json_response(pr_payload(head)),
            json_response(
                {
                    "data": {
                        "resolveReviewThread": {
                            "thread": {
                                "id": "PRRT_thread",
                                "isResolved": True,
                            }
                        }
                    }
                }
            ),
            json_response(pr_payload(head)),
            json_response(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "nodes": [{"isResolved": True}],
                                    "pageInfo": {
                                        "hasNextPage": False,
                                        "endCursor": None,
                                    },
                                }
                            }
                        }
                    }
                }
            ),
            json_response(pr_payload(head)),
            json_response([]),
            json_response({"id": 13}, 200),
        ]
        http, calls = make_http(responses)
        outcome = ConversationPublisher(http=http).publish(
            token="token",
            repository="owner/repo",
            pull_request=1,
            source_comment_id=11,
            source_updated_at=updated,
            head_sha=head,
            reply=ConversationReply.from_dict(
                {"body": "Confirmed—the fix is complete.", "resolve": True}
            ),
            app_slug="review-sensei[bot]",
            root_comment_id=10,
        )
        self.assertEqual(outcome.status, "replied_and_resolved")
        self.assertTrue(outcome.resolved)
        self.assertEqual(outcome.comment_id, 12)
        self.assertEqual(
            [call[0] for call in calls],
            [
                "GET",
                "GET",
                "GET",
                "GET",
                "POST",
                "GET",
                "POST",
                "GET",
                "POST",
                "GET",
                "POST",
                "GET",
                "GET",
                "POST",
            ],
        )
        mutation = json.loads(calls[8][2].decode("utf-8"))
        self.assertEqual(mutation["operationName"], "ResolveReviewThread")
        self.assertEqual(mutation["variables"], {"input": {"threadId": "PRRT_thread"}})
        approval = json.loads(calls[-1][2].decode("utf-8"))
        self.assertEqual(approval["event"], "APPROVE")
        self.assertNotIn("comments", approval)

    def test_ai_resolution_wraps_finalizer_publication_errors(self):
        class FailingFinalizer:
            def finalize(self, **_kwargs):
                raise GitHubPublicationError("finalization rejected")

        publisher = ConversationPublisher(http=make_http([])[0])
        publisher.finalizer = FailingFinalizer()
        publisher._resolve_review_thread = lambda **_kwargs: None

        with self.assertRaises(GitHubConversationError):
            publisher._resolve_and_finalize(
                token="token",
                repository="owner/repo",
                pull_request=1,
                root_comment_id=10,
                head_sha="b" * 40,
                app_slug="review-sensei[bot]",
                auto_approve=True,
                root_is_blocking_finding=True,
            )

    def test_ai_resolution_does_not_resolve_human_root_thread(self):
        head = "b" * 40
        updated = "2026-08-19T00:00:00Z"
        responses = [
            json_response(
                {
                    "id": 10,
                    "pull_request_url": INLINE_URL,
                    "body": "@sensei addressed this",
                    "user": {"login": "alice", "type": "User"},
                    "author_association": "MEMBER",
                    "updated_at": updated,
                    "in_reply_to_id": None,
                    "path": "src/app.py",
                    "diff_hunk": "@@ -1 +1 @@\n-old\n+new",
                }
            ),
            json_response([]),
            json_response(pr_payload(head)),
            json_response({"id": 12}, 201),
        ]
        http, calls = make_http(responses)
        outcome = ConversationPublisher(http=http).publish(
            token="token",
            repository="owner/repo",
            pull_request=1,
            source_comment_id=10,
            source_updated_at=updated,
            head_sha=head,
            reply=ConversationReply.from_dict({"body": "Thanks.", "resolve": True}),
            app_slug="review-sensei[bot]",
            root_comment_id=10,
        )
        self.assertEqual(outcome.status, "replied")
        self.assertFalse(outcome.resolved)
        self.assertEqual(len(calls), 4)

    def test_ai_resolution_retries_an_existing_reply_idempotently(self):
        head = "b" * 40
        updated = "2026-08-19T00:00:00Z"
        marker = reply_marker(
            source_comment_id=11,
            source_updated_digest=__import__("hashlib")
            .sha256(updated.encode("utf-8"))
            .hexdigest(),
            pull_request=1,
            head_sha=head,
        )
        source = {
            "id": 11,
            "pull_request_url": INLINE_URL,
            "body": "@sensei addressed this",
            "user": {"login": "alice", "type": "User"},
            "author_association": "MEMBER",
            "updated_at": updated,
            "in_reply_to_id": 10,
        }
        root = {
            "id": 10,
            "pull_request_url": INLINE_URL,
            "body": "Original finding",
            "user": {"login": "review-sensei[bot]", "type": "Bot"},
            "in_reply_to_id": None,
        }
        responses = [
            json_response(source),
            json_response(root),
            json_response(
                [{"id": 12, "body": marker, "user": {"login": "review-sensei[bot]"}}]
            ),
            json_response(pr_payload(head)),
            json_response(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "nodes": [
                                        {
                                            "id": "PRRT_thread",
                                            "isResolved": True,
                                            "comments": {"nodes": [{"databaseId": 10}]},
                                        }
                                    ],
                                    "pageInfo": {
                                        "hasNextPage": False,
                                        "endCursor": None,
                                    },
                                }
                            }
                        }
                    }
                }
            ),
        ]
        http, calls = make_http(responses)
        outcome = ConversationPublisher(http=http).publish(
            token="token",
            repository="owner/repo",
            pull_request=1,
            source_comment_id=11,
            source_updated_at=updated,
            head_sha=head,
            reply=ConversationReply.from_dict(
                {"body": "Confirmed—the fix is complete.", "resolve": True}
            ),
            app_slug="review-sensei[bot]",
            root_comment_id=10,
        )
        self.assertEqual(outcome.status, "already_replied_and_resolved")
        self.assertTrue(outcome.resolved)
        self.assertEqual(outcome.comment_id, 12)
        self.assertEqual(
            [call[0] for call in calls], ["GET", "GET", "GET", "GET", "POST"]
        )

    def test_ai_resolution_fails_closed_when_head_changes_before_mutation(self):
        head = "b" * 40
        updated = "2026-08-19T00:00:00Z"
        root = {
            "id": 10,
            "pull_request_url": INLINE_URL,
            "body": "Original finding",
            "user": {"login": "review-sensei[bot]", "type": "Bot"},
            "in_reply_to_id": None,
        }
        source = {
            "id": 11,
            "pull_request_url": INLINE_URL,
            "body": "@sensei addressed this",
            "user": {"login": "alice", "type": "User"},
            "author_association": "MEMBER",
            "updated_at": updated,
            "in_reply_to_id": 10,
            "path": "src/app.py",
            "diff_hunk": "@@ -1 +1 @@\n-old\n+new",
        }
        responses = [
            json_response(source),
            json_response(root),
            json_response([]),
            json_response(pr_payload(head)),
            json_response({"id": 12}, 201),
            json_response(pr_payload("c" * 40)),
        ]
        http, calls = make_http(responses)
        with self.assertRaises(GitHubConversationError):
            ConversationPublisher(http=http).publish(
                token="token",
                repository="owner/repo",
                pull_request=1,
                source_comment_id=11,
                source_updated_at=updated,
                head_sha=head,
                reply=ConversationReply.from_dict(
                    {"body": "Confirmed—the fix is complete.", "resolve": True}
                ),
                app_slug="review-sensei[bot]",
                root_comment_id=10,
            )
        self.assertEqual(
            [call[0] for call in calls], ["GET", "GET", "GET", "GET", "POST", "GET"]
        )
        self.assertFalse(any(call[1].endswith("/graphql") for call in calls))

    def test_ai_resolution_fails_closed_on_malformed_thread_lookup(self):
        head = "b" * 40
        updated = "2026-08-19T00:00:00Z"
        root = {
            "id": 10,
            "pull_request_url": INLINE_URL,
            "body": "Original finding",
            "user": {"login": "review-sensei[bot]", "type": "Bot"},
            "in_reply_to_id": None,
        }
        source = {
            "id": 11,
            "pull_request_url": INLINE_URL,
            "body": "@sensei addressed this",
            "user": {"login": "alice", "type": "User"},
            "author_association": "MEMBER",
            "updated_at": updated,
            "in_reply_to_id": 10,
            "path": "src/app.py",
            "diff_hunk": "@@ -1 +1 @@\n-old\n+new",
        }
        responses = [
            json_response(source),
            json_response(root),
            json_response([]),
            json_response(pr_payload(head)),
            json_response({"id": 12}, 201),
            json_response(pr_payload(head)),
            json_response({"data": {}}),
        ]
        http, calls = make_http(responses)
        with self.assertRaises(GitHubConversationError):
            ConversationPublisher(http=http).publish(
                token="token",
                repository="owner/repo",
                pull_request=1,
                source_comment_id=11,
                source_updated_at=updated,
                head_sha=head,
                reply=ConversationReply.from_dict(
                    {"body": "Confirmed—the fix is complete.", "resolve": True}
                ),
                app_slug="review-sensei[bot]",
                root_comment_id=10,
            )
        self.assertEqual(calls[-1][1], "https://api.github.test/graphql")
        self.assertEqual(len(calls), 7)

    def test_ai_resolution_maps_thread_lookup_transport_failure(self):
        head = "b" * 40
        updated = "2026-08-19T00:00:00Z"
        root = {
            "id": 10,
            "pull_request_url": INLINE_URL,
            "body": "Original finding",
            "user": {"login": "review-sensei[bot]", "type": "Bot"},
            "in_reply_to_id": None,
        }
        source = {
            "id": 11,
            "pull_request_url": INLINE_URL,
            "body": "@sensei addressed this",
            "user": {"login": "alice", "type": "User"},
            "author_association": "MEMBER",
            "updated_at": updated,
            "in_reply_to_id": 10,
            "path": "src/app.py",
            "diff_hunk": "@@ -1 +1 @@\n-old\n+new",
        }
        responses = [
            json_response(source),
            json_response(root),
            json_response([]),
            json_response(pr_payload(head)),
            json_response({"id": 12}, 201),
            json_response(pr_payload(head)),
            URLError("timed out"),
        ]
        http, calls = make_http(responses)
        with self.assertRaises(GitHubConversationTransientError):
            ConversationPublisher(http=http).publish(
                token="token",
                repository="owner/repo",
                pull_request=1,
                source_comment_id=11,
                source_updated_at=updated,
                head_sha=head,
                reply=ConversationReply.from_dict(
                    {"body": "Confirmed—the fix is complete.", "resolve": True}
                ),
                app_slug="review-sensei[bot]",
                root_comment_id=10,
            )
        self.assertEqual(calls[-1][1], "https://api.github.test/graphql")
        self.assertEqual(len(calls), 7)

    def test_reconciliation_pagination_failure_fails_closed_before_post(self):
        updated = "2026-08-19T00:00:00Z"
        source = {
            "id": 10,
            "pull_request_url": INLINE_URL,
            "body": "@SENSEI please explain",
            "user": {"login": "alice", "type": "User"},
            "author_association": "MEMBER",
            "updated_at": updated,
            "in_reply_to_id": None,
        }
        http, calls = make_http([json_response(source), json_response({}, 500)])
        with self.assertRaises(GitHubConversationError):
            ConversationPublisher(http=http).publish(
                token="token",
                repository="owner/repo",
                pull_request=1,
                source_comment_id=10,
                source_updated_at=updated,
                head_sha="b" * 40,
                reply=ConversationReply.from_dict({"body": "Thanks."}),
                app_slug="reviewsensei[bot]",
                root_comment_id=10,
            )
        self.assertEqual(len(calls), 2)

    def test_transient_reply_post_reconciles_marker(self):
        updated = "2026-08-19T00:00:00Z"
        head = "b" * 40
        reply = ConversationReply.from_dict({"body": "Thanks."})
        marker = reply_marker(
            source_comment_id=10,
            source_updated_digest=__import__("hashlib")
            .sha256(updated.encode("utf-8"))
            .hexdigest(),
            pull_request=1,
            head_sha=head,
        )
        source = {
            "id": 10,
            "pull_request_url": INLINE_URL,
            "body": "@sensei please explain",
            "user": {"login": "alice", "type": "User"},
            "author_association": "MEMBER",
            "updated_at": updated,
            "in_reply_to_id": None,
        }
        http, calls = make_http(
            [
                json_response(source),
                json_response([]),
                json_response(pr_payload(head)),
                URLError("timed out"),
                json_response(
                    [
                        {
                            "id": 12,
                            "body": f"Thanks.\n\n{marker}",
                            "user": {"login": "reviewsensei[bot]"},
                        }
                    ]
                ),
            ]
        )
        outcome = ConversationPublisher(http=http).publish(
            token="token",
            repository="owner/repo",
            pull_request=1,
            source_comment_id=10,
            source_updated_at=updated,
            head_sha=head,
            reply=reply,
            app_slug="reviewsensei[bot]",
            root_comment_id=10,
        )
        self.assertEqual(outcome.status, "already_replied")
        self.assertEqual(
            [call[0] for call in calls], ["GET", "GET", "GET", "POST", "GET"]
        )
        self.assertIn(
            f"per_page={CONVERSATION_COMMENT_PAGE_SIZES[0]}&page=1",
            calls[1][1],
        )

    def test_edited_source_skips_write(self):
        http, calls = make_http(
            json_response(
                {
                    "id": 10,
                    "pull_request_url": INLINE_URL,
                    "body": "@sensei please explain",
                    "user": {"login": "alice", "type": "User"},
                    "author_association": "MEMBER",
                    "updated_at": "changed",
                    "in_reply_to_id": None,
                    "path": "src/app.py",
                    "line": 2,
                }
            )
        )
        outcome = ConversationPublisher(http=http).publish(
            token="token",
            repository="owner/repo",
            pull_request=1,
            source_comment_id=10,
            source_updated_at="original",
            head_sha="b" * 40,
            reply=ConversationReply.from_dict({"body": "Thanks."}),
            app_slug="review-sensei[bot]",
            root_comment_id=10,
        )
        self.assertEqual(outcome.status, "skipped_edited_source")
        self.assertEqual(len(calls), 1)

    def test_stale_head_skips_write(self):
        updated = "2026-08-19T00:00:00Z"
        responses = [
            json_response(
                {
                    "id": 10,
                    "pull_request_url": INLINE_URL,
                    "body": "@sensei please explain",
                    "user": {"login": "alice", "type": "User"},
                    "author_association": "MEMBER",
                    "updated_at": updated,
                    "in_reply_to_id": None,
                    "path": "src/app.py",
                    "line": 2,
                }
            ),
            json_response([]),
            json_response(pr_payload("c" * 40)),
        ]
        http, calls = make_http(responses)
        outcome = ConversationPublisher(http=http).publish(
            token="token",
            repository="owner/repo",
            pull_request=1,
            source_comment_id=10,
            source_updated_at=updated,
            head_sha="b" * 40,
            reply=ConversationReply.from_dict({"body": "Thanks."}),
            app_slug="review-sensei[bot]",
            root_comment_id=10,
        )
        self.assertEqual(outcome.status, "skipped_stale_head")
        self.assertEqual(len(calls), 3)

    def test_fork_skips_write(self):
        updated = "2026-08-19T00:00:00Z"
        responses = [
            json_response(
                {
                    "id": 10,
                    "pull_request_url": INLINE_URL,
                    "body": "@sensei please explain",
                    "user": {"login": "alice", "type": "User"},
                    "author_association": "MEMBER",
                    "updated_at": updated,
                    "in_reply_to_id": None,
                    "path": "src/app.py",
                    "line": 2,
                }
            ),
            json_response([]),
            json_response(pr_payload("b" * 40, fork=True)),
        ]
        http, calls = make_http(responses)
        outcome = ConversationPublisher(http=http).publish(
            token="token",
            repository="owner/repo",
            pull_request=1,
            source_comment_id=10,
            source_updated_at=updated,
            head_sha="b" * 40,
            reply=ConversationReply.from_dict({"body": "Thanks."}),
            app_slug="review-sensei[bot]",
            root_comment_id=10,
        )
        self.assertEqual(outcome.status, "skipped_fork")
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
