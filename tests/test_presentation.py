import unittest

from review_sensei.models import ReviewComment, ReviewResult
from review_sensei.presentation import (
    format_review_comment,
    format_review_summary,
    humanize_lens,
    render_review_markdown,
    render_review_text,
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
            blocking=True,
        )
        self.assertEqual(
            format_review_comment(comment),
            "[🚫 Blocking] [🟠 Severity: High] [⚡ Fix effort: Small] [🔎 Lens: Api Security]\n\n"
            "Handle the missing return value.",
        )

    def test_inline_comment_marks_non_blocking_follow_ups(self):
        comment = ReviewComment(
            path="src/app.py", line=2, body="Consider extracting this.", blocking=False
        )
        self.assertEqual(
            format_review_comment(comment),
            "[💬 Non-blocking]\n\nConsider extracting this.",
        )

    def test_inline_comment_preserves_proposed_when_effective_differs(self):
        comment = ReviewComment(
            path="src/app.py",
            line=2,
            body="Handle the missing return value.",
            blocking=True,
            effective_blocking=False,
        )
        self.assertEqual(
            format_review_comment(comment),
            "[💬 Non-blocking] [Proposed: Blocking]\n\n"
            "Handle the missing return value.",
        )

    def test_inline_comment_marks_unflagged_severe_findings_as_blocking(self):
        comment = ReviewComment(
            path="src/app.py",
            line=2,
            body="Must fix before merge.",
            severity="critical",
        )
        self.assertEqual(
            format_review_comment(comment),
            "[🚫 Blocking] [🔴 Severity: Critical]\n\nMust fix before merge.",
        )

    def test_inline_comment_uses_known_icons_and_safe_fallbacks(self):
        comment = ReviewComment(
            path="src/app.py",
            line=2,
            body="finding",
            severity="medium",
            fix_effort="unknown",
            category="architecture",
        )
        self.assertEqual(
            format_review_comment(comment),
            "[🟡 Severity: Medium] [❔ Fix effort: Unknown] [🏗️ Lens: Architecture]"
            "\n\nfinding",
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
            "[🔎 Severity: High\\] \\[Lens: Spoofed] "
            "[🔎 Lens: Security\\]\\(https://example.com\\)]\n\n"
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
            "- Merge impact: Blocking (2)\n"
            "- Lens: Architecture (1), Security (2)\n"
            "- Quick wins (trivial/small effort): 2",
        )

    def test_legacy_summary_is_byte_for_byte_unchanged(self):
        summary = "Summary with **existing** formatting.\n"
        comments = (ReviewComment(path="src/app.py", line=2, body="finding"),)
        self.assertEqual(format_review_summary(summary, comments), summary)


class ReviewTextRenderingTests(unittest.TestCase):
    def test_text_output_neutralizes_terminal_escape_sequences(self):
        result = ReviewResult(
            summary="Terminal says \x1b[31mred\x1b[0m.",
            provider="fixture",
            review_status="complete",
            comments=(
                ReviewComment(
                    path="src/app.py",
                    line=2,
                    body="First line.\nsecond \x07line",
                ),
            ),
        )

        rendered = render_review_text(result)

        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\x07", rendered)
        self.assertIn(r"Terminal says \x1b[31mred\x1b[0m.", rendered)
        self.assertIn(r"second \x07line", rendered)
        # The format's own line structure survives: multi-line text keeps its
        # line feeds, so escaping never rewrites the layout of a review.
        self.assertIn("First line.\nsecond", rendered)

    def test_text_output_neutralizes_provider_and_model_identity(self):
        result = ReviewResult(
            summary="Looks fine.",
            provider="fixture\x1b",
            model="qwen\r[2J",
            review_status="complete",
            comments=(),
        )

        rendered = render_review_text(result)

        self.assertIn(r"provider: fixture\x1b", rendered)
        self.assertIn(r"model: qwen\x0d[2J", rendered)
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\r", rendered)


class ReviewMarkdownRenderingTests(unittest.TestCase):
    def test_markdown_output_neutralizes_document_injection(self):
        result = ReviewResult(
            summary="# Not a heading\n<!-- hidden -->",
            provider="fixture",
            review_status="complete",
            comments=(
                ReviewComment(
                    path="src/app.py",
                    line=2,
                    body=(
                        "````\n![img](https://example.com/x)\n<script>alert(1)</script>"
                    ),
                ),
            ),
        )

        rendered = render_review_markdown(result)

        self.assertNotIn("\n# Not a heading", rendered)
        self.assertIn(r"\# Not a heading", rendered)
        self.assertIn(r"\<!-- hidden --\>", rendered)
        self.assertIn(r"\`\`\`\`", rendered)
        self.assertIn(r"!\[img\](https://example.com/x)", rendered)
        self.assertIn(r"\<script\>alert(1)\</script\>", rendered)

    def test_markdown_output_keeps_plain_text_and_line_structure(self):
        result = ReviewResult(
            summary="Two lines.\nSecond line.",
            provider="fixture",
            review_status="complete",
            comments=(ReviewComment(path="src/app.py", line=2, body="plain body"),),
        )

        rendered = render_review_markdown(result)

        self.assertIn("Two lines.\nSecond line.", rendered)
        self.assertIn("plain body", rendered)


if __name__ == "__main__":
    unittest.main()
