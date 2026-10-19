"""Packaged structural schema complements independent inventory authority."""

from __future__ import annotations

import unittest
from copy import deepcopy

from review_sensei_github.approval import ReviewApprovalEligibility

from review_sensei.errors import ReviewInputError
from review_sensei.schemas import load_schema, validate_public_document
from tests.test_default_human_consumers import document, rich_inventory


class Schema5PublicContractTests(unittest.TestCase):
    def test_actual_codec_document_matches_packaged_structural_schema(self):
        schema = load_schema("eligibility-v5")
        self.assertEqual(
            schema["$id"],
            "https://reviewsensei.dev/schemas/v1/eligibility-v5.schema.json",
        )
        inventory = rich_inventory()
        value = document(inventory)
        validate_public_document(value, "eligibility-v5")
        restored = ReviewApprovalEligibility.from_dict(
            value, human_inventory_reader=lambda reference: inventory
        )
        self.assertEqual(restored.to_dict(), value)
        # Structural validity never replaces the caller's authenticated reader.
        with self.assertRaisesRegex(ReviewInputError, "trusted reader"):
            ReviewApprovalEligibility.from_dict(value)

    def test_closed_shapes_and_bounds_reject_before_inventory_access(self):
        inventory = rich_inventory()
        value = document(inventory)
        invalid = []
        invalid.append(dict(value, additional_authority=True))
        for field in ("facts", "human_inventory_reference", "human_resolution"):
            changed = deepcopy(value)
            changed[field]["additional_authority"] = True
            invalid.append(changed)
        for key, replacement in (
            ("event_key", "unbound"),
            ("payload_digest", "A" * 64),
            ("purpose", "human-inventory"),
        ):
            changed = deepcopy(value)
            changed["human_inventory_reference"][key] = replacement
            invalid.append(changed)
        for path in (
            ("head_sha",),
            ("result_digest",),
            ("human_inventory_reference", "event_key"),
            ("human_inventory_reference", "payload_digest"),
            ("human_resolution", "inventory_digest"),
        ):
            changed = deepcopy(value)
            target = changed
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] += "\n"
            invalid.append(changed)
        for resolved in (
            ["partial-id"],
            ["1" * 64 + "\n"],
            ["1" * 64] * 2,
            [f"{i:064x}" for i in range(251)],
        ):
            changed = deepcopy(value)
            changed["human_resolution"]["resolved"] = resolved
            invalid.append(changed)
        for changed in invalid:
            with self.subTest(changed=changed):
                with self.assertRaises(ReviewInputError):
                    validate_public_document(changed, "eligibility-v5")
                with self.assertRaises(ReviewInputError):
                    ReviewApprovalEligibility.from_dict(
                        changed,
                        human_inventory_reader=lambda reference: self.fail(
                            "malformed structure reached authority reader"
                        ),
                    )
