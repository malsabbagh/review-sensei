"""Actual finalizer consumer must enumerate large legal roots without fallback."""

import base64
import json
import unittest
from urllib.parse import parse_qs, urlparse

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.hosting.github.approval import approval_eligibility_from_result
from review_sensei.hosting.github.errors import GitHubPublicationError
from review_sensei.hosting.github.http import GitHubHttp
from review_sensei.hosting.github.publication import (
    APPROVAL_ELIGIBILITY_MARKER_PREFIX,
    ReviewApprovalFinalizer,
    approval_eligibility_marker,
)
from review_sensei.models import ReviewResult
from tests.fake_github_http import json_response
from tests.test_human_assessment import APP, BASE, HEAD, prior


def root(identifier, eligibility=None, *, length=33774):
    body = approval_eligibility_marker(eligibility or prior())
    return {
        "id": identifier,
        "body": "x" * max(0, length - len(body)) + body,
        "commit_id": HEAD,
        "state": "COMMENTED",
        "user": {"login": APP, "type": "Bot", "id": 7},
    }


class ReviewPages:
    def __init__(self, items):
        self.items = items
        self.offsets = []
        self.reads = []
        self.after_read = None
        self.override = None
        self.http = GitHubHttp(api_url="https://api.github.test", opener=self.open)

    def open(self, request, timeout):
        assert request.method == "GET" and 0 < timeout <= 30
        args = parse_qs(urlparse(request.full_url).query)
        size, page = int(args["per_page"][0]), int(args["page"][0])
        offset = (page - 1) * size
        self.offsets.append((offset, size))
        values = self.items[offset : offset + size]
        self.reads.extend(item["id"] for item in values)
        if self.after_read:
            self.after_read()
        return json_response(self.override if self.override is not None else values)

    def load(self, *, budget=None, require_valid=False):
        return ReviewApprovalFinalizer(
            http=self.http, evidence_budget=budget
        ).load_eligibility(
            token="synthetic-read",
            repository="owner/repo",
            pull_request=1,
            head_sha=HEAD,
            app_slug=APP,
            require_valid=require_valid,
        )


class FinalizerAdaptiveReviewsTests(unittest.TestCase):
    def test_complete_twenty_large_legal_roots_selects_late_authority(self):
        clean = approval_eligibility_from_result(
            ReviewResult(summary="Clean", comments=(), provider="fixture"),
            head_sha=HEAD,
            base_sha=BASE,
            enabled=True,
            app_authored=False,
        )
        latest = prior()
        items = [root(index, clean) for index in range(1, 20)] + [root(20, latest)]
        self.assertGreater(len(json.dumps(items).encode()), 512 * 1024)
        self.assertLess(max(len(item["body"].encode()) for item in items), 65536)
        pages = ReviewPages(items)
        budget = EvidenceReadBudget()
        self.assertEqual(pages.load(budget=budget, require_valid=True), latest)
        self.assertEqual(
            pages.offsets,
            [(0, 100), (0, 50), (0, 25), (0, 5), (5, 5), (10, 5), (15, 5), (20, 5)],
        )
        self.assertEqual(budget.calls, 8)

    def test_late_malformed_or_unsupported_owned_root_never_reveals_clean_one(self):
        for body in (
            APPROVAL_ELIGIBILITY_MARKER_PREFIX + " malformed -->",
            APPROVAL_ELIGIBILITY_MARKER_PREFIX
            + " "
            + base64.urlsafe_b64encode(
                json.dumps({"schema_version": "4"}).encode()
            ).decode()
            + " -->",
        ):
            items = [root(i) for i in range(1, 20)] + [
                dict(root(20), body="x" * 33000 + body)
            ]
            self.assertIsNone(ReviewPages(items).load())
            with self.assertRaises(GitHubPublicationError):
                ReviewPages(items).load(require_valid=True)

    def test_other_head_or_human_latest_does_not_replace_owned_exact_head(self):
        expected = prior()
        pages = ReviewPages(
            [
                root(1, expected),
                dict(root(2), commit_id="c" * 40),
                dict(root(3), user={"login": "alice", "type": "User"}),
            ]
        )
        self.assertEqual(pages.load(), expected)

    def test_eof_is_required_and_exact_item_cap_refuses(self):
        def ordinary(index):
            return dict(root(index, length=1), body="ordinary review")

        for count in (999, 1000, 1001):
            items = [ordinary(i) for i in range(1, count)] + [root(count, length=1)]
            pages = ReviewPages(items)
            if count == 999:
                self.assertEqual(pages.load(), prior())
                self.assertEqual(len(pages.offsets), 10)
            else:
                with self.assertRaises(GitHubPublicationError):
                    pages.load()

    def test_single_oversized_or_invalid_page_is_not_complete(self):
        pages = ReviewPages([dict(root(1), body="x" * (512 * 1024))])
        with self.assertRaises(GitHubPublicationError):
            pages.load()
        self.assertEqual(pages.offsets, [(0, 100), (0, 50), (0, 25), (0, 5), (0, 1)])
        pages = ReviewPages([])
        pages.override = {"not": "a complete list"}
        with self.assertRaises(GitHubPublicationError):
            pages.load()

    def test_shared_scan_allowance_and_absolute_deadline_do_not_refill(self):
        budget = EvidenceReadBudget()
        for _ in range(58):
            budget.consume()
        pages = ReviewPages([])
        self.assertIsNone(pages.load(budget=budget))
        self.assertIsNone(pages.load(budget=budget))
        with self.assertRaises(GitHubPublicationError):
            pages.load(budget=budget)
        self.assertEqual(budget.calls, 60)
        for _ in range(4):
            budget.consume(fence=True)
        self.assertEqual(budget.calls, 64)
        clock = [0.0]
        budget = EvidenceReadBudget(clock=lambda: clock[0])
        pages = ReviewPages([root(1)])
        pages.after_read = lambda: clock.__setitem__(0, 60.0)
        with self.assertRaises(GitHubPublicationError):
            pages.load(budget=budget)
        self.assertEqual(budget.calls, 1)


if __name__ == "__main__":
    unittest.main()
