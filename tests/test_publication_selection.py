"""Strict finite publication admission independent of any numeric certificate."""

import itertools
import unittest
from dataclasses import replace

from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewComment
from review_sensei.publication_selection import (
    PublicationCandidate,
    PublishedSlot,
    select_publication,
)


def candidate(
    body, *, required=False, impact=None, evidence="advisory", target=None, label=None
):
    return PublicationCandidate(
        ReviewComment(
            "src/app.py", 2, body, severity=label, effective_blocking=required
        ),
        required,
        impact,
        evidence,
        target,
    )


def slot(target, state="active"):
    return PublishedSlot(target, state, "a" * 64)


class StrictPublicationSelectionTests(unittest.TestCase):
    def test_required_verified_priority_and_stable_ties_ignore_model_labels(self):
        rows = (
            candidate("optional inflated label", label="critical"),
            candidate(
                "verified medium",
                required=True,
                impact="medium",
                evidence="causal-source",
            ),
            candidate(
                "verified high B",
                required=True,
                impact="high",
                evidence="causal-source",
            ),
            candidate(
                "verified high A",
                required=True,
                impact="high",
                evidence="causal-source",
            ),
        )
        plans = [
            select_publication(order, slots=(), max_active_findings=2)
            for order in itertools.permutations(rows)
        ]
        self.assertTrue(all(plan == plans[0] for plan in plans))
        self.assertEqual(
            {item.candidate.comment.body for item in plans[0].selected},
            {"verified high A", "verified high B"},
        )

    def test_full_cap_updates_existing_target_without_evicting_it_for_new_priority(
        self,
    ):
        existing = candidate("moved explanation", target="comment:1")
        required = candidate(
            "new blocker", required=True, impact="critical", evidence="causal-source"
        )
        plan = select_publication(
            (required, existing), slots=(slot("comment:1"),), max_active_findings=1
        )
        self.assertEqual(tuple(item.candidate for item in plan.selected), (existing,))
        self.assertFalse(plan.selected[0].consumes_slot)
        self.assertTrue(plan.required_verdict)
        self.assertNotIn("new blocker", str(plan.to_document()))
        self.assertNotIn("deferred", str(plan.to_document()))

    def test_possible_reopenings_and_unknown_writes_keep_slots_reserved(self):
        rows = (candidate("new concern"),)
        for state in ("active", "contingent", "uncertain"):
            plan = select_publication(
                rows, slots=(slot("comment:1", state),), max_active_findings=1
            )
            self.assertEqual(plan.selected, ())
        released = select_publication(
            rows, slots=(slot("comment:1", "released"),), max_active_findings=1
        )
        self.assertEqual(len(released.selected), 1)
        self.assertTrue(released.selected[0].consumes_slot)

    def test_recurrence_uses_old_target_but_competes_for_a_released_slot(self):
        recurred = candidate(
            "recurred",
            required=True,
            impact="high",
            evidence="causal-source",
            target="comment:old",
        )
        # Confirmed closure of A makes room for B without erasing A's history.
        released_a = (slot("comment:old", "released"),)
        b = candidate("next concern")
        self.assertEqual(
            select_publication((b,), slots=released_a, max_active_findings=1)
            .selected[0]
            .candidate,
            b,
        )
        history = (slot("comment:old", "released"), slot("comment:active"))
        self.assertEqual(
            select_publication(
                (recurred,), slots=history, max_active_findings=1
            ).selected,
            (),
        )
        selected = select_publication(
            (recurred,), slots=history, max_active_findings=2
        ).selected
        self.assertEqual(selected[0].candidate.existing_target, "comment:old")
        self.assertTrue(selected[0].consumes_slot)

    def test_no_inherited_overflow_exception_or_ambiguous_identity(self):
        with self.assertRaisesRegex(ReviewInputError, "exceed the strict cap"):
            select_publication((), slots=(slot("1"), slot("2")), max_active_findings=1)
        for targets, rows in (
            ((slot("1"), slot("1")), ()),
            ((slot("1"),), (candidate("a", target="missing"),)),
            ((slot("1"),), (candidate("a", target="1"), candidate("b", target="1"))),
            ((slot("1", "uncertain"),), (candidate("a", target="1"),)),
        ):
            with self.assertRaises(ReviewInputError):
                select_publication(rows, slots=targets, max_active_findings=2)

    def test_exact_duplicate_does_not_consume_a_second_slot_but_distinct_concern_does(
        self,
    ):
        a, b = candidate("a"), candidate("b")
        plan = select_publication((a, a, b), slots=(), max_active_findings=2)
        self.assertEqual(len(plan.selected), 2)
        with self.assertRaisesRegex(ReviewInputError, "conflicting admission"):
            select_publication(
                (a, replace(a, evidence="supported-source")),
                slots=(),
                max_active_findings=2,
            )

    def test_empty_completed_selection_has_no_required_verdict(self):
        plan = select_publication((), slots=(), max_active_findings=1)
        self.assertFalse(plan.required_verdict)
        self.assertEqual(plan.to_document()["selected"], [])


if __name__ == "__main__":
    unittest.main()
