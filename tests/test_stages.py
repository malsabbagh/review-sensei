import json
import shutil
import tempfile
import unittest
from pathlib import Path

from review_sensei import (
    ProviderResponse,
    ReviewCategory,
    ReviewCategoryCatalog,
    ReviewRequest,
    ReviewService,
    Stage,
    load_review_categories_from_dir,
    load_stages_from_dir,
)
from review_sensei.errors import ReviewFormatError, ReviewInputError
from review_sensei.stages import (
    MAX_CATEGORY_FILE_BYTES,
    MAX_STAGE_FILE_BYTES,
    MAX_STAGE_FILES,
    ContextDocumentSource,
)

DIFF = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,3 @@
 keep
+return value
 end
"""


class FakeProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    @property
    def name(self):
        return "fake"

    @property
    def model(self):
        return "fake-model"

    def complete(self, request):
        self.requests.append(request)
        if not self.responses:
            raise RuntimeError("No more fake responses configured")
        resp_text = self.responses.pop(0)
        return ProviderResponse(
            text=resp_text,
            provider=self.name,
            model=self.model,
        )


class StageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_stage_from_dict_validation(self):
        # Valid stage config
        data = {
            "name": "Summary Stage",
            "prompt_template": "Review {review_categories}\nDraft summary for {diff}",
            "outputs": ["summary"],
            "categories": [
                {
                    "id": "correctness",
                    "title": "Correctness",
                    "focus": ["Incorrect results", "Boundary conditions"],
                }
            ],
        }
        stage = Stage.from_dict(data)
        self.assertEqual(stage.name, "Summary Stage")
        self.assertEqual(
            stage.prompt_template,
            "Review {review_categories}\nDraft summary for {diff}",
        )
        self.assertEqual(stage.outputs, ("summary",))
        self.assertEqual(stage.categories[0].id, "correctness")
        self.assertEqual(
            stage.categories[0].focus, ("Incorrect results", "Boundary conditions")
        )

        # Missing or invalid name
        with self.assertRaises(ReviewInputError):
            Stage.from_dict({"prompt_template": "foo", "outputs": ["summary"]})

        # Missing or invalid prompt_template
        with self.assertRaises(ReviewInputError):
            Stage.from_dict({"name": "foo", "outputs": ["summary"]})

        # Missing or invalid outputs
        with self.assertRaises(ReviewInputError):
            Stage.from_dict({"name": "foo", "prompt_template": "bar"})
        with self.assertRaises(ReviewInputError):
            Stage.from_dict(
                {"name": "foo", "prompt_template": "bar", "outputs": "not-a-list"}
            )

        # Unsupported output fields
        with self.assertRaises(ReviewInputError):
            Stage.from_dict(
                {"name": "foo", "prompt_template": "bar", "outputs": ["unsupported"]}
            )
        with self.assertRaises(ReviewInputError):
            Stage(name="foo", prompt_template="bar", outputs=([],))
        with self.assertRaises(ReviewInputError):
            Stage.from_dict(
                {
                    "name": "foo",
                    "prompt_template": "Review {diff}",
                    "outputs": ["summary"],
                    "outputz": ["comments"],
                }
            )

    def test_loader_does_not_strip_paths_or_treat_source_path_as_a_pattern(self):
        with self.assertRaises(ReviewInputError):
            ContextDocumentSource.from_dict({"path": " docs/architecture.md"})
        with self.assertRaises(ReviewInputError):
            ContextDocumentSource.from_dict({"path": "docs/architecture.md "})
        with self.assertRaises(ReviewInputError):
            ContextDocumentSource.from_dict({"path": "docs/**/*.md"})
        with self.assertRaises(ReviewInputError):
            ContextDocumentSource.from_dict({"path": "docs", "include": [" **/*.md"]})
        with self.assertRaises(ReviewInputError):
            ContextDocumentSource.from_dict({"path": "docs", "exclude": ["*.secret "]})
        with self.assertRaises(ReviewInputError):
            ReviewCategory.from_dict(
                {
                    "id": "architecture",
                    "title": "Architecture",
                    "focus": ["boundaries"],
                    "applies_to": [" src/**"],
                }
            )

    def test_stage_rejects_invalid_categories_and_placeholders(self):
        with self.assertRaises(ReviewInputError):
            ReviewCategory(id="Correctness", title="Correctness", focus=("Behavior",))
        with self.assertRaises(ReviewInputError):
            ReviewCategory(id="correctness", title="Correctness", focus=())
        with self.assertRaises(ReviewInputError):
            Stage(
                name="Missing category placeholder",
                prompt_template="Review {diff}",
                outputs=("comments",),
                categories=(
                    ReviewCategory(
                        id="correctness",
                        title="Correctness",
                        focus=("Behavior",),
                    ),
                ),
            )
        with self.assertRaises(ReviewInputError):
            Stage(
                name="Unknown placeholder",
                prompt_template="Review {diff} using {surprise}",
                outputs=("summary",),
            )

    def test_category_catalog_resolves_reusable_category_files(self):
        category_dir = self.temp_dir / "categories"
        category_dir.mkdir()
        (category_dir / "01-correctness.json").write_text(
            json.dumps(
                {
                    "id": "correctness",
                    "title": "Correctness",
                    "focus": ["Incorrect results"],
                }
            ),
            encoding="utf-8",
        )
        (category_dir / "02-security.json").write_text(
            json.dumps(
                {
                    "id": "security",
                    "title": "Security",
                    "focus": ["Trust boundaries"],
                }
            ),
            encoding="utf-8",
        )

        stage_dir = self.temp_dir / "stages"
        stage_dir.mkdir()
        (stage_dir / "01-review.json").write_text(
            json.dumps(
                {
                    "name": "Reusable lenses",
                    "prompt_template": "{review_categories}\n{diff}",
                    "outputs": ["comments"],
                    "category_ids": ["security", "correctness"],
                }
            ),
            encoding="utf-8",
        )

        catalog = load_review_categories_from_dir(category_dir)
        stage = load_stages_from_dir(
            stage_dir,
            category_catalog=catalog,
        )[0]

        self.assertIsInstance(catalog, ReviewCategoryCatalog)
        self.assertEqual(
            [category.id for category in stage.categories],
            ["security", "correctness"],
        )

    def test_stage_category_references_fail_closed(self):
        stage_data = {
            "name": "Referenced lens",
            "prompt_template": "{review_categories}\n{diff}",
            "outputs": ["comments"],
            "category_ids": ["correctness"],
        }
        with self.assertRaises(ReviewInputError):
            Stage.from_dict(stage_data)
        with self.assertRaises(ReviewInputError):
            Stage.from_dict(stage_data, category_catalog={})
        with self.assertRaises(ReviewInputError):
            ReviewCategoryCatalog().resolve("correctness")
        with self.assertRaises(ReviewInputError):
            ReviewCategoryCatalog(None)
        with self.assertRaises(ReviewInputError):
            ReviewCategoryCatalog().resolve(None)
        with self.assertRaises(ReviewInputError):
            Stage.from_dict(
                stage_data,
                category_catalog=ReviewCategoryCatalog(),
            )

        mixed_data = dict(stage_data)
        mixed_data["categories"] = []
        with self.assertRaises(ReviewInputError):
            Stage.from_dict(
                mixed_data,
                category_catalog=ReviewCategoryCatalog(),
            )

    def test_review_category_loader_rejects_empty_duplicate_and_oversized_files(self):
        category_dir = self.temp_dir / "categories"
        category_dir.mkdir()
        with self.assertRaises(ReviewInputError):
            load_review_categories_from_dir(category_dir)

        category = {
            "id": "correctness",
            "title": "Correctness",
            "focus": ["Incorrect results"],
        }
        (category_dir / "01.json").write_text(json.dumps(category), encoding="utf-8")
        (category_dir / "02.json").write_text(json.dumps(category), encoding="utf-8")
        with self.assertRaises(ReviewInputError):
            load_review_categories_from_dir(category_dir)

        (category_dir / "02.json").unlink()
        (category_dir / "01.json").write_text(
            " " * (MAX_CATEGORY_FILE_BYTES + 1),
            encoding="utf-8",
        )
        with self.assertRaises(ReviewInputError):
            load_review_categories_from_dir(category_dir)

    def test_load_stages_from_dir(self):
        # Write multiple JSON configs in different order
        stage2_path = self.temp_dir / "02-comments.json"
        stage1_path = self.temp_dir / "01-summary.json"

        stage1_data = {
            "name": "Summary Stage",
            "prompt_template": "Template 1",
            "outputs": ["summary"],
        }
        stage2_data = {
            "name": "Comments Stage",
            "prompt_template": "Template 2",
            "outputs": ["comments"],
        }

        stage1_path.write_text(json.dumps(stage1_data), encoding="utf-8")
        stage2_path.write_text(json.dumps(stage2_data), encoding="utf-8")

        # Load stages and verify they are loaded alphabetically
        stages = load_stages_from_dir(self.temp_dir)
        self.assertEqual(len(stages), 2)
        self.assertEqual(stages[0].name, "Summary Stage")
        self.assertEqual(stages[1].name, "Comments Stage")

    def test_load_stages_from_dir_handles_invalid_or_missing_directory(self):
        # Non-existent directory raises OSError
        with self.assertRaises(OSError):
            load_stages_from_dir(Path("/nonexistent/directory/path/123"))

        with self.assertRaises(ReviewInputError):
            load_stages_from_dir(self.temp_dir)

    def test_load_stages_from_dir_rejects_duplicate_names_and_symlinks(self):
        stage_data = {
            "name": "Repeated",
            "prompt_template": "Review {diff}",
            "outputs": ["summary"],
        }
        first = self.temp_dir / "01-first.json"
        second = self.temp_dir / "02-second.json"
        first.write_text(json.dumps(stage_data), encoding="utf-8")
        second.write_text(json.dumps(stage_data), encoding="utf-8")
        with self.assertRaises(ReviewInputError):
            load_stages_from_dir(self.temp_dir)

        second.unlink()
        second.symlink_to(first)
        with self.assertRaises(ReviewInputError):
            load_stages_from_dir(self.temp_dir)

    def test_load_stages_from_dir_enforces_file_count_and_size_limits(self):
        stage_data = {
            "name": "Stage",
            "prompt_template": "Review {diff}",
            "outputs": ["summary"],
        }
        oversized = self.temp_dir / "01-oversized.json"
        oversized.write_text(" " * (MAX_STAGE_FILE_BYTES + 1), encoding="utf-8")
        with self.assertRaises(ReviewInputError):
            load_stages_from_dir(self.temp_dir)

        oversized.unlink()
        for index in range(MAX_STAGE_FILES + 1):
            stage_data["name"] = f"Stage {index}"
            (self.temp_dir / f"{index:02d}.json").write_text(
                json.dumps(stage_data),
                encoding="utf-8",
            )
        with self.assertRaises(ReviewInputError):
            load_stages_from_dir(self.temp_dir)

    def test_review_service_rejects_an_empty_stage_sequence(self):
        with self.assertRaises(ReviewInputError):
            ReviewService(FakeProvider([]), stages=[])

    def test_review_service_rejects_conflicting_category_definitions(self):
        stages = [
            Stage(
                name="First",
                prompt_template="{review_categories}\n{diff}",
                outputs=("summary",),
                categories=(
                    ReviewCategory(
                        id="correctness",
                        title="Correctness",
                        focus=("Incorrect results",),
                    ),
                ),
            ),
            Stage(
                name="Second",
                prompt_template="{review_categories}\n{diff}",
                outputs=("comments",),
                categories=(
                    ReviewCategory(
                        id="correctness",
                        title="Correctness",
                        focus=("Only error handling",),
                    ),
                ),
            ),
        ]

        with self.assertRaises(ReviewInputError):
            ReviewService(FakeProvider([]), stages=stages)

    def test_prompt_substitution_is_single_pass(self):
        stage = Stage(
            name="Single pass",
            prompt_template="Diff: {diff}\nInstructions: {instructions}",
            outputs=("summary",),
        )
        provider = FakeProvider(['{"summary":"Complete."}'])
        service = ReviewService(provider, stages=[stage])

        service.review(
            ReviewRequest(
                diff=DIFF + "\nliteral {instructions} token\n",
                instructions="trusted reviewer instruction",
            )
        )

        self.assertIn("literal {instructions} token", provider.requests[0].prompt)
        self.assertEqual(
            provider.requests[0].prompt.count("trusted reviewer instruction"), 1
        )

    def test_categories_are_rendered_and_constrain_returned_comment_labels(self):
        stage = Stage(
            name="Categorized comments",
            prompt_template="Categories: {review_categories}\nDiff: {diff}",
            outputs=("comments",),
            categories=(
                ReviewCategory(
                    id="correctness",
                    title="Correctness",
                    focus=("Incorrect results", "Boundary conditions"),
                ),
            ),
        )
        provider = FakeProvider(
            [
                '{"comments":[{"path":"src/app.py","line":2,'
                '"body":"This fails.","category":"correctness"}]}'
            ]
        )
        service = ReviewService(provider, stages=[stage])
        result = service.review(ReviewRequest(diff=DIFF))

        self.assertEqual(result.comments[0].category, "correctness")
        self.assertIn('"focus": [', provider.requests[0].prompt)
        self.assertIn('"Boundary conditions"', provider.requests[0].prompt)

        invalid_provider = FakeProvider(
            [
                '{"comments":[{"path":"src/app.py","line":2,'
                '"body":"This fails.","category":"performance"}]}'
            ]
        )
        with self.assertRaises(ReviewFormatError):
            ReviewService(invalid_provider, stages=[stage]).review(
                ReviewRequest(diff=DIFF)
            )

    def test_multi_stage_review_service_execution(self):
        stages = [
            Stage(
                name="Summary Stage",
                prompt_template="Summarize this diff: {diff}\nContext: {context_text}",
                outputs=("summary",),
            ),
            Stage(
                name="Comments Stage",
                prompt_template="Check comments: {diff}\nLearnings: {learnings}",
                outputs=("comments", "learning_proposals"),
            ),
        ]

        provider = FakeProvider(
            [
                # Stage 1 response
                '{"summary": "First half summary."}',
                # Stage 2 response
                '{"comments": [{"path": "src/app.py", "line": 2, "body": "Clean logic.", "severity": "suggest"}], "learning_proposals": [{"title": "L1", "rule": "Rule 1", "scope": ["src/**"]}]}',
            ]
        )

        service = ReviewService(provider, stages=stages)
        request = ReviewRequest(
            diff=DIFF,
            repository="owner/repo",
            pull_request_number=42,
            title="My PR Title",
        )

        result = service.review(request)

        # Verify results are accumulated
        self.assertEqual(result.summary, "First half summary.")
        self.assertEqual(len(result.comments), 1)
        self.assertEqual(result.comments[0].path, "src/app.py")
        self.assertEqual(result.comments[0].line, 2)
        self.assertEqual(result.comments[0].body, "Clean logic.")
        self.assertEqual(len(result.learning_proposals), 1)
        self.assertEqual(result.learning_proposals[0].title, "L1")

        # Verify exact prompts formatted
        self.assertEqual(len(provider.requests), 2)
        self.assertIn("Summarize this diff: ", provider.requests[0].prompt)
        self.assertIn("Repository: owner/repo", provider.requests[0].prompt)
        self.assertIn("My PR Title", provider.requests[0].prompt)
        self.assertIn("Check comments: ", provider.requests[1].prompt)
        self.assertIn("No approved repository learnings", provider.requests[1].prompt)
