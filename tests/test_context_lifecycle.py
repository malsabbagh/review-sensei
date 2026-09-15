import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from review_sensei.context import (
    ContextSnapshot,
    FindingLifecycle,
    ReviewContextCache,
    ReviewContextCacheKey,
    SourceContextExcerpt,
    SymbolAwareContextSelector,
    reconcile_finding_lifecycle,
    stable_finding_fingerprint,
)
from review_sensei.errors import ContextLoadError
from review_sensei.learnings import LearningStore
from review_sensei.models import LearningEntry


class ContextLifecycleTests(unittest.TestCase):
    def test_symbol_selection_is_bounded_and_provenanced(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "main.py").write_text("import helper\ndef run():\n    return 1\n")
            (root / "helper.py").write_text("def help():\n    return 2\n")
            result = SymbolAwareContextSelector(
                root, snapshot=ContextSnapshot("a" * 40), max_files=2
            ).select(("main.py",))
            self.assertEqual(
                [item.path for item in result.excerpts], ["main.py", "helper.py"]
            )
            self.assertTrue(
                all(item.snapshot.revision == "a" * 40 for item in result.excerpts)
            )
            self.assertEqual(result.outcomes[-1], ("main.py", "reviewed"))

    def test_symbol_selection_resolves_dotted_and_relative_imports(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "src" / "pkg"
            (package / "sub").mkdir(parents=True)
            (package / "__init__.py").write_text("from .models import Model\n")
            (package / "models.py").write_text("class Model: pass\n")
            (package / "helper.py").write_text("def help(): return 1\n")
            (package / "sub" / "__init__.py").write_text("from .leaf import VALUE\n")
            (package / "sub" / "leaf.py").write_text("VALUE = 1\n")
            (package / "main.py").write_text(
                "import pkg.models\nfrom . import helper\nfrom .sub import leaf\n"
            )

            result = SymbolAwareContextSelector(
                root, snapshot=ContextSnapshot("a" * 40), max_depth=1
            ).select(("src/pkg/main.py",))

        paths = {item.path for item in result.excerpts}
        self.assertTrue(
            {
                "src/pkg/main.py",
                "src/pkg/models.py",
                "src/pkg/helper.py",
                "src/pkg/sub/__init__.py",
                "src/pkg/sub/leaf.py",
            }.issubset(paths)
        )
        self.assertTrue(result.complete)

    def test_symbol_selection_marks_depth_limited_relationships_incomplete(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "main.py").write_text("import helper\n")
            (root / "helper.py").write_text("import leaf\n")
            (root / "leaf.py").write_text("VALUE = 1\n")

            depth_zero = SymbolAwareContextSelector(root, max_depth=0).select(
                ("main.py",)
            )
            depth_one = SymbolAwareContextSelector(root, max_depth=1).select(
                ("main.py",)
            )

        self.assertFalse(depth_zero.complete)
        self.assertEqual(dict(depth_zero.outcomes)["main.py"], "partially-reviewed")
        self.assertFalse(depth_one.complete)
        self.assertEqual(dict(depth_one.outcomes)["helper.py"], "partially-reviewed")
        self.assertEqual(
            [item.path for item in depth_one.excerpts], ["main.py", "helper.py"]
        )

    def test_symbol_selection_reports_excluded_and_unsupported_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            (root / "src" / "main.py").write_text("import helper\n")
            (root / "helper.py").write_text("VALUE = 1\n")
            result = SymbolAwareContextSelector(
                root, allowed_paths=("src/**",), max_depth=1
            ).select(("README.md", "src/main.py"))

        outcomes = dict(result.outcomes)
        self.assertEqual(outcomes["README.md"], "excluded-by-policy")
        self.assertEqual(outcomes["helper.py"], "excluded-by-policy")
        self.assertFalse(result.complete)

    def test_associated_tests_use_bounded_conventional_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "main.py").write_text("VALUE = 1\n")
            (root / "tests").mkdir()
            (root / "tests" / "test_main.py").write_text("def test_main(): pass\n")
            nested = root / "unrelated" / "deep" / "tests"
            nested.mkdir(parents=True)
            (nested / "test_main.py").write_text("def test_main(): pass\n")

            selector = SymbolAwareContextSelector(root)
            # A recursive walk would find the unrelated copy.  The selector
            # deliberately only checks fixed conventional locations.
            result = selector.select(("main.py",))

        self.assertIn("tests/test_main.py", {item.path for item in result.excerpts})
        self.assertNotIn(
            "unrelated/deep/tests/test_main.py", {item.path for item in result.excerpts}
        )

    def test_excerpt_validation_rejects_invalid_inputs_without_double_document(self):
        with self.assertRaises(ContextLoadError):
            SourceContextExcerpt(path="../outside.py", content="x")
        with self.assertRaises(ContextLoadError):
            SourceContextExcerpt(path="source.py", content=object())  # type: ignore[arg-type]
        with self.assertRaises(ContextLoadError):
            SourceContextExcerpt(path="source.py", content="x", start_line=True)
        with self.assertRaises(ContextLoadError):
            SourceContextExcerpt(path="source.py", content="   ")
        with self.assertRaises(ContextLoadError):
            SourceContextExcerpt(path="source.py", content="x", reason="\t")

    def test_context_reader_enforces_bounded_read_and_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            oversized = root / "oversized.py"
            oversized.write_bytes(b"x" * (128 * 1024 + 1))
            selector = SymbolAwareContextSelector(root)
            self.assertFalse(selector.select(("oversized.py",)).complete)
            try:
                (root / "link.py").symlink_to(oversized)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are unavailable on this platform")
            result = selector.select(("link.py",))
            self.assertEqual(dict(result.outcomes)["link.py"], "unsupported")
            self.assertFalse(result.complete)

    def test_context_reader_rejects_parent_directory_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            outside = Path(temporary) / "outside"
            root = Path(temporary) / "root"
            outside.mkdir()
            root.mkdir()
            (outside / "secret.py").write_text("SECRET = 1\n")
            try:
                (root / "link").symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are unavailable on this platform")
            if not hasattr(os, "O_DIRECTORY"):
                self.skipTest("root-relative open requires O_DIRECTORY")
            result = SymbolAwareContextSelector(root).select(("link/secret.py",))
            self.assertEqual(dict(result.outcomes)["link/secret.py"], "unsupported")
            self.assertFalse(result.complete)

    def test_context_input_iterables_are_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def endless_paths():
                index = 0
                while True:
                    yield f"missing-{index}.py"
                    index += 1

            result = SymbolAwareContextSelector(root).select(endless_paths())
            self.assertFalse(result.complete)
            self.assertLessEqual(len(result.outcomes), 512)

            with self.assertRaises(ContextLoadError):
                SymbolAwareContextSelector(root, allowed_paths="src/**")

    def test_context_input_duplicate_iterable_is_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def endless_duplicates():
                while True:
                    yield "missing.py"

            result = SymbolAwareContextSelector(root).select(endless_duplicates())

        self.assertFalse(result.complete)
        self.assertEqual(dict(result.outcomes)["missing.py"], "unsupported")

    def test_import_expansion_caps_dotted_parts_and_aliases(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            long_module = ".".join(f"part{index}" for index in range(40))
            aliases = ", ".join(f"name{index}" for index in range(80))
            (root / "main.py").write_text(
                f"import {long_module}\nfrom package import {aliases}\n"
            )

            result = SymbolAwareContextSelector(root).select(("main.py",))

        self.assertFalse(result.complete)
        self.assertEqual(dict(result.outcomes)["main.py"], "partially-reviewed")

    def test_fingerprint_ignores_line_and_prose(self):
        one = stable_finding_fingerprint(
            path="src/a.py", symbol="run", defect_kind="Race"
        )
        two = stable_finding_fingerprint(
            path="src/a.py", symbol="run", defect_kind="race"
        )
        self.assertEqual(one, two)
        self.assertEqual(
            stable_finding_fingerprint(
                path=PurePosixPath("src/a.py"), symbol="run", defect_kind="Race"
            ),
            one,
        )
        with self.assertRaises(ContextLoadError):
            stable_finding_fingerprint(path=PurePosixPath("../outside.py"), symbol="run")
        with self.assertRaises(ContextLoadError):
            stable_finding_fingerprint(path="src/../outside.py", symbol="run")
        self.assertEqual(
            reconcile_finding_lifecycle(None, one).state,
            "new",
        )
        self.assertEqual(
            reconcile_finding_lifecycle(
                FindingLifecycle(one, "still-present"), one
            ).state,
            "still-present",
        )

    def test_reconcile_never_marks_fixed_when_review_is_incomplete(self):
        previous = stable_finding_fingerprint(path="src/a.py", symbol="run")
        current = stable_finding_fingerprint(path="src/a.py", symbol="run", evidence="x")
        result = reconcile_finding_lifecycle(
            FindingLifecycle(previous, "still-present"),
            current,
            evidence_confirmed=True,
            review_complete=False,
        )
        self.assertEqual(result.state, "uncertain")

    def test_cache_key_accepts_repository_names_up_to_512_bytes(self):
        repository = "o/" + ("r" * 509)
        key = ReviewContextCacheKey(
            repository,
            1,
            "a" * 40,
            "b" * 40,
            "e",
            "m",
            "p",
            "a" * 64,
            "b" * 64,
            "c" * 64,
        )
        self.assertEqual(key.repository, repository)

    def test_cache_put_rejects_unbounded_metadata_iterables(self):
        cache = ReviewContextCache(max_entries=1)
        key = ReviewContextCacheKey(
            "o/r", 1, "a" * 40, "b" * 40, "e", "m", "p", "a" * 64, "b" * 64, "c" * 64
        )

        def endless_metadata():
            while True:
                yield "metadata"

        with self.assertRaises(ContextLoadError):
            cache.put(key, endless_metadata())

    def test_cache_is_keyed_by_snapshot_and_bounded(self):
        cache = ReviewContextCache(max_entries=1)
        digest_a = "a" * 64
        digest_b = "b" * 64
        digest_c = "c" * 64
        first = ReviewContextCacheKey(
            "o/r", 1, "a" * 40, "b" * 40, "e", "m", "p", digest_a, digest_b, digest_c
        )
        second = ReviewContextCacheKey(
            "o/r", 2, "a" * 40, "b" * 40, "e", "m", "p", digest_a, digest_b, digest_c
        )
        cache.put(first, ("first",))
        cache.put(second, ("second",))
        self.assertIsNone(cache.get(first))
        self.assertEqual(cache.get(second), ("second",))

    def test_learning_diagnostics_accept_naive_expiry_timestamps(self):
        store = LearningStore(
            (
                LearningEntry(
                    id="expired",
                    title="Expired",
                    rule="Rule",
                    expires_at="2020-01-01T00:00:00",
                ),
            )
        )
        diagnostics = store.diagnostics(now=datetime(2025, 1, 1, tzinfo=timezone.utc))
        self.assertTrue(
            any(item.code == "stale" and item.detail == "expired" for item in diagnostics)
        )

    def test_learning_diagnostics_report_conflict_and_expiry(self):
        store = LearningStore(
            (
                LearningEntry(
                    id="one",
                    title="One",
                    rule="Use A",
                    reviewed_at="2020-01-01T00:00:00Z",
                ),
                LearningEntry(
                    id="two",
                    title="Two",
                    rule="Use B",
                    reviewed_at="2020-01-01T00:00:00Z",
                ),
            )
        )
        diagnostics = store.diagnostics(now=datetime(2025, 1, 1, tzinfo=timezone.utc))
        self.assertTrue(any(item.code == "stale" for item in diagnostics))
        self.assertTrue(any(item.code == "conflict" for item in diagnostics))

    def test_learning_diagnostics_skip_conflicts_across_categories(self):
        store = LearningStore(
            (
                LearningEntry(
                    id="python-rule",
                    title="Python rule",
                    rule="Use rule A",
                    scope=("src/**",),
                    category="python",
                ),
                LearningEntry(
                    id="docs-rule",
                    title="Docs rule",
                    rule="Use rule B",
                    scope=("src/**",),
                    category="docs",
                ),
            )
        )
        diagnostics = store.diagnostics()
        self.assertFalse(any(item.code == "conflict" for item in diagnostics))

    def test_learning_diagnostics_detect_glob_scope_intersection(self):
        store = LearningStore(
            (
                LearningEntry(
                    id="python-rule",
                    title="Python rule",
                    rule="Use rule A",
                    scope=("src/**/*.py",),
                ),
                LearningEntry(
                    id="foo-rule",
                    title="Foo rule",
                    rule="Use rule B",
                    scope=("src/foo/**",),
                ),
            )
        )
        diagnostics = store.diagnostics()
        self.assertTrue(any(item.code == "conflict" for item in diagnostics))

    def test_learning_diagnostics_detect_supersession_cycles(self):
        store = LearningStore(
            (
                LearningEntry(
                    id="one",
                    title="One",
                    rule="Rule one",
                    supersedes=("two",),
                ),
                LearningEntry(
                    id="two",
                    title="Two",
                    rule="Rule two",
                    supersedes=("one",),
                ),
            )
        )
        diagnostics = store.diagnostics()
        self.assertTrue(any(item.code == "supersession-cycle" for item in diagnostics))


if __name__ == "__main__":
    unittest.main()
