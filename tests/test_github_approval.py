import unittest

from review_sensei.hosting.github.approval import evaluate_auto_approval
from review_sensei.models import ReviewComment, ReviewResult


def clean_result() -> ReviewResult:
    return ReviewResult(
        summary="Summary.",
        comments=(),
        provider="fixture",
        review_status="complete",
    )


class AutoApprovalPolicyTests(unittest.TestCase):
    def test_clean_review_with_resolved_threads_is_approved(self):
        decision = evaluate_auto_approval(
            enabled=True,
            app_authored=False,
            result=clean_result(),
            has_open_review_threads=False,
        )
        self.assertTrue(decision.approved)
        self.assertEqual(decision.blockers, ())

    def test_open_threads_block_clean_review(self):
        blocked = evaluate_auto_approval(
            enabled=True,
            app_authored=False,
            result=clean_result(),
            has_open_review_threads=True,
        )
        self.assertFalse(blocked.approved)
        self.assertEqual(blocked.blockers, ("review-threads-open",))

    def test_open_findings_and_threads_are_independent_blockers(self):
        result = ReviewResult(
            summary="Summary.",
            comments=(
                ReviewComment(
                    path="src/app.py",
                    line=2,
                    body="finding",
                    blocking=True,
                    severity="low",
                    fix_effort="small",
                    category="maintainability",
                ),
            ),
            provider="fixture",
            review_status="complete",
        )
        decision = evaluate_auto_approval(
            enabled=True,
            app_authored=False,
            result=result,
            has_open_review_threads=True,
        )
        self.assertFalse(decision.approved)
        self.assertEqual(
            decision.blockers,
            ("blocking-findings-open", "review-threads-open"),
        )

    def test_explicitly_non_blocking_findings_do_not_block_approval(self):
        result = ReviewResult(
            summary="Summary.",
            comments=(
                ReviewComment(
                    path="src/app.py",
                    line=2,
                    body="Optional follow-up.",
                    blocking=False,
                ),
            ),
            provider="fixture",
            review_status="complete",
        )

        decision = evaluate_auto_approval(
            enabled=True,
            app_authored=False,
            result=result,
            has_open_review_threads=False,
        )

        self.assertTrue(decision.approved)
        self.assertEqual(decision.blockers, ())

    def test_blocking_findings_prevent_approval(self):
        result = ReviewResult(
            summary="Summary.",
            comments=(
                ReviewComment(
                    path="src/app.py", line=2, body="Must fix.", blocking=True
                ),
            ),
            provider="fixture",
            review_status="complete",
        )

        decision = evaluate_auto_approval(
            enabled=True,
            app_authored=False,
            result=result,
            has_open_review_threads=False,
        )

        self.assertFalse(decision.approved)
        self.assertEqual(decision.blockers, ("blocking-findings-open",))

    def test_unclassified_severe_findings_prevent_approval(self):
        for severity in ("critical", "high"):
            with self.subTest(severity=severity):
                result = ReviewResult(
                    summary="Summary.",
                    comments=(
                        ReviewComment(
                            path="src/app.py",
                            line=2,
                            body="Severe finding",
                            severity=severity,
                        ),
                    ),
                    provider="fixture",
                    review_status="complete",
                )

                decision = evaluate_auto_approval(
                    enabled=True,
                    app_authored=False,
                    result=result,
                    has_open_review_threads=False,
                )

                self.assertFalse(decision.approved)
                self.assertEqual(decision.blockers, ("blocking-findings-open",))

    def test_unclassified_low_severity_findings_do_not_prevent_approval(self):
        result = ReviewResult(
            summary="Summary.",
            comments=(
                ReviewComment(
                    path="src/app.py", line=2, body="Minor follow-up", severity="low"
                ),
            ),
            provider="fixture",
            review_status="complete",
        )

        decision = evaluate_auto_approval(
            enabled=True,
            app_authored=False,
            result=result,
            has_open_review_threads=False,
        )

        self.assertTrue(decision.approved)
        self.assertEqual(decision.blockers, ())

    def test_unclassified_findings_without_severity_do_not_prevent_approval(self):
        result = ReviewResult(
            summary="Summary.",
            comments=(
                ReviewComment(path="src/app.py", line=2, body="Follow-up finding"),
            ),
            provider="fixture",
            review_status="complete",
        )

        decision = evaluate_auto_approval(
            enabled=True,
            app_authored=False,
            result=result,
            has_open_review_threads=False,
        )

        self.assertTrue(decision.approved)
        self.assertEqual(decision.blockers, ())

    def test_unclassified_free_form_severity_findings_do_not_prevent_approval(self):
        for severity in ("warning", "suggestion"):
            with self.subTest(severity=severity):
                result = ReviewResult(
                    summary="Summary.",
                    comments=(
                        ReviewComment(
                            path="src/app.py",
                            line=2,
                            body="Non-severe follow-up",
                            severity=severity,
                        ),
                    ),
                    provider="fixture",
                    review_status="complete",
                )

                decision = evaluate_auto_approval(
                    enabled=True,
                    app_authored=False,
                    result=result,
                    has_open_review_threads=False,
                )

                self.assertTrue(decision.approved)
                self.assertEqual(decision.blockers, ())

    def test_app_authored_pull_requests_cannot_be_approved(self):
        decision = evaluate_auto_approval(
            enabled=True,
            app_authored=True,
            result=clean_result(),
            has_open_review_threads=False,
        )
        self.assertFalse(decision.approved)
        self.assertEqual(decision.blockers, ("app-authored-pull-request",))

    def test_approval_is_enabled_by_default(self):
        decision = evaluate_auto_approval(
            app_authored=False,
            result=clean_result(),
            has_open_review_threads=False,
        )
        self.assertTrue(decision.approved)
        self.assertEqual(decision.blockers, ())

    def test_non_boolean_approval_inputs_fail_closed(self):
        decision = evaluate_auto_approval(
            enabled="true",  # type: ignore[arg-type]
            app_authored=0,  # type: ignore[arg-type]
            result=clean_result(),
            has_open_review_threads="false",  # type: ignore[arg-type]
        )
        self.assertFalse(decision.approved)
        self.assertIn("auto-approval-enabled-invalid", decision.blockers)
        self.assertIn("app-authored-flag-invalid", decision.blockers)
        self.assertIn("review-threads-invalid", decision.blockers)

    def test_non_complete_results_cannot_be_approved(self):
        for status in ("partial", "incomplete", "summary-only"):
            with self.subTest(status=status):
                result = ReviewResult(
                    summary="Summary.",
                    comments=(),
                    provider="fixture",
                    review_status=status,
                )
                decision = evaluate_auto_approval(
                    enabled=True,
                    app_authored=False,
                    result=result,
                    has_open_review_threads=False,
                )
                self.assertFalse(decision.approved)
                self.assertEqual(decision.blockers, (f"review-{status}",))

    def test_incomplete_thread_sweep_fails_closed(self):
        decision = evaluate_auto_approval(
            enabled=True,
            app_authored=False,
            result=clean_result(),
            has_open_review_threads=None,
        )
        self.assertFalse(decision.approved)
        self.assertEqual(decision.blockers, ("review-threads-incomplete",))

    def test_legacy_result_without_status_fails_closed(self):
        legacy = ReviewResult.from_dict(
            {"summary": "Summary.", "comments": [], "provider": "fixture"}
        )
        self.assertEqual(legacy.review_status, "incomplete")
        self.assertEqual(legacy.to_dict()["review_status"], "incomplete")
        decision = evaluate_auto_approval(
            enabled=True,
            app_authored=False,
            result=legacy,
            has_open_review_threads=False,
        )
        self.assertFalse(decision.approved)
        self.assertEqual(decision.blockers, ("review-incomplete",))

    def test_duck_typed_result_without_status_fails_closed(self):
        class DuckResult:
            comments = ()

        decision = evaluate_auto_approval(
            enabled=True,
            app_authored=False,
            result=DuckResult(),  # type: ignore[arg-type]
            has_open_review_threads=False,
        )
        self.assertFalse(decision.approved)
        self.assertIn("review-result-invalid", decision.blockers)
        self.assertIn("review-incomplete", decision.blockers)
        self.assertNotIn("evidence-policy-invalid", decision.blockers)

    def test_incompletely_verified_review_cannot_be_approved(self):
        result = ReviewResult(
            summary="Verification coverage: confirmed=0, rejected=1.",
            comments=(),
            provider="fixture",
            review_status="partial",
            evidence_policy="confirmed",
        )
        decision = evaluate_auto_approval(
            enabled=True,
            app_authored=False,
            result=result,
            has_open_review_threads=False,
        )
        self.assertFalse(decision.approved)
        self.assertIn("review-partial", decision.blockers)
        self.assertIn("review-unverified", decision.blockers)


if __name__ == "__main__":
    unittest.main()
