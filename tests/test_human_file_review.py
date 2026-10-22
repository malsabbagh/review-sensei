import hashlib
import json
import unittest
from dataclasses import replace

from review_sensei.coverage import CoverageManifest, FileCoverage, HunkCoverage
from review_sensei.errors import ReviewInputError
from review_sensei.human_file_review import (
    MAX_DOCUMENT_BYTES,
    MAX_FILES,
    FileReviewReceipt,
    FileReviewReceiptGroup,
    FileReviewRequest,
    FileReviewRequestGroup,
    UnsupportedFile,
    build_receipt,
    build_request_group,
    canonical,
    coverage_only_binary,
    human_confirmation_reason,
    parse_confirmation,
    parse_receipt,
    parse_request,
    validate_inventory,
)
from review_sensei.models import ReviewRequest, ReviewResult
from review_sensei.schemas import validate_public_document
from review_sensei.service import ReviewService
from review_sensei.validation import ReviewLimits
from tests.test_coverage_planning import BINARY, GENERATED, TWO_FILES, FakeProvider


class HumanFileReviewTests(unittest.TestCase):
    def group(self, count=41, *, path_width=3):
        files = tuple(
            UnsupportedFile(
                f"assets/{index:0{path_width}d}.png",
                "added",
                None,
                "a" * 40,
                new_mode="100644",
            )
            for index in range(count)
        )
        return build_request_group(
            "owner/repo", 1, 2, "c" * 40, "d" * 40, 3, "e" * 64, files
        )

    def receipt(self, request, selected=None):
        return build_receipt(
            request,
            request_comment_id=4,
            source_comment_id=5,
            source_updated_at="2026-10-10T01:00:00Z",
            source_sha256="a" * 64,
            actor_id=6,
            actor="alice",
            selected_ids=selected or tuple(item.file_id for item in request.files),
        )

    def test_group_preserves_v1_serialization_and_existing_caps(self):
        request = self.request()
        same = build_request_group(
            request.repository,
            request.repository_id,
            request.pull_request,
            request.base_sha,
            request.head_sha,
            request.review_id,
            request.result_digest,
            request.files,
        )
        self.assertIsInstance(same, FileReviewRequest)
        self.assertEqual(canonical(same.to_dict()), canonical(request.to_dict()))
        self.assertEqual(same.digest, request.digest)
        receipt = self.receipt(request)
        expected = FileReviewReceipt(
            request.digest,
            4,
            5,
            "2026-10-10T01:00:00Z",
            "a" * 64,
            6,
            "alice",
            tuple(item.file_id for item in request.files),
        )
        self.assertEqual(receipt, expected)
        self.assertEqual(parse_request(request.to_dict()), request)
        self.assertEqual(parse_receipt(receipt.to_dict()), receipt)
        self.assertEqual((MAX_FILES, MAX_DOCUMENT_BYTES), (32, 32768))
        with self.assertRaises(ReviewInputError):
            replace(request, files=self.group().files)

    def test_41_file_group_is_one_exact_complete_inventory(self):
        request = self.group()
        self.assertIsInstance(request, FileReviewRequestGroup)
        self.assertEqual([len(part.files) for part in request.parts], [32, 9])
        self.assertEqual(parse_request(request.to_dict()), request)
        validate_public_document(request.to_dict(), "human-file-review-group")
        coverage = CoverageManifest(
            files=tuple(
                FileCoverage(item.path, "unsupported", "binary")
                for item in request.files
            ),
            enumerated_paths=tuple(item.path for item in request.files),
        )
        validate_inventory(request, coverage)
        with self.assertRaisesRegex(ReviewInputError, "exactly all"):
            validate_inventory(request.parts[0], coverage)
        selected = parse_confirmation(
            "@reviewsensei I reviewed the media files and I approve", request
        )
        self.assertEqual(selected, tuple(item.file_id for item in request.files))
        command = f"@sensei media-reviewed {request.digest} " + " ".join(selected)
        self.assertIsNone(parse_confirmation(command, request))
        self.assertEqual(
            parse_confirmation(" ".join(command.split()[: 3 + MAX_FILES]), request),
            selected[:MAX_FILES],
        )
        self.assertIn("41 file changes", request.render())

    def test_group_canonical_order_common_identity_and_exact_paths_are_required(self):
        request = self.group()
        for field, value in (
            ("repository", "other/repo"),
            ("repository_id", 9),
            ("pull_request", 9),
            ("base_sha", "f" * 40),
            ("head_sha", "f" * 40),
            ("review_id", 9),
            ("result_digest", "f" * 64),
        ):
            with self.subTest(field=field), self.assertRaises(ReviewInputError):
                FileReviewRequestGroup(
                    (request.parts[0], replace(request.parts[1], **{field: value}))
                )
        for parts in (
            tuple(reversed(request.parts)),
            (
                replace(request.parts[0], files=request.parts[0].files[:-1]),
                request.parts[1],
            ),
            (
                request.parts[0],
                replace(request.parts[1], files=(request.parts[0].files[0],)),
            ),
        ):
            with self.subTest(parts=parts), self.assertRaises(ReviewInputError):
                FileReviewRequestGroup(parts)
        value = request.to_dict()
        value["approved"] = True
        with self.assertRaises(ReviewInputError):
            parse_request(value)
        with self.assertRaises(ReviewInputError):
            self.group(path_width=700)
        with self.assertRaises(ReviewInputError):
            FileReviewRequestGroup(request.parts * 17)

    def test_group_receipt_binds_one_source_and_preserves_part_limits(self):
        request = self.group()
        receipt = self.receipt(request)
        self.assertIsInstance(receipt, FileReviewReceiptGroup)
        self.assertEqual(receipt.request_digest, request.digest)
        self.assertEqual(
            (
                receipt.request_comment_id,
                receipt.source_comment_id,
                receipt.actor_id,
                receipt.actor,
            ),
            (4, 5, 6, "alice"),
        )
        self.assertEqual(
            set(receipt.selected_ids), {item.file_id for item in request.files}
        )
        self.assertTrue(
            all(len(part.selected_ids) <= MAX_FILES for part in receipt.parts)
        )
        self.assertEqual(parse_receipt(receipt.to_dict()), receipt)
        validate_public_document(receipt.to_dict(), "human-file-receipt-group")
        selected = (request.files[0].file_id, request.files[-1].file_id)
        subset = self.receipt(request, selected)
        self.assertEqual(set(subset.selected_ids), set(selected))
        for field, value in (
            ("request_comment_id", 9),
            ("source_comment_id", 9),
            ("source_updated_at", "2026-10-11T01:00:00Z"),
            ("source_sha256", "f" * 64),
            ("actor_id", 9),
            ("actor", "bob"),
        ):
            with self.subTest(field=field), self.assertRaises(ReviewInputError):
                FileReviewReceiptGroup(
                    request.digest,
                    (receipt.parts[0], replace(receipt.parts[1], **{field: value})),
                )
        with self.assertRaises(ReviewInputError):
            FileReviewReceiptGroup(request.digest, tuple(reversed(receipt.parts)))
        with self.assertRaises(ReviewInputError):
            FileReviewReceiptGroup(request.digest, (receipt.parts[0], receipt.parts[0]))
        for selected in (("f" * 64,), (request.files[0].file_id,) * 2, ([],)):
            with self.subTest(selected=selected), self.assertRaises(ReviewInputError):
                self.receipt(request, selected)

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
        self.assertEqual(
            parse_confirmation(
                "@reviewsensei I reviewed the media files and I approve", request
            ),
            (item.file_id,),
        )
        self.assertIsNone(
            parse_confirmation(
                "@reviewsensei I reviewed the media files and I approve extra",
                request,
            )
        )
        self.assertIn("I reviewed the media files and I approve", request.render())
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

    def test_generated_policy_exclusion_is_human_confirmable(self):
        path = "vendor/brace-expansion-compat/package.json"
        coverage = CoverageManifest(
            files=(
                FileCoverage("src/a.py", "reviewed"),
                FileCoverage(path, "excluded-by-policy", "generated"),
            ),
            hunks=(
                HunkCoverage(1, "src/a.py", "reviewed"),
                HunkCoverage(2, path, "excluded-by-policy", "generated"),
            ),
            enumerated_paths=("src/a.py", path),
        )
        self.assertTrue(coverage_only_binary(coverage))
        item = UnsupportedFile(path, "modified", "a" * 40, "b" * 40)
        request = FileReviewRequest(
            "owner/repo", 1, 2, "c" * 40, "d" * 40, 3, "e" * 64, (item,)
        )
        validate_inventory(request, coverage)
        reason = human_confirmation_reason(coverage)
        self.assertIn("excluded-by-policy (generated)", reason)
        self.assertIn("package.json", reason)
        self.assertIn("I reviewed the media files and I approve", reason)
        blocked = CoverageManifest(
            files=coverage.files,
            hunks=(
                HunkCoverage(1, "src/a.py", "unsupported", "too-large-hunk"),
                HunkCoverage(2, path, "excluded-by-policy", "generated"),
            ),
            enumerated_paths=coverage.enumerated_paths,
        )
        self.assertFalse(coverage_only_binary(blocked))
        with self.assertRaisesRegex(ReviewInputError, "reviewable text hunk"):
            validate_inventory(request, blocked)

    def test_service_marks_generated_policy_exclusion_as_coverage_only_partial(self):
        result = ReviewService(FakeProvider([])).review(ReviewRequest(diff=GENERATED))
        self.assertTrue(result.coverage_only_partial)
        self.assertEqual(result.review_status, "partial")
        self.assertTrue(result.coverage.hunks)
        self.assertTrue(
            all(
                entry.outcome == "excluded-by-policy" and entry.reason == "generated"
                for entry in (*result.coverage.files, *result.coverage.hunks)
            )
        )
        mixed = ReviewService(FakeProvider([])).review(
            ReviewRequest(diff=TWO_FILES + GENERATED)
        )
        self.assertTrue(mixed.coverage_only_partial)
        self.assertTrue(
            any(
                entry.reason == "generated" and entry.outcome == "excluded-by-policy"
                for entry in mixed.coverage.files
            )
        )

    def test_published_summary_names_generated_files_and_the_existing_command(self):
        from review_sensei_github.publication import (
            _incomplete_reason,
            _review_limitations,
        )

        from review_sensei.presentation import (
            build_review_summary_view,
            render_review_summary,
        )

        path = "vendor/pkg/package.json"
        coverage = CoverageManifest(
            files=(FileCoverage(path, "excluded-by-policy", "generated"),),
            hunks=(HunkCoverage(1, path, "excluded-by-policy", "generated"),),
            enumerated_paths=(path,),
        )
        result = ReviewResult(
            "Generated files need human review.",
            (),
            "fixture",
            review_status="partial",
            coverage=coverage,
            coverage_only_partial=True,
        )
        reason = _incomplete_reason(result)
        rendered = render_review_summary(
            build_review_summary_view(
                state="incomplete",
                head_sha="b" * 40,
                limitations=_review_limitations(result, ()),
                reason=reason,
            )
        )
        self.assertIn("excluded-by-policy (generated)", rendered)
        self.assertIn("I reviewed the media files and I approve", rendered)
        self.assertNotIn("Re-run ReviewSensei once resolved.", rendered)
        self.assertNotIn("reported status partial", rendered)

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
