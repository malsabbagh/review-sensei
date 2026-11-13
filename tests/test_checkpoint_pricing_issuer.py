"""Only actual source execution can issue refined checkpoint prices."""

import tempfile
import types
import unittest
from unittest.mock import patch

import test_finding_lifecycle_parts as fixtures

from review_sensei.errors import ReviewInputError
from review_sensei.finding_lifecycle_store import (
    _checkpoint_pricer,
    _issue_checkpoint_pricing,
)


class CheckpointPricingIssuerTests(unittest.TestCase):
    def test_direct_helper_and_factory_injection_refuse_without_io(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, record, _ = fixtures.CurrentRunFindingPartsTests().fixture(
                directory
            )
            count = len(fixture.service.trace)
            with self.assertRaisesRegex(ReviewInputError, "source is unsupported"):
                _issue_checkpoint_pricing(
                    fixture.ledger,
                    record,
                    checkpoint_parts=(1,),
                    capacity={},
                    calls=(7,),
                    validate=lambda: True,
                )
            with self.assertRaisesRegex(ReviewInputError, "source is unsupported"):
                _issue_checkpoint_pricing(
                    fixture.ledger,
                    record,
                    checkpoint_parts=(1,),
                    capacity={},
                    calls=(11,),
                    validate=lambda: True,
                    profile="quarantine-complete-census-v1",
                    page_counts=(3,),
                    _source=fixture.ledger.preflight_finding_lifecycle_checkpoint_pricing.__func__,
                )
            with self.assertRaisesRegex(ReviewInputError, "producer is unsupported"):
                fixture.ledger.preflight_finding_lifecycle_checkpoint_pricing(
                    record,
                    added_parts=1,
                    added_bytes=100,
                    checkpoint_parts=(1,),
                    _checkpoint_factory=lambda *args, **kwargs: None,
                )
            self.assertEqual(len(fixture.service.trace), count)

    def test_custom_decorated_method_cannot_impersonate_registered_producer(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, record, _ = fixtures.CurrentRunFindingPartsTests().fixture(
                directory
            )
            count = len(fixture.service.trace)

            @_checkpoint_pricer
            def preflight_finding_lifecycle_checkpoint_pricing(owner, record, **kwargs):
                self.fail("custom producer body executed")

            custom = types.MethodType(
                preflight_finding_lifecycle_checkpoint_pricing, fixture.ledger
            )
            with patch.object(
                fixture.ledger, "preflight_finding_lifecycle_checkpoint_pricing", custom
            ):
                with self.assertRaisesRegex(
                    ReviewInputError, "producer is unsupported"
                ):
                    custom(
                        record, added_parts=1, added_bytes=100, checkpoint_parts=(1,)
                    )
            self.assertEqual(len(fixture.service.trace), count)

    def test_actual_quarantine_prices_are_closed_source_census_counts(self):
        import test_quarantine_lifecycle_capacity as quarantine

        from review_sensei import finding_lifecycle_store as core

        consume = core._consume_checkpoint_pricing
        prices = []

        def observe(proof, ledger, record, parts):
            issued = core._CHECKPOINT_PRICING.get(proof)
            if issued is not None and issued[9] == "quarantine-complete-census-v1":
                self.assertTrue(all(3 <= pages <= 10 for pages in issued[8]))
                self.assertIsNone(ledger.preflight_finding_lifecycle_inline(record))
                self.assertEqual(
                    issued[5],
                    tuple(
                        17 if n == 0 else pages + 6 + 2 * n
                        for pages, n in zip(issued[8], parts)
                    ),
                )
                prices.append(issued[5])
            return consume(proof, ledger, record, parts)

        with patch.object(core, "_consume_checkpoint_pricing", observe):
            quarantine.QuarantineLifecycleCapacityTests().test_actual_partial_successor_preserves_two_roots_and_seals_capacity()
        self.assertTrue(
            prices, "actual route fell back instead of issuing source prices"
        )
