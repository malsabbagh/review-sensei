"""Mutually exclusive checkpoint futures retain their own cumulative costs."""

import hashlib
import tempfile
import unittest
from unittest.mock import patch

from review_sensei import finding_lifecycle_store as store
from review_sensei.errors import ReviewInputError
from tests.test_finding_lifecycle_parts import CurrentRunFindingPartsTests


class CheckpointBranchTests(unittest.TestCase):
    def test_branch_totals_use_one_frontier_not_sum_of_unreachable_paths(self):
        with tempfile.TemporaryDirectory() as root:
            fixture, record, arguments = CurrentRunFindingPartsTests().fixture(root)
            arguments = dict(arguments)
            arguments.pop("document")
            documents = tuple(
                {
                    "data": "".join(
                        hashlib.sha256(f"{n}-{i}".encode()).hexdigest()
                        for i in range(width)
                    )
                }
                for n, width in enumerate((200, 210, 220, 350))
            )
            alternatives = (
                ((documents[0], documents[1], documents[2]), frozenset({0, 1})),
                ((documents[0], documents[3]), frozenset({0})),
            )

            def measure(branches):
                with (
                    patch.object(
                        store, "_consume_checkpoint_branches", return_value=branches
                    ),
                    patch.object(
                        store,
                        "_refine_checkpoint_pricing",
                        wraps=store._refine_checkpoint_pricing,
                    ) as observation,
                    patch.object(
                        store,
                        "_remote_inline_plan",
                        side_effect=AssertionError("per-branch remote read"),
                    ),
                ):
                    result = store.preflight_finding_lifecycle(
                        fixture.ledger,
                        record,
                        document=branches[0][0][0],
                        future_documents=branches[0][0][1:],
                        branch_documents=object(),
                        **arguments,
                    )
                    self.assertEqual(observation.call_count, 1)
                return result

            singles = [measure((branch,)) for branch in alternatives]
            result = measure(alternatives)
            for key in ("added_bytes", "added_parts", "conditional_calls"):
                self.assertEqual(result[key], max(one[key] for one in singles))
            self.assertLess(
                result["added_bytes"], sum(one["added_bytes"] for one in singles)
            )
            with patch.object(
                fixture.ledger.evidence_budget,
                "preflight",
                side_effect=ReviewInputError("one short"),
            ):
                with self.assertRaisesRegex(ReviewInputError, "one short"):
                    measure(alternatives)

    def test_unissued_caller_branch_flag_and_known_method_cannot_grant_exactness(self):
        from review_sensei_github.finding_lifecycle import FindingLifecycleJournal

        with tempfile.TemporaryDirectory() as root:
            fixture, record, arguments = CurrentRunFindingPartsTests().fixture(root)
            before = list(fixture.service.trace)
            with self.assertRaisesRegex(ReviewInputError, "unissued"):
                store.preflight_finding_lifecycle(
                    fixture.ledger,
                    record,
                    branch_documents={"exact": True},
                    **arguments,
                )
            with self.assertRaisesRegex(ReviewInputError, "unqualified"):
                store._issue_checkpoint_branches(
                    FindingLifecycleJournal.preflight_command_finalization,
                    fixture.ledger,
                    record,
                    ((({},), frozenset({0})),),
                    lambda: True,
                )
            self.assertEqual(fixture.service.trace, before)
