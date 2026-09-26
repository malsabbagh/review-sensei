import unittest

from review_sensei.models import ReviewComment
from review_sensei.presentation import (
    ADVISORY_STATE,
    CHANGES_REQUIRED_STATE,
    NO_REQUIRED_FIXES_STATE,
    FindingView,
    ReviewSummaryView,
    assign_finding_identifiers,
    build_finding_view,
    build_review_summary_view,
    escape_markdown_label,
    finding_identifier,
    render_finding,
    render_finding_reference,
    render_review_summary,
    sanitize_finding_markdown,
)

FINGERPRINT = "0123456789abcdef" * 4
OTHER_FINGERPRINT = "fedcba9876543210" * 4


def _comment(**overrides):
    values = {"path": "src/app.py", "line": 2, "body": "Handle the missing value."}
    values.update(overrides)
    return ReviewComment(**values)


class FindingIdentifierTests(unittest.TestCase):
    def test_projects_a_fingerprint_onto_one_stable_readable_id(self):
        self.assertEqual(finding_identifier(FINGERPRINT), "RS-012345")
        self.assertEqual(
            finding_identifier(FINGERPRINT), finding_identifier(FINGERPRINT)
        )

    def test_rejects_a_missing_or_invalid_fingerprint(self):
        with self.assertRaises(Exception):
            finding_identifier("not-a-digest")

    def test_assignment_is_order_independent_and_unique(self):
        identifiers = assign_finding_identifiers([FINGERPRINT, OTHER_FINGERPRINT])
        reordered = assign_finding_identifiers([OTHER_FINGERPRINT, FINGERPRINT])
        self.assertEqual(identifiers, reordered)
        self.assertEqual(len(set(identifiers.values())), 2)

    def test_colliding_prefixes_widen_deterministically(self):
        left = "abcdef" + "0" * 58
        right = "abcdef" + "1" * 58
        identifiers = assign_finding_identifiers([left, right])
        self.assertEqual(identifiers[left], "RS-ABCDEF00")
        self.assertEqual(identifiers[right], "RS-ABCDEF11")
        # A non-colliding fingerprint keeps the short readable form.
        single = assign_finding_identifiers([FINGERPRINT])
        self.assertEqual(single[FINGERPRINT], "RS-012345")


class FindingRenderingTests(unittest.TestCase):
    def _required_view(self, **overrides):
        values = {
            "identifier": "RS-014",
            "requirement": "required",
            "severity": "high",
            "title": "Scope the query to the authenticated tenant",
            "detail": "The lookup is not bound to the authenticated identity.",
        }
        values.update(overrides)
        return FindingView(**values)

    def test_required_finding_matches_the_required_template(self):
        self.assertEqual(
            render_finding(self._required_view()),
            "**Required fix · High impact — Scope the query to the authenticated tenant**\n"
            "\n"
            "The lookup is not bound to the authenticated identity.\n"
            "\n"
            "`RS-014`",
        )

    def test_optional_finding_states_it_is_not_required(self):
        rendered = render_finding(
            FindingView(
                identifier="RS-015",
                requirement="optional",
                title="Cover an empty result page",
                detail="An empty-page case would protect the pagination behavior.",
            )
        )
        self.assertEqual(
            rendered,
            "**Optional improvement — Cover an empty result page**\n"
            "\n"
            "An empty-page case would protect the pagination behavior.\n"
            "\n"
            "This is not required for this PR.\n"
            "\n"
            "`RS-015`",
        )

    def test_advisory_keeps_defect_severity_without_enforcement_language(self):
        rendered = render_finding(
            self._required_view(
                title="Drop the unvalidated tenant filter",
                advisory=True,
                detail="The filter is not validated.",
            )
        )
        self.assertEqual(
            rendered,
            "**Defect · High impact — Drop the unvalidated tenant filter**\n"
            "\n"
            "The filter is not validated.\n"
            "\n"
            "`RS-014`",
        )
        self.assertNotIn("Required fix", rendered)
        self.assertNotIn("Optional improvement", rendered)

    def test_missing_severity_and_title_drop_only_those_segments(self):
        rendered = render_finding(
            FindingView(identifier="RS-016", detail="Explain the concern.")
        )
        self.assertEqual(
            rendered, "**Required fix**\n\nExplain the concern.\n\n`RS-016`"
        )

    def test_view_mapping_uses_comment_classification(self):
        view = build_finding_view(
            _comment(severity="critical", defect_kind="data-loss", blocking=True),
            fingerprint=FINGERPRINT,
        )
        self.assertEqual(view.identifier, "RS-012345")
        self.assertEqual(view.requirement, "required")
        self.assertEqual(view.title, "Data Loss")
        self.assertIn("Critical impact", render_finding(view))

    def test_view_mapping_skips_unspecific_defect_kinds(self):
        view = build_finding_view(
            _comment(severity="low", defect_kind="unknown", blocking=False),
            fingerprint=FINGERPRINT,
        )
        self.assertIsNone(view.title)
        self.assertEqual(view.requirement, "optional")

    def test_summary_reference_points_at_its_placement(self):
        inline = render_finding_reference(self._required_view(placement="inline"))
        self.assertEqual(
            inline,
            "- **RS-014:** Scope the query to the authenticated tenant. "
            "See the inline discussion.",
        )
        body = render_finding_reference(self._required_view(placement="body"))
        self.assertIn("See the full explanation below.", body)

    def test_unchanged_optional_findings_are_not_repeated_in_full(self):
        view = build_finding_view(
            _comment(blocking=False),
            fingerprint=FINGERPRINT,
            lifecycle_state="still-present",
        )
        self.assertFalse(view.repeat)
        self.assertIn(
            "Previously reported and unchanged.", render_finding_reference(view)
        )
        # Required findings always keep their full reference.
        required = build_finding_view(
            _comment(blocking=True),
            fingerprint=FINGERPRINT,
            lifecycle_state="still-present",
        )
        self.assertTrue(required.repeat)


