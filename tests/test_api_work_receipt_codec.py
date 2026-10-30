"""Lossless caller-root encoding; receipts remain charged across version reads."""

import copy
import json
import unittest
from dataclasses import replace

from review_sensei.api_work import ApiRunAccounting, PendingApiRequest
from review_sensei.errors import ReviewInputError
from tests.test_api_work import accounting


class CallerReceiptCodecTests(unittest.TestCase):
    def state(self):
        state = accounting(limit=1200).reserve("review", 600)
        for index in range(400):
            request = PendingApiRequest(f"review:{index}", 1793290000000 + index)
            state = state.dispatch("review", request).confirm(request)
        return state.dispatch("review", PendingApiRequest("uncertain-post", 42))

    def test_compact_new_roots_preserve_every_receipt_and_unknown_charge(self):
        state = self.state()
        document = state.to_document()
        self.assertEqual(document["version"], "caller-requests-v2")
        self.assertEqual(document["completed"][0], ["review:0", 1793290000000])
        restored = ApiRunAccounting.from_document(document)
        self.assertEqual(restored, state)
        self.assertEqual(restored.uncertain, 1)
        self.assertEqual(restored.confirmed, 400)
        self.assertEqual(restored.remaining, state.remaining)
        self.assertEqual(restored.blocks[0].pending[0].deadline_unix_ms, 42)
        legacy = replace(state, version="caller-requests-v1").to_document()

        def encode(value):
            return json.dumps(value, separators=(",", ":")).encode()

        self.assertGreaterEqual(len(encode(legacy)) - len(encode(document)), 12800)

    def test_legacy_roots_round_trip_without_changing_their_wire_or_version(self):
        state = replace(self.state(), version="caller-requests-v1")
        document = state.to_document()
        self.assertIsInstance(document["completed"][0], dict)
        restored = ApiRunAccounting.from_document(document)
        self.assertEqual(restored.to_document(), document)
        request = PendingApiRequest("later-response", 900)
        successor = restored.dispatch("review", request).confirm(request)
        restored.require_successor(successor)
        self.assertEqual(successor.to_document()["version"], "caller-requests-v1")

    def test_mixed_malformed_duplicate_and_unknown_versions_refuse(self):
        document = self.state().to_document()
        mutations = (
            lambda d: d.pop("version"),
            lambda d: d.update(version="caller-requests-v3"),
            lambda d: d["completed"].append(d["completed"][0]),
            lambda d: d["completed"].append(["uncertain-post", 42]),
            lambda d: d["completed"].__setitem__(0, ["review:0", True]),
            lambda d: d["completed"].__setitem__(0, ["review:0", 0]),
            lambda d: d["completed"].__setitem__(0, ["review:0", 1, "extra"]),
            lambda d: d["completed"].__setitem__(
                0, {"request_id": "review:0", "deadline_unix_ms": 1}
            ),
            lambda d: d.update(version="caller-requests-v1"),
        )
        for mutate in mutations:
            broken = copy.deepcopy(document)
            mutate(broken)
            with (
                self.subTest(broken=broken.get("version")),
                self.assertRaises(ReviewInputError),
            ):
                ApiRunAccounting.from_document(broken)

    def test_successor_cannot_reorder_receipts_or_switch_codec_mid_run(self):
        state = self.state()
        for successor in (
            replace(state, completed=tuple(reversed(state.completed))),
            replace(state, version="caller-requests-v1"),
        ):
            with self.assertRaises(ReviewInputError):
                state.require_successor(successor)
