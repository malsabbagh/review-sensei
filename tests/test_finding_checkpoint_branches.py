"""Mutually exclusive checkpoint futures retain their own cumulative costs."""

import copy
import hashlib
import tempfile
import unittest
from unittest.mock import patch

from original_source_observation import observe_original

from review_sensei import finding_lifecycle_store as store
from review_sensei.errors import ReviewInputError
from tests import test_finding_lifecycle_parts as parts_fixtures


class CheckpointBranchTests(unittest.TestCase):
    def test_branch_totals_use_one_frontier_not_sum_of_unreachable_paths(self):
        with tempfile.TemporaryDirectory() as root:
            fixture, record, arguments = (
                parts_fixtures.CurrentRunFindingPartsTests().fixture(root)
            )
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
                (documents[0], documents[1], documents[2]),
                (documents[0], documents[3]),
            )
            measured = []

            def capture(frame):
                # Original conservative checkpoint measurements only. No fake
                # exact-document/branch token or protected producer replacement.
                measured.extend(copy.deepcopy(frame.f_locals["measured"]))

            singles = []
            for branch in alternatives:
                with observe_original(
                    store._price_checkpoint_alternatives, entered=capture
                ):
                    singles.append(
                        store.preflight_finding_lifecycle(
                            fixture.ledger,
                            record,
                            document=branch[0],
                            future_documents=branch[1:],
                            **arguments,
                        )
                    )
            self.assertEqual(len(measured), len(alternatives))

            def measure():
                refinements = []

                def unexpected_inline_read(frame):
                    self.fail("per-branch remote read")

                with (
                    observe_original(
                        store._refine_checkpoint_pricing,
                        entered=lambda frame: refinements.append(
                            frame.f_locals["record"]
                        ),
                    ),
                    observe_original(
                        store._remote_inline_plan, entered=unexpected_inline_read
                    ),
                ):
                    _, parts, retained_bytes, calls, _ = (
                        store._price_checkpoint_alternatives(
                            fixture.ledger,
                            record,
                            copy.deepcopy(measured),
                            budget=fixture.ledger.evidence_budget,
                            branching=True,
                            reserved_tail_calls=0,
                        )
                    )
                    self.assertEqual(refinements, [record])
                return dict(
                    added_parts=parts,
                    added_bytes=retained_bytes,
                    conditional_calls=calls,
                )

            result = measure()
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
                    measure()

    def test_unissued_caller_branch_flag_and_known_method_cannot_grant_exactness(self):
        from review_sensei_github.finding_lifecycle import FindingLifecycleJournal

        with tempfile.TemporaryDirectory() as root:
            fixture, record, arguments = (
                parts_fixtures.CurrentRunFindingPartsTests().fixture(root)
            )
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
