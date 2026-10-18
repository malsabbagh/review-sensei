"""Installed approval journey; fixtures and broker authority are synthetic only."""

import copy
import itertools
import json
import socket
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

_TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TESTS))
from packaging_guard import assert_distribution_import  # noqa: E402

assert_distribution_import()

import review_sensei_github  # noqa: E402
from review_sensei_github.approval import (  # noqa: E402
    ReviewApprovalEligibility,
    approval_eligibility_from_result,
)
from review_sensei_github.checks import check_outcome_for_result  # noqa: E402

from review_sensei.convergence import (  # noqa: E402
    BlockerCandidate,
    ReviewConvergencePolicy,
    RoundSessionState,
    admit_review_result,
    evaluate_round_admission,
)
from review_sensei.coverage import CoverageManifest, FileCoverage  # noqa: E402
from review_sensei.errors import ReviewInputError  # noqa: E402
from review_sensei.human_file_review import UnsupportedFile  # noqa: E402
from review_sensei.mixed_coverage import (  # noqa: E402
    BinaryConfirmation,
    evaluate_mixed_coverage,
)
from review_sensei.models import ReviewComment, ReviewResult  # noqa: E402


def load_tests(loader, tests, pattern):
    # Reuse the unchanged incident regression instead of a second implementation.
    # This imports only a packaged helper plus the two preserved public fixtures.
    from test_pr308_ledger_regression import PreservedLedgerRegressionTests

    tests.addTests(loader.loadTestsFromTestCase(PreservedLedgerRegressionTests))
    return tests


class InstalledApprovalLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(
            Path(review_sensei_github.__file__)
            .resolve()
            .is_relative_to(Path(sys.prefix).resolve())
        )
        self.network = patch.object(
            socket.socket,
            "connect",
            side_effect=AssertionError("live network forbidden"),
        )
        self.network.start()
        self.addCleanup(self.network.stop)
        self.coverage = CoverageManifest(
            files=(FileCoverage("src/app.py", "reviewed"),),
            enumerated_paths=("src/app.py",),
        )

    def result(self, comments=()):
        return ReviewResult(
            "Synthetic lifecycle review.",
            comments,
            "fixture",
            review_status="complete",
            coverage=self.coverage,
        )

    def test_classification_survives_eligibility_restart_and_agrees_with_check(self):
        policy = ReviewConvergencePolicy(mode="merge-focused")
        for fix, decision, optional in itertools.product((False, True), repeat=3):
            with self.subTest(fix=fix, decision=decision, optional=optional):
                comments, candidates = [], []
                for enabled, fields in (
                    (fix, {}),
                    (decision, {"needs_human": True}),
                    (optional, {"is_preference_or_optional": True}),
                ):
                    if not enabled:
                        continue
                    comments.append(
                        ReviewComment(
                            "src/app.py",
                            len(comments) + 1,
                            "Synthetic finding " + str(len(comments)),
                            blocking=True,
                            severity="high",
                        )
                    )
                    candidates.append(
                        BlockerCandidate(
                            path=comments[-1].path,
                            line=comments[-1].line,
                            proposed_blocking=True,
                            severity="high",
                            has_specific_violation=True,
                            has_actionable_remedy=True,
                            evidence_locations_validated=True,
                            has_failure_condition=True,
                            attribution="pr-change",
                            **fields,
                        )
                    )
                admitted = admit_review_result(
                    self.result(tuple(comments)), policy, candidates=tuple(candidates)
                )
                check = check_outcome_for_result(admitted, policy="blocking")
                self.assertEqual(
                    check.conclusion,
                    "failure" if fix else "action_required" if decision else "success",
                )
                eligibility = approval_eligibility_from_result(
                    admitted,
                    head_sha="a" * 40,
                    base_sha="b" * 40,
                    enabled=True,
                    app_authored=False,
                )
                restarted = ReviewApprovalEligibility.from_dict(
                    json.loads(json.dumps(eligibility.to_dict()))
                )
                approved = restarted.evaluate(
                    app_authored=False, has_open_review_threads=False
                )
                self.assertEqual(approved.approved, not (fix or decision))
                self.assertEqual(restarted.facts.has_blocking_findings, fix)
                self.assertEqual(
                    restarted.facts.has_human_adjudication_findings, decision
                )
                self.assertFalse(
                    restarted.evaluate(
                        app_authored=False, has_open_review_threads=None
                    ).approved
                )

    def test_eligibility_schema_drift_and_broader_review_cannot_erase_obligations(self):
        comment = ReviewComment(
            "src/app.py",
            1,
            "Resolve a synthetic design decision.",
            blocking=False,
            needs_human=True,
        )
        result = self.result((comment,))
        retained = approval_eligibility_from_result(
            result,
            head_sha="a" * 40,
            base_sha="b" * 40,
            enabled=True,
            app_authored=False,
        )
        broader = approval_eligibility_from_result(
            self.result(),
            head_sha="a" * 40,
            base_sha="b" * 40,
            enabled=True,
            app_authored=False,
            retained_eligibility=retained,
        )
        restarted = ReviewApprovalEligibility.from_dict(broader.to_dict())
        self.assertFalse(
            restarted.evaluate(
                app_authored=False, has_open_review_threads=False
            ).approved
        )
        for head, base in (("c" * 40, "b" * 40), ("a" * 40, "c" * 40)):
            with self.assertRaises(ReviewInputError):
                approval_eligibility_from_result(
                    self.result(),
                    head_sha=head,
                    base_sha=base,
                    enabled=True,
                    app_authored=False,
                    retained_eligibility=retained,
                )
        for key, value in (
            ("schema_version", "future"),
            ("unknown", True),
            ("head_sha", "invalid"),
        ):
            document = copy.deepcopy(retained.to_dict())
            document[key] = value
            with self.assertRaises(ReviewInputError):
                ReviewApprovalEligibility.from_dict(document)

    def test_mixed_generated_and_media_confirmation_agreement_and_revocation(self):
        inventory = tuple(
            UnsupportedFile(path, "added", None, digest * 40, new_mode="100644")
            for path, digest in (("photo.png", "a"), ("generated.json", "b"))
        )
        result = replace(
            self.result(),
            review_status="partial",
            coverage_only_partial=True,
            coverage=CoverageManifest(
                files=(
                    FileCoverage("src/app.py", "reviewed"),
                    FileCoverage("photo.png", "unsupported", "binary"),
                    FileCoverage("generated.json", "excluded-by-policy", "generated"),
                ),
                enumerated_paths=("src/app.py", "photo.png", "generated.json"),
            ),
        )
        before = result.to_dict()
        confirmations = tuple(
            BinaryConfirmation(
                file=item,
                event_id="event",
                valid=True,
                permission_current=True,
                current=True,
            )
            for item in inventory
        )
        for label, receipts, expected in (
            ("current", confirmations, True),
            ("missing-generated", confirmations[:1], False),
            (
                "revoked-media",
                (replace(confirmations[0], permission_current=False), confirmations[1]),
                False,
            ),
            (
                "stale-generated",
                (confirmations[0], replace(confirmations[1], current=False)),
                False,
            ),
        ):
            with self.subTest(label=label):
                decision = evaluate_mixed_coverage(
                    result,
                    inventory,
                    receipts,
                    text_completed=True,
                    allow_confirmations=True,
                    event_id="event",
                )
                self.assertEqual(decision.approved, expected)
                check = check_outcome_for_result(
                    result,
                    policy="blocking",
                    mixed_coverage_satisfied=decision.coverage_satisfied,
                )
                self.assertEqual(
                    check.conclusion, "success" if expected else "action_required"
                )
                self.assertFalse(decision.baseline_completed)
                self.assertFalse(decision.cache_completed)
                self.assertEqual(result.to_dict(), before)

    def test_failed_attempt_budget_never_mints_approval_and_completed_rounds_do_not_cap(
        self,
    ):
        policy = ReviewConvergencePolicy(mode="merge-focused")
        state = RoundSessionState(
            completed_initial_reviews=500,
            completed_verification_rounds=500,
            failed_attempts=policy.max_failed_attempts,
        )
        exhausted = evaluate_round_admission(state, policy)
        self.assertFalse(exhausted.admit)
        self.assertFalse(exhausted.may_emit_approve)
        self.assertEqual(exhausted.handoff_reason, "failed-attempt-budget-exhausted")
        continuing = evaluate_round_admission(replace(state, failed_attempts=0), policy)
        self.assertTrue(continuing.admit)
        self.assertFalse(continuing.may_emit_approve)
