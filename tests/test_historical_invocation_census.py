"""Closed one-page reads retain the original C4 execution quote."""

import copy
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

from fake_github_http import json_response
from review_sensei_github import historical_invocation_census as census
from review_sensei_github import quarantine_historical_execution as historical
from review_sensei_github.quarantine_census import QUERY
from test_quarantine_historical_execution import GuardedHistoricalFixture
from test_quarantine_journey import registered_resource
from test_quarantine_published_tail_journey import BROKER_QUALIFICATIONS

from review_sensei.errors import ReviewInputError


class CensusFixture(GuardedHistoricalFixture):
    def __init__(self, directory, fault=None):
        super().__init__(directory)
        self.census_fault = fault
        self.census_attempts = 0
        self.initial_ids = None
        self.fault_injected = False
        self.fake_finish_calls = 0
        self.original_finish = census.finish

    def external(self, request, timeout):
        response = super().external(request, timeout)
        payload = json.loads(request.data) if request.data else {}
        if self.closed_started and payload.get("query") == QUERY:
            self.census_attempts += 1
            if self.census_fault == "export" and self.census_attempts == 1:

                def fake_finish(*args):
                    self.fake_finish_calls += 1
                    return args[3]

                census.finish = fake_finish
                self.fault_injected = True
                return response
            if self.census_fault == "event_id":
                value = json.loads(response.read(512 * 1024))
                nodes = value["data"]["repository"]["pullRequest"]["comments"]["nodes"]
                if self.initial_ids is None:
                    self.initial_ids = {node["fullDatabaseId"] for node in nodes}
                else:
                    added = [
                        node
                        for node in nodes
                        if node["fullDatabaseId"] not in self.initial_ids
                    ]
                    if added and not self.fault_injected:
                        assert len(added) == 1
                        added[0]["fullDatabaseId"] = "999999999"
                        self.fault_injected = True
                return json_response(value)
            if self.census_attempts == 1 and self.census_fault:
                if self.census_fault == "loss":
                    raise TimeoutError("lost original complete census response")
                value = json.loads(response.read(512 * 1024))
                connection = value["data"]["repository"]["pullRequest"]["comments"]
                if self.census_fault == "metadata":
                    connection["nodes"][0]["author"]["login"] = "x" * 5000
                elif self.census_fault == "membership":
                    extra = copy.deepcopy(connection["nodes"][0])
                    extra["fullDatabaseId"] = "999999999"
                    extra["body"] = "Unexpected concurrent comment."
                    connection["nodes"].append(extra)
                    connection["totalCount"] += 1
                else:
                    raise AssertionError(self.census_fault)
                return json_response(value)
        return response


