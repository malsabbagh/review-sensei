"""Pending delivery pays only for fresh fences surrounding actual authority work."""

from __future__ import annotations

import json
import unittest
from copy import deepcopy

from review_sensei_github import (
    BrokerClient,
    GitHubApplication,
    GitHubWriteOptions,
    ReviewPublisher,
)
from review_sensei_github.conversation import ConversationPublisher
from review_sensei_github.human_assessment import HumanAssessmentPublisher
from review_sensei_github.publication_recovery import publication_http

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.errors import ReviewInputError
from review_sensei.human_assessment import HumanAssessmentService
from review_sensei.models import ConversationReply
from tests.fake_github_http import json_response
from tests.test_human_assessment import APP, HEAD, Provider, State, response_for


class FeedbackFenceBudgetTests(unittest.TestCase):
    def setUp(self):
        self.state = State()
        self.budget = EvidenceReadBudget()
        self.http = publication_http(self.state.http, self.budget)
        self.publisher = ConversationPublisher(http=self.http)
        self.expected_source = deepcopy(self.state.source)
        self.expected_root = deepcopy(self.state.reviews)
        self.events = []
        self.fences = 0
        self.state.hook = lambda _state, method, path, _body: self.events.append(
            (method, path)
        )

    def fence(self):
        self.fences += 1
        self.events.append(("fence", "begin"))
        _, source = self.http.request(
            "GET", "/repos/owner/repo/issues/comments/10", token="synthetic"
        )
        _, pr = self.http.request("GET", "/repos/owner/repo/pulls/1", token="synthetic")
        _, roots = self.http.request(
            "GET", "/repos/owner/repo/pulls/1/reviews", token="synthetic"
        )
        self.events.append(("fence", "end"))
        if source != self.expected_source:
            return "skipped_edited_source"
        if pr["head"]["sha"] != HEAD or roots != self.expected_root:
            return "skipped_stale_head"
        return None

    def publish(self):
        return self.publisher.publish_pending(
            token="synthetic",
            repository="owner/repo",
            pull_request=1,
            source_comment_id=10,
            source_updated_at="updated",
            head_sha=HEAD,
            reply=ConversationReply("Supported evidence discussion.", False),
            app_slug=APP,
            root_comment_id=10,
            source_kind="issue",
            eligibility=self.state.eligibility,
            fence=self.fence,
        )

    def test_receipt_scan_precedes_first_fence_and_writes_remain_bracketed(self):
        outcome = self.publish()
        self.assertEqual(outcome.status, "replied")
        self.assertEqual(self.events[0], ("GET", "/repos/owner/repo/issues/1/comments"))
        self.assertEqual(self.fences, 2)  # Before the one page, after its readback.
        write = self.events.index(("POST", "/repos/owner/repo/issues/1/comments"))
        fence_ends = [
            index
            for index, event in enumerate(self.events)
            if event == ("fence", "end")
        ]
        self.assertLess(fence_ends[0], write)
        self.assertGreater(fence_ends[-1], write)
        self.assertEqual(self.budget.calls, len(self.state.calls))

        self.assertEqual(self.state.events(), [])

    def test_exact_receipt_replay_fences_after_scan_without_duplicate_write(self):
        self.publish()
        count = self.state.reply_count
        self.fences = 0
        self.events.clear()
        outcome = self.publish()
        self.assertEqual(outcome.status, "already_replied")
        self.assertEqual(self.events[0], ("GET", "/repos/owner/repo/issues/1/comments"))
        self.assertEqual(self.fences, 1)
        self.assertEqual(self.state.reply_count, count)
        self.assertFalse(any(method == "POST" for method, _ in self.events))
        self.assertEqual(self.budget.calls, len(self.state.calls))

    def test_source_or_root_changed_during_readonly_scan_blocks_every_write(self):
        for changed in ("source", "head", "root"):
            with self.subTest(changed=changed):
                self.setUp()

                def race(state, method, path, _body):
                    self.events.append((method, path))
                    if method == "GET" and path.endswith("/issues/1/comments"):
                        if changed == "source":
                            state.source["body"] += " edited"
                        elif changed == "head":
                            state.head = "c" * 40
                        else:
                            state.reviews[0]["body"] += " changed authority"

                self.state.hook = race
                outcome = self.publish()
                self.assertIn(
                    outcome.status, {"skipped_edited_source", "skipped_stale_head"}
                )
                self.assertEqual(self.state.reply_count, 0)
                self.assertFalse(any(method == "POST" for method, _ in self.events))
                self.assertEqual(self.budget.calls, len(self.state.calls))

    def test_assessment_delivery_defers_fresh_fence_until_after_readonly_receipt_scan(
        self,
    ):
        assessor = HumanAssessmentPublisher(http=self.http, evidence_budget=self.budget)
        prepared = assessor.prepare(
            token="synthetic",
            repository="owner/repo",
            pull_request=1,
            prepared=self.state.prepared(),
            app_slug=APP,
        )
        reply = HumanAssessmentService(
            Provider(response_for(self.state.eligibility))
        ).reply(
            context=prepared.conversation.context,
            pending=prepared.eligibility.human_review,
            source_body=prepared.source_body,
        )
        self.events.clear()
        outcome = assessor.publish(
            token="synthetic",
            review_token="synthetic-review",
            repository="owner/repo",
            pull_request=1,
            prepared=prepared,
            reply=reply,
            app_slug=APP,
            check_token="synthetic-check",
        )
        self.assertEqual(outcome.status, "replied")
        self.assertEqual(self.events[0], ("GET", "/repos/owner/repo/issues/1/comments"))
        reply_write = self.events.index(("POST", "/repos/owner/repo/issues/1/comments"))
        # All mutable authorities are fresh before this first visible mutation.
        before = self.events[:reply_write]
        self.assertIn(("GET", "/repos/owner/repo/issues/comments/10"), before)
        self.assertIn(("GET", "/repos/owner/repo/pulls/1/reviews"), before)
        self.assertIn(("GET", "/repos/owner/repo/pulls/1"), before)
        authority_write = self.events.index(
            ("POST", "/repos/owner/repo/pulls/1/reviews")
        )
        between = self.events[reply_write + 1 : authority_write]
        self.assertIn(("GET", "/repos/owner/repo/issues/comments/10"), between)
        self.assertIn(("GET", "/repos/owner/repo/pulls/1/reviews"), between)
        self.assertIn(("GET", "/repos/owner/repo/pulls/1"), between)
        self.assertEqual(self.budget.calls, len(self.state.calls))


