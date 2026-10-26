"""Hosted phases stay cumulative; local metadata windows remain independent."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from review_sensei_github.http import GitHubHttp
from review_sensei_github.session_ledger import GitHubIssueCommentSessionLedger

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.errors import ProviderError, ReviewInputError
from review_sensei.session_phases import AnalysisSessionPhases
from tests.fake_github_http import json_response


class AnalysisMetadataPhaseTests(unittest.TestCase):
    def test_actual_hosted_run_finishes_after_long_provider_without_phase_reset(self):
        self._hosted_run_journey()

    def test_actual_hosted_provider_failure_keeps_unknown_operation_and_charges(self):
        self._hosted_run_journey(failure=True)

    def _hosted_run_journey(self, *, failure=False):
        from review_sensei_github.session_ledger import parse_session_comment

        from tests.test_api_work_persistence import IDENTITY
        from tests.test_review_run import EntryFixture

        with tempfile.TemporaryDirectory() as directory:
            fixture = EntryFixture(directory)
            elapsed = [0.0]
            budget = []
            before = []

            def admitted():
                caller = fixture.accounted
                clock, monotonic = caller._clock(), caller._monotonic()
                caller._clock = lambda: clock + elapsed[0]
                caller._monotonic = lambda: monotonic + elapsed[0]
                budget.append(caller.budget_view)
                before.append(caller.accounting.confirmed)

            def inference():
                elapsed[0] += 75
                if failure:
                    raise ProviderError("synthetic unknown provider outcome")

            fixture.before_entry, fixture.before_provider = admitted, inference
            self.assertEqual(fixture.invoke(), 1 if failure else 0, fixture.error)
            caller = fixture.accounted
            record = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            self.assertEqual(elapsed[0], 75)
            self.assertIs(caller.budget_view, budget[0])
            self.assertIs(fixture.selected.ledger.evidence_budget, budget[0])
            self.assertGreater(caller.accounting.confirmed, before[0])
            self.assertEqual(len(fixture.transports), 1)
            self.assertEqual(len(caller.observations), len(fixture.trace))
            if failure:
                self.assertIsNone(record.transaction)
                self.assertIsNone(record.reservation_id)
                self.assertEqual(record.completed_initial_reviews, 0)
                self.assertIsNone(record.api_run_accounting["analysis_abort"])
                fixture.before_entry = fixture.before_provider = None
                fixture.next_run()
                self.assertEqual(fixture.invoke(), 1)
                self.assertEqual(fixture.provider_calls, 1)
            else:
                self.assertEqual(record.transaction.phase, "publication_succeeded")
                self.assertEqual(
                    json.loads((fixture.root / "review.json").read_text())[
                        "review_status"
                    ],
                    "complete",
                )

    def test_actual_local_cli_checkpoints_after_long_provider_with_existing_snapshot(
        self,
    ):
        self._local_cli_journey()

    def test_actual_local_cli_failure_releases_under_one_completion_budget(self):
        self._local_cli_journey(failure=True)

    def _local_cli_journey(self, *, failure=False):
        from review_sensei.cli import main
        from review_sensei.session import LocalSessionLedger, SessionIdentity
        from tests import test_review_transaction as tx
        from tests.test_disposition_snapshots import snapshot

        clock = [0.0]
        original_init = EvidenceReadBudget.__init__

        def initialize(budget, **kwargs):
            return original_init(budget, **{**kwargs, "clock": lambda: clock[0]})

        class SlowProvider(tx.HostedAnalysisCliTests._Provider):
            def complete(self, request):
                clock[0] += 75
                if failure:
                    raise ReviewInputError("synthetic provider interruption")
                return super().complete(request)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = LocalSessionLedger(root / "ledger")
            identity = SessionIdentity("owner/repo", 146)
            ledger.initialize(identity)
            authority = snapshot()
            authority["state_manifest"]["binding"].update(
                repository_id=None, pull_request=146
            )
            # The manifest digest covers the unchanged decision document.
            ledger.replace(
                identity,
                lambda current: current.evolve(
                    generation=1, disposition_snapshot=authority
                ),
            )
            args = tx.HostedAnalysisCliTests()._argv(
                root, admission_context_output=root / "admission.json"
            )
            args.extend(["--format", "json"])
            errors = io.StringIO()
            with (
                patch.object(EvidenceReadBudget, "__init__", initialize),
                patch(
                    "review_sensei.cli.default_registry",
                    return_value=tx.HostedAnalysisCliTests._Registry(SlowProvider()),
                ),
                redirect_stderr(errors),
            ):
                self.assertEqual(main(args), 2 if failure else 0, errors.getvalue())
            record = LocalSessionLedger(root / "ledger").load(identity).record
            if failure:
                self.assertIsNone(record.transaction)
                self.assertIsNone(record.reservation_id)
                self.assertEqual(record.failed_attempts, 1)
                self.assertGreaterEqual(clock[0], 75)
                return
            self.assertEqual(record.transaction.phase, "publication_pending")
            self.assertIsNotNone(record.disposition_snapshot)
            result = json.loads((root / "review.json").read_text())
            self.assertEqual(result["review_status"], "complete")
            self.assertGreaterEqual(clock[0], 75)

    def fixture(self):
        clock = [0.0]
        calls = []

        def open(request, timeout):
            calls.append((request.method, timeout))
            return json_response({})

        http = GitHubHttp(api_url="https://api.github.test", opener=open)

        def budget():
            return EvidenceReadBudget(clock=lambda: clock[0])

        def connection(allowance):
            return GitHubIssueCommentSessionLedger(
                http, token="synthetic", evidence_budget=allowance
            )

        phases = AnalysisSessionPhases(
            connection, budget(), completion_budget_factory=budget
        )
        return clock, calls, phases

    def test_local_provider_time_is_separate_and_completion_is_one_budget(self):
        clock, calls, phases = self.fixture()
        admission = phases.admission
        admission._request("GET", "/admission")
        provider = phases.begin_provider(admission)
        self.assertIsNone(provider.evidence_budget)
        self.assertIs(provider.http, admission.http)
        clock[0] = 75
        completion = phases.completion(admission)
        completion._request("GET", "/completion")
        self.assertEqual(len(calls), 2)
        self.assertEqual(admission.evidence_budget.calls, 1)
        self.assertEqual(completion.evidence_budget.calls, 1)
        self.assertIs(phases.completion(admission), completion)
        with self.assertRaises(ReviewInputError):
            admission._request("GET", "/closed")
        clock[0] = 140
        same = phases.completion(completion)
        self.assertIs(same, completion)
        with self.assertRaises(ReviewInputError):
            same._request("GET", "/expired")
        self.assertEqual(len(calls), 2)

    def test_local_admission_exhaustion_cannot_open_provider_or_completion(self):
        clock, calls, phases = self.fixture()
        clock[0] = 61
        with self.assertRaises(ReviewInputError):
            phases.begin_provider(phases.admission)
        with self.assertRaises(ReviewInputError):
            phases.completion(phases.admission)
        self.assertEqual(calls, [])
