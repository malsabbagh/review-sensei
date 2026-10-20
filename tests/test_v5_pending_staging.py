"""Complete v5 pending receipts reconcile once before and once after staging."""

from __future__ import annotations

import unittest
from copy import deepcopy
from dataclasses import replace
from urllib.error import URLError
from urllib.parse import urlparse

from review_sensei_github.conversation import ConversationPublisher
from review_sensei_github.default_human_inventory import HumanInventoryReference
from review_sensei_github.errors import (
    GitHubConversationError,
    GitHubConversationTransientError,
)
from review_sensei_github.publication_recovery import publication_http

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.models import ConversationReply
from tests.test_human_assessment import APP, HEAD, State, prior
from tests.test_reply_ingress_pending import inventory


class PendingState(State):
    def __init__(self):
        current = replace(
            prior(),
            human_review=inventory(250),
            human_inventory_reference=HumanInventoryReference(
                "a" * 64 + "\t" + "b" * 64, "c" * 64
            ),
        )
        super().__init__(current)
        self.lost_post = None
        self.lose_before_commit = False
        self.post_attempts = 0
        self.corrupt_final = None
        self.issue_scans = 0

    def open(self, request, timeout):
        path = urlparse(request.full_url).path
        if request.method == "GET" and path.endswith("/issues/1/comments"):
            self.issue_scans += 1
            if self.issue_scans == 2 and self.corrupt_final and self.replies:
                if self.corrupt_final == "duplicate":
                    self.replies.append({**deepcopy(self.replies[0]), "id": 999})
                elif self.corrupt_final == "edited":
                    self.replies[0]["body"] += " changed"
                elif self.corrupt_final == "missing":
                    self.replies.pop()
                elif self.corrupt_final == "replaced":
                    self.replies[0]["id"] = 999
                elif self.corrupt_final == "foreign":
                    self.replies[0]["issue_url"] = (
                        "https://api.github.test/repos/owner/repo/issues/2"
                    )
        posting = request.method == "POST" and path.endswith("/issues/1/comments")
        if posting:
            self.post_attempts += 1
            if self.post_attempts == self.lost_post and self.lose_before_commit:
                self.calls.append((request.method, path, None))
                raise URLError("synthetic missing acknowledgement")
        response = super().open(request, timeout)
        if posting:
            self.replies[-1]["issue_url"] = (
                "https://api.github.test/repos/owner/repo/issues/1"
            )
            self.replies[-1]["user"] = {"id": 99, "login": APP, "type": "Bot"}
            if self.post_attempts == self.lost_post:
                raise URLError("synthetic committed acknowledgement lost")
        return response


