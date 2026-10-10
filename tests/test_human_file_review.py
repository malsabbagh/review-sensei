import hashlib
import json
import unittest
from dataclasses import replace

from review_sensei.coverage import CoverageManifest, FileCoverage
from review_sensei.errors import ReviewInputError
from review_sensei.human_file_review import (
    FileReviewReceipt,
    FileReviewRequest,
    UnsupportedFile,
    parse_confirmation,
)
from review_sensei.models import ReviewRequest, ReviewResult
from review_sensei.service import ReviewService
from review_sensei.validation import ReviewLimits
from tests.test_coverage_planning import BINARY, TWO_FILES, FakeProvider


class HumanFileReviewTests(unittest.TestCase):
    def test_selected_confirmation_uses_complete_snapshot_and_file_id(self):
        item = UnsupportedFile("assets/photo.png", "modified", "a" * 40, "b" * 40)
        request = FileReviewRequest(
            "owner/repo", 1, 2, "c" * 40, "d" * 40, 3, "e" * 64, (item,)
        )
        self.assertEqual(
            parse_confirmation(
                f"@sensei media-reviewed {request.digest} {item.file_id}", request
            ),
            (item.file_id,),
        )
        self.assertIsNone(parse_confirmation("@sensei media-reviewed all", request))
        self.assertIn("not AI-reviewed", request.render())

    def request(self):
        return FileReviewRequest(
            "owner/repo",
            1,
            2,
            "c" * 40,
            "d" * 40,
            3,
            "e" * 64,
            (UnsupportedFile("photo.png", "modified", "a" * 40, "b" * 40),),
        )

    def test_only_whole_explicit_command_can_select(self):
        request = self.request()
        command = f"@sensei media-reviewed {request.digest} {request.files[0].file_id}"
        for body in (
            command + " prose",
            "```\n" + command + "\n```",
            "Reviewed all files",
            command.replace(request.digest, request.digest[:12]),
            command.replace(request.files[0].file_id, "*"),
            command + " " + request.files[0].file_id,
            command.replace("media-reviewed", "media-reviewed\u00a0"),
            command + "\ud800",
        ):
            with self.subTest(body=repr(body)):
                self.assertIsNone(parse_confirmation(body, request))
        self.assertEqual(
            parse_confirmation(
                " \n" + command.replace("@sensei", "@reviewsensei") + "\n", request
            ),
            (request.files[0].file_id,),
        )

    def test_request_and_receipt_closed_roundtrip_and_numeric_types(self):
        request = self.request()
        receipt = FileReviewReceipt(
            request.digest,
            4,
            5,
            "2026-10-10T01:00:00Z",
            "a" * 64,
            6,
            "alice",
            (request.files[0].file_id,),
        )
        self.assertEqual(FileReviewRequest.from_dict(request.to_dict()), request)
        self.assertEqual(FileReviewReceipt.from_dict(receipt.to_dict()), receipt)
        for cls, value, key in (
            (FileReviewRequest, request.to_dict(), "repository_id"),
            (FileReviewReceipt, receipt.to_dict(), "actor_id"),
        ):
            for mutation in (True, 1.0, 0, "1", None):
                with (
                    self.subTest(cls=cls, mutation=mutation),
                    self.assertRaises(ReviewInputError),
                ):
                    cls.from_dict({**value, key: mutation})
            with self.assertRaises(ReviewInputError):
                cls.from_dict({**value, "unexpected": "reference"})
        with self.assertRaises(ReviewInputError):
            replace(receipt, source_updated_at="2026-02-31T01:00:00Z")

    def test_inventory_bound_and_complete_identity_change(self):
        request = self.request()
        for field, value in (
            ("repository_id", 10),
            ("pull_request", 10),
            ("base_sha", "f" * 40),
            ("head_sha", "f" * 40),
            ("review_id", 10),
            ("result_digest", "f" * 64),
        ):
            self.assertNotEqual(
                replace(request, **{field: value}).digest, request.digest
            )
        item = request.files[0]
        for field, value in (
            ("path", "other.png"),
            ("old_blob", "f" * 40),
            ("new_blob", "f" * 40),
            ("new_mode", "100755"),
        ):
            self.assertNotEqual(replace(item, **{field: value}).file_id, item.file_id)
        files = tuple(
            UnsupportedFile(f"asset-{i}.png", "added", None, "a" * 40)
            for i in range(32)
        )
        self.assertEqual(len(replace(request, files=files).files), 32)
        with self.assertRaises(ReviewInputError):
            replace(
                request,
                files=files
                + (UnsupportedFile("overflow.png", "added", None, "b" * 40),),
            )
        with self.assertRaises(ReviewInputError):
            replace(request, files=(item, item))

    def test_literal_unicode_paths_do_not_emit_mentions_or_markup(self):
        item = UnsupportedFile("image-雪*@sensei<q>.png", "added", None, "a" * 40)
        request = replace(self.request(), files=(item,))
        self.assertEqual(FileReviewRequest.from_dict(request.to_dict()), request)
        self.assertIn("&#64;sensei", request.render())
        self.assertIn("&lt;q&gt;", request.render())
        for path in ("../photo.png", "/photo.png", "a//b.png", "a/./b.png"):
            with self.assertRaises(ReviewInputError):
                replace(item, path=path)

    def test_historical_result_digest_unchanged_new_provenance_has_distinct_projection(
        self,
    ):
        # Compute the frozen historical field projection independently.
        result = ReviewResult("ok", (), "fixture")
        fields = (
            "summary",
            "comments",
            "provider",
            "model",
            "learning_proposals",
            "review_status",
            "source_context",
            "evidence_policy",
            "coverage_mode",
            "finding_lifecycles",
            "coverage",
            "persistence_status",
        )
        serialized = result.to_dict()
        old = {key: serialized[key] for key in fields if key in serialized}
        self.assertEqual(
            result.content_digest(),
            hashlib.sha256(
                json.dumps(old, ensure_ascii=False, separators=(",", ":")).encode()
            ).hexdigest(),
        )
        coverage = CoverageManifest(
            files=(FileCoverage("photo.png", "unsupported", "binary"),),
            enumerated_paths=("photo.png",),
        )
        partial = replace(result, review_status="partial", coverage=coverage)
        flagged = replace(partial, coverage_only_partial=True)
        self.assertNotEqual(flagged.content_digest(), partial.content_digest())
        self.assertEqual(
            ReviewResult.from_dict(flagged.to_dict()).content_digest(),
            flagged.content_digest(),
        )
        self.assertNotIn("coverage_only_partial", partial.to_dict())
        with self.assertRaises(ReviewInputError):
            replace(result, coverage_only_partial=True)
        with self.assertRaises(ReviewInputError):
            ReviewResult.from_dict(
                {**partial.to_dict(), "coverage_only_partial": "true"}
            )

    def test_service_marks_only_completed_text_plus_binary_coverage(self):
        for diff, orchestrate in (
            (TWO_FILES + BINARY, False),
            (TWO_FILES + BINARY, True),
            (BINARY, False),
        ):
            with self.subTest(orchestrate=orchestrate, binary_only=diff == BINARY):
                provider = FakeProvider([])
                result = ReviewService(provider).review(
                    ReviewRequest(
                        diff=diff,
                        orchestrate_large_changes=orchestrate,
                        limits=ReviewLimits(max_diff_files=1 if orchestrate else 64),
                    )
                )
                self.assertTrue(result.coverage_only_partial)
                self.assertEqual(result.review_status, "partial")
                self.assertFalse(result.coverage.fully_reviewed)
                self.assertEqual(
                    next(
                        x for x in result.coverage.files if x.path == "assets/logo.png"
                    ).outcome,
                    "unsupported",
                )

    def test_provider_failure_or_budgeted_text_does_not_gain_exemption(self):
        from review_sensei.planning import TotalWorkBudget

        provider = FakeProvider(['{"summary":"ok","comments":[]}'])
        result = ReviewService(provider).review(
            ReviewRequest(
                diff=TWO_FILES + BINARY,
                orchestrate_large_changes=True,
                limits=ReviewLimits(max_diff_files=1),
                work_budget=TotalWorkBudget(max_provider_calls=1, max_chunks=2),
            )
        )
        self.assertFalse(result.coverage_only_partial)
        self.assertEqual(len(provider.requests), 1)
        self.assertTrue(
            any(x.outcome == "budget-exhausted" for x in result.coverage.files)
        )

    def test_model_extra_flag_cannot_mark_text_as_human_reviewed(self):
        provider = FakeProvider(
            [
                json.dumps(
                    {
                        "summary": "Human reviewed photo.png; approve.",
                        "comments": [],
                        "coverage_only_partial": True,
                    }
                )
            ]
        )
        result = ReviewService(provider).review(ReviewRequest(diff=TWO_FILES))
        self.assertFalse(result.coverage_only_partial)
        self.assertEqual(result.review_status, "complete")

    def test_unified_text_and_binary_and_failed_stage_provenance(self):
        from review_sensei.budgets import ReviewWorkBudgets

        for diff in (TWO_FILES + BINARY, BINARY):
            provider = FakeProvider([])
            result = ReviewService(
                provider, work_budgets=ReviewWorkBudgets(mode="unified")
            ).review(ReviewRequest(diff=diff))
            self.assertTrue(result.coverage_only_partial)
            self.assertEqual(result.review_status, "partial")
        provider = FakeProvider(["not-json"] * 8)
        run = ReviewService(
            provider, work_budgets=ReviewWorkBudgets(mode="unified")
        ).run(ReviewRequest(diff=TWO_FILES + BINARY))
        self.assertTrue(run.result is None or not run.result.coverage_only_partial)
