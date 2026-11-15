"""Synthetic DATA codec tests, not hosted execution or inline-capacity admission."""

import copy
import unittest

from review_sensei_github import finding_lifecycle_prefix as codec
from review_sensei_github.finding_lifecycle import _digest, _pack_journal, _validate
from review_sensei_github.guarded_publication_batch import describe_pair

from tests.test_finding_lifecycle_journal import HEAD, document, effect


def fixture(count=26):
    first = effect(
        {
            "status": "in_progress",
            "output": {},
            "name": "ReviewSensei",
            "head_sha": HEAD,
            "external_id": "synthetic",
        },
        path="/repos/owner/repo/check-runs",
        status="confirmed",
        response=[
            201,
            {"id": 500, "app_id": 42, "producer_id": None, "mutation": None},
        ],
    )
    complete = effect(
        {"status": "completed", "output": {}, "conclusion": "neutral"},
        path="/repos/owner/repo/check-runs/500",
        method="PATCH",
        status="confirmed",
        response=[
            200,
            {"id": 500, "app_id": 42, "producer_id": None, "mutation": None},
        ],
    )
    complete["check_update_precondition"] = {
        "check_run_id": 500,
        "head_sha": HEAD,
        "app_id": 42,
        "app_slug": "reviewsensei",
    }
    review = effect(
        {
            "commit_id": HEAD,
            "event": "COMMENT",
            "body": {"$body_sha256": "a" * 64, "utf8_bytes": 10},
            "comments": [],
        },
        status="confirmed",
        response=[
            200,
            {"id": 100, "producer_id": 999, "app_id": None, "mutation": None},
        ],
    )
    effects = [first, complete, review]
    for i in range(count - 3):
        effects.append(
            effect(
                {"body": {"$body_sha256": _digest(["body", i]), "utf8_bytes": 100 + i}},
                method="PUT",
                path="/repos/owner/repo/pulls/1/reviews/100",
                status="confirmed",
                response=[
                    200,
                    {"id": 100, "producer_id": 999, "app_id": None, "mutation": None},
                ],
            )
        )
    result = document(effects)
    result.update(
        schema_version="finding-lifecycle-journal-v3",
        accepted_semantic_history=None,
        guarded_publication_pair=describe_pair(effects),
    )
    return _validate(result)


