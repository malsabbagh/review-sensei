"""Explicit complete empty cleanup targets do not create effect callbacks."""

import unittest
from unittest.mock import Mock

from review_sensei_github.http import GitHubHttp
from review_sensei_github.publication import ReviewPublisher

from review_sensei.models import ReviewComment


class OrdinaryCleanupNoopTests(unittest.TestCase):
    def fixture(self):
        publisher = ReviewPublisher(http=GitHubHttp())
        publisher.http.request = Mock(side_effect=AssertionError("no native request"))
        publisher._thread_cleanup_token_provider = Mock(
            side_effect=AssertionError("no cleanup capability")
        )
        arguments = dict(
            token="synthetic",
            repository="owner/repo",
            repository_id=123,
            pull_request=7,
            head_sha="b" * 40,
            base_sha="a" * 40,
            app_slug="reviewsensei[bot]",
            eligibility=None,
            review_id=101,
            replacements=(
                (
                    ReviewComment("src/app.py", 2, "Criterion"),
                    "concern",
                    "rendered",
                    "right",
                ),
            ),
        )
        return publisher, arguments

    def test_explicit_empty_preserves_original_no_effect_semantics(self):
        publisher, arguments = self.fixture()
        publisher._schedule_thread_cleanup(**arguments, threads=())
        self.assertIsNone(publisher._pending_thread_cleanup)
        # This is the original closure's actual terminal operation, not a
        # substitute oracle: an empty matching set already returns before I/O.
        direct = {**arguments, "confirmed_review_id": arguments["review_id"]}
        del direct["review_id"]
        self.assertIsNone(publisher._supersede_replaced_threads(**direct, threads=()))
        publisher.http.request.assert_not_called()
        publisher._thread_cleanup_token_provider.assert_not_called()

    def test_unknown_targets_still_schedule_discovery(self):
        publisher, arguments = self.fixture()
        publisher._schedule_thread_cleanup(**arguments, threads=None)
        self.assertTrue(callable(publisher._pending_thread_cleanup))
        publisher.http.request.assert_not_called()
        publisher._thread_cleanup_token_provider.assert_not_called()

    def test_existing_pending_callback_does_not_take_new_shortcut(self):
        publisher, arguments = self.fixture()
        previous = Mock(side_effect=AssertionError("previous owner not invoked"))
        publisher._pending_thread_cleanup = previous
        publisher._schedule_thread_cleanup(**arguments, threads=())
        # Preserve existing scheduler behavior, not a generic callback clear.
        self.assertTrue(callable(publisher._pending_thread_cleanup))
        self.assertIsNot(publisher._pending_thread_cleanup, previous)
        self.assertIsNone(publisher._pending_thread_cleanup())
        previous.assert_not_called()
        publisher.http.request.assert_not_called()
        publisher._thread_cleanup_token_provider.assert_not_called()
