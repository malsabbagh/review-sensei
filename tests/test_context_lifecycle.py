import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from review_sensei.context import (
    MAX_CALLER_DIRECTORY_ENTRIES,
    UNKNOWN_DEFECT_KIND,
    ContextSnapshot,
    FindingLifecycle,
    IncrementalReviewPlan,
    ReviewContextCache,
    ReviewContextCacheKey,
    SourceContextExcerpt,
    SymbolAwareContextSelector,
    cache_key_is_compatible,
    finding_lifecycle_for_comment,
    reconcile_finding_lifecycle,
    reconcile_finding_set,
    stable_concern_identity,
    stable_finding_fingerprint,
)
from review_sensei.errors import ContextLoadError
from review_sensei.learnings import LearningStore
from review_sensei.models import LearningEntry, ReviewComment


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
        with self.assertRaises(ContextLoadError):
            SourceContextExcerpt(
                path="source.py", content="one\ntwo", start_line=1, end_line=5
            )

    def test_context_snapshot_rejects_untrusted_head_kind(self):
        with self.assertRaises(ContextLoadError):
            ContextSnapshot("a" * 40, kind="head")

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
        # Import-node truncation is reported distinctly from a capped caller
        # directory listing.
        self.assertEqual(dict(result.outcomes)["main.py"], "relations-truncated")

    def test_symbol_selection_is_deterministic_and_records_blob_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "main.py").write_text("import helper\nVALUE = 1\n")
            (root / "helper.py").write_text("VALUE = 2\n")
            snapshot = ContextSnapshot("b" * 40)
            first = SymbolAwareContextSelector(root, snapshot=snapshot).select(
                ("main.py",)
            )
            second = SymbolAwareContextSelector(root, snapshot=snapshot).select(
                ("main.py",)
            )

        self.assertEqual(first, second)
        self.assertTrue(first.excerpts)
        for excerpt in first.excerpts:
            self.assertEqual(excerpt.snapshot.revision, "b" * 40)
            self.assertEqual(excerpt.snapshot.kind, "base")
            self.assertRegex(excerpt.blob_oid, r"^[a-f0-9]{40}$")
            self.assertRegex(excerpt.sha256, r"^[a-f0-9]{64}$")

    def test_enclosing_symbol_and_interface_and_caller_relationships(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "api.py").write_text(
                "from typing import Protocol\n\n"
                "class Greeter(Protocol):\n"
                "    def greet(self) -> str: ...\n"
            )
            (root / "main.py").write_text(
                "from api import Greeter\n\n"
                "class Unused:\n"
                "    value = 1\n\n"
                "def run(greeter: Greeter) -> str:\n"
                "    return greeter.greet()\n"
            )
            (root / "caller.py").write_text("from main import run\n")
            result = SymbolAwareContextSelector(root, max_depth=1).select(
                ("main.py",),
                changed_lines={"main.py": frozenset({7})},
            )

        by_path = {item.path: item for item in result.excerpts}
        self.assertEqual(by_path["main.py"].reason, "enclosing-symbol")
        self.assertIn("def run", by_path["main.py"].content)
        self.assertNotIn("class Unused", by_path["main.py"].content)
        self.assertEqual(by_path["api.py"].reason, "interface")
        self.assertIn("class Greeter", by_path["api.py"].content)
        self.assertEqual(by_path["caller.py"].reason, "direct-caller")

    def test_js_fallback_is_unsupported_language_not_complete(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.js").write_text("export const value = 1;\n")
            result = SymbolAwareContextSelector(root).select(("app.js",))

        self.assertEqual([item.path for item in result.excerpts], ["app.js"])
        self.assertEqual(dict(result.outcomes)["app.js"], "unsupported-language")
        self.assertFalse(result.complete)

    def test_rename_and_deletion_paths_use_base_snapshot_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "old.py").write_text("VALUE = 1\n")
            result = SymbolAwareContextSelector(root).select(
                ("old.py", "new.py", "deleted.py")
            )

        paths = {item.path for item in result.excerpts}
        self.assertEqual(paths, {"old.py"})
        outcomes = dict(result.outcomes)
        self.assertEqual(outcomes["new.py"], "unsupported")
        self.assertEqual(outcomes["deleted.py"], "unsupported")
        self.assertFalse(result.complete)

    def test_malicious_paths_and_secret_files_are_unsupported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "secrets.py").write_text("TOKEN = 'x'\n")
            (root / ".env").write_text("SECRET=1\n")
            result = SymbolAwareContextSelector(root).select(
                ("../outside.py", "secrets.py", ".env", "src/../secrets.py")
            )

        outcomes = dict(result.outcomes)
        self.assertEqual(outcomes["../outside.py"], "unsupported")
        self.assertEqual(outcomes["secrets.py"], "unsupported")
        self.assertEqual(outcomes[".env"], "unsupported")
        self.assertFalse(result.complete)
        self.assertEqual(result.excerpts, ())

    def test_malicious_allowed_path_configuration_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(ContextLoadError):
                SymbolAwareContextSelector(root, allowed_paths=("../secret/**",))

    def test_excessive_graph_fan_out_is_bounded_and_incomplete(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            imports = "\n".join(f"import mod{index:03d}" for index in range(80))
            (root / "main.py").write_text(imports + "\n")
            for index in range(80):
                (root / f"mod{index:03d}.py").write_text("VALUE = 1\n")
            result = SymbolAwareContextSelector(root, max_files=16, max_depth=1).select(
                ("main.py",)
            )

        self.assertLessEqual(len(result.excerpts), 16)
        self.assertFalse(result.complete)
        # Both the relation cap and the caller-directory cap trigger here, and
        # coverage names each one instead of collapsing them into a single
        # undifferentiated status.
        self.assertEqual(
            dict(result.outcomes)["main.py"],
            "directory-truncated,relations-truncated",
        )

    def test_caller_directory_cap_counts_only_python_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "main.py").write_text("VALUE = 1\n")
            (root / "caller.py").write_text("from main import VALUE\n")
            # Far more directory entries than the cap, but only two are Python
            # sources, so the directory was inspected exhaustively.
            for index in range(200):
                (root / f"fixture{index:03d}.json").write_text("{}\n")
            result = SymbolAwareContextSelector(root, max_depth=1).select(("main.py",))

        outcomes = dict(result.outcomes)
        self.assertEqual(outcomes["main.py"], "reviewed")
        self.assertTrue(result.complete)
        self.assertIn("caller.py", {excerpt.path for excerpt in result.excerpts})

    def test_caller_directory_cap_reports_directory_truncation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "main.py").write_text("VALUE = 1\n")
            for index in range(MAX_CALLER_DIRECTORY_ENTRIES + 4):
                (root / f"mod{index:03d}.py").write_text("VALUE = 1\n")
            result = SymbolAwareContextSelector(root, max_depth=1).select(("main.py",))

        self.assertEqual(dict(result.outcomes)["main.py"], "directory-truncated")
        self.assertFalse(result.complete)

    def test_selector_parses_statically_and_does_not_execute_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sentinel = root / "executed.flag"
            (root / "main.py").write_text(
                "open('executed.flag', 'w').write('pwned')\nimport helper\n"
            )
            (root / "helper.py").write_text(
                "open('executed.flag', 'w').write('pwned')\nVALUE = 1\n"
            )
            result = SymbolAwareContextSelector(root).select(("main.py",))

        self.assertFalse(sentinel.exists())
        self.assertEqual(
            {item.path for item in result.excerpts}, {"main.py", "helper.py"}
        )

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
            stable_finding_fingerprint(
                path=PurePosixPath("../outside.py"), symbol="run"
            )
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
        current = stable_finding_fingerprint(
            path="src/a.py", symbol="run", evidence="x"
        )
        concern = stable_concern_identity(path="src/a.py", symbol="run")
        result = reconcile_finding_lifecycle(
            FindingLifecycle(previous, "still-present"),
            current,
            previous_concern=concern,
            current_concern=concern,
            evidence_confirmed=True,
            review_complete=False,
        )
        self.assertEqual(result.state, "uncertain")

    def test_reconcile_marks_fixed_only_for_matching_concern(self):
        previous = stable_finding_fingerprint(path="src/a.py", symbol="run")
        current = stable_finding_fingerprint(
            path="src/a.py", symbol="run", evidence="resolved"
        )
        concern = stable_concern_identity(path="src/a.py", symbol="run")
        result = reconcile_finding_lifecycle(
            FindingLifecycle(previous, "still-present"),
            current,
            previous_concern=concern,
            current_concern=concern,
            evidence_confirmed=True,
            review_complete=True,
        )
        self.assertEqual(result.state, "fixed")

    def test_reconcile_does_not_mark_unrelated_finding_as_fixed(self):
        previous = stable_finding_fingerprint(
            path="src/a.py", symbol="run", defect_kind="race"
        )
        current = stable_finding_fingerprint(
            path="src/b.py", symbol="save", defect_kind="null"
        )
        result = reconcile_finding_lifecycle(
            FindingLifecycle(previous, "still-present"),
            current,
            previous_concern=stable_concern_identity(
                path="src/a.py", symbol="run", defect_kind="race"
            ),
            current_concern=stable_concern_identity(
                path="src/b.py", symbol="save", defect_kind="null"
            ),
            evidence_confirmed=True,
            review_complete=True,
        )
        self.assertEqual(result.state, "outdated")

    def test_reconcile_preserves_terminal_previous_state(self):
        previous = stable_finding_fingerprint(path="src/a.py", symbol="run")
        current = stable_finding_fingerprint(
            path="src/a.py", symbol="run", evidence="resolved"
        )
        concern = stable_concern_identity(path="src/a.py", symbol="run")
        result = reconcile_finding_lifecycle(
            FindingLifecycle(previous, "fixed"),
            current,
            previous_concern=concern,
            current_concern=concern,
            evidence_confirmed=True,
            review_complete=True,
        )
        self.assertEqual(result.state, "fixed")
        self.assertEqual(result.fingerprint, previous)

    def test_omitted_finding_is_uncertain_not_fixed(self):
        previous = finding_lifecycle_for_comment(
            ReviewComment(
                path="src/a.py",
                line=2,
                body="race",
                symbol="run",
                defect_kind="race",
            )
        )
        result = reconcile_finding_set(
            (
                FindingLifecycle(
                    previous.fingerprint,
                    "still-present",
                    concern=previous.concern,
                    path="src/a.py",
                ),
            ),
            (),
            review_complete=True,
            reviewed_paths=("src/a.py",),
        )
        self.assertEqual(result[0].state, "uncertain")

    def test_unreviewed_path_stays_still_present(self):
        previous = finding_lifecycle_for_comment(
            ReviewComment(
                path="src/a.py",
                line=2,
                body="race",
                symbol="run",
                defect_kind="race",
            )
        )
        current = finding_lifecycle_for_comment(
            ReviewComment(
                path="src/b.py",
                line=3,
                body="null",
                symbol="save",
                defect_kind="null",
            )
        )
        result = reconcile_finding_set(
            (
                FindingLifecycle(
                    previous.fingerprint,
                    "still-present",
                    concern=previous.concern,
                    path="src/a.py",
                ),
            ),
            (current,),
            review_complete=True,
            reviewed_paths=("src/b.py",),
        )
        states = {item.fingerprint: item.state for item in result}
        self.assertEqual(states[previous.fingerprint], "still-present")
        self.assertEqual(states[current.fingerprint], "new")

    def test_newer_generation_is_not_regressed(self):
        newer = FindingLifecycle(
            stable_finding_fingerprint(path="src/a.py", symbol="run"),
            "still-present",
            generation=2,
        )
        result = reconcile_finding_set(
            (newer,),
            (),
            review_complete=True,
            generation=1,
        )
        self.assertEqual(result, (newer,))

    def test_newer_generation_records_keep_their_own_generation(self):
        """A stale pass must not rewrite the generation of newer records."""

        newer = FindingLifecycle(
            stable_finding_fingerprint(path="src/a.py", symbol="run"),
            "still-present",
            concern=stable_concern_identity(path="src/a.py", symbol="run"),
            path="src/a.py",
            generation=7,
        )
        current = finding_lifecycle_for_comment(
            ReviewComment(
                path="src/b.py",
                line=3,
                body="null",
                symbol="save",
                defect_kind="null",
            ),
            generation=3,
        )
        result = reconcile_finding_set(
            (newer,),
            (current,),
            review_complete=True,
            reviewed_paths=("src/b.py",),
            generation=3,
        )
        self.assertEqual(result, (newer,))
        self.assertEqual(result[0].generation, 7)

    def test_carried_records_do_not_lose_their_generation(self):
        older = FindingLifecycle(
            stable_finding_fingerprint(path="src/a.py", symbol="run"),
            "still-present",
            concern=stable_concern_identity(path="src/a.py", symbol="run"),
            path="src/a.py",
            generation=2,
        )
        result = reconcile_finding_set(
            (older,),
            (),
            review_complete=True,
            reviewed_paths=("src/b.py",),
            generation=4,
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].state, "still-present")
        self.assertEqual(result[0].generation, 4)

    def test_reconcile_rejects_non_canonical_reviewed_paths(self):
        """Reviewed scope decides retention, so it fails closed."""

        current = finding_lifecycle_for_comment(
            ReviewComment(path="src/a.py", line=2, body="race", symbol="run")
        )
        for path in ("..", "/src/a.py", "src/../../a.py"):
            with self.subTest(path=path):
                with self.assertRaises(ContextLoadError):
                    reconcile_finding_set(
                        (),
                        (current,),
                        review_complete=True,
                        reviewed_paths=(path,),
                    )

    def test_moved_concern_keeps_one_record_under_current_identity(self):
        comment = ReviewComment(
            path="src/b.py",
            line=4,
            body="race",
            symbol="run",
            defect_kind="race",
        )
        current = finding_lifecycle_for_comment(comment, generation=3)
        # An evidence-bearing prior fingerprint for the same concern is what a
        # caller replays when the finding moved or was rephrased.
        previous = FindingLifecycle(
            stable_finding_fingerprint(
                path="src/b.py",
                symbol="run",
                defect_kind="race",
                evidence="earlier prose",
            ),
            "still-present",
            "earlier prose",
            current.concern,
            "src/a.py",
            generation=2,
        )
        self.assertNotEqual(previous.fingerprint, current.fingerprint)
        result = reconcile_finding_set(
            (previous,),
            (current,),
            review_complete=True,
            reviewed_paths=("src/b.py",),
            generation=3,
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].fingerprint, current.fingerprint)
        self.assertEqual(result[0].state, "still-present")
        self.assertEqual(result[0].path, "src/b.py")
        self.assertEqual(result[0].concern, current.concern)
        self.assertEqual(result[0].generation, 3)

    def test_retired_concern_reported_again_is_a_new_finding(self):
        comment = ReviewComment(
            path="src/a.py",
            line=2,
            body="race",
            symbol="run",
            defect_kind="race",
        )
        current = finding_lifecycle_for_comment(comment)
        previous = FindingLifecycle(
            stable_finding_fingerprint(
                path="src/a.py",
                symbol="run",
                defect_kind="race",
                evidence="earlier prose",
            ),
            "fixed",
            "earlier prose",
            current.concern,
            "src/a.py",
        )
        result = reconcile_finding_set(
            (previous,),
            (current,),
            review_complete=True,
            reviewed_paths=("src/a.py",),
        )
        # The current identity supersedes the retired record, so one concern
        # never holds two records in the same reconciled set.
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].fingerprint, current.fingerprint)
        self.assertEqual(result[0].state, "new")
        self.assertEqual(
            len({item.concern for item in result if item.concern is not None}), 1
        )

    def test_moved_concern_from_uncertain_prior_stays_live(self):
        """``uncertain`` is not terminal, so the concern is carried forward."""

        comment = ReviewComment(
            path="src/b.py",
            line=4,
            body="race",
            symbol="run",
            defect_kind="race",
        )
        current = finding_lifecycle_for_comment(comment)
        previous = FindingLifecycle(
            stable_finding_fingerprint(
                path="src/b.py",
                symbol="run",
                defect_kind="race",
                evidence="earlier prose",
            ),
            "uncertain",
            "earlier prose",
            current.concern,
            "src/a.py",
            generation=2,
        )
        result = reconcile_finding_set(
            (previous,),
            (current,),
            review_complete=True,
            reviewed_paths=("src/b.py",),
            generation=2,
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].fingerprint, current.fingerprint)
        self.assertEqual(result[0].state, "still-present")
        self.assertEqual(result[0].path, "src/b.py")

    def test_prior_without_a_path_is_treated_as_unreviewed(self):
        """A pathless prior cannot be proven in scope, so it is retained."""

        previous = FindingLifecycle(
            stable_finding_fingerprint(evidence_id="abc"),
            "still-present",
            concern=stable_concern_identity(evidence_id="abc"),
            path=None,
        )
        result = reconcile_finding_set(
            (previous,),
            (),
            review_complete=True,
            reviewed_paths=("src/a.py",),
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].state, "still-present")
        self.assertIsNone(result[0].path)

    def test_duplicate_prior_concerns_resolve_to_the_first_record(self):
        comment = ReviewComment(
            path="src/a.py",
            line=2,
            body="race",
            symbol="run",
            defect_kind="race",
        )
        current = finding_lifecycle_for_comment(comment)
        first = FindingLifecycle(
            stable_finding_fingerprint(
                path="src/a.py",
                symbol="run",
                defect_kind="race",
                evidence="first prose",
            ),
            "still-present",
            "first prose",
            current.concern,
            "src/a.py",
        )
        second = FindingLifecycle(
            stable_finding_fingerprint(
                path="src/a.py",
                symbol="run",
                defect_kind="race",
                evidence="second prose",
            ),
            "still-present",
            "second prose",
            current.concern,
            "src/a.py",
        )
        self.assertNotEqual(first.fingerprint, second.fingerprint)
        result = reconcile_finding_set(
            (first, second),
            (current,),
            review_complete=True,
            reviewed_paths=("src/a.py",),
        )
        # First-wins keeps the concern lookup deterministic; the unmatched
        # duplicate is still reconciled rather than dropped.
        live = [item for item in result if item.fingerprint == current.fingerprint]
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0].evidence, "first prose")
        self.assertEqual(
            {item.fingerprint for item in result},
            {current.fingerprint, second.fingerprint},
        )

    def test_category_is_not_part_of_finding_identity(self):
        """Reclassifying a lens must not fan a concern out into a new thread."""

        correctness = finding_lifecycle_for_comment(
            ReviewComment(
                path="src/a.py",
                line=2,
                body="race",
                symbol="run",
                defect_kind="race",
                category="correctness",
            )
        )
        maintainability = finding_lifecycle_for_comment(
            ReviewComment(
                path="src/a.py",
                line=9,
                body="race rephrased",
                symbol="run",
                defect_kind="race",
                category="maintainability",
            )
        )
        self.assertEqual(correctness.fingerprint, maintainability.fingerprint)
        self.assertEqual(correctness.concern, maintainability.concern)

    def test_missing_defect_kind_buckets_under_unknown(self):
        unclassified = finding_lifecycle_for_comment(
            ReviewComment(
                path="src/a.py",
                line=2,
                body="race",
                symbol="run",
                category="correctness",
            )
        )
        bucketed = finding_lifecycle_for_comment(
            ReviewComment(
                path="src/a.py",
                line=2,
                body="race",
                symbol="run",
                defect_kind=UNKNOWN_DEFECT_KIND,
            )
        )
        declared = finding_lifecycle_for_comment(
            ReviewComment(
                path="src/a.py",
                line=2,
                body="race",
                symbol="run",
                defect_kind="race",
            )
        )
        self.assertEqual(unclassified.fingerprint, bucketed.fingerprint)
        self.assertNotEqual(unclassified.fingerprint, declared.fingerprint)

    def test_distinct_defect_kinds_do_not_share_a_fingerprint(self):
        race = stable_finding_fingerprint(
            path="src/a.py", symbol="run", defect_kind="race"
        )
        null = stable_finding_fingerprint(
            path="src/a.py", symbol="run", defect_kind="null"
        )
        self.assertNotEqual(race, null)

    def test_cache_invalidates_incompatible_base_and_learnings(self):
        cache = ReviewContextCache(max_entries=4)
        digest_a = "a" * 64
        digest_b = "b" * 64
        digest_c = "c" * 64
        digest_d = "d" * 64
        previous = ReviewContextCacheKey(
            "o/r", 1, "a" * 40, "b" * 40, "e", "m", "p", digest_a, digest_b, digest_c
        )
        current = ReviewContextCacheKey(
            "o/r", 1, "c" * 40, "d" * 40, "e", "m", "p", digest_a, digest_b, digest_d
        )
        cache.put(previous, ("old",))
        self.assertFalse(cache_key_is_compatible(current, previous))
        cache.invalidate_incompatible(current)
        self.assertIsNone(cache.get(previous))

    def test_cache_put_if_newer_does_not_regress_generation(self):
        cache = ReviewContextCache(max_entries=2)
        older = ReviewContextCacheKey(
            "o/r", 1, "a" * 40, "b" * 40, "e", "m", "p", "a" * 64, "b" * 64, "c" * 64
        )
        newer = ReviewContextCacheKey(
            "o/r", 1, "a" * 40, "c" * 40, "e", "m", "p", "a" * 64, "b" * 64, "c" * 64
        )
        cache.put_if_newer(newer, (5, "incremental"), generation=5)
        self.assertFalse(cache.put_if_newer(older, (1, "full"), generation=1))
        self.assertEqual(cache.get(newer), (5, "incremental"))

    def test_incremental_plan_rejects_invalid_paths(self):
        key = ReviewContextCacheKey(
            "o/r", 1, "a" * 40, "b" * 40, "e", "m", "p", "a" * 64, "b" * 64, "c" * 64
        )
        with self.assertRaises(ContextLoadError):
            IncrementalReviewPlan(previous_key=key, related_paths=("../secret.py",))

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

    def test_cache_put_rejects_oversized_metadata_items(self):
        cache = ReviewContextCache(max_entries=1)
        key = ReviewContextCacheKey(
            "o/r", 1, "a" * 40, "b" * 40, "e", "m", "p", "a" * 64, "b" * 64, "c" * 64
        )
        with self.assertRaises(ContextLoadError):
            cache.put(key, ("x" * 513,))

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

    def test_cache_key_changes_when_learning_digest_changes(self):
        from review_sensei.learnings import learning_digest

        first_entries = (
            LearningEntry(
                id="one",
                title="One",
                rule="Use A",
            ),
        )
        second_entries = (
            LearningEntry(
                id="one",
                title="One",
                rule="Use B",
            ),
        )
        first = ReviewContextCacheKey(
            "o/r",
            1,
            "a" * 40,
            "b" * 40,
            "e",
            "m",
            "p",
            "a" * 64,
            "b" * 64,
            learning_digest(first_entries),
        )
        second = ReviewContextCacheKey(
            "o/r",
            1,
            "a" * 40,
            "b" * 40,
            "e",
            "m",
            "p",
            "a" * 64,
            "b" * 64,
            learning_digest(second_entries),
        )
        cache = ReviewContextCache()
        cache.put(first, ("kept",))
        self.assertNotEqual(first.digest(), second.digest())
        self.assertEqual(cache.get(first), ("kept",))
        self.assertIsNone(cache.get(second))

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
            any(
                item.code == "stale" and item.detail == "expired"
                for item in diagnostics
            )
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
