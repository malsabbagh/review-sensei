"""Installed reference documents, honest coverage and default host policy."""

import sys
import unittest
from dataclasses import replace
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))

from packaging_guard import assert_distribution_import, is_disarmed  # noqa: E402

assert_distribution_import()

import review_sensei_github  # noqa: E402

if not is_disarmed() and not Path(
    review_sensei_github.__file__
).resolve().is_relative_to(Path(sys.prefix).resolve()):
    raise ImportError("human file review requires the installed hosted adapter")

from review_sensei_github.human_file_review import (  # noqa: E402
    HumanFileReviewPolicy,
)

from review_sensei.coverage import CoverageManifest, FileCoverage  # noqa: E402
from review_sensei.errors import ReviewInputError  # noqa: E402
from review_sensei.human_file_review import (  # noqa: E402
    FileReviewReceipt,
    FileReviewReceiptGroup,
    FileReviewRequest,
    FileReviewRequestGroup,
    UnsupportedFile,
    build_receipt,
    build_request_group,
    parse_confirmation,
    parse_receipt,
    parse_request,
)
from review_sensei.models import ReviewResult  # noqa: E402
from review_sensei.schemas import validate_public_document  # noqa: E402


class InstalledHumanFileReviewTests(unittest.TestCase):
    def request(self):
        return FileReviewRequest(
            "owner/repo",
            1,
            2,
            "a" * 40,
            "b" * 40,
            3,
            "c" * 64,
            (UnsupportedFile("photo.png", "added", None, "d" * 40, new_mode="100644"),),
        )

    def test_closed_packaged_request_receipt_and_exact_selection(self):
        request = self.request()
        receipt = FileReviewReceipt(
            request.digest,
            4,
            5,
            "2026-10-10T01:00:00Z",
            "e" * 64,
            6,
            "alice",
            (request.files[0].file_id,),
        )
        for document, name in (
            (request.to_dict(), "human-file-review"),
            (receipt.to_dict(), "human-file-receipt"),
        ):
            validate_public_document(document, name)
            with self.assertRaises(ReviewInputError):
                validate_public_document({**document, "unknown": True}, name)
        self.assertEqual(FileReviewRequest.from_dict(request.to_dict()), request)
        self.assertEqual(FileReviewReceipt.from_dict(receipt.to_dict()), receipt)
        self.assertEqual(
            parse_confirmation(
                f"@sensei media-reviewed {request.digest} {request.files[0].file_id}",
                request,
            ),
            receipt.selected_ids,
        )
        self.assertIsNone(parse_confirmation("@sensei media-reviewed all", request))

    def test_default_disabled_no_snapshot_carry_and_distinct_coverage(self):
        self.assertFalse(HumanFileReviewPolicy().allow_confirmations)
        request = self.request()
        self.assertNotEqual(replace(request, head_sha="f" * 40).digest, request.digest)
        coverage = CoverageManifest(
            files=(FileCoverage("photo.png", "unsupported", "binary"),),
            enumerated_paths=("photo.png",),
        )
        result = ReviewResult(
            "Binary needs human review.",
            (),
            "fixture",
            review_status="partial",
            coverage=coverage,
            coverage_only_partial=True,
        )
        self.assertEqual(ReviewResult.from_dict(result.to_dict()), result)
        validate_public_document(result.to_dict(), "review-result")
        self.assertFalse(result.coverage.fully_reviewed)
        self.assertEqual(result.review_status, "partial")
        self.assertIn("not AI-reviewed", request.render())

    def test_installed_group_schemas_and_41_file_roundtrip_preserve_v1_parts(self):
        request = build_request_group(
            "owner/repo",
            1,
            2,
            "a" * 40,
            "b" * 40,
            3,
            "c" * 64,
            tuple(
                UnsupportedFile(
                    f"media-{index:03}.png",
                    "added",
                    None,
                    f"{index + 1:040x}",
                    new_mode="100644",
                )
                for index in range(41)
            ),
        )
        self.assertIsInstance(request, FileReviewRequestGroup)
        self.assertEqual([len(part.files) for part in request.parts], [32, 9])
        selected = tuple(item.file_id for item in request.files)
        receipt = build_receipt(
            request,
            request_comment_id=4,
            source_comment_id=5,
            source_updated_at="2026-10-22T01:00:00Z",
            source_sha256="e" * 64,
            actor_id=6,
            actor="alice",
            selected_ids=selected,
        )
        self.assertIsInstance(receipt, FileReviewReceiptGroup)
        for document, schema in (
            (request.to_dict(), "human-file-review-group"),
            (receipt.to_dict(), "human-file-receipt-group"),
        ):
            validate_public_document(document, schema)
            with self.assertRaises(ReviewInputError):
                validate_public_document({**document, "unknown": True}, schema)
        self.assertEqual(parse_request(request.to_dict()), request)
        self.assertEqual(parse_receipt(receipt.to_dict()), receipt)
        for part in request.parts:
            validate_public_document(part.to_dict(), "human-file-review")
        for part in receipt.parts:
            validate_public_document(part.to_dict(), "human-file-receipt")
        self.assertEqual(
            parse_confirmation(
                "@reviewsensei I reviewed the media files and I approve", request
            ),
            selected,
        )
