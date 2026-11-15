"""Synthetic two-page DATA reader; no issuer, action or ledger execution."""

import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from review_sensei_github import quarantine_census
from review_sensei_github import quarantine_command_receipt as receipt

from review_sensei.errors import ReviewInputError
from review_sensei.session import SessionIdentity

IDENTITY = SessionIdentity("owner/repo", 7, repository_id=123)


class Backend:
    api_url = "https://api.github.com"

    def __init__(self, count=40, size=15000):
        self.nodes = [self.node(i, "x" * size) for i in range(count)]
        self.calls = []
        self.change = None

    @staticmethod
    def repository_path(repository, suffix):
        return "/repos/" + repository + suffix

    @staticmethod
    def node(index, body):
        return dict(
            fullDatabaseId=str(1000 + index),
            body=body,
            createdAt="2026-11-15T00:00:00Z",
            updatedAt="2026-11-15T00:00:00Z",
            author={
                "__typename": "Bot",
                "login": "reviewsensei[bot]",
                "databaseId": 99,
            },
            pullRequest={
                "number": 7,
                "repository": {"databaseId": 123, "nameWithOwner": "owner/repo"},
            },
        )

    def request(self, method, path, *, token, body):
        self.calls.append(copy.deepcopy(body))
        variables = body["variables"]
        start = 0 if variables["after"] is None else int(variables["after"][1:])
        end = min(len(self.nodes), start + variables["first"])
        connection = {
            "nodes": copy.deepcopy(self.nodes[start:end]),
            "totalCount": len(self.nodes),
            "pageInfo": {
                "hasNextPage": end < len(self.nodes),
                "endCursor": f"c{end}" if end > start else None,
            },
        }
        response = {
            "data": {
                "repository": {
                    "databaseId": 123,
                    "nameWithOwner": "owner/repo",
                    "pullRequest": {"number": 7, "comments": connection},
                }
            }
        }
        if self.change:
            self.change(connection, len(self.calls))
        return 200, response


