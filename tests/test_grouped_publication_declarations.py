"""Pure finite declarations never become v2 attempts or cold replay authority."""

import copy
import unittest
from dataclasses import replace

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.grouped_publication_declarations import (
    GroupedDeclarations,
    require_declaration_transition,
)
from review_sensei.publication_staging import (
    MAX_STAGING_BYTES,
    PublicationReceipt,
    PublicationStaging,
)


class GroupedPublicationDeclarationsTests(unittest.TestCase):
    def initial(self):
        return GroupedDeclarations(
            "a" * 64, "b" * 64, "c" * 64, ("d" * 32, "e" * 32, "f" * 32)
        )

    def receipts(self):
        return (
            PublicationReceipt(str(2**63 - 1), "1" * 64),
            PublicationReceipt("issue:" + str(2**63 - 2), "2" * 64),
            PublicationReceipt(str(2**63 - 3), "3" * 64),
        )

    def test_three_declared_liabilities_have_no_receipts_or_exact_final_body(self):
        state = self.initial()
        self.assertIsNone(state.final_request_sha256)
        self.assertEqual(
            state.cold_status(),
            {
                "mode": "read-only",
                "logical_unconfirmed": ("part", "manifest", "final"),
                "exact_final_intent_present": False,
            },
        )
        self.assertTrue(
            all(
                getattr(state, role + "_receipt") is None
                for role in ("part", "manifest", "final")
            )
        )
        document = state.to_document()
        self.assertNotIn("physical_unknowns", document)
        self.assertNotIn("owner", document)
        self.assertNotIn("dispatch_authority", document)
        with self.assertRaises(ReviewInputError):
            PublicationStaging.from_document(document)
        old = PublicationStaging.prepare(
            transaction_id="a" * 64,
            result_sha256="b" * 64,
            plan_sha256="c" * 64,
            part_count=1,
            manifest_required=True,
        )
        with self.assertRaises(ReviewInputError):
            GroupedDeclarations.from_document(old.to_document())

    def test_cp2_cp3_cp4_retain_tokens_and_require_exact_dynamic_final_digest(self):
        second = self.initial()
        part, manifest, final = self.receipts()
        with self.assertRaises(ReviewInputError):
            second.finish(final=final)
        with self.assertRaises(ReviewInputError):
            second.prepare_final(
                part=None, manifest=manifest, request_sha256=final.content_sha256
            )
        third = second.prepare_final(
            part=part, manifest=manifest, request_sha256=final.content_sha256
        )
        self.assertEqual(third.tokens, second.tokens)
        self.assertEqual(third.cold_status()["logical_unconfirmed"], ("final",))
        self.assertEqual(third.cold_status()["mode"], "read-only")
        with self.assertRaises(ReviewInputError):
            third.finish(final=replace(final, content_sha256="4" * 64))
        fourth = third.finish(final=final)
        self.assertEqual(fourth.tokens, second.tokens)
        self.assertEqual(fourth.cold_status()["logical_unconfirmed"], ())
        self.assertEqual(fourth.cold_status()["mode"], "read-only")
        self.assertLessEqual(
            len(canonical_bytes(fourth.to_document())), MAX_STAGING_BYTES
        )
        self.assertEqual(
            GroupedDeclarations.from_document(fourth.to_document()), fourth
        )

    def test_loss_and_hold_preserve_unused_declarations_without_reissue(self):
        initial = self.initial()
        part, manifest, final = self.receipts()
        for held in (
            initial.hold(),
            initial.hold(part=part),
            initial.hold(part=part, manifest=manifest),
        ):
            with self.subTest(held=held):
                self.assertEqual(held.tokens, initial.tokens)
                self.assertIn("final", held.cold_status()["logical_unconfirmed"])
                self.assertIsNone(held.final_request_sha256)
                with self.assertRaises(ReviewInputError):
                    held.prepare_final(
                        part=part,
                        manifest=manifest,
                        request_sha256=final.content_sha256,
                    )
                require_declaration_transition(
                    held, GroupedDeclarations.from_document(held.to_document())
                )
                with self.assertRaises(ReviewInputError):
                    require_declaration_transition(held, initial)
        with self.assertRaises(ReviewInputError):
            initial.hold(manifest=manifest)

    def test_changed_tokens_receipts_domains_versions_and_unknown_fields_refuse(self):
        initial = self.initial()
        part, manifest, final = self.receipts()
        third = initial.prepare_final(
            part=part, manifest=manifest, request_sha256=final.content_sha256
        )
        for changed in (
            replace(initial, tokens=("0" * 32, "e" * 32, "f" * 32)),
            replace(initial, transaction_id="0" * 64),
        ):
            with self.assertRaises(ReviewInputError):
                require_declaration_transition(initial, changed)
        with self.assertRaises(ReviewInputError):
            third.finish(final=replace(final, native_id=part.native_id))
        with self.assertRaises(ReviewInputError):
            initial.prepare_final(
                part=part,
                manifest=replace(manifest, native_id="123"),
                request_sha256=final.content_sha256,
            )
        with self.assertRaises(ReviewInputError):
            initial.prepare_final(
                part=replace(part, native_id=str(2**63)),
                manifest=manifest,
                request_sha256=final.content_sha256,
            )
        for delta in (
            {"phase": []},
            {"part_count": True},
            {"schema_version": "publication-staging-v2"},
            {"expires": "past"},
        ):
            with self.subTest(delta=delta), self.assertRaises(ReviewInputError):
                GroupedDeclarations.from_document({**initial.to_document(), **delta})
        changed = copy.deepcopy(initial.to_document())
        changed["declarations"][1]["token"] = changed["declarations"][0]["token"]
        with self.assertRaises(ReviewInputError):
            GroupedDeclarations.from_document(changed)
        self.assertEqual(initial.tokens, ("d" * 32, "e" * 32, "f" * 32))
