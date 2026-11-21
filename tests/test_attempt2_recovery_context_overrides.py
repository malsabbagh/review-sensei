"""Tests for PR 5: Attempt-2 recovery with env/CLI overrides, custom stages, symbol context, and confirmed evidence."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import test_review_transaction as tx
from review_sensei_github import review_run
from review_sensei_github.publication_context_recovery import (
    reconstruct_or_prescribe_publication_contexts,
    reconstruct_publication_contexts,
)
from review_sensei_github.session_ledger import parse_session_comment
from test_api_work_lifecycle import forbid_replacement_phase_budget
from test_api_work_persistence import BASE, HEAD, IDENTITY
from test_review_run import EntryFixture, fixture_process_environment

from review_sensei.api_work import ApiRunAccounting
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.errors import ReviewInputError
from review_sensei.models import ProviderResponse, ReviewResult, ReviewTransaction
from review_sensei.recovery_context import RecoveryContextStore, RecoveryRefusalError
from review_sensei.session import LocalSessionLedger
from review_sensei.verifier import _snapshot_digest

HANDOFF_KEY = b"k" * 32


class _OverrideEntryFixture(EntryFixture):
    """Hosted EntryFixture supporting env overrides, CLI args, custom stages, and handoff."""

    def __init__(
        self,
        directory: str,
        *,
        limit: int = 512,
        backend: str = "cloud-ollama",
        model: str = "deepseek-v4.1-flash:cloud",
        extra_yaml: str = "",
        extra_files: dict[str, str] | None = None,
    ) -> None:
        super().__init__(directory, limit=limit, runner_kind="hosted")
        self.recovery_context_dir: Path | None = None
        self.recovery_context_hmac_path: Path | None = None
        self.recovery_context_ttl_seconds: int | None = None
        self.override_run_directory: Path | None = None
        self.override_policy_commit: str | None = None
        self.extra_env: dict[str, str] = {}
        self.extra_engine_args: list[str] = []
        self.config_bytes = (
            f"schema: 1\n"
            f"api_request_limit: {limit}\n"
            f"inference:\n"
            f"  backend: {backend}\n"
            f"  model: {model}\n"
            f"github:\n"
            f"  writes: true\n"
            f"  artifacts: diagnostics\n"
            f"  publication_inventory: complete\n"
            f"{extra_yaml}"
        ).encode("utf-8")
        (self.root / ".reviewsensei.yml").write_bytes(self.config_bytes)
        if extra_files:
            for rel, content in extra_files.items():
                target = self.root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
        self._commit_repo_state()

    def _commit_repo_state(self) -> None:
        subprocess.run(
            ["git", "-C", str(self.root), "add", "-A"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "--amend",
                "-m",
                "fixture",
            ],
            check=True,
            capture_output=True,
        )
        self.policy = subprocess.check_output(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.PIPE,
        ).strip()
        self.policy_ref = self.policy

    def invoke(self, *, human: bool = False) -> int:
        from review_sensei_github import cli as package_cli
        from review_sensei_github.api_run_requests import ApiRunRequestTransport
        from review_sensei_github.broker_client import BrokerClient
        from review_sensei_github.http import GitHubHttp

        fixture = self
        event_file = self.root / "event.json"
        if self.event_document is not None:
            event_file.write_text(json.dumps(self.event_document))

        def caller_factory(**kwargs):  # type: ignore[no-untyped-def]
            caller = ApiRunRequestTransport(**kwargs)
            fixture.accounted = caller
            fixture.transports.append(caller)
            return caller

        def broker_factory(**kwargs):  # type: ignore[no-untyped-def]
            return BrokerClient(
                broker_url="https://broker.github.test/api/github/token", **kwargs
            )

        def http_factory(**kwargs):  # type: ignore[no-untyped-def]
            http = GitHubHttp(api_url=fixture.api_url, **kwargs)
            fixture.http = http
            return http

        class DynamicProvider:
            def __init__(self, settings: object) -> None:
                self.name = str(getattr(settings, "name", "ollama"))
                self.model = getattr(settings, "model", None) or "qwen3.5:4b"

            def complete(self, request):  # type: ignore[no-untyped-def]
                fixture.provider_calls += 1
                if fixture.before_provider:
                    fixture.before_provider()
                comments = (
                    []
                    if not human
                    else [
                        {
                            "path": "src/app.py",
                            "line": 2,
                            "side": "RIGHT",
                            "body": "Confirm the external caller contract for this change.",
                            "severity": "high",
                            "blocking": False,
                            "needs_human": True,
                        }
                    ]
                )
                if fixture.provider_comments is not None:
                    comments = fixture.provider_comments
                return ProviderResponse(
                    text=json.dumps(
                        {
                            "summary": fixture.provider_summary or "Complete review.",
                            "comments": comments,
                        }
                    ),
                    provider=self.name,
                    model=self.model,
                )

        class DynamicRegistry:
            def create(self, settings):  # type: ignore[no-untyped-def]
                return DynamicProvider(settings)

        original_engine = review_run.engine_main
        original_execute = review_run.ReviewRun.execute
        original_publish = review_run.ReviewRunHooks.publish_review
        original_begin = review_run.ReviewRunHooks.begin_provider_phase

        def engine(*args, **kwargs):  # type: ignore[no-untyped-def]
            result = original_engine(*args, **kwargs)
            if fixture.after_engine:
                fixture.after_engine()
            return result

        def execute(selected, *args, **kwargs):  # type: ignore[no-untyped-def]
            fixture.selected = selected
            return original_execute(selected, *args, **kwargs)

        def publish(hooks, *args, **kwargs):  # type: ignore[no-untyped-def]
            if fixture.before_publication:
                fixture.before_publication()
            if fixture.active_finding_cap is not None:
                kwargs["options"] = replace(
                    kwargs["options"], max_active_findings=fixture.active_finding_cap
                )
            return original_publish(hooks, *args, **kwargs)

        def begin(hooks, *args, **kwargs):  # type: ignore[no-untyped-def]
            result = original_begin(hooks, *args, **kwargs)
            if fixture.before_entry:
                fixture.before_entry()
            return result

        run_directory = (
            self.override_run_directory
            if self.override_run_directory is not None
            else self.root
        )
        policy_commit = (
            self.override_policy_commit
            if self.override_policy_commit is not None
            else self.policy
        )
        extra_review_run: list[str] = []
        if self.recovery_context_dir is not None:
            extra_review_run.extend(
                ["--recovery-context-dir", str(self.recovery_context_dir)]
            )
        if self.recovery_context_hmac_path is not None:
            extra_review_run.extend(
                ["--recovery-context-key-file", str(self.recovery_context_hmac_path)]
            )
        if self.recovery_context_ttl_seconds is not None:
            extra_review_run.extend(
                [
                    "--recovery-context-ttl-seconds",
                    str(self.recovery_context_ttl_seconds),
                ]
            )

        env = {
            **fixture_process_environment(),
            "GITHUB_RUN_ID": str(self.run_id),
            "REVIEW_SENSEI_WORKFLOW_REF": "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@refs/tags/v5",
            "REVIEW_SENSEI_WORKFLOW_SHA": "a" * 40,
            "GITHUB_EVENT_NAME": self.event_name,
            "GITHUB_EVENT_PATH": str(event_file),
            "GITHUB_ACTOR": "alice",
            "GITHUB_TRIGGERING_ACTOR": "alice",
            "GITHUB_RUN_ATTEMPT": str(self.attempt),
            "GITHUB_TOKEN": "synthetic-workflow-read",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic",
            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.github.test/token?fixture=1",
            "OLLAMA_API_KEY": "synthetic-only",
            "REVIEWSENSEI_APP_ID": "999",
            **self.extra_env,
        }

        old = Path.cwd()
        errors = io.StringIO()
        try:
            os.chdir(self.root)
            with ExitStack() as stack:
                for scope in (
                    patch.dict(os.environ, env, clear=True),
                    patch.object(review_run, "physical_opener", self.external),
                    patch.object(
                        review_run, "ApiRunRequestTransport", side_effect=caller_factory
                    ),
                    patch.object(
                        review_run, "BrokerClient", side_effect=broker_factory
                    ),
                    patch.object(review_run, "GitHubHttp", side_effect=http_factory),
                    patch.object(review_run, "engine_main", side_effect=engine),
                    patch.object(review_run.ReviewRun, "execute", execute),
                    patch.object(review_run.ReviewRunHooks, "publish_review", publish),
                    patch.object(
                        review_run.ReviewRunHooks, "begin_provider_phase", begin
                    ),
                    patch(
                        "review_sensei.cli.default_registry",
                        return_value=DynamicRegistry(),
                    ),
                    forbid_replacement_phase_budget(),
                    redirect_stdout(io.StringIO()),
                    redirect_stderr(errors),
                ):
                    stack.enter_context(scope)
                result = package_cli.main(
                    [
                        "review-run",
                        "--policy-commit",
                        policy_commit,
                        "--run-directory",
                        str(run_directory),
                        "--publication-outcome",
                        str(self.root / "publication.json"),
                        *extra_review_run,
                        "--",
                        "--diff",
                        str(self.root / "review.patch"),
                        "--repository",
                        "owner/repo",
                        "--repository-id",
                        "123",
                        "--pull-request",
                        "7",
                        "--base-sha",
                        self.requested_base,
                        "--head-sha",
                        self.requested_head,
                        "--config",
                        str(self.root / ".reviewsensei.yml"),
                        "--review-mode",
                        "merge-focused",
                        "--no-learning-proposals",
                        "--learning-root",
                        str(self.root),
                        "--configuration-context-output",
                        str(self.root / "configuration.json"),
                        "--admission-context-output",
                        str(self.root / "admission.json"),
                        *self.extra_engine_args,
                    ]
                )
            self.error = errors.getvalue()
            return result
        finally:
            os.chdir(old)


def _setup_handoff(handoff_root: Path) -> tuple[Path, Path]:
    handoff_dir = handoff_root / "handoff"
    hmac_file = handoff_root / "handoff.hmac"
    hmac_file.write_bytes(HANDOFF_KEY)
    hmac_file.chmod(0o600)
    return handoff_dir, hmac_file


def _clear_local_diagnostics(root: Path) -> None:
    for name in ("configuration.json", "admission.json", "review.json"):
        (root / name).unlink(missing_ok=True)


def _custom_category_json(category_id: str = "storage") -> str:
    return json.dumps(
        {
            "id": category_id,
            "title": "Storage integrity",
            "focus": ["Lost writes and invalid transaction boundaries"],
            "applies_to": ["src/**"],
            "context": {
                "learnings": {
                    "categories": [category_id],
                    "include_uncategorized": False,
                },
                "documents": [{"path": "docs/storage.md", "required": True}],
            },
        }
    )


def _custom_stage_json(
    name: str = "Storage review", category_id: str = "storage"
) -> str:
    return json.dumps(
        {
            "name": name,
            "category_ids": [category_id],
            "outputs": ["summary", "comments"],
            "prompt_template": (
                "Categories:\n{review_categories}\n"
                "Context:\n{review_context}\n"
                "Diff:\n{diff}"
            ),
        }
    )


@unittest.skipUnless(os.name == "posix", "private recovery storage requires POSIX")
class Attempt2HostedRecoveryContextOverridesTests(unittest.TestCase):
    def _assert_attempt2_approved_and_closed(
        self,
        fx: _OverrideEntryFixture,
        *,
        expected_provider_calls: int = 1,
        expected_states: list[str] | None = None,
    ) -> None:
        self.assertEqual(fx.provider_calls, expected_provider_calls)
        self.assertEqual(
            [r["state"] for r in fx.reviews],
            expected_states
            if expected_states is not None
            else ["COMMENTED", "APPROVED"],
        )
        pub_outcome = json.loads(
            (fx.root / "publication.json").read_text(encoding="utf-8")
        )
        self.assertEqual(pub_outcome["status"], "published")
        self.assertTrue(pub_outcome["accounting_terminal"])
        assert fx.comment is not None
        final_record = parse_session_comment(fx.comment["body"], identity=IDENTITY)
        assert final_record.transaction is not None
        self.assertEqual(final_record.transaction.phase, "publication_succeeded")
        accounting = ApiRunAccounting.from_document(final_record.api_run_accounting)
        self.assertIsNotNone(accounting.terminal_transaction)

    def test_attempt2_recovers_with_env_provider_and_model_overrides(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            tempfile.TemporaryDirectory() as handoff_root,
        ):
            fx = _OverrideEntryFixture(directory)
            handoff_dir, hmac_file = _setup_handoff(Path(handoff_root))
            fx.recovery_context_dir = handoff_dir
            fx.recovery_context_hmac_path = hmac_file
            fx.recovery_context_ttl_seconds = 600
            fx.extra_env = {
                "REVIEWSENSEI_PROVIDER": "openai-compatible",
                "REVIEWSENSEI_MODEL": "gpt-4o",
                "OPENAI_API_KEY": "synthetic-openai-secret",
                "UNRELATED_CUSTOM_VAR": "ignore-me",
            }
            fx.before_publication = lambda: (_ for _ in ()).throw(
                OSError("simulated crash before publication")
            )
            self.assertEqual(fx.invoke(), 1, fx.error)
            self.assertEqual(fx.provider_calls, 1)
            self.assertEqual(fx.reviews, [])

            handoff_files = list(handoff_dir.glob("*.json"))
            self.assertEqual(len(handoff_files), 1)
            raw_handoff = handoff_files[0].read_text(encoding="utf-8")
            self.assertNotIn("synthetic-openai-secret", raw_handoff)
            self.assertNotIn("ignore-me", raw_handoff)
            bundle = json.loads(raw_handoff)["document"]["bundle"]
            self.assertEqual(
                bundle["provider_identity"],
                "openai-compatible:gpt-4o",
            )
            self.assertFalse(bundle["reconstructible_from_defaults"])
            self.assertEqual(bundle["runtime_only_fields"], ["cli_overrides"])

            _clear_local_diagnostics(fx.root)
            fx.before_publication = None
            fx.attempt = 2
            fx.override_run_directory = Path(handoff_root) / "attempt2-run"
            fx.override_run_directory.mkdir()
            # Attempt 2 does not even need the provider secret to recover retained work.
            fx.extra_env = {
                "REVIEWSENSEI_PROVIDER": "openai-compatible",
                "REVIEWSENSEI_MODEL": "gpt-4o",
            }
            self.assertEqual(fx.invoke(), 0, fx.error)
            self._assert_attempt2_approved_and_closed(fx)

    def test_attempt2_recovers_with_openrouter_env_overrides_and_respects_qualification_gate(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            tempfile.TemporaryDirectory() as handoff_root,
        ):
            fx = _OverrideEntryFixture(directory)
            handoff_dir, hmac_file = _setup_handoff(Path(handoff_root))
            fx.recovery_context_dir = handoff_dir
            fx.recovery_context_hmac_path = hmac_file
            fx.recovery_context_ttl_seconds = 600
            fx.extra_env = {
                "REVIEWSENSEI_PROVIDER": "openrouter",
                "REVIEWSENSEI_MODEL": "anthropic/claude-3.5-sonnet",
                "OPENROUTER_API_KEY": "synthetic-openrouter-secret",
                "UNRELATED_CUSTOM_VAR": "ignore-me",
            }
            fx.before_publication = lambda: (_ for _ in ()).throw(
                OSError("simulated crash before publication")
            )
            self.assertEqual(fx.invoke(), 1, fx.error)
            self.assertEqual(fx.provider_calls, 1)
            self.assertEqual(fx.reviews, [])

            handoff_files = list(handoff_dir.glob("*.json"))
            self.assertEqual(len(handoff_files), 1)
            raw_handoff = handoff_files[0].read_text(encoding="utf-8")
            self.assertNotIn("synthetic-openrouter-secret", raw_handoff)
            self.assertNotIn("ignore-me", raw_handoff)
            bundle = json.loads(raw_handoff)["document"]["bundle"]
            self.assertEqual(
                bundle["provider_identity"],
                "openrouter:anthropic/claude-3.5-sonnet",
            )
            self.assertFalse(bundle["reconstructible_from_defaults"])
            self.assertEqual(bundle["runtime_only_fields"], ["cli_overrides"])

            _clear_local_diagnostics(fx.root)
            fx.before_publication = None
            fx.attempt = 2
            fx.override_run_directory = Path(handoff_root) / "attempt2-run"
            fx.override_run_directory.mkdir()
            fx.extra_env = {
                "REVIEWSENSEI_PROVIDER": "openrouter",
                "REVIEWSENSEI_MODEL": "anthropic/claude-3.5-sonnet",
            }
            self.assertEqual(fx.invoke(), 0, fx.error)
            # OpenRouter without qualification evidence still recovers Attempt 1's review
            # with 0 extra provider calls, publishes COMMENTED, closes accounting as
            # confirmed withheld-approval (#471), and withholds unverified approval.
            self.assertEqual(fx.provider_calls, 1)
            self.assertEqual([r["state"] for r in fx.reviews], ["COMMENTED"])
            self.assertIn('"accounting_terminal":true', fx.error)
            self.assertIn('"diagnostic":"qualification_unverified"', fx.error)
            self.assertIn('"publication_status":"published"', fx.error)

    def test_attempt2_recovers_with_cli_provider_and_model_overrides(self) -> None:
        cases = (
            (
                "explicit-cli-provider-and-model",
                ["--provider", "openai-compatible", "--model", "gpt-4o"],
                {"OPENAI_API_KEY": "synthetic-openai-secret"},
                "openai-compatible:gpt-4o",
            ),
            (
                "cli-provider-with-env-model",
                ["--provider", "cloud-ollama"],
                {
                    "REVIEWSENSEI_MODEL": "qwen3.5:35b-a3b",
                    "OLLAMA_API_KEY": "synthetic-ollama-secret",
                },
                "ollama:qwen3.5:35b-a3b",
            ),
        )
        for label, cli_args, attempt1_env, expected_identity in cases:
            with (
                self.subTest(case=label),
                tempfile.TemporaryDirectory() as directory,
                tempfile.TemporaryDirectory() as handoff_root,
            ):
                fx = _OverrideEntryFixture(directory)
                handoff_dir, hmac_file = _setup_handoff(Path(handoff_root))
                fx.recovery_context_dir = handoff_dir
                fx.recovery_context_hmac_path = hmac_file
                fx.extra_engine_args = list(cli_args)
                fx.extra_env = dict(attempt1_env)
                fx.before_publication = lambda: (_ for _ in ()).throw(
                    OSError("simulated crash before publication")
                )
                self.assertEqual(fx.invoke(), 1, fx.error)
                self.assertEqual(fx.provider_calls, 1)
                self.assertEqual(fx.reviews, [])

                handoff_files = list(handoff_dir.glob("*.json"))
                self.assertEqual(len(handoff_files), 1)
                bundle = json.loads(handoff_files[0].read_text(encoding="utf-8"))[
                    "document"
                ]["bundle"]
                self.assertEqual(bundle["provider_identity"], expected_identity)
                self.assertEqual(bundle["runtime_only_fields"], ["cli_overrides"])

                _clear_local_diagnostics(fx.root)
                fx.before_publication = None
                fx.attempt = 2
                fx.override_run_directory = Path(handoff_root) / "attempt2-run"
                fx.override_run_directory.mkdir()
                self.assertEqual(fx.invoke(), 0, fx.error)
                self._assert_attempt2_approved_and_closed(fx)

    def test_attempt2_recovers_with_custom_stages_and_categories(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            tempfile.TemporaryDirectory() as handoff_root,
        ):
            extra_files = {
                "docs/storage.md": "Storage writes must be atomic.\n",
                ".reviewsensei/categories/storage.json": _custom_category_json(),
                ".reviewsensei/stages/10-storage.json": _custom_stage_json(),
            }
            fx = _OverrideEntryFixture(directory, extra_files=extra_files)
            handoff_dir, hmac_file = _setup_handoff(Path(handoff_root))
            fx.recovery_context_dir = handoff_dir
            fx.recovery_context_hmac_path = hmac_file
            fx.extra_engine_args = [
                "--stages-dir",
                ".reviewsensei/stages",
                "--categories-dir",
                ".reviewsensei/categories",
            ]
            fx.before_publication = lambda: (_ for _ in ()).throw(
                OSError("simulated crash before publication")
            )
            self.assertEqual(fx.invoke(), 1, fx.error)
            self.assertEqual(fx.provider_calls, 1)
            self.assertEqual(fx.reviews, [])

            saved_config_doc = json.loads(
                (fx.root / "configuration.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                [stage["name"] for stage in saved_config_doc["stages"]],
                ["Storage review"],
            )
            self.assertEqual(saved_config_doc["category_policy"], ["storage"])

            _clear_local_diagnostics(fx.root)
            fx.before_publication = None
            fx.attempt = 2
            fx.override_run_directory = Path(handoff_root) / "attempt2-run"
            fx.override_run_directory.mkdir()
            self.assertEqual(fx.invoke(), 0, fx.error)
            self._assert_attempt2_approved_and_closed(fx)

    def test_direct_reconstruction_loads_customization_directories_under_source_root(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".reviewsensei.yml").write_text(
                "schema: 1\ninference:\n  backend: local-ollama\n  model: qwen3.5:4b\n",
                encoding="utf-8",
            )
            (root / "docs").mkdir()
            (root / "docs" / "storage.md").write_text(
                "Storage invariant.\n", encoding="utf-8"
            )
            (root / ".reviewsensei" / "categories").mkdir(parents=True)
            (root / ".reviewsensei" / "categories" / "storage.json").write_text(
                _custom_category_json(), encoding="utf-8"
            )
            (root / ".reviewsensei" / "stages").mkdir(parents=True)
            (root / ".reviewsensei" / "stages" / "10-storage.json").write_text(
                _custom_stage_json(), encoding="utf-8"
            )
            (root / "review.patch").write_text(
                tx.HostedAnalysisCliTests.ANALYSIS_DIFF,
                encoding="utf-8",
                newline="\n",
            )
            argv = [
                "--config",
                str(root / ".reviewsensei.yml"),
                "--diff",
                str(root / "review.patch"),
                "--repository",
                IDENTITY.repository,
                "--pull-request",
                str(IDENTITY.pull_request),
                "--base-sha",
                BASE,
                "--head-sha",
                HEAD,
                "--transaction",
                "--github-session-ledger",
                "--repository-id",
                str(IDENTITY.repository_id),
                "--learning-root",
                str(root),
                "--stages-dir",
                str(root / ".reviewsensei" / "stages"),
                "--categories-dir",
                str(root / ".reviewsensei" / "categories"),
                "--no-learning-proposals",
                "--format",
                "json",
                "--output",
                str(root / "result.json"),
                "--configuration-context-output",
                str(root / "configuration.json"),
                "--admission-context-output",
                str(root / "admission.json"),
                "--outcome",
                str(root / "analysis-outcome.json"),
                "--exit-semantics",
                "operational",
            ]
            from review_sensei_github.review_hooks import GitHubReviewHooks

            from review_sensei import cli as engine_cli

            class LocalProvider:
                name = "ollama"
                model = "qwen3.5:4b"

                def complete(self, request):  # type: ignore[no-untyped-def]
                    return ProviderResponse(
                        text=json.dumps({"summary": "ok", "comments": []}),
                        provider=self.name,
                        model=self.model,
                    )

            class LocalRegistry:
                def create(self, settings):  # type: ignore[no-untyped-def]
                    return LocalProvider()

            with (
                patch.dict(os.environ, {}, clear=True),
                patch.object(
                    engine_cli, "default_registry", return_value=LocalRegistry()
                ),
                patch.object(
                    GitHubReviewHooks,
                    "resolve_ledger",
                    return_value=LocalSessionLedger(root / "ledger"),
                ),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(
                    engine_cli.main(argv, hosted_hooks=GitHubReviewHooks()), 0
                )

            reconstructed = reconstruct_publication_contexts(
                source_root=root,
                diff=(root / "review.patch").read_text(encoding="utf-8"),
                ledger=LocalSessionLedger(root / "ledger"),
                identity=IDENTITY,
                base_sha=BASE,
                head_sha=HEAD,
                policy=ReviewConvergencePolicy(),
            )
            self.assertEqual(
                reconstructed.configuration,
                json.loads((root / "configuration.json").read_text(encoding="utf-8")),
            )

    def test_attempt2_recovers_with_symbol_context_enabled(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            tempfile.TemporaryDirectory() as handoff_root,
        ):
            extra_files = {
                "src/app.py": "import helper\nkeep\nchange\n",
                "src/helper.py": "VALUE = 1\n",
            }
            fx = _OverrideEntryFixture(directory, extra_files=extra_files)
            handoff_dir, hmac_file = _setup_handoff(Path(handoff_root))
            fx.recovery_context_dir = handoff_dir
            fx.recovery_context_hmac_path = hmac_file
            fx.extra_engine_args = [
                "--enable-symbol-context",
                "--symbol-context-allowed-path",
                "src/**",
            ]
            fx.before_publication = lambda: (_ for _ in ()).throw(
                OSError("simulated crash before publication")
            )
            self.assertEqual(fx.invoke(), 1, fx.error)
            self.assertEqual(fx.provider_calls, 1)

            _clear_local_diagnostics(fx.root)
            fx.before_publication = None
            fx.attempt = 2
            fx.override_run_directory = Path(handoff_root) / "attempt2-run"
            fx.override_run_directory.mkdir()
            self.assertEqual(fx.invoke(), 0, fx.error)
            self._assert_attempt2_approved_and_closed(fx)

    def test_confirmed_evidence_reconstruction_and_missing_snapshot_refusal(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".reviewsensei.yml").write_text(
                "schema: 1\ninference:\n  backend: local-ollama\n  model: qwen3.5:4b\n",
                encoding="utf-8",
            )
            diff_text = tx.HostedAnalysisCliTests.ANALYSIS_DIFF
            snapshot = {"src/app.py": "keep\nchange\n"}
            snap_sha = _snapshot_digest(snapshot)

            from review_sensei.cli import (
                _CANONICAL_BACKEND_ADAPTERS,
                _configured_resource_budget,
                _transaction_provider_identity,
            )
            from review_sensei.configuration import (
                load_configuration,
                resolve_inference,
            )
            from review_sensei.context import (
                RepositoryContextStore,
                SymbolAwareContextPolicy,
                build_review_context_cache_key,
                build_review_context_selection,
            )
            from review_sensei.diff import analyze_diff
            from review_sensei.models import (
                ReviewRequest,
                build_transaction_configuration_context,
                transaction_stage_identity,
            )
            from review_sensei.outcomes import ResourceBudget
            from review_sensei.providers.registry import ProviderSettings
            from review_sensei.service import DEFAULT_STAGES, review_work_policy_digest
            from review_sensei.session import (
                prepare_review_transaction,
                session_reservation_id,
            )
            from review_sensei.validation import DEFAULT_REVIEW_LIMITS

            config = load_configuration(root / ".reviewsensei.yml")
            resolved = resolve_inference(config, environ={})
            settings = ProviderSettings(
                name=_CANONICAL_BACKEND_ADAPTERS[resolved.backend],
                model=resolved.model,
                base_url=resolved.base_url,
                timeout_seconds=resolved.timeout_seconds,
            )
            provider, model = _transaction_provider_identity(settings)
            budget = _configured_resource_budget(
                ResourceBudget.for_limits(DEFAULT_REVIEW_LIMITS), config
            )
            work_policy = review_work_policy_digest(
                config.advanced.review_work, budget, decision=config.decision
            )
            stage_ids, cat_ids = transaction_stage_identity(DEFAULT_STAGES)
            config_ctx = build_transaction_configuration_context(
                provider=provider,
                model=model,
                stages=stage_ids,
                category_policy=cat_ids,
                publication_mode="merge-focused",
                orchestration_enabled=False,
                work_policy_digest=work_policy,
            )
            evidence_ctx = {"evidence_policy": "confirmed", "snapshot_sha256": snap_sha}
            policy = ReviewConvergencePolicy()
            analysis = analyze_diff(diff_text, limits=DEFAULT_REVIEW_LIMITS)
            catalog = tuple(
                {c.id: c for s in DEFAULT_STAGES for c in s.categories}.values()
            )
            selection = build_review_context_selection(
                catalog,
                changed_paths=analysis.changed_paths,
                learnings=(),
                context_store=RepositoryContextStore(root),
                source_context_policy=SymbolAwareContextPolicy(enabled=False),
                snapshot=None,
                changed_lines=analysis.changed_lines,
            )
            key = build_review_context_cache_key(
                ReviewRequest(
                    diff=diff_text,
                    repository=IDENTITY.repository,
                    pull_request_number=IDENTITY.pull_request,
                    model=model,
                    learnings=(),
                    active_category_ids=selection.active_category_ids,
                    lens_contexts=selection.lens_contexts,
                    source_context=selection.source_context,
                    base_sha=BASE,
                    head_sha=HEAD,
                    work_policy_digest=work_policy,
                ),
                provider_name=settings.name,
                stages=DEFAULT_STAGES,
                profile="default",
                effective_model=model,
            )
            assert key is not None
            config_digest = ReviewTransaction.compute_configuration_digest(config_ctx)
            evidence_digest = ReviewTransaction.compute_evidence_digest(evidence_ctx)
            reservation = session_reservation_id(
                repository=IDENTITY.repository,
                pull_request=IDENTITY.pull_request,
                head_sha=HEAD,
                kind=(
                    "publish:"
                    + key.digest()
                    + ":"
                    + policy.digest()
                    + ":"
                    + config_digest
                    + ":"
                    + evidence_digest
                ),
            )
            ledger = LocalSessionLedger(root / "ledger")
            prepared = prepare_review_transaction(
                ledger,
                IDENTITY,
                policy,
                reservation_id=reservation,
                base_sha=BASE,
                head_sha=HEAD,
                configuration_digest=config_digest,
                evidence_digest=evidence_digest,
            )
            expected_result_sha = ReviewResult(
                summary="Confirmed review.",
                comments=(),
                provider="ollama",
                model=model,
                evidence_policy="confirmed",
            ).content_digest()
            result = ReviewResult(
                summary="Confirmed review.",
                comments=(),
                provider="ollama",
                model=model,
                evidence_policy="confirmed",
                transaction=replace(
                    prepared.transaction,
                    phase="publication_pending",
                    result_sha256=expected_result_sha,
                ),
            )
            ledger.replace(
                IDENTITY,
                lambda rec: rec.evolve(
                    reservation_id=None,
                    reserved_slot=None,
                    last_committed_reservation_id=reservation,
                    transaction=result.transaction,
                ),
            )

            # Valid confirmed recovery succeeds with matching snapshot_sha256 and snapshot.
            contexts = reconstruct_publication_contexts(
                source_root=root,
                diff=diff_text,
                ledger=ledger,
                identity=IDENTITY,
                base_sha=BASE,
                head_sha=HEAD,
                policy=policy,
                evidence_policy="confirmed",
                snapshot_sha256=snap_sha,
                snapshot=snapshot,
            )
            self.assertEqual(contexts.evidence, evidence_ctx)

            # Missing snapshot_sha256 refuses fail-closed (absent_context).
            with self.assertRaises(RecoveryRefusalError) as missing_exc:
                reconstruct_publication_contexts(
                    source_root=root,
                    diff=diff_text,
                    ledger=ledger,
                    identity=IDENTITY,
                    base_sha=BASE,
                    head_sha=HEAD,
                    policy=policy,
                    evidence_policy="confirmed",
                    snapshot_sha256=None,
                )
            self.assertEqual(missing_exc.exception.refusal_code, "absent_context")

            # Mismatched snapshot contents vs snapshot_sha256 refuses fail-closed.
            with self.assertRaises(RecoveryRefusalError) as tampered_snap:
                reconstruct_publication_contexts(
                    source_root=root,
                    diff=diff_text,
                    ledger=ledger,
                    identity=IDENTITY,
                    base_sha=BASE,
                    head_sha=HEAD,
                    policy=policy,
                    evidence_policy="confirmed",
                    snapshot_sha256=snap_sha,
                    snapshot={"src/app.py": "tampered\n"},
                )
            self.assertEqual(
                tampered_snap.exception.refusal_code,
                "snapshot_or_transaction_mismatch",
            )

            # Wrong snapshot_sha256 refuses with snapshot_or_transaction_mismatch.
            with self.assertRaises(RecoveryRefusalError) as wrong_sha:
                reconstruct_publication_contexts(
                    source_root=root,
                    diff=diff_text,
                    ledger=ledger,
                    identity=IDENTITY,
                    base_sha=BASE,
                    head_sha=HEAD,
                    policy=policy,
                    evidence_policy="confirmed",
                    snapshot_sha256="b" * 64,
                )
            self.assertEqual(
                wrong_sha.exception.refusal_code,
                "snapshot_or_transaction_mismatch",
            )

            # Legacy evidence_policy with snapshot_sha256 refuses fail-closed.
            with self.assertRaises(RecoveryRefusalError) as legacy_with_snap:
                reconstruct_publication_contexts(
                    source_root=root,
                    diff=diff_text,
                    ledger=ledger,
                    identity=IDENTITY,
                    base_sha=BASE,
                    head_sha=HEAD,
                    policy=policy,
                    evidence_policy="legacy",
                    snapshot_sha256=snap_sha,
                )
            self.assertEqual(
                legacy_with_snap.exception.refusal_code,
                "snapshot_or_transaction_mismatch",
            )

    def test_negative_controls_fail_closed_with_zero_native_writes(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            tempfile.TemporaryDirectory() as handoff_root,
        ):
            extra_files = {
                "docs/storage.md": "Storage writes must be atomic.\n",
                ".reviewsensei/categories/storage.json": _custom_category_json(),
                ".reviewsensei/stages/10-storage.json": _custom_stage_json(),
            }
            fx = _OverrideEntryFixture(directory, extra_files=extra_files)
            handoff_dir, hmac_file = _setup_handoff(Path(handoff_root))
            fx.recovery_context_dir = handoff_dir
            fx.recovery_context_hmac_path = hmac_file
            fx.extra_env = {
                "REVIEWSENSEI_PROVIDER": "openai-compatible",
                "REVIEWSENSEI_MODEL": "gpt-4o",
                "OPENAI_API_KEY": "synthetic-openai-secret",
            }
            fx.extra_engine_args = [
                "--stages-dir",
                ".reviewsensei/stages",
                "--categories-dir",
                ".reviewsensei/categories",
            ]
            fx.before_publication = lambda: (_ for _ in ()).throw(
                OSError("simulated crash before publication")
            )
            self.assertEqual(fx.invoke(), 1, fx.error)
            self.assertEqual(fx.provider_calls, 1)
            self.assertEqual(fx.reviews, [])
            _clear_local_diagnostics(fx.root)
            assert fx.comment is not None
            saved_comment = fx.comment["body"]
            saved_stage_bytes = (
                fx.root / ".reviewsensei" / "stages" / "10-storage.json"
            ).read_bytes()
            fx.before_publication = None
            fx.attempt = 2
            run_counter = [0]

            def reset_attempt2() -> None:
                assert fx.comment is not None
                fx.comment["body"] = saved_comment
                fx.trace = [entry for entry in fx.trace if entry[0] != 2]
                run_counter[0] += 1
                fresh_run = Path(handoff_root) / f"neg-run-{run_counter[0]}"
                fresh_run.mkdir(parents=True, exist_ok=True)
                fx.override_run_directory = fresh_run
                fx.override_policy_commit = None
                fx.extra_env = {
                    "REVIEWSENSEI_PROVIDER": "openai-compatible",
                    "REVIEWSENSEI_MODEL": "gpt-4o",
                }
                fx.extra_engine_args = [
                    "--stages-dir",
                    ".reviewsensei/stages",
                    "--categories-dir",
                    ".reviewsensei/categories",
                ]
                stage_path = fx.root / ".reviewsensei" / "stages" / "10-storage.json"
                stage_path.parent.mkdir(parents=True, exist_ok=True)
                stage_path.write_bytes(saved_stage_bytes)

            def assert_no_native_writes() -> None:
                self.assertEqual(fx.provider_calls, 1)
                self.assertEqual(fx.reviews, [])
                assert fx.comment is not None
                saved_record = parse_session_comment(saved_comment, identity=IDENTITY)
                current_record = parse_session_comment(
                    fx.comment["body"], identity=IDENTITY
                )
                self.assertEqual(current_record.transaction, saved_record.transaction)
                self.assertFalse(
                    any(
                        method in {"POST", "PATCH"} and "/pulls/" in path
                        for attempt_num, method, path in fx.trace
                        if attempt_num == 2 and "/reviews" in path
                    )
                )

            # 1. Changed REVIEWSENSEI_MODEL on Attempt 2 -> snapshot_or_transaction_mismatch
            reset_attempt2()
            fx.extra_env["REVIEWSENSEI_MODEL"] = "gpt-4.1"
            self.assertEqual(fx.invoke(), 1)
            self.assertIn("original committed transaction", fx.error)
            assert_no_native_writes()

            # 2. Dropped overrides on Attempt 2 -> snapshot_or_transaction_mismatch
            reset_attempt2()
            fx.extra_env = {}
            self.assertEqual(fx.invoke(), 1)
            self.assertIn("original committed transaction", fx.error)
            assert_no_native_writes()

            # 3. Tampered custom stage JSON -> snapshot_or_transaction_mismatch
            reset_attempt2()
            (fx.root / ".reviewsensei" / "stages" / "10-storage.json").write_text(
                _custom_stage_json(name="Tampered storage review"),
                encoding="utf-8",
            )
            self.assertEqual(fx.invoke(), 1)
            self.assertIn("original committed transaction", fx.error)
            assert_no_native_writes()

            # 4. Missing custom stage file -> fails closed (no stage files)
            reset_attempt2()
            (fx.root / ".reviewsensei" / "stages" / "10-storage.json").unlink()
            self.assertEqual(fx.invoke(), 1)
            self.assertIn(
                "stages directory must contain at least one JSON file", fx.error
            )
            assert_no_native_writes()

            # 5. Mismatched policy_commit on Attempt 2 -> fails closed
            reset_attempt2()
            fx.override_policy_commit = "0" * 40
            self.assertEqual(fx.invoke(), 1)
            assert_no_native_writes()

            # 6. Direct reconstruct_publication_contexts with mismatched policy_commit -> snapshot_or_transaction_mismatch
            reset_attempt2()
            store = RecoveryContextStore(handoff_dir, key=HANDOFF_KEY)
            pending_record = parse_session_comment(saved_comment, identity=IDENTITY)
            assert pending_record.transaction is not None
            diff_text = (fx.root / "review.patch").read_text(encoding="utf-8")
            with self.assertRaises(RecoveryRefusalError) as commit_exc:
                reconstruct_publication_contexts(
                    source_root=fx.root,
                    diff=diff_text,
                    ledger=LocalSessionLedger(fx.root / "unused")
                    if False
                    else _SingleRecordLedger(pending_record),  # type: ignore[arg-type]
                    identity=IDENTITY,
                    base_sha=BASE,
                    head_sha=HEAD,
                    policy=ReviewConvergencePolicy(),
                    environ=fx.extra_env,
                    recovery_store=store,
                    policy_commit="0" * 40,
                )
            self.assertEqual(
                commit_exc.exception.refusal_code, "snapshot_or_transaction_mismatch"
            )

            # 7. Path traversal outside source_root for stages_dir fails closed
            outside = Path(handoff_root) / "outside_stages"
            outside.mkdir()
            (outside / "10-storage.json").write_bytes(saved_stage_bytes)
            with self.assertRaisesRegex(
                ReviewInputError, "contained inside trusted source_root"
            ):
                reconstruct_publication_contexts(
                    source_root=fx.root,
                    diff=diff_text,
                    ledger=_SingleRecordLedger(pending_record),  # type: ignore[arg-type]
                    identity=IDENTITY,
                    base_sha=BASE,
                    head_sha=HEAD,
                    policy=ReviewConvergencePolicy(),
                    stages_dir=outside,
                )

            # 8. Symlinked symbol/stage/custom dir fails closed
            link_stages = fx.root / "linked_stages"
            os.symlink(fx.root / ".reviewsensei" / "stages", link_stages)
            with self.assertRaisesRegex(ReviewInputError, "symbolic link"):
                reconstruct_publication_contexts(
                    source_root=fx.root,
                    diff=diff_text,
                    ledger=_SingleRecordLedger(pending_record),  # type: ignore[arg-type]
                    identity=IDENTITY,
                    base_sha=BASE,
                    head_sha=HEAD,
                    policy=ReviewConvergencePolicy(),
                    stages_dir=link_stages,
                )

            # 9. Valid Attempt 2 still recovers after negative controls
            reset_attempt2()
            self.assertEqual(fx.invoke(), 0, fx.error)
            self._assert_attempt2_approved_and_closed(fx)

    def test_default_environ_none_ignores_host_env_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".reviewsensei.yml").write_text(
                "schema: 1\ninference:\n  backend: local-ollama\n  model: qwen3.5:4b\n",
                encoding="utf-8",
            )
            (root / "review.patch").write_text(
                tx.HostedAnalysisCliTests.ANALYSIS_DIFF,
                encoding="utf-8",
                newline="\n",
            )
            from review_sensei_github.review_hooks import GitHubReviewHooks

            from review_sensei import cli as engine_cli

            class LocalProvider:
                name = "ollama"
                model = "qwen3.5:4b"

                def complete(self, request):  # type: ignore[no-untyped-def]
                    return ProviderResponse(
                        text=json.dumps({"summary": "ok", "comments": []}),
                        provider=self.name,
                        model=self.model,
                    )

            class LocalRegistry:
                def create(self, settings):  # type: ignore[no-untyped-def]
                    return LocalProvider()

            argv = [
                "--config",
                str(root / ".reviewsensei.yml"),
                "--diff",
                str(root / "review.patch"),
                "--repository",
                IDENTITY.repository,
                "--pull-request",
                str(IDENTITY.pull_request),
                "--base-sha",
                BASE,
                "--head-sha",
                HEAD,
                "--transaction",
                "--github-session-ledger",
                "--repository-id",
                str(IDENTITY.repository_id),
                "--learning-root",
                str(root),
                "--no-learning-proposals",
                "--format",
                "json",
                "--output",
                str(root / "result.json"),
                "--configuration-context-output",
                str(root / "configuration.json"),
                "--admission-context-output",
                str(root / "admission.json"),
                "--outcome",
                str(root / "analysis-outcome.json"),
                "--exit-semantics",
                "operational",
            ]
            with (
                patch.dict(os.environ, {}, clear=True),
                patch.object(
                    engine_cli, "default_registry", return_value=LocalRegistry()
                ),
                patch.object(
                    GitHubReviewHooks,
                    "resolve_ledger",
                    return_value=LocalSessionLedger(root / "ledger"),
                ),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(
                    engine_cli.main(argv, hosted_hooks=GitHubReviewHooks()), 0
                )

            # Even when os.environ carries overrides, calling reconstruct_publication_contexts
            # with default environ=None stays pinned to `{}` and reconstructs YAML defaults.
            with patch.dict(
                os.environ,
                {
                    "REVIEWSENSEI_PROVIDER": "openrouter",
                    "REVIEWSENSEI_MODEL": "anthropic/claude-3.5-sonnet",
                },
                clear=True,
            ):
                reconstructed = reconstruct_publication_contexts(
                    source_root=root,
                    diff=(root / "review.patch").read_text(encoding="utf-8"),
                    ledger=LocalSessionLedger(root / "ledger"),
                    identity=IDENTITY,
                    base_sha=BASE,
                    head_sha=HEAD,
                    policy=ReviewConvergencePolicy(),
                )
            self.assertEqual(reconstructed.configuration["model"], "qwen3.5:4b")

            # Also verify reconstruct_or_prescribe_publication_contexts respects missing snapshot_sha256
            _, refusal = reconstruct_or_prescribe_publication_contexts(
                source_root=root,
                diff=(root / "review.patch").read_text(encoding="utf-8"),
                ledger=LocalSessionLedger(root / "ledger"),
                identity=IDENTITY,
                base_ref="main",
                base_sha=BASE,
                head_sha=HEAD,
                policy_commit="e" * 40,
                policy=ReviewConvergencePolicy(),
                evidence_policy="confirmed",
                snapshot_sha256=None,
            )
            self.assertFalse(refusal.admitted)
            self.assertEqual(refusal.refusal_code, "absent_context")


class _SingleRecordLedger:
    def __init__(self, record):  # type: ignore[no-untyped-def]
        self._record = record

    def load(self, identity):  # type: ignore[no-untyped-def]
        from review_sensei.session import SessionLoadResult

        return SessionLoadResult(status="ok", record=self._record)

    def read_baseline(self, record):  # type: ignore[no-untyped-def]
        return None


if __name__ == "__main__":
    unittest.main()
