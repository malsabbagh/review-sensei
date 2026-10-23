"""Long inference does not reset or consume a different metadata window."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from review_sensei_github.http import GitHubHttp
from review_sensei_github.review_hooks import GitHubReviewHooks
from review_sensei_github.session_ledger import GitHubIssueCommentSessionLedger
from review_sensei_github.session_phases import AnalysisSessionPhases

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.errors import ReviewInputError
from tests.fake_github_http import json_response


class AnalysisMetadataPhaseTests(unittest.TestCase):
    def test_actual_hosted_cli_checkpoints_after_long_provider(self):
        self._hosted_cli_journey()

    def test_actual_hosted_cli_failure_releases_under_one_completion_budget(self):
        self._hosted_cli_journey(failure=True)

    def _hosted_cli_journey(self, *, failure=False):
        from review_sensei_github.session_ledger import render_session_comment

        from review_sensei.cli import main
        from review_sensei.session import SessionRecord
        from tests import test_review_transaction as tx
        from tests.test_disposition_snapshots import snapshot
        from tests.test_hosted_disposition_snapshots import (
            APP,
            BASE,
            HEAD,
            IDENTITY,
            HostedDecisionState,
        )

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

        state = HostedDecisionState()

        def connection(budget):
            return GitHubIssueCommentSessionLedger(
                state.http, token="synthetic", app_slug=APP, evidence_budget=budget
            )

        authority = snapshot()
        authority["state_manifest"]["binding"]["producer"] = "github-bot:99"
        initial = SessionRecord.create(
            IDENTITY, generation=1, disposition_snapshot=authority
        )
        state.issue_comments[600]["body"] = render_session_comment(
            repository_id=1, pull_request=1, record=initial
        )

        class Hooks(GitHubReviewHooks):
            def resolve_ledger(self, args, **kwargs):
                return AnalysisSessionPhases(connection, EvidenceReadBudget()).admission

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = tx.HostedAnalysisCliTests()._argv(
                root, admission_context_output=root / "admission.json"
            )
            index = args.index("--session-ledger")
            del args[index : index + 2]
            args[args.index("--pull-request") + 1] = "1"
            args[args.index("--head-sha") + 1] = HEAD
            args[args.index("--base-sha") + 1] = BASE
            args.extend(
                ["--github-session-ledger", "--repository-id", "1", "--format", "json"]
            )
            errors = io.StringIO()
            with (
                patch.object(EvidenceReadBudget, "__init__", initialize),
                patch(
                    "review_sensei.cli.default_registry",
                    return_value=tx.HostedAnalysisCliTests._Registry(SlowProvider()),
                ),
                redirect_stderr(errors),
            ):
                self.assertEqual(
                    main(args, hosted_hooks=Hooks()),
                    2 if failure else 0,
                    errors.getvalue(),
                )
            record = connection(EvidenceReadBudget()).load(IDENTITY).record
            if failure:
                self.assertIsNone(record.transaction)
                self.assertIsNone(record.reservation_id)
                self.assertEqual(record.failed_attempts, 1)
                self.assertGreaterEqual(clock[0], 75)
                return
            self.assertEqual(record.transaction.phase, "publication_pending")
            self.assertIsNotNone(record.disposition_snapshot)
            self.assertEqual(
                json.loads((root / "review.json").read_text())["review_status"],
                "complete",
            )
            self.assertGreaterEqual(clock[0], 75)

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
        return clock, calls, phases, GitHubReviewHooks()

    def test_provider_time_is_separate_and_completion_is_one_budget(self):
        clock, calls, phases, hooks = self.fixture()
        admission = phases.admission
        admission._request("GET", "/admission")
        provider = hooks.begin_provider_phase(admission)
        self.assertIsNone(provider.evidence_budget)
        self.assertIs(provider.http, admission.http)
        clock[0] = 75
        completion = hooks.begin_completion_phase(admission)
        completion._request("GET", "/completion")
        self.assertEqual(len(calls), 2)
        self.assertEqual(admission.evidence_budget.calls, 1)
        self.assertEqual(completion.evidence_budget.calls, 1)
        self.assertIs(hooks.begin_completion_phase(admission), completion)
        with self.assertRaises(ReviewInputError):
            admission._request("GET", "/closed")
        clock[0] = 140
        same = hooks.begin_completion_phase(completion)
        self.assertIs(same, completion)
        with self.assertRaises(ReviewInputError):
            same._request("GET", "/expired")
        self.assertEqual(len(calls), 2)

    def test_admission_exhaustion_cannot_open_provider_or_completion(self):
        clock, calls, phases, hooks = self.fixture()
        clock[0] = 61
        with self.assertRaises(ReviewInputError):
            hooks.begin_provider_phase(phases.admission)
        with self.assertRaises(ReviewInputError):
            hooks.begin_completion_phase(phases.admission)
        self.assertEqual(calls, [])
