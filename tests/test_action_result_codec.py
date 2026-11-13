"""Lossless action result carriers retain existing evidence size ceilings."""

from __future__ import annotations

import base64
import copy
import hashlib
import tempfile
import unittest
import zlib

from review_sensei_github.api_run_history import (
    decode_action_result_carrier,
    read_action_result,
)

from review_sensei.bounded_evidence import (
    MAX_MANIFEST_BYTES,
    MAX_PARTITION_DECODED_BYTES,
    canonical_bytes,
    encode_evidence,
)
from review_sensei.errors import ReviewInputError
from tests import test_api_work_actions as native_actions
from tests.test_api_work_persistence import IDENTITY


def envelope(value):
    return {
        "schema_version": "caller-action-result-v1",
        "intent_sha256": "a" * 64,
        "outcome": native_actions.OUTCOME,
        "result": value,
    }


def carrier(value):
    return {
        "schema_version": "caller-action-result-v1",
        "payload": encode_evidence(
            value, max_decoded_bytes=MAX_PARTITION_DECODED_BYTES
        ),
    }


class ActionResultCodecTests(unittest.TestCase):
    def test_v2_reference_commitment_is_closed_lossless_and_fixed_width(self):
        from review_sensei.action_result_codec import encode_action_result_carrier

        history = {
            "schema_version": "accepted-semantic-history-v1",
            "qualification": "unavailable",
            "reason": "complete_inventory_required",
        }
        original = envelope(
            {"summary": "Complete.", "accepted_semantic_history": history}
        )
        prepared = encode_action_result_carrier(original, reference_history=True)
        self.assertEqual(prepared["schema_version"], "caller-action-result-v2")
        self.assertEqual(
            decode_action_result_carrier(prepared, history=history), original
        )
        alternate = {**history, "reason": "other_reason"}
        other = encode_action_result_carrier(
            envelope({"summary": "Complete.", "accepted_semantic_history": alternate}),
            reference_history=True,
        )
        self.assertEqual(prepared["payload"], other["payload"])
        self.assertEqual(len(canonical_bytes(prepared)), len(canonical_bytes(other)))
        for supplied in (None, {}, alternate):
            with self.assertRaisesRegex(ReviewInputError, "commitment"):
                decode_action_result_carrier(prepared, history=supplied)
        for key, value in (
            ("schema_version", "caller-action-result-v3"),
            ("accepted_semantic_history_sha256", "f" * 64),
            ("extra", None),
        ):
            changed = {**prepared, key: value}
            with self.assertRaises(ReviewInputError):
                decode_action_result_carrier(changed, history=history)
        mixed = {**prepared, "payload": carrier(original)["payload"]}
        with self.assertRaises(ReviewInputError):
            decode_action_result_carrier(mixed, history=history)
        # Legacy full-history results and no-history ordinary results stay v1.
        legacy = encode_action_result_carrier(original)
        self.assertEqual(legacy["schema_version"], "caller-action-result-v1")
        self.assertEqual(decode_action_result_carrier(legacy), original)
        self.assertEqual(
            encode_action_result_carrier(
                envelope({"summary": "Complete."}), reference_history=True
            )["schema_version"],
            "caller-action-result-v1",
        )

    def test_v2_preserves_full_logical_decoded_limit(self):
        from review_sensei.action_result_codec import encode_action_result_carrier

        history = {
            "schema_version": "accepted-semantic-history-v1",
            "qualification": "unavailable",
            "reason": "complete_inventory_required",
        }
        document = envelope({"padding": "", "accepted_semantic_history": history})
        document["result"]["padding"] = "x" * (
            MAX_PARTITION_DECODED_BYTES - len(canonical_bytes(document))
        )
        prepared = encode_action_result_carrier(document, reference_history=True)
        self.assertEqual(
            decode_action_result_carrier(prepared, history=history), document
        )
        document["result"]["padding"] += "x"
        with self.assertRaisesRegex(ReviewInputError, "decoded byte bound"):
            encode_action_result_carrier(document, reference_history=True)
        business = copy.deepcopy(document)
        del business["result"]["accepted_semantic_history"]
        # A foreign producer cannot hide expansion in the separately retained
        # history even though the compressed business part individually fits.
        oversized = {**prepared, "payload": carrier(business)["payload"]}
        with self.assertRaisesRegex(ReviewInputError, "decoded byte bound"):
            decode_action_result_carrier(oversized, history=history)

    def test_existing_decoded_limit_is_exact_and_lossless(self):
        document = envelope({"padding": ""})
        overhead = len(canonical_bytes(document))
        document["result"]["padding"] = "x" * (MAX_PARTITION_DECODED_BYTES - overhead)
        self.assertEqual(len(canonical_bytes(document)), MAX_PARTITION_DECODED_BYTES)
        prepared = carrier(document)
        self.assertLess(len(canonical_bytes(prepared)), MAX_MANIFEST_BYTES)
        self.assertEqual(decode_action_result_carrier(prepared), document)
        document["result"]["padding"] += "x"
        with self.assertRaisesRegex(ReviewInputError, "decoded byte bound"):
            carrier(document)

    def test_encoded_declared_expansion_stream_and_closed_shape_refusals(self):
        original = carrier(envelope({"summary": "Complete."}))
        for fault in (
            "encoded",
            "declared",
            "truncated",
            "trailing",
            "digest",
            "bomb",
            "outer",
            "inner",
            "plain",
        ):
            with self.subTest(fault=fault):
                value = copy.deepcopy(original)
                if fault == "encoded":
                    value["payload"]["data"] = "A" * (MAX_MANIFEST_BYTES + 1)
                elif fault == "declared":
                    value["payload"]["decoded_bytes"] = MAX_PARTITION_DECODED_BYTES + 1
                elif fault == "truncated":
                    value["payload"]["data"] = value["payload"]["data"][:-4]
                elif fault == "trailing":
                    data = base64.b64decode(value["payload"]["data"])
                    value["payload"]["data"] = base64.b64encode(
                        data + b"trailing"
                    ).decode()
                elif fault == "digest":
                    value["payload"]["sha256"] = "f" * 64
                elif fault == "bomb":
                    # The cheap declared bound and the streaming expansion
                    # limit reject this before materializing the whole output.
                    value["payload"]["data"] = base64.b64encode(
                        zlib.compress(b"x" * (MAX_PARTITION_DECODED_BYTES + 1))
                    ).decode()
                elif fault == "outer":
                    value["extra"] = None
                elif fault == "inner":
                    document = envelope({})
                    document["extra"] = None
                    value = carrier(document)
                else:
                    value = envelope({"old": "plain result shape"})
                with self.assertRaises(ReviewInputError):
                    decode_action_result_carrier(value)

    def test_actual_manifest_boundary_refuses_before_terminal_write(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = native_actions.NativeActionTests()
            f, ledger = helper.prepare(directory)
            intent = helper.admit(f, ledger)
            boundary = f.accounted.begin_action_effect(ledger, IDENTITY, intent)
            noise = "".join(
                hashlib.sha256(str(i).encode()).hexdigest() for i in range(600)
            )
            before = len(f.trace)

            def prepared(size):
                return f.accounted.settle_action_effect(
                    ledger,
                    IDENTITY,
                    boundary,
                    outcome=native_actions.OUTCOME,
                    result_document={"payload": noise[:size]},
                )

            lower, upper = 0, len(noise)
            while lower < upper:
                middle = (lower + upper + 1) // 2
                try:
                    prepared(middle)
                except ReviewInputError as error:
                    self.assertIn("inline retention bound", str(error))
                    upper = middle - 1
                else:
                    lower = middle
            proof = prepared(lower)
            self.assertLessEqual(
                len(canonical_bytes(proof.manifest)), MAX_MANIFEST_BYTES
            )
            with self.assertRaisesRegex(ReviewInputError, "inline retention bound"):
                prepared(lower + 1)
            with self.assertRaisesRegex(ReviewInputError, "decoded byte bound"):
                f.accounted.settle_action_effect(
                    ledger,
                    IDENTITY,
                    boundary,
                    outcome=native_actions.OUTCOME,
                    result_document={"padding": "x" * MAX_PARTITION_DECODED_BYTES},
                )
            self.assertEqual(len(f.trace), before)
            self.assertEqual(f.accounted.accounting.action["phase"], "pending")
            root = f.accounted.close_action(ledger, IDENTITY, proof)
            self.assertEqual(
                read_action_result(ledger, IDENTITY, root)["result"],
                {"payload": noise[:lower]},
            )


if __name__ == "__main__":
    unittest.main()
