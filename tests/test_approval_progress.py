"""Finalization observations explain progress without minting approval authority."""

import json
import tempfile
import unittest
from types import SimpleNamespace
from urllib.parse import urlsplit

from review_sensei_github.approval_progress import (
    build_approval_progress,
    render_command_progress,
)
from review_sensei_github.http import GitHubHttp

from review_sensei.disposition import MaintainerCommandResult
from tests.fake_github_http import json_response
from tests.test_command_capacity_recovery import CommandCapacityState


class ApprovalProgressTests(unittest.TestCase):
    def build(self, **changes):
        values = dict(
            head_sha="a" * 40,
            result_digest="b" * 64,
            source_review_id=123,
            honored_findings=25,
            pending_human_assessments=0,
            body_blocker_remaining=False,
            review_status="complete",
            coverage_blocker=None,
            recorded_qualification="not-required",
            decided_thread_reconciliation="confirmed",
            approval_status="approved",
            approval_review_id=456,
            check_attempted=True,
            check_confirmed=True,
            check_run_id=789,
            requested_check_conclusion="success",
        )
        return build_approval_progress(**(values | changes))

    def test_check_failure_preserves_confirmed_approval_observation(self):
        progress = self.build(check_confirmed=False, check_run_id=None)
        document = progress.to_dict()
        self.assertEqual(document["approval_state"], "submitted")
        self.assertEqual(document["approval_review_id"], 456)
        self.assertEqual(document["check_state"], "unconfirmed")
        self.assertEqual(document["next_action"], "retry_verify")

    def test_partial_unknown_source_does_not_promise_continuation_or_media_approval(
        self,
    ):
        progress = self.build(
            approval_status="approval_withheld",
            approval_review_id=None,
            review_status="partial",
            coverage_blocker="partial",
        )
        document = progress.to_dict()
        self.assertEqual(document["next_action"], "inspect_partial_source")
        self.assertIsNone(document["covered_units"])
        self.assertIsNone(document["pending_units"])
        self.assertIsNone(document["media_source_comment_id"])
        self.assertEqual(document["review_status"], "partial")

    def test_no_check_token_is_not_success_and_unknown_native_outcome_stays_unknown(
        self,
    ):
        progress = self.build(
            approval_status="gate_reevaluation_failed",
            approval_review_id=None,
            check_attempted=False,
            check_confirmed=False,
            check_run_id=None,
        )
        self.assertEqual(progress.to_dict()["check_state"], "not-attempted")
        self.assertEqual(progress.to_dict()["approval_state"], "not-observed")
        self.assertEqual(progress.to_dict()["next_action"], "retry_verify")

    def test_stale_source_requires_refresh_rather_than_unchanged_verify(self):
        progress = self.build(
            approval_status="skipped_stale_head", approval_review_id=None
        )
        self.assertEqual(progress.next_action, "refresh_current_source")
        self.assertEqual(progress.approval_state, "not-observed")

    def test_render_is_allowlisted_machine_readable_and_preserves_result_flags(self):
        progress = self.build()
        result = MaintainerCommandResult(
            action="verify",
            applied=True,
            operator_paused=False,
            summary="Decisions loaded; native approval submitted.",
        )
        publication = SimpleNamespace(approval_progress=progress)
        rendered = render_command_progress(result, publication)
        self.assertEqual(
            json.loads(rendered.summary.split("\napproval-progress: ")[1]),
            progress.to_dict(),
        )
        self.assertTrue(rendered.applied)
        self.assertFalse(rendered.evaluation_failed)
        self.assertNotIn("token", rendered.summary)
        self.assertEqual(render_command_progress(result, SimpleNamespace()), result)


