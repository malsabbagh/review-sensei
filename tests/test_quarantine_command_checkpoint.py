"""Plain original commands qualify cold checkpoint mechanics, not guarded admission."""

import copy
import hashlib
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


class AllCheckpointLossFixture(CommandCheckpointFixture):
    def __init__(self, directory, fault=None):
        super().__init__(directory, fault)
        self.command_started = False
        self.all_writes = {}
        self.all_final_reads = {}
        self.all_lost = set()
        self.root_phases = {}

    def external(self, request, timeout):
        snapshot = getattr(
            getattr(self, "accounted", None), "_quarantine_checkpoint_snapshot", None
        )
        selected = (
            self.command_started
            and snapshot is not None
            and urlsplit(request.full_url).path
            == f"/repos/owner/repo/issues/comments/{snapshot.comment_id}"
        )
        root = snapshot.root if selected else None
        method = request.get_method()
        if selected and method == "PATCH":
            self.root_phases[root] = self.accounted._terminal_reason
            self.all_writes[root] = self.all_writes.get(root, 0) + 1
        response = super().external(request, timeout)
        if selected and method == "GET" and snapshot.operation.write_attempted:
            self.all_final_reads[root] = self.all_final_reads.get(root, 0) + 1
            if root not in self.all_lost:
                self.all_lost.add(root)
                raise TimeoutError("synthetic final GET loss at every command root")
        return response


class CommandCheckpointTests(unittest.TestCase):
    def exercise(
        self,
        fault=None,
        observer=None,
        tracer=None,
        *,
        fixture_type=CommandCheckpointFixture,
    ):
        with tempfile.TemporaryDirectory() as directory:
            fixture = fixture_type(directory, fault)
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
                if isinstance(fixture, AllCheckpointLossFixture):
                    from review_sensei_github.quarantine_action import (
                        _require_installed_command_tail,
                    )

                    supplied_observer = observer

                    def observe_tail(frame, event, arg):
                        if (
                            frame.f_code is _require_installed_command_tail.__code__
                            and event == "return"
                        ):
                            fixture.command_started = True
                        if supplied_observer is not None:
                            supplied_observer(frame, event, arg)

                    observer = observe_tail
                previous = sys.getprofile()
                previous_trace = sys.gettrace()
                try:
                    if observer is not None:
                        sys.setprofile(observer)
                    if tracer is not None:
                        sys.settrace(tracer)
                    if not isinstance(fixture, AllCheckpointLossFixture):
                        fixture.command_started = True
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
                if isinstance(fixture, AllCheckpointLossFixture):
                    self.assertEqual(len(fixture.all_writes), 5)
                    self.assertEqual(
                        sum(phase is None for phase in fixture.root_phases.values()), 4
                    )
                    self.assertEqual(set(fixture.all_writes), fixture.all_lost)
                    self.assertTrue(all(n == 1 for n in fixture.all_writes.values()))
                    self.assertTrue(
                        all(n == 2 for n in fixture.all_final_reads.values())
                    )
                    self.assertGreaterEqual(fixture.accounted.accounting.uncertain, 5)
                    from review_sensei_github.api_run_history import read_action_result

                    from review_sensei.disposition import OverrideReceipt
                    from review_sensei.presentation import render_command_receipt

                    current = fixture.current_record()
                    action = current.api_run_accounting["action"]
                    self.assertEqual(action["phase"], "terminal")
                    self.assertTrue(action["execution_started"])
                    retained = read_action_result(
                        fixture.selected.ledger, fixture.selected.identity, current
                    )
                    self.assertEqual(
                        retained["outcome"], {"kind": "applied", "reason": None}
                    )
                    result = retained["result"]
                    self.assertTrue(result["applied"])
                    self.assertFalse(result["evaluation_failed"])
                    self.assertEqual(len(result["receipts"]), 1)
                    self.assertEqual(result["receipts"][0]["outcome"], "recorded")
                    digest = hashlib.sha256(fixture.command_body.encode()).hexdigest()
                    marker = (
                        "<!-- reviewsensei:command-receipt:v1 "
                        f"source={fixture.source['id']} digest={digest} -->"
                    )
                    receipts = [
                        row
                        for row in fixture.extra_comments.values()
                        if row.get("body", "").rstrip().endswith(marker)
                    ]
                    self.assertEqual(len(receipts), 1)
                    self.assertEqual(receipts[0]["user"]["type"], "Bot")
                    self.assertEqual(
                        receipts[0]["user"]["id"],
                        fixture.selected.ledger._evidence_author_id,
                    )
                    self.assertEqual(
                        receipts[0]["body"],
                        render_command_receipt(
                            actor=fixture.source["user"]["login"],
                            source_comment_id=fixture.source["id"],
                            command_digest=digest,
                            receipts=tuple(
                                OverrideReceipt(**row) for row in result["receipts"]
                            ),
                            ledger_written=True,
                        ),
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
        scopes = []

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
                scope = frame.f_locals["scope"]
                scopes.append(scope)
                with self.assertRaisesRegex(ReviewInputError, "census scope changed"):
                    type(scope)._require_scope(
                        copy.copy(scope), scope.ledger, scope.identity
                    )
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
        self.assertEqual(len(scopes), 1)
        for scope in scopes:
            with self.assertRaisesRegex(ReviewInputError, "census scope changed"):
                type(scope)._require_scope(scope, scope.ledger, scope.identity)

    def test_all_four_command_checkpoint_losses_reconcile_without_repeated_patch(self):
        self.exercise(fixture_type=AllCheckpointLossFixture)

    def test_original_second_get_and_one_patch_survive_each_lost_final_read(self):
        self.exercise("one-loss")

    def test_two_lost_final_reads_preserve_both_unknowns_and_hold(self):
        self.exercise("two-losses")
