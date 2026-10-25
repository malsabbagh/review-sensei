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
    ANALYSIS_ACTION_INTERNAL_DISPATCHES,
    ANALYSIS_SESSION_INTERNAL_DISPATCHES,
    GitHubReviewHooks,
)

from review_sensei.bounded_evidence import MAX_PART_READS, EvidenceReadBudget
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

    def client(self, budget, transport, liability=ANALYSIS_SESSION_INTERNAL_DISPATCHES):
        client = GitHubReviewHooks._admission_broker(
            self.args, budget, liability=liability
        )
        client.opener = transport
        return client

    def response(
        self, request, *, dispatched=ANALYSIS_SESSION_INTERNAL_DISPATCHES, fault=None
    ):
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
        # 30 is the exact enumerated cold ceiling in ADR 0088, not an estimate.
        # Pin the production constant to that literal so a dispatch added to the
        # exchange fails here together with the ADR instead of drifting quietly.
        self.assertEqual(ANALYSIS_SESSION_INTERNAL_DISPATCHES, 30)
        liability = ANALYSIS_SESSION_INTERNAL_DISPATCHES
        budget = EvidenceReadBudget()
        deadline = budget.wall_deadline_ms
        requests = []

        def transport(request, timeout):
            requests.append(json.loads(request.data))
            self.assertEqual(budget.calls, liability + 1)
            self.assertEqual(
                requests[-1]["control_budget"]["max_dispatches"], liability
            )
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
        self.assertEqual(budget.calls, liability + 1)

    def test_rescan_exchange_keeps_the_prefix_only_reservation(self):
        # Pin the documented action reservation alongside the session ceiling
        # above, so a change to either contract value fails here.
        self.assertEqual(ANALYSIS_ACTION_INTERNAL_DISPATCHES, 12)
        liability = ANALYSIS_ACTION_INTERNAL_DISPATCHES
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
        # The action ceiling is the whole receipt bound, not just the value the
        # warm transport happens to echo: one dispatch above it must refuse.
        over = self.client(
            EvidenceReadBudget(),
            lambda request, timeout: self.response(request, dispatched=liability + 1),
            liability=liability,
        )
        with self.assertRaises(GitHubBrokerClientError):
            self.open_session(over)

    def test_combined_reservations_fit_one_ordinary_admission_budget(self):
        # Enrollment and rescan authorization share the admission budget: both
        # prepaid reservations plus their two caller POSTs must fit the ordinary
        # ceiling, or a complete rescan lifecycle refuses only in production.
        ceiling = EvidenceReadBudget().ordinary_remaining()
        self.assertLessEqual(
            ANALYSIS_SESSION_INTERNAL_DISPATCHES
            + ANALYSIS_ACTION_INTERNAL_DISPATCHES
            + 2,
            ceiling,
        )

    def test_full_reservation_is_kept_when_the_exchange_reports_fewer_dispatches(
        self,
    ):
        # Verification accepts any observed count inside the admitted maximum
        # (0 <= dispatched <= max_dispatches), so 9 is a valid warm-path receipt
        # rather than a mismatch. Both arms keep the full prepaid reservation
        # plus the caller's outer HTTP dispatch; unused liability is not
        # refunded. Reference the constant so the total cannot drift alone.
        # A second cold admission cannot follow in the same budget: two full
        # reservations plus their caller POSTs exceed the ordinary ceiling, a
        # bound the combined-reservation test above asserts.
        for dispatched in (9, ANALYSIS_SESSION_INTERNAL_DISPATCHES):
            with self.subTest(dispatched=dispatched):
                budget = EvidenceReadBudget()
                client = self.client(
                    budget,
                    lambda request, timeout: self.response(
                        request, dispatched=dispatched
                    ),
                )
                self.open_session(client)
                self.assertEqual(budget.calls, ANALYSIS_SESSION_INTERNAL_DISPATCHES + 1)

    def test_last_funded_request_and_atomic_unfunded_refusal(self):
        liability = ANALYSIS_SESSION_INTERNAL_DISPATCHES
        ceiling = EvidenceReadBudget().ordinary_remaining()
        # Four of the 64 calls stay reserved for finalization; pin that ceiling
        # so a change to either offset fails here rather than moving the
        # boundary silently. One admission needs its whole prepaid liability plus
        # the caller HTTP request, so the boundary sits at the ceiling minus
        # both, not at the liability itself.
        self.assertEqual(ceiling, MAX_PART_READS - 4)
        self.assertGreaterEqual(ceiling - liability - 1, 0)
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
                self.assertEqual(budget.calls, ANALYSIS_SESSION_INTERNAL_DISPATCHES + 1)

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

            def admission_broker(broker_args, allowance, *, liability):
                liabilities.append(liability)
                client = original_factory(broker_args, allowance, liability=liability)
                client.opener = transport
                return client

            client = original_factory(
                args, budget, liability=ANALYSIS_SESSION_INTERNAL_DISPATCHES
            )
            client.opener = transport
            client.open_session(
                args.oidc_token,
                repository_id=args.repository_id,
                pull_request=args.pull_request,
                head_sha=args.head_sha,
            )
            self.assertEqual(budget.calls, ANALYSIS_SESSION_INTERNAL_DISPATCHES + 1)
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
            self.assertEqual(liabilities, [ANALYSIS_ACTION_INTERNAL_DISPATCHES])
            # One admission and one rescan, each prepaying its internal
            # liability plus its caller HTTP dispatch, plus the rescan's own
            # reads. Reference both constants so the relation stays visible.
            self.assertEqual(
                budget.calls,
                (ANALYSIS_SESSION_INTERNAL_DISPATCHES + 1)
                + (ANALYSIS_ACTION_INTERNAL_DISPATCHES + 1)
                + len(state.rescan_calls),
            )
            self.assertLessEqual(budget.calls, 60)
            self.assertEqual(budget.wall_deadline_ms, deadline)
            self.assertEqual(len(controls), 2)
            self.assertEqual(len({item["operation_id"] for item in controls}), 1)
            self.assertEqual(len({item["request_id"] for item in controls}), 2)
            self.assertEqual(
                [item["max_dispatches"] for item in controls],
                [
                    ANALYSIS_SESSION_INTERNAL_DISPATCHES,
                    ANALYSIS_ACTION_INTERNAL_DISPATCHES,
                ],
            )


if __name__ == "__main__":
    unittest.main()
