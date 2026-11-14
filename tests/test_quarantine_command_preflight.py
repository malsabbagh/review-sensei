"""Cold commands must not write a decision with a known unsupported tail."""

import copy
import sys
import tempfile
import unittest
from unittest.mock import patch

from review_sensei_github import quarantine_action
from review_sensei_github.quarantine_published_tail import BROKER_QUALIFICATIONS
from test_quarantine_journey import JourneyFixture, registered_resource
from test_quarantine_published_tail_journey import PublishedTailJourneyFixture


class ColdCommandPreflightTests(unittest.TestCase):
    def test_unsupported_installed_tail_stops_before_decision_and_action_boundary(self):
        self.exercise_guard("unsupported")

    def test_removed_and_copied_installed_guards_cannot_bypass_preflight(self):
        for fault in ("removed", "copied"):
            with self.subTest(fault=fault):
                self.exercise_guard(fault)

    def test_genuine_plain_target_does_not_require_a_native_guard(self):
        self.exercise_guard("plain")

    def exercise_guard(self, fault):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            fixture = (
                JourneyFixture if fault == "plain" else PublishedTailJourneyFixture
            )(directory)
            old, registration = fixture.original_origins(
                failed_count=1, published_count=1
            )
            original_history = old.to_dict()
            with registered_resource([registration]):
                self.assertEqual(fixture.invoke(), 0, fixture.error)
                inventories = (
                    fixture.selected.ledger._quarantine_authenticated.inventories
                )
                origin = next(iter(inventories))
                instance = inventories[origin]["assessments"][0]["instance"]
                fixture.quarantine_predecessor_comment_id = None
                fixture.select_command(
                    "@reviewsensei dismiss "
                    + instance
                    + ' --reason "quarantine:'
                    + origin
                    + ' Exact source disproves this historical concern."',
                    new_run=True,
                )
                entered = []
                touched = []
                helper_code = quarantine_action._require_installed_command_tail.__code__
                stage_code = quarantine_action._stage_decision_event.__code__

                def observe(frame, event, arg):
                    if frame.f_code is helper_code:
                        if event == "call":
                            from review_sensei_github.publication_recovery import (
                                _publication_transport,
                            )

                            transport = _publication_transport(
                                frame.f_locals["run"].ledger.http
                            )
                            original = transport._quarantine_capacity_guard
                            touched.append((transport, original))
                            if fault == "removed":
                                transport._quarantine_capacity_guard = None
                            elif fault == "copied":
                                transport._quarantine_capacity_guard = copy.copy(
                                    original
                                )
                            elif fault == "plain":
                                self.assertIsNone(original)
                        elif event == "return" and touched:
                            transport, original = touched[-1]
                            transport._quarantine_capacity_guard = original
                    if event == "call" and (
                        frame.f_code is stage_code
                        or frame.f_code.co_name == "begin_action_effect"
                    ):
                        if fault == "plain":
                            from review_sensei.errors import ReviewInputError

                            raise ReviewInputError(
                                "synthetic stop after plain capacity preflight"
                            )
                        entered.append(frame.f_code.co_name)

                previous = sys.getprofile()
                try:
                    sys.setprofile(observe)
                    self.assertNotEqual(fixture.invoke_command(), 0)
                finally:
                    sys.setprofile(previous)
                expected = (
                    "synthetic stop after plain capacity preflight"
                    if fault == "plain"
                    else "installed command tail has no complete lifecycle proof"
                    if fault == "unsupported"
                    else "installed capacity source changed"
                )
                self.assertIn(expected, fixture.error)
                self.assertEqual(len(touched), 1)
                self.assertEqual(entered, [])
                self.assertEqual(old.to_dict(), original_history)


if __name__ == "__main__":
    unittest.main()
