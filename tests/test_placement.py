import unittest

from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.models import ReviewComment, ReviewInputError
from review_sensei.placement import (
    ADVISORY_PLACEMENT_NOTE,
    BODY_PLACEMENT,
    BODY_PLACEMENT_NOTE,
    BODY_REASON_ADVISORY,
    BODY_REASON_CONVERSATION_RESOLUTION,
    BODY_REASON_HUMAN_ASSESSMENT,
    BODY_REASON_OPTIONAL_POLICY,
    BODY_REASON_UNANCHORED,
    CONVERSATION_RESOLUTION_NOT_REQUIRED,
    CONVERSATION_RESOLUTION_PLACEMENT_NOTE,
    CONVERSATION_RESOLUTION_REQUIRED,
    CONVERSATION_RESOLUTION_UNKNOWN,
    INLINE_PLACEMENT,
    HostPlacementFacts,
    placement_note,
    plan_finding_placement,
    plan_finding_placement_detail,
)


def _facts(state=CONVERSATION_RESOLUTION_NOT_REQUIRED):
    return HostPlacementFacts(conversation_resolution=state)


def _comment(*, blocking=False, needs_human=False):
    return ReviewComment(
        path="src/app.py",
        line=2,
        body="Handle the missing value.",
        blocking=blocking,
        needs_human=needs_human,
    )


class HostPlacementFactsTests(unittest.TestCase):
    def test_defaults_to_unknown_and_never_permits_optional_threads(self):
        facts = HostPlacementFacts()
        self.assertEqual(facts.conversation_resolution, CONVERSATION_RESOLUTION_UNKNOWN)
        self.assertFalse(facts.optional_threads_permitted)

    def test_only_not_required_permits_optional_threads(self):
        self.assertTrue(_facts().optional_threads_permitted)
        self.assertFalse(
            _facts(CONVERSATION_RESOLUTION_REQUIRED).optional_threads_permitted
        )

    def test_rejects_an_unknown_conversation_resolution_state(self):
        with self.assertRaises(ReviewInputError):
            HostPlacementFacts(conversation_resolution="maybe")


class FindingPlacementTests(unittest.TestCase):
    def _plan(
        self, comment, *, anchor="right", facts=None, policy=None, advisory=False
    ):
        return plan_finding_placement(
            comment,
            anchor=anchor,
            facts=facts if facts is not None else _facts(),
            policy=policy if policy is not None else ReviewConvergencePolicy(),
            advisory=advisory,
        )

    def test_required_findings_stay_inline_even_when_threads_are_required(self):
        for state in (
            CONVERSATION_RESOLUTION_REQUIRED,
            CONVERSATION_RESOLUTION_UNKNOWN,
            CONVERSATION_RESOLUTION_NOT_REQUIRED,
        ):
            with self.subTest(state=state):
                planned = self._plan(_comment(blocking=True), facts=_facts(state))
                self.assertEqual(planned, INLINE_PLACEMENT)

    def test_required_findings_without_a_valid_anchor_go_to_the_body(self):
        for anchor in ("summary", "file"):
            with self.subTest(anchor=anchor):
                self.assertEqual(
                    self._plan(_comment(blocking=True), anchor=anchor),
                    BODY_PLACEMENT,
                )

    def test_optional_findings_go_inline_only_when_rules_permit(self):
        legacy = ReviewConvergencePolicy(mode="legacy")
        self.assertEqual(self._plan(_comment(), policy=legacy), INLINE_PLACEMENT)
        for state in (
            CONVERSATION_RESOLUTION_REQUIRED,
            CONVERSATION_RESOLUTION_UNKNOWN,
        ):
            with self.subTest(state=state):
                self.assertEqual(
                    self._plan(_comment(), policy=legacy, facts=_facts(state)),
                    BODY_PLACEMENT,
                )

    def test_operator_modes_consolidate_optional_findings_in_the_body(self):
        policy = ReviewConvergencePolicy(mode="merge-focused")
        self.assertEqual(self._plan(_comment(), policy=policy), BODY_PLACEMENT)

    def test_advisory_mode_never_creates_a_thread(self):
        policy = ReviewConvergencePolicy(mode="advisory")
        self.assertEqual(
            self._plan(_comment(blocking=True), policy=policy, advisory=True),
            BODY_PLACEMENT,
        )

    def test_human_assessment_is_labeled_in_the_body_not_enforced_inline(self):
        self.assertEqual(
            self._plan(_comment(blocking=True, needs_human=True)), BODY_PLACEMENT
        )

    def test_legacy_policy_keeps_optional_inline_threads_on_permitted_branches(self):
        legacy = ReviewConvergencePolicy(mode="legacy")
        self.assertEqual(self._plan(_comment(), policy=legacy), INLINE_PLACEMENT)
        self.assertEqual(
            self._plan(
                _comment(),
                policy=legacy,
                facts=_facts(CONVERSATION_RESOLUTION_REQUIRED),
            ),
            BODY_PLACEMENT,
        )

    def test_rejects_invalid_inputs(self):
        with self.assertRaises(ReviewInputError):
            self._plan(_comment(), anchor="middle")
        with self.assertRaises(ReviewInputError):
            plan_finding_placement(
                _comment(),
                anchor="right",
                facts=_facts(),
                policy=ReviewConvergencePolicy(),
                advisory="yes",
            )
        with self.assertRaises(ReviewInputError):
            plan_finding_placement(
                "not a comment",
                anchor="right",
                facts=_facts(),
                policy=ReviewConvergencePolicy(),
                advisory=False,
            )


