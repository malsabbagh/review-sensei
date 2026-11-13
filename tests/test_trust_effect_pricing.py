"""Source-issued opted pricing is arithmetic only, never route permission."""

import copy
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import Request

from review_sensei_github.trust_effect_pricing import (
    price_finalization_verification,
    price_verification_components,
    require_verification_cost,
)
from review_sensei_github.trust_effects import enable

from review_sensei.errors import ReviewInputError
from tests.test_run_authority import NOW
from tests.test_trust_effects import EffectSourceFixture


class TrustEffectPricingTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch("time.time", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.fixture = EffectSourceFixture(self.directory.name)

    def enable(self):
        return enable(
            self.fixture.caller,
            self.fixture.broker,
            self.fixture.session,
            api_url="https://api.github.test",
        )

    def components(self):
        return dict(
            native_requests=(
                ("POST", "/repos/owner/repo/pulls/7/reviews", {"event": "COMMENT"}),
                ("PATCH", "/repos/owner/repo/check-runs/9", {"status": "completed"}),
            ),
            checkpoint_parts=(0, 0),
            report_pages=("report",),
            entry_checkpoint=True,
            command_receipt=True,
            terminal_checkpoint=True,
        )

    def test_default_zero_and_real_profile_counts_every_declared_write(self):
        caller = self.fixture.caller
        before = len(self.fixture.calls)
        old = price_verification_components(caller, **self.components())
        self.assertEqual(require_verification_cost(old, caller), 0)
        self.enable()
        with self.assertRaisesRegex(ReviewInputError, "custody changed"):
            require_verification_cost(old, caller)
        cost = price_verification_components(caller, **self.components())
        self.assertEqual(
            (
                cost.native,
                cost.checkpoints,
                cost.reports,
                cost.entry,
                cost.receipt,
                cost.terminal,
            ),
            (2, 2, 1, 1, 1, 1),
        )
        self.assertEqual(require_verification_cost(cost, caller), 8)
        self.assertEqual(len(self.fixture.calls), before)
        # Pricing does not activate even one otherwise authentic native dispatch.
        with self.assertRaisesRegex(
            ReviewInputError, "complete route admission unavailable"
        ):
            caller.open(
                Request(
                    "https://api.github.test/repos/owner/repo/pulls/7/reviews",
                    data=b"{}",
                    method="POST",
                ),
                timeout=30,
            )
        self.assertEqual(len(self.fixture.calls), before)

    def test_original_physical_budget_exact_fit_and_one_short(self):
        from review_sensei.errors import EvidenceBudgetExhausted

        self.enable()
        caller = self.fixture.caller
        cost = price_verification_components(caller, **self.components())
        extra = require_verification_cost(cost, caller)
        available = caller.budget_view.ordinary_remaining()
        base = available - extra
        self.assertGreaterEqual(base, 0)
        caller.budget_view.preflight(base + extra)
        with self.assertRaises(EvidenceBudgetExhausted):
            caller.budget_view.preflight(base + 1 + extra)
        self.assertEqual(len(self.fixture.calls), 1)

    def test_copied_mutated_foreign_or_changed_root_cost_is_not_reusable(self):
        self.enable()
        caller = self.fixture.caller
        cost = price_verification_components(caller, **self.components())
        for foreign in (copy.copy(cost), object()):
            with self.assertRaises(ReviewInputError):
                require_verification_cost(foreign, caller)
        with self.assertRaises(ReviewInputError):
            require_verification_cost(cost, copy.copy(caller))
        caller._root = "f" * 64
        with self.assertRaises(ReviewInputError):
            require_verification_cost(cost, caller)
        caller._root = None
        object.__setattr__(cost, "native", 0)
        with self.assertRaises(ReviewInputError):
            require_verification_cost(cost, caller)
        self.assertEqual(len(self.fixture.calls), 1)

    def test_opted_partition_and_malformed_components_refuse_without_io(self):
        self.enable()
        for kwargs in (
            {"checkpoint_parts": (1,)},
            {"checkpoint_parts": (True,)},
            {"entry_checkpoint": 1},
            {"native_requests": (("GET", "/x", {}),)},
            {"native_requests": (("POST", "https://other.test/x", {}),)},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ReviewInputError):
                price_verification_components(self.fixture.caller, **kwargs)
        self.assertEqual(len(self.fixture.calls), 1)

    def test_real_default_command_plan_supplies_detached_zero_cost(self):
        import tests.test_command_capacity_binding as commands

        costs = []
        previous = sys.getprofile()

        def observe(frame, event, arg):
            if (
                event != "call"
                or frame.f_code.co_name != "preflight_command_finalization"
                or not frame.f_code.co_filename.replace("\\", "/").endswith(
                    "/finding_lifecycle.py"
                )
            ):
                return
            args = frame.f_locals
            plan, journal = args["plan"], args["self"]
            cost = price_finalization_verification(
                plan,
                journal,
                http=args["http"],
                budget=args["budget"],
                checkpoint_parts=(0,),
            )
            costs.append(require_verification_cost(cost, journal.ledger.caller))
            with self.assertRaises(ReviewInputError):
                price_finalization_verification(
                    copy.copy(plan),
                    journal,
                    http=args["http"],
                    budget=args["budget"],
                    checkpoint_parts=(0,),
                )

        try:
            sys.setprofile(observe)
            commands.CommandCapacityBindingTests().test_default_budget_two_finding_command_and_cold_verify_use_one_check_capability()
        finally:
            sys.setprofile(previous)
        self.assertGreaterEqual(len(costs), 2)
        self.assertEqual(set(costs), {0})

    def test_fake_finalizer_plan_cannot_price_or_grant_any_route(self):
        self.enable()
        with self.assertRaisesRegex(ReviewInputError, "unissued"):
            price_finalization_verification(
                object(),
                object(),
                http=object(),
                budget=object(),
                checkpoint_parts=(0,),
            )
        self.assertEqual(len(self.fixture.calls), 1)
