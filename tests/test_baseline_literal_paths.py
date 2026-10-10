"""Actual Git paths retain literal glob/quoted Unicode identity through storage."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from review_sensei.baseline import (
    baseline_complete_document,
    baseline_from_history_document,
    baseline_history_document,
)
from review_sensei.errors import ReviewInputError
from review_sensei.session import LocalSessionLedger
from review_sensei.validation import validate_repository_path
from tests import test_review_transaction as fixture
from tests.test_authenticated_partitions import binding
from tests.test_evidence_capacity import varied_baseline


class LiteralBaselinePathTests(unittest.TestCase):
    def test_complete_plain_inline_and_partition_paths_preserve_exact_identity(self):
        paths = tuple(
            sorted(
                ("src/[draft]*.py", 'src/"quoted"é🙂?.py', "src/draft.py", "src/*.py")
            )
        )
        baseline = varied_baseline(12)
        baseline = replace(
            baseline,
            findings=tuple(
                replace(item, path=paths[index % len(paths)])
                for index, item in enumerate(baseline.findings)
            ),
            reviewed_paths=paths,
            related_paths=paths,
        )
        plain = baseline_complete_document(baseline)
        inline = baseline_history_document(baseline, require_complete=True)
        self.assertEqual(baseline_from_history_document(plain), baseline)
        self.assertEqual(baseline_from_history_document(inline), baseline)
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory), enable_partition_writes=True)
            context = binding(generation=baseline.generation)
            root = ledger.stage_evidence(
                fixture.IDENTITY,
                binding=context,
                document=plain,
                item_count=len(baseline.findings),
                max_manifest_bytes=8192,
            )
            restored = baseline_from_history_document(
                root,
                reader=lambda manifest: ledger.read_evidence(
                    fixture.IDENTITY, manifest, expected_binding=context
                ),
            )
            self.assertEqual(restored, baseline)
            self.assertEqual(restored.reviewed_paths, paths)
            self.assertEqual(restored.related_paths, paths)
            self.assertNotEqual(restored.findings[0].path, restored.findings[2].path)

    def test_literal_opt_in_keeps_path_security_and_configured_field_defaults(self):
        baseline = varied_baseline(12)
        for path in (
            "../[draft]*.py",
            "src/../[draft]*.py",
            "/src/[draft]*.py",
            "src//[draft]*.py",
            "src\\[draft]*.py",
            "src/e\u0301?.py",
            "src/\u202e?.py",
            "C:/[draft]*.py",
            "src/?.py ",
        ):
            with self.subTest(path=path):
                for change in (
                    {"reviewed_paths": (path,)},
                    {"related_paths": (path,)},
                    {"findings": (replace(baseline.findings[0], path=None),)},
                ):
                    if "findings" in change:
                        with self.assertRaises(ReviewInputError):
                            replace(baseline.findings[0], path=path)
                    else:
                        with self.assertRaises(ReviewInputError):
                            replace(baseline, **change)
        with self.assertRaises(ReviewInputError):
            validate_repository_path("src/[draft]*.py")
        self.assertEqual(
            validate_repository_path("src/[draft]*.py", allow_glob_chars=True),
            "src/[draft]*.py",
        )


if __name__ == "__main__":
    unittest.main()