class ReceiptTwoPageTests(unittest.TestCase):
    def setup_reader(self):
        backend = Backend()
        ledger = SimpleNamespace(http=backend, app_slug="reviewsensei[bot]")
        rows = [quarantine_census._comment(n, ledger, IDENTITY) for n in backend.nodes]
        args = dict(future_bounds=(65536,), allow_graphql=True, identity=IDENTITY)
        with self.assertRaisesRegex(ReviewInputError, "GraphQL page exceeds"):
            receipt.project_receipt_page(rows, **args)
        projection = receipt.project_receipt_page(rows, max_reads=2, **args)
        membership = receipt.freeze_receipt_membership(
            rows,
            projection=projection,
            receipt_text="own receipt",
            update_id=None,
            app_id=99,
        )
        return backend, ledger, projection, membership

    def read(self, b, ledger, m, reconcile=False):
        return receipt.read_receipt_page(
            b,
            path="/repos/owner/repo/issues/7/comments",
            token="synthetic",
            membership=m,
            reconcile=reconcile,
            ledger=ledger,
            identity=IDENTITY,
        )

    def test_geometry_complete_and_stored_or_absent_loss(self):
        b, ledger, p, m = self.setup_reader()
        self.assertEqual(p.protocol, "graphql-two-page")
        self.assertEqual(len(self.read(b, ledger, m)), 40)
        self.assertEqual(len(b.calls), 2)
        self.assertEqual(b.calls[0]["variables"]["first"], p.pages[0][0])
        self.assertEqual(b.calls[1]["variables"]["first"], p.pages[1][0])
        self.assertEqual(p.future, (65536,))
        self.assertEqual(len(self.read(b, ledger, m, True)), 40)
        b.nodes.append(b.node(40, m.receipt_text))
        self.assertEqual(len(self.read(b, ledger, m, True)), 41)
        self.assertEqual(len(b.calls), 6)

    def test_three_page_geometry_refuses(self):
        b = Backend(60, 20000)
        ledger = SimpleNamespace(http=b, app_slug="reviewsensei[bot]")
        rows = [quarantine_census._comment(n, ledger, IDENTITY) for n in b.nodes]
        with self.assertRaisesRegex(ReviewInputError, "two-page"):
            receipt.project_receipt_page(
                rows,
                future_bounds=(65536,),
                allow_graphql=True,
                identity=IDENTITY,
                max_reads=2,
            )
        self.assertEqual(b.calls, [])

    def test_corrupt_complete_rows_or_paging_refuse_without_retry(self):
        for fault in (
            "omit",
            "duplicate",
            "growth",
            "cursor",
            "count",
            "more",
            "metadata",
            "body",
            "owner",
        ):
            b, ledger, p, m = self.setup_reader()
            if fault == "omit":
                b.nodes.pop()
            elif fault == "duplicate":
                b.nodes[-1] = copy.deepcopy(b.nodes[0])
            elif fault == "growth":
                b.nodes.append(b.node(40, "foreign"))
            elif fault == "metadata":
                b.nodes[0]["author"]["login"] = "x" * 1100
            elif fault == "body":
                b.nodes[-1]["body"] = "changed"
            elif fault == "owner":
                b.nodes[-1]["author"]["databaseId"] = 88
            else:

                def change(c, n):
                    if n == 2:
                        if fault == "cursor":
                            c["pageInfo"]["endCursor"] = f"c{p.pages[0][0]}"
                        elif fault == "count":
                            c["totalCount"] += 1
                        else:
                            c["pageInfo"]["hasNextPage"] = True

                b.change = change
            with self.subTest(fault=fault), self.assertRaises(ReviewInputError):
                self.read(b, ledger, m)
            self.assertLessEqual(len(b.calls), 2)

    def test_replaced_source_refuses_before_callback(self):
        b, ledger, p, m = self.setup_reader()
        calls = []

        def bad(*a, **k):
            calls.append((a, k))
            return {}

        with patch.object(quarantine_census, "_comment", bad):
            with self.assertRaises(ReviewInputError):
                self.read(b, ledger, m)
        self.assertEqual(calls, [])
        self.assertEqual(b.calls, [])

    def test_tampered_schedule_refuses_without_callbacks(self):
        b, ledger, p, m = self.setup_reader()
        calls = []

        class Meta(type):
            def __eq__(cls, other):
                calls.append("eq")
                return False

        class Poison(metaclass=Meta):
            pass

        object.__setattr__(m, "pages", ((Poison(), 100), (2, 100)))
        with self.assertRaises(ReviewInputError):
            self.read(b, ledger, m)
        self.assertEqual(calls, [])
        self.assertEqual(b.calls, [])

    def test_early_complete_keeps_two_page_upper_and_default_routes(self):
        backend = Backend(2, 1000)
        ledger = SimpleNamespace(http=backend, app_slug="reviewsensei[bot]")
        rows = [quarantine_census._comment(n, ledger, IDENTITY) for n in backend.nodes]
        original = receipt.project_receipt_page(rows, future_bounds=(100,))
        self.assertEqual(original.protocol, "rest")
        self.assertEqual(original.pages, ())
        projected = receipt.project_receipt_page(
            rows,
            future_bounds=(30000,) * 20,
            allow_graphql=True,
            identity=IDENTITY,
            max_reads=2,
        )
        membership = receipt.freeze_receipt_membership(
            rows,
            projection=projected,
            receipt_text="own receipt",
            update_id=None,
            app_id=99,
        )
        self.assertEqual(len(self.read(backend, ledger, membership)), 2)
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(len(membership.pages), 2)
        self.assertEqual(projected.future, (30000,) * 20)

    def test_late_source_replacement_and_large_body_refuse(self):
        backend, ledger, _, membership = self.setup_reader()
        calls = []
        original = quarantine_census._comment

        def poison(*args):
            calls.append(args)

        def replace_source(connection, count):
            quarantine_census._comment = poison

        backend.change = replace_source
        try:
            with self.assertRaises(ReviewInputError):
                self.read(backend, ledger, membership)
        finally:
            quarantine_census._comment = original
        self.assertEqual(calls, [])
        self.assertEqual(len(backend.calls), 1)
        backend, ledger, _, membership = self.setup_reader()
        backend.nodes[0]["body"] = "x" * 524288
        with self.assertRaises(ReviewInputError):
            self.read(backend, ledger, membership)
        self.assertEqual(len(backend.calls), 1)
