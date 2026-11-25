"""Installed-wheel contracts for stalled-review next-safe-action guidance."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

_TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TESTS))
from packaging_guard import assert_distribution_import  # noqa: E402

assert_distribution_import()

from review_sensei_github.publication import PublicationResult  # noqa: E402
from review_sensei_github.review_guidance import (  # noqa: E402
    render_next_action_markdown,
    render_publication_failure_step_summary,
    render_status_guidance_lines,
    resolve_next_action_guidance,
    write_publication_failure_step_summary,
)

from review_sensei.coverage import CoverageManifest, FileCoverage, HunkCoverage  # noqa: E402
from review_sensei.errors import ReviewInputError  # noqa: E402
from review_sensei.models import ReviewComment, ReviewResult  # noqa: E402

HEAD = "b" * 40


class InstalledStalledGuidanceContractTests(unittest.TestCase):
    def test_installed_guidance_and_step_summary_offline(self) -> None:
        partial = ReviewResult(
            summary="Partial analysis.",
            comments=(),
            provider="fixture",
            review_status="partial",
            coverage=CoverageManifest(
                enumeration_complete=True,
                enumerated_paths=("src/a.py", "src/b.py"),
                files=(
                    FileCoverage(path="src/a.py", outcome="reviewed"),
                    FileCoverage(
                        path="src/b.py",
                        outcome="budget-exhausted",
                        reason="provider-call-budget",
                    ),
                ),
                hunks=(
                    HunkCoverage(index=1, path="src/a.py", outcome="reviewed"),
                    HunkCoverage(
                        index=2,
                        path="src/b.py",
                        outcome="budget-exhausted",
                        reason="provider-call-budget",
                    ),
                ),
            ),
        )
        guidance = resolve_next_action_guidance(head_sha=HEAD, retained_result=partial)
        self.assertEqual(guidance.state, "remaining_text_coverage")
        self.assertEqual(
            guidance.command, f"@reviewsensei review remaining commit {HEAD}"
        )
        self.assertTrue(guidance.requires_model_call)
        md = render_next_action_markdown(guidance)
        status_lines = render_status_guidance_lines(guidance)
        self.assertIn(guidance.command, md)
        self.assertIn(guidance.command, status_lines)
        with tempfile.TemporaryDirectory() as tmp:
            summary_path = Path(tmp) / "summary.md"
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary_path)}):
                wrote = write_publication_failure_step_summary(
                    guidance,
                    repository="owner/repo",
                    pull_request=7,
                    head_sha=HEAD,
                )
            self.assertTrue(wrote)
            self.assertIn(guidance.command, summary_path.read_text(encoding="utf-8"))

        # Validate additional states & non-hex head_sha sanitization contract.
        complete_clean = replace(partial, review_status="complete", coverage=None)
        blocker_result = replace(
            complete_clean,
            comments=(
                ReviewComment(
                    path="src/a.py",
                    line=1,
                    body="Must fix.",
                    blocking=True,
                    effective_blocking=True,
                    severity="high",
                ),
            ),
        )
        human_result = replace(
            complete_clean,
            comments=(
                ReviewComment(
                    path="src/a.py",
                    line=1,
                    body="Confirm intent.",
                    blocking=False,
                    effective_blocking=False,
                    needs_human=True,
                    severity="low",
                ),
            ),
        )
        media_result = replace(
            partial,
            coverage_only_partial=True,
            coverage=CoverageManifest(
                enumeration_complete=True,
                enumerated_paths=("src/a.py", "assets/diagram.png"),
                files=(
                    FileCoverage(path="src/a.py", outcome="reviewed"),
                    FileCoverage(
                        path="assets/diagram.png",
                        outcome="unsupported",
                        reason="binary",
                    ),
                ),
                hunks=(HunkCoverage(index=1, path="src/a.py", outcome="reviewed"),),
            ),
        )
        for label, result_obj, pub_obj, err_obj, expected_state, expected_cmd in (
            (
                "no_review",
                None,
                None,
                None,
                "no_review",
                f"@reviewsensei rescan commit {HEAD}",
            ),
            (
                "media",
                media_result,
                None,
                None,
                "incomplete_non_text_coverage",
                "@reviewsensei I reviewed the media files and I approve",
            ),
            ("fixes", blocker_result, None, None, "required_fixes_open", None),
            ("human", human_result, None, None, "human_decisions_pending", None),
            (
                "ready_verify",
                complete_clean,
                PublicationResult(
                    status="published", diagnostic="approval_withheld", review_id=12
                ),
                None,
                "ready_for_verify",
                "@reviewsensei verify",
            ),
            (
                "completed",
                complete_clean,
                PublicationResult(status="published", review_id=12),
                None,
                "completed",
                None,
            ),
            (
                "budget",
                None,
                None,
                ReviewInputError("partition shared request budget exhausted"),
                "budget_exhausted",
                None,
            ),
        ):
            with self.subTest(label=label):
                auto_approve = label != "completed"
                g = resolve_next_action_guidance(
                    head_sha=HEAD,
                    retained_result=result_obj,
                    publication=pub_obj,
                    error=err_obj,
                    auto_approve=auto_approve,
                )
                self.assertEqual(g.state, expected_state)
                self.assertEqual(g.command, expected_cmd)

        # Non-hex head_sha is never interpolated into step summary or commands.
        non_hex_guidance = resolve_next_action_guidance(head_sha="refs/heads/main")
        self.assertIsNone(non_hex_guidance.command)
        rendered = render_publication_failure_step_summary(
            non_hex_guidance, head_sha="refs/heads/main"
        )
        self.assertNotIn("refs/heads/main", rendered)


def setUpModule() -> None:
    global _network
    _network = patch(
        "socket.socket.connect", side_effect=AssertionError("live network forbidden")
    )
    _network.start()


def tearDownModule() -> None:
    _network.stop()
