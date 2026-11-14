"""Prose-v4 is auxiliary projection, not an alternate native lifecycle authority.

A genuine ordinary subprocess enrolls the lifecycle before the cold publisher
attempts this distinct host route. No lifecycle state is injected by the tests.
"""

import tempfile
import unittest
from pathlib import Path

from review_sensei_github import ReviewPublisher
from review_sensei_github.finding_lifecycle import restore_finding_lifecycle

from review_sensei.errors import ReviewInputError
from review_sensei.session import LocalSessionLedger
from tests import test_durable_finding_lifecycle_restart as restart
from tests import test_rich250_public_writer as prose
from tests.test_durable_finding_lifecycle_restart import IDENTITY
from tests.test_evidence_capacity import publication_diff
from tests.test_human_assessment import APP


class FindingLifecyclePartitionedTests(unittest.TestCase):
    def test_auxiliary_prose_projection_does_not_enroll_or_count_finding_round(self):
        fixtures = prose.Rich250PublicWriterTests()
        with tempfile.TemporaryDirectory() as directory:
            values = fixtures.fixture(directory)
            host = values[-1]
            before = host.ledger.load(host.identity).record
            receipt = fixtures.publish(values)
            after = host.ledger.load(host.identity).record
            self.assertIsNone(after.finding_lifecycle)
            self.assertEqual(
                after.completed_initial_reviews, before.completed_initial_reviews
            )
            self.assertEqual(
                after.completed_verification_rounds,
                before.completed_verification_rounds,
            )
            self.assertEqual(len(receipt.eligibility.human_review.pending), 250)
            self.assertNotIn("APPROVE", values[0].events())
            self.assertEqual(receipt.diagnostic, "approval_withheld")

    def test_cold_enrolled_native_lifecycle_cannot_be_replaced_by_prose_projection(
        self,
    ):
        for mode in ("window", "drift"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                restart.DurableFindingRestartTests().run_phase(root, mode, "initial")
                ledger = LocalSessionLedger(
                    root / "ledger", enable_finding_lifecycle_writes=True
                )
                original = ledger.load(IDENTITY).record
                values = prose.Rich250PublicWriterTests().fixture(root / "prose-host")
                state, comments, result, policy, configuration, host = values
                publisher = ReviewPublisher(http=state.http)
                selected, document = restore_finding_lifecycle(
                    publisher, ledger, IDENTITY
                )
                self.assertEqual(selected.record_sha256, original.record_sha256)
                self.assertIsNotNone(document)
                calls = list(state.calls)
                prose_root = host.ledger.load(host.identity).record.record_sha256
                with self.assertRaisesRegex(
                    ReviewInputError, "prose cannot advance an enrolled"
                ):
                    publisher.publish_partitioned(
                        token="synthetic",
                        host=host,
                        result=result,
                        diff=publication_diff(comments),
                        configuration_context=configuration,
                        convergence_policy=policy,
                        app_slug=APP,
                        check_token="synthetic-checks",
                    )
                self.assertEqual(state.calls, calls)
                self.assertEqual(
                    host.ledger.load(host.identity).record.record_sha256, prose_root
                )
                self.assertEqual(
                    ledger.load(IDENTITY).record.record_sha256, original.record_sha256
                )
                self.assertEqual(
                    len(
                        publisher._active_window_states
                        if mode == "window"
                        else publisher._drift_states
                    ),
                    1,
                )
