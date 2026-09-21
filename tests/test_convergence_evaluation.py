from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone
from unittest.mock import patch

from review_sensei.cli import main
from review_sensei.convergence import (
    DEFAULT_REVIEW_MODE,
    REVIEW_SHADOW_ENV,
    ReviewConvergencePolicy,
    RoundSessionState,
    observe_shadow_admission,
    resolve_review_mode,
    resolve_shadow_review_mode,
)
from review_sensei.diagnostics import build_plan, render_diagnostic, run_doctor
from review_sensei.errors import ReviewInputError
from review_sensei.hosting.github import GitHubApplication, GitHubWriteOptions
from review_sensei.hosting.github.publication import PublicationResult
from review_sensei.models import ReviewResult
from review_sensei.sequence import (
    SequenceStep,
    compare_sequence_policies,
    replay_review_sequence,
    run_observed_review_sequence,
)
from review_sensei.session import InMemorySessionLedger, SessionIdentity, SessionRecord

DIFF = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1,2 @@
 keep
+change
"""

FIXED_NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
IDENTITY = SessionIdentity("owner/repo", 136, repository_id=99)
HEAD = "a" * 40


class RecordingBroker:
    def request_oidc_token(self):
        return "oidc-token"

    def exchange(self, token, *, capability=None):
        return f"capability-{capability}"


class RecordingReviewer:
    def __init__(self):
        self.calls = []

    def publish(self, **kwargs):
        self.calls.append(kwargs)
        return PublicationResult(status="published", review_id=1)


def _policy(mode: str = "merge-focused") -> ReviewConvergencePolicy:
    return ReviewConvergencePolicy(mode=mode)


class DefaultRemainsLegacyTests(unittest.TestCase):
    def test_installed_default_stays_legacy(self):
        self.assertEqual(DEFAULT_REVIEW_MODE, "legacy")
        self.assertEqual(resolve_review_mode(), "legacy")
        self.assertEqual(ReviewConvergencePolicy().mode, "legacy")
        self.assertEqual(run_doctor()["review_convergence"]["mode"], "legacy")
        self.assertNotIn("shadow_review_convergence", run_doctor())


class SequenceReplayTests(unittest.TestCase):
    def test_fixing_ab_with_only_optional_leftovers_can_converge(self):
        report = replay_review_sequence(
            (
                SequenceStep(
                    head_sha="a" * 40,
                    blocking_identities=("defect-a", "defect-b"),
                    label="initial-ab",
                ),
                SequenceStep(
                    head_sha="b" * 40,
                    blocking_identities=(),
                    independently_approval_eligible=True,
                    label="optional-cd",
                ),
            ),
            _policy(),
        )
        self.assertTrue(report.steps[0].admit)
        self.assertEqual(report.steps[0].round_kind, "initial")
        self.assertTrue(report.steps[1].admit)
        self.assertEqual(report.steps[1].round_kind, "verification")
        self.assertTrue(report.steps[1].may_emit_approve)
        self.assertEqual(report.completed_initial_reviews, 1)
        self.assertEqual(report.completed_verification_rounds, 1)
        self.assertEqual(report.handoffs, 0)
        self.assertFalse(report.cap_created_approval)
        payload = report.to_dict()
        self.assertEqual(payload["mode"], "merge-focused")
        self.assertIn("not a claim of zero missed defects", payload["limitations"][0])

    def test_regression_is_detected_and_cap_does_not_approve(self):
        report = replay_review_sequence(
            (
                SequenceStep(
                    head_sha="a" * 40,
                    blocking_identities=("defect-a",),
                    label="initial",
                ),
                SequenceStep(
                    head_sha="b" * 40,
                    blocking_identities=("defect-c",),
                    label="regression",
                ),
                SequenceStep(
                    head_sha="c" * 40,
                    blocking_identities=("defect-e",),
                    label="still-open",
                ),
                SequenceStep(
                    head_sha="d" * 40,
                    blocking_identities=("defect-f",),
                    independently_approval_eligible=False,
                    label="at-cap",
                ),
            ),
            _policy(),
        )
        self.assertTrue(report.steps[1].admit)
        self.assertEqual(report.steps[1].round_kind, "verification")
        self.assertFalse(report.steps[3].admit)
        self.assertTrue(report.steps[3].handoff)
        self.assertEqual(report.steps[3].handoff_reason, "round-budget-exhausted")
        self.assertFalse(report.steps[3].may_emit_approve)
        self.assertFalse(report.cap_created_approval)

    def test_aba_oscillation_is_no_progress(self):
        report = replay_review_sequence(
            (
                SequenceStep(
                    head_sha="a" * 40,
                    blocking_identities=("defect-a",),
                    label="first-a",
                ),
                SequenceStep(
                    head_sha="b" * 40,
                    blocking_identities=("defect-b",),
                    label="then-b",
                ),
                SequenceStep(
                    head_sha="c" * 40,
                    blocking_identities=("defect-a",),
                    label="again-a",
                ),
            ),
            _policy(),
        )
        self.assertTrue(report.steps[2].no_progress)
        self.assertFalse(report.steps[2].admit)
        self.assertEqual(report.steps[2].handoff_reason, "no-progress")
        self.assertEqual(report.no_progress_events, 1)
        self.assertFalse(report.cap_created_approval)

    def test_comparison_keeps_legacy_default_and_never_mints_cap_approval(self):
        steps = (
            SequenceStep(
                head_sha="a" * 40,
                blocking_identities=("defect-a", "defect-b"),
                label="initial",
            ),
            SequenceStep(
                head_sha="b" * 40,
                blocking_identities=(),
                independently_approval_eligible=True,
                label="clean",
            ),
        )
        payload = compare_sequence_policies(steps)
        self.assertEqual(payload["publication_default"], "legacy")
        self.assertEqual(payload["current"]["mode"], "legacy")
        self.assertEqual(payload["proposed"]["mode"], "merge-focused")
        self.assertFalse(payload["cap_created_approval"])
        self.assertFalse(payload["current"]["cap_created_approval"])
        self.assertFalse(payload["proposed"]["cap_created_approval"])


class ObservedSequenceTests(unittest.TestCase):
    def test_observed_harness_records_real_service_and_publication_events(self):
        report = run_observed_review_sequence(
            (
                SequenceStep(head_sha="a" * 40, label="initial"),
                SequenceStep(
                    head_sha="b" * 40,
                    independently_approval_eligible=True,
                    label="verification",
                ),
            ),
            _policy(),
        )
        self.assertEqual(report.mode, "merge-focused")
        self.assertEqual(len(report.events), 2)
        self.assertTrue(all(event.provider_calls == 1 for event in report.events))
        self.assertEqual(
            [event.publication_status for event in report.events],
            ["published", "published"],
        )
        self.assertEqual(report.approval_events, 2)
        self.assertIsNone(report.cap_created_approval)
        self.assertEqual(report.cutover_status, "not_ready")
        self.assertTrue(report.unmet_criteria)
        self.assertTrue(report.unmet_criteria)
        self.assertEqual(
            report.to_dict()["events"][0]["publication_status"], "published"
        )


class ShadowObservationTests(unittest.TestCase):
    def test_shadow_rejects_legacy_and_is_observation_only(self):
        with self.assertRaisesRegex(ReviewInputError, "cannot be legacy"):
            resolve_shadow_review_mode("legacy")
        self.assertIsNone(resolve_shadow_review_mode())
        decision = observe_shadow_admission(
            RoundSessionState(
                completed_initial_reviews=1,
                completed_verification_rounds=2,
                latest_head_reviewed=True,
                coverage_complete=True,
            ),
            explicit="merge-focused",
        )
        self.assertIsNotNone(decision)
        self.assertFalse(decision.admit)
        self.assertTrue(decision.handoff)
        self.assertFalse(decision.may_emit_approve)

    def test_doctor_and_plan_display_shadow_without_changing_publication(self):
        with patch.dict("os.environ", {REVIEW_SHADOW_ENV: "merge-focused"}):
            doctor = run_doctor()
            plan = build_plan(diff=DIFF)
        check = next(
            item for item in doctor["checks"] if item["name"] == "review-shadow"
        )
        self.assertEqual(check["status"], "pass")
        self.assertIn("observation-only", check["detail"])
        self.assertEqual(doctor["review_convergence"]["mode"], "legacy")
        self.assertEqual(doctor["shadow_review_convergence"]["mode"], "merge-focused")
        self.assertEqual(plan["review_convergence"]["mode"], "legacy")
        self.assertEqual(plan["shadow_review_convergence"]["mode"], "merge-focused")
        self.assertFalse(plan["operations"]["publication"])
        rendered = render_diagnostic(doctor)
        self.assertIn("review_shadow: mode=merge-focused observation-only", rendered)
        with patch.dict("os.environ", {REVIEW_SHADOW_ENV: "legacy"}):
            invalid = run_doctor()
        shadow_check = next(
            item for item in invalid["checks"] if item["name"] == "review-shadow"
        )
        self.assertEqual(shadow_check["status"], "action")
        self.assertEqual(invalid["review_convergence"]["mode"], "legacy")

    def test_shadow_does_not_skip_legacy_publication(self):
        ledger = InMemorySessionLedger()
        record = SessionRecord.create(
            IDENTITY,
            now=FIXED_NOW,
            completed_initial_reviews=1,
            completed_verification_rounds=2,
        )
        ledger._records[(IDENTITY.repository, IDENTITY.pull_request)] = record
        reviewer = RecordingReviewer()
        application = GitHubApplication(
            broker=RecordingBroker(),
            http=None,
            reviewer=reviewer,
            learner=object(),
            replier=object(),
            session_ledger=ledger,
        )
        with patch.dict("os.environ", {REVIEW_SHADOW_ENV: "merge-focused"}):
            result = application.publish_review(
                options=GitHubWriteOptions(auto_review=True, github_writes=True),
                oidc_token="oidc",
                repository=IDENTITY.repository,
                repository_id=99,
                pull_request=IDENTITY.pull_request,
                head_sha=HEAD,
                base_branch="main",
                base_sha="b" * 40,
                result=ReviewResult(summary="ok", comments=(), provider="fixture"),
                diff="diff",
                app_slug="reviewsensei[bot]",
                convergence_policy=ReviewConvergencePolicy(),
            )
        self.assertEqual(result.status, "published")
        self.assertEqual(len(reviewer.calls), 1)
        self.assertIsNotNone(result.shadow)
        self.assertTrue(result.shadow["observation_only"])
        self.assertEqual(result.shadow["mode"], "merge-focused")
        self.assertFalse(result.shadow["admit"])
        self.assertTrue(result.shadow["handoff"])
        self.assertEqual(result.shadow["handoff_reason"], "round-budget-exhausted")


class EvaluateConvergenceCliTests(unittest.TestCase):
    def test_cli_emits_observed_real_component_evidence(self):
        stdout = io.StringIO()
        with redirect_stderr(io.StringIO()):
            with patch("sys.stdout", stdout):
                status = main(["evaluate-convergence", "--json", "--observed"])
        self.assertEqual(status, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["mode"], "merge-focused")
        self.assertTrue(payload["events"])
        self.assertEqual(payload["events"][0]["provider_calls"], 1)

    def test_cli_replays_sentinel_and_keeps_legacy_default(self):
        stdout = io.StringIO()
        with redirect_stderr(io.StringIO()):
            with patch("sys.stdout", stdout):
                status = main(["evaluate-convergence", "--json", "--compare-default"])
        self.assertEqual(status, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["publication_default"], "legacy")
        self.assertFalse(payload["cap_created_approval"])
        self.assertEqual(payload["proposed"]["mode"], "merge-focused")
        self.assertEqual(payload["current"]["mode"], "legacy")

    def test_cli_rejects_invalid_mode(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with patch("sys.stdout", stdout):
                status = main(["evaluate-convergence", "--review-mode", "soft"])
        self.assertEqual(status, 1)
        self.assertIn("unsupported", stderr.getvalue())


class PublicExportTests(unittest.TestCase):
    def test_sequence_exports_are_importable(self):
        import review_sensei

        self.assertIs(review_sensei.SequenceStep, SequenceStep)
        self.assertIs(review_sensei.replay_review_sequence, replay_review_sequence)
        self.assertIs(
            review_sensei.run_observed_review_sequence, run_observed_review_sequence
        )
        self.assertIs(review_sensei.observe_shadow_admission, observe_shadow_admission)
        self.assertIs(
            review_sensei.resolve_shadow_review_mode, resolve_shadow_review_mode
        )
