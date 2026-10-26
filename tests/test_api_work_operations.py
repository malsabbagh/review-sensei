"""Real physical caller stops, preserving enumeration and uncertain write tails."""

from __future__ import annotations

import tempfile
import unittest

from review_sensei_github.api_run_requests import ApiRunSessionLedger
from review_sensei_github.caller_operation import CallerOperation
from review_sensei_github.errors import (
    GitHubHTTPPaginationLimitError,
    GitHubHTTPTransientError,
)
from review_sensei_github.operation_comments import GitHubIssueCommentOperationPort
from review_sensei_github.operation_host import OperationWriteUnconfirmed
from review_sensei_github.session_ledger import parse_session_comment

from review_sensei.errors import ReviewInputError
from tests.test_api_work_persistence import (
    IDENTITY,
    QUALIFICATION_LIMIT,
    StartupFixture,
)


class GrowingAfterWriteFixture(StartupFixture):
    grow_after_patch = False

    def _external_open(self, request, timeout):
        result = super()._external_open(request, timeout)
        if self.grow_after_patch and request.method == "PATCH":
            # Fresh readback must enumerate the changed complete history, not
            # treat the earlier short listing as an enduring freshness proof.
            self.history_count = 199
        return result


class CallerOperationBoundaryTests(unittest.TestCase):
    def prepare(self, directory, fixture_type=StartupFixture):
        fixture = fixture_type(directory, limit=QUALIFICATION_LIMIT)
        fixture.initial()
        fixture.recover(reserve_successor=True)
        return fixture

    def test_history_growth_exhausts_shared_scan_allocation_before_patch(self):
        with tempfile.TemporaryDirectory() as directory:
            f = self.prepare(directory)
            old = f.comment["body"]
            f.history_count = 600
            before = len(f.trace)
            with self.assertRaisesRegex(
                ReviewInputError, "preserve write confirmation"
            ):
                f.accounted.replace_checkpoint(f.ledger, IDENTITY)
            self.assertEqual(len(f.trace) - before, 7)
            self.assertTrue(all(method == "GET" for _, method, _ in f.trace[before:]))
            self.assertEqual(f.comment["body"], old)
            self.assertIsNone(f.accounted._checkpoint)
            self.assertEqual(f.accounted.accounting.confirmed, 7)
            self.assertEqual(f.accounted.usable, 24)
            with self.assertRaisesRegex(ReviewInputError, "cannot be replenished"):
                f.accounted.replace_checkpoint(f.ledger, IDENTITY)
            self.assertEqual(len(f.trace) - before, 7)

    def test_exact_full_page_includes_final_empty_confirmation_request(self):
        with tempfile.TemporaryDirectory() as directory:
            f = self.prepare(directory)
            f.history_count = 99  # plus the authenticated marker = 100
            before = len(f.trace)
            updated = f.accounted.replace_checkpoint(f.ledger, IDENTITY)
            self.assertEqual(len(f.trace) - before, 7)
            self.assertEqual(f.list_requests[-2:], [(2, 100, 1), (2, 100, 2)])
            self.assertEqual(
                parse_session_comment(f.comment["body"], identity=IDENTITY), updated
            )
            self.assertIsNone(f.accounted._checkpoint)

    def test_post_write_history_growth_retains_uncertainty_without_new_confirmation_pool(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            f = self.prepare(directory, GrowingAfterWriteFixture)
            f.grow_after_patch = True
            before = len(f.trace)
            with self.assertRaisesRegex(
                ReviewInputError, "confirmation allocation exhausted"
            ):
                f.accounted.replace_checkpoint(f.ledger, IDENTITY)
            self.assertEqual(len(f.trace) - before, 5)
            self.assertEqual(sum(m == "PATCH" for _, m, _ in f.trace[before:]), 1)
            intent = f.accounted._checkpoint
            self.assertIsNotNone(intent)
            self.assertTrue(f.accounted._checkpoint_operation.write_attempted)
            self.assertFalse(f.accounted._checkpoint_operation.confirmed)
            self.assertEqual(f.accounted.accounting.confirmed, 5)
            self.assertEqual(
                f.accounted.accounting.uncertain, 0
            )  # HTTP responses known; publication unconfirmed
            deadline = [o["deadline_unix_ms"] for o in f.accounted.observations]
            for action in (
                lambda: f.accounted.replace_checkpoint(f.ledger, IDENTITY),
                lambda: f.accounted.reconcile_checkpoint(f.ledger, IDENTITY),
            ):
                with self.assertRaises(ReviewInputError):
                    action()
            self.assertIs(f.accounted._checkpoint, intent)
            self.assertEqual(len(f.trace) - before, 5)
            self.assertEqual(
                deadline, [o["deadline_unix_ms"] for o in f.accounted.observations]
            )

    def test_read_allocation_counts_last_page_and_refuses_incomplete_history(self):
        for history, succeeds in ((998, True), (999, False)):
            with (
                self.subTest(history=history),
                tempfile.TemporaryDirectory() as directory,
            ):
                f = self.prepare(directory)
                f.history_count = history
                before = len(f.trace)
                if succeeds:
                    rows = f.http.paginate(
                        path="/repos/owner/repo/issues/7/comments", token="synthetic"
                    )
                    self.assertEqual(len(rows), 999)
                else:
                    with self.assertRaises(GitHubHTTPPaginationLimitError):
                        f.http.paginate(
                            path="/repos/owner/repo/issues/7/comments",
                            token="synthetic",
                        )
                self.assertEqual(len(f.trace) - before, 10)
                self.assertEqual(f.accounted.accounting.confirmed, 10)
                self.assertEqual(f.accounted.usable, 21)

    def test_oversized_page_and_nested_reader_share_one_physical_allocation(self):
        with tempfile.TemporaryDirectory() as directory:
            f = self.prepare(directory)
            f.history_count = 48
            f.oversized_first_page = True
            before = len(f.trace)
            ledger = ApiRunSessionLedger(f.ledger, f.accounted, IDENTITY)
            # Native reader's distinct adaptive ladder and outer load are nested.
            loaded = ledger.load(IDENTITY)
            self.assertEqual(loaded.status, "ok")
            self.assertEqual(len(f.trace) - before, 4)
            self.assertEqual(
                f.list_requests[-4:], [(2, 100, 1), (2, 20, 1), (2, 20, 2), (2, 20, 3)]
            )
            self.assertEqual(f.accounted.accounting.confirmed, 4)

    def test_foreign_allocation_cannot_replenish_an_owned_read(self):
        with tempfile.TemporaryDirectory() as directory:
            f = self.prepare(directory)
            before = len(f.trace)
            with self.assertRaisesRegex(
                ReviewInputError, "foreign, settled or reconstructed"
            ):
                with f.accounted.read_operation(
                    "foreign", retained=CallerOperation(10)
                ):
                    self.fail("foreign allocation was accepted")
            self.assertEqual(len(f.trace), before)

    def test_owned_comment_lost_post_reconciliation_reuses_original_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            f = self.prepare(directory)
            port = GitHubIssueCommentOperationPort(
                f.http,
                token="synthetic",
                repository="owner/repo",
                pull_request=7,
                app_user_id=999,
            )
            f.fault = "unknown-absent"
            before = len(f.trace)
            with self.assertRaises(GitHubHTTPTransientError):
                port.create("synthetic retained part")
            f.history_count = 199
            with self.assertRaisesRegex(
                ReviewInputError, "operation allocation exhausted"
            ):
                port.list_comments()
            self.assertEqual(len(f.trace) - before, 3)
            self.assertEqual(f.accounted.accounting.uncertain, 1)
            self.assertEqual(f.accounted.accounting.confirmed, 2)
            with self.assertRaisesRegex(
                OperationWriteUnconfirmed, "write is unresolved"
            ):
                port.create("synthetic retained part")
            self.assertEqual(len(f.trace) - before, 3)
            self.assertEqual(sum(m == "POST" for _, m, _ in f.trace[before:]), 1)
            self.assertTrue(
                all(p.endswith("/comments") for _, _, p in f.trace[before:])
            )
