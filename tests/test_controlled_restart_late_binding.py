"""Late package data must match the actual held execution before installation."""

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from review_sensei_github import controlled_restart_workflow as workflow
from test_controlled_restart import registration

from review_sensei.errors import ReviewInputError


class LateBindingTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "existing Actions metadata step uses POSIX")
    def test_scope_report_observes_planning_values_without_guessing_registration(self):
        import yaml

        root = Path(__file__).resolve().parents[1]
        jobs = yaml.safe_load(
            (root / ".github/workflows/review-sensei-run.yml").read_text()
        )["jobs"]
        script = next(
            s["run"]
            for s in jobs["bootstrap"]["steps"]
            if s["name"] == "Report frozen restart scope"
        )
        with tempfile.TemporaryDirectory() as temporary:
            binaries = Path(temporary) / "review-sensei-venv/bin"
            binaries.mkdir(parents=True)
            (binaries / "python").symlink_to(sys.executable)
            env = dict(
                PATH=os.environ["PATH"],
                RUNNER_TEMP=temporary,
                GITHUB_RUN_ID="456",
                GITHUB_RUN_ATTEMPT="1",
            )
            fields = dict(
                REPOSITORY="owner/repo",
                REPOSITORY_ID="123",
                PR="7",
                BASE="a" * 40,
                HEAD="b" * 40,
                POLICY="c" * 40,
                WORKFLOW="d" * 40,
                JOB="hosted",
            )
            env.update({"SCOPE_" + k: v for k, v in fields.items()})
            result = subprocess.run(
                ["bash", "-e", "-c", script],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            observed = json.loads(result.stdout.removeprefix("Held restart scope: "))
            self.assertEqual(observed["run_id"], "456")
            self.assertEqual(observed["policy_commit"], fields["POLICY"])
            self.assertEqual(observed["job"], "hosted")
            self.assertNotIn("request_id", observed)
            self.assertNotIn("old_comments", observed)

    def test_explicit_caller_forwards_late_intent_without_changing_normal_setup(self):
        import yaml
        from review_sensei_github.controlled_restart_caller import restart_caller
        from review_sensei_github.setup import _tagged_workflow

        normal = _tagged_workflow()
        self.assertNotIn("controlled_restart_hosted_version", normal)
        prepared = restart_caller()
        jobs = yaml.safe_load(prepared)["jobs"]
        self.assertNotIn("id-token", jobs["resolve-trigger"]["permissions"])
        forwarded = jobs["review-or-reply"]["with"]
        for name in ("hosted_version", "core_version", "parent_directory"):
            self.assertIn(
                "inputs.controlled_restart_" + name,
                forwarded["controlled_restart_" + name],
            )
        self.assertIn("inputs.controlled_restart_runner", forwarded["hosted_runner"])
        self.assertEqual(_tagged_workflow(), normal)

    @unittest.skipIf(os.name == "nt", "Actions hold uses the existing POSIX runner")
    def test_actual_hold_script_rejects_automatic_partial_and_replayed_intent(self):
        from test_ci_policy import _run_block_containing

        root = Path(__file__).resolve().parents[1]
        script = _run_block_containing(
            (root / ".github/workflows/review-sensei-run.yml").read_text(),
            "late restart requires one protected",
        )
        base = dict(
            PATH=os.environ["PATH"],
            MODE="manual",
            OPERATION="review",
            GITHUB_EVENT_NAME="workflow_dispatch",
            GITHUB_RUN_ATTEMPT="1",
            RUNNER_ENVIRONMENT="self-hosted",
            HOSTED="0.3.99",
            CORE="0.8.99",
            PARENT="/approved/retained",
            PRIOR_ID="",
            PRIOR_DIRECTORY="",
            QUARANTINE="",
        )
        for changes, allowed in (
            ({}, True),
            ({"MODE": "automatic"}, False),
            ({"GITHUB_RUN_ATTEMPT": "2"}, False),
            ({"HOSTED": ""}, False),
            ({"CORE": "latest"}, False),
            ({"PARENT": "relative"}, False),
            ({"PRIOR_ID": "e" * 64}, False),
            ({"OPERATION": "reply"}, False),
            ({"RUNNER_ENVIRONMENT": "github-hosted"}, False),
        ):
            with self.subTest(changes=changes):
                result = subprocess.run(
                    ["bash", "-e", "-c", script],
                    env={**base, **changes},
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode == 0, allowed, result.stderr)

    def test_workflow_holds_every_capability_job_before_restart_installation(self):
        import yaml

        root = Path(__file__).resolve().parents[1]
        jobs = yaml.safe_load(
            (root / ".github/workflows/review-sensei-run.yml").read_text()
        )["jobs"]
        hold = jobs["restart_hold"]
        self.assertEqual(hold["permissions"], {})
        self.assertEqual(hold["environment"], "reviewsensei-controlled-restart")
        self.assertNotIn("needs", jobs["bootstrap"])
        self.assertEqual(jobs["bootstrap"]["permissions"], {"contents": "read"})
        self.assertEqual(
            jobs["restart_hold"]["needs"], ["bootstrap", "authoritative-preflight"]
        )
        report = next(
            s
            for s in jobs["bootstrap"]["steps"]
            if s["name"] == "Report frozen restart scope"
        )
        self.assertIn("steps.policy.outputs.commit", report["env"]["SCOPE_POLICY"])
        self.assertIn("steps.plan.outputs.runner_kind", report["env"]["SCOPE_JOB"])
        for name in ("command", "hosted", "local"):
            self.assertIn("restart_hold", jobs[name]["needs"])
            self.assertIn("always()", jobs[name]["if"])
            self.assertIn("'skipped'", jobs[name]["if"])
            self.assertIn("needs.bootstrap.result == 'success'", jobs[name]["if"])
        self.assertIn("needs.bootstrap.outputs.hosted_runner", jobs["local"]["runs-on"])
        self.assertIn(
            '"self-hosted","linux","x64","ollama","{0}"', jobs["local"]["runs-on"]
        )
        for lane in ("hosted", "local"):
            steps = jobs[lane]["steps"]
            names = [s["name"] for s in steps]
            self.assertLess(
                names.index("Install designated restart package"),
                names.index("Scope the selected backend credential"),
            )
            step = next(
                s for s in steps if s["name"] == "Install designated restart package"
            )
            self.assertIn("job.workflow_sha", step["env"]["FROZEN_WORKFLOW_SHA"])
            self.assertNotIn("secrets.", json.dumps(step))
            self.assertIn("controlled_restart_launch.py", step["run"])

    def test_compiled_row_binds_actual_run_and_derives_existing_private_child(self):
        row = registration()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            parent = root / "retained"
            parent.mkdir(mode=0o700)
            child = parent / row["request_id"]
            child.mkdir(mode=0o700)
            temp, workspace = root / "temporary", root / "workspace"
            temp.mkdir()
            workspace.mkdir()
            env = {
                "GITHUB_EVENT_NAME": "workflow_dispatch",
                "GITHUB_RUN_ID": row["run_id"],
                "GITHUB_RUN_ATTEMPT": "1",
                "RUNNER_ENVIRONMENT": "self-hosted",
                "RUNNER_TEMP": str(temp),
                "GITHUB_WORKSPACE": str(workspace),
            }
            scope = {
                k: row[k]
                for k in (
                    "repository",
                    "repository_id",
                    "pull_request",
                    "base_sha",
                    "head_sha",
                    "policy_commit",
                    "workflow_sha",
                    "job",
                )
            }
            raw = json.dumps(
                {
                    "schema_version": "controlled-restart-registry-v1",
                    "registrations": [row],
                }
            ).encode()
            with patch.dict(os.environ, env, clear=True):
                selected, path = workflow.select_late_registration(
                    raw, parent=parent, **scope
                )
                self.assertEqual(selected, row["request_id"])
                self.assertEqual(path, child)
                self.assertFalse(list(child.iterdir()))
                for key, value in (
                    ("GITHUB_RUN_ID", "9999"),
                    ("GITHUB_RUN_ATTEMPT", "2"),
                    ("GITHUB_EVENT_NAME", "pull_request"),
                    ("RUNNER_ENVIRONMENT", "github-hosted"),
                ):
                    with self.subTest(key=key), patch.dict(os.environ, {key: value}):
                        with self.assertRaises(ReviewInputError):
                            workflow.select_late_registration(
                                raw, parent=parent, **scope
                            )
                for key in scope:
                    changed = copy.copy(scope)
                    changed[key] = 999 if type(scope[key]) is int else "foreign"
                    with self.subTest(key=key), self.assertRaises(ReviewInputError):
                        workflow.select_late_registration(raw, parent=parent, **changed)
                (child / "old").write_text("preserved")
                with self.assertRaises(ReviewInputError):
                    workflow.select_late_registration(raw, parent=parent, **scope)
                self.assertEqual((child / "old").read_text(), "preserved")


if __name__ == "__main__":
    unittest.main()
