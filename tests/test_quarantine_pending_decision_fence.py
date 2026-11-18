"""Pending decision permits need their actual fresh own-CAS business fence."""

import copy
import sys
import tempfile
import unittest
from types import FunctionType

from review_sensei_github import quarantine_action as action

from review_sensei.errors import ReviewInputError


class PendingDecisionFenceTests(unittest.TestCase):
    def test_actual_pending_permit_needs_exact_fence_and_cannot_cross_root_or_actor(
        self,
    ):
        from test_quarantine_journey import JourneyFixture, registered_resource

        issued, checked, entry_counts, fresh_fences = [], [], [], []
        fence_entries = {}
        authority_code = action.decision_authority.__code__
        issue_code = action._issue_decision_permit.__code__
        consume_code = action.require_native_decision.__code__

        def observe(frame, event, arg):
            if frame.f_code is authority_code:
                run = frame.f_locals["run"]
                if event == "call":
                    fence_entries[id(frame)] = len(run.caller.observations)
                elif event == "return":
                    count = len(run.caller.observations) - fence_entries.pop(id(frame))
                    self.assertEqual(count, 4)
                    fresh_fences.append(count)
            if frame.f_code is issue_code:
                run = frame.f_locals["run"]
                if event == "call":
                    entry_counts.append(len(run.caller.observations))
                elif event == "return":
                    permit = run.ledger._quarantine_decision_permit
                    self.assertEqual(len(run.caller.observations), entry_counts[-1])
                    self.assertNotIn(permit, action._FENCED_DECISIONS)
                    self.assertFalse(
                        action._has_current_decision_fence(
                            run.ledger, permit, run.caller._effect_record
                        )
                    )
                    with self.assertRaisesRegex(
                        ReviewInputError, "original fresh CAS fence"
                    ):
                        action._seal_pending_decision_fence(
                            run.ledger.business_guard, "admin"
                        )
                    self.assertNotIn(permit, action._FENCED_DECISIONS)
                    forged_globals = dict(vars(action))
                    forged_globals["decision_authority"] = lambda *_args: "admin"
                    forged = FunctionType(
                        action._DECISION_FENCE_CALL.__code__, forged_globals
                    )
                    with self.assertRaisesRegex(
                        ReviewInputError, "original fresh CAS fence"
                    ):
                        forged(run.ledger.business_guard)
                    self.assertEqual(len(run.caller.observations), entry_counts[-1])
                    issued.append(permit)
            if frame.f_code is consume_code and event == "call":
                ledger, before, after = (
                    frame.f_locals[k] for k in ("ledger", "before", "after")
                )
                if (
                    before.quarantine_lineage["assessment_state_manifest"]
                    != after.quarantine_lineage["assessment_state_manifest"]
                ):
                    permit = ledger._quarantine_decision_permit
                    self.assertIn(permit, issued)
                    self.assertTrue(
                        action._has_current_decision_fence(ledger, permit, before)
                    )
                    self.assertFalse(
                        action._has_current_decision_fence(
                            ledger, copy.copy(permit), before
                        )
                    )
                    root = ledger.caller._root
                    try:
                        ledger.caller._root = "0" * 64
                        self.assertFalse(
                            action._has_current_decision_fence(ledger, permit, before)
                        )
                    finally:
                        ledger.caller._root = root
                    document = permit.authority.document
                    login = document["actor"]["login"]
                    try:
                        document["actor"]["login"] = "different-actor"
                        self.assertFalse(
                            action._has_current_decision_fence(ledger, permit, before)
                        )
                    finally:
                        document["actor"]["login"] = login
                    checked.append(permit)
            return (
                observe
                if frame.f_code in {issue_code, consume_code, authority_code}
                else None
            )

        with tempfile.TemporaryDirectory() as directory:
            fixture = JourneyFixture(directory)
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
                instance = inventory["assessments"][0]["instance"]
                fixture.quarantine_predecessor_comment_id = None
                fixture.select_command(
                    "@reviewsensei dismiss "
                    + instance
                    + ' --reason "quarantine:'
                    + origin
                    + ' Exact source disproves this historical concern."',
                    new_run=True,
                )
                prior = sys.getprofile()
                try:
                    sys.setprofile(observe)
                    self.assertEqual(fixture.invoke_command(), 0, fixture.error)
                finally:
                    sys.setprofile(prior)
                self.assertEqual(
                    len(
                        fixture.selected.ledger._quarantine_authenticated.state[
                            "settlements"
                        ]
                    ),
                    1,
                )
                self.assertFalse(
                    any(row.get("state") == "APPROVED" for row in fixture.reviews)
                )
        self.assertTrue(fresh_fences)
        self.assertEqual(len(issued), 1)
        self.assertEqual(checked, issued)
        self.assertTrue(issued[0].consumed)
        self.assertNotIn(issued[0], action._FENCED_DECISIONS)
