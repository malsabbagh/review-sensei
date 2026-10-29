"""Manual recovery stays explicit in both canonical reusable runner lanes."""

import hashlib
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from review_sensei_github.setup import (
    WORKFLOW_PATH,
    _looks_like_managed_v5_setup,
    _prepared_recovery_workflow,
    _tagged_workflow,
)

from review_sensei.configuration import parse_configuration_text
from review_sensei.hosted import plan_hosted_execution
from tests.test_ci_policy import _job_section, _run_block_containing, _step_block

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/review-sensei-run.yml"


class TrustedRecoveryWorkflowTests(unittest.TestCase):
    def test_default_setup_and_installed_caller_use_exact_released_bytes(self):
        from review_sensei_github.setup import (
            SetupPlanBuilder,
            _pre_recovery_v5_workflow,
        )

        old = (
            ROOT
            / "packages/python/review-sensei-github/src/review_sensei_github/fixtures/released-v5-pre-recovery-caller.yml"
        ).read_text()
        self.assertEqual(_pre_recovery_v5_workflow("v5"), old)
        self.assertEqual(
            SetupPlanBuilder().build("owner/repo").files[0].content,
            (ROOT / ".github/workflows/review-sensei-review.yml").read_text(),
        )

    def test_default_caller_matches_selected_released_runner_input_types(self):
        from tests.test_trusted_stage_config import caller_reusable_with_keys

        fixture = (ROOT / "tests/fixtures/released-v5-runner-inputs.yml").read_text()
        self.assertEqual(
            hashlib.sha256(fixture.encode()).hexdigest(),
            "5596051e9270010ac5e8a13b207ead0b88c06b3b3dbebf7e4c7ac43c972245e0",
        )

        def input_types(text):
            block = text.split("  workflow_call:\n", 1)[1].split("    secrets:", 1)[0]
            return dict(
                re.findall(
                    r"^      ([a-z_]+):(?:\n.*?)*?\n        type: (string|boolean|number)$",
                    block,
                    re.M,
                )
            )

        released = input_types(fixture)
        prepared = input_types(WORKFLOW.read_text())
        self.assertEqual(len(released), 19)
        self.assertEqual(set(released.values()), {"string"})
        self.assertEqual({name: prepared[name] for name in released}, released)
        self.assertEqual(
            {name: value for name, value in prepared.items() if name not in released},
            {
                "recover_publication": "boolean",
                "recovery_policy_commit": "string",
                "recovery_expected_head": "string",
                "quarantine_predecessor_comment_id": "string",
            },
        )
        for caller in (
            _tagged_workflow("v5"),
            (ROOT / ".github/workflows/review-sensei-review.yml").read_text(),
        ):
            passed = caller_reusable_with_keys(caller)
            self.assertEqual(len(passed), 18)
            self.assertLessEqual(passed, released.keys())
            self.assertEqual(
                {name: released[name] for name in passed},
                {name: prepared[name] for name in passed},
            )

    def test_both_lanes_delegate_recovery_to_the_original_lifecycle(self):
        text = WORKFLOW.read_text()
        for lane, name in (
            ("hosted", "Run hosted review"),
            ("local", "Run local review"),
        ):
            job = _job_section(text, lane)
            step = _step_block(job, name)
            self.assertIn("if: inputs.operation == 'review'", step)
            self.assertIn('" review-run ', step)
            self.assertNotIn('" publish ', job)
            self.assertNotIn("gh api", job.split("- name: Generate and publish", 1)[0])
            self.assertNotIn("recover-retained", job)
            self.assertNotIn("ref: ${{ inputs.recovery_policy_commit }}", job)
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
            ({}, False),
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

    def test_canonical_caller_exposes_only_supported_recovery(self):
        text = _prepared_recovery_workflow("v5")
        self.assertNotIn("      recover_publication:\n", text)
        self.assertNotIn("      recovery_policy_commit:\n", text)
        self.assertEqual(text, _tagged_workflow("v5"))
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
                self.assertNotIn("inputs.recover_publication", materialize)
                review_step = _step_block(
                    job, "Run hosted review" if lane == "hosted" else "Run local review"
                )
                self.assertIn("if: inputs.operation == 'review'", review_step)
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