class PlacementReasonTests(unittest.TestCase):
    def _detail(
        self, comment, *, anchor="right", facts=None, policy=None, advisory=False
    ):
        return plan_finding_placement_detail(
            comment,
            anchor=anchor,
            facts=facts if facts is not None else _facts(),
            policy=policy if policy is not None else ReviewConvergencePolicy(),
            advisory=advisory,
        )

    def test_each_body_rule_reports_its_own_reason(self):
        cases = (
            (
                "advisory",
                _comment(blocking=True),
                {"policy": ReviewConvergencePolicy(mode="advisory"), "advisory": True},
                BODY_REASON_ADVISORY,
            ),
            (
                "unanchored",
                _comment(blocking=True),
                {"anchor": "file"},
                BODY_REASON_UNANCHORED,
            ),
            (
                "human-assessment",
                _comment(blocking=True, needs_human=True),
                {},
                BODY_REASON_HUMAN_ASSESSMENT,
            ),
            (
                "optional-policy",
                _comment(),
                {"policy": ReviewConvergencePolicy(mode="merge-focused")},
                BODY_REASON_OPTIONAL_POLICY,
            ),
            (
                "conversation-resolution",
                _comment(),
                {
                    "policy": ReviewConvergencePolicy(mode="legacy"),
                    "facts": _facts(CONVERSATION_RESOLUTION_REQUIRED),
                },
                BODY_REASON_CONVERSATION_RESOLUTION,
            ),
        )
        for name, comment, options, reason in cases:
            with self.subTest(rule=name):
                self.assertEqual(
                    self._detail(comment, **options), (BODY_PLACEMENT, reason)
                )

    def test_inline_placement_reports_no_body_reason(self):
        self.assertEqual(
            self._detail(_comment(blocking=True)), (INLINE_PLACEMENT, None)
        )
        self.assertEqual(
            self._detail(_comment(), policy=ReviewConvergencePolicy(mode="legacy")),
            (INLINE_PLACEMENT, None),
        )

    def test_placement_note_uses_the_deciding_rule(self):
        self.assertEqual(placement_note([]), BODY_PLACEMENT_NOTE)
        self.assertEqual(
            placement_note([BODY_REASON_ADVISORY]), ADVISORY_PLACEMENT_NOTE
        )
        # A run-level advisory condition outranks a per-finding rule.
        self.assertEqual(
            placement_note([BODY_REASON_CONVERSATION_RESOLUTION, BODY_REASON_ADVISORY]),
            ADVISORY_PLACEMENT_NOTE,
        )
        self.assertEqual(
            placement_note([BODY_REASON_UNANCHORED, BODY_REASON_OPTIONAL_POLICY]),
            BODY_PLACEMENT_NOTE,
        )
        self.assertEqual(
            placement_note(
                [BODY_REASON_OPTIONAL_POLICY, BODY_REASON_CONVERSATION_RESOLUTION]
            ),
            CONVERSATION_RESOLUTION_PLACEMENT_NOTE,
        )


if __name__ == "__main__":
    unittest.main()
