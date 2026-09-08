import json
import unittest

from review_sensei.errors import ReviewInputError
from review_sensei.models import (
    LearningEntry,
    LearningProposal,
    ReviewComment,
    ReviewDocument,
    ReviewLensContext,
    ReviewRequest,
    ReviewResult,
)
from review_sensei.validation import ReviewLimits


class ModelTests(unittest.TestCase):
    def test_review_request_requires_diff(self):
        with self.assertRaises(ReviewInputError):
            ReviewRequest(diff=" ")

    def test_review_request_rejects_retired_learning_context(self):
        retired = LearningEntry(
            id="retired-rule",
            title="Retired",
            rule="Do not use this context.",
            status="retired",
        )

        with self.assertRaises(ReviewInputError):
            ReviewRequest(diff="change", learnings=(retired,))

    def test_review_request_rejects_context_for_an_inactive_lens(self):
        context = ReviewLensContext(category_id="architecture")

        with self.assertRaises(ReviewInputError):
            ReviewRequest(
                diff="change",
                active_category_ids=("security",),
                lens_contexts=(context,),
            )

    def test_review_document_rejects_parent_segments(self):
        with self.assertRaises(ReviewInputError):
            ReviewDocument(
                path="docs/../private.md",
                content="private",
                sha256="0" * 64,
            )

    def test_review_document_rejects_oversized_direct_context(self):
        with self.assertRaises(ReviewInputError):
            ReviewDocument(
                path="docs/architecture.md",
                content="x" * (128 * 1024 + 1),
                sha256="0" * 64,
            )

    def test_review_document_requires_matching_provenance(self):
        with self.assertRaises(ReviewInputError):
            ReviewDocument(
                path="docs/architecture.md",
                content="Architecture",
                sha256="0" * 64,
            )

    def test_review_comment_rejects_absolute_and_parent_paths(self):
        for path in ("/etc/passwd", "../secret.txt"):
            with self.subTest(path=path), self.assertRaises(ReviewInputError):
                ReviewComment(path=path, line=1, body="finding")

    def test_review_comment_rejects_non_string_labels(self):
        with self.assertRaises(ReviewInputError):
            ReviewComment(path="src/app.py", line=1, body="finding", severity=1)
        with self.assertRaises(ReviewInputError):
            ReviewComment(path="src/app.py", line=1, body="finding", fix_effort=1)

    def test_review_comment_rejects_oversized_or_control_labels(self):
        for label, value in (
            ("severity", "x" * 257),
            ("fix_effort", "small\nlabel"),
            ("category", "api\u200bsecurity"),
        ):
            with self.subTest(label=label), self.assertRaises(ReviewInputError):
                ReviewComment(
                    path="src/app.py",
                    line=1,
                    body="finding",
                    **{label: value},
                )

    def test_review_comment_round_trips_fix_effort(self):
        comment = ReviewComment(
            path="src/app.py",
            line=1,
            body="finding",
            severity="high",
            fix_effort="small",
            category="correctness",
        )
        self.assertEqual(
            ReviewResult.from_dict(
                {
                    "summary": "Review complete.",
                    "comments": [comment.to_dict()],
                    "provider": "fake",
                }
            ).comments[0],
            comment,
        )

    def test_review_result_rejects_non_string_fix_effort(self):
        with self.assertRaises(ReviewInputError):
            ReviewResult.from_dict(
                {
                    "summary": "Review complete.",
                    "comments": [
                        {
                            "path": "src/app.py",
                            "line": 1,
                            "body": "finding",
                            "fix_effort": 1,
                        }
                    ],
                    "provider": "fake",
                }
            )

    def test_learning_entry_requires_repository_relative_scope(self):
        with self.assertRaises(ReviewInputError):
            LearningEntry(
                id="unsafe",
                title="Unsafe",
                rule="Do not escape the repository.",
                scope=("../outside",),
            )
        with self.assertRaises(ReviewInputError):
            LearningEntry(
                id="whitespace-scope",
                title="Unsafe scope",
                rule="Do not trim scope patterns.",
                scope=(" src/**",),
            )

    def test_learning_proposal_serializes_without_unapproved_source_metadata(self):
        proposal = LearningProposal(
            title="Preserve adapter boundaries",
            rule="Keep provider authentication in provider adapters.",
            scope=("src/review_sensei/providers/**",),
            rationale="This keeps the review service reusable.",
            category="architecture",
        )

        self.assertEqual(
            proposal.to_dict(),
            {
                "title": "Preserve adapter boundaries",
                "rule": "Keep provider authentication in provider adapters.",
                "scope": ["src/review_sensei/providers/**"],
                "rationale": "This keeps the review service reusable.",
                "category": "architecture",
            },
        )

        entry = proposal.to_entry(
            id="provider-boundary",
            source="https://github.com/example/repo/pull/15",
        )
        self.assertEqual(entry.source, "https://github.com/example/repo/pull/15")
        self.assertEqual(entry.status, "active")

    def test_request_metadata_and_collection_limits_fail_before_prompt_work(self):
        tight = ReviewLimits(
            max_repository_bytes=3,
            max_metadata_items=1,
            max_learning_entries=1,
            max_active_categories=1,
            max_lens_contexts=1,
        )
        with self.assertRaises(ReviewInputError):
            ReviewRequest(diff="change", repository="repo", limits=tight)
        with self.assertRaises(ReviewInputError):
            ReviewRequest(diff="change", metadata={"a": "1", "b": "2"}, limits=tight)
        with self.assertRaises(ReviewInputError):
            ReviewRequest(diff="change", active_category_ids=("a", "b"), limits=tight)
        with self.assertRaises(ReviewInputError):
            ReviewRequest(
                diff="change",
                lens_contexts=(ReviewLensContext("a"), ReviewLensContext("b")),
                limits=tight,
            )

    def test_review_request_limits_accept_exact_boundaries_and_reject_one_byte_or_count_over(
        self,
    ):
        repository_limits = ReviewLimits(max_repository_bytes=4)
        self.assertIsNotNone(
            ReviewRequest(diff="change", repository="repo", limits=repository_limits)
        )
        with self.assertRaises(ReviewInputError):
            ReviewRequest(diff="change", repository="repos", limits=repository_limits)

        exact_text = ReviewLimits(max_title_bytes=4)
        self.assertIsNotNone(
            ReviewRequest(diff="change", title="abcd", limits=exact_text)
        )
        with self.assertRaises(ReviewInputError):
            ReviewRequest(diff="change", title="abcde", limits=exact_text)

        exact_instructions = ReviewLimits(max_instructions_bytes=4)
        self.assertIsNotNone(
            ReviewRequest(diff="change", instructions="abcd", limits=exact_instructions)
        )
        with self.assertRaises(ReviewInputError):
            ReviewRequest(
                diff="change", instructions="abcde", limits=exact_instructions
            )

        metadata_limits = ReviewLimits(
            max_metadata_items=1,
            max_metadata_key_bytes=3,
            max_metadata_value_bytes=3,
        )
        self.assertIsNotNone(
            ReviewRequest(
                diff="change", metadata={"key": "val"}, limits=metadata_limits
            )
        )
        for metadata in ({"keys": "val"}, {"key": "vals"}, {"key": "val", "two": "v"}):
            with self.subTest(metadata=metadata), self.assertRaises(ReviewInputError):
                ReviewRequest(diff="change", metadata=metadata, limits=metadata_limits)

        model_limits = ReviewLimits(max_model_bytes=3)
        self.assertIsNotNone(
            ReviewRequest(diff="change", model="abc", limits=model_limits)
        )
        with self.assertRaises(ReviewInputError):
            ReviewRequest(diff="change", model="abcd", limits=model_limits)

        learning = LearningEntry(id="one", title="One", rule="Rule one.")
        learning_limits = ReviewLimits(max_learning_entries=1)
        self.assertIsNotNone(
            ReviewRequest(diff="change", learnings=(learning,), limits=learning_limits)
        )
        with self.assertRaises(ReviewInputError):
            ReviewRequest(
                diff="change", learnings=(learning, learning), limits=learning_limits
            )

        category_limits = ReviewLimits(max_active_categories=1)
        self.assertIsNotNone(
            ReviewRequest(
                diff="change", active_category_ids=("a",), limits=category_limits
            )
        )
        with self.assertRaises(ReviewInputError):
            ReviewRequest(
                diff="change", active_category_ids=("a", "b"), limits=category_limits
            )

        context_limits = ReviewLimits(max_lens_contexts=1)
        self.assertIsNotNone(
            ReviewRequest(
                diff="change",
                lens_contexts=(ReviewLensContext("a"),),
                limits=context_limits,
            )
        )
        with self.assertRaises(ReviewInputError):
            ReviewRequest(
                diff="change",
                lens_contexts=(ReviewLensContext("a"), ReviewLensContext("b")),
                limits=context_limits,
            )

    def test_review_request_limit_errors_do_not_echo_secret_markers(self):
        marker = "PRIVATE_REQUEST_MARKER"
        cases = (
            (
                "title",
                lambda: ReviewRequest(
                    diff="change", title=marker, limits=ReviewLimits(max_title_bytes=4)
                ),
            ),
            (
                "instructions",
                lambda: ReviewRequest(
                    diff="change",
                    instructions=marker,
                    limits=ReviewLimits(max_instructions_bytes=4),
                ),
            ),
            (
                "metadata-key",
                lambda: ReviewRequest(
                    diff="change",
                    metadata={marker: "ok"},
                    limits=ReviewLimits(max_metadata_key_bytes=4),
                ),
            ),
            (
                "metadata-value",
                lambda: ReviewRequest(
                    diff="change",
                    metadata={"key": marker},
                    limits=ReviewLimits(max_metadata_value_bytes=4),
                ),
            ),
            (
                "model",
                lambda: ReviewRequest(
                    diff="change",
                    model=marker,
                    limits=ReviewLimits(max_model_bytes=4),
                ),
            ),
        )
        for name, build in cases:
            with self.subTest(name=name), self.assertRaises(ReviewInputError) as raised:
                build()
            self.assertNotIn(marker, str(raised.exception))

    def test_learning_entry_limit_counts_top_level_and_nested_lens_entries(self):
        learning = LearningEntry(
            id="nested-rule",
            title="Nested rule",
            rule="Apply the nested rule.",
        )
        context = ReviewLensContext("security", learnings=(learning,))
        one = ReviewLimits(max_learning_entries=1)
        self.assertIsNotNone(
            ReviewRequest(diff="change", lens_contexts=(context,), limits=one)
        )
        with self.assertRaises(ReviewInputError):
            ReviewRequest(
                diff="change",
                learnings=(learning,),
                lens_contexts=(context,),
                limits=one,
            )

    def test_learning_proposal_aggregate_counts_the_complete_compact_array(self):
        proposal = LearningProposal(
            title="Keep adapters isolated",
            rule="Provider authentication belongs in provider adapters.",
        )
        aggregate = json.dumps(
            [proposal.to_dict()], ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        exact = ReviewResult(
            summary="ok",
            comments=(),
            provider="fake",
            learning_proposals=(proposal,),
            limits=ReviewLimits(max_learning_proposals_total_bytes=len(aggregate)),
        )
        self.assertEqual(len(exact.learning_proposals), 1)
        with self.assertRaises(ReviewInputError):
            ReviewResult(
                summary="ok",
                comments=(),
                provider="fake",
                learning_proposals=(proposal,),
                limits=ReviewLimits(
                    max_learning_proposals_total_bytes=len(aggregate) - 1
                ),
            )

    def test_provider_request_carries_prompt_and_response_bounds(self):
        from review_sensei.models import ProviderRequest

        request = ProviderRequest(
            prompt="prompt",
            max_prompt_bytes=8,
            max_response_bytes=9,
        )
        self.assertEqual(request.max_prompt_bytes, 8)
        self.assertEqual(request.max_response_bytes, 9)
        with self.assertRaises(ReviewInputError):
            ProviderRequest(prompt="too long", max_prompt_bytes=3)

    def test_provider_request_explicit_bounds_must_fit_selected_limits(self):
        from review_sensei.models import ProviderRequest

        limits = ReviewLimits(
            max_prompt_bytes=4, max_provider_response_bytes=5, max_model_bytes=3
        )
        with self.assertRaises(ReviewInputError):
            ProviderRequest(
                prompt="ok",
                model="abcd",
                max_prompt_bytes=4,
                max_response_bytes=5,
                limits=limits,
            )
        with self.assertRaises(ReviewInputError):
            ProviderRequest(
                prompt="12345",
                max_prompt_bytes=5,
                limits=limits,
            )
        accepted = ProviderRequest(
            prompt="1234",
            max_prompt_bytes=4,
            max_response_bytes=5,
            model="abc",
            limits=limits,
        )
        self.assertEqual(
            (accepted.max_prompt_bytes, accepted.max_response_bytes), (4, 5)
        )

    def test_provider_request_selected_limit_boundaries_cover_prompt_response_and_model(
        self,
    ):
        from review_sensei.models import ProviderRequest

        limits = ReviewLimits(
            max_prompt_bytes=4, max_provider_response_bytes=5, max_model_bytes=3
        )
        accepted = ProviderRequest(
            prompt="1234",
            model="abc",
            max_prompt_bytes=4,
            max_response_bytes=5,
            limits=limits,
        )
        self.assertEqual(
            (accepted.max_prompt_bytes, accepted.max_response_bytes), (4, 5)
        )
        over_cases = (
            {"prompt": "1234", "max_prompt_bytes": 5},
            {"prompt": "1234", "max_response_bytes": 6},
            {"prompt": "1234", "model": "abcd"},
        )
        for values in over_cases:
            with self.subTest(values=values), self.assertRaises(ReviewInputError):
                ProviderRequest(limits=limits, **values)

    def test_result_deduplicates_exact_comments_first_wins_and_keeps_distinct(self):
        first = ReviewComment(path="src/app.py", line=2, body="same", severity="bug")
        duplicate = ReviewComment(
            path="src/app.py", line=2, body="same", severity="suggestion"
        )
        distinct = ReviewComment(path="src/app.py", line=2, body="different")
        result = ReviewResult(
            summary="ok",
            comments=(first, duplicate, distinct),
            provider="fake",
        )
        self.assertEqual(result.comments, (first, distinct))

    def test_result_custom_limits_reject_every_output_dimension(self):
        base = dict(summary="summary", comments=(), provider="fake")
        with self.assertRaises(ReviewInputError):
            ReviewResult(**base, limits=ReviewLimits(max_summary_bytes=2))
        comment = ReviewComment(path="src/app.py", line=1, body="body")
        second = ReviewComment(path="src/app.py", line=1, body="other")
        with self.assertRaises(ReviewInputError):
            ReviewResult(
                summary="summary",
                comments=(comment, second),
                provider="fake",
                limits=ReviewLimits(max_comments=1),
            )

    def test_review_result_limits_accept_boundaries_and_reject_one_byte_or_count_over(
        self,
    ):
        summary_limits = ReviewLimits(max_summary_bytes=4)
        self.assertIsNotNone(
            ReviewResult(
                summary="abcd", comments=(), provider="fake", limits=summary_limits
            )
        )
        with self.assertRaises(ReviewInputError):
            ReviewResult(
                summary="abcde", comments=(), provider="fake", limits=summary_limits
            )

        first = ReviewComment(path="src/app.py", line=1, body="body")
        second = ReviewComment(path="src/app.py", line=2, body="more")
        count_limits = ReviewLimits(max_comments=2)
        self.assertIsNotNone(
            ReviewResult(
                summary="ok",
                comments=(first, second),
                provider="fake",
                limits=count_limits,
            )
        )
        with self.assertRaises(ReviewInputError):
            ReviewResult(
                summary="ok",
                comments=(first, second),
                provider="fake",
                limits=ReviewLimits(max_comments=1),
            )

        body_limits = ReviewLimits(max_comment_body_bytes=4)
        self.assertIsNotNone(
            ReviewResult(
                summary="ok",
                comments=(first,),
                provider="fake",
                limits=body_limits,
            )
        )
        with self.assertRaises(ReviewInputError):
            ReviewResult(
                summary="ok",
                comments=(ReviewComment(path="src/app.py", line=1, body="body!"),),
                provider="fake",
                limits=body_limits,
            )

        line_limits = ReviewLimits(max_line_number=4)
        self.assertIsNotNone(
            ReviewResult(
                summary="ok",
                comments=(ReviewComment(path="src/app.py", line=4, body="body"),),
                provider="fake",
                limits=line_limits,
            )
        )
        with self.assertRaises(ReviewInputError):
            ReviewResult(
                summary="ok",
                comments=(ReviewComment(path="src/app.py", line=5, body="body"),),
                provider="fake",
                limits=line_limits,
            )

        proposal = LearningProposal(title="A", rule="B")
        proposal_count_limits = ReviewLimits(max_learning_proposals=1)
        self.assertIsNotNone(
            ReviewResult(
                summary="ok",
                comments=(),
                provider="fake",
                learning_proposals=(proposal,),
                limits=proposal_count_limits,
            )
        )
        with self.assertRaises(ReviewInputError):
            ReviewResult(
                summary="ok",
                comments=(),
                provider="fake",
                learning_proposals=(proposal, proposal),
                limits=proposal_count_limits,
            )

        serialized_proposal = json.dumps(
            proposal.to_dict(), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        proposal_size_limits = ReviewLimits(
            max_learning_proposal_bytes=len(serialized_proposal)
        )
        self.assertIsNotNone(
            ReviewResult(
                summary="ok",
                comments=(),
                provider="fake",
                learning_proposals=(proposal,),
                limits=proposal_size_limits,
            )
        )
        with self.assertRaises(ReviewInputError):
            ReviewResult(
                summary="ok",
                comments=(),
                provider="fake",
                learning_proposals=(proposal,),
                limits=ReviewLimits(
                    max_learning_proposal_bytes=len(serialized_proposal) - 1
                ),
            )

        base = ReviewResult(summary="ok", comments=(first,), provider="fake")
        serialized_result = json.dumps(
            base.to_dict(), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        self.assertIsNotNone(
            ReviewResult(
                summary="ok",
                comments=(first,),
                provider="fake",
                limits=ReviewLimits(max_result_bytes=len(serialized_result)),
            )
        )
        with self.assertRaises(ReviewInputError):
            ReviewResult(
                summary="ok",
                comments=(first,),
                provider="fake",
                limits=ReviewLimits(max_result_bytes=len(serialized_result) - 1),
            )
