"""Paid preparation remains charged; projected data cannot admit approval."""

import copy
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

from review_sensei_github import quarantine_command_finalization as finalization
from review_sensei_github import quarantine_command_namespace as namespace
from test_quarantine_journey import registered_resource
from test_quarantine_published_tail_journey import (
    BROKER_QUALIFICATIONS,
    PublishedTailJourneyFixture,
)

from review_sensei.api_work import PendingApiRequest
from review_sensei.errors import ReviewInputError


class PaidPrefixProjectionTests(unittest.TestCase):
    def test_original_paid_prefix_and_final_command_hold_preserve_all_funding(self):
        captured = []
        measured = []

        def observe(frame, event, result):
            if (
                event == "return"
                and frame.f_code is namespace.capture_paid_prefix.__code__
            ):
                caller = frame.f_locals["caller"]
                prepared = namespace.project_prepared_prefix(caller)
                baseline = namespace.project_current(caller)
                self.assertEqual(
                    len(prepared["completed"]), len(baseline["completed"]) + 2
                )
                self.assertEqual(
                    sum(len(b["pending"]) for b in prepared["blocks"]), 162
                )
                self.assertEqual(
                    sum(len(b["pending"]) for b in baseline["blocks"]), 164
                )
                self.assertEqual(caller.budget_view.ordinary_remaining(), 112)
                prior = {
                    p.request_id: p.deadline_unix_ms
                    for b in caller.accounting.blocks
                    for p in b.pending
                }
                for block in prepared["blocks"]:
                    for pending in block["pending"]:
                        self.assertEqual(
                            pending["deadline_unix_ms"],
                            prior.get(
                                pending["request_id"],
                                namespace.MAX_COLD_DEADLINE_UNIX_MS,
                            ),
                        )
                self.assertEqual(caller.accounting.remaining, 190)
                self.assertEqual(
                    next(
                        b.undispatched
                        for b in caller.accounting.blocks
                        if b.block_id == caller._terminal_block
                    ),
                    50,
                )
                with self.assertRaises(ReviewInputError):
                    namespace.capture_paid_prefix(caller)
                with self.assertRaises(ReviewInputError):
                    namespace.project_prepared_prefix(copy.copy(caller))
                copied = copy.deepcopy(prepared)
                copied["remaining"] = 0
                self.assertEqual(namespace.project_prepared_prefix(caller), prepared)
                state = caller._state
                request = PendingApiRequest("invented-paid-prefix", 1)
                try:
                    caller._state = state.dispatch(caller._block, request).confirm(
                        request
                    )
                    with self.assertRaises(ReviewInputError):
                        namespace.project_prepared_prefix(caller)
                finally:
                    caller._state = state
                sequence = caller._sequence
                try:
                    caller._sequence += 1
                    with self.assertRaises(ReviewInputError):
                        namespace.project_prepared_prefix(caller)
                finally:
                    caller._sequence = sequence
                item = caller.observations[-1]
                outcome = item["outcome"]
                try:
                    item["outcome"] = "unknown"
                    with self.assertRaises(ReviewInputError):
                        namespace.project_prepared_prefix(caller)
                finally:
                    item["outcome"] = outcome
                self.assertEqual(namespace.project_prepared_prefix(caller), prepared)
                captured.append(caller)
            if (
                event == "call"
                and frame.f_code is finalization.intermediate_suffix_recipe.__code__
            ):
                observation = frame.f_locals["observation"]
                if json.loads(observation.payload)["conditional_approval"] is None:
                    return
                # Source-fixed native IDs now let this small two-obligation
                # fixture fit. Complete sizing still grants no final-command
                # lease: the original application must retain its hold below.
                result = finalization.measure_inline_branches(observation)
                self.assertTrue(result["all_growth_roots_fit"])
                self.assertTrue(
                    result["future_approval_domain_requires_execution_proof"]
                )
                for root in result["growth_roots"]:
                    self.assertTrue(root["original_validated_and_rendered"])
                    self.assertLessEqual(root["ordinary_root_bytes"], 32768)
                    self.assertLessEqual(root["terminal_root_bytes"], 32768)
                    self.assertLessEqual(root["comment_body_bytes"], 65536)
                measured.append(result)

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            fixture = PublishedTailJourneyFixture(directory)
            _, registration = fixture.original_origins(
                failed_count=1, published_count=1
            )
            with registered_resource([registration]):
                self.assertEqual(fixture.invoke(), 0, fixture.error)
                targets = [
                    (key, row["instance"])
                    for key, inventory in fixture.selected.ledger._quarantine_authenticated.inventories.items()
                    for row in inventory["assessments"]
                ]
                self.assertEqual(len(targets), 2)
                for index, (origin, instance) in enumerate(targets):
                    fixture.quarantine_predecessor_comment_id = None
                    fixture.select_command(
                        "@reviewsensei dismiss "
                        + instance
                        + ' --reason "quarantine:'
                        + origin
                        + ' Exact source disproves this historical concern."',
                        new_run=True,
                    )
                    previous = sys.getprofile()
                    try:
                        sys.setprofile(observe)
                        code = fixture.invoke_command()
                    finally:
                        sys.setprofile(previous)
                    self.assertEqual(code, index, fixture.error)
                    self.assertEqual(
                        len(
                            fixture.selected.ledger._quarantine_authenticated.state[
                                "settlements"
                            ]
                        ),
                        1,
                    )
                    self.assertFalse(
                        any(r["state"] == "APPROVED" for r in fixture.reviews)
                    )
                self.assertIn("one update and check replay only", fixture.error)
        self.assertEqual(len(captured), 2)
        self.assertEqual(len(measured), 1)

    def test_coordinated_original_and_live_accounting_mutation_is_refused(self):
        baselines, changed, callers = [], [], []

        def observe(frame, event, result):
            if (
                event == "return"
                and frame.f_code is namespace.prepare.__code__
                and result is not None
            ):
                baselines.append(frame.f_locals["state"])
            if (
                event == "call"
                and frame.f_code is namespace.capture_paid_prefix.__code__
            ):
                caller = frame.f_locals["caller"]
                request = baselines[-1].completed[0]
                self.assertIs(request, caller.accounting.completed[0])
                changed.append((request, request.deadline_unix_ms))
                object.__setattr__(
                    request, "deadline_unix_ms", request.deadline_unix_ms + 1
                )
                callers.append(caller)

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            fixture = PublishedTailJourneyFixture(directory)
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
                previous = sys.getprofile()
                try:
                    sys.setprofile(observe)
                    code = fixture.invoke_command()
                finally:
                    sys.setprofile(previous)
                    for request, deadline in changed:
                        object.__setattr__(request, "deadline_unix_ms", deadline)
                self.assertEqual(code, 1, fixture.error)
                self.assertIn(
                    "cold namespace original accounting changed", fixture.error
                )
                self.assertEqual(len(callers), 1)
                caller = callers[0]
                self.assertFalse(caller.accounting.action["execution_started"])
                self.assertEqual(caller.budget_view.ordinary_remaining(), 112)
                self.assertEqual(caller.accounting.remaining, 190)
                self.assertEqual(
                    next(
                        b.undispatched
                        for b in caller.accounting.blocks
                        if b.block_id == caller._terminal_block
                    ),
                    50,
                )
                self.assertEqual(caller.accounting.uncertain, 0)
                self.assertEqual(
                    len(
                        fixture.selected.ledger._quarantine_authenticated.state[
                            "settlements"
                        ]
                    ),
                    0,
                )
