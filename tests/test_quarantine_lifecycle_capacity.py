"""Actual quarantine publication retains its original complete capacity promises."""

import copy
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from review_sensei_github import quarantine_census as census
from review_sensei_github import quarantine_checkpoint as checkpoint
from review_sensei_github.quarantine_capacity import full_review_comment_tail
from test_quarantine_activation import event, registered_resource
from test_quarantine_journey import JourneyFixture

from review_sensei.errors import ReviewInputError


class QuarantineLifecycleCapacityTests(unittest.TestCase):
    def test_actual_partial_successor_preserves_two_roots_and_seals_capacity(self):
        original = census.lifecycle_capacity_plan
        checked = set()
        with tempfile.TemporaryDirectory() as directory:
            fixture = JourneyFixture(directory)
            old, registration = fixture.original_origins()
            fixture.revision(fixture.live_head, binary=True)
            registration["head_sha"] = fixture.live_head
            fixture.event_document = event(registration)

            def inspect(ledger, identity, **kwargs):
                scope = original(ledger, identity, **kwargs)
                count = len(fixture.trace)
                if "scope" not in checked:
                    checked.add("scope")
                    self.assertLessEqual(scope.plan.worst_read_calls(), 10)
                    self.assertLessEqual(
                        len(scope.plan.known) + len(scope.plan.unknown), 200
                    )
                    self.assertEqual(
                        scope.plan.unknown[:31], tuple(full_review_comment_tail())
                    )
                    self.assertGreaterEqual(
                        len(scope.plan.unknown),
                        31
                        + 2 * len(ledger._quarantine_authenticated.pending)
                        + 5
                        + kwargs["added_parts"],
                    )
                    predecessor_id = ledger._quarantine_authenticated.lineage[
                        "predecessor_comment_id"
                    ]
                    rows = dict(ledger._comments_by_id)
                    without = {
                        key: value
                        for key, value in rows.items()
                        if key != predecessor_id
                    }
                    with (
                        patch.object(ledger, "_comments_by_id", without),
                        patch.object(
                            ledger,
                            "_comment_order",
                            [
                                key
                                for key in ledger._comment_order
                                if key != predecessor_id
                            ],
                        ),
                    ):
                        with self.assertRaisesRegex(
                            ReviewInputError, "predecessor/current roots changed"
                        ):
                            original(ledger, identity, **kwargs)
                    foreign = copy.deepcopy(rows)
                    foreign[predecessor_id]["user"]["id"] += 1
                    with patch.object(ledger, "_comments_by_id", foreign):
                        with self.assertRaises(ReviewInputError):
                            original(ledger, identity, **kwargs)
                    with self.assertRaises(ReviewInputError):
                        original(ledger, identity, added_parts=256)

                    class DuckScope:
                        def require(self, *args):
                            raise AssertionError("duck callback must not run")

                    with patch.object(
                        ledger,
                        "_quarantine_lifecycle_capacity",
                        DuckScope(),
                        create=True,
                    ):
                        with self.assertRaisesRegex(ReviewInputError, "unissued"):
                            census.fetch_comments(ledger, identity)
                    overridden = original(ledger, identity, **kwargs)
                    object.__setattr__(
                        overridden,
                        "require",
                        lambda *args: self.fail("overridden scope callback ran"),
                    )
                    with census.activate_lifecycle_capacity(overridden):
                        with self.assertRaises(ReviewInputError):
                            census.fetch_comments(ledger, identity)
                    forged = replace(scope)
                    with self.assertRaisesRegex(ReviewInputError, "source-issued"):
                        with census.activate_lifecycle_capacity(forged):
                            self.fail("copied scope activated")
                    probe = original(ledger, identity, **kwargs)
                    with census.activate_lifecycle_capacity(probe):
                        with self.assertRaises(ReviewInputError):
                            probe.require(copy.copy(ledger), identity)
                        with self.assertRaises(ReviewInputError):
                            probe.require(ledger, identity)
                    with self.assertRaisesRegex(ReviewInputError, "source-issued"):
                        with census.activate_lifecycle_capacity(probe):
                            self.fail("expired scope reused")
                    mutated = original(ledger, identity, **kwargs)
                    object.__setattr__(mutated, "root", "a" * 64)
                    with self.assertRaisesRegex(ReviewInputError, "source-issued"):
                        with census.activate_lifecycle_capacity(mutated):
                            self.fail("mutated scope activated")
                if (
                    ledger._quarantine_authenticated.record_sha256
                    != ledger.caller._root
                    and "receipt" not in checked
                ):
                    checked.add("receipt")
                    caller = ledger.caller
                    receipt = checkpoint._CONFIRMED[caller]
                    self.assertIs(
                        checkpoint.confirmed_checkpoint_capacity(
                            caller, ledger, identity
                        ),
                        receipt,
                    )
                    try:
                        del checkpoint._CONFIRMED[caller]
                        with self.assertRaisesRegex(
                            ReviewInputError, "receipt is unavailable"
                        ):
                            original(ledger, identity, **kwargs)
                        checkpoint._CONFIRMED[caller] = replace(
                            receipt, record=copy.copy(receipt.record)
                        )
                        with self.assertRaisesRegex(
                            ReviewInputError, "receipt is unavailable"
                        ):
                            original(ledger, identity, **kwargs)
                        checkpoint._CONFIRMED[caller] = replace(
                            receipt, body=receipt.body + "changed"
                        )
                        with self.assertRaises(ReviewInputError):
                            original(ledger, identity, **kwargs)
                        checkpoint._CONFIRMED[caller] = receipt
                        with self.assertRaises(ReviewInputError):
                            checkpoint.confirmed_checkpoint_capacity(
                                caller, copy.copy(ledger), identity
                            )
                    finally:
                        checkpoint._CONFIRMED[caller] = receipt
                self.assertEqual(
                    len(fixture.trace), count, "capacity probes performed I/O"
                )
                return scope

            with (
                patch.object(census, "lifecycle_capacity_plan", inspect),
                registered_resource([registration]),
            ):
                self.assertEqual(fixture.invoke(), 0, fixture.error)
            self.assertEqual(checked, {"scope", "receipt"})
            self.assertIsNone(old.api_run_accounting["terminal_transaction"])
            self.assertFalse(
                any(row.get("state") == "APPROVED" for row in fixture.reviews)
            )
            self.assertEqual(
                fixture.current_record().api_run_accounting["configuration"]["limit"],
                512,
            )
