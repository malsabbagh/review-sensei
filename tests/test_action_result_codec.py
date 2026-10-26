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
