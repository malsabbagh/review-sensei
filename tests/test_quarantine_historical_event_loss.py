"""A lost historical event never authorizes the decision cursor or native PUT."""

import json
import sys
import tempfile
import unittest
from unittest.mock import patch

from review_sensei_github import quarantine_historical_execution as closed
from test_quarantine_historical_execution import GuardedHistoricalFixture
from test_quarantine_journey import registered_resource
from test_quarantine_published_tail_journey import BROKER_QUALIFICATIONS


class EventLossFixture(GuardedHistoricalFixture):
    def __init__(self, directory, *, stored):
        super().__init__(directory)
        self.stored = stored
        self.event_attempts = 0

    def external(self, request, timeout):
        body = json.loads(request.data) if request.data else {}
        event = (
            self.closed_started
            and request.get_method() == "POST"
            and "reviewsensei:evidence-part:v1" in str(body.get("body", ""))
        )
        if event:
            self.event_attempts += 1
            if self.stored:
                super().external(request, timeout)
            else:
                self._meter(request)
            raise TimeoutError("synthetic event response loss; no retry")
        return super().external(request, timeout)


class HistoricalEventLossTests(unittest.TestCase):
    def exercise(self, stored):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            fixture = EventLossFixture(directory, stored=stored)
            old, registration = fixture.original_origins(
                failed_count=1, published_count=1
            )
            original = old.to_dict()
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
                runs = []

                def observe(frame, event, result):
                    if (
                        event == "return"
                        and frame.f_code is closed.activate.__code__
                        and result is not None
                    ):
                        fixture.closed_started = True
                        runs.append(frame.f_locals["run"])

                previous = sys.getprofile()
                try:
                    sys.setprofile(observe)
                    result = fixture.invoke_command()
                finally:
                    sys.setprofile(previous)
                self.assertEqual(result, 1, fixture.error)
                self.assertEqual(len(runs), 1)
                run = runs[0]
                state = run.caller.accounting
                blocks = {b.block_id: b for b in state.blocks}
                self.assertEqual(fixture.event_attempts, 1)
                self.assertEqual(fixture.native_puts, 0)
                self.assertEqual(len(fixture.root_writes), 1)
                self.assertEqual(list(fixture.root_writes.values()), [1])
                self.assertEqual(state.uncertain, 1)
                self.assertEqual(state.remaining, 190)
                self.assertEqual(blocks[run.caller._terminal_block].undispatched, 50)
                self.assertEqual(state.action["phase"], "pending")
                self.assertIsNone(state.action["result"])
                self.assertEqual(old.to_dict(), original)
                self.assertFalse(
                    any(
                        "reviewsensei:command-receipt:v1" in row.get("body", "")
                        for row in fixture.extra_comments.values()
                    )
                )
                self.assertFalse(
                    any(row.get("state") == "APPROVED" for row in fixture.reviews)
                )

    def test_stored_event_response_loss_stops_before_cursor_without_replay(self):
        self.exercise(True)

    def test_absent_event_response_loss_stops_before_cursor_without_replay(self):
        self.exercise(False)
