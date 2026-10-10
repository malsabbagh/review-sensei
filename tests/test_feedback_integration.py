"""Explicit rich feedback through the public hosted adapter; synthetic host only."""

import unittest
from copy import deepcopy
from dataclasses import replace

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.errors import ReviewInputError
from review_sensei.feedback import (
    FeedbackAdmissionError,
    FeedbackReference,
    FeedbackSelector,
)
from review_sensei.hosting.github.errors import GitHubConversationError
from review_sensei.hosting.github.human_assessment import HumanAssessmentPublisher
from review_sensei.human_assessment import HumanAssessmentService
from tests.test_human_assessment import APP, HUMAN, Provider, State, response_for


class PublicFeedbackTests(unittest.TestCase):
    def setup_rich(self, *, length=4097, sources=1):
        self.state = State()
        self.state.source["user"]["id"] = 42
        self.state.source["body"] = HUMAN + " " + "x" * (length - len(HUMAN) - 1)
        self.budget = EvidenceReadBudget()
        self.guard = self.budget.consume
        self.publisher = HumanAssessmentPublisher(
            http=self.state.http, before_read=self.guard
        )
        selector = None
        if sources == 2:
            self.other = deepcopy(self.state.source)
            self.other.update(id=11, body="Additional original complete explanation.")
            self.other["user"] = {"login": "bob", "type": "User", "id": 43}
            original = self.state.http.request

            def request(method, path, **kwargs):
                if method == "GET" and path.endswith("/issues/comments/11"):
                    self.state.calls.append((method, path, None))
                    return 200, self.other
                return original(method, path, **kwargs)

            self.state.http.request = request
            selector = FeedbackSelector(
                "owner/repo",
                1,
                self.state.base,
                self.state.head,
                FeedbackReference("issue", 10, "updated"),
                (
                    FeedbackReference("issue", 10, "updated"),
                    FeedbackReference("issue", 11, "updated"),
                ),
                (self.state.eligibility.human_review.pending[0].fingerprint,),
            )
        self.prepared = self.publisher.prepare_feedback(
            token="synthetic",
            repository="owner/repo",
            pull_request=1,
            prepared=self.state.prepared(),
            app_slug=APP,
            selector=selector,
        )
        self.assertIsNotNone(self.prepared)
        self.assertEqual(self.budget.calls, len(self.state.calls))

    def publish(self):
        provider = Provider(response_for(self.state.eligibility))
        reply = HumanAssessmentService(provider).reply(
            context=self.prepared.conversation.context,
            pending=self.prepared.eligibility.human_review,
            source_body=self.prepared.source_body,
            feedback=self.prepared.feedback,
        )
        self.assertIn(self.state.source["body"], provider.calls[0].prompt)
        return self.publisher.publish(
            token="synthetic",
            review_token="synthetic",
            repository="owner/repo",
            pull_request=1,
            prepared=self.prepared,
            reply=reply,
            app_slug=APP,
        )

    def test_default_trigger_keeps_complete_body_and_charges_entire_publication(self):
        self.setup_rich()
        original = self.prepared.feedback
        self.assertEqual(original.sources[0].body, self.state.source["body"])
        self.assertEqual(len(original.sources), 1)
        self.assertEqual(self.publish().status, "replied")
        self.assertEqual(self.state.events(), ["COMMENT", "APPROVE"])
        self.assertEqual(self.budget.calls, len(self.state.calls))
        self.assertEqual(original.digest, self.prepared.feedback.digest)

    def test_multisource_edit_at_second_fence_cannot_activate_assessment(self):
        self.setup_rich(length=500, sources=2)
        source_reads = sum(
            path.endswith("/issues/comments/11") for _, path, _ in self.state.calls
        )
        self.assertEqual(source_reads, 2)  # admission and direct prepare authentication

        def edit_after_reply(state, method, path, body):
            if method == "POST" and path.endswith("/issues/1/comments"):
                self.other["body"] += " edited"

        self.state.hook = edit_after_reply
        with self.assertRaises(FeedbackAdmissionError) as caught:
            self.publish()
        self.assertEqual(caught.exception.diagnostic, "feedback_source_changed")
        self.assertEqual(self.state.events(), [])
        self.assertEqual(self.state.reply_count, 1)
        self.assertEqual(self.budget.calls, len(self.state.calls))

    def test_missing_or_revoked_secondary_source_refuses_before_reply(self):
        for field, value in (("author_association", "NONE"), ("updated_at", "edited")):
            with self.subTest(field=field):
                self.setup_rich(length=500, sources=2)
                self.other[field] = value
                with self.assertRaises(FeedbackAdmissionError):
                    self.publish()
                self.assertEqual(self.state.reply_count, 0)
                self.assertEqual(self.state.events(), [])

    def test_original_budget_exhaustion_before_publication_preserves_obligations(self):
        self.setup_rich()
        before = len(self.state.calls)
        self.budget.calls = 60
        with self.assertRaises(ReviewInputError):
            self.publish()
        self.assertEqual(len(self.state.calls), before)
        self.assertEqual(self.state.events(), [])
        self.assertTrue(self.prepared.eligibility.human_review.pending)

    def test_rich_route_requires_budget_and_legacy_limit_is_preserved(self):
        self.setup_rich()
        legacy = HumanAssessmentPublisher(http=self.state.http)
        with self.assertRaises(GitHubConversationError):
            legacy.prepare(
                token="synthetic",
                repository="owner/repo",
                pull_request=1,
                prepared=self.state.prepared(),
                app_slug=APP,
            )
        before = len(self.state.calls)
        with self.assertRaises(FeedbackAdmissionError):
            legacy.load_selected_feedback(
                token="synthetic",
                repository="owner/repo",
                pull_request=1,
                prepared=self.state.prepared(),
                app_slug=APP,
            )
        self.assertEqual(len(self.state.calls), before)

    def test_context_mismatch_cannot_reuse_selection(self):
        self.setup_rich()
        before = len(self.state.calls)
        selector = FeedbackSelector(
            "other/repo",
            1,
            self.state.base,
            self.state.head,
            self.prepared.feedback.trigger,
            tuple(item.reference for item in self.prepared.feedback.sources),
        )
        with self.assertRaises(FeedbackAdmissionError):
            self.publisher.load_selected_feedback(
                token="synthetic",
                repository="owner/repo",
                pull_request=1,
                prepared=self.state.prepared(),
                app_slug=APP,
                selector=selector,
            )
        self.assertEqual(len(self.state.calls), before)
        with self.assertRaises(FeedbackAdmissionError):
            replace(
                self.prepared,
                feedback=replace(self.prepared.feedback, head_sha="c" * 40),
            )


if __name__ == "__main__":
    unittest.main()
