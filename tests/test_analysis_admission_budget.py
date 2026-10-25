"""Analysis admission prepays the cold fallback from its original budget."""

import hashlib
import json
import os
import unittest
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from review_sensei_github.errors import GitHubBrokerClientError
from review_sensei_github.review_hooks import (
    _ANALYSIS_SESSION_INTERNAL_DISPATCHES,
    GitHubReviewHooks,
)

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.errors import ReviewInputError
from tests.test_github_broker_client import FakeResponse


class AnalysisAdmissionBudgetTests(unittest.TestCase):
    def setUp(self):
        self.args = SimpleNamespace(
            repository="acme/widgets",
            repository_id=987654321,
            pull_request=7,
            head_sha="a" * 40,
            oidc_token="synthetic",
            app_slug="reviewsensei[bot]",
        )

    def client(self, budget, transport, liability=30):
        client = GitHubReviewHooks._admission_broker(self.args, budget, liability)
        client.opener = transport
        return client

    def response(self, request, *, dispatched=30, fault=None):
        payload = json.loads(request.data)
        control = payload["control_budget"]
        receipt = {**control, "dispatched": dispatched}
        if fault:
            receipt[fault] = "b" * 64
        return FakeResponse(
            json.dumps(
                {
                    "token": "synthetic",
                    "capability": "review_session",
                    "session_state": "enrolled",
                    "control_receipt": receipt,
                }
            ).encode()
        )

    def open_session(self, client):
        return client.open_session(
            "synthetic",
            repository_id=self.args.repository_id,
            pull_request=7,
            head_sha=self.args.head_sha,
        )

    def test_fallback_prepays_thirty_internal_and_one_outer_dispatch(self):
        budget = EvidenceReadBudget()
        deadline = budget.wall_deadline_ms
        requests = []

        def transport(request, timeout):
            requests.append(json.loads(request.data))
            self.assertEqual(budget.calls, 31)
            self.assertEqual(requests[-1]["control_budget"]["max_dispatches"], 30)
            return self.response(request)

        self.assertEqual(
            self.open_session(self.client(budget, transport)).state, "enrolled"
        )
        control = requests[0]["control_budget"]
        self.assertEqual(control["deadline_unix_ms"], deadline)
        self.assertEqual(
            control["operation_id"],
            hashlib.sha256(
                f"analysis-admission:987654321:7:{self.args.head_sha}".encode()
            ).hexdigest(),
        )
        self.assertEqual(budget.calls, 31)

    def test_rescan_exchange_keeps_the_prefix_only_reservation(self):
        liability = 12
        budget = EvidenceReadBudget()
        controls = []

        def transport(request, timeout):
            controls.append(json.loads(request.data)["control_budget"])
            return self.response(request, dispatched=liability)

        client = self.client(budget, transport, liability=liability)
        for _ in range(2):
            before = budget.calls
            self.open_session(client)
            self.assertEqual(budget.calls - before, liability + 1)
        self.assertEqual(budget.calls, 2 * (liability + 1))
        self.assertEqual(
            [item["max_dispatches"] for item in controls], [liability, liability]
        )

    def test_full_reservation_is_kept_for_a_warm_shorter_exchange(self):
        for dispatched in (9, 30):
            with self.subTest(dispatched=dispatched):
                budget = EvidenceReadBudget()
                client = self.client(
                    budget,
                    lambda request, timeout: self.response(
                        request, dispatched=dispatched
                    ),
                )
                self.open_session(client)
                self.assertEqual(budget.calls, 31)

    def test_last_funded_request_and_atomic_unfunded_refusal(self):
        liability = _ANALYSIS_SESSION_INTERNAL_DISPATCHES
        ceiling = EvidenceReadBudget().ordinary_remaining()
        # One admission needs its whole prepaid liability plus the caller HTTP
        # request, so the boundary sits at the ceiling minus both, not at the
        # liability itself; deriving it keeps the pair tracking the constant.
        for prior in (ceiling - liability - 1, ceiling - liability):
            with self.subTest(prior=prior):
                budget = EvidenceReadBudget()
                for _ in range(prior):
                    budget.consume()
                dispatched = []

                def transport(request, timeout):
                    dispatched.append(request)
                    return self.response(request, dispatched=liability)

                client = self.client(budget, transport, liability=liability)
                if prior == ceiling - liability - 1:
                    self.open_session(client)
                    self.assertEqual(budget.ordinary_remaining(), 0)
                    self.assertEqual(len(dispatched), 1)
                else:
                    with self.assertRaisesRegex(ReviewInputError, "original budget"):
                        self.open_session(client)
                    self.assertEqual(budget.calls, prior)
                    self.assertEqual(dispatched, [])

    def test_changed_receipt_refuses_and_keeps_original_charges(self):
        for fault in ("request_id", "operation_id", "deadline_unix_ms"):
            with self.subTest(fault=fault):
                budget = EvidenceReadBudget()
                client = self.client(
                    budget, lambda req, timeout: self.response(req, fault=fault)
                )
                with self.assertRaises(GitHubBrokerClientError):
                    self.open_session(client)
                self.assertEqual(budget.calls, 31)

    def test_expired_original_deadline_refuses_before_transport(self):
        clock = [0.0]
        budget = EvidenceReadBudget(clock=lambda: clock[0])
        client = self.client(
            budget, lambda *_args, **_kwargs: self.fail("expired transport")
        )
        clock[0] = 60.0
        with self.assertRaises(ReviewInputError):
            self.open_session(client)
        self.assertEqual(budget.calls, 0)

    def test_admission_then_real_rescan_keeps_one_cumulative_budget(self):
        from tests.test_default_human_rescan import DefaultHumanRescanTests

        with TemporaryDirectory() as directory:
            state, ledger, env, args = DefaultHumanRescanTests().prepare(
                directory, count=3, detail=1
            )
            args.oidc_token = "synthetic"
            budget = EvidenceReadBudget()
            deadline = budget.wall_deadline_ms
            ledger.evidence_budget = budget
            controls = []
            liabilities = []
            original_factory = GitHubReviewHooks._admission_broker

            def transport(request, timeout):
                payload = json.loads(request.data)
                controls.append(payload["control_budget"])
                return self.response(request, dispatched=6)

            def admission_broker(broker_args, allowance, internal_dispatches):
                liabilities.append(internal_dispatches)
                client = original_factory(broker_args, allowance, internal_dispatches)
                client.opener = transport
                return client

            client = original_factory(args, budget, 30)
            client.opener = transport
            client.open_session(
                args.oidc_token,
                repository_id=args.repository_id,
                pull_request=args.pull_request,
                head_sha=args.head_sha,
            )
            self.assertEqual(budget.calls, 31)
            with (
                patch.dict(os.environ, env),
                patch.object(
                    GitHubReviewHooks,
                    "_admission_broker",
                    side_effect=admission_broker,
                ),
            ):
                authorized = GitHubReviewHooks().resolve_rescan(args, ledger)
            self.assertEqual(authorized.request_id, 10)
            self.assertEqual(authorized.review_id, state.reviews[-1]["id"])
            self.assertIs(ledger.evidence_budget, budget)
            self.assertEqual(liabilities, [12])
            self.assertEqual(budget.calls, 44 + len(state.rescan_calls))
            self.assertLessEqual(budget.calls, 60)
            self.assertEqual(budget.wall_deadline_ms, deadline)
            self.assertEqual(len(controls), 2)
            self.assertEqual(len({item["operation_id"] for item in controls}), 1)
            self.assertEqual(len({item["request_id"] for item in controls}), 2)
            self.assertEqual([item["max_dispatches"] for item in controls], [30, 12])


if __name__ == "__main__":
    unittest.main()