class V5PendingStagingTests(unittest.TestCase):
    def setUp(self):
        self.state = PendingState()
        self.budget = EvidenceReadBudget()
        self.http = publication_http(self.state.http, self.budget)
        self.publisher = ConversationPublisher(http=self.http)
        self.expected_source = deepcopy(self.state.source)
        self.expected_roots = deepcopy(self.state.reviews)
        self.fence_calls = 0
        self.fail_after = None

    def fence(self):
        self.fence_calls += 1
        _, source = self.http.request(
            "GET", "/repos/owner/repo/issues/comments/10", token="synthetic"
        )
        _, pr = self.http.request("GET", "/repos/owner/repo/pulls/1", token="synthetic")
        _, roots = self.http.request(
            "GET", "/repos/owner/repo/pulls/1/reviews", token="synthetic"
        )
        if source != self.expected_source:
            return "skipped_edited_source"
        if pr["head"]["sha"] != HEAD or roots != self.expected_roots:
            return "skipped_stale_head"
        if self.fail_after is not None and self.state.reply_count >= self.fail_after:
            return "skipped_stale_head"
        return None

    def publish(self, *, fence=None):
        return self.publisher.publish_pending(
            token="synthetic",
            repository="owner/repo",
            pull_request=1,
            source_comment_id=10,
            source_updated_at="updated",
            head_sha=HEAD,
            reply=ConversationReply("Supported original evidence.", False),
            app_slug=APP,
            root_comment_id=10,
            source_kind="issue",
            eligibility=self.state.eligibility,
            fence=fence or self.fence,
        )

    def test_multiple_pages_use_two_complete_scans_and_every_write_is_fenced(self):
        outcome = self.publish()
        self.assertEqual(outcome.status, "replied")
        self.assertGreater(self.state.reply_count, 1)
        self.assertEqual(self.state.issue_scans, 2)
        self.assertEqual(self.fence_calls, self.state.reply_count + 1)
        self.assertEqual(self.budget.calls, len(self.state.calls))
        self.assertEqual(self.state.events(), [])
        before = self.state.reply_count
        scans = self.state.issue_scans
        replay = self.publish()
        self.assertEqual(replay.status, "already_replied")
        self.assertEqual(self.state.reply_count, before)
        self.assertEqual(self.state.issue_scans - scans, 1)

    def test_lost_final_post_reconciles_complete_actual_remote_report(self):
        # Derive the page count from the same public presentation seam.
        from review_sensei_github.pending import pending_reply_pages

        pages = pending_reply_pages(
            body="Supported original evidence.",
            head_sha=HEAD,
            eligibility=self.state.eligibility,
        )
        self.state.lost_post = len(pages)
        outcome = self.publish()
        self.assertIn(outcome.status, {"replied", "already_replied"})
        self.assertEqual(self.state.reply_count, len(pages))
        count = self.state.reply_count
        self.publish()
        self.assertEqual(self.state.reply_count, count)

    def test_partial_unknown_write_holds_and_retry_stages_only_missing_pages(self):
        self.state.lost_post = 2
        self.state.lose_before_commit = True
        with self.assertRaises(GitHubConversationTransientError):
            self.publish()
        self.assertEqual(self.state.reply_count, 1)
        original = deepcopy(self.state.replies[0])
        self.state.lost_post = None
        self.assertEqual(self.publish().status, "replied")
        self.assertEqual(self.state.replies[0], original)
        self.assertEqual(
            len({item["body"] for item in self.state.replies}), len(self.state.replies)
        )
        self.assertEqual(self.state.events(), [])

    def test_duplicate_edited_foreign_or_incomplete_final_set_never_completes(self):
        for corruption in ("duplicate", "edited", "foreign", "missing", "replaced"):
            with self.subTest(corruption=corruption):
                self.setUp()
                self.state.corrupt_final = corruption
                with self.assertRaises(GitHubConversationError):
                    self.publish()
                self.assertEqual(self.state.events(), [])

    def test_changed_source_head_or_root_at_each_mutation_fence_holds(self):
        for mutation in ("source", "head", "root"):
            with self.subTest(mutation=mutation):
                self.setUp()

                def race(state, method, path, body):
                    if method == "POST" and path.endswith("/issues/1/comments"):
                        if mutation == "source":
                            state.source["body"] += " edited"
                        elif mutation == "head":
                            state.head = "d" * 40
                        else:
                            state.reviews[0]["body"] += " changed authority"

                self.state.hook = race
                outcome = self.publish()
                self.assertIn(
                    outcome.status, {"skipped_edited_source", "skipped_stale_head"}
                )
                self.assertEqual(
                    outcome.assessment_status, "pending_delivery_incomplete"
                )
                self.assertEqual(self.state.reply_count, 1)
                self.assertEqual(self.state.events(), [])

    def test_duplicate_existing_subset_refuses_before_any_new_mutation(self):
        self.fail_after = 1
        self.publish()
        self.fail_after = None
        self.state.replies.append({**deepcopy(self.state.replies[0]), "id": 999})
        posts = self.state.post_attempts
        with self.assertRaises(GitHubConversationError):
            self.publish()
        self.assertEqual(self.state.post_attempts, posts)

    def test_lost_nonfinal_committed_page_stops_then_reconciles_without_duplicate(self):
        self.state.lost_post = 1
        with self.assertRaises(GitHubConversationTransientError):
            self.publish()
        self.assertEqual(self.state.post_attempts, 1)
        self.assertEqual(self.state.reply_count, 1)
        first_id = self.state.replies[0]["id"]
        self.state.lost_post = None
        self.assertEqual(self.publish().status, "replied")
        self.assertEqual(self.state.replies[0]["id"], first_id)
        self.assertEqual(
            len({item["body"] for item in self.state.replies}), len(self.state.replies)
        )

    def test_each_staged_page_still_independently_checks_source_authorization(self):
        def late_revocation():
            outcome = self.fence()
            self.state.source["author_association"] = "NONE"
            return outcome

        result = self.publish(fence=late_revocation)
        self.assertEqual(result.status, "skipped_unauthorized")
        self.assertEqual(result.assessment_status, "pending_delivery_incomplete")
        self.assertEqual(self.state.post_attempts, 0)

    def test_final_fresh_fence_holds_complete_delivery_after_root_changes(self):
        def final_root_change(state, method, path, body):
            if (
                method == "GET"
                and path.endswith("/issues/1/comments")
                and state.issue_scans == 2
            ):
                state.reviews[0]["body"] += " authority changed after delivery"

        self.state.hook = final_root_change
        result = self.publish()
        self.assertEqual(result.status, "skipped_stale_head")
        self.assertEqual(result.assessment_status, "pending_delivery_incomplete")
        self.assertGreater(self.state.reply_count, 1)
        self.assertEqual(self.state.events(), [])

    def test_invalid_pending_stage_is_forwarded_and_refused_before_transport(self):
        with self.assertRaisesRegex(
            GitHubConversationError, "pending reply stage is invalid"
        ):
            self.publisher.publish(
                token="synthetic",
                repository="owner/repo",
                pull_request=1,
                source_comment_id=10,
                source_updated_at="updated",
                head_sha=HEAD,
                reply=ConversationReply("Supported original evidence.", False),
                app_slug=APP,
                root_comment_id=10,
                source_kind="issue",
                _pending_stage=object(),
            )
        self.assertEqual(self.state.calls, [])


if __name__ == "__main__":
    unittest.main()
