"""Detached stop-only envelopes do not admit or mutate a publication route."""

import copy
import tempfile
import unittest
from unittest.mock import patch

from review_sensei_github import ordinary_publication_plan
from review_sensei_github.ordinary_drift_plan import stopped_prefix_documents

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from tests import test_recovery_context_g17 as recovery


class OrdinaryDriftPlanTests(unittest.TestCase):
    def test_detached_projection_is_repeatable_and_preserves_original_meter(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = (
                recovery.OrdinaryEntryHostedAndCliRecoveryG17Tests._make_hosted_fixture(
                    directory
                )
            )
            original = ordinary_publication_plan.admit_current
            observed = []

            def inspect(publisher, observer, journal, **kwargs):
                if journal is not None and not observed:
                    original_record = canonical_bytes(journal._record.to_dict())
                    original_effects = copy.deepcopy(journal.effects)
                    original_trace = tuple(fixture.trace)
                    original_remaining = (
                        journal.ledger.evidence_budget.ordinary_remaining()
                    )
                    requests = (
                        (
                            "POST",
                            f"/repos/{journal.identity.repository}/check-runs",
                            {
                                "head_sha": journal.head_sha,
                                "name": "ReviewSensei",
                                "status": "in_progress",
                            },
                            None,
                        ),
                        (
                            "POST",
                            f"/repos/{journal.identity.repository}/pulls/"
                            f"{journal.identity.pull_request}/reviews",
                            {
                                "commit_id": journal.head_sha,
                                "event": "COMMENT",
                                "body": "Pure sizing only; never dispatched",
                                "comments": [],
                            },
                            None,
                        ),
                    )
                    first = stopped_prefix_documents(journal, requests)
                    second = stopped_prefix_documents(journal, requests)
                    self.assertEqual(canonical_bytes(first), canonical_bytes(second))
                    self.assertEqual(len(first[0]), len(requests) + 1)
                    hot, _ = ordinary_publication_plan.price_prefixes(
                        journal, requests, native_calls=4
                    )
                    self.assertEqual(len(hot), 2 * len(requests) + 1)
                    # The stopped projection retains both pending and terminal
                    # shapes from the unchanged complete hot-recovery producer.
                    self.assertEqual(first[0][0], hot[0])
                    self.assertEqual(first[0][1], hot[2])
                    self.assertEqual(first[0][-1], hot[-1])
                    with self.assertRaisesRegex(ReviewInputError, "replay"):
                        stopped_prefix_documents(journal, (requests[0], requests[0]))
                    self.assertEqual(journal.effects, original_effects)
                    self.assertEqual(
                        canonical_bytes(journal._record.to_dict()), original_record
                    )
                    self.assertEqual(tuple(fixture.trace), original_trace)
                    self.assertEqual(
                        journal.ledger.evidence_budget.ordinary_remaining(),
                        original_remaining,
                    )
                    observed.append(first)
                return original(publisher, observer, journal, **kwargs)

            with patch.object(ordinary_publication_plan, "admit_current", inspect):
                self.assertEqual(fixture.invoke(), 0, fixture.error)
            self.assertEqual(len(observed), 1)


if __name__ == "__main__":
    unittest.main()
