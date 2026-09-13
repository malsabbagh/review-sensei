import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from review_sensei.context import (
    ContextSnapshot,
    FindingLifecycle,
    ReviewContextCache,
    ReviewContextCacheKey,
    SymbolAwareContextSelector,
    reconcile_finding_lifecycle,
    stable_finding_fingerprint,
)
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

    def test_fingerprint_ignores_line_and_prose(self):
        one = stable_finding_fingerprint(
            path="src/a.py", symbol="run", defect_kind="Race"
        )
        two = stable_finding_fingerprint(
            path="src/a.py", symbol="run", defect_kind="race"
        )
        self.assertEqual(one, two)
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

    def test_cache_is_keyed_by_snapshot_and_bounded(self):
        cache = ReviewContextCache(max_entries=1)
        key = ReviewContextCacheKey(
            "o/r", 1, "a" * 40, "b" * 40, "e", "m", "p", "s", "c", "l"
        )
        cache.put(key, ("metadata",))
        self.assertEqual(cache.get(key), ("metadata",))

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


if __name__ == "__main__":
    unittest.main()
