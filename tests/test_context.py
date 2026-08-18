import tempfile
import unittest
from pathlib import Path

from review_sensei import (
    ContextDocumentSource,
    LearningEntry,
    RepositoryContextStore,
    ReviewCategory,
)
from review_sensei.context import MAX_CONTEXT_FILE_BYTES, build_review_context_selection
from review_sensei.errors import ContextLoadError, ReviewInputError


class ReviewContextConfigurationTests(unittest.TestCase):
    def test_category_parses_applicability_and_context_sources(self):
        category = ReviewCategory.from_dict(
            {
                "id": "architecture",
                "title": "Architecture",
                "focus": ["Dependency direction"],
                "applies_to": ["src/**", "docs/**"],
                "context": {
                    "learnings": {
                        "categories": ["architecture"],
                        "include_uncategorized": True,
                    },
                    "documents": [
                        {
                            "path": "docs/architecture",
                            "include": ["**/*.md"],
                            "exclude": ["drafts/**"],
                            "required": True,
                        }
                    ],
                },
            }
        )

        self.assertEqual(category.applies_to, ("src/**", "docs/**"))
        self.assertEqual(category.learning_categories, ("architecture",))
        self.assertTrue(category.include_uncategorized_learnings)
        self.assertEqual(category.document_sources[0].path, "docs/architecture")
        self.assertEqual(category.document_sources[0].include, ("**/*.md",))
        self.assertEqual(category.document_sources[0].exclude, ("drafts/**",))
        self.assertTrue(category.document_sources[0].required)

    def test_category_rejects_unsafe_context_paths(self):
        with self.assertRaises(ReviewInputError):
            ReviewCategory.from_dict(
                {
                    "id": "architecture",
                    "title": "Architecture",
                    "focus": ["Dependency direction"],
                    "context": {
                        "documents": [{"path": "../private", "required": True}]
                    },
                }
            )

    def test_category_rejects_unknown_fields_at_every_context_level(self):
        base = {
            "id": "architecture",
            "title": "Architecture",
            "focus": ["Dependency direction"],
        }
        invalid_values = (
            {**base, "appplies_to": ["src/**"]},
            {**base, "context": {"documentz": []}},
            {**base, "context": {"learnings": {"categorie": ["architecture"]}}},
            {
                **base,
                "context": {
                    "documents": [{"path": "docs", "excludes": ["private/**"]}]
                },
            },
        )

        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(ReviewInputError):
                ReviewCategory.from_dict(value)


class RepositoryContextStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir_handle = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir_handle.name)

    def tearDown(self):
        self.temp_dir_handle.cleanup()

    def test_loads_explicit_text_documents_with_provenance(self):
        (self.root / "AGENTS.md").write_text("Repository rules", encoding="utf-8")
        architecture = self.root / "docs" / "architecture"
        (architecture / "drafts").mkdir(parents=True)
        (architecture / "drafts" / "private").mkdir()
        (architecture / "overview.md").write_text(
            "Architecture overview", encoding="utf-8"
        )
        (architecture / "drafts" / "future.md").write_text("Future", encoding="utf-8")
        (architecture / "drafts" / "private" / "future.md").write_text(
            "Private future",
            encoding="utf-8",
        )

        documents = RepositoryContextStore(self.root).for_sources(
            (
                ContextDocumentSource(path="AGENTS.md"),
                ContextDocumentSource(
                    path="docs/architecture",
                    include=("**/*.md",),
                    exclude=("drafts/**",),
                ),
            )
        )

        self.assertEqual(
            [document.path for document in documents],
            ["AGENTS.md", "docs/architecture/overview.md"],
        )
        self.assertEqual(documents[0].content, "Repository rules")
        self.assertEqual(documents[1].content, "Architecture overview")

    def test_optional_missing_sources_are_skipped_but_required_sources_fail(self):
        store = RepositoryContextStore(self.root)
        self.assertEqual(
            store.for_sources((ContextDocumentSource(path="docs/architecture"),)),
            (),
        )
        with self.assertRaises(ContextLoadError):
            store.for_sources(
                (ContextDocumentSource(path="docs/architecture", required=True),)
            )

    def test_rejects_symlinks_and_oversized_documents(self):
        target = self.root / "architecture.md"
        target.write_text("Architecture", encoding="utf-8")
        link = self.root / "linked.md"
        link.symlink_to(target)
        store = RepositoryContextStore(self.root)
        with self.assertRaises(ContextLoadError):
            store.for_sources((ContextDocumentSource(path="linked.md"),))

        real_directory = self.root / "real-docs"
        real_directory.mkdir()
        (real_directory / "overview.md").write_text("Architecture", encoding="utf-8")
        linked_directory = self.root / "linked-docs"
        linked_directory.symlink_to(real_directory, target_is_directory=True)
        with self.assertRaises(ContextLoadError):
            store.for_sources((ContextDocumentSource(path="linked-docs"),))

        oversized = self.root / "oversized.md"
        oversized.write_text("x" * (MAX_CONTEXT_FILE_BYTES + 1), encoding="utf-8")
        with self.assertRaises(ContextLoadError):
            store.for_sources((ContextDocumentSource(path="oversized.md"),))


class ReviewContextSelectionTests(unittest.TestCase):
    def test_selects_applicable_lenses_documents_and_learnings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "architecture.md").write_text(
                "Keep dependencies pointing inward.",
                encoding="utf-8",
            )
            architecture = ReviewCategory(
                id="architecture",
                title="Architecture",
                focus=("Dependency direction",),
                applies_to=("src/**",),
                learning_categories=("architecture",),
                include_uncategorized_learnings=True,
                document_sources=(
                    ContextDocumentSource(path="architecture.md", required=True),
                ),
            )
            security = ReviewCategory(
                id="security",
                title="Security",
                focus=("Trust boundaries",),
                applies_to=("security/**",),
            )
            learnings = (
                LearningEntry(
                    id="architecture-rule",
                    title="Architecture rule",
                    rule="Dependencies point inward.",
                    category="architecture",
                ),
                LearningEntry(
                    id="security-rule",
                    title="Security rule",
                    rule="Validate signatures.",
                    category="security",
                ),
                LearningEntry(
                    id="global-rule",
                    title="Global rule",
                    rule="Keep changes focused.",
                ),
            )

            selection = build_review_context_selection(
                (architecture, security),
                changed_paths=("src/app.py",),
                learnings=learnings,
                context_store=RepositoryContextStore(root),
            )

        self.assertEqual(selection.active_category_ids, ("architecture",))
        self.assertEqual(len(selection.lens_contexts), 1)
        lens_context = selection.lens_contexts[0]
        self.assertEqual(lens_context.category_id, "architecture")
        self.assertEqual(
            [learning.id for learning in lens_context.learnings],
            ["architecture-rule", "global-rule"],
        )
        self.assertEqual(
            [document.path for document in lens_context.documents],
            ["architecture.md"],
        )

    def test_recursive_applicability_matches_nested_paths_and_empty_paths_match_none(
        self,
    ):
        category = ReviewCategory(
            id="source",
            title="Source",
            focus=("Nested source files",),
            applies_to=("src/**",),
        )

        nested = build_review_context_selection(
            (category,),
            changed_paths=("src/package/module.py",),
        )
        empty = build_review_context_selection((category,), changed_paths=())

        self.assertEqual(nested.active_category_ids, ("source",))
        self.assertEqual(empty.active_category_ids, ())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
