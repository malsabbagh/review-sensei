"""An actual small producer observation cannot release or authorize capacity."""

import tempfile
import unittest

from review_sensei_github.quarantine_materialization_observation import (
    observe_materialization,
)

from tests.test_review_run import EntryFixture


class MaterializationObservationTests(unittest.TestCase):
    def test_actual_source_report_is_read_only_and_keeps_generic_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = EntryFixture(directory, limit=512)
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            ledger = fixture.selected.ledger
            calls = len(fixture.accounted.observations)
            accounting = ledger.caller.accounting.to_document()
            report = observe_materialization(ledger)
            self.assertEqual(len(fixture.accounted.observations), calls)
            self.assertEqual(ledger.caller.accounting.to_document(), accounting)
            self.assertEqual(
                report.unresolved_requests, ledger.caller.accounting.uncertain
            )
            self.assertEqual(report.caller_identity, id(ledger.caller))
            self.assertEqual(report.record_sha256, ledger.caller._root)
            self.assertGreater(report.payload_bytes, 0)
            self.assertGreater(report.event_comment_id, 0)
            self.assertEqual(
                report.actual_writer_variant, "raw-checkpoint-and-inventory"
            )
            self.assertEqual(report.generic_tail_slots, 31)
            self.assertEqual(report.generic_tail_body_bytes, 1_624_792)
            self.assertEqual(report.releasable_allocations, 0)
            self.assertFalse(hasattr(report, "activate"))
