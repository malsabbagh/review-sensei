"""Guarded pending-history profiles retain their original total and prior work."""

import copy
import sys
import tempfile
import unittest
from unittest.mock import patch

from review_sensei_github.api_run_budget import CallerRunBudget
from test_quarantine_activation import registered_resource
from test_quarantine_published_tail_journey import (
    BROKER_QUALIFICATIONS,
    PublishedTailJourneyFixture,
)

from review_sensei.api_work import ApiRunAccounting
from review_sensei.bounded_evidence import canonical_bytes
from tests.test_quarantine_evidence import git


class GuardedPendingPublicationTests(unittest.TestCase):
    def test_one_short_original_total_holds_before_service_mutations(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            fixture = PublishedTailJourneyFixture(directory)
            # Configure the original producer itself, never mutate a live budget.
            fixture.config_bytes = fixture.config_bytes.replace(
                b"api_request_limit: 512", b"api_request_limit: 504"
            )
            (fixture.root / ".reviewsensei.yml").write_bytes(fixture.config_bytes)
            git(fixture.root, "add", ".reviewsensei.yml")
            git(fixture.root, "commit", "-m", "one-short original request allocation")
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
                    frame.f_code is CallerRunBudget.preflight.__code__
                    and event == "call"
                    and frame.f_locals["calls"] == 70
                ):
                    budget = frame.f_locals["self"]
                    quotes.append(
                        (
                            frame.f_locals["calls"],
                            budget.ordinary_remaining(),
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
            self.assertEqual(quotes, [(70, 69)])
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
            self.assertEqual(accounting.configuration.limit, 504)
            self.assertEqual(accounting.startup.liability, 130)
            self.assertEqual(accounting.startup_used, (("hosted", 1),))
            self.assertGreater(accounting.confirmed, 0)
            self.assertEqual(accounting.uncertain, 0)
            self.assertEqual(len(fixture.accounted.observations), len(fixture.trace))
