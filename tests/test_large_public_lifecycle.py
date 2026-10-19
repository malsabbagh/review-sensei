"""Finite public reply qualification with real adapters and synthetic HTTP only.

This profile uses compact complete inline authority. It does not qualify rich
partitioned writer activation, deployed GitHub delivery, or a live provider.
"""

from __future__ import annotations

import hashlib
import unittest
from copy import deepcopy
from dataclasses import replace
from urllib.parse import parse_qs, urlparse

from review_sensei_github.application import GitHubApplication, GitHubWriteOptions
from review_sensei_github.conversation import ConversationPublisher
from review_sensei_github.operation_comments import ResultPartStore
from review_sensei_github.operation_host import GitHubCommentOperationStore
from review_sensei_github.publication import approval_eligibility_from_body

from review_sensei.budgets import ReviewWorkBudgets
from review_sensei.errors import ReviewInputError
from review_sensei.human_assessment import HumanReviewFinding, PendingHumanReview
from tests.fake_github_http import json_response
from tests.test_human_assessment import (
    APP,
    BASE,
    DIFF,
    HEAD,
    HUMAN,
    Broker,
    State,
    prior,
)
from tests.test_operation_entry import MemoryComments
from tests.test_review_work import AssessingProvider


class PagedState(State):
    """Honor server-side pagination while retaining every public host mutation."""

    def __init__(self, count, *, history_count, varied_identities=True):
        findings = tuple(
            HumanReviewFinding(
                (
                    hashlib.sha256(f"public-concern:{index}".encode()).hexdigest()
                    if varied_identities
                    else f"{index:064x}"
                ),
                "src/app.py",
                f"Concern {index}: confirm local-only behavior 深🙂.",
            )
            for index in range(count)
        )
        super().__init__(
            replace(prior(), human_review=PendingHumanReview(BASE, findings))
        )
        self.operation_comments = MemoryComments()
        self.operations = GitHubCommentOperationStore(
            self.operation_comments, app_user_id=42
        )
        self.operation_parts = ResultPartStore(self.operation_comments, app_user_id=42)
        self.files = [
            {"filename": "src/app.py", "patch": DIFF, "additions": 1, "deletions": 1}
        ]
        self.history = [
            {
                "id": 1000 + index,
                "issue_url": self.source["issue_url"],
                "body": f"Ordinary historical discussion {index} 深🙂.",
                "created_at": f"2026-10-10T00:{index // 60:02}:{index % 60:02}Z",
                "updated_at": "historical",
                "user": {"login": "bob", "type": "User"},
                "author_association": "MEMBER",
            }
            for index in range(history_count)
        ]
        self.page_reads = []
        self.source["created_at"] = "2026-10-11T00:00:00Z"

    def open(self, request, timeout):
        parsed = urlparse(request.full_url)
        values = None
        if request.method == "GET":
            if parsed.path.endswith("/issues/1/comments"):
                values = [*self.history, self.source, *self.replies]
            elif parsed.path.endswith("/pulls/1/reviews"):
                values = self.reviews
            elif parsed.path.endswith("/pulls/1/files"):
                values = self.files
        if values is None:
            return super().open(request, timeout)
        query = parse_qs(parsed.query)
        size = int(query.get("per_page", [100])[0])
        offset = (int(query.get("page", [1])[0]) - 1) * size
        self.calls.append((request.method, parsed.path, None))
        self.page_reads.append((parsed.path, offset, size))
        return json_response(values[offset : offset + size])

    def reply(self, provider):
        return GitHubApplication(
            broker=Broker(),
            reviewer=None,
            learner=None,
            replier=ConversationPublisher(http=self.http),
            http=self.http,
        ).generate_and_publish_reply(
            options=GitHubWriteOptions(github_writes=True, mention_replies=True),
            oidc_token="synthetic-oidc",
            read_token="read",
            repository="owner/repo",
            pull_request=1,
            source_comment_id=10,
            source_updated_at=self.source["updated_at"],
            expected_head_sha=HEAD,
            reply_provider=provider,
            model="fixture-model",
            app_slug=APP,
            root_comment_id=10,
            source_kind="issue",
            work_budgets=ReviewWorkBudgets(mode="unified"),
            operation_entry="enabled",
            operation_store=self.operations,
            operation_parts=self.operation_parts,
            repository_id=1,
            app_id=7,
            run_id="123:" + self.source["updated_at"],
        )

    def latest_inventory(self):
        return next(
            eligibility.human_review
            for review in reversed(self.reviews)
            if (eligibility := approval_eligibility_from_body(review["body"]))
            is not None
        )


