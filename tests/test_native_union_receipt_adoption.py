"""Original union evidence must fit the existing lifecycle representation atomically."""

import copy
import unittest

from review_sensei_github.native_union import NativeUnionReceipt
from review_sensei_github.publication import ReviewPublisher
from test_native_group_publication import GroupWriterBackend

from review_sensei.errors import ReviewInputError


class NativeUnionReceiptAdoptionTests(unittest.TestCase):
    def source(self, widths):
        backend = GroupWriterBackend()
        backend.host.publish_parts()
        identifiers = [int(row["x"]["nodes"][0]["n"]) for row in backend.roots]
        backend.thread_ids = {
            identifier: ("T" + str(index)).ljust(width, "X")
            for index, (identifier, width) in enumerate(
                zip(identifiers, widths, strict=True)
            )
        }
        state, evidence = backend.publish_final()
        receipt = NativeUnionReceipt.from_staging(backend.plan, state, producer_id=999)
        # Use the original producer and genuine completed opening/body/closing
        # evidence. No forged NativePublicationEvidence, source wrapper or grant.
        publisher = ReviewPublisher(http=backend.http)
        publisher._native_finding_context = {
            "repository_id": receipt.repository_id,
            "pull_request": receipt.pull_request,
            "head_sha": receipt.head_sha,
            "base_sha": receipt.base_sha,
            "producer_id": receipt.producer_id,
            "app_slug": "reviewsensei[bot]",
            "assembled": backend.assembled,
        }
        return backend, publisher, evidence, receipt

    def test_existing_256_byte_boundary_adopts_complete_union(self):
        backend, publisher, evidence, receipt = self.source((256, 256))
        physical = len(backend.calls)
        publisher._adopt_native_union_finding_receipts(evidence, receipt)
        context = publisher._native_finding_context
        self.assertEqual(len(context["existing_bindings"]), 2)
        self.assertEqual(len(context["receipts"][1]), 2)
        self.assertEqual(
            len(
                publisher.native_finding_receipts(
                    repository_id=receipt.repository_id,
                    pull_request=receipt.pull_request,
                )
            ),
            2,
        )
        self.assertEqual(len(backend.calls), physical)

    def test_mixed_valid_and_oversized_union_never_partially_adopts(self):
        for widths in ((256, 257), (257, 256), (256, 2048)):
            with self.subTest(widths=widths):
                backend, publisher, evidence, receipt = self.source(widths)
                context = publisher._native_finding_context
                before_context = copy.deepcopy(context)
                before_receipts = copy.deepcopy(
                    publisher._native_finding_receipt_records
                )
                before_guard = copy.deepcopy(backend.guard.stats)
                before_root = backend.ledger.load(
                    backend.host.identity, now=backend.host.now
                ).record
                physical = len(backend.calls)
                with self.assertRaisesRegex(ReviewInputError, "lifecycle"):
                    publisher._adopt_native_union_finding_receipts(evidence, receipt)
                self.assertEqual(context, before_context)
                self.assertEqual(
                    publisher._native_finding_receipt_records, before_receipts
                )
                self.assertEqual(backend.guard.stats, before_guard)
                self.assertEqual(
                    backend.ledger.load(
                        backend.host.identity, now=backend.host.now
                    ).record,
                    before_root,
                )
                self.assertEqual(len(backend.calls), physical)
