"""Guarded suffix observations remain separate from every executable lease."""

import copy
import sys
import tempfile
import unittest
from unittest.mock import patch

from review_sensei_github import quarantine_command_finalization as finalization
from review_sensei_github import quarantine_command_suffix as suffix
from review_sensei_github.command_result_capacity import raw_result_manifest_upper
from review_sensei_github.quarantine_capacity import _guard_state, installed_capacity
from test_quarantine_journey import registered_resource
from test_quarantine_published_tail_journey import (
    BROKER_QUALIFICATIONS,
    PublishedTailJourneyFixture,
)

from review_sensei.errors import ReviewInputError


class GuardedSuffixTests(unittest.TestCase):
    def test_paid_complete_proposal_cannot_grant_effects_or_survive_change(self):
        captured = []
        recipes = []
        preparation_counts = []
        rendered_inputs = []
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
                inventories = (
                    fixture.selected.ledger._quarantine_authenticated.inventories
                )
                origin, inventory = next(
                    (key, value)
                    for key, value in inventories.items()
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

                def observe(frame, event, arg):
                    if (
                        event == "return"
                        and frame.f_code
                        is finalization._root_growth_measurement.__code__
                        and arg is not None
                    ):
                        self.assertTrue(arg["original_validated_and_rendered"])
                        values = frame.f_locals
                        rendered_inputs.append(
                            (
                                values["record"],
                                values["accounting"],
                                values["witness"],
                                values["result_upper"],
                                values["grants"],
                            )
                        )
                    if (
                        event == "return"
                        and frame.f_code is suffix.prepare.__code__
                        and "before_preparation" in frame.f_locals
                    ):
                        values = frame.f_locals
                        run = values["run"]
                        preparation_counts.append(
                            (
                                len(run.caller.observations)
                                - values["before_preparation"],
                                values["ordinary_before"],
                                run.caller.budget_view.ordinary_remaining(),
                                run.caller.accounting.uncertain,
                                run.caller.accounting.prior_nonstartup_liability,
                            )
                        )
                    if (
                        event == "return"
                        and frame.f_code is suffix.prepare.__code__
                        and arg is not None
                    ):
                        recipe = frame.f_locals["recipe"]
                        self.assertEqual(recipe["normal_calls"], 98)
                        self.assertEqual(recipe["combined_loss_calls"], 105)
                        self.assertTrue(
                            recipe["root_measurement"]["all_growth_roots_fit"]
                        )
                        self.assertLess(
                            recipe["root_measurement"]["result_manifest_bound"], 8192
                        )
                        recipes.append(recipe)
                    if (
                        event != "call"
                        or frame.f_code is not suffix.require_execution.__code__
                    ):
                        return
                    token, run = frame.f_locals["token"], frame.f_locals["run"]
                    guard = installed_capacity(run.ledger, run.identity)
                    before = _guard_state(guard)
                    page = suffix.require_proposal(token, run)
                    self.assertLessEqual(page.maximum_bytes, 512 * 1024)
                    self.assertEqual(len(page.future), 2)
                    self.assertEqual(len(guard.comment_plan.unknown), 16)
                    self.assertFalse(guard.complete_tail)
                    self.assertEqual(
                        run.caller.accounting.prior_nonstartup_liability, 0
                    )
                    self.assertFalse(
                        any(block.pending for block in run.caller.accounting.blocks)
                    )
                    with self.assertRaisesRegex(ReviewInputError, "copied or expired"):
                        suffix.require_proposal(copy.copy(token), run)
                    with self.assertRaisesRegex(ReviewInputError, "copied or expired"):
                        suffix.require_proposal(token, copy.copy(run))
                    root = run.caller._root
                    try:
                        run.caller._root = "0" * 64
                        with self.assertRaisesRegex(ReviewInputError, "source changed"):
                            suffix.require_proposal(token, run)
                    finally:
                        run.caller._root = root
                    with self.assertRaisesRegex(
                        ReviewInputError, "complete root/result"
                    ):
                        suffix.require_execution(token, run)
                    self.assertEqual(_guard_state(guard), before)
                    captured.append((run.caller, len(run.caller.observations), root))

                previous = sys.getprofile()
                try:
                    sys.setprofile(observe)
                    code = fixture.invoke_command()
                finally:
                    sys.setprofile(previous)
                self.assertEqual(code, 1, fixture.error)
                self.assertEqual(
                    len(captured), 1, str(preparation_counts) + fixture.error
                )
                self.assertEqual(len(recipes), 1)
                recipe = recipes[0]
                roots = recipe["root_measurement"]["growth_roots"]
                self.measurements = {
                    "paid_preparation": preparation_counts,
                    "normal_calls": recipe["normal_calls"],
                    "combined_loss_calls": recipe["combined_loss_calls"],
                    "result_manifest_bound": recipe["root_measurement"][
                        "result_manifest_bound"
                    ],
                    "max_ordinary_root": max(
                        root["ordinary_root_bytes"] for root in roots
                    ),
                    "max_terminal_root": max(
                        root["terminal_root_bytes"] for root in roots
                    ),
                    "max_comment_body": max(
                        root["comment_body_bytes"] for root in roots
                    ),
                }
                caller, count, root = captured[0]
                self.assertEqual(len(caller.observations), count)
                self.assertEqual(caller._root, root)
                self.assertEqual(caller.accounting.action["phase"], "pending")
                self.assertFalse(caller.accounting.action["execution_started"])
                self.assertEqual(caller.accounting.remaining, 190)

                self.assertTrue(rendered_inputs)
                overflow_refusals = 0
                for record, accounting, witness, upper, grants in rendered_inputs:
                    larger = raw_result_manifest_upper(
                        envelope_bytes=4800,
                        binding=upper["binding"],
                        reference_history=True,
                    )
                    try:
                        finalization._root_growth_measurement(
                            record, accounting, witness, larger, grants
                        )
                    except ReviewInputError as error:
                        self.assertIn("size limit", str(error))
                        overflow_refusals += 1
                self.assertGreater(overflow_refusals, 0)
                self.assertEqual(len(caller.observations), count)
