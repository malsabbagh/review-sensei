"""Exact existing quarantine part writer honors its quoted remaining tail."""

import copy
import dis
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from review_sensei_github import quarantine_census as census
from test_quarantine_activation import event, registered_resource
from test_quarantine_journey import JourneyFixture

from review_sensei import finding_lifecycle_store as core
from review_sensei.errors import ReviewInputError


class QuarantineStagingPricingTests(unittest.TestCase):
    def exercise(self, *, one_short, inline=False):
        with tempfile.TemporaryDirectory() as directory:
            fixture = JourneyFixture(directory)
            old, registration = fixture.original_origins()
            if inline:
                fixture.revision(fixture.live_head, binary=True)
            if not inline:
                import random
                import string

                rng = random.Random(8129)
                fixture.provider_comments = [
                    {
                        "path": "src/app.py",
                        "line": 2,
                        "side": "RIGHT",
                        "body": "Independently confirm this external requirement. "
                        + "".join(
                            rng.choices(string.ascii_letters + string.digits, k=3000)
                        ),
                        "severity": "high",
                        "blocking": False,
                        "needs_human": True,
                    }
                ]
            registration["head_sha"] = fixture.live_head
            fixture.event_document = event(registration)
            stage, activate = (
                core.stage_finding_lifecycle,
                census.activate_lifecycle_capacity,
            )
            current_tail = []
            selected = []
            proofs = []
            consume = core._consume_checkpoint_pricing

            def staging(*args, **kwargs):
                if inline and not selected:
                    before_pricing = len(fixture.trace)
                    measured = core.preflight_finding_lifecycle(
                        *args, future_documents=(kwargs["document"],), **kwargs
                    )
                    scope = census.lifecycle_capacity_plan(
                        args[0], args[1].identity(), added_parts=0
                    )
                    self.assertEqual(
                        measured["conditional_calls"],
                        2 * (scope.plan.worst_read_calls() + 6),
                    )
                    self.assertEqual(len(fixture.trace), before_pricing)
                current_tail.append(kwargs.get("reserved_tail_calls", 0))

            holds = {}

            def narrowed(scope, frame):
                if selected or (scope.parts == 0) != inline:
                    return
                budget = scope.caller.budget_view
                quoted = scope.plan.worst_read_calls() + 6 + 2 * scope.parts
                if inline:
                    before_pricing = len(fixture.trace)
                    measured = core._remote_inline_plan(scope.ledger, scope.record)
                    self.assertEqual(measured["conditional_calls"], quoted)
                    self.assertEqual(len(fixture.trace), before_pricing)
                tail = current_tail[-1]
                original_hold = budget._ordinary_hold
                remaining = budget.ordinary_remaining()
                self.assertGreaterEqual(remaining, quoted + tail)
                before = len(fixture.trace)
                selected.append((quoted, tail, before))
                budget._ordinary_hold += remaining - quoted - tail + int(one_short)
                holds[id(frame)] = (budget, original_hold)

            def observe(proof, ledger, record, parts, **kwargs):
                issued = core._CHECKPOINT_PRICING.get(proof)
                if issued is not None and issued[9] == (
                    "quarantine-inline-staging-v1"
                    if inline
                    else "quarantine-staging-v1"
                ):
                    self.assertIsNot(record, ledger.caller._effect_record)
                    self.assertEqual(issued[5], (2 * parts[0] + 5,))
                    self.assertIsNotNone(kwargs.get("staging_target"))
                    if not proofs:
                        before = len(fixture.trace)
                        with self.assertRaisesRegex(
                            ReviewInputError, "changed or reused"
                        ):
                            consume(copy.copy(proof), ledger, record, parts, **kwargs)
                        target = kwargs["staging_target"]
                        alternate = (
                            ledger.preflight_finding_lifecycle_checkpoint_pricing(
                                record,
                                added_parts=parts[0],
                                added_bytes=0,
                                checkpoint_parts=parts,
                                checkpoint_stage=True,
                                checkpoint_target=target,
                            )
                        )
                        changed = copy.deepcopy(target)
                        changed["document_sha256"] = "f" * 64
                        with self.assertRaisesRegex(ReviewInputError, "target changed"):
                            consume(
                                alternate, ledger, record, parts, staging_target=changed
                            )
                        with patch.object(
                            ledger, "_quarantine_checkpoint_snapshot", None
                        ):
                            self.assertIsNone(
                                ledger.preflight_finding_lifecycle_checkpoint_pricing(
                                    record,
                                    added_parts=parts[0],
                                    added_bytes=0,
                                    checkpoint_parts=parts,
                                    checkpoint_stage=True,
                                    checkpoint_target=target,
                                )
                            )
                        snapshot = ledger._quarantine_checkpoint_snapshot
                        for changed_snapshot in (copy.copy(snapshot), object()):
                            with patch.object(
                                ledger,
                                "_quarantine_checkpoint_snapshot",
                                changed_snapshot,
                            ):
                                self.assertIsNone(
                                    ledger.preflight_finding_lifecycle_checkpoint_pricing(
                                        record,
                                        added_parts=parts[0],
                                        added_bytes=0,
                                        checkpoint_parts=parts,
                                        checkpoint_stage=True,
                                        checkpoint_target=target,
                                    )
                                )
                        self.assertIsNone(
                            ledger.preflight_finding_lifecycle_checkpoint_pricing(
                                copy.copy(record),
                                added_parts=parts[0],
                                added_bytes=0,
                                checkpoint_parts=parts,
                                checkpoint_stage=True,
                                checkpoint_target=target,
                            )
                        )
                        self.assertEqual(len(fixture.trace), before)
                    proofs.append(issued[5])

            activation_code = next(
                code
                for code in activate.__code__.co_consts
                if isinstance(code, types.CodeType) and code.co_name == "active"
            )
            yields = {
                i.offset
                for i in dis.get_instructions(activation_code)
                if i.opname == "YIELD_VALUE"
            }

            def profile(frame, event, value):
                if (
                    frame.f_code is stage.__code__
                    and frame.f_globals is stage.__globals__
                ):
                    if event == "call":
                        values = dict(frame.f_locals)
                        ledger, record = values.pop("ledger"), values.pop("record")
                        staging(ledger, record, **values)
                    elif event == "return":
                        current_tail.pop()
                elif (
                    frame.f_code is activation_code
                    and frame.f_globals is activate.__globals__
                ):
                    if event == "return" and frame.f_lasti in yields:
                        narrowed(frame.f_locals["scope"], frame)
                    elif event == "call" and id(frame) in holds:
                        budget, original_hold = holds.pop(id(frame))
                        selected[0] += (len(fixture.trace),)
                        budget._ordinary_hold = original_hold
                elif (
                    event == "call"
                    and frame.f_code is consume.__code__
                    and frame.f_globals is consume.__globals__
                ):
                    values = frame.f_locals
                    observe(
                        values["proof"],
                        values["ledger"],
                        values["record"],
                        values["checkpoint_parts"],
                        staging_target=values["staging_target"],
                    )

            previous = sys.getprofile()
            try:
                sys.setprofile(profile)
                with registered_resource([registration]):
                    result = fixture.invoke(human=not inline)
            finally:
                sys.setprofile(previous)
                for budget, original_hold in holds.values():
                    budget._ordinary_hold = original_hold
            self.assertTrue(selected)
            if not one_short:
                self.assertTrue(
                    proofs,
                    "fresh parsed checkpoint used generic staging: " + fixture.error,
                )
            quoted, tail, before, after = selected[0]
            calls = fixture.trace[before:after]
            self.assertLessEqual(len(calls), quoted)
            self.assertEqual(
                fixture.current_record().api_run_accounting["configuration"]["limit"],
                512,
            )
            self.assertIsNone(old.api_run_accounting["terminal_transaction"])
            if one_short:
                self.assertEqual(result, 1)
                self.assertFalse(
                    any(
                        method in {"PATCH", "PUT"}
                        or method == "POST"
                        and path != "/graphql"
                        for _, method, path in calls
                    ),
                    calls,
                )
            else:
                self.assertEqual(result, 0, fixture.error)
                self.assertEqual(
                    any(
                        method == "POST" and path.endswith("/issues/7/comments")
                        for _, method, path in calls
                    ),
                    not inline,
                )
                self.assertTrue(any(method == "PATCH" for _, method, _ in calls))

    def test_actual_exact_quote_keeps_reserved_tail_and_confirms_part_and_root(self):
        self.exercise(one_short=False)

    def test_one_short_holds_before_part_or_root_mutation(self):
        self.exercise(one_short=True)

    def test_actual_inline_exact_funding_preserves_full_tail(self):
        self.exercise(one_short=False, inline=True)

    def test_actual_inline_one_short_holds_before_any_census_or_mutation(self):
        self.exercise(one_short=True, inline=True)
