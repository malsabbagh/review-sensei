"""Comment routing and guidance from installed engine/adapter wheels only."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from importlib.resources import files
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TESTS_ROOT))

from packaging_guard import assert_distribution_import  # noqa: E402

assert_distribution_import()

from review_sensei_github import trigger  # noqa: E402
from review_sensei_github.setup import SetupPlanBuilder  # noqa: E402

from review_sensei.presentation import MAINTAINER_RESPONSE_INSTRUCTIONS  # noqa: E402


class InstalledCommentCommandTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(
            Path(trigger.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
        )

    def resolve_in_fresh_process(self, body):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            pull = root / "pull.json"
            pull.write_text(
                json.dumps(
                    {
                        "number": 7,
                        "head": {"sha": "b" * 40, "ref": "feature/commands"},
                        "base": {"sha": "a" * 40, "ref": "main"},
                    }
                )
            )
            output = root / "output"
            environment = {
                key: value
                for key, value in os.environ.items()
                if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"}
            }
            environment["GITHUB_OUTPUT"] = str(output)
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(Path(trigger.__file__).resolve()),
                    "--event",
                    "issue_comment",
                    "--comment-body",
                    body,
                    "--pull-json",
                    str(pull),
                ],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )
            return result, output.read_text() if output.exists() else None

    def test_installed_cli_separates_explicit_commands_from_reports_and_examples(self):
        for body, operation in (
            ("@reviewsensei rescan commit BBBBBBB", "review"),
            ("@sensei re-scan", "review"),
            ("@reviewsensei verify", "command"),
            ("@sensei The previous rescan completed. " + "x" * 8702, "reply"),
            ("> @reviewsensei rescan", "reply"),
            ("```\n@reviewsensei rescan\n```", "reply"),
            ("@reviewsensei rescan commit invalid", "reply"),
        ):
            with self.subTest(body=body[:100]):
                result, output = self.resolve_in_fresh_process(body)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"operation={operation}\n", output)
                self.assertIn("head_sha=" + "b" * 40, output)

    def test_installed_cli_rejects_oversized_command_before_writing_outputs(self):
        result, output = self.resolve_in_fresh_process(
            "@reviewsensei rescan" + "\u3000" * 1400
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("exceeds 4096 UTF-8 bytes", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIsNone(output)

    def test_installed_guidance_and_frozen_caller_asset_are_available(self):
        prior = (
            files("review_sensei_github.fixtures")
            .joinpath("released-v5-keyword-rescan-caller.yml")
            .read_bytes()
        )
        self.assertEqual(
            hashlib.sha256(prior).hexdigest(),
            "cea8cc8e3905d30c973a76ec42afb1e999032f63c678e8f78e208ac110e7bbaf",
        )
        caller = SetupPlanBuilder().build("owner/repo").files[0].content
        self.assertNotEqual(caller, prior.decode())
        self.assertFalse(
            trigger.issue_comment_requests_rescan(MAINTAINER_RESPONSE_INSTRUCTIONS)
        )
        self.assertIn(
            "Editing a comment does not trigger", MAINTAINER_RESPONSE_INSTRUCTIONS
        )
        self.assertIn(
            "does not request a fresh review", MAINTAINER_RESPONSE_INSTRUCTIONS
        )


if __name__ == "__main__":
    unittest.main()
