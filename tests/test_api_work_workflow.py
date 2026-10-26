"""Selected review steps retain a single process and cancellation forwarding."""

import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


class CallerWorkflowTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "Actions runner scripts use Bash")
    def test_command_and_reply_lanes_use_one_policy_bound_process_without_shell_http(
        self,
    ):
        from tests.test_ci_policy import _job_section, _run_block_containing

        runner = Path(".github/workflows/review-sensei-run.yml").read_text()
        routes = (
            ("command", "command-run"),
            ("hosted", "reply-run"),
            ("local", "reply-run"),
        )
        for job, entry in routes:
            section = _job_section(runner, job)
            script = _run_block_containing(section, 'review-sensei-github" ' + entry)
            self.assertEqual(script.count('review-sensei-github" ' + entry), 1)
            self.assertIn('--policy-commit "$POLICY_COMMIT"', script)
            self.assertIn(
                "REVIEW_SENSEI_WORKFLOW_REF: ${{ job.workflow_ref }}", section
            )
            self.assertIn(
                "REVIEW_SENSEI_WORKFLOW_SHA: ${{ job.workflow_sha }}", section
            )
            self.assertNotIn("curl ", script)
            self.assertNotIn("gh api", script)
            self.assertNotIn("ollama show", script)
            for status, exit_code in (
                (("replied", 0), ("unrecognized", 1))
                if entry == "reply-run"
                else (("paused", 0),)
            ):
                with (
                    self.subTest(job=job, status=status),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    root = Path(directory)
                    executable = root / "review-sensei-venv/bin/review-sensei-github"
                    executable.parent.mkdir(parents=True)
                    executable.write_text(
                        "#!/usr/bin/env python3\nimport json,os,sys\n"
                        "open(os.environ['CAPTURE'], 'w').write(json.dumps({'args':sys.argv[1:],"
                        "'policy':os.environ['POLICY_COMMIT'],'workflow':os.environ['REVIEW_SENSEI_WORKFLOW_REF'],"
                        "'sha':os.environ['REVIEW_SENSEI_WORKFLOW_SHA']}))\n"
                        "print(os.environ['FAKE_STATUS'])\n"
                    )
                    executable.chmod(0o755)
                    for tool in ("gh", "curl", "ollama"):
                        path = root / tool
                        path.write_text("#!/bin/sh\necho forbidden-http >&2\nexit 99\n")
                        path.chmod(0o755)
                    capture = root / "capture.json"
                    environment = {
                        "PATH": str(root) + os.pathsep + os.environ["PATH"],
                        "RUNNER_TEMP": directory,
                        "GITHUB_OUTPUT": str(root / "output"),
                        "CAPTURE": str(capture),
                        "FAKE_STATUS": status,
                        "POLICY_COMMIT": "c" * 40,
                        "REVIEW_SENSEI_WORKFLOW_REF": "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@refs/tags/v5",
                        "REVIEW_SENSEI_WORKFLOW_SHA": "a" * 40,
                        "BACKEND": "ollama",
                        "MODEL": "fixture-model",
                        "BASE_URL": "https://provider.invalid/api",
                        "ALLOW_CUSTOM_ENDPOINT": "false",
                        "CREDENTIAL_REQUIRED": "false",
                        "REPOSITORY": "owner/repo",
                        "REPOSITORY_ID": "123",
                        "PULL_REQUEST": "7",
                        "HEAD_SHA": "b" * 40,
                        "SOURCE_COMMENT_ID": "10",
                        "SOURCE_UPDATED_AT": "2026-10-26T00:00:00Z",
                        "SOURCE_KIND": "issue",
                        "ROOT_COMMENT_ID": "10",
                        "COMMENT_BODY": "@sensei review pause",
                        "COMMENT_ACTOR": "alice",
                        "COMMENT_ACTOR_TYPE": "User",
                        "COMMENT_ASSOCIATION": "OWNER",
                    }
                    result = subprocess.run(
                        ["bash", "-e", "-c", textwrap.dedent(script)],
                        env=environment,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    self.assertEqual(result.returncode, exit_code, result.stderr)
                    self.assertNotIn("forbidden-http", result.stderr)
                    document = json.loads(capture.read_text())
                    self.assertEqual(document["args"][0], entry)
                    self.assertEqual(
                        document["args"][1:4],
                        ["--policy-commit", "c" * 40, "--run-directory"],
                    )
                    self.assertEqual(document["policy"], "c" * 40)
                    self.assertEqual(document["sha"], "a" * 40)
                    self.assertEqual(
                        document["workflow"], environment["REVIEW_SENSEI_WORKFLOW_REF"]
                    )
                    self.assertNotIn("--oidc-token", document["args"])
                    self.assertNotIn("--session-attestation", document["args"])

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
            runner, "Live preflight belongs to the selected metered lifecycle."
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
