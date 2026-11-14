"""Stop the original nine-plus-five incident after one historical decision."""

import sys
import tempfile
import unittest
from unittest.mock import patch

import test_quarantine_historical_execution as historical_fixture
import test_quarantine_published_tail_journey as published_fixture
from review_sensei_github import quarantine_historical_execution as closed
from review_sensei_github.api_run_history import read_action_result
from review_sensei_github.quarantine_capacity import installed_capacity
from test_quarantine_journey import registered_resource


class PublishedFirstCommandTests(unittest.TestCase):
    def test_original_nine_plus_five_first_decision_has_durable_result(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(published_fixture.BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            fixture = historical_fixture.GuardedHistoricalFixture(directory)
            old, registration = fixture.original_origins(
                failed_count=9, published_count=5
            )
            old_document = old.to_dict()
            with registered_resource([registration]):
                self.assertEqual(fixture.invoke(), 0, fixture.error)
                inventories = (
                    fixture.selected.ledger._quarantine_authenticated.inventories
                )
                self.assertEqual(
                    sorted(len(v["assessments"]) for v in inventories.values()), [5, 9]
                )
                origin, inventory = next(
                    (k, v) for k, v in inventories.items() if v["assessments"]
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
                providers, reviews = fixture.provider_calls, len(fixture.reviews)
                issued = []

                def observe(frame, event, result):
                    if (
                        event == "return"
                        and frame.f_code is closed.activate.__code__
                        and result is not None
                    ):
                        run = frame.f_locals["run"]
                        state = run.caller.accounting
                        self.assertEqual(
                            (
                                state.configuration.limit,
                                len(state.completed),
                                run.caller.budget_view.ordinary_remaining(),
                                state.remaining,
                            ),
                            (512, 35, 105, 190),
                        )
                        self.assertEqual(
                            next(
                                b.undispatched
                                for b in state.blocks
                                if b.block_id == run.caller._terminal_block
                            ),
                            50,
                        )
                        self.assertEqual(state.uncertain, 0)
                        guard = installed_capacity(run.ledger, run.identity)
                        self.assertFalse(guard.complete_tail)
                        self.assertEqual(len(guard.comment_plan.unknown), 16)
                        fixture.closed_started = True
                        issued.append(run)

                previous = sys.getprofile()
                try:
                    sys.setprofile(observe)
                    rc = fixture.invoke_command()
                finally:
                    sys.setprofile(previous)
                self.assertEqual(rc, 0, fixture.error)
                self.assertEqual(len(issued), 1)
                run = issued[0]
                state = run.caller.accounting
                self.assertEqual(old.to_dict(), old_document)
                self.assertEqual(fixture.provider_calls, providers)
                self.assertEqual(len(fixture.reviews), reviews)
                self.assertEqual(fixture.native_puts, 1)
                self.assertFalse(
                    any(row["state"] == "APPROVED" for row in fixture.reviews)
                )
                self.assertEqual(len(fixture.root_writes), 5)
                self.assertTrue(
                    all(count == 1 for count in fixture.root_writes.values())
                )
                current = fixture.current_record()
                self.assertEqual(
                    current.api_run_accounting["action"]["phase"], "terminal"
                )
                retained = read_action_result(run.ledger, run.identity, current)
                self.assertEqual(
                    retained["outcome"], {"kind": "applied", "reason": None}
                )
                self.assertFalse(retained["result"]["evaluation_failed"])
                self.assertEqual(
                    [r["outcome"] for r in retained["result"]["receipts"]], ["recorded"]
                )
                self.assertEqual(
                    len(
                        fixture.selected.ledger._quarantine_authenticated.state[
                            "settlements"
                        ]
                    ),
                    1,
                )
                self.assertEqual(state.uncertain, 0)
                self.assertEqual(state.remaining, 190)
                self.assertEqual(
                    state.startup.liability
                    + len(state.completed)
                    + sum(b.undispatched + len(b.pending) for b in state.blocks)
                    + state.prior_nonstartup_liability
                    + state.remaining,
                    512,
                )
                receipts = [
                    r
                    for r in fixture.extra_comments.values()
                    if "reviewsensei:command-receipt:v1" in r.get("body", "")
                ]
                self.assertEqual(len(receipts), 1)
                self.assertEqual(
                    receipts[0]["user"]["id"], run.ledger._evidence_author_id
                )