class ActualFinalizerProgressTests(unittest.TestCase):
    def test_permission_denied_check_is_not_confirmed(self):
        import tempfile
        from urllib.parse import urlsplit

        from review_sensei_github.http import GitHubHttp

        from tests.fake_github_http import json_response
        from tests.test_command_capacity_recovery import CommandCapacityState

        state = CommandCapacityState(complete=True)

        def opener(request, timeout):
            if request.method == "POST" and urlsplit(request.full_url).path.endswith(
                "/check-runs"
            ):
                return json_response({"message": "Resource not accessible"}, 403)
            return state.open(request, timeout)

        state.http = GitHubHttp(api_url="https://api.github.test", opener=opener)
        with tempfile.TemporaryDirectory() as directory:
            outcome, _, _ = state.verify(state.record(directory))
        self.assertEqual(sum(r["state"] == "APPROVED" for r in state.reviews), 1)
        self.assertEqual(state.checks, [])
        self.assertEqual(outcome.approval_progress.approval_state, "submitted")
        self.assertEqual(outcome.approval_progress.check_state, "unconfirmed")
        self.assertIsNone(outcome.approval_progress.check_run_id)
        self.assertEqual(outcome.approval_progress.next_action, "retry_verify")

    def test_lost_approval_response_is_unknown_until_existing_review_is_observed(self):
        import tempfile
        from unittest.mock import patch

        from review_sensei_github.errors import GitHubPublicationError
        from review_sensei_github.publication import ReviewApprovalFinalizer

        from tests.test_command_capacity_recovery import CommandCapacityState

        state = CommandCapacityState(complete=True)
        real_finalize = ReviewApprovalFinalizer.finalize

        def lose_response(finalizer, **kwargs):
            result = real_finalize(finalizer, **kwargs)
            self.assertEqual(result.status, "approved")
            raise GitHubPublicationError("Synthetic unreadable committed approval")

        with tempfile.TemporaryDirectory() as directory:
            decisions = state.record(directory)
            with patch.object(ReviewApprovalFinalizer, "finalize", lose_response):
                outcome, _, _ = state.verify(decisions)
            replay, _, _ = state.verify(decisions)
        # Existing result compatibility is preserved; the new observation must
        # not interpret a caught transport/publication exception as a refusal.
        self.assertEqual(outcome.status, "approval_withheld")
        self.assertEqual(outcome.approval_progress.approval_state, "not-observed")
        self.assertIsNone(outcome.approval_progress.approval_review_id)
        operation = outcome.operation_diagnostics.to_dict()
        self.assertTrue(operation["approval_request_attempted"])
        self.assertEqual(operation["approval"]["first"]["reason"], "unknown")
        self.assertNotIn("Synthetic unreadable", json.dumps(operation))
        from review_sensei_github import GitHubApplication

        app = GitHubApplication(
            broker=SimpleNamespace(exchange=lambda *args, **kwargs: "synthetic"),
            http=None,
            reviewer=SimpleNamespace(
                finalizer=SimpleNamespace(
                    refinalize_with_dispositions=lambda **kwargs: outcome
                )
            ),
            learner=SimpleNamespace(),
            replier=SimpleNamespace(),
        )
        command = app._refinalize_command(
            MaintainerCommandResult(
                action="verify",
                applied=True,
                operator_paused=False,
                summary="Decisions loaded",
            ),
            oidc_token="synthetic",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha="b" * 40,
            app_slug="reviewsensei[bot]",
            actor="alice",
            record=SimpleNamespace(),
        )
        self.assertIn("approval outcome unconfirmed", command.summary)
        self.assertNotIn("approval withheld", command.summary)
        self.assertEqual(replay.approval_progress.approval_state, "observed-existing")
        self.assertEqual(sum(r["state"] == "APPROVED" for r in state.reviews), 1)

    def test_diagnostics_add_no_http_calls_and_preserve_native_approval(self):
        import tempfile
        from unittest.mock import patch

        from tests.test_command_capacity_recovery import CommandCapacityState

        paths = []
        for enabled in (False, True):
            state = CommandCapacityState(complete=True)
            with tempfile.TemporaryDirectory() as directory:
                decisions = state.record(directory)
                if enabled:
                    outcome, budget, calls = state.verify(decisions)
                else:
                    with patch(
                        "review_sensei_github.publication.build_approval_progress",
                        return_value=None,
                    ):
                        outcome, budget, calls = state.verify(decisions)
            self.assertEqual(outcome.status, "approved")
            self.assertEqual(sum(r["state"] == "APPROVED" for r in state.reviews), 1)
            self.assertEqual(budget.calls, len(calls))
            paths.append([(method, path) for method, path, _ in calls])
            if enabled:
                self.assertEqual(outcome.approval_progress.source_review_id, 40)
                self.assertEqual(outcome.approval_progress.approval_review_id, 41)
                self.assertEqual(outcome.approval_progress.check_state, "confirmed")
        self.assertEqual(paths[0], paths[1])

    def test_actual_check_failure_keeps_one_native_approval_across_retry(self):
        import tempfile

        from review_sensei_github.errors import GitHubPublicationError

        from tests.test_command_capacity_recovery import CommandCapacityState

        state = CommandCapacityState(complete=True)

        def fail_check(state, method, path, data):
            if method == "POST" and path.endswith("/check-runs"):
                raise GitHubPublicationError("Synthetic unconfirmed check response")

        state.hook = fail_check
        with tempfile.TemporaryDirectory() as directory:
            decisions = state.record(directory)
            outcome, _, _ = state.verify(decisions)
            replay, _, _ = state.verify(decisions)
        self.assertEqual(outcome.status, "gate_reevaluation_failed")
        self.assertEqual(outcome.approval_progress.approval_state, "submitted")
        self.assertEqual(outcome.approval_progress.check_state, "unconfirmed")
        self.assertEqual(replay.approval_progress.approval_state, "observed-existing")
        self.assertEqual(sum(r["state"] == "APPROVED" for r in state.reviews), 1)

    def test_actual_partial_review_reports_unknown_coverage_and_source(self):
        import tempfile

        from tests.test_command_capacity_recovery import CommandCapacityState

        state = CommandCapacityState(complete=False)
        with tempfile.TemporaryDirectory() as directory:
            outcome, _, _ = state.verify(state.record(directory))
        self.assertEqual(outcome.status, "approval_withheld")
        self.assertEqual(
            outcome.approval_progress.next_action, "inspect_partial_source"
        )
        self.assertEqual(outcome.approval_progress.pending_human_assessments, 0)
        self.assertIsNone(outcome.approval_progress.pending_units)
        self.assertEqual(state.checks[-1]["conclusion"], "action_required")
        self.assertFalse(any(r["state"] == "APPROVED" for r in state.reviews))


