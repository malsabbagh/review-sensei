"""Execute the inert workflow routes locally; never dispatch or grant access."""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from review_sensei_github import controlled_restart_workflow
from review_sensei_github.controlled_restart_workflow import retained_directory

from review_sensei import errors
from review_sensei.errors import ReviewInputError

ROOT = Path(__file__).resolve().parents[1]
REQUEST = "e" * 64


@unittest.skipIf(os.name == "nt", "existing Actions runner is POSIX")
class ControlledRestartWorkflowTests(unittest.TestCase):
    def test_bootstrap_rejects_incomplete_or_automatic_restart_intent(self):
        from tests.test_ci_policy import _run_block_containing

        workflow = (ROOT / ".github/workflows/review-sensei-run.yml").read_text()
        script = _run_block_containing(
            workflow, "operation must be review, reply, or command"
        )
        base = {
            "PATH": os.environ["PATH"],
            "MODE": "manual",
            "OPERATION": "review",
            "REPOSITORY": "owner/repo",
            "REPOSITORY_ID": "123",
            "PULL_REQUEST": "7",
            "BASE_REF": "main",
            "BASE_SHA": "a" * 40,
            "HEAD_REF": "topic",
            "HEAD_SHA": "b" * 40,
            "HEAD_REPOSITORY": "owner/repo",
            "RECOVER_PUBLICATION": "false",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "CONTROLLED_RESTART_ID": REQUEST,
            "CONTROLLED_RESTART_DIRECTORY": "/operator/retained/" + REQUEST,
        }
        for changes, allowed in (
            ({}, True),
            ({"CONTROLLED_RESTART_ID": ""}, False),
            ({"CONTROLLED_RESTART_DIRECTORY": ""}, False),
            ({"CONTROLLED_RESTART_ID": "invalid"}, False),
            ({"MODE": "automatic"}, False),
            ({"GITHUB_EVENT_NAME": "pull_request"}, False),
            ({"OPERATION": "reply"}, False),
            ({"QUARANTINE_PREDECESSOR_COMMENT_ID": "71"}, False),
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

    def environment(self, root):
        root = root.resolve()
        temporary, workspace, retained = (
            root / "temporary",
            root / "workspace",
            root / "retained" / REQUEST,
        )
        for path in (temporary, workspace, retained):
            path.mkdir(parents=True, mode=0o700)
        return {
            "PATH": os.environ["PATH"],
            "PYTHONPATH": os.pathsep.join(
                [
                    str(
                        Path(controlled_restart_workflow.__file__).resolve().parents[1]
                    ),
                    str(Path(errors.__file__).resolve().parents[1]),
                ]
            ),
            "MODE": "manual",
            "OPERATION": "review",
            "RUNNER_ENVIRONMENT": "self-hosted",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "CONTROLLED_RESTART_ID": REQUEST,
            "CONTROLLED_RESTART_DIRECTORY": str(retained),
            "RUNNER_TEMP": str(temporary),
            "GITHUB_WORKSPACE": str(workspace),
        }

    def test_retained_directory_is_read_only_and_cannot_be_ephemeral_or_consumed(self):
        with tempfile.TemporaryDirectory() as temporary:
            env = self.environment(Path(temporary))
            retained = Path(env["CONTROLLED_RESTART_DIRECTORY"])
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(retained_directory(), retained)
                self.assertEqual(list(retained.iterdir()), [])
            for changes in (
                {"MODE": "automatic"},
                {"OPERATION": "reply"},
                {"GITHUB_EVENT_NAME": "pull_request"},
                {"RUNNER_ENVIRONMENT": "github-hosted"},
                {"CONTROLLED_RESTART_ID": "arbitrary"},
                {"CONTROLLED_RESTART_DIRECTORY": "relative"},
                {"CONTROLLED_RESTART_DIRECTORY": str(retained.parent / "missing")},
                {"RUNNER_TEMP": str(retained.parent)},
                {"GITHUB_WORKSPACE": str(retained.parent)},
                {"RUNNER_TEMP": ""},
                {"QUARANTINE_PREDECESSOR_COMMENT_ID": "71"},
            ):
                with (
                    self.subTest(changes=changes),
                    patch.dict(os.environ, {**env, **changes}, clear=True),
                ):
                    with self.assertRaises(ReviewInputError):
                        retained_directory()
            retained.chmod(0o755)
            with (
                patch.dict(os.environ, env, clear=True),
                self.assertRaises(ReviewInputError),
            ):
                retained_directory()
            retained.chmod(0o700)
            link = retained.parent / "alias"
            link.symlink_to(retained, target_is_directory=True)
            with (
                patch.dict(
                    os.environ,
                    {**env, "CONTROLLED_RESTART_DIRECTORY": str(link)},
                    clear=True,
                ),
                self.assertRaises(ReviewInputError),
            ):
                retained_directory()
            (retained / "abandoned-history.json").write_bytes(b"prior evidence")
            with (
                patch.dict(os.environ, env, clear=True),
                self.assertRaises(ReviewInputError),
            ):
                retained_directory()
            self.assertEqual(
                (retained / "abandoned-history.json").read_bytes(), b"prior evidence"
            )

    def route(self, lane, env):
        from tests.test_ci_policy import _job_section, _run_blocks, _step_block

        workflow = (ROOT / ".github/workflows/review-sensei-run.yml").read_text()
        name = "Run hosted review" if lane == "hosted" else "Run local review"
        (script,) = _run_blocks(_step_block(_job_section(workflow, lane), name))
        bin_dir = Path(env["RUNNER_TEMP"]) / "review-sensei-venv/bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "python").symlink_to(sys.executable)
        binary = bin_dir / "review-sensei-github"
        binary.write_text(
            "#!" + sys.executable + "\nimport os,sys,json\n"
            "open(os.environ['CAPTURE'],'w').write(json.dumps(sys.argv[1:]))\n"
        )
        binary.chmod(0o755)
        capture = Path(env["RUNNER_TEMP"]) / "captured.json"
        variables = {
            **env,
            "RUNNER_KIND": lane,
            "TITLE_B64": "",
            "LEARNING": "disabled",
            "ARTIFACTS": "none",
            "REPOSITORY": "owner/repo",
            "REPOSITORY_ID": "123",
            "PULL_REQUEST": "7",
            "POLICY_COMMIT": env.get("POLICY_COMMIT", "c" * 40),
            "BASE_SHA": env.get("BASE_SHA", "a" * 40),
            "HEAD_SHA": env.get("HEAD_SHA", "b" * 40),
            "BACKEND": "cloud-ollama" if lane == "hosted" else "local-ollama",
            "BASE_URL": "https://provider.invalid",
            "MODEL": env.get("MODEL", "fixture"),
            "ALLOW_CUSTOM_ENDPOINT": "false",
            "CREDENTIAL_REQUIRED": "false",
            "QUARANTINE_PREDECESSOR_COMMENT_ID": "",
            "GITHUB_OUTPUT": str(Path(env["RUNNER_TEMP"]) / "output"),
            "CAPTURE": str(capture),
        }
        result = subprocess.run(
            ["bash", "-e", "-c", textwrap.dedent(script)],
            env=variables,
            cwd=env["GITHUB_WORKSPACE"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result, json.loads(capture.read_text()) if capture.exists() else None

    def test_bash_public_restart_keeps_only_archive_and_claim_after_success_error_cancel(
        self,
    ):
        from review_sensei_github import cli, controlled_restart, review_run
        from test_controlled_restart_journeys import RestartJourney
        from test_review_transaction import HostedAnalysisCliTests

        for lane, outcome in (
            (lane, outcome)
            for lane in ("hosted", "local")
            for outcome in ("success", "error", "cancel")
        ):
            with (
                self.subTest(lane=lane, outcome=outcome),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary).resolve()
                env = self.environment(root)
                policy = root / "policy"
                policy.mkdir()
                journey = RestartJourney(policy, runner=lane)
                fixture = journey.fixture
                retained = Path(env["CONTROLLED_RESTART_DIRECTORY"])
                retained = retained.rename(retained.parent / journey.row["request_id"])
                env.update(
                    CONTROLLED_RESTART_ID=journey.row["request_id"],
                    CONTROLLED_RESTART_DIRECTORY=str(retained),
                    POLICY_COMMIT=fixture.policy,
                    BASE_SHA=fixture.requested_base,
                    HEAD_SHA=fixture.requested_head,
                    MODEL="deepseek-v4.1-flash:cloud"
                    if lane == "hosted"
                    else "qwen3.5:4b",
                )
                result, argv = self.route(lane, env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("--recovery-artifact", argv)
                original = cli.main
                prepared = []
                evidence = {}
                observed_live_diff = []

                def diff(**kwargs):
                    path = kwargs["output"]
                    prepared.append(path)
                    evidence.update(
                        {
                            p.name: p.read_bytes()
                            for p in retained.iterdir()
                            if p.name == "abandoned-history.json"
                            or p.suffix == ".claim"
                        }
                    )
                    path.write_text(
                        HostedAnalysisCliTests.ANALYSIS_DIFF,
                        encoding="utf-8",
                        newline="\n",
                    )

                def publication_boundary():
                    observed_live_diff.append(prepared[0].read_text())
                    if outcome == "error":
                        raise ReviewInputError("synthetic post-diff failure")
                    if outcome == "cancel":
                        raise KeyboardInterrupt()

                fixture.before_publication = publication_boundary
                with (
                    patch.object(
                        controlled_restart, "registrations", return_value=[journey.row]
                    ),
                    patch.object(cli, "main", side_effect=lambda _: original(argv)),
                    patch.object(review_run, "prepare_diff", side_effect=diff),
                ):
                    self.assertEqual(
                        fixture.invoke(),
                        {"success": 0, "error": 1, "cancel": 130}[outcome],
                        fixture.error,
                    )
                self.assertEqual(
                    observed_live_diff, [HostedAnalysisCliTests.ANALYSIS_DIFF]
                )
                self.assertEqual(len(prepared), 1)
                self.assertFalse(prepared[0].is_relative_to(retained))
                self.assertFalse(prepared[0].exists())
                self.assertFalse(prepared[0].parent.exists())
                self.assertEqual(len(evidence), 2)
                self.assertEqual(
                    {p.name: p.read_bytes() for p in retained.iterdir()},
                    evidence,
                )
                archived = json.loads(evidence["abandoned-history.json"])
                self.assertEqual(archived["historical_effects"], "unsettled")
                self.assertEqual(fixture.provider_calls, 1)
                self.assertEqual(fixture.accounted.accounting.configuration.limit, 512)
                self.assertEqual(
                    len(fixture.accounted.observations), len(fixture.trace)
                )
                if outcome == "success":
                    self.assertIsNotNone(
                        fixture.selected.caller.accounting.terminal_transaction
                    )
                else:
                    self.assertFalse(fixture.reviews)
                self.assertFalse((policy / "abandoned-history.json").exists())

    def test_actual_bash_selects_restart_without_rescan_and_preserves_ordinary_entry(
        self,
    ):
        for lane in ("hosted", "local"):
            for restart in (False, True):
                with (
                    self.subTest(lane=lane, restart=restart),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    env = self.environment(Path(temporary))
                    if not restart:
                        env.update(
                            CONTROLLED_RESTART_ID="",
                            CONTROLLED_RESTART_DIRECTORY="",
                            GITHUB_EVENT_NAME="pull_request",
                        )
                    result, args = self.route(lane, env)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(
                        args[0], "restart-run" if restart else "review-run"
                    )
                    self.assertEqual(
                        args[args.index("--run-directory") + 1],
                        env["CONTROLLED_RESTART_DIRECTORY"]
                        if restart
                        else env["RUNNER_TEMP"],
                    )
                    self.assertNotIn("--rescan", args)
                    self.assertNotIn("--review-remaining", args)
                    if restart:
                        self.assertEqual(args[args.index("--restart-id") + 1], REQUEST)
                    else:
                        self.assertNotIn("--restart-id", args)
                    self.assertIn("--transaction", args)
                    self.assertIn("--github-session-ledger", args)

    def test_actual_bash_refuses_hosted_ephemeral_and_reused_slots_before_cli(self):
        for lane in ("hosted", "local"):
            for failure in ("hosted", "ephemeral", "consumed"):
                with (
                    self.subTest(lane=lane, failure=failure),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    env = self.environment(Path(temporary))
                    retained = Path(env["CONTROLLED_RESTART_DIRECTORY"])
                    if failure == "hosted":
                        env["RUNNER_ENVIRONMENT"] = "github-hosted"
                    elif failure == "ephemeral":
                        env["GITHUB_WORKSPACE"] = str(retained.parent)
                    else:
                        (retained / "abandoned-history.json").write_bytes(b"keep")
                    result, args = self.route(lane, env)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIsNone(args)
                    if failure == "consumed":
                        self.assertEqual(
                            (retained / "abandoned-history.json").read_bytes(), b"keep"
                        )
