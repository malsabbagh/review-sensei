"""Conditional two-read geometry and fresh DATA parsing, without action grants."""

import copy
import itertools
import unittest
from types import SimpleNamespace

from review_sensei_github import quarantine_census
from review_sensei_github import quarantine_command_receipt as receipt

from review_sensei.errors import ReviewInputError
from tests.test_receipt_two_page_projection import IDENTITY, Backend


def interleavings(known, future):
    if not known:
        yield future
    elif not future:
        yield known
    else:
        for suffix in interleavings(known[1:], future):
            yield (known[0],) + suffix
        for suffix in interleavings(known, future[1:]):
            yield (future[0],) + suffix


class OrderedReceiptProjectionTests(unittest.TestCase):
    def reader(self):
        backend = Backend(20, 1000)
        for node in backend.nodes[:8]:
            node["body"] = "x" * 60000
        ledger = SimpleNamespace(http=backend, app_slug="reviewsensei[bot]")
        rows = [quarantine_census._comment(n, ledger, IDENTITY) for n in backend.nodes]
        args = dict(
            future_bounds=(60011, 65536),
            allow_graphql=True,
            identity=IDENTITY,
            max_reads=2,
        )
        with self.assertRaisesRegex(ReviewInputError, "two-page"):
            receipt.project_receipt_page(rows, **args)
        projection = receipt.project_receipt_page(rows, ordered_known=True, **args)
        self.assertEqual(projection.protocol, "graphql-two-page-ordered")
        self.assertEqual(projection.future, (60011, 65536))
        self.assertEqual(receipt._projection_binding(projection)[0], projection.known)
        membership = receipt.freeze_receipt_membership(
            rows,
            projection=projection,
            receipt_text="exact receipt",
            update_id=None,
            app_id=99,
        )
        return backend, ledger, rows, projection, membership

    @staticmethod
    def read(backend, ledger, membership, *, reconcile=False):
        return receipt.read_receipt_page(
            backend,
            path="/repos/owner/repo/issues/7/comments",
            token="synthetic",
            membership=membership,
            reconcile=reconcile,
            ledger=ledger,
            identity=IDENTITY,
        )

    def test_all_small_future_permutations_positions_and_omissions_fit(self):
        # Enumerating all small images is independent of the production prefix
        # arithmetic. Large body weights exercise actual page-cap selection.
        for known, future in (
            ((200000, 10000, 190000, 10000), (150000, 5000)),
            ((60000,) * 8 + (1000,) * 4, (60000, 2000)),
            ((10, 20), (300000, 100000, 1000)),
            ((), (200000, 150000, 1000)),
            ((200000, 190000, 180000), ()),
        ):
            with self.subTest(known=known, future=future):
                pages = receipt._ordered_two_page_schedule(known, future)
                split = pages[0][0]
                self.assertEqual(pages[1][0], len(known) + len(future) - split + 1)
                observed = 0
                for mask in range(1 << len(future)):
                    selected = tuple(
                        value
                        for index, value in enumerate(future)
                        if mask & (1 << index)
                    )
                    for order in set(itertools.permutations(selected)):
                        for values in interleavings(known, order):
                            observed += 1
                            first, second = values[:split], values[split:]
                            for actual, (_, maximum) in zip((first, second), pages):
                                self.assertLessEqual(
                                    4096 + sum(actual) + 1024 * len(actual), maximum
                                )
                                self.assertLessEqual(maximum, 512 * 1024)
                            self.assertLess(len(second), pages[1][0])
                self.assertGreater(observed, 0)

    def test_fresh_complete_reads_and_receipt_loss_keep_all_rows(self):
        backend, ledger, rows, projection, membership = self.reader()
        actual = self.read(backend, ledger, membership)
        self.assertEqual([row["id"] for row in actual], [row["id"] for row in rows])
        self.assertEqual(len(backend.calls), 2)
        self.assertEqual(
            len(self.read(backend, ledger, membership, reconcile=True)), 20
        )
        self.assertEqual(len(backend.calls), 4)
        # A genuine newly-created receipt may interleave anywhere; it does not
        # become a new authority for older bodies or their ordering.
        backend.nodes.insert(3, backend.node(20, membership.receipt_text))
        self.assertEqual(
            len(self.read(backend, ledger, membership, reconcile=True)), 21
        )
        self.assertEqual(len(backend.calls), 6)
        self.assertEqual(membership.pages, projection.pages)

    def test_materialized_event_may_interleave_but_old_known_order_may_not(self):
        backend, ledger, rows, projection, _ = self.reader()
        event = backend.node(20, "x" * 60000)
        backend.nodes.insert(5, event)
        rows.insert(5, quarantine_census._comment(event, ledger, IDENTITY))
        membership = receipt.freeze_receipt_membership(
            rows,
            projection=projection,
            receipt_text="exact receipt",
            update_id=None,
            app_id=99,
        )
        self.assertEqual(len(self.read(backend, ledger, membership)), 21)
        changed = list(rows)
        changed[0], changed[-1] = changed[-1], changed[0]
        with self.assertRaisesRegex(ReviewInputError, "known member order"):
            receipt.freeze_receipt_membership(
                changed,
                projection=projection,
                receipt_text="exact receipt",
                update_id=None,
                app_id=99,
            )

    def test_reordering_growth_missing_and_body_changes_hold_without_retry(self):
        for fault in ("within", "across", "growth", "missing", "body", "owner"):
            backend, ledger, _, _, membership = self.reader()
            if fault == "within":
                backend.nodes[0], backend.nodes[1] = backend.nodes[1], backend.nodes[0]
            elif fault == "across":
                backend.nodes[0], backend.nodes[-1] = (
                    backend.nodes[-1],
                    backend.nodes[0],
                )
            elif fault == "growth":
                backend.nodes.append(backend.node(20, "unrelated"))
            elif fault == "missing":
                backend.nodes.pop()
            elif fault == "body":
                backend.nodes[-1]["body"] = "changed"
            else:
                backend.nodes[-1]["author"]["databaseId"] = 88
            with self.subTest(fault=fault), self.assertRaises(ReviewInputError):
                self.read(backend, ledger, membership, reconcile=True)
            self.assertLessEqual(len(backend.calls), 2)

    def test_projection_protocol_order_and_page_bounds_are_not_mutable_hints(self):
        _, _, _, projection, _ = self.reader()
        for field, changed in (
            ("known", tuple(reversed(projection.known))),
            ("protocol", "graphql-two-page"),
            ("pages", ((projection.first, 512 * 1024), projection.pages[1])),
        ):
            value = copy.copy(projection)
            object.__setattr__(value, field, changed)
            with self.subTest(field=field), self.assertRaises(ReviewInputError):
                receipt._projection_binding(value)

    def test_generic_profiles_and_larger_than_two_pages_stay_unchanged(self):
        backend = Backend(2, 100)
        ledger = SimpleNamespace(http=backend, app_slug="reviewsensei[bot]")
        rows = [quarantine_census._comment(n, ledger, IDENTITY) for n in backend.nodes]
        self.assertEqual(
            receipt.project_receipt_page(rows),
            receipt.project_receipt_page(rows, ordered_known=True),
        )
        with self.assertRaises(ReviewInputError):
            receipt.project_receipt_page(rows, ordered_known=1)
        with self.assertRaisesRegex(ReviewInputError, "ordered two-page"):
            receipt._ordered_two_page_schedule((60000,) * 20, (65536,))
        self.assertEqual(backend.calls, [])


if __name__ == "__main__":
    unittest.main()
