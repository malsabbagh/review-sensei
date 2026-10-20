"""Operation-scoped capacity observations never replace fresh evidence reads."""

import io
import json
import unittest
from urllib.parse import parse_qs, urlsplit

from review_sensei_github.http import GitHubHttp
from review_sensei_github.operation_comments import GitHubIssueCommentOperationPort
from review_sensei_github.publication_recovery import (
    publication_http,
    publication_session_ledger,
)
from review_sensei_github.session_ledger import GitHubIssueCommentSessionLedger

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.session import SessionIdentity


class Response(io.BytesIO):
    status = 200


class PublicationPaginationBudgetTests(unittest.TestCase):
    def transport(self, sizes):
        calls = []
        comments = [
            {"id": index + 1, "body": "x" * size, "user": {"id": 42, "type": "Bot"}}
            for index, size in enumerate(sizes)
        ]

        def opener(request, timeout):
            query = parse_qs(urlsplit(request.full_url).query)
            size = int(query["per_page"][0])
            page = int(query["page"][0])
            calls.append((size, (page - 1) * size))
            return Response(
                json.dumps(comments[(page - 1) * size : page * size]).encode()
            )

        return GitHubHttp(opener=opener), calls, comments

    def scan(self, http, budget):
        return GitHubIssueCommentOperationPort(
            publication_http(http, budget),
            token="synthetic",
            repository="o/r",
            pull_request=7,
            app_user_id=42,
        ).list_comments()

    def test_fresh_sibling_readers_reuse_only_oversized_offset_observations(self):
        http, calls, comments = self.transport([20_480] * 100)
        budget = EvidenceReadBudget()
        first = self.scan(http, budget)
        boundary = len(calls)
        comments[0]["body"] = "fresh mutable bytes"
        second = self.scan(http, budget)
        self.assertEqual(len(first), 100)
        self.assertEqual(second[0].body, "fresh mutable bytes")
        self.assertEqual(calls[boundary], (20, 0))
        self.assertEqual(budget.calls, len(calls))
        # A new original budget must relearn capacity; there is no global cache.
        boundary = len(calls)
        self.scan(http, EvidenceReadBudget())
        self.assertEqual(calls[boundary], (100, 0))

    def test_one_heavy_offset_does_not_pin_later_offsets_to_small_pages(self):
        http, calls, _ = self.transport([400_000] + [2_000] * 249)
        result = publication_http(http, EvidenceReadBudget()).paginate(
            path="/repos/o/r/issues/7/comments",
            token="synthetic",
            page_sizes=(100, 50, 20, 10, 5, 1),
            max_requests=10,
        )
        self.assertEqual(len(result), 250)
        self.assertIn((100, 100), calls)
        self.assertLessEqual(len(calls), 10)

    def test_remote_ledger_and_fresh_reader_share_capacity_without_double_debit(self):
        http, calls, _ = self.transport([20_480] * 100)
        budget = EvidenceReadBudget()
        ledger = publication_session_ledger(
            GitHubIssueCommentSessionLedger(http, token="synthetic"), budget
        )
        self.assertEqual(ledger.load(SessionIdentity("o/r", 7, 9)).status, "missing")
        boundary = len(calls)
        self.assertEqual(len(self.scan(http, budget)), 100)
        # Ledger tried100 then20. Its20 success must not claim50 is too large.
        self.assertEqual(calls[boundary], (50, 0))
        self.assertEqual(budget.calls, len(calls))

    def test_remote_ledger_keeps_existing_one_shot_primed_inventory(self):
        http, calls, comments = self.transport([100] * 100)
        http.primed_issue_comments["/repos/o/r/issues/7/comments"] = comments
        budget = EvidenceReadBudget()
        ledger = publication_session_ledger(
            GitHubIssueCommentSessionLedger(http, token="synthetic"), budget
        )
        self.assertEqual(ledger.load(SessionIdentity("o/r", 7, 9)).status, "missing")
        self.assertEqual(calls, [])
        self.assertEqual(budget.calls, 0)
        self.assertEqual(http.primed_issue_comments, {})

    def test_success_at_one_schedule_does_not_poison_another_schedule(self):
        # Independently chosen page ladders both see the same bytes. The first
        # succeeds at10 after25 is too large; that says nothing about20.
        http, calls, _ = self.transport([22_000] * 32)
        budget = EvidenceReadBudget()
        wrapper = publication_http(http, budget)
        first = wrapper.paginate(
            path="/repos/o/r/issues/7/comments",
            token="synthetic",
            page_sizes=(100, 50, 25, 10, 1),
            max_requests=10,
        )
        self.assertEqual(len(first), 32)
        boundary = len(calls)
        ledger = publication_session_ledger(
            GitHubIssueCommentSessionLedger(http, token="synthetic"), budget
        )
        self.assertEqual(ledger.load(SessionIdentity("o/r", 7, 9)).status, "missing")
        self.assertEqual(calls[boundary], (20, 0))
        boundary = len(calls)
        self.assertEqual(len(self.scan(http, budget)), 32)
        self.assertEqual(calls[boundary], (20, 0))
        self.assertEqual(budget.calls, len(calls))

    def test_other_transport_and_query_do_not_inherit_observations(self):
        http, calls, _ = self.transport([20_480] * 100)
        budget = EvidenceReadBudget()
        self.scan(http, budget)
        other, other_calls, _ = self.transport([1] * 100)
        self.scan(other, budget)
        self.assertEqual(other_calls[0], (100, 0))
        boundary = len(calls)
        publication_http(http, budget).paginate(
            path="/repos/o/r/issues/7/comments?since=2026-01-01T00:00:00Z",
            token="synthetic",
            page_sizes=(100, 50, 20, 5, 1),
            max_requests=20,
        )
        self.assertEqual(calls[boundary], (100, 0))

    def test_supported_dense_reader_controls_remain_bounded(self):
        for count, size in ((100, 16_384), (100, 20_480), (250, 100)):
            with self.subTest(count=count, size=size):
                http, calls, _ = self.transport([size] * count)
                budget = EvidenceReadBudget()
                self.assertEqual(len(self.scan(http, budget)), count)
                self.assertEqual(budget.calls, len(calls))
                self.assertLessEqual(len(calls), 10)

    def test_individually_oversized_item_refuses_within_original_budget(self):
        from review_sensei_github.errors import GitHubHTTPResponseTooLargeError

        http, calls, _ = self.transport([600_000])
        budget = EvidenceReadBudget()
        with self.assertRaises(GitHubHTTPResponseTooLargeError):
            self.scan(http, budget)
        self.assertEqual(budget.calls, len(calls))
        self.assertLessEqual(len(calls), 10)


if __name__ == "__main__":
    unittest.main()