class LargePublicLifecycleTests(unittest.TestCase):
    def test_enabled_accepted_partial_replay_does_not_reapply_or_refill(self):
        for count, varied in ((100, True), (250, False)):
            with self.subTest(count=count):
                state = PagedState(count, history_count=count, varied_identities=varied)
                provider = AssessingProvider()
                first = state.reply(provider)
                self.assertEqual(first.approval_status, "approval_withheld")
                self.assertEqual(len(provider.calls), 8)
                self.assertEqual(len(state.latest_inventory().pending), count - 32)
                self.assertFalse(
                    any("/reactions" in path for _, path, _ in state.calls)
                )
                snapshot = (
                    deepcopy(state.operation_comments.comments),
                    len(state.reviews),
                    state.reply_count,
                )
                restored = AssessingProvider()
                outcome = state.reply(restored)
                self.assertEqual(outcome.status, "already_replied")
                self.assertEqual(restored.calls, [])
                self.assertEqual(len(state.latest_inventory().pending), count - 32)
                self.assertEqual(
                    (
                        state.operation_comments.comments,
                        len(state.reviews),
                        state.reply_count,
                    ),
                    snapshot,
                )
                self.assertNotIn("APPROVE", state.events())

    def test_accepted_result_recovers_after_publication_interruption_without_inference(
        self,
    ):
        state = PagedState(100, history_count=250)

        def interrupt(state, method, path, body):
            if method == "POST" and path.endswith("/issues/1/comments"):
                raise RuntimeError(
                    "synthetic process interruption after accepted result"
                )

        state.hook = interrupt
        provider = AssessingProvider()
        with self.assertRaisesRegex(RuntimeError, "process interruption"):
            state.reply(provider)
        self.assertEqual(len(provider.calls), 8)
        self.assertEqual(len(state.latest_inventory().pending), 100)
        self.assertEqual(state.events(), [])
        state.hook = None
        restored = AssessingProvider()
        outcome = state.reply(restored)
        self.assertEqual(outcome.approval_status, "approval_withheld")
        self.assertEqual(restored.calls, [])
        self.assertEqual(len(state.latest_inventory().pending), 68)
        self.assertEqual(state.events(), ["COMMENT"])
        replay = AssessingProvider()
        self.assertEqual(state.reply(replay).status, "already_replied")
        self.assertEqual(replay.calls, [])
        self.assertEqual(state.events(), ["COMMENT"])

    def test_fully_resolved_enabled_delivery_replays_without_another_receipt(self):
        state = PagedState(4, history_count=250)
        self.assertEqual(state.reply(AssessingProvider()).approval_status, "approved")
        before = (len(state.reviews), state.reply_count)
        provider = AssessingProvider()
        replay = state.reply(provider)
        self.assertEqual(replay.status, "already_replied")
        self.assertEqual(replay.approval_status, "already_approved")
        self.assertEqual(provider.calls, [])
        self.assertEqual((len(state.reviews), state.reply_count), before)

    def test_accepted_replay_rejects_changed_source_evidence_and_authority(self):
        for mutation in ("body", "actor", "patch", "result", "finding"):
            with self.subTest(mutation=mutation):
                state = PagedState(100, history_count=100)
                state.reply(AssessingProvider())
                if mutation == "body":
                    state.source["body"] += " Edited without a trusted revision."
                elif mutation == "actor":
                    state.source["user"]["login"] = "bob"
                elif mutation == "patch":
                    state.files[0]["patch"] += "\n"
                else:
                    current = approval_eligibility_from_body(state.reviews[-1]["body"])
                    if mutation == "result":
                        current = replace(current, result_digest="f" * 64)
                    else:
                        inventory = current.human_review
                        inventory = replace(
                            inventory,
                            findings=(
                                replace(
                                    inventory.findings[0], body="A changed concern."
                                ),
                                *inventory.findings[1:],
                            ),
                        )
                        current = replace(current, human_review=inventory)
                    state.reviews[-1] = state.review(current, state.reviews[-1]["id"])
                snapshot = (deepcopy(state.reviews), state.reply_count)
                provider = AssessingProvider()
                with self.assertRaisesRegex(
                    ReviewInputError, "replay authority changed"
                ):
                    state.reply(provider)
                self.assertEqual(provider.calls, [])
                self.assertEqual((state.reviews, state.reply_count), snapshot)

    def test_fully_resolved_replay_rejects_changed_context_before_publication(self):
        for mutation in ("body", "actor", "patch", "result", "finding"):
            with self.subTest(mutation=mutation):
                state = PagedState(4, history_count=250)
                self.assertEqual(
                    state.reply(AssessingProvider()).approval_status, "approved"
                )
                self.assertEqual(state.latest_inventory().pending, ())
                if mutation == "body":
                    state.source["body"] += " Edited without a trusted revision."
                elif mutation == "actor":
                    state.source["user"]["login"] = "bob"
                elif mutation == "patch":
                    state.files[0]["patch"] += "\n"
                else:
                    current = next(
                        eligibility
                        for review in reversed(state.reviews)
                        if (
                            eligibility := approval_eligibility_from_body(
                                review["body"]
                            )
                        )
                        is not None
                    )
                    if mutation == "result":
                        current = replace(current, result_digest="f" * 64)
                    else:
                        inventory = current.human_review
                        inventory = replace(
                            inventory,
                            findings=(
                                replace(
                                    inventory.findings[0], body="A changed concern."
                                ),
                                *inventory.findings[1:],
                            ),
                        )
                        current = replace(current, human_review=inventory)
                    # A clean authority COMMENT is the finalizer's input; the
                    # previous APPROVE is not sufficient to authorize this root.
                    state.reviews.append(state.review(current, 9000))
                before = (
                    deepcopy(state.reviews),
                    deepcopy(state.replies),
                    deepcopy(state.check_runs),
                    deepcopy(state.operation_comments.comments),
                )
                provider = AssessingProvider()
                with self.assertRaisesRegex(
                    ReviewInputError, "replay authority changed"
                ):
                    state.reply(provider)
                self.assertEqual(provider.calls, [])
                self.assertEqual(
                    (
                        state.reviews,
                        state.replies,
                        state.check_runs,
                        state.operation_comments.comments,
                    ),
                    before,
                )

    def test_replay_after_durable_comment_interruption_keeps_one_authority(self):
        state = PagedState(100, history_count=250)

        def interrupt(state, method, path, body):
            if (
                method == "GET"
                and path.endswith("/pulls/1")
                and len(state.reviews) == 2
            ):
                raise RuntimeError("synthetic interruption after durable COMMENT")

        state.hook = interrupt
        with self.assertRaisesRegex(RuntimeError, "durable COMMENT"):
            state.reply(AssessingProvider())
        self.assertEqual(len(state.latest_inventory().pending), 68)
        self.assertEqual(state.events(), ["COMMENT"])
        state.hook = None
        provider = AssessingProvider()
        self.assertEqual(state.reply(provider).status, "already_replied")
        self.assertEqual(provider.calls, [])
        self.assertEqual(len(state.latest_inventory().pending), 68)
        self.assertEqual(state.events(), ["COMMENT"])

    def test_rich_250_inline_authority_refuses_without_claiming_supported_capacity(
        self,
    ):
        with self.assertRaisesRegex(ReviewInputError, "configured size limit"):
            PagedState(250, history_count=250)

    def test_100_250_findings_and_history_complete_over_bounded_new_mentions(self):
        for count, varied_identities in ((100, True), (250, False)):
            with self.subTest(count=count, varied_identities=varied_identities):
                state = PagedState(
                    count, history_count=count, varied_identities=varied_identities
                )
                original = state.eligibility.human_review.findings
                counts = []
                while state.latest_inventory().pending:
                    # Each edit is a distinct authenticated source revision. This
                    # is a new invocation, not a refill of an original operation.
                    revision = len(counts)
                    state.source["updated_at"] = f"revision-{revision}"
                    state.source["body"] = HUMAN + f" Complete revision {revision}."
                    provider = AssessingProvider()
                    before_calls = len(state.calls)
                    outcome = state.reply(provider)
                    current = state.latest_inventory()
                    self.assertEqual(current.findings, original)
                    counts.append(len(current.pending))
                    self.assertEqual(counts[-1], max(0, count - 32 * len(counts)))
                    self.assertLessEqual(len(provider.calls), 8)
                    self.assertLessEqual(len(state.calls) - before_calls, 60)
                    for request in provider.calls:
                        self.assertIn(state.source["body"], request.prompt)
                        self.assertLessEqual(len(request.prompt.encode()), 48 * 1024)
                    self.assertEqual(
                        outcome.approval_status,
                        "approval_withheld" if current.pending else "approved",
                    )
                    self.assertEqual(
                        state.check_runs[-1]["conclusion"],
                        "action_required" if current.pending else "success",
                    )
                    # Accepted exact-source deliveries replay without inference
                    # or duplicate authority/review/comment publication.
                    snapshot = (len(state.reviews), state.reply_count)
                    replay_provider = AssessingProvider()
                    replay = state.reply(replay_provider)
                    self.assertEqual(replay.status, "already_replied")
                    self.assertEqual(replay_provider.calls, [])
                    self.assertEqual((len(state.reviews), state.reply_count), snapshot)
                self.assertEqual(len(counts), (count + 31) // 32)
                self.assertEqual(len(state.latest_inventory().resolved), count)
                self.assertEqual(state.events().count("APPROVE"), 1)
                self.assertTrue(
                    any(
                        path.endswith("/issues/1/comments") and offset >= 100
                        for path, offset, _ in state.page_reads
                    )
                )


if __name__ == "__main__":
    unittest.main()
