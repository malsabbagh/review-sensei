import tempfile
import unittest
from pathlib import Path

from review_sensei.context import (
    ContextSnapshot,
    FindingLifecycle,
    IncrementalReviewPlan,
    ReviewContextCacheKey,
    SourceContextCoverage,
    SourceContextExcerpt,
    SymbolAwareContextPolicy,
    SymbolAwareContextSelector,
    reconcile_finding_set,
    stable_concern_identity,
    stable_finding_fingerprint,
)
from review_sensei.errors import ContextLoadError


class LiteralContextPathTests(unittest.TestCase):
    path = "src/[draft]*?.py"

    def key(self):
        return ReviewContextCacheKey(
            "owner/repo",
            1,
            "a" * 40,
            "b" * 40,
            "fixture",
            "fixture",
            "fixture",
            "c" * 64,
            "d" * 64,
            "e" * 64,
        )

    def test_source_selection_reads_only_the_literal_file_and_preserves_provenance(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            (root / self.path).write_text("VALUE = 1\n")
            (root / "src/draft-other.py").write_text("VALUE = 2\n")
            snapshot = ContextSnapshot("a" * 40)
            result = SymbolAwareContextSelector(
                root, snapshot=snapshot, allowed_paths=("src/**",)
            ).select((self.path,))
            self.assertTrue(result.complete)
            self.assertEqual(result.outcomes, ((self.path, "reviewed"),))
            self.assertEqual(tuple(item.path for item in result.excerpts), (self.path,))
            self.assertEqual(result.excerpts[0].content, "VALUE = 1\n")
            self.assertEqual(result.excerpts[0].snapshot, snapshot)
            coverage = SourceContextCoverage(
                True, True, snapshot, result.outcomes, len(result.excerpts)
            )
            self.assertEqual(coverage.outcomes, result.outcomes)

    def test_finding_and_incremental_scopes_use_exact_path_equality(self):
        fingerprint = stable_finding_fingerprint(path=self.path, defect_kind="fixture")
        concern = stable_concern_identity(path=self.path, defect_kind="fixture")
        sibling = FindingLifecycle("f" * 64, "new", path="src/draft-other.py")
        literal = FindingLifecycle(fingerprint, "new", concern=concern, path=self.path)
        plan = IncrementalReviewPlan(
            self.key(), (literal, sibling), (self.path,), (self.path,)
        )
        reconciled = reconcile_finding_set(
            plan.previous_findings,
            (),
            review_complete=True,
            reviewed_paths=plan.reviewed_paths,
            evidence_confirmed_concerns=(concern,),
        )
        self.assertEqual(
            {item.path: item.state for item in reconciled},
            {self.path: "fixed", sibling.path: "still-present"},
        )
        self.assertNotEqual(fingerprint, stable_finding_fingerprint(path=sibling.path))

    def test_noncanonical_paths_still_fail_closed_at_actual_path_seams(self):
        for path in (
            "../[draft]*.py",
            "src/../[draft]*.py",
            "/src/[draft]*.py",
            "src//[draft]*.py",
            "src/./[draft]*.py",
            "src\\[draft]*.py",
        ):
            with self.subTest(path=path):
                for construct in (
                    lambda: SourceContextExcerpt(path, "VALUE = 1\n"),
                    lambda: SourceContextCoverage(
                        True, True, ContextSnapshot(), ((path, "reviewed"),)
                    ),
                    lambda: stable_finding_fingerprint(path=path),
                    lambda: FindingLifecycle("f" * 64, "new", path=path),
                    lambda: IncrementalReviewPlan(self.key(), reviewed_paths=(path,)),
                    lambda: IncrementalReviewPlan(self.key(), related_paths=(path,)),
                    lambda: reconcile_finding_set(
                        (), (), review_complete=True, reviewed_paths=(path,)
                    ),
                ):
                    with self.assertRaises(ContextLoadError):
                        construct()

    def test_configured_patterns_and_literal_symlink_protections_are_preserved(self):
        with self.assertRaises(ContextLoadError):
            SymbolAwareContextPolicy(allowed_paths=("../src/**",))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            target = root / "target.py"
            target.write_text("VALUE = 1\n")
            try:
                (root / self.path).symlink_to(target)
            except OSError:
                self.skipTest("symlinks are unavailable")
            result = SymbolAwareContextSelector(root).select((self.path,))
            self.assertFalse(result.complete)
            self.assertEqual(result.excerpts, ())
            excluded = SymbolAwareContextSelector(
                root, allowed_paths=("safe/**",)
            ).select((self.path,))
            self.assertEqual(excluded.outcomes, ((self.path, "excluded-by-policy"),))


if __name__ == "__main__":
    unittest.main()