class PrefixCodecTests(unittest.TestCase):
    def test_synthetic_23_decisions_and_32_effect_roundtrip(self):
        for count in (26, 32):
            original = fixture(count)
            before = copy.deepcopy(original)
            legacy = _pack_journal(original)
            packed = codec.pack(original, prefix_count=count - 1)
            self.assertEqual(codec.unpack(packed), original)
            self.assertEqual(
                codec.remainder(original, prefix_count=count - 1), packed["remainder"]
            )
            self.assertEqual(original, before)
            self.assertEqual(_pack_journal(original), legacy)
            self.assertEqual(packed["schema_version"], "finding-lifecycle-packed-v5")

    def test_pending_rejected_and_unknown_suffix_remain_lossless(self):
        for status, response in [
            ("pending", None),
            ("rejected", [403, None]),
            (
                "confirmed",
                [
                    200,
                    {
                        "id": 9223372036854775807,
                        "producer_id": 9223372036854775807,
                        "app_id": None,
                        "mutation": None,
                    },
                ],
            ),
        ]:
            value = fixture(11)
            value["effects"][-1].update(status=status, response=response)
            value["unknown_trust_effect"] = True
            encoded = codec.pack(value, prefix_count=10)
            self.assertEqual(codec.unpack(encoded), value)
            prefix = codec.prepare_prefix(value, prefix_count=10)
            self.assertEqual(
                codec.assemble_data(prefix, remainder=encoded["remainder"]), encoded
            )

    def test_inert_assembly_does_not_accept_invalid_suffix_semantics(self):
        value = fixture(11)
        encoded = codec.pack(value, prefix_count=10)
        from review_sensei_github.finding_lifecycle import _unpack_journal

        suffix = _unpack_journal(encoded["remainder"])
        suffix["effects"][0]["request_sha256"] = "f" * 64
        inert = codec.assemble_data(
            codec.prepare_prefix(value, prefix_count=10),
            remainder=_pack_journal(suffix),
        )
        from review_sensei.errors import ReviewInputError

        with self.assertRaises(ReviewInputError):
            codec.unpack(inert)

    def test_invalid_boundaries_and_conflicting_logical_fields_refuse(self):
        from review_sensei.errors import ReviewInputError

        for count in (True, 0, 2, 9, 11, 32):
            with self.subTest(count=count), self.assertRaises(ReviewInputError):
                codec.pack(fixture(11), prefix_count=count)
        for fault in ("digest", "pair", "prefix_pending", "extra", "too_many"):
            value = fixture(11)
            if fault == "digest":
                value["effects"][4]["request_sha256"] = "f" * 64
            elif fault == "pair":
                value["guarded_publication_pair"]["start_receipt_sha256"] = "f" * 64
            elif fault == "prefix_pending":
                value["effects"][4].update(status="pending", response=None)
            elif fault == "extra":
                value["unknown_extra"] = False
            else:
                value["effects"] *= 3
            with self.subTest(fault=fault), self.assertRaises(ReviewInputError):
                codec.pack(value, prefix_count=10)

    def test_malformed_count_overlap_recursive_and_prefix_edits_refuse(self):
        from review_sensei_github.finding_lifecycle import _unpack_journal

        from review_sensei.bounded_evidence import decode_evidence, encode_evidence
        from review_sensei.errors import ReviewInputError

        baseline = codec.pack(fixture(11), prefix_count=10)
        for fault in (
            "count",
            "bool",
            "extra",
            "recursive",
            "overlap",
            "duplicate",
            "pair",
            "digest",
        ):
            changed = copy.deepcopy(baseline)
            if fault == "count":
                changed["prefix_count"] -= 1
            elif fault == "bool":
                changed["prefix_count"] = True
            elif fault == "extra":
                changed["extra"] = None
            elif fault == "recursive":
                changed["remainder"] = copy.deepcopy(baseline)
            elif fault == "overlap":
                raw = _unpack_journal(changed["remainder"])
                raw["guarded_publication_pair"] = {}
                changed["remainder"] = _pack_journal(raw)
            else:
                raw = decode_evidence(
                    changed["prefix"],
                    max_encoded_bytes=1048576,
                    max_decoded_bytes=1048576,
                )
                if fault == "duplicate":
                    raw["effects"][4] = copy.deepcopy(raw["effects"][3])
                elif fault == "pair":
                    raw["guarded_publication_pair"]["complete_index"] = 2
                else:
                    raw["effects"][4]["request_sha256"] = "f" * 64
                changed["prefix"] = encode_evidence(raw, max_decoded_bytes=1048576)
            with self.subTest(fault=fault), self.assertRaises(ReviewInputError):
                codec.unpack(changed)

    def test_bounded_canonical_compression_and_plain_input(self):
        import base64

        from review_sensei.errors import ReviewInputError

        baseline = codec.pack(fixture(11), prefix_count=10)
        for fault in ("trailing", "truncated", "length", "base64", "huge"):
            changed = copy.deepcopy(baseline)
            p = changed["prefix"]
            binary = base64.b64decode(p["data"])
            if fault == "trailing":
                p["data"] = base64.b64encode(binary + b"x").decode()
            elif fault == "truncated":
                p["data"] = base64.b64encode(binary[:-1]).decode()
            elif fault == "length":
                p["decoded_bytes"] += 1
            elif fault == "base64":
                p["data"] += "\n"
            else:
                p["data"] = "A" * 1048577
            with self.subTest(fault=fault), self.assertRaises(ReviewInputError):
                codec.unpack(changed)
        calls = []

        class Meta(type):
            def __eq__(cls, other):
                calls.append("eq")
                return False

        class Poison(metaclass=Meta):
            def __getattribute__(self, name):
                calls.append(name)
                raise AssertionError(name)

        with self.assertRaises(ReviewInputError):
            codec.unpack(Poison())
        self.assertEqual(calls, [])

    def test_replaced_pair_producer_refuses_without_callback(self):
        from unittest.mock import patch

        from review_sensei_github import guarded_publication_batch as pair

        from review_sensei.errors import ReviewInputError

        value = fixture(11)
        calls = []

        def changed(*a, **k):
            calls.append((a, k))

        with patch.object(pair, "describe_pair", changed):
            with self.assertRaises(ReviewInputError):
                codec.pack(value, prefix_count=10)
        self.assertEqual(calls, [])
