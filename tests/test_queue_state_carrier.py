"""Lossless queue storage carrier; compression never supplies host authority."""

import copy
import unittest
from unittest.mock import patch

from review_sensei.assessment_queue import (
    MAX_OPERATION_RECEIPT_BYTES,
    FactoredAssessmentJournal,
    pack_queue_state,
    unpack_queue_state,
)
from review_sensei.bounded_evidence import (
    canonical_bytes,
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


if __name__ == "__main__":
    unittest.main()
