"""Individual immutable decision carriers avoid rewriting all earlier decisions."""

import random
import string
import unittest
from copy import deepcopy

from test_quarantine_lineage import decision, lineage_value

from review_sensei.bounded_evidence import (
    AuthenticatedPart,
    EvidenceReadBudget,
    canonical_bytes,
    evidence_manifest,
    partition_evidence,
)
from review_sensei.errors import ReviewInputError


def chain(count=3, *, maximal_reasons=False):
    from review_sensei.quarantine_state import assessment_cursor, next_assessment_event

    binding = {
        **lineage_value()["origin_manifest"]["binding"],
        "policy_digest": "0" * 64,
        "generation": 200,
    }
    cursor = assessment_cursor(scope="0" * 64)
    storage = {}
    rng = random.Random(387)
    for index in range(1, count + 1):
        selected = {**decision("a" * 64), "instance": f"{index:064x}"}
        if maximal_reasons:
            selected["reason"] = "".join(
                rng.choices(string.ascii_letters + string.digits + " ,.;:", k=4096)
            )
        event = next_assessment_event(cursor, selected)
        part_binding = {**binding, "generation": index}
        parts = partition_evidence(event, binding=part_binding, item_count=1)
        identifiers = tuple(str(len(storage) + i + 1) for i in range(len(parts)))
        storage.update(
            {
                identifier: AuthenticatedPart(part, binding["producer"])
                for identifier, part in zip(identifiers, parts)
            }
        )
        manifest = evidence_manifest(
            event,
            binding=part_binding,
            item_count=1,
            parts=parts,
            storage_ids=identifiers,
        )
        cursor = assessment_cursor(scope="0" * 64, count=index, tail_manifest=manifest)
    return binding, cursor, storage


class QuarantineStateChainTests(unittest.TestCase):
    def test_complete_chain_retains_individual_decision_order_without_rewriting_history(
        self,
    ):
        from review_sensei.quarantine_state import read_assessment_state

        binding, cursor, storage = chain(52, maximal_reasons=True)
        restored = read_assessment_state(
            cursor,
            expected_binding=binding,
            reader=storage.__getitem__,
            budget=EvidenceReadBudget(),
        )
        self.assertEqual(len(storage), 52)
        self.assertLess(
            sum(len(canonical_bytes(part.document)) for part in storage.values()),
            524288,
        )
        self.assertEqual(len(restored["settlements"]), 52)
        self.assertEqual(
            [d["instance"] for d in restored["settlements"]],
            [f"{i:064x}" for i in range(1, 53)],
        )

    def test_missing_corrupt_foreign_and_wrong_count_chain_refuse(self):
        from review_sensei.quarantine_state import read_assessment_state

        binding, cursor, storage = chain()
        for changed in ({**cursor, "count": 2}, {**cursor, "scope_sha256": "f" * 64}):
            with (
                self.subTest(count=changed["count"]),
                self.assertRaises(ReviewInputError),
            ):
                read_assessment_state(
                    changed,
                    expected_binding=binding,
                    reader=storage.__getitem__,
                    budget=EvidenceReadBudget(),
                )
        corrupt = deepcopy(storage)
        part = corrupt["2"]
        corrupt["2"] = AuthenticatedPart(
            {**part.document, "data": "eA=="}, part.producer
        )
        with self.assertRaises(ReviewInputError):
            read_assessment_state(
                cursor,
                expected_binding=binding,
                reader=corrupt.__getitem__,
                budget=EvidenceReadBudget(),
            )
        with self.assertRaises(KeyError):
            read_assessment_state(
                cursor,
                expected_binding=binding,
                reader=lambda key: {}[key],
                budget=EvidenceReadBudget(),
            )

    def test_cursor_and_append_bounds_are_closed(self):
        from review_sensei.quarantine_state import (
            assessment_cursor,
            next_assessment_event,
        )

        for count in (True, -1, 201):
            with self.subTest(count=count), self.assertRaises(ReviewInputError):
                assessment_cursor(scope="0" * 64, count=count)
        binding, cursor, storage = chain(200)
        with self.assertRaises(ReviewInputError):
            next_assessment_event(cursor, decision("a" * 64))
