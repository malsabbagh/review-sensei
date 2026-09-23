import importlib.metadata
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from review_sensei import ProviderResponse
from review_sensei.cli import (
    _cli_option_set,
    _doctor_parser,
    _explicit_cli_options,
    _github_parser,
    _learnings_parser,
    _parser,
    _plan_parser,
    main,
)
from review_sensei.errors import ReviewInputError

DIFF = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1,2 @@
 keep
+change
"""


class FakeProvider:
    name = "fake"
    model = "fake-model"

    def __init__(self):
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        return ProviderResponse(
            text='{"summary":"Architecture reviewed."}',
            provider=self.name,
            model=self.model,
        )


class SymbolContextProvider(FakeProvider):
    def complete(self, request):
        self.requests.append(request)
        return ProviderResponse(
            text=(
                '{"summary":"Architecture reviewed.","comments":[],'
                '"learning_proposals":[]}'
            ),
            provider=self.name,
            model=self.model,
        )


class FakeRegistry:
    def __init__(self, provider):
        self.provider = provider

    def create(self, settings):
        return self.provider


class CliTests(unittest.TestCase):
    def test_github_review_cli_wires_review_and_learning_publication(self):
        from review_sensei.hosting import github as github_module

        class FakeApplication:
            instances = []

            def __init__(self, **kwargs):
                self.calls = []
                self.__class__.instances.append(self)

            def publish_review(self, **kwargs):
                self.calls.append(("review", kwargs))
                return SimpleNamespace(status="published")

            def publish_learning_proposals(self, **kwargs):
                self.calls.append(("learning", kwargs))
                return (SimpleNamespace(status="created"),)

        class FakeRegistry:
            def create(self, settings):
                return FakeProvider()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            result_path = root / "result.json"
            diff_path = root / "diff.patch"
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
            with patch.multiple(
                github_module,
                BrokerClient=lambda: object(),
                GitHubHttp=lambda: object(),
                ReviewPublisher=lambda **kwargs: object(),
                LearningPRPublisher=lambda **kwargs: object(),
                ConversationPublisher=lambda **kwargs: object(),
                GitHubApplication=FakeApplication,
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
                        "2",
                        "--head-sha",
                        "a" * 40,
                        "--base-branch",
                        "main",
                        "--base-sha",
                        "b" * 40,
                        "--allow-write",
                        "--enable-review",
                        "--enable-learning-prs",
                    ]
                )

        self.assertEqual(status, 0)
        self.assertEqual(
            [kind for kind, _ in FakeApplication.instances[0].calls],
            ["review", "learning"],
        )
        review_call = FakeApplication.instances[0].calls[0][1]
        self.assertEqual(review_call["base_branch"], "main")
        self.assertEqual(review_call["base_sha"], "b" * 40)
        self.assertTrue(review_call["options"].auto_approve)

    def test_github_parser_uses_the_installed_app_bot_slug(self):
        args = _github_parser().parse_args(
            [
                "review",
                "--result",
                "result.json",
                "--diff",
                "diff.patch",
                "--repository",
                "owner/repo",
                "--repository-id",
                "1",
                "--pull-request",
                "2",
                "--head-sha",
                "a" * 40,
            ]
        )
        self.assertEqual(args.app_slug, "reviewsensei[bot]")
        self.assertTrue(args.enable_auto_approve)

    def test_github_parser_accepts_review_mode(self):
        args = _github_parser().parse_args(
            [
                "review",
                "--result",
                "result.json",
                "--diff",
                "diff.patch",
                "--repository",
                "owner/repo",
                "--repository-id",
                "1",
                "--pull-request",
                "2",
                "--head-sha",
                "a" * 40,
                "--review-mode",
                "merge-focused",
            ]
        )
        self.assertEqual(args.review_mode, "merge-focused")

    def test_github_reply_parser_omits_review_mode(self):
        args = _github_parser().parse_args(
            [
                "reply",
                "--repository",
                "owner/repo",
                "--pull-request",
                "2",
                "--source-comment-id",
                "3",
                "--source-updated-at",
                "2026-09-19T00:00:00Z",
                "--allow-write",
                "--enable-reply",
            ]
        )
        self.assertFalse(hasattr(args, "review_mode"))
        self.assertIsNone(getattr(args, "review_mode", None))

    def test_github_recover_ignores_env_review_mode(self):
        from datetime import datetime, timedelta, timezone

        from review_sensei.convergence import REVIEW_MODE_ENV
        from review_sensei.hosting import github as github_module
        from review_sensei.models import ReviewResult
        from review_sensei.outcomes import RecoveryArtifact

        class FakeApplication:
            instance = None

            def __init__(self, **kwargs):
                self.calls = []
                FakeApplication.instance = self

            def recover_review(self, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(status="published")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            diff_path = root / "diff.patch"
            artifact_path = root / "recovery.json"
            diff_path.write_text(DIFF, encoding="utf-8")
            artifact = RecoveryArtifact.create(
                repository="owner/repo",
                pull_request_number=2,
                base_sha="b" * 40,
                head_sha="a" * 40,
                result=ReviewResult(
                    summary="Summary.",
                    comments=(),
                    provider="fixture",
                    review_status="complete",
                ).to_dict(),
                expires_at=(
                    datetime.now(timezone.utc) + timedelta(hours=1)
                ).isoformat(),
            )
            artifact_path.write_text(json.dumps(artifact.to_dict()), encoding="utf-8")
            with patch.dict("os.environ", {REVIEW_MODE_ENV: "merge-focused"}):
                with patch.multiple(
                    github_module,
                    BrokerClient=lambda: object(),
                    GitHubHttp=lambda: object(),
                    ReviewPublisher=lambda **kwargs: object(),
                    LearningPRPublisher=lambda **kwargs: object(),
                    ConversationPublisher=lambda **kwargs: object(),
                    GitHubApplication=FakeApplication,
                ):
                    status = main(
                        [
                            "github",
                            "review",
                            "--diff",
                            str(diff_path),
                            "--repository",
                            "owner/repo",
                            "--repository-id",
                            "1",
                            "--pull-request",
                            "2",
                            "--head-sha",
                            "a" * 40,
                            "--base-branch",
                            "main",
                            "--base-sha",
                            "b" * 40,
                            "--allow-write",
                            "--enable-review",
                            "--recover-from",
                            str(artifact_path),
                        ]
                    )

        self.assertEqual(status, 0)
        policy = FakeApplication.instance.calls[0]["convergence_policy"]
        self.assertEqual(policy.mode, "merge-focused")
        self.assertEqual(policy.enforcement, "publication")

    def test_github_review_cli_refuses_ambient_retired_mode_before_any_request(self):
        # The retired-mode guard is a user-visible contract at the CLI
        # boundary: the run must stop with the migration instruction and exit
        # non-zero before any GitHub request, not fail inside the publication
        # path after work has been done.
        from review_sensei.convergence import REVIEW_MODE_ENV
        from review_sensei.hosting import github as github_module

        requests = []

        class RecordingHttp:
            def __init__(self, *args, **kwargs):
                pass

            def request(self, *args, **kwargs):
                requests.append((args, kwargs))
                raise AssertionError("HTTP request issued before mode resolution")

        class FakeApplication:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            result_path = root / "result.json"
            diff_path = root / "diff.patch"
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
            stderr = io.StringIO()
            with patch.multiple(
                github_module,
                BrokerClient=lambda: object(),
                GitHubHttp=RecordingHttp,
                ReviewPublisher=lambda **kwargs: object(),
                LearningPRPublisher=lambda **kwargs: object(),
                ConversationPublisher=lambda **kwargs: object(),
                GitHubApplication=FakeApplication,
            ):
                with patch.dict("os.environ", {REVIEW_MODE_ENV: "legacy"}):
                    with redirect_stderr(stderr):
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
                                "2",
                                "--head-sha",
                                "a" * 40,
                                "--base-branch",
                                "main",
                                "--base-sha",
                                "b" * 40,
                                "--allow-write",
                                "--enable-review",
                            ]
                        )

        self.assertEqual(status, 1)
        self.assertEqual(
            stderr.getvalue(),
            "review-sensei: legacy review mode is retired; "
            "migrate configuration to merge-focused\n",
        )
        self.assertEqual(requests, [])

    def test_github_reply_cli_is_exempt_from_the_retired_mode_guard(self):
        from review_sensei.convergence import REVIEW_MODE_ENV
        from review_sensei.hosting import github as github_module

        class FakeApplication:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.calls = []
                self.__class__.instance = self

            def publish_reply(self, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(status="replied")

        with tempfile.TemporaryDirectory() as temp_dir:
            reply_path = Path(temp_dir) / "reply.json"
            reply_path.write_text('{"body":"Thanks."}', encoding="utf-8")
            with patch.multiple(
                github_module,
                BrokerClient=lambda: object(),
                GitHubHttp=lambda: object(),
                ReviewPublisher=lambda **kwargs: object(),
                LearningPRPublisher=lambda **kwargs: object(),
                ConversationPublisher=lambda **kwargs: object(),
                GitHubApplication=FakeApplication,
            ):
                with patch.dict("os.environ", {REVIEW_MODE_ENV: "legacy"}):
                    status = main(
                        [
                            "github",
                            "reply",
                            "--reply",
                            str(reply_path),
                            "--repository",
                            "owner/repo",
                            "--pull-request",
                            "2",
                            "--source-comment-id",
                            "10",
                            "--source-updated-at",
                            "2026-08-19T00:00:00Z",
                            "--head-sha",
                            "a" * 40,
                            "--allow-write",
                            "--enable-reply",
                        ]
                    )

        self.assertEqual(status, 0)
        (reply_call,) = FakeApplication.instance.calls
        self.assertEqual(reply_call["reply"].body, "Thanks.")
        # The reply path resolves no review mode, so an ambient retired value
        # can neither fail nor downgrade the reply.
        self.assertNotIn("convergence_policy", reply_call)

    def test_github_parser_allows_disabling_default_auto_approval(self):
        args = _github_parser().parse_args(
            [
                "review",
                "--result",
                "result.json",
                "--diff",
                "diff.patch",
                "--repository",
                "owner/repo",
                "--repository-id",
                "1",
                "--pull-request",
                "2",
                "--head-sha",
                "a" * 40,
                "--no-auto-approve",
            ]
        )
        self.assertFalse(args.enable_auto_approve)

    def test_github_generated_reply_cli_reads_named_token_and_publishes(self):
        from review_sensei.hosting import github as github_module

        class FakeApplication:
            def __init__(self, **kwargs):
                self.calls = []
                self.__class__.instance = self

            def generate_and_publish_reply(self, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(status="replied")

        class FakeRegistry:
            def create(self, settings):
                self.settings = settings
                return FakeProvider()

        registry = FakeRegistry()
        with patch.multiple(
            github_module,
            BrokerClient=lambda: object(),
            GitHubHttp=lambda: object(),
            ReviewPublisher=lambda **kwargs: object(),
            LearningPRPublisher=lambda **kwargs: object(),
            ConversationPublisher=lambda **kwargs: object(),
            GitHubApplication=FakeApplication,
        ):
            with patch.dict(
                "os.environ",
                {"REVIEW_SENSEI_READ_TOKEN": "read-token"},
                clear=True,
            ):
                with patch("review_sensei.cli.default_registry", return_value=registry):
                    status = main(
                        [
                            "github",
                            "reply",
                            "--generate",
                            "--repository",
                            "owner/repo",
                            "--pull-request",
                            "2",
                            "--source-comment-id",
                            "10",
                            "--source-updated-at",
                            "2026-08-19T00:00:00Z",
                            "--source-kind",
                            "issue",
                            "--github-token-env",
                            "REVIEW_SENSEI_READ_TOKEN",
                            "--allow-write",
                            "--enable-reply",
                        ]
                    )

        self.assertEqual(status, 0)
        self.assertEqual(FakeApplication.instance.calls[0]["read_token"], "read-token")
        self.assertEqual(registry.settings.api_key, None)

    def test_github_legacy_reply_cli_reads_reply_file(self):
        from review_sensei.hosting import github as github_module

        class FakeApplication:
            def __init__(self, **kwargs):
                self.__class__.instance = self

            def publish_reply(self, **kwargs):
                self.call = kwargs
                return SimpleNamespace(status="replied")

        with tempfile.TemporaryDirectory() as temp_dir:
            reply_path = Path(temp_dir) / "reply.json"
            reply_path.write_text('{"body":"Thanks."}', encoding="utf-8")
            with patch.multiple(
                github_module,
                BrokerClient=lambda: object(),
                GitHubHttp=lambda: object(),
                ReviewPublisher=lambda **kwargs: object(),
                LearningPRPublisher=lambda **kwargs: object(),
                ConversationPublisher=lambda **kwargs: object(),
                GitHubApplication=FakeApplication,
            ):
                status = main(
                    [
                        "github",
                        "reply",
                        "--reply",
                        str(reply_path),
                        "--repository",
                        "owner/repo",
                        "--pull-request",
                        "2",
                        "--source-comment-id",
                        "10",
                        "--source-updated-at",
                        "2026-08-19T00:00:00Z",
                        "--head-sha",
                        "a" * 40,
                        "--allow-write",
                        "--enable-reply",
                    ]
                )

        self.assertEqual(status, 0)
        self.assertEqual(FakeApplication.instance.call["head_sha"], "a" * 40)

    def test_provider_mode_selects_mode_specific_defaults(self):
        with patch.dict(
            "os.environ",
            {"REVIEWSENSEI_PROVIDER_MODE": "cloud", "OLLAMA_API_KEY": "secret"},
            clear=True,
        ):
            cloud = _parser().parse_args([])
        self.assertEqual(cloud.base_url, "https://ollama.com/api")
        self.assertEqual(cloud.model, "deepseek-v4.1-flash:cloud")

        with patch.dict(
            "os.environ", {"REVIEWSENSEI_PROVIDER_MODE": "local"}, clear=True
        ):
            local = _parser().parse_args([])
        self.assertEqual(local.base_url, "http://127.0.0.1:11434/api")
        self.assertEqual(local.model, "qwen3.5:4b")

    def test_provider_defaults_follow_openai_compatible_adapter(self):
        args = _parser().parse_args(["--provider", "openai-compatible"])
        self.assertEqual(args.base_url, "https://api.openai.com/v1")
        self.assertEqual(args.model, "gpt-4o-mini")
        self.assertEqual(args.api_key_env, "OPENAI_API_KEY")
        self.assertEqual(args.timeout_seconds, 120.0)

    def test_provider_defaults_follow_fixture_adapter(self):
        args = _parser().parse_args(["--provider", "fixture"])
        self.assertEqual(args.model, "fixture-v1")
        self.assertIsNone(args.api_key_env)

    def test_cli_option_set_detects_space_separated_values(self):
        self.assertTrue(_cli_option_set(["--model", "gpt-4o"], "--model"))
        self.assertFalse(
            _cli_option_set(["--provider", "openai-compatible"], "--model")
        )
        self.assertTrue(_cli_option_set(["--model=gpt-4o"], "--model"))

    def test_openai_timeout_rejects_invalid_environment_value(self):
        with patch.dict("os.environ", {"OPENAI_TIMEOUT_SECONDS": "abc"}, clear=True):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                status = main(
                    [
                        "--diff",
                        "review.patch",
                        "--provider",
                        "openai-compatible",
                    ]
                )
        self.assertEqual(status, 1)
        self.assertIn(
            "OPENAI_TIMEOUT_SECONDS must be a positive number", stderr.getvalue()
        )

    def test_openai_timeout_prefers_reviewsensei_environment_variable(self):
        with patch.dict(
            "os.environ",
            {
                "REVIEWSENSEI_OPENAI_TIMEOUT_SECONDS": "45",
                "OPENAI_TIMEOUT_SECONDS": "9",
            },
            clear=True,
        ):
            args = _parser().parse_args(["--provider", "openai-compatible"])
        self.assertEqual(args.timeout_seconds, 45.0)

    def test_github_reply_rejects_invalid_openai_timeout_environment_value(self):
        with patch.dict("os.environ", {"OPENAI_TIMEOUT_SECONDS": "abc"}, clear=True):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                status = main(
                    [
                        "github",
                        "reply",
                        "--generate",
                        "--repository",
                        "owner/repo",
                        "--pull-request",
                        "1",
                        "--source-comment-id",
                        "2",
                        "--source-updated-at",
                        "2026-01-01T00:00:00Z",
                        "--provider",
                        "openai-compatible",
                    ]
                )
        self.assertEqual(status, 1)
        self.assertIn(
            "OPENAI_TIMEOUT_SECONDS must be a positive number", stderr.getvalue()
        )

    def test_cli_option_set_ignores_empty_space_separated_values(self):
        self.assertFalse(_cli_option_set(["--model", ""], "--model"))
        self.assertFalse(_cli_option_set(["--model"], "--model"))

    def test_cli_option_set_detects_equals_form_and_rejects_empty_values(self):
        self.assertTrue(
            _cli_option_set(["--api-key-env=OPENAI_API_KEY"], "--api-key-env")
        )
        self.assertFalse(_cli_option_set(["--api-key-env="], "--api-key-env"))

    def test_explicit_cli_options_treat_unknown_hyphen_values_as_set(self):
        argv = ["--model", "--looks-like-flag"]
        explicit = _explicit_cli_options(_parser(), argv)
        self.assertIn("--model", explicit)
        equals_argv = ["--model=--looks-like-flag"]
        self.assertEqual(_explicit_cli_options(_parser(), equals_argv), {"--model"})
        args = _parser().parse_args(equals_argv)
        self.assertEqual(args.model, "--looks-like-flag")

    def test_explicit_cli_options_ignore_option_tokens_without_values(self):
        argv = ["--model", "--provider", "ollama"]
        explicit = _explicit_cli_options(_parser(), argv)
        self.assertNotIn("--model", explicit)
        self.assertNotIn("--provider", explicit)

    def test_explicit_cli_options_ignore_chained_option_tokens(self):
        argv = ["--provider", "--model", "ollama"]
        explicit = _explicit_cli_options(_parser(), argv)
        self.assertNotIn("--provider", explicit)
        self.assertNotIn("--model", explicit)

    def test_profile_missing_api_key_env_is_rejected_at_cli_boundary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            diff_path = Path(temp_dir) / "review.patch"
            diff_path.write_text(DIFF, encoding="utf-8")
            stderr = io.StringIO()
            with patch.dict("os.environ", {}, clear=True):
                with redirect_stderr(stderr):
                    status = main(
                        [
                            "--diff",
                            str(diff_path),
                            "--profile",
                            "fast-triage",
                            "--provider",
                            "openai-compatible",
                            "--no-learning-proposals",
                        ]
                    )
        self.assertEqual(status, 1)
        self.assertIn("OPENAI_API_KEY is unavailable", stderr.getvalue())

    def test_profile_rejects_allow_custom_endpoint(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = main(
                [
                    "--diff",
                    "review.patch",
                    "--profile",
                    "fast-triage",
                    "--provider",
                    "openai-compatible",
                    "--allow-custom-endpoint",
                ]
            )
        self.assertEqual(status, 1)
        self.assertIn(
            "--allow-custom-endpoint cannot be combined with --profile",
            stderr.getvalue(),
        )

    def test_profile_rejects_mismatched_api_key_env(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = main(
                [
                    "--diff",
                    "review.patch",
                    "--profile",
                    "fast-triage",
                    "--provider",
                    "openai-compatible",
                    "--api-key-env",
                    "OLLAMA_API_KEY",
                ]
            )
        self.assertEqual(status, 1)
        self.assertIn(
            "--api-key-env OLLAMA_API_KEY does not match profile 'fast-triage' "
            "(requires OPENAI_API_KEY)",
            stderr.getvalue(),
        )

    def test_local_profile_rejects_api_key_env(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = main(
                [
                    "--diff",
                    "review.patch",
                    "--profile",
                    "local-private",
                    "--provider",
                    "ollama",
                    "--api-key-env",
                    "OLLAMA_API_KEY",
                ]
            )
        self.assertEqual(status, 1)
        self.assertIn(
            "--api-key-env cannot be combined with profile 'local-private'",
            stderr.getvalue(),
        )

    def test_profile_accepts_matching_explicit_api_key_env(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            diff_path = Path(temp_dir) / "review.patch"
            diff_path.write_text(DIFF, encoding="utf-8")
            created = []

            class Registry:
                def create(self, settings):
                    created.append(settings)
                    return FakeProvider()

            with patch.dict(
                "os.environ", {"OPENAI_API_KEY": "openai-secret"}, clear=True
            ):
                with patch(
                    "review_sensei.cli.default_registry", return_value=Registry()
                ):
                    status = main(
                        [
                            "--diff",
                            str(diff_path),
                            "--profile",
                            "fast-triage",
                            "--provider",
                            "openai-compatible",
                            "--api-key-env",
                            "OPENAI_API_KEY",
                            "--no-learning-proposals",
                        ]
                    )

        self.assertEqual(status, 0)
        self.assertEqual(created[0].api_key, "openai-secret")

    def test_github_parser_exposes_allow_custom_endpoint_on_review(self):
        args = _github_parser().parse_args(
            [
                "--allow-custom-endpoint",
                "review",
                "--result",
                "result.json",
                "--diff",
                "pr.patch",
                "--repository",
                "owner/repo",
                "--repository-id",
                "1",
                "--pull-request",
                "2",
                "--head-sha",
                "abc123",
            ]
        )
        self.assertTrue(args.allow_custom_endpoint)

    def test_evaluate_rejects_invalid_openai_timeout_environment_value(self):
        with patch.dict("os.environ", {"OPENAI_TIMEOUT_SECONDS": "abc"}, clear=True):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                status = main(
                    [
                        "evaluate",
                        "--mode",
                        "live",
                        "--provider",
                        "openai-compatible",
                        "--allow-live-model",
                        "--allow-data-egress",
                        "--provider-version",
                        "test",
                    ]
                )
        self.assertEqual(status, 1)
        self.assertIn(
            "OPENAI_TIMEOUT_SECONDS must be a positive number", stderr.getvalue()
        )

    def test_profile_fast_triage_omits_conflicting_defaults(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            diff_path = Path(temp_dir) / "review.patch"
            diff_path.write_text(DIFF, encoding="utf-8")
            created = []

            class Registry:
                def create(self, settings):
                    created.append(settings)
                    return FakeProvider()

            with patch.dict(
                "os.environ", {"OPENAI_API_KEY": "openai-secret"}, clear=True
            ):
                with patch(
                    "review_sensei.cli.default_registry", return_value=Registry()
                ):
                    status = main(
                        [
                            "--diff",
                            str(diff_path),
                            "--profile",
                            "fast-triage",
                            "--provider",
                            "openai-compatible",
                            "--no-learning-proposals",
                        ]
                    )

        self.assertEqual(status, 0)
        self.assertEqual(created[0].profile, "fast-triage")
        self.assertIsNone(created[0].model)
        self.assertIsNone(created[0].base_url)
        self.assertIsNone(created[0].timeout_seconds)
        self.assertEqual(created[0].api_key, "openai-secret")

    def test_profile_requires_matching_explicit_provider(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = main(
                [
                    "--diff",
                    "review.patch",
                    "--profile",
                    "fast-triage",
                ]
            )
        self.assertEqual(status, 1)
        self.assertIn("requires --provider openai-compatible", stderr.getvalue())

    def test_profile_deep_verification_uses_profile_api_key_env(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            diff_path = Path(temp_dir) / "review.patch"
            diff_path.write_text(DIFF, encoding="utf-8")
            created = []

            class Registry:
                def create(self, settings):
                    created.append(settings)
                    return FakeProvider()

            with patch.dict(
                "os.environ",
                {
                    "OPENAI_API_KEY": "openai-secret",
                    "OLLAMA_API_KEY": "ollama-secret",
                },
                clear=True,
            ):
                with patch(
                    "review_sensei.cli.default_registry", return_value=Registry()
                ):
                    status = main(
                        [
                            "--diff",
                            str(diff_path),
                            "--profile",
                            "deep-verification",
                            "--provider",
                            "ollama",
                            "--no-learning-proposals",
                        ]
                    )

        self.assertEqual(status, 0)
        self.assertEqual(created[0].api_key, "ollama-secret")

    def test_profile_rejects_fixture_provider(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = main(
                [
                    "--diff",
                    "review.patch",
                    "--profile",
                    "local-private",
                    "--provider",
                    "fixture",
                    "--fixture-response",
                    "response.json",
                ]
            )
        self.assertEqual(status, 1)
        self.assertIn("fixture cannot be combined with --profile", stderr.getvalue())

    def test_openai_compatible_explicit_flags_override_environment(self):
        with patch.dict(
            "os.environ",
            {
                "OPENAI_BASE_URL": "https://api.openai.com/v1",
                "OPENAI_MODEL": "env-model",
                "OPENAI_TIMEOUT_SECONDS": "30",
            },
            clear=True,
        ):
            args = _parser().parse_args(
                [
                    "--provider",
                    "openai-compatible",
                    "--base-url",
                    "https://api.openai.com/v1",
                    "--model",
                    "cli-model",
                    "--timeout-seconds",
                    "9",
                ]
            )
        self.assertEqual(args.base_url, "https://api.openai.com/v1")
        self.assertEqual(args.model, "cli-model")
        self.assertEqual(args.timeout_seconds, 9.0)

    def test_prepare_diff_parser_does_not_receive_provider_defaults(self):
        from review_sensei.cli import _prepare_diff_parser

        args = _prepare_diff_parser().parse_args(
            ["--base-ref", "main", "--head-ref", "feature"]
        )
        self.assertFalse(hasattr(args, "provider"))
        self.assertFalse(hasattr(args, "base_url"))
        self.assertFalse(hasattr(args, "model"))
        self.assertFalse(hasattr(args, "api_key_env"))

    def test_github_review_parser_does_not_receive_provider_defaults(self):
        args = _github_parser().parse_args(
            [
                "review",
                "--result",
                "result.json",
                "--diff",
                "pr.patch",
                "--repository",
                "owner/repo",
                "--repository-id",
                "1",
                "--pull-request",
                "2",
                "--head-sha",
                "b" * 40,
            ]
        )
        self.assertFalse(hasattr(args, "provider"))
        self.assertFalse(hasattr(args, "base_url"))
        self.assertFalse(hasattr(args, "model"))
        self.assertFalse(hasattr(args, "api_key_env"))

    def test_openai_provider_rejects_non_allowlisted_environment_endpoint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            diff_path = Path(temp_dir) / "review.patch"
            diff_path.write_text(DIFF, encoding="utf-8")
            created = []

            class Registry:
                def create(self, settings):
                    created.append(settings)
                    return FakeProvider()

            with patch.dict(
                "os.environ",
                {
                    "OPENAI_API_KEY": "openai-secret",
                    "OPENAI_BASE_URL": "https://attacker.example/v1",
                },
                clear=True,
            ):
                with patch(
                    "review_sensei.cli.default_registry", return_value=Registry()
                ):
                    stderr = io.StringIO()
                    with redirect_stderr(stderr):
                        status = main(
                            [
                                "--diff",
                                str(diff_path),
                                "--provider",
                                "openai-compatible",
                                "--no-learning-proposals",
                            ]
                        )

        self.assertEqual(status, 1)
        self.assertEqual(created, [])
        self.assertIn("not allowlisted", stderr.getvalue())

    def test_openai_provider_accepts_explicit_custom_endpoint_opt_in(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            diff_path = Path(temp_dir) / "review.patch"
            diff_path.write_text(DIFF, encoding="utf-8")
            created = []

            class Registry:
                def create(self, settings):
                    created.append(settings)
                    return FakeProvider()

            with patch.dict(
                "os.environ",
                {
                    "OPENAI_API_KEY": "openai-secret",
                    "OPENAI_BASE_URL": "https://llm.internal/v1",
                },
                clear=True,
            ):
                with patch(
                    "review_sensei.cli.default_registry", return_value=Registry()
                ):
                    status = main(
                        [
                            "--diff",
                            str(diff_path),
                            "--provider",
                            "openai-compatible",
                            "--allow-custom-endpoint",
                            "--no-learning-proposals",
                        ]
                    )

        self.assertEqual(status, 0)
        self.assertEqual(created[0].base_url, "https://llm.internal/v1")
        self.assertTrue(created[0].allow_custom_endpoint)

    def test_openai_provider_uses_openai_credential_environment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            diff_path = Path(temp_dir) / "review.patch"
            diff_path.write_text(DIFF, encoding="utf-8")
            created = []

            class Registry:
                def create(self, settings):
                    created.append(settings)
                    return FakeProvider()

            with patch.dict(
                "os.environ",
                {"OPENAI_API_KEY": "openai-secret", "OLLAMA_API_KEY": "ollama-secret"},
                clear=True,
            ):
                with patch(
                    "review_sensei.cli.default_registry", return_value=Registry()
                ):
                    status = main(
                        [
                            "--diff",
                            str(diff_path),
                            "--provider",
                            "openai-compatible",
                            "--no-learning-proposals",
                        ]
                    )

        self.assertEqual(status, 0)
        self.assertEqual(created[0].api_key, "openai-secret")
        self.assertEqual(created[0].base_url, "https://api.openai.com/v1")
        self.assertEqual(created[0].model, "gpt-4o-mini")

    def test_parser_accepts_version_flag(self):
        args = _parser().parse_args(["--version"])
        self.assertTrue(args.version)

    def test_parser_accepts_prepare_diff_flags(self):
        from review_sensei.cli import _prepare_diff_parser

        args = _prepare_diff_parser().parse_args(
            [
                "--base-ref",
                "main",
                "--head-ref",
                "feature",
                "--head-repository",
                "owner/repo",
                "--repository",
                "repo",
                "--output",
                "pr.patch",
                "--max-diff-bytes",
                "1024",
                "--max-diff-lines",
                "10",
                "--max-diff-files",
                "2",
                "--max-diff-hunks",
                "3",
            ]
        )
        self.assertEqual(args.base_ref, "main")
        self.assertEqual(args.head_ref, "feature")
        self.assertEqual(args.head_repository, "owner/repo")
        self.assertEqual(args.repository, Path("repo"))
        self.assertEqual(args.output, Path("pr.patch"))
        self.assertEqual(args.max_diff_bytes, 1024)
        self.assertEqual(args.max_diff_lines, 10)
        self.assertEqual(args.max_diff_files, 2)
        self.assertEqual(args.max_diff_hunks, 3)

    def test_github_review_without_write_opt_in_does_not_write(self):
        from review_sensei.cli import _github_parser

        args = _github_parser().parse_args(
            [
                "review",
                "--result",
                "result.json",
                "--diff",
                "pr.patch",
                "--repository",
                "owner/repo",
                "--repository-id",
                "1",
                "--pull-request",
                "2",
                "--head-sha",
                "b" * 40,
                "--allow-write",
            ]
        )
        self.assertFalse(args.enable_review)

    def test_github_reply_without_write_opt_in_is_disabled(self):
        from review_sensei.cli import _github_parser

        args = _github_parser().parse_args(
            [
                "reply",
                "--reply",
                "reply.json",
                "--repository",
                "owner/repo",
                "--pull-request",
                "2",
                "--source-comment-id",
                "10",
                "--source-updated-at",
                "2026-08-19T00:00:00Z",
                "--head-sha",
                "b" * 40,
                "--allow-write",
                "--root-comment-id",
                "10",
            ]
        )
        self.assertFalse(args.enable_reply)

    def test_github_commands_require_allow_write(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = main(
                [
                    "github",
                    "review",
                    "--result",
                    "result.json",
                    "--diff",
                    "pr.patch",
                    "--repository",
                    "owner/repo",
                    "--repository-id",
                    "1",
                    "--pull-request",
                    "2",
                    "--head-sha",
                    "b" * 40,
                ]
            )
        self.assertEqual(status, 1)
        self.assertIn("--allow-write", stderr.getvalue())

    def test_github_command_uses_the_broker_backed_session_ledger(self):
        from review_sensei.hosting import github as github_module

        class FakeApplication:
            instances = []

            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.calls = []
                self.__class__.instances.append(self)

            def apply_maintainer_command(self, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(summary="paused")

        attestation = {
            "version": 1,
            "repository": "owner/repo",
            "repository_id": 1,
            "pull_request": 2,
            "head_sha": "b" * 40,
            "operation": "command",
            "source_comment_id": 10,
            "run_id": "123",
            "issued_at": 1,
            "concurrency_group": "reviewsensei-session-1-2",
            "job_workflow_ref": "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@refs/tags/v5",
            "job_workflow_sha": "a" * 40,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "attestation.json"
            path.write_text(json.dumps(attestation), encoding="utf-8")
            with patch.object(github_module, "GitHubApplication", FakeApplication):
                status = main(
                    [
                        "github",
                        "command",
                        "--comment-body",
                        "@sensei review pause",
                        "--actor",
                        "alice",
                        "--association",
                        "OWNER",
                        "--repository",
                        "owner/repo",
                        "--repository-id",
                        "1",
                        "--pull-request",
                        "2",
                        "--head-sha",
                        "b" * 40,
                        "--source-comment-id",
                        "10",
                        "--session-attestation",
                        str(path),
                        "--github-session-ledger",
                        "--allow-write",
                    ]
                )
        self.assertEqual(status, 0)
        call = FakeApplication.instances[0].calls[0]
        self.assertTrue(call["options"].github_session_ledger)
        self.assertTrue(call["options"].github_writes)
        self.assertEqual(call["session_attestation"], attestation)
        self.assertEqual(call["source_comment_id"], 10)

    def test_github_command_rejects_an_attestation_that_disagrees_with_the_cli(self):
        from review_sensei.hosting import github as github_module

        class FakeApplication:
            instances = []

            def __init__(self, **kwargs):
                self.__class__.instances.append(self)

        attestation = {
            "version": 1,
            "repository": "owner/repo",
            "repository_id": 1,
            "pull_request": 2,
            "head_sha": "a" * 40,
            "operation": "command",
            "source_comment_id": 10,
            "run_id": "123",
            "issued_at": 1,
            "concurrency_group": "reviewsensei-session-1-2",
            "job_workflow_ref": "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@refs/tags/v5",
            "job_workflow_sha": "b" * 40,
        }
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "attestation.json"
            path.write_text(json.dumps(attestation), encoding="utf-8")
            with (
                patch.object(github_module, "GitHubApplication", FakeApplication),
                redirect_stderr(stderr),
            ):
                status = main(
                    [
                        "github",
                        "command",
                        "--comment-body",
                        "@sensei review pause",
                        "--actor",
                        "alice",
                        "--association",
                        "OWNER",
                        "--repository",
                        "owner/repo",
                        "--repository-id",
                        "1",
                        "--pull-request",
                        "2",
                        "--head-sha",
                        "c" * 40,
                        "--source-comment-id",
                        "10",
                        "--session-attestation",
                        str(path),
                        "--github-session-ledger",
                        "--allow-write",
                    ]
                )
        self.assertEqual(status, 1)
        self.assertIn("does not match the hosted command identity", stderr.getvalue())
        self.assertEqual(FakeApplication.instances, [])

    def test_github_command_reaches_the_real_application_with_an_injected_transport(
        self,
    ):
        from review_sensei.hosting import github as github_module

        constructed = []

        class RecordingHttp:
            def __init__(self):
                constructed.append(self)

        broker_calls = []

        class FakeBroker:
            def __init__(self):
                broker_calls.append(self)

            def authorize_session_mutation(self, token, **kwargs):
                broker_calls.append((token, kwargs))
                # A grant without a session grant id must stop the mutation
                # before any write, in the real application code.
                return type(
                    "Grant",
                    (),
                    {"token": "capability-token", "grant": None, "attestation": {}},
                )()

        attestation = {
            "version": 1,
            "repository": "owner/repo",
            "repository_id": 1,
            "pull_request": 2,
            "head_sha": "b" * 40,
            "operation": "command",
            "source_comment_id": 10,
            "run_id": "123",
            "issued_at": 1,
            "concurrency_group": "reviewsensei-session-1-2",
            "job_workflow_ref": "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@refs/tags/v5",
            "job_workflow_sha": "c" * 40,
        }
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "attestation.json"
            path.write_text(json.dumps(attestation), encoding="utf-8")
            with (
                patch.object(github_module, "GitHubHttp", RecordingHttp),
                patch.object(github_module, "BrokerClient", FakeBroker),
                redirect_stderr(stderr),
            ):
                status = main(
                    [
                        "github",
                        "command",
                        "--comment-body",
                        "@sensei review pause",
                        "--actor",
                        "alice",
                        "--association",
                        "OWNER",
                        "--repository",
                        "owner/repo",
                        "--repository-id",
                        "1",
                        "--pull-request",
                        "2",
                        "--head-sha",
                        "b" * 40,
                        "--source-comment-id",
                        "10",
                        "--session-attestation",
                        str(path),
                        "--github-session-ledger",
                        "--oidc-token",
                        "caller-oidc",
                        "--allow-write",
                    ]
                )
        self.assertEqual(status, 1)
        self.assertIn(
            "broker command authorization returned an invalid grant",
            stderr.getvalue(),
        )
        self.assertEqual(len(constructed), 1)
        token, kwargs = broker_calls[1]
        self.assertEqual(token, "caller-oidc")
        self.assertEqual(kwargs["repository_id"], 1)
        self.assertEqual(kwargs["pull_request"], 2)
        self.assertEqual(kwargs["head_sha"], "b" * 40)
        self.assertEqual(kwargs["session_attestation"], attestation)

    def test_github_command_local_ledger_rejects_hosted_only_flags(self):
        from review_sensei.session import LocalSessionLedger, SessionIdentity

        hosted_only = (
            ("--repository-id", "1"),
            ("--source-comment-id", "10"),
            ("--session-attestation", "attestation.json"),
            ("--oidc-token", "token"),
        )
        for flag, value in hosted_only:
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as raw:
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    status = main(
                        [
                            "github",
                            "command",
                            "--comment-body",
                            "@sensei review pause",
                            "--actor",
                            "alice",
                            "--association",
                            "MEMBER",
                            "--repository",
                            "owner/repo",
                            "--pull-request",
                            "136",
                            flag,
                            value,
                            "--allow-write",
                            "--session-ledger",
                            raw,
                        ]
                    )
                self.assertEqual(status, 1)
                self.assertIn(
                    f"{flag} requires --github-session-ledger", stderr.getvalue()
                )
                loaded = LocalSessionLedger(Path(raw)).load(
                    SessionIdentity("owner/repo", 136)
                )
                self.assertEqual(loaded.status, "missing")

    def test_cli_version_returns_metadata_version(self):
        expected = importlib.metadata.version("review-sensei")
        stdout = io.StringIO()
        with redirect_stderr(io.StringIO()):
            with patch("sys.stdout", stdout):
                status = main(["--version"])
        self.assertEqual(status, 0)
        self.assertEqual(stdout.getvalue().strip(), expected)

    def test_cli_prepare_diff_version_returns_metadata_version(self):
        expected = importlib.metadata.version("review-sensei")
        stdout = io.StringIO()
        with redirect_stderr(io.StringIO()):
            with patch("sys.stdout", stdout):
                status = main(["prepare-diff", "--version"])
        self.assertEqual(status, 0)
        self.assertEqual(stdout.getvalue().strip(), expected)

    def test_cli_prepare_diff_requires_refs(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = main(["prepare-diff", "--base-ref", "main"])
        self.assertEqual(status, 1)
        self.assertIn("--base-ref and --head-ref are required", stderr.getvalue())

    def test_cli_requires_diff_for_review(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = main([])
        self.assertEqual(status, 1)
        self.assertIn("--diff is required", stderr.getvalue())

    def test_parser_accepts_category_and_stage_directories(self):
        args = _parser().parse_args(
            [
                "--diff",
                "review.patch",
                "--categories-dir",
                "categories",
                "--stages-dir",
                "stages",
                "--context-root",
                "target-checkout",
            ]
        )

        self.assertEqual(args.categories_dir, Path("categories"))
        self.assertEqual(args.stages_dir, Path("stages"))
        self.assertEqual(args.context_root, Path("target-checkout"))
        self.assertFalse(args.enable_symbol_context)

    def test_parser_accepts_symbol_context_opt_in_flags(self):
        args = _parser().parse_args(
            [
                "--diff",
                "review.patch",
                "--enable-symbol-context",
                "--base-sha",
                "a" * 40,
                "--head-sha",
                "b" * 40,
                "--symbol-context-allowed-path",
                "src/**",
                "--symbol-context-max-files",
                "8",
                "--symbol-context-max-bytes",
                "4096",
                "--symbol-context-max-depth",
                "2",
            ]
        )
        self.assertTrue(args.enable_symbol_context)
        self.assertEqual(args.base_sha, "a" * 40)
        self.assertEqual(args.head_sha, "b" * 40)
        self.assertEqual(args.symbol_context_allowed_path, ["src/**"])
        self.assertEqual(args.symbol_context_max_files, 8)
        self.assertEqual(args.symbol_context_max_bytes, 4096)
        self.assertEqual(args.symbol_context_max_depth, 2)

    def test_parser_accepts_fixture_response(self):
        args = _parser().parse_args(
            ["--diff", "review.patch", "--fixture-response", "response.json"]
        )

        self.assertEqual(args.fixture_response, Path("response.json"))

    def test_parser_accepts_orchestrate_large_changes(self):
        args = _parser().parse_args(
            ["--diff", "review.patch", "--orchestrate-large-changes"]
        )

        self.assertTrue(args.orchestrate_large_changes)

    def test_fixture_response_is_rejected_for_non_fixture_provider(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = main(
                [
                    "--diff",
                    "review.patch",
                    "--fixture-response",
                    "response.json",
                    "--provider",
                    "ollama",
                ]
            )

        self.assertEqual(status, 1)
        self.assertIn(
            "--fixture-response is only valid with --provider fixture",
            stderr.getvalue(),
        )

    def test_fixture_provider_requires_fixture_response(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = main(["--diff", "review.patch", "--provider", "fixture"])

        self.assertEqual(status, 1)
        self.assertIn(
            "--provider fixture requires --fixture-response", stderr.getvalue()
        )

    def test_fixture_cli_does_not_lookup_api_key_environment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            diff_path = root / "review.patch"
            diff_path.write_text(DIFF, encoding="utf-8")
            response_path = root / "response.json"
            response_path.write_text('{"summary":"ok","comments":[]}', encoding="utf-8")
            output_path = root / "review.json"

            class Registry:
                def __init__(self):
                    self.created = []

                def create(self, settings):
                    self.created.append(settings)
                    return FakeProvider()

            registry = Registry()
            with patch.dict("os.environ", {"OLLAMA_API_KEY": "secret-key"}):
                with patch("review_sensei.cli.default_registry", return_value=registry):
                    status = main(
                        [
                            "--diff",
                            str(diff_path),
                            "--provider",
                            "fixture",
                            "--fixture-response",
                            str(response_path),
                            "--model",
                            "fixture-v1",
                            "--no-learning-proposals",
                            "--output",
                            str(output_path),
                        ]
                    )

        self.assertEqual(status, 0)
        self.assertEqual(registry.created[0].api_key, None)
        self.assertEqual(registry.created[0].fixture_response, response_path)

    def test_evaluate_live_requires_allow_live_model_before_provider_creation(self):
        created = []

        class Registry:
            def create(self, settings):
                created.append(settings)
                return FakeProvider()

        stderr = io.StringIO()
        with patch("review_sensei.cli.default_registry", return_value=Registry()):
            with redirect_stderr(stderr):
                status = main(
                    [
                        "evaluate",
                        "--mode",
                        "live",
                        "--corpus",
                        "missing.json",
                        "--provider-version",
                        "1.0",
                    ]
                )

        self.assertEqual(status, 1)
        self.assertEqual(created, [])
        self.assertIn("--mode live requires --allow-live-model", stderr.getvalue())

    def test_evaluate_live_remote_requires_data_egress_before_provider_creation(self):
        created = []

        class Registry:
            def create(self, settings):
                created.append(settings)
                return FakeProvider()

        stderr = io.StringIO()
        with patch("review_sensei.cli.default_registry", return_value=Registry()):
            with redirect_stderr(stderr):
                status = main(
                    [
                        "evaluate",
                        "--mode",
                        "live",
                        "--corpus",
                        "missing.json",
                        "--provider-version",
                        "1.0",
                        "--allow-live-model",
                        "--base-url",
                        "https://ollama.example.com/api",
                    ]
                )

        self.assertEqual(status, 1)
        self.assertEqual(created, [])
        self.assertIn("--allow-data-egress", stderr.getvalue())

    def test_evaluate_live_named_remote_profile_requires_data_egress(self):
        created = []

        class Registry:
            def create(self, settings):
                created.append(settings)
                return FakeProvider()

        stderr = io.StringIO()
        with patch("review_sensei.cli.default_registry", return_value=Registry()):
            with redirect_stderr(stderr):
                status = main(
                    [
                        "evaluate",
                        "--mode",
                        "live",
                        "--corpus",
                        "missing.json",
                        "--provider-version",
                        "1.0",
                        "--allow-live-model",
                        "--profile",
                        "fast-triage",
                        "--provider",
                        "openai-compatible",
                    ]
                )

        self.assertEqual(status, 1)
        self.assertEqual(created, [])
        self.assertIn("--allow-data-egress", stderr.getvalue())

    def test_evaluate_live_local_private_profile_does_not_require_data_egress(self):
        created = []

        class Registry:
            def create(self, settings):
                created.append(settings)
                return FakeProvider()

        stderr = io.StringIO()
        with patch("review_sensei.cli.default_registry", return_value=Registry()):
            with patch("review_sensei.evaluation.load_corpus", return_value=object()):
                with patch(
                    "review_sensei.evaluation.evaluate_live",
                    return_value={"passed": True},
                ):
                    with redirect_stderr(stderr):
                        status = main(
                            [
                                "evaluate",
                                "--mode",
                                "live",
                                "--corpus",
                                "missing.json",
                                "--provider-version",
                                "1.0",
                                "--allow-live-model",
                                "--profile",
                                "local-private",
                                "--provider",
                                "ollama",
                            ]
                        )

        self.assertEqual(status, 0)
        self.assertEqual(len(created), 1)
        self.assertNotIn("--allow-data-egress", stderr.getvalue())

    def test_evaluate_live_deep_verification_profile_requires_data_egress(self):
        created = []

        class Registry:
            def create(self, settings):
                created.append(settings)
                return FakeProvider()

        stderr = io.StringIO()
        with patch("review_sensei.cli.default_registry", return_value=Registry()):
            with redirect_stderr(stderr):
                status = main(
                    [
                        "evaluate",
                        "--mode",
                        "live",
                        "--corpus",
                        "missing.json",
                        "--provider-version",
                        "1.0",
                        "--allow-live-model",
                        "--profile",
                        "deep-verification",
                        "--provider",
                        "ollama",
                    ]
                )

        self.assertEqual(status, 1)
        self.assertEqual(created, [])
        self.assertIn("--allow-data-egress", stderr.getvalue())

    def test_evaluate_fixture_rejects_live_acknowledgement_flags(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = main(
                [
                    "evaluate",
                    "--mode",
                    "fixture",
                    "--corpus",
                    "missing.json",
                    "--allow-live-model",
                ]
            )

        self.assertEqual(status, 1)
        self.assertIn(
            "--allow-live-model is only valid with --mode live",
            stderr.getvalue(),
        )

    def test_cli_loads_lens_documents_and_learnings_from_the_trusted_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            diff_path = root / "review.patch"
            diff_path.write_text(DIFF, encoding="utf-8")
            output_path = root / "review.json"
            categories_dir = root / "categories"
            stages_dir = root / "stages"
            learnings_dir = root / ".github" / "review-sensei" / "learnings"
            categories_dir.mkdir()
            stages_dir.mkdir()
            learnings_dir.mkdir(parents=True)
            (root / "architecture.md").write_text(
                "Dependencies point inward.",
                encoding="utf-8",
            )
            (categories_dir / "architecture.json").write_text(
                json.dumps(
                    {
                        "id": "architecture",
                        "title": "Architecture",
                        "focus": ["Dependency direction"],
                        "context": {
                            "learnings": {"categories": ["architecture"]},
                            "documents": [
                                {"path": "architecture.md", "required": True}
                            ],
                        },
                    }
                ),
                encoding="utf-8",
            )
            (stages_dir / "review.json").write_text(
                json.dumps(
                    {
                        "name": "Architecture review",
                        "category_ids": ["architecture"],
                        "outputs": ["summary"],
                        "prompt_template": (
                            "{review_categories}\n{review_context}\n{diff}"
                        ),
                    }
                ),
                encoding="utf-8",
            )
            (learnings_dir / "architecture.json").write_text(
                json.dumps(
                    {
                        "id": "architecture-rule",
                        "title": "Architecture rule",
                        "rule": "Dependencies point inward.",
                        "category": "architecture",
                    }
                ),
                encoding="utf-8",
            )
            provider = FakeProvider()
            with patch(
                "review_sensei.cli.default_registry",
                return_value=FakeRegistry(provider),
            ):
                status = main(
                    [
                        "--diff",
                        str(diff_path),
                        "--learning-root",
                        str(root),
                        "--categories-dir",
                        str(categories_dir),
                        "--stages-dir",
                        str(stages_dir),
                        "--output",
                        str(output_path),
                    ]
                )

        self.assertEqual(status, 0)
        self.assertIn("architecture.md", provider.requests[0].prompt)
        self.assertIn("architecture-rule", provider.requests[0].prompt)
        self.assertEqual(provider.requests[0].prompt.count("architecture-rule"), 1)
        self.assertNotIn("symbol-aware-source-context", provider.requests[0].prompt)

    def test_cli_symbol_context_default_stays_on_document_learning_boundary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            src = root / "src"
            src.mkdir()
            (src / "app.py").write_text("import helper\nkeep\n")
            (src / "helper.py").write_text("VALUE = 1\n")
            diff_path = root / "review.patch"
            diff_path.write_text(DIFF, encoding="utf-8")
            output_path = root / "review.json"
            provider = SymbolContextProvider()
            with patch(
                "review_sensei.cli.default_registry",
                return_value=FakeRegistry(provider),
            ):
                status = main(
                    [
                        "--diff",
                        str(diff_path),
                        "--learning-root",
                        str(root),
                        "--output",
                        str(output_path),
                    ]
                )

            self.assertEqual(status, 0)
            self.assertNotIn("helper.py", provider.requests[0].prompt)
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertNotIn("source_context", payload)

    def test_cli_symbol_context_opt_in_uses_trusted_base_and_records_coverage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            src = root / "src"
            src.mkdir()
            (src / "app.py").write_text("import helper\nkeep\nchange\n")
            (src / "helper.py").write_text("VALUE = 1\n")
            diff_path = root / "review.patch"
            diff_path.write_text(DIFF, encoding="utf-8")
            output_path = root / "review.json"
            provider = SymbolContextProvider()
            with patch(
                "review_sensei.cli.default_registry",
                return_value=FakeRegistry(provider),
            ):
                status = main(
                    [
                        "--diff",
                        str(diff_path),
                        "--learning-root",
                        str(root),
                        "--enable-symbol-context",
                        "--base-sha",
                        "a" * 40,
                        "--head-sha",
                        "b" * 40,
                        "--output",
                        str(output_path),
                    ]
                )

            self.assertEqual(status, 0)
            prompt = provider.requests[0].prompt
            self.assertIn("symbol-aware-source-context", prompt)
            self.assertIn("helper.py", prompt)
            self.assertIn("trusted-base", prompt)
            self.assertIn("a" * 40, prompt)
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            coverage = payload["source_context"]
            self.assertTrue(coverage["enabled"])
            self.assertEqual(coverage["snapshot"]["kind"], "base")
            self.assertEqual(coverage["snapshot"]["revision"], "a" * 40)
            self.assertEqual(coverage["untrusted_head_sha"], "b" * 40)
            self.assertEqual(coverage["languages"], ["python"])
            self.assertNotIn("content", coverage)

    def test_cli_symbol_context_requires_base_sha(self):
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as temp_dir:
            diff_path = Path(temp_dir) / "review.patch"
            diff_path.write_text(DIFF, encoding="utf-8")
            with redirect_stderr(stderr):
                status = main(
                    [
                        "--diff",
                        str(diff_path),
                        "--enable-symbol-context",
                    ]
                )

        self.assertEqual(status, 1)
        self.assertIn("--base-sha", stderr.getvalue())

    def test_cli_symbol_context_rejects_malformed_base_sha(self):
        for value in ("a" * 39, "a" * 41, "z" * 40, "not-a-sha"):
            with self.subTest(base_sha=value):
                stderr = io.StringIO()
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    diff_path = root / "review.patch"
                    diff_path.write_text(DIFF, encoding="utf-8")
                    with redirect_stderr(stderr):
                        status = main(
                            [
                                "--diff",
                                str(diff_path),
                                "--learning-root",
                                str(root),
                                "--enable-symbol-context",
                                "--base-sha",
                                value,
                            ]
                        )

                self.assertEqual(status, 1)
                message = stderr.getvalue()
                self.assertIn("context snapshot revision must be a commit SHA", message)
                # The sanitized boundary names the offending input without a
                # traceback.
                self.assertNotIn("Traceback", message)

    def test_cli_symbol_context_rejects_malformed_head_sha(self):
        for value in ("b" * 39, "b" * 41, "z" * 40, "not-a-sha"):
            with self.subTest(head_sha=value):
                stderr = io.StringIO()
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    src = root / "src"
                    src.mkdir()
                    (src / "app.py").write_text("import helper\nkeep\nchange\n")
                    (src / "helper.py").write_text("VALUE = 1\n")
                    diff_path = root / "review.patch"
                    diff_path.write_text(DIFF, encoding="utf-8")
                    provider = SymbolContextProvider()
                    with patch(
                        "review_sensei.cli.default_registry",
                        return_value=FakeRegistry(provider),
                    ):
                        with redirect_stderr(stderr):
                            status = main(
                                [
                                    "--diff",
                                    str(diff_path),
                                    "--learning-root",
                                    str(root),
                                    "--enable-symbol-context",
                                    "--base-sha",
                                    "a" * 40,
                                    "--head-sha",
                                    value,
                                ]
                            )

                self.assertEqual(status, 1)
                message = stderr.getvalue()
                self.assertIn("untrusted_head_sha must be a commit SHA", message)
                self.assertNotIn("Traceback", message)
                self.assertEqual(provider.requests, [])

    def test_cli_symbol_context_normalizes_uppercase_shas(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            src = root / "src"
            src.mkdir()
            (src / "app.py").write_text("import helper\nkeep\nchange\n")
            (src / "helper.py").write_text("VALUE = 1\n")
            diff_path = root / "review.patch"
            diff_path.write_text(DIFF, encoding="utf-8")
            output_path = root / "review.json"
            provider = SymbolContextProvider()
            with patch(
                "review_sensei.cli.default_registry",
                return_value=FakeRegistry(provider),
            ):
                status = main(
                    [
                        "--diff",
                        str(diff_path),
                        "--learning-root",
                        str(root),
                        "--enable-symbol-context",
                        "--base-sha",
                        "A" * 40,
                        "--head-sha",
                        "B" * 40,
                        "--output",
                        str(output_path),
                    ]
                )

            self.assertEqual(status, 0)
            # Uppercase input is normalized rather than rejected, so the
            # recorded identity stays canonical lowercase hex.
            coverage = json.loads(output_path.read_text(encoding="utf-8"))[
                "source_context"
            ]
            self.assertEqual(coverage["snapshot"]["revision"], "a" * 40)
            self.assertEqual(coverage["untrusted_head_sha"], "b" * 40)

    def test_cli_symbol_context_rejects_malicious_allowed_path(self):
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            diff_path = root / "review.patch"
            diff_path.write_text(DIFF, encoding="utf-8")
            with redirect_stderr(stderr):
                status = main(
                    [
                        "--diff",
                        str(diff_path),
                        "--learning-root",
                        str(root),
                        "--enable-symbol-context",
                        "--base-sha",
                        "a" * 40,
                        "--symbol-context-allowed-path",
                        "../secret/**",
                    ]
                )

        self.assertEqual(status, 1)
        self.assertIn(
            "source context allowed path pattern is invalid", stderr.getvalue()
        )

    def test_categories_directory_requires_a_stages_directory(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = main(
                [
                    "--diff",
                    "unused.patch",
                    "--categories-dir",
                    "categories",
                ]
            )

        self.assertEqual(status, 1)
        self.assertIn("--categories-dir requires --stages-dir", stderr.getvalue())

    def test_cli_rejects_oversized_diff_before_provider_creation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "oversized.patch"
            path.write_bytes(b"x" * (1_048_576 + 1))
            created = []

            class Registry:
                def create(self, settings):
                    created.append(settings)
                    return FakeProvider()

            stderr = io.StringIO()
            with patch("review_sensei.cli.default_registry", return_value=Registry()):
                with redirect_stderr(stderr):
                    status = main(["--diff", str(path)])
            self.assertEqual(status, 1)
            self.assertEqual(created, [])
            self.assertNotIn("x" * 20, stderr.getvalue())


class DoctorPlanCliTests(unittest.TestCase):
    def test_doctor_parser_accepts_configuration_flags(self):
        args = _doctor_parser().parse_args(
            [
                "--stages-dir",
                "stages",
                "--categories-dir",
                "categories",
                "--context-root",
                "context",
                "--network",
                "--json",
            ]
        )
        self.assertEqual(args.stages_dir, Path("stages"))
        self.assertEqual(args.categories_dir, Path("categories"))
        self.assertEqual(args.context_root, Path("context"))
        self.assertTrue(args.network)
        self.assertTrue(args.as_json)
        self.assertIsNone(args.review_mode)
        mode = _doctor_parser().parse_args(["--review-mode", "merge-focused"])
        self.assertEqual(mode.review_mode, "merge-focused")
        networked = _doctor_parser().parse_args(
            [
                "--network",
                "--repository",
                "owner/repo",
                "--allow-data-egress",
                "--compatibility-manifest",
                "manifest.json",
            ]
        )
        self.assertEqual(networked.repository, "owner/repo")
        self.assertTrue(networked.allow_data_egress)

    def test_plan_parser_accepts_preview_flags(self):
        args = _plan_parser().parse_args(
            [
                "--diff",
                "review.patch",
                "--repository",
                "owner/repo",
                "--pull-request",
                "3",
                "--session-ledger",
                "ledger",
                "--title",
                "Preview",
                "--stage",
                "review",
                "--provider-mode",
                "local",
                "--review-mode",
                "advisory",
                "--base-sha",
                "a" * 40,
                "--head-sha",
                "b" * 40,
                "--json",
            ]
        )
        self.assertEqual(args.diff, Path("review.patch"))
        self.assertEqual(args.repository, "owner/repo")
        self.assertEqual(args.pull_request, 3)
        self.assertEqual(args.session_ledger, Path("ledger"))
        self.assertEqual(args.stage, ["review"])
        self.assertEqual(args.base_sha, "a" * 40)
        self.assertEqual(args.head_sha, "b" * 40)
        self.assertEqual(args.review_mode, "advisory")
        self.assertTrue(args.as_json)

    def test_doctor_cli_renders_json_and_exit_code(self):
        stdout = io.StringIO()
        with redirect_stderr(io.StringIO()):
            with patch("sys.stdout", stdout):
                status = main(["doctor", "--json"])
        self.assertEqual(status, 2)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["schema_version"], "v1")
        self.assertIn("checks", payload)
        self.assertEqual(payload["status"], "action")
        self.assertEqual(payload["review_convergence"]["mode"], "merge-focused")
        self.assertEqual(payload["review_convergence"]["enforcement"], "publication")

    def test_doctor_cli_network_flag_reports_unreachable_endpoint(self):
        stdout = io.StringIO()
        with redirect_stderr(io.StringIO()):
            with patch("sys.stdout", stdout):
                with patch.dict("os.environ", {"REVIEWSENSEI_PROVIDER_MODE": "local"}):
                    with patch(
                        "review_sensei.diagnostics._bounded_probe_get",
                        side_effect=ReviewInputError("unreachable endpoint: URLError"),
                    ):
                        status = main(
                            [
                                "doctor",
                                "--network",
                                "--json",
                                "--base-url",
                                "http://127.0.0.1:11434/api",
                            ]
                        )
        self.assertEqual(status, 2)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "action")
        self.assertTrue(
            any(
                check["name"] == "endpoint" and "unreachable" in check["detail"]
                for check in payload["checks"]
            )
        )

    def test_plan_cli_renders_ready_plan_from_diff(self):
        with tempfile.TemporaryDirectory() as temporary:
            diff_path = Path(temporary) / "review.patch"
            diff_path.write_text(DIFF, encoding="utf-8")
            stdout = io.StringIO()
            with redirect_stderr(io.StringIO()):
                with patch("sys.stdout", stdout):
                    status = main(
                        [
                            "plan",
                            "--diff",
                            str(diff_path),
                            "--repository",
                            "owner/repo",
                            "--base-sha",
                            "a" * 40,
                            "--head-sha",
                            "b" * 40,
                            "--json",
                        ]
                    )
        self.assertEqual(status, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["operations"]["provider_calls"], 0)
        self.assertEqual(payload["identity"]["base_sha"], "a" * 40)
        self.assertEqual(payload["identity"]["head_sha"], "b" * 40)

    def test_plan_cli_without_diff_exits_incomplete(self):
        stdout = io.StringIO()
        with redirect_stderr(io.StringIO()):
            with patch("sys.stdout", stdout):
                status = main(["plan"])
        self.assertEqual(status, 3)
        self.assertIn("incomplete", stdout.getvalue())

    def test_plan_cli_unexpected_analyze_diff_failure_exits_without_traceback(self):
        with tempfile.TemporaryDirectory() as temporary:
            diff_path = Path(temporary) / "review.patch"
            diff_path.write_text(DIFF, encoding="utf-8")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                with patch(
                    "review_sensei.diagnostics.analyze_diff",
                    side_effect=TypeError("boom"),
                ):
                    status = main(["plan", "--diff", str(diff_path)])
        self.assertEqual(status, 2)
        self.assertIn("boom", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_learnings_parser_accepts_diagnose_and_feedback(self):
        diagnose = _learnings_parser().parse_args(
            ["diagnose", "--learning-root", ".", "--json"]
        )
        self.assertEqual(diagnose.command, "diagnose")
        self.assertTrue(diagnose.as_json)
        feedback = _learnings_parser().parse_args(
            ["feedback", "--file", "feedback.json", "--json"]
        )
        self.assertEqual(feedback.command, "feedback")
        self.assertEqual(str(feedback.file), "feedback.json")

    def test_learnings_diagnose_requires_an_explicit_trusted_root(self):
        # ADR 0005 forbids implicitly scanning the current working directory.
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                _learnings_parser().parse_args(["diagnose", "--json"])

    def test_learnings_diagnose_cli_is_advisory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / ".github" / "review-sensei" / "learnings"
            directory.mkdir(parents=True)
            (directory / "rule.json").write_text(
                json.dumps(
                    {
                        "id": "stale-rule",
                        "title": "Stale",
                        "rule": "Replace me.",
                        "scope": ["*"],
                        "reviewed_at": "2020-01-01T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                status = main(
                    [
                        "learnings",
                        "diagnose",
                        "--learning-root",
                        str(root),
                        "--json",
                    ]
                )
        self.assertEqual(status, 0)
        report = json.loads(stdout.getvalue())
        self.assertFalse(report["automatic_mutation"])
        self.assertTrue(report["human_decision_required"])

    def test_learnings_feedback_cli_states_absence_is_not_approval(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "feedback.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "records": [
                            {
                                "learning_id": "provider-boundary",
                                "finding_id": "finding-1",
                                "outcome": "unverified",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                status = main(["learnings", "feedback", "--file", str(path)])
        self.assertEqual(status, 0)
        self.assertIn("Absence of feedback is not approval.", stdout.getvalue())
        self.assertIn("unverified 1", stdout.getvalue())
        # Text mode must distinguish "no store loaded" from "all have feedback".
        self.assertIn("not enumerated: no approved store loaded", stdout.getvalue())

    def test_learnings_feedback_cli_text_lists_learnings_without_feedback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / ".github" / "review-sensei" / "learnings"
            directory.mkdir(parents=True)
            (directory / "rule.json").write_text(
                json.dumps(
                    {
                        "id": "unused-rule",
                        "title": "Unused",
                        "rule": "Keep adapters isolated.",
                        "scope": ["*"],
                    }
                ),
                encoding="utf-8",
            )
            path = root / "feedback.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "records": [
                            {
                                "learning_id": "provider-boundary",
                                "finding_id": "finding-1",
                                "outcome": "useful",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                status = main(
                    [
                        "learnings",
                        "feedback",
                        "--file",
                        str(path),
                        "--learning-root",
                        str(root),
                    ]
                )
        self.assertEqual(status, 0)
        self.assertIn("known learnings without feedback unused-rule", stdout.getvalue())

    def test_learnings_feedback_cli_marks_known_id_scope_unset(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "feedback.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "records": [
                            {
                                "learning_id": "provider-boundary",
                                "finding_id": "finding-1",
                                "outcome": "useful",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                status = main(["learnings", "feedback", "--file", str(path), "--json"])
        self.assertEqual(status, 0)
        summary = json.loads(stdout.getvalue())
        self.assertEqual(summary["known_learning_ids_scope"], "unset")
        self.assertEqual(summary["known_learning_ids_without_feedback"], [])

    def _run_compare_learnings(self, *, passed: bool) -> tuple[int, str]:
        # The corpus and comparison are stubbed so this exercises only the exit
        # code contract, with no dependency on the working directory.
        report = {"schema_version": "1.0", "with_learnings_passed": passed}
        stdout = io.StringIO()
        with patch("review_sensei.evaluation.load_corpus", return_value=object()):
            with patch(
                "review_sensei.evaluation.compare_learning_effect",
                return_value=report,
            ):
                with redirect_stderr(io.StringIO()):
                    with patch("sys.stdout", stdout):
                        status = main(
                            ["evaluate", "--mode", "fixture", "--compare-learnings"]
                        )
        return status, stdout.getvalue()

    def test_evaluate_compare_learnings_preserves_fixture_pass_fail(self):
        status, rendered = self._run_compare_learnings(passed=True)
        self.assertEqual(status, 0)
        self.assertTrue(json.loads(rendered)["with_learnings_passed"])

        # Adding the flag must not mask failing fixture cases with exit 0.
        status, rendered = self._run_compare_learnings(passed=False)
        self.assertEqual(status, 1)
        self.assertFalse(json.loads(rendered)["with_learnings_passed"])

    def test_evaluate_compare_learnings_is_fixture_only(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = main(
                [
                    "evaluate",
                    "--mode",
                    "live",
                    "--compare-learnings",
                    "--allow-live-model",
                    "--provider-version",
                    "local",
                ]
            )
        self.assertEqual(status, 1)
        self.assertIn(
            "--compare-learnings is only valid with --mode fixture",
            stderr.getvalue(),
        )


class PromotionCliTests(unittest.TestCase):
    def _make_report(self, **kwargs):
        path = Path(__file__).resolve().parent / "test_promotion_release.py"
        spec = importlib.util.spec_from_file_location("promotion_release_helpers", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module._make_report(**kwargs)

    def test_promotion_cli_emits_supported_record_from_live_reports(self):

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reports = []
            for index in (1, 2, 3):
                path = root / f"live-{index}.json"
                path.write_text(
                    json.dumps(self._make_report(elapsed_total_ms=index)),
                    encoding="utf-8",
                )
                reports.append(path)
            output = root / "promotion.json"
            status = main(
                [
                    "promotion",
                    "emit",
                    "--report",
                    str(reports[0]),
                    "--report",
                    str(reports[1]),
                    "--report",
                    str(reports[2]),
                    "--observed-revision",
                    "local-ollama-1",
                    "--evaluated-at",
                    "2026-09-16T00:00:00Z",
                    "--reproducibility-json",
                    '{"seed":"fixed"}',
                    "--output",
                    str(output),
                ]
            )
            self.assertEqual(status, 0)
            record = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(record["status"], "supported")
            validate_status = main(
                [
                    "promotion",
                    "validate",
                    "--require-supported",
                    "--record",
                    str(output),
                    "--report",
                    str(reports[0]),
                    "--report",
                    str(reports[1]),
                    "--report",
                    str(reports[2]),
                ]
            )
            self.assertEqual(validate_status, 0)

    def test_promotion_cli_emits_from_reproducibility_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reports = []
            for index in (1, 2, 3):
                path = root / f"live-{index}.json"
                path.write_text(
                    json.dumps(self._make_report(elapsed_total_ms=index)),
                    encoding="utf-8",
                )
                reports.append(path)
            settings = root / "reproducibility.json"
            settings.write_text('{"seed":"file"}\n', encoding="utf-8")
            output = root / "promotion.json"
            status = main(
                [
                    "promotion",
                    "emit",
                    "--report",
                    str(reports[0]),
                    "--report",
                    str(reports[1]),
                    "--report",
                    str(reports[2]),
                    "--observed-revision",
                    "local-ollama-1",
                    "--evaluated-at",
                    "2026-09-16T00:00:00Z",
                    "--reproducibility-file",
                    str(settings),
                    "--output",
                    str(output),
                ]
            )
            self.assertEqual(status, 0)
            record = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(record["reproducibility"], {"seed": "file"})

    def test_promotion_cli_rejects_fixture_reports_for_supported_status(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reports = []
            for index in (1, 2, 3):
                path = root / f"fixture-{index}.json"
                path.write_text(
                    json.dumps(
                        self._make_report(
                            mode="fixture",
                            provider="fixture",
                            model="fixture-v1",
                            provider_version=None,
                            endpoint_scope="none",
                            elapsed_total_ms=index,
                        )
                    ),
                    encoding="utf-8",
                )
                reports.append(path)
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                status = main(
                    [
                        "promotion",
                        "emit",
                        "--report",
                        str(reports[0]),
                        "--report",
                        str(reports[1]),
                        "--report",
                        str(reports[2]),
                        "--observed-revision",
                        "fixture-v1",
                        "--evaluated-at",
                        "2026-01-01T00:00:00Z",
                        "--reproducibility-json",
                        '{"seed":"fixed"}',
                        "--status",
                        "supported",
                    ]
                )
            self.assertEqual(status, 1)
            self.assertIn("supported promotion requires", stderr.getvalue())

    def test_openrouter_provider_requires_api_key_at_cli_boundary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            diff_path = Path(temp_dir) / "review.patch"
            diff_path.write_text(DIFF, encoding="utf-8")
            stderr = io.StringIO()
            with patch.dict("os.environ", {}, clear=True):
                with redirect_stderr(stderr):
                    status = main(
                        [
                            "--diff",
                            str(diff_path),
                            "--provider",
                            "openrouter",
                            "--no-learning-proposals",
                        ]
                    )
        self.assertEqual(status, 1)
        self.assertIn(
            "environment variable OPENROUTER_API_KEY is unavailable",
            stderr.getvalue(),
        )

    def test_openrouter_rejects_non_allowlisted_base_url(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            diff_path = Path(temp_dir) / "review.patch"
            diff_path.write_text(DIFF, encoding="utf-8")
            stderr = io.StringIO()
            with patch.dict(
                "os.environ", {"OPENROUTER_API_KEY": "router-secret"}, clear=True
            ):
                with redirect_stderr(stderr):
                    status = main(
                        [
                            "--diff",
                            str(diff_path),
                            "--provider",
                            "openrouter",
                            "--base-url",
                            "https://evil.example/api/v1",
                            "--no-learning-proposals",
                        ]
                    )
        self.assertEqual(status, 1)
        self.assertIn("openrouter endpoint is not allowlisted", stderr.getvalue())

    def test_openrouter_rejects_non_allowlisted_openrouter_base_url_env(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            diff_path = Path(temp_dir) / "review.patch"
            diff_path.write_text(DIFF, encoding="utf-8")
            stderr = io.StringIO()
            with patch.dict(
                "os.environ",
                {
                    "OPENROUTER_API_KEY": "router-secret",
                    "OPENROUTER_BASE_URL": "https://evil.example/api/v1",
                },
                clear=True,
            ):
                with redirect_stderr(stderr):
                    status = main(
                        [
                            "--diff",
                            str(diff_path),
                            "--provider",
                            "openrouter",
                            "--no-learning-proposals",
                        ]
                    )
        self.assertEqual(status, 1)
        self.assertIn("openrouter endpoint is not allowlisted", stderr.getvalue())

    def test_openrouter_upstream_provider_selects_routing_policy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            diff_path = Path(temp_dir) / "review.patch"
            diff_path.write_text(DIFF, encoding="utf-8")
            created = []

            class Registry:
                def create(self, settings):
                    created.append(settings)
                    return FakeProvider()

            with patch.dict(
                "os.environ",
                {
                    "OPENROUTER_API_KEY": "router-secret",
                    "OPENROUTER_UPSTREAM_PROVIDER": "openai",
                },
                clear=True,
            ):
                with patch(
                    "review_sensei.cli.default_registry", return_value=Registry()
                ):
                    status = main(
                        [
                            "--diff",
                            str(diff_path),
                            "--provider",
                            "openrouter",
                            "--no-learning-proposals",
                        ]
                    )
        self.assertEqual(status, 0)
        self.assertEqual(created[0].openrouter_policy.upstream_provider, "openai")

    def test_openrouter_timeout_prefers_reviewsensei_env(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            diff_path = Path(temp_dir) / "review.patch"
            diff_path.write_text(DIFF, encoding="utf-8")
            created = []

            class Registry:
                def create(self, settings):
                    created.append(settings)
                    return FakeProvider()

            with patch.dict(
                "os.environ",
                {
                    "OPENROUTER_API_KEY": "router-secret",
                    "REVIEWSENSEI_OPENROUTER_TIMEOUT_SECONDS": "300",
                    "OPENROUTER_TIMEOUT_SECONDS": "120",
                },
                clear=True,
            ):
                with patch(
                    "review_sensei.cli.default_registry", return_value=Registry()
                ):
                    status = main(
                        [
                            "--diff",
                            str(diff_path),
                            "--provider",
                            "openrouter",
                            "--no-learning-proposals",
                        ]
                    )
        self.assertEqual(status, 0)
        self.assertEqual(created[0].timeout_seconds, 300.0)

    def test_openrouter_api_key_env_override(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            diff_path = Path(temp_dir) / "review.patch"
            diff_path.write_text(DIFF, encoding="utf-8")
            created = []

            class Registry:
                def create(self, settings):
                    created.append(settings)
                    return FakeProvider()

            with patch.dict(
                "os.environ",
                {"CUSTOM_ROUTER_KEY": "router-secret"},
                clear=True,
            ):
                with patch(
                    "review_sensei.cli.default_registry", return_value=Registry()
                ):
                    status = main(
                        [
                            "--diff",
                            str(diff_path),
                            "--provider",
                            "openrouter",
                            "--api-key-env",
                            "CUSTOM_ROUTER_KEY",
                            "--no-learning-proposals",
                        ]
                    )
        self.assertEqual(status, 0)
        self.assertEqual(created[0].api_key, "router-secret")

    def test_openrouter_profile_wires_policy_and_credential(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            diff_path = Path(temp_dir) / "review.patch"
            diff_path.write_text(DIFF, encoding="utf-8")
            created = []

            class Registry:
                def create(self, settings):
                    created.append(settings)
                    return FakeProvider()

            with patch.dict(
                "os.environ", {"OPENROUTER_API_KEY": "router-secret"}, clear=True
            ):
                with patch(
                    "review_sensei.cli.default_registry", return_value=Registry()
                ):
                    status = main(
                        [
                            "--diff",
                            str(diff_path),
                            "--profile",
                            "openrouter-sonnet",
                            "--provider",
                            "openrouter",
                            "--allow-unqualified-profile",
                            "--no-learning-proposals",
                        ]
                    )

        self.assertEqual(status, 0)
        self.assertEqual(created[0].profile, "openrouter-sonnet")
        self.assertEqual(created[0].api_key, "router-secret")
        self.assertEqual(
            created[0].openrouter_policy.upstream_provider,
            "anthropic",
        )

    def test_openrouter_profile_rejects_mismatched_provider(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = main(
                [
                    "--diff",
                    "review.patch",
                    "--profile",
                    "openrouter-sonnet",
                ]
            )
        self.assertEqual(status, 1)
        self.assertIn("requires --provider openrouter", stderr.getvalue())

    def test_openrouter_profile_rejects_mismatched_explicit_provider(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = main(
                [
                    "--diff",
                    "review.patch",
                    "--profile",
                    "openrouter-sonnet",
                    "--provider",
                    "ollama",
                ]
            )
        self.assertEqual(status, 1)
        self.assertIn("does not match profile", stderr.getvalue())

    def test_openrouter_profile_requires_unqualified_opt_in(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            diff_path = Path(temp_dir) / "review.patch"
            diff_path.write_text(DIFF, encoding="utf-8")
            stderr = io.StringIO()
            with patch.dict(
                "os.environ", {"OPENROUTER_API_KEY": "router-secret"}, clear=True
            ):
                with redirect_stderr(stderr):
                    status = main(
                        [
                            "--diff",
                            str(diff_path),
                            "--profile",
                            "openrouter-sonnet",
                            "--provider",
                            "openrouter",
                            "--no-learning-proposals",
                        ]
                    )
        self.assertEqual(status, 1)
        self.assertIn("is unqualified", stderr.getvalue())

    def test_promotion_cli_does_not_accept_live_model_flags(self):
        stderr = io.StringIO()
        with self.assertRaises(SystemExit):
            with redirect_stderr(stderr):
                main(
                    [
                        "promotion",
                        "emit",
                        "--allow-live-model",
                        "--report",
                        "missing.json",
                        "--observed-revision",
                        "r1",
                        "--evaluated-at",
                        "2026-01-01",
                        "--reproducibility-json",
                        '{"seed":"fixed"}',
                    ]
                )
        self.assertIn("unrecognized arguments", stderr.getvalue())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
