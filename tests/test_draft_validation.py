"""Exact live-draft validation at publication consumers and hosted routes."""

from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from review_sensei_github import (
    ConversationPublisher,
    GitHubConversationError,
    GitHubPublicationError,
    ReviewPublisher,
)
from review_sensei_github.publication import ReviewApprovalFinalizer

from review_sensei.models import ConversationReply

try:
    from fake_github_http import json_response, make_http
    from test_action_reply_entry import ReplyEntryFixture
    from test_github_conversation import ISSUE_URL
    from test_github_conversation import pr_payload as reply_pr
    from test_github_publication import pr_payload as review_pr
    from test_review_run import EntryFixture
except ModuleNotFoundError:
    from tests.fake_github_http import json_response, make_http
    from tests.test_action_reply_entry import ReplyEntryFixture
    from tests.test_github_conversation import ISSUE_URL
    from tests.test_github_conversation import pr_payload as reply_pr
    from tests.test_github_publication import pr_payload as review_pr
    from tests.test_review_run import EntryFixture


ABSENT = object()
MALFORMED_DRAFTS = (
    ("absent", ABSENT),
    ("null", None),
    ("zero", 0),
    ("one", 1),
    ("string-false", "false"),
    ("string-true", "true"),
)
HEAD = "b" * 40
BASE = "a" * 40


def draft_payload(payload, value):
    if value is ABSENT:
        payload.pop("draft", None)
    else:
        payload["draft"] = value
    return payload


class _DraftResponses:
    """Change only fake live PR JSON, preserving original physical accounting."""

    draft_value = False
    draft_armed = False
    arm_after_comment = False

    def external(self, request, timeout):
        response = super().external(request, timeout)
        path = urlsplit(request.full_url).path
        if (
            self.arm_after_comment
            and request.get_method() == "POST"
            and path.endswith("/pulls/7/reviews")
            and json.loads(request.data).get("event") == "COMMENT"
            and response.status == 200
        ):
            self.draft_armed = True
        if (
            self.draft_armed
            and request.get_method() == "GET"
            and path.endswith("/pulls/7")
        ):
            payload = json.loads(response.read(524288))
            self.draft_reads.append(len(self.reviews))
            return json_response(draft_payload(payload, self.draft_value))
        return response


class DraftReviewFixture(_DraftResponses, EntryFixture):
    pass


class DraftReplyFixture(_DraftResponses, ReplyEntryFixture):
    pass


def hosted_fixture(directory, value, *, phase):
    fixture = (DraftReplyFixture if phase == "reply" else DraftReviewFixture)(directory)
    fixture.draft_value = value
    fixture.draft_reads = []
    if phase == "bootstrap":
        fixture.draft_armed = True
    elif phase == "publication":
        fixture.before_publication = lambda: setattr(fixture, "draft_armed", True)
    elif phase == "finalization":
        fixture.arm_after_comment = True
    return fixture


def invoke_reply_with_late_draft(fixture):
    original = ConversationPublisher._publish

    def publish(publisher, **arguments):
        fixture.draft_armed = True
        return original(publisher, **arguments)

    with patch.object(ConversationPublisher, "_publish", publish):
        return fixture.invoke_reply()


class ComponentDraftValidationTests(unittest.TestCase):
    def review_preflight(self, value, *, finalization=False):
        http, calls = make_http(
            json_response(draft_payload(review_pr(head_sha=HEAD), value))
        )
        arguments = dict(
            token="synthetic-token",
            repository="owner/repo",
            pull_request=2,
            head_sha=HEAD,
            app_slug="reviewsensei[bot]",
        )
        if finalization:
            result = ReviewApprovalFinalizer(http=http)._preflight(**arguments)
        else:
            result = ReviewPublisher(http=http)._preflight_pr(
                **arguments, repository_id=1, base_branch="main", base_sha=BASE
            )
        return result, calls

    def test_publication_preflight_rejects_malformed_draft(self):
        for label, value in MALFORMED_DRAFTS:
            with self.subTest(draft=label):
                with self.assertRaisesRegex(
                    GitHubPublicationError, "^review preflight draft was invalid$"
                ):
                    self.review_preflight(value)

    def test_finalization_preflight_rejects_malformed_draft(self):
        for label, value in MALFORMED_DRAFTS:
            with self.subTest(draft=label):
                with self.assertRaisesRegex(
                    GitHubPublicationError,
                    "^approval finalization preflight draft was invalid$",
                ):
                    self.review_preflight(value, finalization=True)

    def test_exact_booleans_preserve_both_review_preflight_results(self):
        for finalization in (False, True):
            for draft in (False, True):
                with self.subTest(finalization=finalization, draft=draft):
                    result, calls = self.review_preflight(
                        draft, finalization=finalization
                    )
                    self.assertEqual(
                        None if result.result is None else result.result.status,
                        "skipped_pr_state" if draft else None,
                    )
                    self.assertTrue(all(call[0] == "GET" for call in calls))

    def reply_publication(self, value):
        http, calls = make_http(
            [
                json_response(
                    {
                        "id": 10,
                        "issue_url": ISSUE_URL,
                        "body": "@sensei please explain",
                        "user": {"login": "alice", "type": "User"},
                        "author_association": "MEMBER",
                        "updated_at": "2026-11-09T00:00:00Z",
                    }
                ),
                json_response([]),
                json_response(draft_payload(reply_pr(HEAD), value)),
                json_response({"id": 12}, 201),
            ]
        )
        publisher = ConversationPublisher(http=http)
        arguments = dict(
            token="synthetic-token",
            repository="owner/repo",
            pull_request=1,
            source_comment_id=10,
            source_updated_at="2026-11-09T00:00:00Z",
            head_sha=HEAD,
            reply=ConversationReply.from_dict({"body": "Synthetic explanation."}),
            app_slug="reviewsensei[bot]",
            root_comment_id=10,
            source_kind="issue",
        )
        return publisher, arguments, calls

    def test_reply_publication_rejects_malformed_draft_before_post(self):
        for label, value in MALFORMED_DRAFTS:
            with self.subTest(draft=label):
                publisher, arguments, calls = self.reply_publication(value)
                with self.assertRaisesRegex(
                    GitHubConversationError, "^reply PR preflight draft was invalid$"
                ):
                    publisher.publish(**arguments)
                self.assertTrue(all(call[0] == "GET" for call in calls))

    def test_exact_boolean_reply_behavior_is_preserved(self):
        for draft in (False, True):
            with self.subTest(draft=draft):
                publisher, arguments, calls = self.reply_publication(draft)
                result = publisher.publish(**arguments)
                self.assertEqual(
                    result.status, "skipped_pr_state" if draft else "replied"
                )
                self.assertEqual(
                    sum(call[0] == "POST" for call in calls), 0 if draft else 1
                )


