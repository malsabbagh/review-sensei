"""Checkout-only pure window/drift native event classification matrix."""

import unittest
from dataclasses import replace

from review_sensei_github import ReviewPublisher
from test_human_assessment import BASE, HEAD


class NativeEventClassificationTests(unittest.TestCase):
    def test_window_and_drift_native_events_distinguish_human_gates_from_fixes(self):
        from review_sensei_github.publication import (
            CHANGES_REQUIRED_STATE,
            NO_REQUIRED_FIXES_STATE,
            WITHHELD_STATE,
            AssembledReview,
        )
        from test_active_window_lifecycle_g10 import _comment, _result

        from review_sensei.active_finding_window import evaluate_active_window_round
        from review_sensei.convergence import ReviewConvergencePolicy
        from review_sensei.finding_drift import evaluate_drift_round
        from review_sensei.publication_selection import PublishedSlot

        assembled = AssembledReview(
            inline_comments=(),
            index_entries=(),
            head_sha=HEAD,
            state=NO_REQUIRED_FIXES_STATE,
            reason=None,
            overview="Complete review.",
            coverage_line=None,
            required_views=(),
            optional_views=(),
            limitations=(),
            placement_note=None,
            body_explanations=(),
            inline_blocker_instances=(),
            requests_changes=False,
        )
        human = _comment(blocking=False, needs_human=True)
        for mode, evaluate, adjust in (
            (
                "window",
                evaluate_active_window_round,
                ReviewPublisher._adjust_assembled_for_window,
            ),
            (
                "drift",
                evaluate_drift_round,
                ReviewPublisher._adjust_assembled_for_drift,
            ),
        ):
            for case in (
                "human",
                "hidden_human",
                "blocking",
                "omitted_human",
                "uncertain_slot",
            ):
                with self.subTest(mode=mode, case=case):
                    findings = (human,)
                    if case == "hidden_human":
                        findings += (
                            _comment(
                                blocking=False,
                                needs_human=True,
                                symbol="second",
                                body="Independent human decision.",
                            ),
                        )
                    elif case == "blocking":
                        findings = (_comment(blocking=True, needs_human=False),)
                    state = evaluate(
                        _result(*findings),
                        head_sha=HEAD,
                        base_sha=BASE,
                        max_active_findings=1,
                    )
                    inventory = (
                        state.inventory
                        if mode == "drift"
                        else state.authoritative_decision_inventory
                    )
                    # This approval-gating domain property must not be weakened
                    # just to select the proper native GitHub review event.
                    self.assertTrue(inventory.blocking_obligations)
                    if case == "omitted_human":
                        state = evaluate(
                            _result(),
                            head_sha="c" * 40,
                            base_sha=BASE,
                            max_active_findings=1,
                            prior_inventory=inventory,
                            round_number=2,
                        )
                    elif case == "uncertain_slot":
                        slots = (PublishedSlot("PRRT_unknown", "uncertain", "f" * 64),)
                        state = replace(
                            state,
                            **(
                                {"slots": slots, "active_entries": ()}
                                if mode == "drift"
                                else {
                                    "display_active_findings": replace(
                                        state.display_active_findings,
                                        slots=slots,
                                        entries=(),
                                    )
                                }
                            ),
                        )
                    actual = adjust(
                        assembled,
                        state,
                        convergence_policy=ReviewConvergencePolicy(
                            enforcement="publication"
                        ),
                        auto_approval=True,
                    )
                    requires_changes = case in {
                        "blocking",
                        "omitted_human",
                        "uncertain_slot",
                    }
                    self.assertEqual(actual.requests_changes, requires_changes)
                    self.assertEqual(
                        actual.state,
                        CHANGES_REQUIRED_STATE if requires_changes else WITHHELD_STATE,
                    )
                    self.assertFalse(
                        state.approved
                        if mode == "drift"
                        else state.approval_eligibility.approved
                    )
