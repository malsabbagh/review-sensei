"""Actual initialized source profile refines only its prepaid inline root cost."""

import copy
import tempfile
import unittest
from unittest.mock import patch

from review_sensei_github.api_run_requests import ApiRunSessionLedger

from review_sensei.errors import ReviewInputError
from review_sensei.finding_lifecycle_store import (
    _consume_checkpoint_pricing,
    _remote_inline_plan,
)
from tests.test_run_authority import NOW
from tests.test_trust_effect_initialization import IDENTITY, InitialEffectFixture


class TrustInlinePricingTests(unittest.TestCase):
    def setUp(self):
        clock = patch("time.time", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.fixture = InitialEffectFixture(self.directory.name)
        self.fixture.initialize()
        self.ledger = ApiRunSessionLedger(
            self.fixture.ledger, self.fixture.caller, IDENTITY
        )
        self.ledger.load(IDENTITY)
        self.record = self.fixture.caller._effect_record

    def proof(self, parts=(0,)):
        return self.ledger.preflight_finding_lifecycle_checkpoint_pricing(
            self.record, added_parts=sum(parts), added_bytes=0, checkpoint_parts=parts
        )

    def test_actual_issued_profile_adds_exactly_one_root_refresh_without_dispatch(self):
        before = len(self.fixture.calls) + len(self.fixture.native)
        proof = self.proof()
        self.assertIsNotNone(proof)
        _, prices = _consume_checkpoint_pricing(proof, self.ledger, self.record, (0,))
        self.assertEqual(prices, (6,))
        plan = _remote_inline_plan(self.ledger, self.record)
        self.assertEqual(plan["conditional_calls"], 5)
        self.assertEqual(plan["post_mutator_calls"], 4)
        self.assertEqual(len(self.fixture.calls) + len(self.fixture.native), before)
        self.assertEqual(self.fixture.caller.accounting.configuration.limit, 512)

    def test_original_remaining_allowance_exact_fit_then_one_short(self):
        from urllib.request import Request

        from review_sensei.errors import EvidenceBudgetExhausted

        _, prices = _consume_checkpoint_pricing(
            self.proof(), self.ledger, self.record, (0,)
        )
        caller = self.fixture.caller
        while caller.usable > prices[0]:
            with caller.open(
                Request("https://api.github.test/repos/owner/repo/pulls/7"), timeout=30
            ) as response:
                response.read(65536)
        caller.budget_view.preflight(prices[0])
        with caller.open(
            Request("https://api.github.test/repos/owner/repo/pulls/7"), timeout=30
        ) as response:
            response.read(65536)
        with self.assertRaises(EvidenceBudgetExhausted):
            caller.budget_view.preflight(prices[0])
        self.assertEqual(caller.usable, prices[0] - 1)
        self.assertEqual(sum(m == "POST" for m, _, _ in self.fixture.native), 1)
        self.assertEqual(caller.accounting.configuration.limit, 512)

    def test_missing_scope_changed_profile_and_unqualified_parts_refuse(self):
        proof = self.proof()
        for altered in (object(), copy.copy(proof)):
            with self.assertRaises(ReviewInputError):
                _consume_checkpoint_pricing(altered, self.ledger, self.record, (0,))
        with (
            patch.object(self.fixture.caller, "attempt", 2),
            self.assertRaises(ReviewInputError),
        ):
            _consume_checkpoint_pricing(proof, self.ledger, self.record, (0,))
        with self.assertRaisesRegex(ReviewInputError, "plain inline only"):
            self.proof((1,))
        with patch.object(self.fixture.caller, "_effect_record", None):
            with self.assertRaisesRegex(
                ReviewInputError, "source scope is unsupported"
            ):
                self.proof()
        self.assertEqual(sum(m == "POST" for m, _, _ in self.fixture.native), 1)
