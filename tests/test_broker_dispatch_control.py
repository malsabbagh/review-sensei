"""Prepaid original broker liability; production TS proves internal dispatches."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from review_sensei_github.broker_client import BrokerClient
from review_sensei_github.broker_control import (
    COMMAND_EXCHANGE_DISPATCH_LIABILITY,
    BrokerDispatchAccounting,
)
from review_sensei_github.errors import GitHubBrokerClientError

from review_sensei.bounded_evidence import MAX_PART_READS, EvidenceReadBudget
from review_sensei.errors import ReviewInputError
from tests.test_github_broker_client import FakeResponse


class BrokerDispatchControlTests(unittest.TestCase):
    def test_oidc_response_after_original_deadline_is_not_accepted(self):
        clock = [0.0]
        budget = EvidenceReadBudget(clock=lambda: clock[0])

        def open_request(request, timeout):
            clock[0] = 60.0
            return FakeResponse(b'{"value":"synthetic"}')

        with patch.dict(
            os.environ,
            {
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic",
                "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.github.test/token?fixture=1",
            },
            clear=True,
        ):
            with self.assertRaises(ReviewInputError):
                self.client(budget, open_request).request_oidc_token()
        self.assertEqual(budget.calls, 1)

    def test_expiry_crossed_during_http_cannot_accept_receipt_or_renew_deadline(self):
        clock = [0.0]
        budget = EvidenceReadBudget(
            clock=lambda: clock[0], wall_clock=lambda: 1_792_368_000
        )
        deadline = budget.wall_deadline_ms

        def open_request(request, timeout):
            control = json.loads(request.data)["control_budget"]
            clock[0] = 60.0
            return FakeResponse(
                json.dumps(
                    {
                        "token": "synthetic",
                        "control_receipt": {**control, "dispatched": 13},
                    }
                ).encode()
            )

        with self.assertRaises(ReviewInputError):
            self.client(budget, open_request).exchange(
                "synthetic", capability="review_session"
            )
        self.assertEqual(budget.calls, 14)
        self.assertEqual(budget.wall_deadline_ms, deadline)

    def client(self, budget, opener):
        return BrokerClient(
            opener=opener,
            control=BrokerDispatchAccounting(
                budget=budget,
                operation_id="a" * 64,
                exchange_liability=13,
                verification_liability=1,
            ),
        )

    def test_admits_internal_liability_before_transport_and_never_refunds_unused_slots(
        self,
    ):
        budget = EvidenceReadBudget()
        dispatched = []
        deadline = budget.wall_deadline_ms

        def open_request(request, timeout):
            dispatched.append(request)
            self.assertEqual(budget.calls, 14)
            payload = json.loads(request.data)
            control = payload["control_budget"]
            self.assertEqual(control["deadline_unix_ms"], deadline)
            self.assertEqual(control["max_dispatches"], 13)
            self.assertEqual(control["operation_id"], "a" * 64)
            self.assertLessEqual(timeout, 60)
            return FakeResponse(
                json.dumps(
                    {
                        "token": "synthetic",
                        "control_receipt": {**control, "dispatched": 12},
                    }
                ).encode()
            )

        self.assertEqual(
            self.client(budget, open_request).exchange(
                "synthetic", capability="review_session"
            ),
            "synthetic",
        )
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(budget.calls, 14)
        self.assertEqual(budget.wall_deadline_ms, deadline)

    def test_unknown_response_or_missing_changed_and_overspent_receipts_burn_liability(
        self,
    ):
        for fault in ("missing", "changed", "extra", "overspent", "type", "unknown"):
            with self.subTest(fault=fault):
                budget = EvidenceReadBudget()

                def open_request(request, timeout):
                    if fault == "unknown":
                        raise TimeoutError("synthetic uncertain response")
                    control = json.loads(request.data)["control_budget"]
                    receipt = {**control, "dispatched": 13}
                    if fault == "changed":
                        receipt["request_id"] = "b" * 64
                    if fault == "extra":
                        receipt["authority"] = True
                    if fault == "overspent":
                        receipt["dispatched"] = 14
                    if fault == "type":
                        receipt["dispatched"] = True
                    return FakeResponse(
                        json.dumps(
                            {
                                "token": "synthetic",
                                **(
                                    {}
                                    if fault == "missing"
                                    else {"control_receipt": receipt}
                                ),
                            }
                        ).encode()
                    )

                with self.assertRaises(GitHubBrokerClientError):
                    self.client(budget, open_request).exchange(
                        "synthetic", capability="review_session"
                    )
                self.assertEqual(budget.calls, 14)

    def test_unfunded_internal_liability_refuses_before_any_transport(self):
        budget = EvidenceReadBudget()
        for _ in range(48):
            budget.consume()
        with self.assertRaises(ReviewInputError):
            self.client(
                budget,
                lambda *_args, **_kwargs: self.fail("unfunded request dispatched"),
            ).exchange("synthetic", capability="review_session")
        self.assertEqual(budget.calls, 48)

    def test_ordinary_hold_narrows_the_phase_ceiling_without_spending_calls(self):
        budget = EvidenceReadBudget()
        for calls in (0, True, 61):
            with self.subTest(calls=calls):
                with self.assertRaises(ReviewInputError):
                    budget.hold_ordinary(calls)
        budget.hold_ordinary(48)
        with self.assertRaises(ReviewInputError):
            budget.hold_ordinary(10)
        self.assertEqual(budget.ordinary_remaining(), 12)
        for _ in range(12):
            budget.consume()
        with self.assertRaises(ReviewInputError):
            budget.consume()
        with self.assertRaises(ReviewInputError):
            budget.preflight(1)
        self.assertEqual(budget.calls, 12)
        with self.assertRaises(ReviewInputError):
            budget.release_ordinary(47)
        # A mismatched release must not discard the outstanding hold.
        with self.assertRaises(ReviewInputError):
            budget.consume()
        self.assertEqual(budget.calls, 12)
        budget.release_ordinary(48)
        self.assertEqual(budget.ordinary_remaining(), 48)
        budget.preflight(13)
        self.assertEqual(budget.calls, 12)
        budget.consume()
        self.assertEqual(budget.calls, 13)

    def test_held_ordinary_allowance_refuses_one_unfunded_exchange_atomically(self):
        budget = EvidenceReadBudget()
        for _ in range(30):
            budget.consume()
        budget.hold_ordinary(18)
        control = BrokerDispatchAccounting(budget, "a" * 64, 13, 1)
        with self.assertRaises(ReviewInputError):
            control.admit(verification=False)
        self.assertEqual(budget.calls, 30)
        # The hold narrows the ceiling; it does not block a funded verification.
        self.assertEqual(control.admit(verification=True)["max_dispatches"], 1)
        self.assertEqual(budget.calls, 31)
        budget.release_ordinary(18)
        self.assertEqual(control.admit(verification=False)["max_dispatches"], 13)
        self.assertEqual(budget.calls, 44)

    def test_held_allowance_admits_exactly_the_liability_and_request(self):
        # The command broker's check_publish exchange liability is 10 internal
        # dispatches; the same client charges its request separately, so the
        # whole exchange costs exactly the 11 calls still under the ceiling.
        liability = COMMAND_EXCHANGE_DISPATCH_LIABILITY
        hold = 18
        request_cost = liability + 1
        budget = EvidenceReadBudget()
        pre_consumed = MAX_PART_READS - 4 - hold - request_cost
        for _ in range(pre_consumed):
            budget.consume()
        budget.hold_ordinary(hold)
        control = BrokerDispatchAccounting(budget, "a" * 64, liability, 1)
        self.assertEqual(budget.ordinary_remaining(), request_cost)
        self.assertEqual(control.admit(verification=False)["max_dispatches"], liability)
        self.assertEqual(budget.calls, pre_consumed + liability)
        with self.assertRaises(ReviewInputError):
            control.admit(verification=True)
        self.assertEqual(budget.calls, pre_consumed + liability)

    def test_admission_boundary_is_exact_at_the_narrowed_ceiling(self):
        # ``ordinary_remaining`` already subtracts the consumed calls, so an
        # exchange is one whole-request test against that room. Pin both sides:
        # a request that exactly fills the room is admitted, and one call short
        # is refused atomically.
        liability = COMMAND_EXCHANGE_DISPATCH_LIABILITY
        request_cost = liability + 1
        for shortfall in (0, 1):
            with self.subTest(shortfall=shortfall):
                budget = EvidenceReadBudget()
                target = request_cost - shortfall
                for _ in range(budget.ordinary_remaining() - target):
                    budget.consume()
                self.assertEqual(budget.ordinary_remaining(), target)
                pre_consumed = budget.calls
                control = BrokerDispatchAccounting(budget, "a" * 64, liability, 1)
                if shortfall:
                    with self.assertRaises(ReviewInputError):
                        control.admit(verification=False)
                    self.assertEqual(budget.calls, pre_consumed)
                else:
                    self.assertEqual(
                        control.admit(verification=False)["max_dispatches"],
                        liability,
                    )
                    self.assertEqual(budget.calls, pre_consumed + liability)

    def test_refused_hold_leaves_no_reservation_to_release(self):
        # Acquisition is all-or-nothing and validates with the same strict
        # integer test as the release, so a refused call never leaves a hold a
        # caller could not release.
        budget = EvidenceReadBudget()
        ceiling = budget.ordinary_remaining()
        for calls in (0, True, 61):
            with self.subTest(calls=calls):
                with self.assertRaises(ReviewInputError):
                    budget.hold_ordinary(calls)
                self.assertEqual(budget.ordinary_remaining(), ceiling)
        budget.hold_ordinary(18)
        with self.assertRaises(ReviewInputError):
            budget.hold_ordinary(10)
        # A refused reentrant hold must preserve the outstanding reservation.
        self.assertEqual(budget.ordinary_remaining(), ceiling - 18)
        budget.release_ordinary(18)
        self.assertEqual(budget.ordinary_remaining(), ceiling)

    def test_restored_budget_starts_unheld_and_recovers_the_full_ceiling(self):
        # A hold is phase-local and never part of the snapshot: a resumed
        # operation re-establishes any reservation it needs, so restoring a
        # snapshot taken mid-hold must report the un-narrowed ceiling rather
        # than inherit a reservation nobody will release.
        budget = EvidenceReadBudget()
        for _ in range(10):
            budget.consume()
        budget.hold_ordinary(18)
        self.assertEqual(budget.ordinary_remaining(), MAX_PART_READS - 4 - 18 - 10)
        restored = EvidenceReadBudget(snapshot=budget.snapshot())
        self.assertEqual(restored.calls, 10)
        self.assertEqual(restored.ordinary_remaining(), MAX_PART_READS - 4 - 10)
        for _ in range(MAX_PART_READS - 4 - 10):
            restored.consume()
        with self.assertRaises(ReviewInputError):
            restored.consume()

    def test_receipt_scope_requires_exact_json_types(self):
        budget = EvidenceReadBudget()
        accounting = BrokerDispatchAccounting(budget, "a" * 64, 1, 1)
        request = accounting.admit(verification=False)
        for field, value in (
            ("max_dispatches", True),
            ("deadline_unix_ms", float(request["deadline_unix_ms"])),
        ):
            with self.subTest(field=field):
                with self.assertRaises(GitHubBrokerClientError):
                    accounting.verify(
                        {**request, field: value, "dispatched": 1}, request
                    )
        self.assertEqual(budget.calls, 1)

    def test_other_before_request_budget_cannot_replace_original_owner(self):
        budget, other = EvidenceReadBudget(), EvidenceReadBudget()
        control = BrokerDispatchAccounting(
            budget=budget,
            operation_id="a" * 64,
            exchange_liability=13,
            verification_liability=1,
        )
        with self.assertRaises(GitHubBrokerClientError):
            BrokerClient(control=control, before_request=other.consume)
