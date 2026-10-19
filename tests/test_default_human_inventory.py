"""Default application acceptance; synthetic external APIs, no trusted host seam."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from review_sensei.context import finding_lifecycle_for_comment
from review_sensei.human_assessment import (
    PendingHumanReview,
    finding_instance_fingerprint,
)
from review_sensei.session import LocalSessionLedger
from tests.default_human_inventory_contract import (
    APP,
    BASE,
    HEAD,
    IDENTITY,
    DefaultHumanState,
    application,
    human_checkpoint,
    publish_human,
)


class DefaultHumanInventoryTests(unittest.TestCase):
    def assert_complete_pending_publication(
        self, *, count, detail, complete_metadata=True
    ):
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(
                Path(directory) / "ledger", enable_partition_writes=True
            )
            result, key = human_checkpoint(ledger, count=count, detail=detail)
            expected = PendingHumanReview.from_result(result, BASE)
            state = DefaultHumanState(result.comments)
            # Publication is a distinct invocation after completed analysis;
            # reload its actual checkpoint and parts, not an in-memory record.
            ledger = LocalSessionLedger(Path(directory) / "ledger")
            outcome = publish_human(state, ledger, result, key)
            self.assertEqual(outcome.status, "published")
            # A new ordinary application receives the actual persisted ledger;
            # it must derive reader authority from that state, not a test host.
            reader = application(
                state, LocalSessionLedger(Path(directory) / "ledger")
            ).reviewer.finalizer
            eligibility = reader.load_eligibility(
                token="synthetic-read",
                repository=IDENTITY.repository,
                pull_request=IDENTITY.pull_request,
                head_sha=HEAD,
                app_slug=APP,
                require_valid=True,
            )
            self.assertIsNotNone(eligibility)
            self.assertEqual(eligibility.human_review.findings, expected.findings)
            self.assertEqual(eligibility.human_review.base_sha, BASE)
            if complete_metadata:
                self.assertEqual(
                    eligibility.human_review.inventory_document(),
                    expected.inventory_document(),
                )
            self.assertEqual(len(eligibility.human_review.pending), count)
            self.assertEqual(eligibility.human_review.resolved, ())
            self.assertTrue(eligibility.facts.has_human_adjudication_findings)
            self.assertEqual(eligibility.facts.qualification, "not-required")
            index = reader.finding_index_from_reviews(
                state.reviews,
                repository_id=IDENTITY.repository_id,
                pull_request=IDENTITY.pull_request,
                head_sha=HEAD,
                app_slug=APP,
                eligibility=eligibility,
            )
            self.assertIsNotNone(index)
            self.assertEqual(
                {item.instance: item.lifecycle for item in index.entries},
                {
                    finding_instance_fingerprint(
                        comment
                    ): finding_lifecycle_for_comment(comment).fingerprint
                    for comment in result.comments
                },
            )
            visible = "\n".join(
                item["body"] for item in [*state.reviews, *state.inline]
            )
            for comment in result.comments:
                self.assertIn(comment.body, visible)
            self.assertNotIn("omitted for size", visible)
            self.assertEqual(state.check_runs[-1]["conclusion"], "action_required")
            self.assertNotIn("APPROVE", state.events())
            restored = (
                LocalSessionLedger(Path(directory) / "ledger").load(IDENTITY).record
            )
            self.assertEqual(restored.transaction.phase, "publication_succeeded")

    def test_control_inline_human_findings_use_the_same_default_publication_and_reader(
        self,
    ):
        self.assert_complete_pending_publication(
            count=12, detail=1, complete_metadata=False
        )

    def test_complete_77_rich_human_findings_publish_and_fresh_default_reader_keeps_all_pending(
        self,
    ):
        self.assert_complete_pending_publication(count=77, detail=3)


if __name__ == "__main__":
    unittest.main()
