"""Source-produced initial-publication sizing never installs a live prefix."""

import copy
import tempfile
import unittest
from unittest.mock import patch

from review_sensei_github import ordinary_publication_plan
from review_sensei_github.ordinary_publication_plan import price_prefixes

from review_sensei.bounded_evidence import canonical_bytes
from tests import test_recovery_context_g17 as recovery


class OrdinaryPublicationPlanTests(unittest.TestCase):
    def test_private_prefix_envelopes_leave_actual_source_and_meter_unchanged(self):
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
                    record = canonical_bytes(journal._record.to_dict())
                    effects = copy.deepcopy(journal.effects)
                    trace = list(fixture.trace)
                    budget = journal.ledger.evidence_budget
                    remaining = budget.ordinary_remaining()
                    repository = journal.identity.repository
                    requests = (
                        (
                            "POST",
                            f"/repos/{repository}/check-runs",
                            {
                                "head_sha": journal.head_sha,
                                "name": "ReviewSensei",
                                "status": "in_progress",
                            },
                            None,
                        ),
                        (
                            "POST",
                            f"/repos/{repository}/pulls/{journal.identity.pull_request}/reviews",
                            {
                                "commit_id": journal.head_sha,
                                "event": "COMMENT",
                                "body": "Sizing boundary only, never submitted",
                                "comments": [],
                            },
                            None,
                        ),
                    )
                    documents, count = price_prefixes(journal, requests, native_calls=4)
                    self.assertEqual(len(documents), 3)
                    self.assertEqual(count, len(effects) + 3)
                    self.assertEqual(journal.effects, effects)
                    self.assertEqual(canonical_bytes(journal._record.to_dict()), record)
                    self.assertEqual(fixture.trace, trace)
                    self.assertEqual(budget.ordinary_remaining(), remaining)
                    observed.append(documents)
                return original(publisher, observer, journal, **kwargs)

            with patch.object(ordinary_publication_plan, "admit_current", inspect):
                self.assertEqual(fixture.invoke(), 0, fixture.error)
            self.assertTrue(observed)
