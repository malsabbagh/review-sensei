import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from review_sensei import (
    ReviewRun,
    ReviewService,
    outcome_for_plan,
    plan_review_execution,
)
from review_sensei.cli import main
from review_sensei.errors import ProviderError, ReviewInputError
from review_sensei.hosting.github import (
    GitHubApplication,
    GitHubWriteOptions,
    PublicationResult,
)
from review_sensei.hosting.github.publication import outcome_from_publication
from review_sensei.models import ProviderResponse, ReviewRequest, ReviewResult
from review_sensei.outcomes import (
    RecoveryArtifact,
    ResourceBudget,
    RunOutcome,
    diagnostic_for_recovery_error,
    emit_host_outcome,
    load_recovery_artifact,
    recovery_expires_at,
    sanitize_diagnostic,
)
from review_sensei.providers.transport import parse_retry_after_seconds
from review_sensei.stages import Stage
from review_sensei.workflow import ReviewExecutionPlan

DIFF = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,3 @@
 keep
+return value
 end
"""
CANARY = "ghp_canary_secret_36_ABCDEF"
GIT_SHA = "a" * 40
HEAD_SHA = "b" * 40


class FakeProvider:
    name = "fake"
    model = "fake-model"

    def __init__(self, response_text, *, error=None):
        self.response_text = response_text
        self.error = error
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return ProviderResponse(
            text=self.response_text,
            provider=self.name,
            model=self.model,
        )


class SequenceProvider(FakeProvider):
    def __init__(self, responses):
        super().__init__("")
        self.responses = list(responses)

    def complete(self, request):
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return ProviderResponse(text=item, provider=self.name, model=self.model)


class Clock:
    def __init__(self, value=0.0):
        self.value = value
        self.sleeps = []

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


class RecordingReviewer:
    def __init__(self, results=None):
        self.calls = []
        self.results = list(
            results or [PublicationResult(status="published", review_id=1)]
        )

    def publish(self, **kwargs):
        self.calls.append(kwargs)
        return self.results.pop(0)


class RecordingLearner:
    def __init__(self):
        self.calls = []

    def propose_batch(self, **kwargs):
        self.calls.append(kwargs)
        raise AssertionError("recovery must not write learning pull requests")


class RecordingBroker:
    def request_oidc_token(self):
        return "oidc-token"

    def exchange(self, token, *, capability=None):
        return f"capability-{capability}"


class RunOutcomeWiringTests(unittest.TestCase):
    def test_successful_run_emits_reviewed_outcome(self):
        provider = FakeProvider('{"summary":"Looks good.","comments":[]}')
        run = ReviewService(provider).run(ReviewRequest(diff=DIFF))
        self.assertIsInstance(run, ReviewRun)
        self.assertEqual(run.outcome.status, "reviewed")
        self.assertEqual(run.outcome.provider_calls, 1)
        self.assertEqual(run.outcome.retry_attempts, 0)
        self.assertIsNotNone(run.result)
        self.assertIsNone(run.error)

    def test_skipped_lenses_are_partial(self):
        from review_sensei import ReviewCategory

        category = ReviewCategory(
            id="docs",
            title="Documentation",
            focus=("Documentation accuracy",),
            applies_to=("docs/**",),
        )
        stage = Stage(
            name="Docs review",
            prompt_template="Categories: {review_categories}\nDiff: {diff}",
            outputs=("summary", "comments"),
            categories=(category,),
        )
        provider = FakeProvider('{"summary":"unused","comments":[]}')
        run = ReviewService(provider, stages=[stage]).run(
            ReviewRequest(diff=DIFF, active_category_ids=())
        )
        self.assertEqual(run.outcome.status, "partial")
        self.assertEqual(run.outcome.diagnostic, "partial_coverage")
        self.assertEqual(run.outcome.stage_summary["Docs review"], "skipped")
        self.assertEqual(provider.requests, [])

    def test_provider_call_budget_is_enforced(self):
        provider = FakeProvider('{"summary":"ok","comments":[]}')
        budget = ResourceBudget.create(max_provider_calls=0)
        run = ReviewService(provider).run(ReviewRequest(diff=DIFF), budget=budget)
        self.assertEqual(run.outcome.status, "budget_exhausted")
        self.assertEqual(run.outcome.diagnostic, "provider_call_limit")
        self.assertIsNone(run.result)
        self.assertEqual(provider.requests, [])
        with self.assertRaises(ReviewInputError):
            ReviewService(provider).review(ReviewRequest(diff=DIFF), budget=budget)

    def test_deadline_cancels_before_another_provider_call(self):
        provider = FakeProvider('{"summary":"ok","comments":[]}')
        budget = ResourceBudget.create(timeout_ms=0)
        run = ReviewService(provider).run(ReviewRequest(diff=DIFF), budget=budget)
        self.assertEqual(run.outcome.status, "budget_exhausted")
        self.assertEqual(run.outcome.diagnostic, "deadline_exceeded")
        self.assertEqual(provider.requests, [])

    def test_transport_retry_is_distinct_from_structural_retry(self):
        clock = Clock()
        provider = SequenceProvider(
            [
                ProviderError("rate limited", transient=True, retry_after_seconds=0.25),
                '{"summary":"Recovered.","comments":[]}',
            ]
        )
        run = ReviewService(provider).run(
            ReviewRequest(diff=DIFF),
            monotonic=clock,
            sleeper=clock.sleep,
        )
        self.assertEqual(run.outcome.status, "reviewed")
        self.assertEqual(run.outcome.provider_calls, 2)
        self.assertEqual(run.outcome.retry_attempts, 1)
        self.assertEqual(clock.sleeps, [0.25])
        self.assertIn("Recovered.", run.result.summary)

    def test_structural_retry_does_not_count_as_transport_retry(self):
        provider = SequenceProvider(
            [
                "not-json",
                '{"summary":"Corrected.","comments":[]}',
            ]
        )
        run = ReviewService(provider).run(ReviewRequest(diff=DIFF))
        self.assertEqual(run.outcome.status, "reviewed")
        self.assertEqual(run.outcome.provider_calls, 2)
        self.assertEqual(run.outcome.retry_attempts, 0)

    def test_stage_failure_does_not_publish_earlier_stages(self):
        stages = [
            Stage(name="one", prompt_template="{diff}", outputs=("summary",)),
            Stage(name="two", prompt_template="{diff}", outputs=("summary",)),
        ]
        provider = SequenceProvider(
            [
                '{"summary":"First."}',
                ProviderError(CANARY),
            ]
        )
        run = ReviewService(provider, stages=stages).run(ReviewRequest(diff=DIFF))
        self.assertEqual(run.outcome.status, "provider_failed")
        self.assertEqual(run.outcome.stage_summary["one"], "complete")
        self.assertIsNone(run.result)
        self.assertNotIn(CANARY, json.dumps(run.outcome.to_dict()))
        self.assertIsInstance(run.error, ProviderError)
        self.assertEqual(len(provider.requests), 2)

    def test_outcome_and_summary_redact_canary_secrets(self):
        leaked = RunOutcome(
            "provider_failed",
            diagnostic=CANARY,
            stage_summary={"stage": "failed"},
        )
        self.assertEqual(sanitize_diagnostic(CANARY), "secret_redacted")
        with tempfile.TemporaryDirectory() as temp_dir:
            summary = Path(temp_dir) / "summary.md"
            github_output = Path(temp_dir) / "output.txt"
            outcome_path = Path(temp_dir) / "outcome.json"
            with patch.dict(
                os.environ,
                {
                    "GITHUB_STEP_SUMMARY": str(summary),
                    "GITHUB_OUTPUT": str(github_output),
                },
            ):
                emit_host_outcome(leaked, output_path=outcome_path)
            text = summary.read_text(encoding="utf-8")
            self.assertIn("provider_failed", text)
            self.assertNotIn(CANARY, text)
            self.assertIn("outcome_status=provider_failed", github_output.read_text())
            self.assertNotIn(CANARY, outcome_path.read_text(encoding="utf-8"))

    def test_ineligible_plan_emits_skipped_policy(self):
        plan = plan_review_execution(
            repository="acme/repo",
            repository_id=1,
            pull_request_number=7,
            base_ref="main",
            base_sha=GIT_SHA,
            head_ref="feature",
            head_repository="acme/repo",
            head_sha=HEAD_SHA,
            draft=True,
        )
        outcome = outcome_for_plan(plan)
        self.assertEqual(outcome.status, "skipped_policy")
        self.assertEqual(outcome.diagnostic, "draft_pr")
        eligible = ReviewExecutionPlan(
            repository="acme/repo",
            repository_id=1,
            pull_request_number=7,
            base_ref="main",
            base_sha=GIT_SHA,
            head_ref="feature",
            head_repository="acme/repo",
            head_sha=HEAD_SHA,
        )
        with self.assertRaises(ReviewInputError):
            outcome_for_plan(eligible)

    def test_publication_projection_and_disabled_writes(self):
        published = outcome_from_publication(
            PublicationResult(status="published", review_id=1),
            repository="acme/repo",
            pull_request_number=1,
            base_sha=GIT_SHA,
            head_sha=HEAD_SHA,
        )
        self.assertEqual(published.status, "reviewed")
        skipped = outcome_from_publication(
            PublicationResult(status="skipped_stale_head"),
            head_sha=HEAD_SHA,
        )
        self.assertEqual(skipped.status, "skipped_stale")
        disabled = outcome_from_publication(PublicationResult(status="disabled"))
        self.assertEqual(disabled.status, "skipped_policy")
        self.assertEqual(disabled.diagnostic, "writes_disabled")

    def test_recovery_artifact_round_trip_and_expiry(self):
        result = {
            "summary": "ok",
            "comments": [],
            "provider": "fixture",
            "review_status": "complete",
        }
        artifact = RecoveryArtifact.create(
            repository="acme/repo",
            pull_request_number=1,
            base_sha=GIT_SHA,
            head_sha=HEAD_SHA,
            result=result,
            expires_at=recovery_expires_at(ttl_seconds=60),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "recovery.json"
            path.write_text(json.dumps(artifact.to_dict()), encoding="utf-8")
            loaded = load_recovery_artifact(path)
            loaded.validate(
                repository="acme/repo",
                pull_request_number=1,
                base_sha=GIT_SHA,
                head_sha=HEAD_SHA,
            )
            missing = diagnostic_for_recovery_error(
                ReviewInputError("recovery artifact is missing")
            )
            self.assertEqual(missing, "recovery_artifact_missing")
        expired = RecoveryArtifact.create(
            repository="acme/repo",
            pull_request_number=1,
            base_sha=GIT_SHA,
            head_sha=HEAD_SHA,
            result=result,
            created_at="2026-01-01T00:00:00+00:00",
            expires_at="2026-01-01T01:00:00+00:00",
        )
        with self.assertRaises(ReviewInputError):
            expired.validate(
                repository="acme/repo",
                pull_request_number=1,
                base_sha=GIT_SHA,
                head_sha=HEAD_SHA,
                now=datetime(2026, 1, 2, tzinfo=timezone.utc),
            )

    def test_publication_recovery_invokes_no_model_and_skips_learnings(self):
        result = ReviewResult(summary="ok", comments=(), provider="fixture")
        artifact = RecoveryArtifact.create(
            repository="acme/repo",
            pull_request_number=1,
            base_sha=GIT_SHA,
            head_sha=HEAD_SHA,
            result={**result.to_dict(), "review_status": "complete"},
            expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        )
        reviewer = RecordingReviewer(
            [PublicationResult(status="published", review_id=9)]
        )
        learner = RecordingLearner()
        application = GitHubApplication(
            broker=RecordingBroker(),
            http=None,
            reviewer=reviewer,
            learner=learner,
            replier=None,
        )
        outcome = application.recover_review(
            options=GitHubWriteOptions(github_writes=True, auto_review=True),
            oidc_token="oidc",
            repository="acme/repo",
            repository_id=1,
            pull_request=1,
            head_sha=HEAD_SHA,
            base_branch="main",
            base_sha=GIT_SHA,
            artifact=artifact,
            diff=DIFF,
            app_slug="reviewsensei[bot]",
        )
        self.assertEqual(outcome.status, "published")
        self.assertEqual(learner.calls, [])
        self.assertEqual(len(reviewer.calls), 1)

        reviewer.results = [PublicationResult(status="already_published")]
        second = application.recover_review(
            options=GitHubWriteOptions(github_writes=True, auto_review=True),
            oidc_token="oidc",
            repository="acme/repo",
            repository_id=1,
            pull_request=1,
            head_sha=HEAD_SHA,
            base_branch="main",
            base_sha=GIT_SHA,
            artifact=artifact,
            diff=DIFF,
            app_slug="reviewsensei[bot]",
        )
        self.assertEqual(second.status, "already_published")

    def test_retry_after_parser_bounds_numeric_hints(self):
        class HeaderError:
            def __init__(self, value):
                self.headers = {"Retry-After": value}

        self.assertEqual(parse_retry_after_seconds(HeaderError("5")), 5.0)
        self.assertEqual(parse_retry_after_seconds(HeaderError("120")), 60.0)
        self.assertIsNone(parse_retry_after_seconds(HeaderError("Wed, 01 Jan 2026")))
        self.assertIsNone(parse_retry_after_seconds(HeaderError("-1")))

    def test_cli_review_writes_outcome_without_echoing_canary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            diff_path = root / "diff.patch"
            outcome_path = root / "outcome.json"
            result_path = root / "result.json"
            fixture = root / "fixture.json"
            diff_path.write_text(DIFF, encoding="utf-8")
            fixture.write_text(
                '{"summary":"Looks good.","comments":[]}', encoding="utf-8"
            )
            status = main(
                [
                    "--provider",
                    "fixture",
                    "--fixture-response",
                    str(fixture),
                    "--diff",
                    str(diff_path),
                    "--output",
                    str(result_path),
                    "--outcome",
                    str(outcome_path),
                    "--repository",
                    "acme/repo",
                    "--pull-request",
                    "1",
                    "--base-sha",
                    GIT_SHA,
                    "--head-sha",
                    HEAD_SHA,
                ]
            )
            self.assertEqual(status, 0)
            payload = json.loads(outcome_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "reviewed")
            self.assertNotIn(CANARY, outcome_path.read_text(encoding="utf-8"))

    def test_cli_recovery_refuses_learning_writes_and_missing_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            diff_path = root / "diff.patch"
            outcome_path = root / "outcome.json"
            diff_path.write_text(DIFF, encoding="utf-8")
            status = main(
                [
                    "github",
                    "review",
                    "--diff",
                    str(diff_path),
                    "--repository",
                    "acme/repo",
                    "--repository-id",
                    "1",
                    "--pull-request",
                    "1",
                    "--head-sha",
                    HEAD_SHA,
                    "--base-branch",
                    "main",
                    "--base-sha",
                    GIT_SHA,
                    "--allow-write",
                    "--enable-review",
                    "--recover-from",
                    str(root / "missing.json"),
                    "--outcome",
                    str(outcome_path),
                ]
            )
            self.assertEqual(status, 1)
            payload = json.loads(outcome_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "publication_failed")
            self.assertEqual(payload["diagnostic"], "recovery_artifact_missing")


if __name__ == "__main__":
    unittest.main()