class FinalizationOperationDiagnosticsTests(unittest.TestCase):
    def test_malformed_check_response_preserves_approval_and_reports_check_stage(self):
        state = CommandCapacityState(complete=True)

        def opener(request, timeout):
            if request.method == "GET" and urlsplit(request.full_url).path.endswith(
                "/check-runs"
            ):
                return json_response({"unexpected": "SECRET-upstream-body"})
            return state.open(request, timeout)

        state.http = GitHubHttp(api_url="https://api.github.test", opener=opener)
        with tempfile.TemporaryDirectory() as directory:
            result, budget, _ = state.verify(state.record(directory))
        self.assertEqual(result.status, "gate_reevaluation_failed")
        self.assertEqual(result.approval_progress.approval_state, "submitted")
        self.assertEqual(result.approval_progress.check_state, "unconfirmed")
        self.assertEqual(sum(r["state"] == "APPROVED" for r in state.reviews), 1)
        report = result.operation_diagnostics.to_dict()
        self.assertEqual(report["check"]["first"]["stage"], "check.publication")
        self.assertEqual(report["check"]["first"]["reason"], "unknown")
        self.assertEqual(report["budget_calls"], budget.calls)
        self.assertNotIn("SECRET", json.dumps(report))

    def test_initial_transport_failure_reaches_public_command_without_raw_error(self):
        from urllib.error import URLError

        from review_sensei_github import GitHubApplication
        from review_sensei_github.publication import ReviewApprovalFinalizer

        def opener(request, timeout):
            raise URLError("SECRET-network-detail")

        app = GitHubApplication(
            broker=SimpleNamespace(exchange=lambda *args, **kwargs: "synthetic"),
            http=None,
            reviewer=SimpleNamespace(
                finalizer=ReviewApprovalFinalizer(
                    http=GitHubHttp(api_url="https://api.github.test", opener=opener)
                )
            ),
            learner=SimpleNamespace(),
            replier=SimpleNamespace(),
        )
        command = MaintainerCommandResult(
            action="verify", applied=True, operator_paused=False, summary="Retained"
        )
        result = app._refinalize_command(
            command,
            oidc_token="synthetic",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha="b" * 40,
            app_slug="reviewsensei[bot]",
            actor="alice",
            record=SimpleNamespace(),
        )
        self.assertTrue(result.applied)
        self.assertTrue(result.evaluation_failed)
        report = json.loads(result.summary.split("\napproval-operation-failure: ")[1])
        self.assertEqual(
            report["eligibility"]["first"]["stage"], "eligibility.readback"
        )
        self.assertEqual(report["eligibility"]["first"]["reason"], "transport")
        self.assertFalse(report["approval_request_attempted"])
        self.assertNotIn("SECRET", result.summary)

    def test_green_check_and_failed_queries_report_pre_approval_failure(self):
        state = CommandCapacityState(complete=True)
        state.resolved_threads.clear()

        def opener(request, timeout):
            if urlsplit(request.full_url).path == "/graphql":
                return json_response(
                    {
                        "errors": [
                            {"type": "FORBIDDEN", "message": "SECRET-upstream"},
                            {"type": "SECRET-category"},
                        ]
                    }
                )
            return state.open(request, timeout)

        state.http = GitHubHttp(api_url="https://api.github.test", opener=opener)
        with tempfile.TemporaryDirectory() as directory:
            outcome, budget, _ = state.verify(state.record(directory))
        self.assertEqual(state.checks[-1]["conclusion"], "success")
        self.assertFalse(any(r["state"] == "APPROVED" for r in state.reviews))
        self.assertEqual(outcome.approval_progress.approval_state, "not-observed")
        diagnostic = outcome.operation_diagnostics.to_dict()
        self.assertEqual(diagnostic["schema_version"], "1.0")
        self.assertEqual(diagnostic["cleanup"]["first"]["stage"], "cleanup.discovery")
        self.assertEqual(
            diagnostic["approval"]["first"]["stage"], "approval.thread-scan"
        )
        self.assertEqual(diagnostic["cleanup"]["first"]["reason"], "graphql-rejected")
        self.assertEqual(
            diagnostic["approval"]["first"]["graphql_categories"],
            ["FORBIDDEN", "OTHER"],
        )
        self.assertFalse(diagnostic["approval_request_attempted"])
        self.assertEqual(diagnostic["budget_calls"], budget.calls)
        self.assertEqual(diagnostic["call_limit"], 64)
        self.assertEqual(diagnostic["ordinary_call_limit"], 60)
        result = MaintainerCommandResult(
            action="verify",
            applied=True,
            operator_paused=False,
            summary="Decisions retained",
        )
        rendered = render_command_progress(result, outcome)
        self.assertIn("\napproval-operation-failure: ", rendered.summary)
        self.assertNotIn("SECRET", rendered.summary)
        self.assertEqual(
            json.loads(rendered.summary.split("\napproval-progress: ")[1]),
            outcome.approval_progress.to_dict(),
        )
        self.assertTrue(rendered.applied)
        self.assertEqual(rendered.evaluation_failed, result.evaluation_failed)

    def test_original_call_limit_is_reported_without_refill_or_approval_attempt(self):
        from review_sensei_github.publication import ReviewApprovalFinalizer
        from review_sensei_github.publication_recovery import publication_http

        from review_sensei.bounded_evidence import EvidenceReadBudget

        state = CommandCapacityState(complete=True)
        state.resolved_threads.clear()
        budget = EvidenceReadBudget()

        def opener(request, timeout):
            if urlsplit(request.full_url).path == "/graphql":
                # Model already charged admission work in this same original
                # envelope, without changing its ceiling or deadline.
                while budget.calls < 60:
                    budget.consume()
            return state.open(request, timeout)

        state.http = GitHubHttp(api_url="https://api.github.test", opener=opener)
        finalizer = ReviewApprovalFinalizer(
            http=publication_http(state.http, budget), evidence_budget=budget
        )
        with tempfile.TemporaryDirectory() as directory:
            outcome = finalizer.refinalize_with_dispositions(
                token="synthetic",
                check_token="synthetic-check",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha="b" * 40,
                app_slug="reviewsensei[bot]",
                actor="alice",
                finding_ids=(),
                dispositions=state.record(directory),
            )
        self.assertEqual(budget.calls, 60)
        self.assertEqual(state.checks[-1]["conclusion"], "success")
        self.assertFalse(any(r["state"] == "APPROVED" for r in state.reviews))
        diagnostic = outcome.operation_diagnostics.to_dict()
        self.assertEqual(diagnostic["cleanup"]["first"]["reason"], "call-limit")
        self.assertEqual(diagnostic["approval"]["first"]["reason"], "call-limit")
        self.assertFalse(diagnostic["approval_request_attempted"])
        self.assertEqual(diagnostic["budget_calls"], 60)

    def test_first_graphql_failure_survives_later_approval_call_exhaustion(self):
        from review_sensei_github.publication import ReviewApprovalFinalizer
        from review_sensei_github.publication_recovery import publication_http

        from review_sensei.bounded_evidence import EvidenceReadBudget

        state = CommandCapacityState(complete=True)
        state.resolved_threads.clear()
        budget = EvidenceReadBudget()

        def opener(request, timeout):
            if urlsplit(request.full_url).path == "/graphql":
                while budget.calls < 59:
                    budget.consume()
                return json_response(
                    {"errors": [{"type": "RATE_LIMITED", "message": "SECRET-limits"}]}
                )
            return state.open(request, timeout)

        state.http = GitHubHttp(api_url="https://api.github.test", opener=opener)
        finalizer = ReviewApprovalFinalizer(
            http=publication_http(state.http, budget), evidence_budget=budget
        )
        with tempfile.TemporaryDirectory() as directory:
            outcome = finalizer.refinalize_with_dispositions(
                token="synthetic",
                check_token="synthetic-check",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha="b" * 40,
                app_slug="reviewsensei[bot]",
                actor="alice",
                finding_ids=(),
                dispositions=state.record(directory),
            )
        diagnostic = outcome.operation_diagnostics.to_dict()
        self.assertEqual(
            diagnostic["cleanup"]["first"]["graphql_categories"], ["RATE_LIMITED"]
        )
        self.assertEqual(diagnostic["approval"]["first"]["reason"], "call-limit")
        self.assertEqual(budget.calls, 60)
        self.assertFalse(diagnostic["approval_request_attempted"])
        self.assertNotIn("SECRET", json.dumps(diagnostic))

    def test_absent_observation_preserves_existing_constructor_and_rendering(self):
        from review_sensei_github.publication import PublicationResult

        publication = PublicationResult(status="approval_withheld")
        self.assertIsNone(publication.operation_diagnostics)
        command = MaintainerCommandResult(
            action="verify", applied=True, operator_paused=False, summary="Kept"
        )
        self.assertEqual(render_command_progress(command, SimpleNamespace()), command)
