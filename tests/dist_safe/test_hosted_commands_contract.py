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


class HostedSemanticHistoryAndDriftGapContractTests(unittest.TestCase):
    def test_carried_history_fallback_and_hierarchical_drift_matching(self):
        from review_sensei_github.accepted_semantic_history import (
            action_allows_carried_history_fallback,
        )

        from review_sensei.coverage import CoverageManifest, FileCoverage
        from review_sensei.finding_drift import (
            ConfirmedFixEvidence,
            FileDriftDescriptor,
            evaluate_drift_round,
        )
        from review_sensei.models import ReviewComment, ReviewResult

        self.assertTrue(
            action_allows_carried_history_fallback(
                {"outcome": {"kind": "explicit_noop", "reason": "capacity_unavailable"}}
            )
        )
        self.assertTrue(
            action_allows_carried_history_fallback(
                {"outcome": {"kind": "explicit_noop", "reason": "evaluation_failed"}}
            )
        )
        self.assertFalse(
            action_allows_carried_history_fallback(
                {"outcome": {"kind": "explicit_noop", "reason": "no_change"}}
            )
        )

        head_1, head_2, base = "a" * 40, "b" * 40, "0" * 40
        validator_coverage = CoverageManifest(
            files=(FileCoverage("src/validator.py", "reviewed"),),
            enumerated_paths=("src/validator.py",),
            enumeration_complete=True,
        )
        r1 = evaluate_drift_round(
            ReviewResult(
                summary="Round 1",
                comments=(
                    ReviewComment(
                        path="src/validator.py",
                        line=10,
                        side="RIGHT",
                        body="Reject empty header token.",
                        severity="high",
                        blocking=True,
                        effective_blocking=True,
                        needs_human=False,
                        symbol="parse_header",
                        evidence_id="ev-header-empty",
                    ),
                    ReviewComment(
                        path="src/validator.py",
                        line=40,
                        side="RIGHT",
                        body="Consider validating optional hint length.",
                        severity="low",
                        blocking=False,
                        effective_blocking=False,
                        needs_human=False,
                        symbol="format_hint",
                        evidence_id="ev-hint-length",
                    ),
                ),
                provider="synthetic-provider",
                model="synthetic-model",
                review_status="complete",
                coverage=validator_coverage,
            ),
            head_sha=head_1,
            base_sha=base,
            max_active_findings=20,
            round_number=1,
            diff_text="diff --git a/src/validator.py b/src/validator.py\n--- a/src/validator.py\n+++ b/src/validator.py\n@@ -1 +1 @@\n-old\n+new\n",
        )
        r2 = evaluate_drift_round(
            ReviewResult(
                summary="Round 2",
                comments=(
                    ReviewComment(
                        path="src/validator.py",
                        line=85,
                        side="RIGHT",
                        body="Validate footer checksum length.",
                        severity="medium",
                        blocking=True,
                        effective_blocking=True,
                        needs_human=False,
                        symbol="parse_footer",
                        evidence_id="ev-footer-len",
                    ),
                ),
                provider="synthetic-provider",
                model="synthetic-model",
                review_status="complete",
                coverage=validator_coverage,
            ),
            head_sha=head_2,
            base_sha=base,
            max_active_findings=20,
            round_number=2,
            prior_inventory=r1.inventory,
            drift_descriptors=(
                FileDriftDescriptor(
                    drift_kind="line_deletion", source_path="src/validator.py"
                ),
            ),
            confirmed_fixes=tuple(
                ConfirmedFixEvidence(
                    obligation_id=ob.obligation_id,
                    resolution_criterion=ob.criterion.resolution_criterion,
                    head_sha=head_2,
                    evidence_sha256="f" * 64,
                    verified_paths=("src/validator.py",),
                )
                for ob in r1.inventory.obligations
            ),
            diff_text="diff --git a/src/validator.py b/src/validator.py\n--- a/src/validator.py\n+++ b/src/validator.py\n@@ -1 +1 @@\n-old\n+new\n",
        )
        by_sym = {
            (ob.present_anchor.symbol or ob.criterion.origin_symbol): ob
            for ob in r2.inventory.obligations
        }
        self.assertEqual(by_sym["parse_header"].lifecycle_state, "verified-fixed")
        self.assertEqual(by_sym["format_hint"].lifecycle_state, "verified-fixed")
        self.assertFalse(by_sym["format_hint"].criterion.blocking)
        self.assertEqual(by_sym["parse_footer"].lifecycle_state, "active-unresolved")
