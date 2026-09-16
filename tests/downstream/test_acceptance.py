from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))

from packaging_guard import assert_distribution_import  # noqa: E402

assert_distribution_import()

from fake_github_http import make_http  # noqa: E402

from review_sensei.cli import main  # noqa: E402
from review_sensei.concurrency import ReviewConcurrencyPlan  # noqa: E402
from review_sensei.hosting.github import (  # noqa: E402
    GitHubApplication,
    GitHubWriteOptions,
    SetupPlanBuilder,
)
from review_sensei.hosting.github.conversation import ReplyResult  # noqa: E402
from review_sensei.hosting.github.learning_pr import LearningPRResult  # noqa: E402
from review_sensei.hosting.github.publication import PublicationResult  # noqa: E402
from review_sensei.hosting.github.setup import (  # noqa: E402
    CONFIG_PATH,
    UNINSTALL_WORKFLOW_PATH,
    WORKFLOW_PATH,
)
from review_sensei.models import (  # noqa: E402
    ProviderResponse,
    ReviewRequest,
    ReviewResult,
)
from review_sensei.service import ReviewService  # noqa: E402
from review_sensei.workflow import plan_review_execution  # noqa: E402

DIFF = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1,2 @@
 keep
+change
"""

HEAD_SHA = "b" * 40
BASE_SHA = "a" * 40


class FakeProvider:
    name = "fake"
    model = "fake-model"

    def __init__(
        self,
        response_text='{"summary":"Architecture reviewed.","comments":[]}',
    ):
        self.requests = []
        self.response_text = response_text

    def complete(self, request):
        self.requests.append(request)
        return ProviderResponse(
            text=self.response_text,
            provider=self.name,
            model=self.model,
        )


class RecordingBroker:
    def __init__(self):
        self.requested = 0
        self.exchanges = []

    def request_oidc_token(self):
        self.requested += 1
        return "oidc-token"

    def exchange(self, token, *, capability=None):
        self.exchanges.append((token, capability))
        return f"capability-{capability}"


class RecordingReviewer:
    def __init__(self):
        self.calls = []

    def publish(self, **kwargs):
        self.calls.append(kwargs)
        return PublicationResult(status="published", review_id=1)


class RecordingLearner:
    def __init__(self):
        self.calls = []

    def propose(self, **kwargs):
        self.calls.append(kwargs)
        return LearningPRResult(status="created")

    def propose_batch(self, **kwargs):
        self.calls.append(kwargs)
        return LearningPRResult(status="created")


class RecordingReplier:
    def __init__(self):
        self.calls = []
        self.prepare_calls = []
        self.reaction_calls = []

    def add_processing_reaction(self, **kwargs):
        self.reaction_calls.append(("add", kwargs))
        return SimpleNamespace(reaction_id=99)

    def remove_processing_reaction(self, **kwargs):
        self.reaction_calls.append(("remove", kwargs))

    def publish(self, **kwargs):
        self.calls.append(kwargs)
        return ReplyResult(status="replied", comment_id=2)

    def prepare_context(self, **kwargs):
        self.prepare_calls.append(kwargs)
        raise AssertionError("prepare_context must not run when writes are disabled")


def _generated_files() -> dict[str, str]:
    plan = SetupPlanBuilder().build("owner/repo")
    return {file.path: file.content for file in plan.files}


def _make_application(http=None):
    return GitHubApplication(
        broker=RecordingBroker(),
        http=http,
        reviewer=RecordingReviewer(),
        learner=RecordingLearner(),
        replier=RecordingReplier(),
    ), http


class GeneratedCallerContractTests(unittest.TestCase):
    def test_setup_smoke_generates_v4_caller_config_and_uninstall(self) -> None:
        files = _generated_files()
        workflow = files[WORKFLOW_PATH]
        config = files[CONFIG_PATH]
        uninstall = files[UNINSTALL_WORKFLOW_PATH]
        self.assertIn("# ReviewSensei setup version: 4", workflow)
        self.assertIn("review-sensei-run.yml@v4", workflow)
        self.assertIn("setup_version: 4", config)
        self.assertIn("auto_review: false", config)
        self.assertIn("github_writes: false", config)
        self.assertIn("mention_replies: false", config)
        self.assertIn("learning_prs: false", config)
        self.assertIn("Remove ReviewSensei setup", uninstall)
        self.assertIn(".github/review-sensei/config.yml", uninstall)
        self.assertNotIn("OLLAMA_API_KEY", uninstall)
        self.assertIn("learnings and secrets remain untouched", uninstall)

    def test_manual_and_automatic_review_paths_share_the_runner_contract(self) -> None:
        workflow = _generated_files()[WORKFLOW_PATH]
        self.assertIn(
            "mode: ${{ github.event_name == 'pull_request' && 'automatic' || 'manual' }}",
            workflow,
        )
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("options: [review]", workflow)
        self.assertIn(
            "enable_github_writes: ${{ vars.REVIEWSENSEI_GITHUB_WRITES }}",
            workflow,
        )
        self.assertIn(
            "provider_mode: ${{ vars.REVIEWSENSEI_PROVIDER_MODE || 'local' }}",
            workflow,
        )
        self.assertIn("resolve-trigger:", workflow)
        self.assertIn(
            "operation: ${{ needs.resolve-trigger.outputs.operation }}",
            workflow,
        )
        self.assertIn(
            "enable_review: ${{ needs.resolve-trigger.outputs.enable_review == 'true' && 'true' || 'false' }}",
            workflow,
        )
        self.assertIn('operation = "reply"', workflow)
        self.assertIn("@sensei", workflow)
        self.assertIn("vars.REVIEWSENSEI_MENTION_REPLIES == 'true'", workflow)
        self.assertIn("vars.REVIEWSENSEI_GITHUB_WRITES == 'true'", workflow)
        self.assertIn("github.event_name == 'pull_request'", workflow)
        self.assertIn("github.event_name == 'workflow_dispatch'", workflow)

    def test_cloud_and_local_configuration_parity(self) -> None:
        files = _generated_files()
        workflow = files[WORKFLOW_PATH]
        config = files[CONFIG_PATH]
        self.assertIn(
            "provider_mode: ${{ vars.REVIEWSENSEI_PROVIDER_MODE || 'local' }}",
            workflow,
        )
        self.assertNotIn("vars.REVIEWSENSEI_PROVIDER_MODE != 'cloud'", workflow)
        self.assertNotIn("vars.REVIEWSENSEI_PROVIDER_MODE == 'cloud'", workflow)
        self.assertIn("provider_mode: local", config)
        self.assertIn("cloud_base_url: https://ollama.com/api", config)
        self.assertIn("base_url: http://127.0.0.1:11434/api", config)
        self.assertIn("enable_learning_proposals:", workflow)
        self.assertIn("enable_mention_replies:", workflow)
        self.assertIn("upload_artifacts:", workflow)

    def test_uninstall_is_upgrade_safe_and_does_not_wipe_operator_state(self) -> None:
        uninstall = _generated_files()[UNINSTALL_WORKFLOW_PATH]
        self.assertIn('"git", "rm", "--force", "--ignore-unmatch"', uninstall)
        self.assertIn(".github/workflows/review-sensei-review.yml", uninstall)
        self.assertIn(".github/workflows/review-sensei-uninstall.yml", uninstall)
        self.assertNotIn("delete_repo", uninstall)
        self.assertNotIn("gh secret", uninstall)


class EngineAndPublicationAcceptanceTests(unittest.TestCase):
    def test_review_path_uses_fake_provider_without_live_credentials(self) -> None:
        provider = FakeProvider()
        with patch.dict(os.environ, {"OLLAMA_API_KEY": "must-not-be-read"}):
            result = ReviewService(provider).review(ReviewRequest(diff=DIFF))
        self.assertEqual(len(provider.requests), 1)
        self.assertTrue(result.summary)
        self.assertEqual(result.provider, "fake")

    def test_writes_disabled_make_zero_github_calls(self) -> None:
        http, calls = make_http([])
        application, _ = _make_application(http)
        application.http = http
        options = GitHubWriteOptions()
        review = application.publish_review(
            options=options,
            oidc_token=None,
            repository="owner/repo",
            repository_id=1,
            pull_request=7,
            head_sha=HEAD_SHA,
            result=ReviewResult(summary="ok", comments=(), provider="fake"),
            diff=DIFF,
            app_slug="reviewsensei[bot]",
        )
        learning = application.publish_learning_proposals(
            options=options,
            oidc_token=None,
            repository="owner/repo",
            repository_id=1,
            pull_request=7,
            head_sha=HEAD_SHA,
            base_branch="main",
            base_sha=BASE_SHA,
            result=ReviewResult(
                summary="ok",
                comments=(),
                provider="fake",
                learning_proposals=(),
            ),
        )
        reply = application.generate_and_publish_reply(
            options=options,
            oidc_token=None,
            read_token="read-token",
            repository="owner/repo",
            pull_request=7,
            source_comment_id=9,
            source_updated_at="2026-09-16T00:00:00Z",
            expected_head_sha=HEAD_SHA,
            reply_provider=FakeProvider(),
            model="fake-model",
            app_slug="reviewsensei[bot]",
            root_comment_id=9,
            source_kind="issue",
        )
        self.assertEqual(review.status, "disabled")
        self.assertEqual(learning, ())
        self.assertEqual(reply.status, "disabled")
        self.assertEqual(application.broker.requested, 0)
        self.assertEqual(application.broker.exchanges, [])
        self.assertEqual(application.reviewer.calls, [])
        self.assertEqual(application.learner.calls, [])
        self.assertEqual(application.replier.calls, [])
        self.assertEqual(application.replier.prepare_calls, [])
        self.assertEqual(calls, [])

    def test_stale_and_ineligible_inputs_skip_without_provider_calls(self) -> None:
        provider = FakeProvider()
        closed = plan_review_execution(
            repository="owner/repo",
            repository_id=42,
            pull_request_number=7,
            base_ref="main",
            base_sha=BASE_SHA,
            head_ref="feature",
            head_repository="owner/repo",
            head_sha=HEAD_SHA,
            state="closed",
        )
        draft = plan_review_execution(
            repository="owner/repo",
            repository_id=42,
            pull_request_number=7,
            base_ref="main",
            base_sha=BASE_SHA,
            head_ref="feature",
            head_repository="owner/repo",
            head_sha=HEAD_SHA,
            draft=True,
        )
        fork = plan_review_execution(
            repository="owner/repo",
            repository_id=42,
            pull_request_number=7,
            base_ref="main",
            base_sha=BASE_SHA,
            head_ref="feature",
            head_repository="fork/repo",
            head_sha=HEAD_SHA,
        )
        self.assertFalse(closed.eligible)
        self.assertEqual(closed.skip_reason, "pr_not_open")
        self.assertEqual(draft.skip_reason, "draft_pr")
        self.assertEqual(fork.skip_reason, "fork_not_allowed")
        self.assertEqual(provider.requests, [])

    def test_review_cancellation_is_latest_wins_and_reply_is_isolated(self) -> None:
        review = ReviewConcurrencyPlan.for_pull_request("owner/repo", 7)
        skipped = ReviewConcurrencyPlan.for_non_review_trigger("owner/repo", "skip-42")
        self.assertTrue(review.workflow.cancel_in_progress)
        self.assertEqual(review.workflow.max_active, 1)
        self.assertFalse(review.provider.cancel_in_progress)
        self.assertNotEqual(review.workflow_key, skipped.workflow_key)
        self.assertIsNone(skipped.provider)

    def test_negative_skip_and_disabled_writes_assert_zero_model_and_github_calls(
        self,
    ) -> None:
        provider = FakeProvider()
        http, calls = make_http([RuntimeError("GitHub must not be contacted")])
        application, _ = _make_application(http)
        application.http = http
        plan = plan_review_execution(
            repository="owner/repo",
            repository_id=42,
            pull_request_number=7,
            base_ref="main",
            base_sha=BASE_SHA,
            head_ref="feature",
            head_repository="owner/repo",
            head_sha=HEAD_SHA,
            state="closed",
        )
        self.assertFalse(plan.eligible)
        self.assertEqual(plan.skip_reason, "pr_not_open")
        # The plan gates the provider call, so a closed pull request must reach
        # publication without any model request having been made.
        self.assertEqual(provider.requests, [])
        outcome = application.publish_review(
            options=GitHubWriteOptions(auto_review=True),
            oidc_token=None,
            repository=plan.repository,
            repository_id=plan.repository_id,
            pull_request=plan.pull_request_number,
            head_sha=plan.head_sha,
            result=ReviewResult(summary="unused", comments=(), provider="fake"),
            diff=DIFF,
            app_slug="reviewsensei[bot]",
        )
        self.assertEqual(outcome.status, "disabled")
        self.assertEqual(provider.requests, [])
        self.assertEqual(application.broker.exchanges, [])
        self.assertEqual(application.reviewer.calls, [])
        self.assertEqual(calls, [])

    def test_github_cli_without_allow_write_does_not_construct_publishers(self) -> None:
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as temp_dir:
            result_path = Path(temp_dir) / "result.json"
            diff_path = Path(temp_dir) / "diff.patch"
            result_path.write_text(
                json.dumps(
                    {
                        "summary": "Summary.",
                        "comments": [],
                        "provider": "fixture",
                        "model": "fixture-model",
                    }
                ),
                encoding="utf-8",
            )
            diff_path.write_text(DIFF, encoding="utf-8")
            with (
                patch("sys.stderr", stderr),
                patch(
                    "review_sensei.cli.default_registry",
                    side_effect=AssertionError("provider must not be created"),
                ),
            ):
                status = main(
                    [
                        "github",
                        "review",
                        "--result",
                        str(result_path),
                        "--diff",
                        str(diff_path),
                        "--repository",
                        "owner/repo",
                        "--repository-id",
                        "1",
                        "--pull-request",
                        "7",
                        "--head-sha",
                        HEAD_SHA,
                    ]
                )
        self.assertEqual(status, 1)
        self.assertIn("--allow-write", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
