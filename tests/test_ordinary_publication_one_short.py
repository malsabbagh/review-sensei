"""Original physical caller refuses one short of its source-derived full tail."""

import tempfile
import unittest
from unittest.mock import patch

from review_sensei_github import ordinary_publication_plan as plan
from review_sensei_github.api_run_budget import CallerRunBudget

from review_sensei.errors import EvidenceBudgetExhausted
from tests import test_recovery_context_g17 as recovery


class OrdinaryPublicationOneShortTests(unittest.TestCase):
    def test_one_short_of_actual_source_quote_has_zero_native_effects(self):
        original = plan.admit_prefix_branches
        quotes = []

        def observe(journal, *args, **kwargs):
            available = journal.ledger.evidence_budget.ordinary_remaining()
            quoted = original(journal, *args, **kwargs)
            required = quoted["conditional_calls"] + quoted["reserved_tail_calls"]
            quotes.append((available, required))
            return quoted

        make = recovery.OrdinaryEntryHostedAndCliRecoveryG17Tests._make_hosted_fixture
        with tempfile.TemporaryDirectory() as directory:
            fixture = make(directory)
            with patch.object(plan, "admit_prefix_branches", observe):
                self.assertEqual(fixture.invoke(), 0, fixture.error)
        self.assertEqual(len(quotes), 1)
        available, required = quotes[0]
        # Original allocation gives equal current/successor shares. Derive a
        # smaller frozen total, never add funds or substitute the source meter.
        limit = 512 - 2 * (available - required + 1)
        self.assertLess(limit, 512)
        failures = []
        active = False
        real_preflight = CallerRunBudget.preflight

        def observe_preflight(budget, calls):
            try:
                return real_preflight(budget, calls)
            except EvidenceBudgetExhausted:
                if active:
                    failures.append((calls, budget.ordinary_remaining()))
                raise

        def observe_short(journal, *args, **kwargs):
            nonlocal active
            active = True
            try:
                return original(journal, *args, **kwargs)
            finally:
                active = False

        with tempfile.TemporaryDirectory() as directory:
            fixture = make(directory, limit=limit)
            with (
                patch.object(plan, "admit_prefix_branches", observe_short),
                patch.object(CallerRunBudget, "preflight", observe_preflight),
            ):
                self.assertEqual(fixture.invoke(), 1, fixture.error)
            self.assertIn(
                "caller run cannot fund the complete operation", fixture.error
            )
            self.assertEqual(fixture.reviews, [])
            self.assertEqual(fixture.check_runs, [])
        self.assertEqual(failures, [(required, required - 1)])
