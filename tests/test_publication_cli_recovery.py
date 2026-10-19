"""Actual CLI policy and retained-result routing before provider construction."""

import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from review_sensei_github.cli import main

from review_sensei.models import ReviewResult


class PublicationCliRecoveryTests(unittest.TestCase):
    def invoke(self, *, retained=False, policy="complete", operation="enabled"):
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
                return SimpleNamespace(status="published")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "result.json").write_text(json.dumps(result.to_dict()))
            (root / "diff.patch").write_text(
                "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
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
            arguments = [
                "github",
                "review",
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
            ]
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
                redirect_stdout(StringIO()),
            ):
                code = main(arguments)
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


if __name__ == "__main__":
    unittest.main()
