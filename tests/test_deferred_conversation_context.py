"""Deferred mention preparation authenticates scope without generic evidence IO."""

from __future__ import annotations

import unittest
from copy import deepcopy
from unittest.mock import patch
from urllib.parse import urlparse

from review_sensei_github.conversation import (
    ConversationPublisher,
    PreparedConversation,
)
from review_sensei_github.errors import GitHubConversationError

from tests.fake_github_http import json_response
from tests.test_human_assessment import APP, BASE, HEAD, State


class DeferredConversationContextTests(unittest.TestCase):
    def setUp(self):
        self.state = State()
        self.publisher = ConversationPublisher(http=self.state.http)

    def prepare(self, **overrides):
        arguments = dict(
            token="synthetic",
            repository="owner/repo",
            pull_request=1,
            source_comment_id=10,
            source_updated_at="updated",
            app_slug=APP,
            root_comment_id=10,
            source_kind="issue",
            expected_head_sha=HEAD,
            defer_evidence=True,
        )
        arguments.update(overrides)
        return self.publisher.prepare_context(**arguments)

    def test_deferred_issue_context_reads_only_source_and_current_pr(self):
        prepared = self.prepare()
        self.assertIsInstance(prepared, PreparedConversation)
        self.assertEqual(
            [(method, path) for method, path, _ in self.state.calls],
            [
                ("GET", "/repos/owner/repo/issues/comments/10"),
                ("GET", "/repos/owner/repo/pulls/1"),
            ],
        )
        self.assertEqual(prepared.head_sha, HEAD)
        self.assertEqual(prepared.context.base_sha, BASE)
        self.assertEqual(prepared.context.messages[-1].body, self.state.source["body"])
        self.assertEqual(len(prepared.context.messages), 1)
        self.assertEqual(prepared.context.prior_findings, ())
        self.assertIsNone(prepared.context.diff_context)
        self.assertEqual(prepared.context.learnings, ())

    def test_default_and_explicit_false_keep_full_context_acquisition(self):
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                self.setUp()
                arguments = dict(
                    token="synthetic",
                    repository="owner/repo",
                    pull_request=1,
                    source_comment_id=10,
                    source_updated_at="updated",
                    app_slug=APP,
                    root_comment_id=10,
                    source_kind="issue",
                    expected_head_sha=HEAD,
                )
                if explicit:
                    arguments["defer_evidence"] = False
                prepared = self.publisher.prepare_context(**arguments)
                paths = [path for _, path, _ in self.state.calls]
                for suffix in (
                    "/issues/1/comments",
                    "/pulls/1/comments",
                    "/pulls/1/files",
                    "/contents/.github/review-sensei/learnings",
                ):
                    self.assertTrue(
                        any(path.endswith(suffix) for path in paths), suffix
                    )
                self.assertIsNotNone(prepared.context.diff_context)

    def test_invalid_defer_gate_refuses_before_any_dispatch(self):
        for value in (None, 0, 1, "true", []):
            with self.subTest(value=value), self.assertRaises(GitHubConversationError):
                self.prepare(defer_evidence=value)
        self.assertEqual(self.state.calls, [])

    def test_deferred_source_authorization_and_exact_update_stay_fail_closed(self):
        for change, status in (
            ("mention", "skipped_no_mention"),
            ("association", "skipped_unauthorized"),
            ("bot", "skipped_unauthorized"),
            ("updated", "skipped_edited_source"),
        ):
            with self.subTest(change=change):
                self.setUp()
                if change == "mention":
                    self.state.source["body"] = "ordinary unrelated comment"
                elif change == "association":
                    self.state.source["author_association"] = "NONE"
                elif change == "bot":
                    self.state.source["user"]["type"] = "Bot"
                else:
                    self.state.source["updated_at"] = "changed"
                self.assertEqual(self.prepare().status, status)
                self.assertEqual(len(self.state.calls), 1)
        self.setUp()
        self.state.source["issue_url"] = (
            "https://api.github.test/repos/owner/repo/issues/2"
        )
        with self.assertRaises(GitHubConversationError):
            self.prepare()

    def test_deferred_live_pr_head_state_fork_and_base_are_still_authenticated(self):
        for change, status in (
            ("head", "skipped_stale_head"),
            ("closed", "skipped_pr_state"),
            ("draft", "skipped_pr_state"),
            ("fork", "skipped_fork"),
            ("repository", "skipped_fork"),
            ("base", None),
        ):
            with self.subTest(change=change):
                self.setUp()
                pr = deepcopy(self.state.pr())
                if change == "head":
                    pr["head"]["sha"] = "c" * 40
                elif change == "closed":
                    pr["state"] = "closed"
                elif change == "draft":
                    pr["draft"] = True
                elif change == "fork":
                    pr["head"]["repo"]["fork"] = True
                elif change == "repository":
                    pr["base"]["repo"]["full_name"] = "foreign/repo"
                else:
                    pr["base"]["sha"] = "invalid"
                with patch.object(self.state, "pr", return_value=pr):
                    if status is None:
                        with self.assertRaises(GitHubConversationError):
                            self.prepare()
                    else:
                        self.assertEqual(self.prepare().status, status)
                self.assertEqual(len(self.state.calls), 2)

    def inline_context(self, *, foreign_root=False, caller_root=10):
        source = {
            **deepcopy(self.state.source),
            "id": 11,
            "in_reply_to_id": 10,
            "pull_request_url": "https://api.github.test/repos/owner/repo/pulls/1",
        }
        root = {
            "id": 10,
            "body": "Original root.",
            "user": {"login": APP, "type": "Bot"},
            "pull_request_url": "https://api.github.test/repos/owner/repo/pulls/2"
            if foreign_root
            else "https://api.github.test/repos/owner/repo/pulls/1",
        }
        ordinary = self.state.http.opener

        def open_request(request, timeout):
            path = urlparse(request.full_url).path
            if path.endswith("/pulls/comments/11") or path.endswith(
                "/pulls/comments/10"
            ):
                self.state.calls.append((request.method, path, None))
                return json_response(source if path.endswith("/11") else root)
            return ordinary(request, timeout)

        with patch.object(self.state.http, "opener", side_effect=open_request):
            return self.prepare(
                source_kind="inline", source_comment_id=11, root_comment_id=caller_root
            )

    def test_deferred_inline_reply_reads_and_validates_actual_root_association(self):
        prepared = self.inline_context()
        self.assertIsInstance(prepared, PreparedConversation)
        self.assertEqual(prepared.root_comment_id, 10)
        self.assertEqual(len(prepared.context.messages), 1)
        self.assertEqual(prepared.context.messages[0].body, self.state.source["body"])
        self.assertEqual(len(self.state.calls), 3)
        self.assertEqual(self.state.calls[-1][1], "/repos/owner/repo/pulls/comments/10")
        self.setUp()
        with self.assertRaises(GitHubConversationError):
            self.inline_context(foreign_root=True)
        self.setUp()
        with self.assertRaises(GitHubConversationError):
            self.inline_context(caller_root=99)


if __name__ == "__main__":
    unittest.main()
