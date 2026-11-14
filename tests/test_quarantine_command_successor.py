"""A real command keeps confirmed-root custody across independent losses."""

import copy
import unittest

import test_quarantine_command_checkpoint as checkpoint_tests
from review_sensei_github import quarantine_command_namespace as namespace
from review_sensei_github import quarantine_command_successor as successor

from review_sensei.errors import ReviewInputError


class CommandSuccessorTests(unittest.TestCase):
    def test_original_root_handoff_preserves_loss_receipts_and_rejects_replay(self):
        issued = []
        confirmed = []
        pending = []

        def observe(frame, event, arg):
            if event == "return" and frame.f_code is successor.start.__code__:
                if arg is None:
                    return
                run = frame.f_locals["run"]
                issued.append((arg, run))
                self.assertIs(
                    successor.require_current(arg, run.caller, run.ledger),
                    run.caller._effect_record,
                )
                for token, caller, ledger in (
                    (copy.copy(arg), run.caller, run.ledger),
                    (arg, copy.copy(run.caller), run.ledger),
                    (arg, run.caller, copy.copy(run.ledger)),
                ):
                    with self.assertRaisesRegex(ReviewInputError, "copied or stale"):
                        successor.require_current(token, caller, ledger)
            if (
                event == "call"
                and frame.f_code is namespace.before_sequence.__code__
                and issued
            ):
                token, run = issued[0]
                if frame.f_locals["caller"] is run.caller:
                    successor.require_current(token, run.caller, run.ledger)
            if (
                event == "call"
                and frame.f_code is successor.confirm_checkpoint.__code__
                and issued
                and frame.f_locals["caller"] is issued[0][1].caller
            ):
                values = frame.f_locals
                with self.assertRaisesRegex(ReviewInputError, "original confirmation"):
                    successor.confirm_checkpoint(
                        values["caller"],
                        values["ledger"],
                        values["record"],
                        values["persisted"],
                    )
                token, run = issued[0]
                old = run.caller._root
                try:
                    run.caller._root = "0" * 64
                    with self.assertRaisesRegex(ReviewInputError, "copied or stale"):
                        successor.require_current(token, run.caller, run.ledger)
                finally:
                    run.caller._root = old
                confirmed.append(values["record"].record_sha256)
                pending.append(values["caller"].accounting.uncertain)

        checkpoint_tests.CommandCheckpointTests().exercise(
            fixture_type=checkpoint_tests.AllCheckpointLossFixture, observer=observe
        )
        self.assertEqual(len(issued), 1)
        self.assertEqual(len(confirmed), 5)
        self.assertEqual(len(set(confirmed)), 5)
        self.assertEqual(pending, [1, 2, 3, 4, 5])
        token, run = issued[0]
        with self.assertRaisesRegex(ReviewInputError, "copied or stale"):
            successor.require_current(token, run.caller, run.ledger)
