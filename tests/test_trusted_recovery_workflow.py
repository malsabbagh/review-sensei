"""Manual recovery stays explicit in both canonical reusable runner lanes."""

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from review_sensei_github.setup import (
    WORKFLOW_PATH,
    _looks_like_managed_v5_setup,
    _tagged_workflow,
)

from review_sensei.configuration import parse_configuration_text
from review_sensei.hosted import plan_hosted_execution
from tests.test_ci_policy import _job_section, _run_block_containing, _step_block

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/review-sensei-run.yml"


class TrustedRecoveryWorkflowTests(unittest.TestCase):
    def test_both_lanes_publish_explicit_retained_recovery_and_skip_inference(self):
        text = WORKFLOW.read_text()
        for lane in ("hosted", "local"):
            with self.subTest(lane=lane):
                job = _job_section(text, lane)
                self.assertIn(
                    "if: inputs.operation == 'review' && !inputs.recover_publication",
                    job,
                )
                self.assertIn("--recover-retained --reconstruct-contexts", job)
                self.assertIn('--trusted-context-root "$PWD/recovery-policy"', job)
                self.assertIn(
                    "steps.provider-review.outputs.result_ready == 'true' || inputs.recover_publication",
                    job,
                )
                self.assertIn("git merge-base --is-ancestor", job)
                self.assertIn("ref: ${{ inputs.recovery_policy_commit }}", job)
                self.assertIn("--github-session-ledger", job)
                self.assertNotIn("pull-requests: write", job)

    @unittest.skipIf(os.name == "nt", "Linux runner script requires POSIX Bash")
    def test_recovery_intent_requires_manual_dispatch_and_exact_original_head(self):
        block = _run_block_containing(
            WORKFLOW.read_text(), "operation must be review, reply, or command"
        )
        base = {
            "PATH": os.environ.get("PATH", ""),
            "MODE": "manual",
            "OPERATION": "review",
            "REPOSITORY": "owner/repo",
            "REPOSITORY_ID": "1",
            "PULL_REQUEST": "1",
            "BASE_REF": "main",
            "HEAD_REPOSITORY": "",
            "BASE_SHA": "a" * 40,
            "HEAD_REF": "feature",
            "HEAD_SHA": "b" * 40,
            "RECOVER_PUBLICATION": "true",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "RECOVERY_POLICY_COMMIT": "c" * 40,
            "RECOVERY_EXPECTED_HEAD": "b" * 40,
        }
        for changes, allowed in (
            ({}, True),
            ({"GITHUB_EVENT_NAME": "pull_request"}, False),
            ({"MODE": "automatic"}, False),
            ({"RECOVERY_EXPECTED_HEAD": "d" * 40}, False),
            ({"RECOVERY_POLICY_COMMIT": "main"}, False),
            ({"RECOVERY_EXPECTED_HEAD": ""}, False),
        ):
            with self.subTest(changes=changes):
                result = subprocess.run(
                    ["bash", "-c", block],
                    env={**base, **changes},
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode == 0, allowed, result.stderr)

    def test_canonical_caller_exposes_opt_in_without_changing_normal_operation(self):
        text = _tagged_workflow("v5")
        self.assertIn("      recover_publication:\n", text)
        self.assertIn("      recovery_policy_commit:\n", text)
        self.assertIn("      recovery_expected_head:\n", text)
        self.assertIn(
            "recover_publication: ${{ inputs.recover_publication || false }}", text
        )
        self.assertIn("operation: ${{ needs.resolve-trigger.outputs.operation }}", text)
        self.assertTrue(_looks_like_managed_v5_setup(WORKFLOW_PATH, text))
        self.assertFalse(
            _looks_like_managed_v5_setup(WORKFLOW_PATH, text + "\n# operator edit\n")
        )

    def test_exact_pre_recovery_caller_remains_managed_but_operator_edits_do_not(self):
        old = (
            ROOT
            / "packages/python/review-sensei-github/src/review_sensei_github/fixtures/released-v5-pre-recovery-caller.yml"
        ).read_text()
        self.assertTrue(_looks_like_managed_v5_setup(WORKFLOW_PATH, old))
        self.assertFalse(
            _looks_like_managed_v5_setup(WORKFLOW_PATH, old + "\n# operator edit\n")
        )

    @unittest.skipIf(os.name == "nt", "Linux runner script requires POSIX Bash")
    def test_recovery_bootstrap_and_both_lanes_need_no_provider_credentials(self):
        text = WORKFLOW.read_text()
        for lane, backend, model in (
            ("hosted", "cloud-ollama", "deepseek-v4.1-flash:cloud"),
            ("local", "local-ollama", "qwen3.5:4b"),
        ):
            with self.subTest(lane=lane), tempfile.TemporaryDirectory() as directory:
                # This is the same pure resolver the bootstrap host-plan uses.
                # A required but absent cloud key must not block recovery setup.
                plan = plan_hosted_execution(
                    parse_configuration_text(
                        f"schema: 1\ninference:\n  backend: {backend}\n  model: {model}\n",
                        root=Path(directory),
                    ),
                    environ={},
                )
                self.assertFalse(plan.credential_present)
                self.assertEqual(plan.runner_kind, lane)
                self.assertEqual(plan.credential_required, lane == "hosted")
                job = _job_section(text, lane)
                validate = textwrap.dedent(
                    _run_block_containing(job, "workflow mode is invalid")
                )
                environment = {
                    "PATH": str(Path(sys.executable).parent)
                    + os.pathsep
                    + os.environ.get("PATH", ""),
                    "MODE": "manual",
                    "OPERATION": "review",
                    "RUNNER_KIND": lane,
                    "BACKEND": backend,
                    "MODEL": model,
                    "BASE_URL": plan.base_url,
                    "ALLOW_CUSTOM_ENDPOINT": "false",
                    "CREDENTIAL_ENV": plan.credential_env or "",
                    "CREDENTIAL_REQUIRED": str(plan.credential_required).lower(),
                    "REPOSITORY": "owner/repo",
                    "BASE_REF": "main",
                    "HEAD_REF": "feature",
                    "BASE_SHA": "a" * 40,
                    "HEAD_SHA": "b" * 40,
                    "HEAD_REPOSITORY": "",
                }
                result = subprocess.run(
                    ["bash", "-c", validate],
                    env=environment,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                materialize = _step_block(job, "Scope the selected backend credential")
                self.assertIn("if: ${{ !inputs.recover_publication }}", materialize)
                review_step = _step_block(
                    job, "Run hosted review" if lane == "hosted" else "Run local review"
                )
                self.assertIn("&& !inputs.recover_publication", review_step)
                # Outside recovery, required credentials still fail explicitly;
                # the recovery opt-in does not relax the credential contract.
                credential_run = _run_block_containing(
                    job, 'credential_value="${!CREDENTIAL_ENV:-}"'
                )
                credential_run = credential_run.replace(
                    "${{ needs.bootstrap.outputs.backend }}", backend
                )
                result = subprocess.run(
                    ["bash", "-c", credential_run],
                    env={
                        **environment,
                        "CREDENTIAL_REQUIRED": "true",
                        "CREDENTIAL_ENV": "OLLAMA_API_KEY",
                        "GITHUB_ENV": str(Path(directory) / "env"),
                    },
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    "secret OLLAMA_API_KEY is required", result.stdout + result.stderr
                )


if __name__ == "__main__":
    unittest.main()
