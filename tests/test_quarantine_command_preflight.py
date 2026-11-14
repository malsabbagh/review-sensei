"""Cold commands must not write a decision with a known unsupported tail."""

import sys
import tempfile
import unittest
from unittest.mock import patch

from review_sensei_github import quarantine_action
from review_sensei_github.quarantine_published_tail import BROKER_QUALIFICATIONS
from test_quarantine_journey import registered_resource
from test_quarantine_published_tail_journey import PublishedTailJourneyFixture


class ColdCommandPreflightTests(unittest.TestCase):
    def test_unsupported_installed_tail_stops_before_decision_and_action_boundary(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            fixture = PublishedTailJourneyFixture(directory)
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
                stage_code = quarantine_action._stage_decision_event.__code__

                def observe(frame, event, arg):
                    if event == "call" and (
                        frame.f_code is stage_code
                        or frame.f_code.co_name == "begin_action_effect"
                    ):
                        entered.append(frame.f_code.co_name)

                previous = sys.getprofile()
                try:
                    sys.setprofile(observe)
                    self.assertNotEqual(fixture.invoke_command(), 0)
                finally:
                    sys.setprofile(previous)
                self.assertIn(
                    "installed command tail has no complete lifecycle proof",
                    fixture.error,
                )
                self.assertEqual(entered, [])
                self.assertEqual(old.to_dict(), original_history)


if __name__ == "__main__":
    unittest.main()
