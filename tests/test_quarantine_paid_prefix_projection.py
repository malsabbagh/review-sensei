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
from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError


class PaidPrefixProjectionTests(unittest.TestCase):
    def test_original_paid_prefix_and_last_root_hold_preserve_all_funding(self):
        captured = []
        overflow = []

        def observe(frame, event, result):
            if (
                event == "return"
                and frame.f_code is namespace.capture_paid_prefix.__code__
            ):
                caller = frame.f_locals["caller"]
                prepared = namespace.project_prepared_prefix(caller)
                baseline = namespace.project_current(caller)
                self.assertEqual(
                    len(prepared["completed"]), len(baseline["completed"]) + 5
                )
                self.assertEqual(
                    sum(len(b["pending"]) for b in prepared["blocks"]), 155
                )
                self.assertEqual(
                    sum(len(b["pending"]) for b in baseline["blocks"]), 160
                )
                self.assertEqual(caller.budget_view.ordinary_remaining(), 105)
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
                try:
                    finalization.measure_inline_branches(observation)
                except ReviewInputError as error:
                    self.assertIn("session record exceeds", str(error))
                    trace = error.__traceback__
                    while trace is not None:
                        if (
                            trace.tb_frame.f_code.co_name == "render"
                            and trace.tb_frame.f_globals is vars(finalization)
                        ):
                            overflow.append(
                                len(
                                    canonical_bytes(trace.tb_frame.f_locals["document"])
                                )
                            )
                        trace = trace.tb_next
                else:
                    self.fail("unpriced final branch unexpectedly lost its root hold")

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
        self.assertTrue(overflow)
        self.assertTrue(all(size > 32768 for size in overflow))