class EscapingTests(unittest.TestCase):
    def test_escapes_markdown_metacharacters_in_untrusted_labels(self):
        self.assertEqual(
            escape_markdown_label("security](https://example.com)"),
            "security\\]\\(https://example.com\\)",
        )

    def test_escapes_line_breaks_before_markdown_label_rendering(self):
        self.assertEqual(
            escape_markdown_label("line\nfeed\rreturn"),
            r"line\nfeed\rreturn",
        )

    def test_preserves_newlines_code_blocks_and_real_links(self):
        body = (
            "Reproduce with:\n\n"
            "```python\n"
            "call(user_id=other_tenant)\n"
            "```\n\n"
            "See [the adapter](https://docs.github.com/rest)."
        )
        self.assertEqual(sanitize_finding_markdown(body), body)

    def test_preserves_unicode_and_inline_code_spans(self):
        body = "Naïve path `src/über.py` fails on `ß`."
        self.assertEqual(sanitize_finding_markdown(body), body)

    def test_normalizes_windows_line_endings(self):
        self.assertEqual(sanitize_finding_markdown("a\r\nb\rc"), "a\nb\nc")

    def test_neutralizes_forged_markers_and_html_comments(self):
        forged = (
            "<!-- reviewsensei:finding:v2 repo=1 pr=1 head=a base=b fingerprint=c -->"
        )
        sanitized = sanitize_finding_markdown(forged)
        self.assertNotIn("<!--", sanitized)
        self.assertIn(r"<\!--", sanitized)

    def test_neutralizes_forged_markers_inside_code_fences(self):
        body = "```\n<!-- reviewsensei:finding:v2 repo=1 -->\n```"
        self.assertNotIn("<!--", sanitize_finding_markdown(body))

    def test_escapes_mentions_outside_code(self):
        self.assertEqual(
            sanitize_finding_markdown("Ask @maintainer about @sensei."),
            r"Ask \@maintainer about \@sensei.",
        )
        self.assertEqual(
            sanitize_finding_markdown("Run `@sensei review` here."),
            "Run `@sensei review` here.",
        )

    def test_neutralizes_unbalanced_fences(self):
        sanitized = sanitize_finding_markdown("```python\nprint('x')")
        self.assertNotIn("```", sanitized)
        self.assertIn(r"\`\`\`", sanitized)

    def test_keeps_balanced_fences_verbatim(self):
        body = "```\ncode\n```"
        self.assertEqual(sanitize_finding_markdown(body), body)

    def test_neutralizes_unsafe_link_destinations(self):
        sanitized = sanitize_finding_markdown("Click [here](javascript:alert(1)) now.")
        self.assertEqual(sanitized, r"Click \[here](javascript:alert(1)) now.")

    def test_keeps_relative_and_anchor_links(self):
        body = "See [below](#next-action) and [the file](../src/app.py)."
        self.assertEqual(sanitize_finding_markdown(body), body)

    def test_long_content_is_preserved_without_boundary_loss(self):
        body = "x" * 20_000
        self.assertEqual(len(sanitize_finding_markdown(body)), 20_000)


