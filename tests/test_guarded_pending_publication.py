"""Guarded pending-history profiles retain their original total and prior work."""

import copy
import sys
import tempfile
import unittest
from unittest.mock import patch

from review_sensei_github.guarded_publication_plan import require_durable_floor
from test_quarantine_activation import registered_resource
from test_quarantine_published_tail_journey import (
    BROKER_QUALIFICATIONS,
    PublishedTailJourneyFixture,
)

from review_sensei.api_work import ApiRunAccounting
from review_sensei.bounded_evidence import canonical_bytes


class GuardedPendingPublicationTests(unittest.TestCase):
    def test_one_plus_one_original_total_holds_before_service_mutations(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            fixture = PublishedTailJourneyFixture(directory)
            old, registration = fixture.original_origins(
                failed_count=1, published_count=1
            )
            original = canonical_bytes(old.to_dict())
            old_body = fixture.comment["body"]
            reviews, checks = (
                copy.deepcopy(fixture.reviews),
                copy.deepcopy(fixture.check_runs),
            )
            quotes = []

            def observe(frame, event, arg):
                if (
                    frame.f_code is require_durable_floor.__code__
                    and event == "exception"
                    and type(arg[1]).__name__ == "EvidenceBudgetExhausted"
                ):
                    journal = frame.f_locals["journal"]
                    quotes.append(
                        (
                            frame.f_locals["minimum_calls"],
                            journal.ledger.caller.budget_view.ordinary_remaining(),
                        )
                    )
                return observe

            prior = sys.gettrace()
            try:
                sys.settrace(observe)
                with registered_resource([registration]):
                    self.assertNotEqual(fixture.invoke(), 0)
            finally:
                sys.settrace(prior)
            self.assertEqual(quotes, [(63, 51)])
            self.assertEqual(fixture.check_runs, checks)
            self.assertEqual(fixture.reviews, reviews)
            self.assertEqual(fixture.comment["body"], old_body)
            self.assertEqual(canonical_bytes(old.to_dict()), original)
            self.assertEqual(
                [
                    (method, path)
                    for _attempt, method, path in fixture.trace
                    if method in {"POST", "PUT", "PATCH", "DELETE"}
                    and ("check-runs" in path or path.endswith("/reviews"))
                ],
                [],
            )
            accounting = ApiRunAccounting.from_document(
                fixture.current_record().api_run_accounting
            )
            self.assertEqual(accounting.configuration.limit, 512)
            self.assertEqual(accounting.startup.liability, 130)
            self.assertEqual(accounting.startup_used, (("hosted", 1),))
            self.assertGreater(accounting.confirmed, 0)
            self.assertEqual(accounting.uncertain, 0)
            self.assertEqual(len(fixture.accounted.observations), len(fixture.trace))