class HostedDraftValidationTests(unittest.TestCase):
    def test_malformed_startup_is_blocked_by_existing_strict_admission(self):
        for route in ("review", "reply"):
            for label, value in (*MALFORMED_DRAFTS, ("true", True)):
                with (
                    self.subTest(route=route, draft=label),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    fixture = (
                        hosted_fixture(directory, value, phase="bootstrap")
                        if route == "review"
                        else DraftReplyFixture(directory)
                    )
                    fixture.draft_value, fixture.draft_armed, fixture.draft_reads = (
                        value,
                        True,
                        [],
                    )
                    code = (
                        fixture.invoke()
                        if route == "review"
                        else fixture.invoke_reply()
                    )
                    self.assertEqual(code, 1)
                    self.assertEqual(fixture.provider_calls, 0)
                    self.assertFalse(fixture.reviews)
                    self.assertEqual(getattr(fixture, "reply_posts", 0), 0)
                    self.assertTrue(fixture.draft_reads)

    def test_malformed_after_analysis_blocks_review_publication(self):
        for label, value in MALFORMED_DRAFTS:
            with self.subTest(draft=label), tempfile.TemporaryDirectory() as directory:
                fixture = hosted_fixture(directory, value, phase="publication")
                self.assertEqual(fixture.invoke(), 1, fixture.error)
                self.assertEqual(fixture.provider_calls, 1)
                self.assertFalse(fixture.reviews)
                self.assertTrue(fixture.draft_reads)
                self.assertTrue(all(count == 0 for count in fixture.draft_reads))

    def test_malformed_after_review_post_blocks_only_later_approval(self):
        for label, value in MALFORMED_DRAFTS:
            with self.subTest(draft=label), tempfile.TemporaryDirectory() as directory:
                fixture = hosted_fixture(directory, value, phase="finalization")
                self.assertEqual(fixture.invoke(), 1, fixture.error)
                self.assertEqual(fixture.provider_calls, 1)
                self.assertEqual(
                    [review["state"] for review in fixture.reviews], ["COMMENTED"]
                )
                self.assertTrue(fixture.draft_reads)
                self.assertTrue(all(count == 1 for count in fixture.draft_reads))

    def test_malformed_at_last_reply_gate_blocks_the_business_reply(self):
        for label, value in MALFORMED_DRAFTS:
            with self.subTest(draft=label), tempfile.TemporaryDirectory() as directory:
                fixture = hosted_fixture(directory, value, phase="reply")
                self.assertEqual(
                    invoke_reply_with_late_draft(fixture), 1, fixture.error
                )
                self.assertEqual(fixture.provider_calls, 1)
                self.assertEqual(fixture.reply_posts, 0)
                self.assertFalse(fixture.resolved_threads)
                self.assertTrue(fixture.draft_reads)

    def test_reply_finalizer_is_an_earlier_guard_for_post_inference_faults(self):
        for label, value in MALFORMED_DRAFTS:
            with self.subTest(draft=label), tempfile.TemporaryDirectory() as directory:
                fixture = hosted_fixture(directory, value, phase="reply")

                def response(_request):
                    fixture.draft_armed = True
                    return fixture.reply_document

                fixture.provider_callback = response
                self.assertEqual(fixture.invoke_reply(), 1, fixture.error)
                self.assertEqual(fixture.provider_calls, 1)
                self.assertEqual(fixture.reply_posts, 0)
                self.assertTrue(fixture.draft_reads)

    def test_exact_booleans_preserve_complete_hosted_route_behavior(self):
        for phase in ("publication", "finalization", "reply"):
            for draft in (False, True):
                with (
                    self.subTest(phase=phase, draft=draft),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    fixture = hosted_fixture(directory, draft, phase=phase)
                    code = (
                        invoke_reply_with_late_draft(fixture)
                        if phase == "reply"
                        else fixture.invoke()
                    )
                    self.assertEqual(
                        code,
                        1 if draft and phase != "finalization" else 0,
                        fixture.error,
                    )
                    self.assertEqual(fixture.provider_calls, 1)
                    self.assertTrue(fixture.draft_reads)
                    if phase == "reply":
                        self.assertEqual(fixture.reply_posts, 0 if draft else 1)
                    else:
                        self.assertEqual(
                            sum(
                                review["state"] == "APPROVED"
                                for review in fixture.reviews
                            ),
                            0 if draft else 1,
                        )
                        self.assertEqual(
                            sum(
                                review["state"] == "COMMENTED"
                                for review in fixture.reviews
                            ),
                            0 if draft and phase == "publication" else 1,
                        )