class SummaryRenderingTests(unittest.TestCase):
    def _required(self, identifier="RS-014"):
        return FindingView(
            identifier=identifier,
            requirement="required",
            severity="high",
            title="Bind tenant scope to the authenticated identity",
            detail="detail",
            placement="inline",
        )

    def _optional(self, identifier="RS-015"):
        return FindingView(
            identifier=identifier,
            requirement="optional",
            title="Add an empty-page regression test",
            detail="detail",
            placement="inline",
        )

    def test_summary_matches_the_changes_required_template(self):
        view = build_review_summary_view(
            state=CHANGES_REQUIRED_STATE,
            head_sha="abc1234" + "0" * 33,
            overview="Two concerns were found.",
            coverage_line="Coverage: complete within the configured review scope",
            required=(self._required(),),
            optional=(self._optional(),),
        )
        self.assertEqual(
            render_review_summary(view),
            "## ReviewSensei — Changes required\n"
            "\n"
            "Reviewed head: `abc1234`\n"
            "Coverage: complete within the configured review scope\n"
            "\n"
            "Two concerns were found.\n"
            "\n"
            "**1 required fix · 1 optional improvement**\n"
            "\n"
            "### Required\n"
            "- **RS-014:** Bind tenant scope to the authenticated identity. "
            "See the inline discussion.\n"
            "\n"
            "### Optional\n"
            "- **RS-015:** Add an empty-page regression test. "
            "See the inline discussion.\n"
            "\n"
            "### Next action\n"
            "Address RS-014 and run verification. "
            "Optional suggestions do not affect the review gate.",
        )

    def test_advisory_summary_states_enforcement_is_disabled(self):
        view = build_review_summary_view(
            state=ADVISORY_STATE,
            head_sha="a" * 40,
            required=(self._required(),),
        )
        rendered = render_review_summary(view)
        self.assertIn(
            "## ReviewSensei — Advisory review (enforcement disabled)", rendered
        )
        self.assertIn("enforcement is disabled", rendered)

    def test_no_required_fixes_heading_states_the_result_without_approval_claims(self):
        view = build_review_summary_view(
            state=NO_REQUIRED_FIXES_STATE,
            head_sha="b" * 40,
            optional=(self._optional(),),
        )
        rendered = render_review_summary(view)
        self.assertIn("## ReviewSensei — No required fixes found", rendered)
        self.assertNotIn("Approved", rendered)
        self.assertIn("**No required fixes · 1 optional improvement**", rendered)

    def test_only_three_optional_items_are_highlighted_and_the_rest_group(self):
        optional = tuple(self._optional(f"RS-{index:03d}") for index in range(10, 15))
        view = build_review_summary_view(
            state=CHANGES_REQUIRED_STATE,
            head_sha="c" * 40,
            required=(self._required(),),
            optional=optional,
        )
        rendered = render_review_summary(view)
        optional_section = rendered.split("### Optional", 1)[1]
        self.assertEqual(optional_section.count("- **RS-"), 3)
        self.assertEqual(rendered.count("- **RS-"), 4)
        self.assertIn(
            "2 additional optional suggestions are covered in this review body.",
            rendered,
        )
        self.assertIn("**1 required fix · 5 optional improvements**", rendered)

    def test_required_findings_are_never_capped_and_keep_stable_order(self):
        required = tuple(self._required(f"RS-{index:03d}") for index in range(20, 25))
        view = build_review_summary_view(
            state=CHANGES_REQUIRED_STATE, head_sha="d" * 40, required=required
        )
        rendered = render_review_summary(view)
        self.assertEqual(rendered.count("- **RS-"), 5)
        self.assertIn("**5 required fixes**", rendered)
        self.assertIn(
            "Address the required fixes above and run verification.", rendered
        )
        self.assertEqual(render_review_summary(view), rendered)

    def test_empty_sections_are_omitted(self):
        view = build_review_summary_view(
            state=NO_REQUIRED_FIXES_STATE, head_sha="e" * 40
        )
        rendered = render_review_summary(view)
        self.assertNotIn("### Required", rendered)
        self.assertNotIn("### Optional", rendered)
        self.assertNotIn("### Limitations", rendered)
        self.assertIn("### Next action", rendered)

    def test_limitations_render_separately_from_code_defects(self):
        view = build_review_summary_view(
            state=CHANGES_REQUIRED_STATE,
            head_sha="f" * 40,
            required=(self._required(),),
            limitations=("Coverage enumeration was incomplete for 2 files.",),
        )
        rendered = render_review_summary(view)
        self.assertIn(
            "### Limitations\n- Coverage enumeration was incomplete for 2 files.",
            rendered,
        )
        self.assertLess(
            rendered.index("### Required"), rendered.index("### Limitations")
        )

    def test_placement_note_is_stated_once(self):
        view = build_review_summary_view(
            state=NO_REQUIRED_FIXES_STATE,
            head_sha="0" * 40,
            optional=(self._optional(),),
            placement_note="Optional feedback is explained here because this branch requires conversation resolution.",
        )
        rendered = render_review_summary(view)
        self.assertEqual(rendered.count("conversation resolution"), 1)

    def test_rejects_more_than_three_highlighted_optional_items(self):
        with self.assertRaises(Exception):
            ReviewSummaryView(
                state=CHANGES_REQUIRED_STATE,
                head_sha="1" * 40,
                optional=tuple(self._optional(f"RS-{index:03d}") for index in range(4)),
            )


if __name__ == "__main__":
    unittest.main()
