"""Selected review steps retain a single process and cancellation forwarding."""

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


class CallerWorkflowTests(unittest.TestCase):
    def test_review_steps_own_publication_and_cleanup(self):
        text = Path(".github/workflows/review-sensei-run.yml").read_text()
        for name in ("Run hosted review", "Run local review"):
            step = text.split("      - name: " + name + "\n", 1)[1].split(
                "      - name:", 1
            )[0]
            self.assertEqual(step.count('" review-run '), 1)
            self.assertIn('--policy-commit "$POLICY_COMMIT"', step)
            self.assertIn("--publication-outcome publication-outcome.json", step)
            self.assertIn('kill -TERM "$review_pid"', step)
            self.assertNotIn("curl ", step)
            self.assertNotIn("ollama show", step)
        self.assertNotIn('" release-reservation ', text)
        self.assertIn("Upload diagnostics artifact", text)

    @unittest.skipIf(os.name == "nt", "Actions runner scripts use Bash")
    def test_normal_review_routing_and_preflight_make_no_business_http(self):
        from tests.test_ci_policy import _run_block_containing

        caller = Path(".github/workflows/review-sensei-review.yml").read_text()
        runner = Path(".github/workflows/review-sensei-run.yml").read_text()
        route = _run_block_containing(
            caller, 'pull_json="$RUNNER_TEMP/review-sensei-pull.json"'
        )
        preflight = _run_block_containing(
            runner, "Live review preflight belongs to the metered lifecycle."
        )
        for event, body, script in (
            ("workflow_dispatch", "", route),
            ("issue_comment", "@reviewsensei rescan commit " + "a" * 40, route),
            ("issue_comment", "@reviewsensei review remaining", route),
            ("pull_request", "", preflight),
        ):
            with (
                self.subTest(event=event, body=body),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                gh = root / "gh"
                gh.write_text("#!/bin/sh\necho forbidden-http >&2\nexit 99\n")
                gh.chmod(0o755)
                output = root / "outputs"
                result = subprocess.run(
                    ["bash", "-e", "-c", textwrap.dedent(script)],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    env={
                        "PATH": str(root) + os.pathsep + os.environ["PATH"],
                        "RUNNER_TEMP": directory,
                        "GITHUB_OUTPUT": str(output),
                        "EVENT_NAME": event,
                        "COMMENT_BODY": body,
                        "PULL_REQUEST": "7",
                        "REPOSITORY": "owner/repo",
                        "REPOSITORY_ID": "123",
                        "DEFAULT_BRANCH": "main",
                        "RECOVER_PUBLICATION": "false",
                        "OPERATION": "review",
                    },
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("forbidden-http", result.stderr)
                self.assertIn("pull_request_number=7", output.read_text())
