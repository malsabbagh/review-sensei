"""Exact existing quarantine part writer honors its quoted remaining tail."""

import copy
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from review_sensei_github import quarantine_census as census
from test_quarantine_activation import event, registered_resource
from test_quarantine_journey import JourneyFixture

from review_sensei import finding_lifecycle_store as core
from review_sensei.errors import ReviewInputError


class QuarantineStagingPricingTests(unittest.TestCase):
    def exercise(self, *, one_short):
        with tempfile.TemporaryDirectory() as directory:
            fixture = JourneyFixture(directory)
            old, registration = fixture.original_origins()
            fixture.revision(fixture.live_head, binary=True)
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
                current_tail.append(kwargs.get("reserved_tail_calls", 0))
                try:
                    return stage(*args, **kwargs)
                finally:
                    current_tail.pop()

            @contextmanager
            def narrowed(scope):
                with activate(scope):
                    if selected:
                        yield
                        return
                    budget = scope.caller.budget_view
                    quoted = scope.plan.worst_read_calls() + 6 + 2 * scope.parts
                    tail = current_tail[-1]
                    original_hold = budget._ordinary_hold
                    remaining = budget.ordinary_remaining()
                    self.assertGreaterEqual(remaining, quoted + tail)
                    before = len(fixture.trace)
                    selected.append((quoted, tail, before))
                    budget._ordinary_hold += remaining - quoted - tail + int(one_short)
                    try:
                        yield
                    finally:
                        selected[0] += (len(fixture.trace),)
                        budget._ordinary_hold = original_hold

            def observe(proof, ledger, record, parts, **kwargs):
                issued = core._CHECKPOINT_PRICING.get(proof)
                if issued is not None and issued[9] == "quarantine-staging-v1":
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
                return consume(proof, ledger, record, parts, **kwargs)

            with (
                patch.object(core, "stage_finding_lifecycle", staging),
                patch.object(census, "activate_lifecycle_capacity", narrowed),
                patch.object(core, "_consume_checkpoint_pricing", observe),
                registered_resource([registration]),
            ):
                result = fixture.invoke()
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
                self.assertTrue(
                    any(
                        method == "POST" and path.endswith("/issues/7/comments")
                        for _, method, path in calls
                    )
                )
                self.assertTrue(any(method == "PATCH" for _, method, _ in calls))

    def test_actual_exact_quote_keeps_reserved_tail_and_confirms_part_and_root(self):
        self.exercise(one_short=False)

    def test_one_short_holds_before_part_or_root_mutation(self):
        self.exercise(one_short=True)
