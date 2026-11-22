"""Actual CLI policy and retained-result routing before provider construction."""

import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from review_sensei_github.cli import main

from review_sensei.models import ReviewResult


class PublicationCliRecoveryTests(unittest.TestCase):
    def invoke(
        self,
        *,
        retained=False,
        artifact=False,
        policy="complete",
        operation="enabled",
        thread_resolution=None,
        diagnostic=None,
    ):
        import review_sensei_github as github

        calls = []
        result = ReviewResult(
            summary="Validated complete result.", comments=(), provider="fixture"
        )

        class Application:
            def __init__(self, **kwargs):
                pass

            def recover_retained_review_result(self, **kwargs):
                calls.append(("recover", kwargs))
                return result

            def publish_review(self, **kwargs):
                calls.append(("publish", kwargs))
                return SimpleNamespace(
                    status="published",
                    thread_resolution=thread_resolution,
                    diagnostic=diagnostic,
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "result.json").write_text(json.dumps(result.to_dict()))
            (root / "diff.patch").write_text(
                "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n",
                encoding="utf-8",
                newline="\n",
            )
            (root / "context.json").write_text('{"fixture":"trusted"}')
            (root / "config.yml").write_text(
                f"schema: 1\ngithub:\n  publication_inventory: {policy}\n  operation_entry: {operation}\n"
            )
            source = (
                ["--recover-retained"]
                if retained
                else ["--result", str(root / "result.json")]
            )
            if artifact:
                source = ["--recover-from", str(root / "result.json")]
            arguments = [
                "publish",
                *source,
                "--diff",
                str(root / "diff.patch"),
                "--repository",
                "owner/repo",
                "--repository-id",
                "7",
                "--pull-request",
                "11",
                "--head-sha",
                "a" * 40,
                "--base-sha",
                "b" * 40,
                "--base-branch",
                "main",
                "--session-ledger",
                str(root / "ledger.json"),
                "--config",
                str(root / "config.yml"),
                "--configuration-context",
                str(root / "context.json"),
                "--evidence-context",
                str(root / "context.json"),
                "--allow-write",
                "--enable-review",
                "--outcome",
                str(root / "outcome.json"),
            ]
            stderr = StringIO()
            stdout = StringIO()
            with (
                patch.dict(
                    os.environ, {"GITHUB_TOKEN": "fixture-metadata"}, clear=True
                ),
                patch.multiple(
                    github,
                    BrokerClient=lambda: object(),
                    GitHubHttp=lambda: object(),
                    ReviewPublisher=lambda **kwargs: object(),
                    LearningPRPublisher=lambda **kwargs: object(),
                    ConversationPublisher=lambda **kwargs: object(),
                    GitHubApplication=Application,
                ),
                patch(
                    "socket.socket.connect",
                    side_effect=AssertionError("network forbidden"),
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                code = main(arguments)
            self.stderr = stderr.getvalue()
            self.stdout = stdout.getvalue()
            self.outcome = (
                json.loads((root / "outcome.json").read_text())
                if (root / "outcome.json").exists()
                else None
            )
        return code, calls

    def test_trusted_config_reaches_actual_publication_options(self):
        code, calls = self.invoke(operation="disabled")
        self.assertEqual(code, 0)
        self.assertEqual([name for name, _ in calls], ["publish"])
        options = calls[0][1]["options"]
        self.assertTrue(options.complete_inventory)
        self.assertEqual(options.operation_entry, "disabled")

    def test_retained_recovery_and_publication_share_one_attempt_budget(self):
        code, calls = self.invoke(retained=True)
        self.assertEqual(code, 0)
        self.assertEqual([name for name, _ in calls], ["recover", "publish"])
        self.assertIs(calls[0][1]["budget"], calls[1][1]["publication_budget"])
        self.assertIsNotNone(calls[0][1]["ledger"])
        self.assertEqual(calls[0][1]["token"], "fixture-metadata")

    def test_legacy_artifact_does_not_silently_ignore_publication_profile(self):
        code, calls = self.invoke(artifact=True)
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])
        self.assertIn("does not accept publication profile settings", self.stderr)

    def test_unconfirmed_cleanup_reports_publication_only_retry_without_changing_outcome(
        self,
    ):
        message = (
            "review-sensei: old-thread cleanup is unconfirmed or incomplete; "
            "use --result with the exact original result and trusted contexts "
            "for this head and base to continue cleanup; "
            "no inference is needed.\n"
        )
        for diagnostic in ("required_fixes_open", "human_adjudication_open"):
            for resolution in (None, "resolved", "unconfirmed", "untrusted\ntext"):
                with self.subTest(diagnostic=diagnostic, resolution=resolution):
                    code, calls = self.invoke(
                        diagnostic=diagnostic, thread_resolution=resolution
                    )
                    self.assertEqual(code, 0)
                    self.assertEqual([name for name, _ in calls], ["publish"])
                    self.assertEqual(self.stdout, "published\n")
                    self.assertEqual(self.outcome["status"], "partial")
                    self.assertEqual(self.outcome["diagnostic"], diagnostic)
                    self.assertNotIn("thread_resolution", self.outcome)
                    self.assertEqual(
                        self.stderr,
                        f"review-sensei: partial: {diagnostic}\n"
                        + (message if resolution == "unconfirmed" else ""),
                    )

    def test_standalone_publish_refuses_metered_ledger_without_recovery_dir(self):
        import review_sensei_github as github

        from review_sensei.api_work import (
            ApiExecutionOwner,
            ApiRunIdentity,
            FrozenApiConfiguration,
            StartupPlan,
            StartupSlot,
            initial_accounting,
        )
        from review_sensei.session import LocalSessionLedger, SessionIdentity

        accounting = initial_accounting(
            ApiRunIdentity(7, 456),
            FrozenApiConfiguration("a" * 40, "b" * 64, 384),
            ApiExecutionOwner(1, "hosted", 0),
            StartupPlan(
                (
                    StartupSlot("hosted", 1, ("configuration",)),
                    StartupSlot("hosted", 2, ("configuration",)),
                )
            ),
            prior_nonstartup_liability_upper_bound=10,
        ).reserve("retained", 10)
        result_doc = ReviewResult(
            summary="Validated complete result.", comments=(), provider="fixture"
        ).to_dict()
        for source_args in (["--recover-retained"], ["--result", "RESULT_PATH"]):
            with (
                self.subTest(source=source_args[0]),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                calls = []
                (root / "result.json").write_text(
                    json.dumps(result_doc), encoding="utf-8"
                )
                (root / "diff.patch").write_text(
                    "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n",
                    encoding="utf-8",
                    newline="\n",
                )
                (root / "context.json").write_text('{"fixture":"trusted"}')
                (root / "config.yml").write_text(
                    "schema: 1\ngithub:\n  publication_inventory: complete\n  operation_entry: disabled\n"
                )
                ledger_dir = root / "ledger"
                ledger = LocalSessionLedger(ledger_dir)
                identity = SessionIdentity("owner/repo", 11, repository_id=7)
                ledger.initialize(identity)
                ledger.replace(
                    identity,
                    lambda rec: rec.evolve(api_run_accounting=accounting.to_document()),
                )
                resolved_source = [
                    str(root / "result.json") if item == "RESULT_PATH" else item
                    for item in source_args
                ]

                class Application:
                    def __init__(self, **kwargs):
                        pass

                    def recover_retained_review_result(self, **kwargs):
                        calls.append("recover")
                        return ReviewResult.from_dict(result_doc)

                    def publish_review(self, **kwargs):
                        calls.append("publish")
                        return SimpleNamespace(status="published")

                arguments = [
                    "publish",
                    *resolved_source,
                    "--diff",
                    str(root / "diff.patch"),
                    "--repository",
                    "owner/repo",
                    "--repository-id",
                    "7",
                    "--pull-request",
                    "11",
                    "--head-sha",
                    "a" * 40,
                    "--base-sha",
                    "b" * 40,
                    "--base-branch",
                    "main",
                    "--session-ledger",
                    str(ledger_dir),
                    "--config",
                    str(root / "config.yml"),
                    "--configuration-context",
                    str(root / "context.json"),
                    "--evidence-context",
                    str(root / "context.json"),
                    "--allow-write",
                    "--enable-review",
                ]
                stderr = StringIO()
                with (
                    patch.dict(
                        os.environ, {"GITHUB_TOKEN": "fixture-metadata"}, clear=True
                    ),
                    patch.multiple(
                        github,
                        BrokerClient=lambda: object(),
                        GitHubHttp=lambda: object(),
                        ReviewPublisher=lambda **kwargs: object(),
                        LearningPRPublisher=lambda **kwargs: object(),
                        ConversationPublisher=lambda **kwargs: object(),
                        GitHubApplication=Application,
                    ),
                    redirect_stdout(StringIO()),
                    redirect_stderr(stderr),
                ):
                    code = main(arguments)
                self.assertEqual(code, 1)
                self.assertIn(
                    "original_caller_accounting_transport_required", stderr.getvalue()
                )
                self.assertEqual(calls, [])

    def test_standalone_publish_forwards_pr473_reconstruction_flags(self):
        import review_sensei_github as github
        from review_sensei_github import publication_context_recovery
        from review_sensei_github.publication_context_recovery import (
            ReconstructedPublicationContexts,
        )

        from review_sensei.context import ReviewContextCacheKey

        captured = []
        result = ReviewResult(
            summary="Validated complete result.", comments=(), provider="fixture"
        )

        def fake_reconstruct(**kwargs):
            captured.append(kwargs)
            return ReconstructedPublicationContexts(
                configuration={"provider": "openai-compatible", "model": "gpt-4o"},
                evidence={"evidence_policy": "legacy", "snapshot_sha256": None},
                baseline=None,
                current_key=ReviewContextCacheKey(
                    repository="owner/repo",
                    pull_request=11,
                    base_sha="b" * 40,
                    head_sha="a" * 40,
                    engine="openai-compatible",
                    model="gpt-4o",
                    profile="default",
                    stage_digest="c" * 64,
                    context_digest="d" * 64,
                    learning_digest="e" * 64,
                ),
            )

        class Application:
            def __init__(self, **kwargs):
                pass

            def recover_retained_review_result(self, **kwargs):
                return result

            def publish_review(self, **kwargs):
                return SimpleNamespace(status="published")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "diff.patch").write_text(
                "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n",
                encoding="utf-8",
                newline="\n",
            )
            (root / "config.yml").write_text(
                "schema: 1\ngithub:\n  publication_inventory: complete\n  operation_entry: enabled\n"
            )
            (root / "stages").mkdir()
            (root / "categories").mkdir()
            arguments = [
                "publish",
                "--recover-retained",
                "--reconstruct-contexts",
                "--trusted-context-root",
                str(root),
                "--diff",
                str(root / "diff.patch"),
                "--repository",
                "owner/repo",
                "--repository-id",
                "7",
                "--pull-request",
                "11",
                "--head-sha",
                "a" * 40,
                "--base-sha",
                "b" * 40,
                "--base-branch",
                "main",
                "--session-ledger",
                str(root / "ledger"),
                "--config",
                str(root / "config.yml"),
                "--provider",
                "openai-compatible",
                "--model",
                "gpt-4o",
                "--stages-dir",
                str(root / "stages"),
                "--categories-dir",
                str(root / "categories"),
                "--enable-symbol-context",
                "--symbol-context-allowed-path",
                "src/**",
                "--symbol-context-max-files",
                "4",
                "--symbol-context-max-bytes",
                "4096",
                "--symbol-context-max-depth",
                "2",
                "--orchestrate-large-changes",
                "--allow-write",
                "--enable-review",
            ]
            stderr = StringIO()
            with (
                patch.dict(
                    os.environ, {"GITHUB_TOKEN": "fixture-metadata"}, clear=True
                ),
                patch.multiple(
                    github,
                    BrokerClient=lambda: object(),
                    GitHubHttp=lambda: object(),
                    ReviewPublisher=lambda **kwargs: object(),
                    LearningPRPublisher=lambda **kwargs: object(),
                    ConversationPublisher=lambda **kwargs: object(),
                    GitHubApplication=Application,
                ),
                patch.object(
                    publication_context_recovery,
                    "reconstruct_publication_contexts",
                    side_effect=fake_reconstruct,
                ),
                redirect_stdout(StringIO()),
                redirect_stderr(stderr),
            ):
                code = main(arguments)
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertEqual(len(captured), 1)
        kwargs = captured[0]
        self.assertEqual(kwargs["cli_provider"], "openai-compatible")
        self.assertEqual(kwargs["cli_model"], "gpt-4o")
        self.assertEqual(kwargs["stages_dir"], root / "stages")
        self.assertEqual(kwargs["categories_dir"], root / "categories")
        self.assertTrue(kwargs["enable_symbol_context"])
        self.assertEqual(kwargs["symbol_context_allowed_paths"], ("src/**",))
        self.assertEqual(kwargs["symbol_context_max_files"], 4)
        self.assertEqual(kwargs["symbol_context_max_bytes"], 4096)
        self.assertEqual(kwargs["symbol_context_max_depth"], 2)
        self.assertTrue(kwargs["orchestrate_large_changes"])
        self.assertEqual(
            kwargs["runtime_only_flags"],
            {"custom_stages": True, "cli_overrides": True},
        )


if __name__ == "__main__":
    unittest.main()
