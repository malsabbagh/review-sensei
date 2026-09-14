import unittest

from review_sensei.hosting.github.approval import evaluate_auto_approval
from review_sensei.models import ReviewComment, ReviewResult


def clean_result() -> ReviewResult:
    return ReviewResult(summary="Summary.", comments=(), provider="fixture")


class AutoApprovalPolicyTests(unittest.TestCase):
    def test_clean_review_with_resolved_threads_is_approved(self):
        decision = evaluate_auto_approval(
            app_authored=False,
            result=clean_result(),
            has_open_review_threads=False,
        )
        self.assertTrue(decision.approved)
        self.assertEqual(decision.blockers, ())

    def test_open_threads_block_clean_review(self):
        blocked = evaluate_auto_approval(
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
        )
        decision = evaluate_auto_approval(
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
        )

        decision = evaluate_auto_approval(
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
        )

        decision = evaluate_auto_approval(
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
                )

                decision = evaluate_auto_approval(
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
        )

        decision = evaluate_auto_approval(
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
        )

        decision = evaluate_auto_approval(
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
                )

                decision = evaluate_auto_approval(
                    app_authored=False,
                    result=result,
                    has_open_review_threads=False,
                )

                self.assertTrue(decision.approved)
                self.assertEqual(decision.blockers, ())

    def test_app_authored_pull_requests_cannot_be_approved(self):
        decision = evaluate_auto_approval(
            app_authored=True,
            result=clean_result(),
            has_open_review_threads=False,
        )
        self.assertFalse(decision.approved)
        self.assertEqual(decision.blockers, ("app-authored-pull-request",))


if __name__ == "__main__":
    unittest.main()
