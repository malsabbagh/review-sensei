"""P1 assessment coalescing and physical current-run caller qualification.

Four pending decisions still share one provider request and two durable saves.
Hosted accounting uses the repository-wide 512 candidate default through the
current complete CLI lifecycle, including bounded history reads and replay.
"""

from __future__ import annotations

import copy
import tempfile
import unittest
from unittest.mock import patch

import test_coalesced_admission as coalesced
from review_sensei_github.api_run_plan import NATIVE_OPERATION_REQUESTS
from review_sensei_github.publication import parse_finding_index
from review_sensei_github.session_ledger import (
    GitHubIssueCommentSessionLedger,
    parse_session_comment,
)
from test_api_work_lifecycle import caller_measurement
from test_api_work_persistence import HEAD, IDENTITY
from test_assessment_queue import DocumentStore, fixture
from test_default_publication_cli_journey import current_paged_entry
from test_review_work import AssessingProvider

from review_sensei.api_work import ApiRunAccounting
from review_sensei.bounded_evidence import MAX_PART_READS, PART_READ_SECONDS
from review_sensei.budgets import ReviewWorkBudgets
from review_sensei.configuration import GitHubSection
from review_sensei.outcomes import ResourceBudget, ResourceBudgetTracker


class P1PublicProfileTests(unittest.TestCase):
    def test_p1_assessment_coalesces_four_pending_decisions_without_extra_provider(
        self,
    ):
        self.assertEqual(MAX_PART_READS, 64)
        self.assertEqual(PART_READ_SECONDS, 60.0)
        self.assertEqual(GitHubSection().operation_entry, "enabled")
        pending, bundle, queue = fixture(4)
        store = DocumentStore()
        provider = AssessingProvider()
        case = coalesced.CoalescedAdmissionTests()
        work = case.run_work(
            pending,
            bundle,
            queue,
            provider=provider,
            checkpoint=case.checkpoint(store),
            tracker=ResourceBudgetTracker(ResourceBudget.create(max_provider_calls=1)),
            work_budgets=ReviewWorkBudgets(mode="unified"),
        )
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(len(store.writes), 2)
        self.assertEqual(len(work.reply.decisions), 4)
        self.assertTrue(store.document["completed"])
        self.assertEqual(store.document["counters"]["provider_calls"], 1)

    def _entry(self, directory, comments):
        current = current_paged_entry(directory)
        current.provider_comments = [
            {
                "path": "src/app.py",
                "line": None,
                "side": "FILE",
                "body": f"Optional finding {number} retains its explanation.",
                "severity": "low",
                "blocking": False,
                "needs_human": False,
            }
            for number in range(4)
        ]
        for identifier, body, bot in comments:
            current.extra_comments[identifier] = {
                "id": identifier,
                "body": body,
                "user": {
                    "id": 999 if bot else 7,
                    "type": "Bot" if bot else "User",
                    "login": "reviewsensei[bot]" if bot else "dev",
                },
                "updated_at": "2019-01-01T00:00:00Z",
                "issue_url": "https://api.github.test/repos/owner/repo/issues/7",
            }
        return current

    def _assert_complete(self, current):
        self.assertEqual(current.provider_calls, 1)
        record = parse_session_comment(current.comment["body"], identity=IDENTITY)
        self.assertEqual(record.transaction.phase, "publication_succeeded")
        accounting = ApiRunAccounting.from_document(record.api_run_accounting)
        self.assertEqual(accounting.configuration.limit, 512)
        self.assertEqual(
            accounting.terminal_transaction, record.transaction.transaction_id
        )
        roots = [item for item in current.reviews if item["state"] == "COMMENTED"]
        self.assertEqual(len(roots), 1)
        index = parse_finding_index(
            roots[0]["body"], repository_id=123, pull_request=7, head_sha=HEAD
        )
        self.assertEqual(len(index.entries), 4)
        self.assertEqual(
            sum(item["state"] == "APPROVED" for item in current.reviews), 1
        )
        measured = caller_measurement(current)
        self.assertEqual(
            measured["physical_attempts"],
            sum(len(transport.observations) for transport in current.transports),
        )
        self.assertLessEqual(measured["physical_attempts"], 512)
        self.assertEqual(measured["terminal_owner"], "publication-completion")

    def test_current_p1_retained_result_replay_does_not_reinfer_or_duplicate_approval(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            current = self._entry(directory, [])

            def interrupted():
                raise KeyboardInterrupt()

            current.before_publication = interrupted
            self.assertEqual(current.invoke(), 130, current.error)
            pending = parse_session_comment(current.comment["body"], identity=IDENTITY)
            old = ApiRunAccounting.from_document(pending.api_run_accounting)
            retained = copy.deepcopy(current.extra_comments)
            current.before_publication = None
            current.attempt = 2
            self.assertEqual(current.invoke(), 0, current.error)
            self._assert_complete(current)
            accounting = current.accounted.accounting
            self.assertEqual(accounting.identity, old.identity)
            self.assertEqual(accounting.configuration, old.configuration)
            self.assertEqual(accounting.startup, old.startup)
            self.assertEqual(accounting.blocks[: len(old.blocks)], old.blocks)
            for key, value in retained.items():
                self.assertEqual(current.extra_comments[key], value)

    def test_ordinary_comment_threads_complete_inside_the_repository_limit(self):
        for count in (25, 300):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as directory:
                comments = [(3000 + index, "note", False) for index in range(count)]
                current = self._entry(directory, comments)
                self.assertEqual(current.invoke(), 0, current.error)
                self._assert_complete(current)
                for identifier, body, _ in comments:
                    self.assertEqual(current.extra_comments[identifier]["body"], body)

    def test_maximum_size_comments_adapt_to_larger_pages_within_a_bounded_walk(self):
        with tempfile.TemporaryDirectory() as directory:
            comments = [
                (3000 + index, "m" * 60_000 if index < 10 else "note", index < 10)
                for index in range(24)
            ]
            current = self._entry(directory, comments)
            self.assertEqual(current.invoke(), 0, current.error)
            self._assert_complete(current)
            # The initial complete walk shrinks to5, then grows to10 and ends.
            # Larger100 pages in later walks must not masquerade as evidence of
            # growth within this particular operation.
            self.assertEqual(
                current.comment_page_reads[:8],
                [
                    (100, 1),
                    (50, 1),
                    (25, 1),
                    (10, 1),
                    (5, 1),
                    (5, 2),
                    (10, 2),
                    (10, 3),
                ],
            )

    def test_original_300_comment_profile_refuses_at_finite_walk_without_effects(self):
        # Preserve the exact old P1 input. Its previous expected success under
        # 60ordinary/64total is not qualified for the current10attempt operation.
        with tempfile.TemporaryDirectory() as directory:
            comments = [
                (
                    3000 + index,
                    "m" * 60_000 if 100 <= index < 120 else "note",
                    100 <= index < 120,
                )
                for index in range(300)
            ]
            current = self._entry(directory, comments)
            before = copy.deepcopy(current.extra_comments)
            loads = []
            original_load = GitHubIssueCommentSessionLedger.load

            def observed_load(ledger, *args, **kwargs):
                result = original_load(ledger, *args, **kwargs)
                loads.append(result)
                return result

            with patch.object(
                GitHubIssueCommentSessionLedger, "load", new=observed_load
            ):
                self.assertEqual(current.invoke(), 1)
            self.assertIn("previous caller root cannot be authenticated", current.error)
            self.assertEqual(len(loads), 1)
            self.assertEqual(loads[0].status, "integrity-failed")
            self.assertEqual(
                loads[0].detail, "session comment discovery exceeded bound"
            )
            self.assertEqual(NATIVE_OPERATION_REQUESTS, 10)
            self.assertEqual(
                current.comment_page_reads,
                [
                    (100, 1),
                    (100, 2),
                    (50, 3),
                    (25, 5),
                    (10, 11),
                    (5, 21),
                    (5, 22),
                    (10, 12),
                    (5, 23),
                    (5, 24),
                ],
            )
            self.assertEqual(current.provider_calls, 0)
            self.assertIsNone(current.comment)
            self.assertEqual(current.extra_comments, before)
            self.assertEqual(current.reviews, [])
            self.assertEqual(current.check_runs, [])
            self.assertFalse(
                any(
                    method in {"POST", "PATCH", "PUT", "DELETE"}
                    and path.startswith("/repos/")
                    for _, method, path in current.trace
                )
            )
            observed = sum(
                len(transport.observations) for transport in current.transports
            )
            self.assertEqual(len(current.trace), observed)
            self.assertEqual(observed, 19)
            self.assertEqual(current.accounted.startup_dispatches, observed)
            self.assertLess(observed, len(current.accounted._slot.requests))
            self.assertLess(current.accounted.plan.liability, 512)
            self.assertIsNone(current.accounted._state)
