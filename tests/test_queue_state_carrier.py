"""Lossless queue storage carrier; compression never supplies host authority."""

import copy
import hashlib
import unittest
from unittest.mock import patch

from review_sensei.assessment_queue import (
    MAX_OPERATION_RECEIPT_BYTES,
    AssessmentQueue,
    FactoredAssessmentJournal,
    pack_queue_state,
    unpack_queue_state,
)
from review_sensei.bounded_evidence import (
    canonical_bytes,
    decode_evidence,
    encode_evidence,
    inline_manifest,
    read_inline_evidence,
)
from review_sensei.errors import ReviewInputError
from tests.test_assessment_queue import fixture
from tests.test_authenticated_partitions import binding


class QueueStateCarrierTests(unittest.TestCase):
    def envelope(self, count=40):
        _, _, queue = fixture(count)
        return {
            "schema_version": "1.0",
            "kind": "assessment-queue-state",
            "journal": FactoredAssessmentJournal(queue).to_document(),
        }

    def test_complete_varied_40_100_250_queue_storage_roundtrip(self):
        for count in (40, 100, 250):
            with self.subTest(count=count):
                envelope = self.envelope(count)
                packed = pack_queue_state(envelope)
                self.assertEqual(unpack_queue_state(packed), envelope)
                self.assertEqual(unpack_queue_state(envelope), envelope)
                self.assertLess(
                    len(canonical_bytes(packed)), len(canonical_bytes(envelope))
                )

    def test_changed_bytes_digest_size_schema_and_extraneous_fields_refuse(self):
        original = pack_queue_state(self.envelope())
        for change in ("data", "sha256", "decoded_bytes", "schema", "extra", "kind"):
            with self.subTest(change=change):
                packed = copy.deepcopy(original)
                if change == "data":
                    packed["state"]["data"] = "AAAA"
                elif change == "sha256":
                    packed["state"]["sha256"] = "f" * 64
                elif change == "decoded_bytes":
                    packed["state"]["decoded_bytes"] = MAX_OPERATION_RECEIPT_BYTES + 1
                elif change == "schema":
                    packed["schema_version"] = "2.0"
                elif change == "extra":
                    packed["authority"] = "untrusted assertion"
                else:
                    packed["kind"] = "unknown-carrier"
                with self.assertRaises(ReviewInputError):
                    unpack_queue_state(packed)

    def test_independent_encoded_and_decoded_bounds_do_not_truncate_or_expand(self):
        packed = pack_queue_state(self.envelope())
        decoded_size = packed["state"]["decoded_bytes"]
        with patch(
            "review_sensei.assessment_queue.MAX_OPERATION_RECEIPT_BYTES",
            decoded_size - 1,
        ):
            with self.assertRaises(ReviewInputError):
                unpack_queue_state(packed)
        packed["state"]["data"] = "A" * (MAX_OPERATION_RECEIPT_BYTES + 1)
        with self.assertRaises(ReviewInputError):
            unpack_queue_state(packed)
        envelope = self.envelope()
        with patch(
            "review_sensei.assessment_queue.MAX_OPERATION_RECEIPT_BYTES",
            len(canonical_bytes(envelope)) - 1,
        ):
            with self.assertRaises(ReviewInputError):
                pack_queue_state(envelope)

    def test_existing_manifest_binding_is_still_required(self):
        context = binding(purpose="queue")
        manifest = inline_manifest(
            pack_queue_state(self.envelope()), binding=context, item_count=40
        )
        self.assertIsNotNone(manifest)
        with self.assertRaises(ReviewInputError):
            read_inline_evidence(
                manifest, expected_binding={**context, "head_sha": "c" * 40}
            )

    def test_high_entropy_250_retains_full_state_and_cannot_force_inline_capacity(self):
        envelope = self.envelope(250)
        packed = pack_queue_state(envelope)
        self.assertIsNone(
            inline_manifest(packed, binding=binding(purpose="queue"), item_count=250)
        )
        self.assertEqual(unpack_queue_state(packed), envelope)

    def test_semantically_invalid_queue_cannot_hide_inside_valid_compression(self):
        envelope = self.envelope()
        envelope["journal"]["queue"]["pending_order"].append("f" * 64)
        with self.assertRaises(ReviewInputError):
            pack_queue_state(envelope)

    def test_explicit_reference_roundtrip_keeps_full_original_100_and_250(self):
        for count in (100, 250):
            with self.subTest(count=count):
                envelope = self.envelope(count)
                queue = AssessmentQueue.from_document(envelope["journal"]["queue"])
                original = copy.deepcopy(envelope)
                packed = pack_queue_state(envelope, queue=queue)
                self.assertEqual(envelope, original)
                self.assertEqual(unpack_queue_state(packed, queue=queue), original)
                self.assertLess(
                    len(canonical_bytes(packed)),
                    len(canonical_bytes(pack_queue_state(envelope))),
                )
                # Old/default readers have no authenticated inventory reference.
                with self.assertRaises(ReviewInputError):
                    unpack_queue_state(packed)

    def test_reference_requires_exact_original_snapshot_inventory_and_order(self):
        envelope = self.envelope(100)
        queue = AssessmentQueue.from_document(envelope["journal"]["queue"])
        packed = pack_queue_state(envelope, queue=queue)
        for change in ("snapshot", "inventory_digest", "inventory_ids"):
            changed = queue.to_document()
            if change == "snapshot":
                changed["snapshot"]["head_sha"] = "f" * 40
            elif change == "inventory_digest":
                changed[change] = "f" * 64
            else:
                changed[change].reverse()
            other = AssessmentQueue.from_document(changed)
            with self.subTest(change=change):
                with self.assertRaises(ReviewInputError):
                    unpack_queue_state(packed, queue=other)
                with self.assertRaises(ReviewInputError):
                    pack_queue_state(envelope, queue=other)

    def test_referenced_closed_shape_full_hash_and_reconstructed_bounds(self):
        envelope = self.envelope(100)
        queue = AssessmentQueue.from_document(envelope["journal"]["queue"])
        original = pack_queue_state(envelope, queue=queue)
        projected = decode_evidence(
            original["state"],
            max_encoded_bytes=MAX_OPERATION_RECEIPT_BYTES,
            max_decoded_bytes=MAX_OPERATION_RECEIPT_BYTES,
        )
        understated = copy.deepcopy(original)
        understated["original_bytes"] = len(canonical_bytes(projected)) + 5
        with self.assertRaisesRegex(ReviewInputError, "exceeds bound"):
            unpack_queue_state(understated, queue=queue)
        for field, value in (
            ("kind", []),
            ("kind", {}),
            ("schema_version", "2.0"),
            ("extra", True),
            ("original_bytes", True),
            ("original_bytes", 1),
            ("original_bytes", MAX_OPERATION_RECEIPT_BYTES + 1),
            ("original_sha256", "f" * 64),
            ("queue_reference", {}),
        ):
            with self.subTest(field=field, value=value):
                packed = copy.deepcopy(original)
                packed[field] = value
                with self.assertRaises(ReviewInputError):
                    unpack_queue_state(packed, queue=queue)
        for indexes in ([True], [-1], [100], [0] * 251, {}, ["0"]):
            with self.subTest(indexes=indexes):
                packed = copy.deepcopy(original)
                projected = decode_evidence(
                    packed["state"],
                    max_encoded_bytes=MAX_OPERATION_RECEIPT_BYTES,
                    max_decoded_bytes=MAX_OPERATION_RECEIPT_BYTES,
                )
                projected["journal"]["queue"]["inventory_ids"] = indexes
                packed["state"] = encode_evidence(
                    projected, max_decoded_bytes=MAX_OPERATION_RECEIPT_BYTES
                )
                with self.assertRaises(ReviewInputError):
                    unpack_queue_state(packed, queue=queue)
        for value in ({"inventory_id": True}, {"inventory_id": 0, "extra": 1}, []):
            packed = copy.deepcopy(original)
            projected = decode_evidence(
                packed["state"],
                max_encoded_bytes=MAX_OPERATION_RECEIPT_BYTES,
                max_decoded_bytes=MAX_OPERATION_RECEIPT_BYTES,
            )
            projected["journal"]["strings"] = [value]
            packed["state"] = encode_evidence(
                projected, max_decoded_bytes=MAX_OPERATION_RECEIPT_BYTES
            )
            with self.assertRaises(ReviewInputError):
                unpack_queue_state(packed, queue=queue)

    def test_recomputed_frame_hash_cannot_substitute_inner_inventory_binding(self):
        envelope = self.envelope(100)
        queue = AssessmentQueue.from_document(envelope["journal"]["queue"])
        changed = copy.deepcopy(envelope)
        changed["journal"]["queue"]["inventory_digest"] = "f" * 64
        other = AssessmentQueue.from_document(changed["journal"]["queue"])
        packed = pack_queue_state(changed, queue=other)
        original = pack_queue_state(envelope, queue=queue)
        packed["queue_reference"] = original["queue_reference"]
        self.assertEqual(
            packed["original_sha256"],
            hashlib.sha256(canonical_bytes(changed)).hexdigest(),
        )
        with self.assertRaisesRegex(ReviewInputError, "inventory changed"):
            unpack_queue_state(packed, queue=queue)


if __name__ == "__main__":
    unittest.main()