class ReplyBrokerBudgetTests(unittest.TestCase):
    def reply(self, *, expire_after_review=False):
        self.state = State()
        self.clock = [0.0]
        self.budget = EvidenceReadBudget(clock=lambda: self.clock[0])
        self.broker_calls = []

        def exchange(request, timeout):
            capability = json.loads(request.data)["capability"]
            self.broker_calls.append((capability, timeout))
            if expire_after_review and capability == "review_publish":
                self.clock[0] = 60.0
            return json_response({"token": "synthetic"})

        application = GitHubApplication(
            broker=BrokerClient(opener=exchange),
            http=self.state.http,
            reviewer=ReviewPublisher(http=self.state.http),
            learner=None,
            replier=ConversationPublisher(http=self.state.http),
        )
        return application.generate_and_publish_reply(
            options=GitHubWriteOptions(github_writes=True, mention_replies=True),
            oidc_token="synthetic",
            read_token="read",
            repository="owner/repo",
            pull_request=1,
            source_comment_id=10,
            source_updated_at="updated",
            expected_head_sha=HEAD,
            reply_provider=Provider(response_for(self.state.eligibility)),
            model="fixture",
            app_slug=APP,
            root_comment_id=10,
            source_kind="issue",
            evidence_budget=self.budget,
        )

    def test_every_real_broker_capability_dispatch_shares_the_http_allowance(self):
        outcome = self.reply()
        self.assertEqual(outcome.approval_status, "approved")
        self.assertEqual(
            [name for name, _ in self.broker_calls],
            ["issue_reply", "review_publish", "check_publish"],
        )
        self.assertEqual(
            self.budget.calls, len(self.state.calls) + len(self.broker_calls)
        )
        self.assertLessEqual(self.budget.calls, 60)
        self.assertTrue(all(0 < timeout <= 30 for _, timeout in self.broker_calls))

    def test_expired_original_deadline_prevents_check_capability_dispatch(self):
        with self.assertRaises(ReviewInputError):
            self.reply(expire_after_review=True)
        self.assertEqual(
            [name for name, _ in self.broker_calls], ["issue_reply", "review_publish"]
        )
        self.assertNotIn("APPROVE", self.state.events())


if __name__ == "__main__":
    unittest.main()
