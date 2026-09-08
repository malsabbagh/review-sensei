import unittest

from review_sensei.models import ReviewComment
from review_sensei.presentation import (
    format_review_comment,
    format_review_summary,
    humanize_lens,
)


class ReviewPresentationTests(unittest.TestCase):
    def test_humanizes_stable_lens_ids(self):
        self.assertEqual(humanize_lens("api-security"), "Api Security")
        self.assertEqual(humanize_lens("test_coverage"), "Test Coverage")

    def test_inline_comment_includes_independent_classification_labels(self):
        comment = ReviewComment(
            path="src/app.py",
            line=2,
            body="Handle the missing return value.",
            severity="high",
            fix_effort="small",
            category="api-security",
        )
        self.assertEqual(
            format_review_comment(comment),
            "[Severity: High] [Fix effort: Small] [Lens: Api Security]\n\n"
            "Handle the missing return value.",
        )

    def test_legacy_inline_comment_is_unchanged(self):
        comment = ReviewComment(path="src/app.py", line=2, body="finding")
        self.assertEqual(format_review_comment(comment), "finding")

    def test_escapes_markdown_metacharacters_in_untrusted_labels(self):
        comment = ReviewComment(
            path="src/app.py",
            line=2,
            body="finding",
            severity="high] [Lens: Spoofed",
            category="security](https://example.com)",
        )
        self.assertEqual(
            format_review_comment(comment),
            "[Severity: High\\] \\[Lens: Spoofed] "
            "[Lens: Security\\]\\(https://example.com\\)]\n\n"
            "finding",
        )

    def test_escapes_line_breaks_before_markdown_label_rendering(self):
        from review_sensei.presentation import _escape_markdown_label

        self.assertEqual(
            _escape_markdown_label("line\nfeed\rreturn"),
            r"line\nfeed\rreturn",
        )

    def test_summary_counts_are_deterministic_and_include_quick_wins(self):
        comments = (
            ReviewComment(
                path="src/one.py",
                line=2,
                body="one",
                severity="medium",
                fix_effort="moderate",
                category="security",
            ),
            ReviewComment(
                path="src/two.py",
                line=2,
                body="two",
                severity="critical",
                fix_effort="trivial",
                category="architecture",
            ),
            ReviewComment(
                path="src/three.py",
                line=2,
                body="three",
                severity="critical",
                fix_effort="small",
                category="security",
            ),
        )
        self.assertEqual(
            format_review_summary("Summary.", comments),
            "Summary.\n\n"
            "Review classification:\n"
            "- Severity: Critical (2), Medium (1)\n"
            "- Lens: Architecture (1), Security (2)\n"
            "- Quick wins (trivial/small effort): 2",
        )

    def test_legacy_summary_is_byte_for_byte_unchanged(self):
        summary = "Summary with **existing** formatting.\n"
        comments = (ReviewComment(path="src/app.py", line=2, body="finding"),)
        self.assertEqual(format_review_summary(summary, comments), summary)


if __name__ == "__main__":
    unittest.main()
