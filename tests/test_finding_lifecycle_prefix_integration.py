"""Pure representation compatibility, not historical command admission."""

import copy
import unittest
from unittest.mock import patch

from review_sensei_github import finding_lifecycle as journal
from review_sensei_github import finding_lifecycle_dictionary as dictionary
from review_sensei_github import finding_lifecycle_prefix as codec

from review_sensei.bounded_evidence import canonical_bytes, inline_manifest
from review_sensei.errors import ReviewInputError
from tests.test_finding_lifecycle_prefix_codec import fixture


class PrefixIntegrationTests(unittest.TestCase):
    def test_current_encoding_is_lossless_and_never_larger_than_legacy(self):
        for count in (4, 11, 26, 32):
            value = fixture(count)
            before = copy.deepcopy(value)
            legacy = journal._pack_journal(value)
            current = journal._pack_current_journal(value)
            with self.subTest(count=count):
                self.assertEqual(journal._unpack_journal(current), value)
                self.assertLessEqual(
                    len(canonical_bytes(current)), len(canonical_bytes(legacy))
                )
                self.assertEqual(journal._pack_journal(value), legacy)
                self.assertEqual(value, before)
        self.assertEqual(current["schema_version"], codec.VERSION)

    def test_manifest_keeps_exact_old_or_current_committed_bytes(self):
        value = fixture()
        envelopes = [
            {
                "schema_version": "finding-lifecycle-evidence-v2",
                "lifecycle": pack(value),
            }
            for pack in (journal._pack_journal, journal._pack_current_journal)
        ]
        self.assertNotEqual(envelopes[0], envelopes[1])
        for envelope in envelopes:
            manifest = {"sha256": journal._digest(envelope)}
            self.assertEqual(journal._envelope_for_manifest(value, manifest), envelope)
            self.assertEqual(manifest, {"sha256": journal._digest(envelope)})
        self.assertIsNone(journal._envelope_for_manifest(value, {"sha256": "f" * 64}))

    def test_unconfirmed_prefix_keeps_original_representation(self):
        value = fixture(11)
        value["effects"][4].update(status="pending", response=None)
        self.assertEqual(
            journal._pack_current_journal(value), journal._pack_journal(value)
        )

    def test_generic_maximum_does_not_treat_v5_as_prefix_authority(self):
        value = fixture(11)
        legacy = journal._pack_journal(value)
        current = journal._pack_current_journal(value)
        for observed in (False, True):
            self.assertEqual(
                journal._maximum_native_allocation(current, preserve_observed=observed),
                journal._maximum_native_allocation(legacy, preserve_observed=observed),
            )

    def test_suffix_data_upper_dominates_each_fixed_prefix_outcome(self):
        value = fixture(26)
        original_prefix = codec.prepare_prefix(value, prefix_count=25)
        for status, response in (
            ("pending", None),
            ("rejected", [403, None]),
            (
                "confirmed",
                [
                    200,
                    {"id": 100, "producer_id": 999, "app_id": None, "mutation": None},
                ],
            ),
        ):
            candidate = copy.deepcopy(value)
            candidate["effects"][-1].update(status=status, response=response)
            candidate["unknown_trust_effect"] = True
            self.assertEqual(
                codec.prepare_prefix(candidate, prefix_count=25), original_prefix
            )
            upper = codec.assemble_data(
                original_prefix,
                remainder=journal._maximum_native_allocation(
                    codec.remainder(candidate, prefix_count=25), preserve_observed=True
                ),
            )
            actual = journal._pack_current_journal(candidate)
            self.assertLessEqual(
                len(canonical_bytes(actual)), len(canonical_bytes(upper))
            )
            self.assertEqual(journal._unpack_journal(actual), candidate)

    def test_dictionary_base_accepts_exact_v3_and_v5_without_hash_substitution(self):
        value = fixture(11)
        binding = dict(
            repository="owner/repo",
            repository_id=7,
            pull_request=1,
            base_sha="b" * 40,
            head_sha="a" * 40,
            policy_digest="c" * 64,
            configuration_digest="d" * 64,
            generation=1,
            producer="github-bot:999",
            purpose="finding-lifecycle",
            schema_version="1.0",
        )
        for pack in (journal._pack_journal, journal._pack_current_journal):
            base = {
                "schema_version": "finding-lifecycle-evidence-v2",
                "lifecycle": pack(value),
            }
            manifest = inline_manifest(base, binding=binding, item_count=11)
            current = {**binding, "generation": 2}
            target = copy.deepcopy(value)
            target["effects"][-1].update(status="pending", response=None)
            packed = dictionary.pack(
                target, base=base, manifest=manifest, current_binding=current
            )
            self.assertEqual(
                dictionary.unpack(packed, base=base, current_binding=current), target
            )
            changed = copy.deepcopy(base)
            changed["lifecycle"] = journal._pack_journal(target)
            with self.assertRaises(ReviewInputError):
                dictionary.unpack(packed, base=changed, current_binding=current)

    def test_substituted_codec_exports_never_execute_at_runtime_boundaries(self):
        value = fixture(11)
        legacy = journal._pack_journal(value)
        compact = codec.pack(value, prefix_count=10)
        for boundary, action, export in (
            ("selection", lambda: journal._pack_current_journal(value), "_sources"),
            (
                "maximum",
                lambda: journal._maximum_native_allocation(
                    legacy, preserve_observed=True, _pair_source=object()
                ),
                "_sources",
            ),
            ("decode", lambda: journal._unpack_journal(compact), "unpack"),
        ):
            calls = []

            def replaced(*args, **kwargs):
                calls.append((args, kwargs))
                raise ReviewInputError("substituted export ran")

            with self.subTest(boundary=boundary), patch.object(codec, export, replaced):
                with self.assertRaises(ReviewInputError):
                    action()
                self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
