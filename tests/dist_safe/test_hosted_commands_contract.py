"""Installed hosted commands use ordinary imports and preserve late patches."""

from __future__ import annotations

import argparse
import importlib.metadata
import io
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

_TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TESTS))
from packaging_guard import assert_distribution_import, is_disarmed  # noqa: E402

assert_distribution_import()

import review_sensei_github  # noqa: E402
from review_sensei_github import command_bodies, commands  # noqa: E402

import review_sensei.cli as engine_cli  # noqa: E402
from review_sensei.broker_diagnostics import BrokerDiagnostic  # noqa: E402

if not is_disarmed() and not Path(
    review_sensei_github.__file__
).resolve().is_relative_to(Path(sys.prefix).resolve()):
    raise ImportError("hosted command qualification requires the installed adapter")


class HostedCommandsContractTests(unittest.TestCase):
    def test_exports_are_the_normally_imported_functions(self):
        names = {
            "hosted_package_version": "hosted_package_version",
            "run_hosted_provider": "run_hosted_provider",
            "report_broker_failure": "report_broker_failure",
            "run_github": "run_github",
            "run_host_plan": "run_host_plan",
            "resolve_hosted_openrouter_parser": "resolve_hosted_openrouter_parser",
            "github_parser": "_github_parser",
            "host_plan_parser": "_host_plan_parser",
            "require_accepted_result": "_require_accepted_result",
            "lookup_collaborator_permission": "_collaborator_permission",
        }
        self.assertEqual(set(commands.__all__), set(names))
        for exported, implementation in names.items():
            self.assertIs(
                getattr(commands, exported), getattr(command_bodies, implementation)
            )

    def test_import_does_not_read_or_execute_command_source_itself(self):
        script = """from pathlib import Path
from unittest.mock import patch
original = Path.read_text
def read(path, *args, **kwargs):
    if path.name == "command_bodies.py":
        raise AssertionError("dynamic command source read")
    return original(path, *args, **kwargs)
with patch.object(Path, "read_text", new=read):
    from review_sensei_github import commands, command_bodies
    assert commands.run_github is command_bodies.run_github
"""
        result = subprocess.run(
            [sys.executable, "-I", "-c", script],
            cwd=Path(sys.prefix),
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_version_and_parser_program_contract(self):
        expected = importlib.metadata.version("review-sensei-github")
        self.assertEqual(commands.hosted_package_version(), expected)
        parser = commands.resolve_hosted_openrouter_parser()
        self.assertEqual(parser.prog, "review-sensei resolve-hosted-openrouter")
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            parser.parse_args(["--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), expected)
        self.assertEqual(commands.github_parser().prog, "review-sensei github")
        self.assertEqual(commands.host_plan_parser().prog, "review-sensei host-plan")

    def test_late_engine_parser_patch_is_observed_after_import(self):
        with patch.object(engine_cli, "_positive_float", return_value=42.25) as convert:
            parsed = commands.github_parser().parse_args(
                [
                    "reply",
                    "--repository",
                    "owner/repo",
                    "--pull-request",
                    "1",
                    "--source-comment-id",
                    "2",
                    "--source-updated-at",
                    "2026-10-30T00:00:00Z",
                    "--timeout-seconds",
                    "2",
                ]
            )
        self.assertEqual(parsed.timeout_seconds, 42.25)
        convert.assert_called_once_with("2")

    def test_late_engine_outcome_patch_is_observed_after_import(self):
        diagnostic = BrokerDiagnostic("broker_workflow_rejected")
        with (
            patch(
                "review_sensei.broker_diagnostics.exception_broker_diagnostic",
                return_value=diagnostic,
            ),
            patch.object(engine_cli, "emit_host_outcome") as emit,
            redirect_stderr(io.StringIO()),
        ):
            self.assertTrue(
                commands.report_broker_failure(
                    RuntimeError("synthetic"), argparse.Namespace(outcome=None)
                )
            )
        self.assertEqual(emit.call_args.args[0].status, "action_required")
        self.assertEqual(emit.call_args.args[0].diagnostic, "broker_rejected")
