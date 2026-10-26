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


if __name__ == "__main__":
    unittest.main()
