"""Exact immutable event framing remains source-bound and enforced pre-debit."""

import sys
import tempfile
import unittest
from unittest.mock import patch

from review_sensei_github import quarantine_action, quarantine_creation
from review_sensei_github.quarantine_command_suffix import _closed_event_body
from test_quarantine_historical_execution import GuardedHistoricalFixture
from test_quarantine_journey import registered_resource
from test_quarantine_published_tail_journey import BROKER_QUALIFICATIONS

from review_sensei.errors import ReviewInputError


class EventProjectionTests(unittest.TestCase):
    def test_replaced_original_event_framer_cannot_issue_a_smaller_bound(self):
        with patch.object(quarantine_creation, "_body", lambda part: ""):
            with self.assertRaises(ReviewInputError):
                _closed_event_body(None, None, None)

    def test_changed_event_payload_is_refused_before_its_physical_debit(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            fixture = GuardedHistoricalFixture(directory)
            _, registration = fixture.original_origins(
                failed_count=1, published_count=1
            )
            with registered_resource([registration]):
                self.assertEqual(fixture.invoke(), 0, fixture.error)
                origin, inventory = next(
                    (k, v)
                    for k, v in fixture.selected.ledger._quarantine_authenticated.inventories.items()
                    if v["assessments"]
                )
                fixture.quarantine_predecessor_comment_id = None
                fixture.select_command(
                    "@reviewsensei dismiss "
                    + inventory["assessments"][0]["instance"]
                    + ' --reason "quarantine:'
                    + origin
                    + ' Exact source disproves this historical concern."',
                    new_run=True,
                )
                captured = []

                def observe(frame, event, result):
                    if (
                        event == "call"
                        and frame.f_code
                        is quarantine_action._stage_decision_event.__code__
                    ):
                        run = frame.f_locals["run"]
                        frame.f_locals["event"]["synthetic_unplanned_member"] = True
                        captured.append((run, len(run.caller.observations)))

                previous = sys.getprofile()
                try:
                    sys.setprofile(observe)
                    result = fixture.invoke_command()
                finally:
                    sys.setprofile(previous)
                self.assertEqual(result, 1, fixture.error)
                self.assertIn(
                    "historical event exceeds its promised wire bound", fixture.error
                )
                self.assertEqual(len(captured), 1)
                run, start = captured[0]
                later = run.caller.observations[start:]
                self.assertFalse(any(o.get("mutation") for o in later))
                self.assertEqual(run.caller.accounting.uncertain, 0)
                self.assertEqual(run.caller.accounting.remaining, 190)
                self.assertEqual(run.caller.accounting.action["phase"], "pending")
                self.assertIsNone(run.caller.accounting.action["result"])
                self.assertFalse(
                    any(
                        "reviewsensei:command-receipt:v1" in row.get("body", "")
                        for row in fixture.extra_comments.values()
                    )
                )
