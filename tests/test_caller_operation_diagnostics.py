"""Current diagnostic counters describe the original caller-run allowance."""

import tempfile
import unittest

from review_sensei_github.operation_diagnostics import OperationObserver

from tests.test_api_work_lifecycle import LifecycleFixture
from tests.test_api_work_persistence import IDENTITY


class CallerOperationDiagnosticsTests(unittest.TestCase):
    def test_large_current_run_retains_observed_calls_and_configured_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LifecycleFixture(directory, limit=512)
            native, configuration, _, owner = fixture.prelude()
            fixture.accounted.initialize_and_activate(
                native, IDENTITY, configuration, owner
            )
            for _ in range(70):
                fixture.http.request(
                    "GET", "/repos/owner/repo/pulls/7", token="synthetic"
                )
            budget = fixture.accounted.budget_view
            observer = OperationObserver(budget)
            observer.at("check.publication")
            observer.response(503, None)
            report = observer.report().to_dict()
            self.assertGreater(report["budget_calls"], 64)
            self.assertEqual(report["budget_calls"], budget.calls)
            self.assertEqual(report["call_limit"], 512)
            self.assertEqual(
                report["ordinary_call_limit"], 512 - fixture.plan.liability
            )
            self.assertEqual(report["check"]["first"]["budget_calls"], budget.calls)

    def test_unbound_local_evidence_diagnostics_keep_their_own_limits(self):
        from review_sensei.bounded_evidence import EvidenceReadBudget

        observer = OperationObserver(EvidenceReadBudget())
        observer.response(500, None)
        report = observer.report().to_dict()
        self.assertEqual(
            (report["call_limit"], report["ordinary_call_limit"]), (64, 60)
        )
