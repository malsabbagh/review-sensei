"""Plain original commands qualify cold checkpoint mechanics, not guarded admission."""

import copy
import sys
import tempfile
import unittest
from dataclasses import replace
from urllib.parse import urlsplit

from review_sensei_github import quarantine_command_checkpoint as phase
from test_quarantine_journey import JourneyFixture, registered_resource

from review_sensei.errors import ReviewInputError


class CommandCheckpointFixture(JourneyFixture):
    def __init__(self, directory, fault=None):
        super().__init__(directory)
        self.fault = fault
        self.lost = set()
        self.writes = {}
        self.reads = {}
        self.final_reads = {}
        self.copies = set()

    def external(self, request, timeout):
        snapshot = getattr(
            getattr(self, "accounted", None), "_quarantine_checkpoint_snapshot", None
        )
        if snapshot is None or phase.observation(snapshot.ledger) is None:
            return super().external(request, timeout)
        if (
            urlsplit(request.full_url).path
            != f"/repos/owner/repo/issues/comments/{snapshot.comment_id}"
        ):
            return super().external(request, timeout)
        root, method = snapshot.root, request.get_method()
        try:
            replace(snapshot).require()
        except ReviewInputError:
            self.copies.add(root)
        else:
            raise AssertionError("copied checkpoint snapshot was accepted")
        if method == "PATCH":
            self.writes[root] = self.writes.get(root, 0) + 1
        if method == "GET" and not snapshot.operation.write_attempted:
            self.reads[root] = self.reads.get(root, 0) + 1
        response = super().external(request, timeout)
        if method == "GET" and snapshot.operation.write_attempted:
            self.final_reads[root] = self.final_reads.get(root, 0) + 1
            if (
                self.fault == "two-losses"
                or self.fault == "one-loss"
                and root not in self.lost
            ):
                self.lost.add(root)
                raise TimeoutError("synthetic exact cold final GET response loss")
        return response


class CommandCheckpointTests(unittest.TestCase):
    def exercise(self, fault=None, observer=None, tracer=None):
        with tempfile.TemporaryDirectory() as directory:
            fixture = CommandCheckpointFixture(directory, fault)
            _old, registration = fixture.original_origins(
                failed_count=1, published_count=1
            )
            with registered_resource([registration]):
                self.assertEqual(fixture.invoke(), 0, fixture.error)
                inventories = (
                    fixture.selected.ledger._quarantine_authenticated.inventories
                )
                origin, inventory = next(
                    (scope, value)
                    for scope, value in inventories.items()
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
                previous = sys.getprofile()
                previous_trace = sys.gettrace()
                try:
                    if observer is not None:
                        sys.setprofile(observer)
                    if tracer is not None:
                        sys.settrace(tracer)
                    result = fixture.invoke_command()
                finally:
                    sys.setprofile(previous)
                    sys.settrace(previous_trace)
                if fault in {None, "one-loss"}:
                    self.assertEqual(result, 0, fixture.error)
                    self.assertEqual(len(fixture.writes), 2)
                else:
                    self.assertEqual(result, 1, fixture.error)
                self.assertTrue(all(value == 1 for value in fixture.writes.values()))
                self.assertTrue(all(value == 1 for value in fixture.reads.values()))
                self.assertEqual(set(fixture.writes), fixture.copies)
                self.assertEqual(fixture.accounted.accounting.configuration.limit, 512)
                self.assertFalse(
                    any(row.get("state") == "APPROVED" for row in fixture.reviews)
                )
                if fault == "intervening":
                    self.assertEqual(fixture.writes, {})
                if fault == "two-losses":
                    self.assertEqual(len(fixture.writes), 1)
                    self.assertEqual(tuple(fixture.final_reads.values()), (2,))
                    self.assertGreaterEqual(fixture.accounted.accounting.uncertain, 2)
                elif fault == "one-loss":
                    self.assertEqual(set(fixture.writes), fixture.lost)
                    self.assertTrue(
                        all(count == 2 for count in fixture.final_reads.values())
                    )

    def test_intervening_io_expires_real_census_before_any_journal_patch(self):
        injected = []
        rejected = []

        def trace(frame, event, arg):
            if frame.f_code is not phase.before_get.__code__:
                return None
            if event == "exception" and isinstance(arg[1], ReviewInputError):
                rejected.append(str(arg[1]))
            return trace

        def observe(frame, event, arg):
            if (
                frame.f_code is phase.completed_census.__code__
                and event == "return"
                and frame.f_locals.get("token") is not None
                and not injected
            ):
                token = frame.f_locals["token"]
                ledger = frame.f_locals["ledger"]
                before = len(ledger.caller.observations)
                with self.assertRaisesRegex(ReviewInputError, "source changed"):
                    phase.completed_census(copy.copy(token), arg)
                with self.assertRaisesRegex(ReviewInputError, "source changed"):
                    phase.completed_census(token, arg)
                self.assertEqual(len(ledger.caller.observations), before)
                ledger.http.request(
                    "GET", "/repos/owner/repo/pulls/7", token=ledger.token
                )
                self.assertEqual(len(ledger.caller.observations), before + 1)
                injected.append(True)

        self.exercise("intervening", observe, trace)
        self.assertEqual(
            rejected, ["cold census first-root receipt is stale or foreign"]
        )
        self.assertEqual(injected, [True])

    def test_original_second_get_and_one_patch_survive_each_lost_final_read(self):
        self.exercise("one-loss")

    def test_two_lost_final_reads_preserve_both_unknowns_and_hold(self):
        self.exercise("two-losses")