class HistoricalInvocationCensusTests(unittest.TestCase):
    def exercise(self, fault=None, extra_observer=None):
        issued, completed, starts = [], [], {}
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            fixture = CensusFixture(directory, fault)
            _, registration = fixture.original_origins(
                failed_count=1, published_count=1
            )
            with registered_resource([registration]):
                self.assertEqual(fixture.invoke(), 0, fixture.error)
                origin, inventory = next(
                    (key, value)
                    for key, value in fixture.selected.ledger._quarantine_authenticated.inventories.items()
                    if value["assessments"]
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

                def observe(frame, event, result):
                    if (
                        event == "return"
                        and frame.f_code is historical.activate.__code__
                        and result is not None
                    ):
                        fixture.closed_started = True
                        run = frame.f_locals["run"]
                        issued.append(run)
                        recipe = json.loads(frame.f_locals["saved"]["recipe"])
                        self.assertEqual(
                            (recipe["comment_reads"], recipe["combined_loss_calls"]),
                            (4, 105),
                        )
                    if (
                        event == "return"
                        and frame.f_code is census.choose.__code__
                        and result is not None
                    ):
                        ledger, identity = (
                            frame.f_locals["ledger"],
                            frame.f_locals["identity"],
                        )
                        starts[result] = len(ledger.caller.observations)
                        with self.assertRaises(ReviewInputError):
                            census.choose(ledger, identity)
                    if (
                        event == "return"
                        and frame.f_code is census.finish.__code__
                        and result is not None
                    ):
                        values = frame.f_locals
                        ledger, identity, token = (
                            values["ledger"],
                            values["identity"],
                            values["token"],
                        )
                        self.assertEqual(
                            len(ledger.caller.observations) - starts[token], 1
                        )
                        self.assertIs(result, values["rows"])
                        for candidate in (token, copy.copy(token)):
                            with self.assertRaises(ReviewInputError):
                                census.finish(
                                    candidate,
                                    ledger,
                                    identity,
                                    result,
                                    values["response"],
                                )
                        completed.append(token)
                    if extra_observer is not None:
                        extra_observer(frame, event, result)

                previous = sys.getprofile()
                try:
                    sys.setprofile(observe)
                    code = fixture.invoke_command()
                finally:
                    sys.setprofile(previous)
                    census.finish = fixture.original_finish
                self.assertEqual(len(issued), 1)
                caller = issued[0].caller
                self.assertEqual(caller.accounting.configuration.limit, 512)
                self.assertEqual(caller.accounting.remaining, 190)
                self.assertFalse(
                    any(row["state"] == "APPROVED" for row in fixture.reviews)
                )
                if fault is None:
                    self.assertEqual(code, 0, fixture.error)
                    self.assertGreaterEqual(len(completed), 5)
                    self.assertEqual(caller.accounting.action["phase"], "terminal")
                    self.assertIsNotNone(caller.accounting.action["result"])
                    self.assertEqual(
                        len(
                            fixture.selected.ledger._quarantine_authenticated.state[
                                "settlements"
                            ]
                        ),
                        1,
                    )
                else:
                    self.assertEqual(code, 1, fixture.error)
                    if fault == "event_id":
                        self.assertTrue(fixture.fault_injected)
                        self.assertGreater(fixture.census_attempts, 1)
                        self.assertGreaterEqual(len(completed), 1)
                    else:
                        self.assertEqual(fixture.census_attempts, 1)
                        self.assertEqual(completed, [])
                    self.assertEqual(fixture.fake_finish_calls, 0)
                    self.assertEqual(fixture.native_puts, 0)
                    if fault != "event_id":
                        self.assertFalse(caller.accounting.action["execution_started"])
                    self.assertEqual(caller.accounting.action["phase"], "pending")
                    if fault != "event_id":
                        self.assertEqual(caller.budget_view.ordinary_remaining(), 102)
                    self.assertEqual(
                        next(
                            b.undispatched
                            for b in caller.accounting.blocks
                            if b.block_id == caller._terminal_block
                        ),
                        50,
                    )
                    self.assertEqual(caller.accounting.uncertain, int(fault == "loss"))
                    self.assertEqual(
                        len(
                            fixture.selected.ledger._quarantine_authenticated.state[
                                "settlements"
                            ]
                        ),
                        0,
                    )
                    expected = {
                        "export": "source",
                        "event_id": "confirmed event identity changed",
                        "metadata": "metadata exceeds its bound",
                        "membership": "unpriced membership",
                        "loss": "session ledger discovery failed",
                    }[fault]
                    self.assertIn(expected, fixture.error)

    def test_original_one_page_reads_complete_under_unchanged_c4_quote(self):
        self.exercise()

    def test_metadata_growth_refuses_before_any_command_effect(self):
        self.exercise("metadata")

    def test_foreign_membership_refuses_before_any_command_effect(self):
        self.exercise("membership")

    def test_lost_response_preserves_its_physical_unknown_without_retry(self):
        self.exercise("loss")

    def test_response_cannot_replace_completion_export(self):
        self.exercise("export")

    def test_same_signed_event_body_cannot_alias_confirmed_event_id(self):
        self.exercise("event_id")
