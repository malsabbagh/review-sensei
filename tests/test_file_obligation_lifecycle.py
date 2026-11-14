"""Normalized file-level criteria retain their durable lifecycle identity."""

import copy
import unittest
from dataclasses import replace

from review_sensei.active_finding_window import (
    evaluate_active_window_round,
    obligation_from_comment,
)
from review_sensei.errors import ReviewInputError
from review_sensei.finding_lifecycle import (
    decode_lifecycle_state,
    encode_lifecycle_state,
)
from tests import test_finding_lifecycle_codec as fixture


class FileObligationLifecycleTests(unittest.TestCase):
    def test_file_criteria_round_trip_without_becoming_inline_or_resolved(self):
        comments = tuple(
            replace(fixture.comment(path), side="FILE", line=None)
            for path in ("src/one.py", "src/two.py")
        )
        state = evaluate_active_window_round(
            fixture.result(*comments),
            head_sha=fixture.HEAD,
            base_sha=fixture.BASE,
            max_active_findings=1,
        )
        document = encode_lifecycle_state(state)
        restored = decode_lifecycle_state(document).state
        self.assertEqual(encode_lifecycle_state(restored), document)
        obligations = restored.authoritative_decision_inventory.obligations
        self.assertEqual(len(obligations), 2)
        self.assertEqual(len(restored.display_active_findings.entries), 1)
        for comment, item in zip(comments, obligations, strict=True):
            expected = obligation_from_comment(comment)
            self.assertEqual(
                (item.side, item.line, item.native_target), ("FILE", None, None)
            )
            self.assertEqual(item.resolution_criterion, expected.resolution_criterion)
            self.assertTrue(item.blocks_approval_now)
            self.assertTrue(item.needs_human)
        self.assertFalse(restored.approval_eligibility.approved)

    def test_file_side_cannot_carry_inline_line_or_unknown_side(self):
        obligation = obligation_from_comment(
            replace(fixture.comment(), side="FILE", line=None)
        )
        for updates in ({"line": 9}, {"side": "BODY"}):
            with self.subTest(updates=updates), self.assertRaises(ReviewInputError):
                replace(obligation, **updates)

    def test_wire_file_side_with_line_is_rejected(self):
        comment = replace(fixture.comment(), side="FILE", line=None)
        state = evaluate_active_window_round(
            fixture.result(comment),
            head_sha=fixture.HEAD,
            base_sha=fixture.BASE,
            max_active_findings=1,
        )
        document = copy.deepcopy(encode_lifecycle_state(state))
        document["state"]["authoritative_decision_inventory"]["obligations"][0][
            "line"
        ] = 9
        with self.assertRaises(ReviewInputError):
            decode_lifecycle_state(document)
