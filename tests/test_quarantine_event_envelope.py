"""Detached codec bounds; no native producer, census or funding is replaced."""

import random
import string
import unittest

from review_sensei_github.quarantine_creation import _body
from review_sensei_github.quarantine_event_envelope import one_part_event_envelope

from review_sensei.bounded_evidence import (
    MAX_PART_BYTES,
    PART_FRAMING_RESERVE,
    canonical_bytes,
    evidence_manifest,
    partition_evidence,
)
from review_sensei.errors import ReviewInputError
from review_sensei.quarantine_state import assessment_cursor, next_assessment_event
from tests.test_quarantine_lineage import decision, lineage_value


class QuarantineEventEnvelopeTests(unittest.TestCase):
    def test_full_chain_and_unicode_reasons_fit_without_compression_assumption(self):
        bound = one_part_event_envelope()
        rng = random.Random(387)
        reasons = [
            "¡" * 2048,
            "😀" * 1024,
            "漢" * 1365,
            '"\\' * 2048,
            "".join(rng.choices(string.ascii_letters + string.digits, k=4096)),
        ]
        binding = {
            **lineage_value()["origin_manifest"]["binding"],
            "repository": "a" * 127 + "/" + "b" * 128,
            "repository_id": 2_147_483_647,
            "pull_request": 2_147_483_647,
            "producer": "github-bot:" + "9" * 19,
            "policy_digest": "0" * 64,
        }
        cursor = assessment_cursor(scope="0" * 64)
        for index in range(1, 201):
            selected = {
                **decision("a" * 64),
                "instance": f"{index:064x}",
                "reason": reasons[(index - 1) % len(reasons)],
                "actor": "a" * 39,
                "action": "accept-risk",
                "role": "maintain",
                "source_id": 9_007_199_254_740_991,
            }
            event = next_assessment_event(cursor, selected)
            part_binding = {**binding, "generation": 2_147_483_447 + index}
            parts = partition_evidence(event, binding=part_binding, item_count=1)
            self.assertEqual(len(parts), 1)
            body = _body(parts[0])
            self.assertLessEqual(len(canonical_bytes(event)), bound.decoded_bytes)
            self.assertLessEqual(len(canonical_bytes(parts[0])), bound.part_bytes)
            self.assertLessEqual(len(body.encode()), bound.body_bytes)
            self.assertLessEqual(
                len(canonical_bytes({"body": body})), bound.body_json_bytes
            )
            manifest = evidence_manifest(
                event,
                binding=part_binding,
                item_count=1,
                parts=parts,
                storage_ids=(f"{index:064x}",),
            )
            cursor = assessment_cursor(
                scope="0" * 64, count=index, tail_manifest=manifest
            )
        with self.assertRaises(ReviewInputError):
            next_assessment_event(cursor, selected)

    def test_envelope_keeps_full_archive_slots_and_is_not_authority(self):
        bound = one_part_event_envelope()
        self.assertLess(bound.body_json_bytes, MAX_PART_BYTES + 512)
        self.assertLessEqual(bound.part_bytes + PART_FRAMING_RESERVE, MAX_PART_BYTES)
        # Still109 allocations:52 events,52 full archives,5 unchanged extras.
        generic = 109 * (MAX_PART_BYTES + 512)
        typed = 52 * bound.body_json_bytes + 57 * (MAX_PART_BYTES + 512)
        self.assertLess(typed, generic)
        self.assertFalse(hasattr(bound, "manifest"))
        self.assertFalse(hasattr(bound, "authorize"))

    def test_typed_events_alone_do_not_qualify_complete_census(self):
        from review_sensei_github.packed_comment_census import CommentAllocationPlan
        from review_sensei_github.quarantine_capacity import full_review_comment_tail

        bound = one_part_event_envelope()
        slots = (
            *full_review_comment_tail(),
            *([bound.body_json_bytes] * 52),
            *([MAX_PART_BYTES + 512] * 57),
        )
        # Even this optimistic absence of all retained bodies does not qualify.
        # This is NOT an observed frontier or a source-admission certificate.
        self.assertEqual(CommentAllocationPlan({}, slots).worst_read_calls(), 11)

    def test_reason_beyond_existing_limit_still_rejected(self):
        with self.assertRaises(ReviewInputError):
            next_assessment_event(
                assessment_cursor(scope="0" * 64),
                {**decision("a" * 64), "reason": "¡" * 2049},
            )
