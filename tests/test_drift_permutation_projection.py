"""A lossless display permutation must not invent a new native identity."""

import unittest
from dataclasses import replace

from test_finding_location_drift_g16 import (
    BASE_SHA,
    HEAD_1,
    _comment,
    _published_transaction,
    _result,
)

from review_sensei.finding_drift import (
    evaluate_drift_round,
    project_drift_review_result,
)


class DriftPermutationProjectionTests(unittest.TestCase):
    def source(self):
        comments = tuple(
            replace(
                _comment(
                    f"src/contract_{i}.py",
                    blocking=False,
                    needs_human=True,
                    symbol=f"contract_{i}",
                    defect_kind=f"contract-{i}",
                ),
                evidence_id=f"independent-{i}",
                body=f"Independent contract {i} needs confirmation.",
            )
            for i in range(3)
        )
        seed = _result(*comments, paths=tuple(comment.path for comment in comments))
        evaluation = self.evaluate(seed)
        ordered = project_drift_review_result(seed, evaluation)
        original = replace(
            ordered,
            comments=tuple(reversed(ordered.comments)),
            finding_lifecycles=tuple(reversed(ordered.finding_lifecycles)),
        )
        transaction = replace(
            _published_transaction(HEAD_1), result_sha256=original.content_digest()
        )
        return replace(original, transaction=transaction)

    @staticmethod
    def evaluate(result, count=3):
        return evaluate_drift_round(
            result,
            head_sha=HEAD_1,
            base_sha=BASE_SHA,
            max_active_findings=count,
            round_number=1,
        )

    def test_complete_permutation_retains_exact_original_transaction_and_bytes(self):
        original = self.source()
        before = original.to_dict()
        projected = project_drift_review_result(original, self.evaluate(original))
        self.assertIs(projected, original)
        self.assertIs(projected.transaction, original.transaction)
        self.assertEqual(projected.to_dict(), before)
        self.assertEqual(projected.content_digest(), original.transaction.result_sha256)

    def test_changed_values_multiplicity_flags_and_hidden_items_do_not_reuse(self):
        original = self.source()
        evaluation = self.evaluate(original)
        unbound = replace(original, transaction=None)
        first, second, third = original.comments
        variants = (
            replace(
                unbound,
                comments=(replace(first, body="Changed criterion."), second, third),
            ),
            replace(unbound, comments=(first, first, third)),
            replace(
                unbound, comments=(replace(first, needs_human=False), second, third)
            ),
            replace(
                unbound,
                comments=(replace(first, effective_blocking=True), second, third),
            ),
            replace(unbound, comments=(replace(first, line=42), second, third)),
            replace(unbound, finding_lifecycles=(original.finding_lifecycles[0],) * 3),
        )
        for candidate in variants:
            candidate = replace(
                candidate,
                transaction=replace(
                    original.transaction, result_sha256=candidate.content_digest()
                ),
            )
            with self.subTest(candidate=candidate):
                projected = project_drift_review_result(candidate, evaluation)
                self.assertIsNot(projected, candidate)
                self.assertIsNone(projected.transaction)
        hidden = project_drift_review_result(original, self.evaluate(original, count=2))
        self.assertIsNot(hidden, original)
        self.assertIsNone(hidden.transaction)
        self.assertEqual(len(hidden.comments), 2)
