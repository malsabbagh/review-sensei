"""The inline optimization leaves room for the source's bounded history pair."""

import hashlib
import tempfile
import unittest
from unittest.mock import patch

from review_sensei import finding_lifecycle_store as store
from review_sensei.bounded_evidence import canonical_bytes, inline_manifest
from review_sensei.errors import ReviewInputError
from tests.test_finding_lifecycle_parts import CurrentRunFindingPartsTests


class SemanticReferenceReserveTests(unittest.TestCase):
    def test_source_pair_reserve_selects_priced_partition_without_publishing_history(
        self,
    ):
        with tempfile.TemporaryDirectory() as root:
            fixture, record, args = CurrentRunFindingPartsTests().fixture(root)
            args = dict(args)
            selected = None
            for count in range(1, 200):
                args["document"] = {
                    "value": "".join(
                        hashlib.sha256(str(i).encode()).hexdigest()
                        for i in range(count)
                    )
                }
                reference, envelope, _, allowance, candidate = store._prepare_lifecycle(
                    fixture.ledger, record, **args
                )
                manifest = candidate.finding_lifecycle["manifest"]
                inline = inline_manifest(
                    envelope, binding=manifest["binding"], item_count=args["item_count"]
                )
                if inline is not None and len(canonical_bytes(inline)) > allowance:
                    selected = (reference, manifest)
                    break
            self.assertIsNotNone(selected)
            reference, manifest = selected
            self.assertEqual(manifest["encoding"], "partitioned-json-v1")
            self.assertNotIn("accepted_semantic_history", reference)
            widened = store.normalize_reference(
                {
                    **reference,
                    "manifest": manifest,
                    "schema_version": store.HISTORY_REFERENCE_VERSION,
                    "accepted_semantic_history": store._source_semantic_history_width(
                        manifest["binding"]
                    ),
                }
            )
            self.assertLessEqual(len(canonical_bytes(widened)), 8192)
            plan = store.preflight_finding_lifecycle(fixture.ledger, record, **args)
            self.assertGreater(plan["added_parts"], 0)
            before = list(fixture.service.trace)
            with patch.object(
                fixture.ledger.evidence_budget,
                "preflight",
                side_effect=ReviewInputError("one short"),
            ):
                with self.assertRaisesRegex(ReviewInputError, "one short"):
                    store.stage_finding_lifecycle(fixture.ledger, record, **args)
            self.assertEqual(fixture.service.trace, before)
            current = store.stage_finding_lifecycle(fixture.ledger, record, **args)
            self.assertEqual(
                current.finding_lifecycle["manifest"]["encoding"], "partitioned-json-v1"
            )
            self.assertNotIn("accepted_semantic_history", current.finding_lifecycle)
            self.assertEqual(
                store.read_finding_lifecycle(fixture.ledger, current), args["document"]
            )

    def test_inline_dictionary_dependency_is_not_a_read_free_checkpoint(self):
        from types import SimpleNamespace

        record = SimpleNamespace(
            assessment_queue=None,
            historical_assessments=None,
            quarantine_lineage=None,
            convergence_history=None,
            disposition_snapshot=None,
            finding_lifecycle={
                "manifest": {
                    "encoding": "inline-json-v1",
                    "document": {
                        "schema_version": "finding-lifecycle-evidence-v2",
                        "lifecycle": {"schema_version": "finding-lifecycle-packed-v4"},
                    },
                }
            },
        )
        self.assertFalse(store._inline_checkpoint_read_free(record))
        record.finding_lifecycle["manifest"]["document"]["lifecycle"][
            "schema_version"
        ] = "finding-lifecycle-packed-v3"
        self.assertTrue(store._inline_checkpoint_read_free(record))
