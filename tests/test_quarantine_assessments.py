"""Origin membership and monotonic decisions are independent of fresh output."""

import hashlib
import unittest
from copy import deepcopy

from test_quarantine_lineage import decision, lineage_value

from review_sensei.bounded_evidence import (
    canonical_bytes,
    evidence_manifest,
    partition_evidence,
)
from review_sensei.errors import ReviewInputError


def origins_value():
    binding = lineage_value()["origin_manifest"]["binding"]
    rows, inventories = [], {}
    for kind, number in (("retained-result", 42), ("published-assessment", 43)):
        source = {"synthetic_source": kind}
        source_parts = partition_evidence(source, binding=binding, item_count=1)
        inventory = {
            "schema_version": "quarantine-assessment-inventory-v1",
            "assessments": [{"instance": "1" * 64, "concern": "2" * 64}],
        }
        inventory_parts = partition_evidence(inventory, binding=binding, item_count=1)
        origin = {
            "schema_version": "quarantine-assessment-origin-v1",
            "kind": kind,
            "source_manifest": evidence_manifest(
                source,
                binding=binding,
                item_count=1,
                parts=source_parts,
                storage_ids=(str(number),),
            ),
            "inventory_manifest": evidence_manifest(
                inventory,
                binding=binding,
                item_count=1,
                parts=inventory_parts,
                storage_ids=(str(number + 10),),
            ),
        }
        origin["scope_sha256"] = hashlib.sha256(
            canonical_bytes(
                {"domain": "quarantine-assessment-origin-v1", "origin": dict(origin)}
            )
        ).hexdigest()
        rows.append(origin)
        inventories[origin["scope_sha256"]] = inventory
    return {
        "schema_version": "quarantine-assessment-origins-v1",
        "origins": sorted(rows, key=lambda row: row["scope_sha256"]),
    }, inventories


class QuarantineAssessmentTests(unittest.TestCase):
    def test_equal_instances_remain_pending_in_each_origin_until_individually_settled(
        self,
    ):
        from review_sensei.quarantine_assessments import pending_assessments

        origins, inventories = origins_value()
        scopes = [row["scope_sha256"] for row in origins["origins"]]
        state = {
            "schema_version": "quarantine-assessment-state-v1",
            "scope_sha256": "0" * 64,
            "settlements": [],
        }
        pending = pending_assessments(origins, inventories, state, scope="0" * 64)
        self.assertEqual(pending, tuple((s, "1" * 64) for s in scopes))
        state["settlements"].append(decision(scopes[0]))
        self.assertEqual(
            pending_assessments(origins, inventories, state, scope="0" * 64),
            ((scopes[1], "1" * 64),),
        )

    def test_missing_foreign_duplicate_and_changed_inventory_refuse(self):
        from review_sensei.quarantine_assessments import pending_assessments

        origins, inventories = origins_value()
        state = {
            "schema_version": "quarantine-assessment-state-v1",
            "scope_sha256": "0" * 64,
            "settlements": [],
        }
        bad = deepcopy(inventories)
        first = origins["origins"][0]["scope_sha256"]
        bad[first]["assessments"].clear()
        for values in ({}, {**inventories, "f" * 64: inventories[first]}, bad):
            with self.subTest(values=len(values)), self.assertRaises(ReviewInputError):
                pending_assessments(origins, values, state, scope="0" * 64)
        for change in ({"origin_scope_sha256": "f" * 64}, {"instance": "f" * 64}):
            altered = {**state, "settlements": [{**decision(first), **change}]}
            with self.subTest(change=change), self.assertRaises(ReviewInputError):
                pending_assessments(origins, inventories, altered, scope="0" * 64)

    def test_complete_state_successor_cannot_drop_rewrite_or_reorder_decisions(self):
        from review_sensei.quarantine_assessments import require_assessment_successor

        origins, inventories = origins_value()
        scopes = [row["scope_sha256"] for row in origins["origins"]]
        empty = {
            "schema_version": "quarantine-assessment-state-v1",
            "scope_sha256": "0" * 64,
            "settlements": [],
        }
        one = {**empty, "settlements": [decision(scopes[0])]}
        two = {**empty, "settlements": [decision(s) for s in scopes]}
        require_assessment_successor(origins, inventories, empty, one, scope="0" * 64)
        require_assessment_successor(origins, inventories, one, two, scope="0" * 64)
        for after in (
            empty,
            {
                **one,
                "settlements": [
                    {
                        **decision(scopes[0]),
                        "reason": "A replacement reason cannot rewrite the decision.",
                    }
                ],
            },
            {**two, "settlements": list(reversed(two["settlements"]))},
        ):
            with (
                self.subTest(after=len(after["settlements"])),
                self.assertRaises(ReviewInputError),
            ):
                require_assessment_successor(
                    origins, inventories, one, after, scope="0" * 64
                )

    def test_origin_count_order_source_domains_and_scope_are_closed(self):
        from review_sensei.quarantine_assessments import validate_origins

        origins, _ = origins_value()
        self.assertEqual(validate_origins(origins), origins)
        for rows in ([], origins["origins"] * 2, list(reversed(origins["origins"]))):
            with self.subTest(count=len(rows)), self.assertRaises(ReviewInputError):
                validate_origins({**origins, "origins": rows})
        altered = deepcopy(origins)
        altered["origins"][0]["source_manifest"]["binding"]["producer"] = (
            "github-bot:100"
        )
        with self.assertRaises(ReviewInputError):
            validate_origins(altered)

    def test_all_inventory_items_remain_bounded_without_cross_origin_deduplication(
        self,
    ):
        from review_sensei.quarantine_assessments import validate_inventory

        inventory = {
            "schema_version": "quarantine-assessment-inventory-v1",
            "assessments": [
                {"instance": f"{i:064x}", "concern": "2" * 64} for i in range(100)
            ],
        }
        self.assertEqual(validate_inventory(inventory), inventory)
        for item in (
            {"instance": "f" * 64, "concern": "2" * 64},
            inventory["assessments"][0],
        ):
            with (
                self.subTest(item=item["instance"]),
                self.assertRaises(ReviewInputError),
            ):
                validate_inventory(
                    {**inventory, "assessments": [*inventory["assessments"], item]}
                )
