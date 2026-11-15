"""An original native claim consumes authority without erasing sizing evidence."""

import copy
import dis
import sys
import unittest
from unittest.mock import patch

from review_sensei_github import quarantine_checkpoint as checkpoint

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError


class CheckpointReceiptOwnershipTests(unittest.TestCase):
    def test_original_claim_retains_sizing_but_never_allows_a_second_claim(self):
        from test_bounded_snapshot_lifecycle import BoundedSnapshotLifecycleTests

        class Captured(BaseException):
            pass

        code = checkpoint.take_confirmed_checkpoint.__code__
        returns = {
            item.offset
            for item in dis.get_instructions(code)
            if item.opname in {"RETURN_VALUE", "RETURN_CONST"}
        }
        captured = []
        previous = sys.getprofile()

        def observe(frame, event, receipt):
            if previous is not None:
                previous(frame, event, receipt)
            if (
                event != "return"
                or frame.f_code is not code
                or frame.f_lasti not in returns
            ):
                return
            caller, ledger, identity = (
                frame.f_locals[key] for key in ("caller", "ledger", "identity")
            )
            before = canonical_bytes(caller.accounting.to_document())
            observations = copy.deepcopy(caller.observations)
            self.assertIs(
                checkpoint.confirmed_checkpoint_capacity(caller, ledger, identity),
                receipt,
            )
            with self.assertRaisesRegex(ReviewInputError, "receipt is unavailable"):
                checkpoint.take_confirmed_checkpoint(
                    caller, ledger, identity, staging=receipt.record.publication_staging
                )
            self.assertIs(
                checkpoint.confirmed_checkpoint_capacity(caller, ledger, identity),
                receipt,
            )
            for attribute, changed in (
                ("_root", "a" * 64),
                ("_effect_record", copy.copy(receipt.record)),
                ("_checkpoint", object()),
                ("_block", "foreign-block"),
            ):
                with (
                    self.subTest(attribute=attribute),
                    patch.object(caller, attribute, changed),
                ):
                    with self.assertRaises(ReviewInputError):
                        checkpoint.confirmed_checkpoint_capacity(
                            caller, ledger, identity
                        )
            with self.assertRaises(ReviewInputError):
                checkpoint.confirmed_checkpoint_capacity(
                    caller, copy.copy(ledger), identity
                )
            with self.assertRaises(ReviewInputError):
                checkpoint.confirmed_checkpoint_capacity(
                    copy.copy(caller), ledger, identity
                )
            self.assertEqual(canonical_bytes(caller.accounting.to_document()), before)
            self.assertEqual(caller.observations, observations)
            captured.append(receipt)
            raise Captured()

        try:
            sys.setprofile(observe)
            with self.assertRaises(Captured):
                BoundedSnapshotLifecycleTests(
                    "test_current_and_historical_decisions_finish_with_all_late_carriers"
                ).test_current_and_historical_decisions_finish_with_all_late_carriers()
        finally:
            sys.setprofile(previous)
        self.assertEqual(len(captured), 1)
