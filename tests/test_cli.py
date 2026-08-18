import importlib.metadata
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from review_sensei import ProviderResponse
from review_sensei.cli import _parser, main

DIFF = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1,2 @@
 keep
+change
"""


class FakeProvider:
    name = "fake"
    model = "fake-model"

    def __init__(self):
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        return ProviderResponse(
            text='{"summary":"Architecture reviewed."}',
            provider=self.name,
            model=self.model,
        )


class FakeRegistry:
    def __init__(self, provider):
        self.provider = provider

    def create(self, settings):
        return self.provider


class CliTests(unittest.TestCase):
    def test_provider_mode_selects_mode_specific_defaults(self):
        with patch.dict(
            "os.environ",
            {"REVIEWSENSEI_PROVIDER_MODE": "cloud", "OLLAMA_API_KEY": "secret"},
            clear=True,
        ):
            cloud = _parser().parse_args([])
        self.assertEqual(cloud.base_url, "https://ollama.com/api")
        self.assertEqual(cloud.model, "deepseek-v4-flash:cloud")

        with patch.dict(
            "os.environ", {"REVIEWSENSEI_PROVIDER_MODE": "local"}, clear=True
        ):
            local = _parser().parse_args([])
        self.assertEqual(local.base_url, "http://127.0.0.1:11434/api")
        self.assertEqual(local.model, "qwen3.5:4b")

    def test_parser_accepts_version_flag(self):
        args = _parser().parse_args(["--version"])
        self.assertTrue(args.version)

    def test_parser_accepts_prepare_diff_flags(self):
        from review_sensei.cli import _prepare_diff_parser

        args = _prepare_diff_parser().parse_args(
            [
                "--base-ref",
                "main",
                "--head-ref",
                "feature",
                "--head-repository",
                "owner/repo",
                "--repository",
                "repo",
                "--output",
                "pr.patch",
                "--max-diff-bytes",
                "1024",
                "--max-diff-lines",
                "10",
                "--max-diff-files",
                "2",
                "--max-diff-hunks",
                "3",
            ]
        )
        self.assertEqual(args.base_ref, "main")
        self.assertEqual(args.head_ref, "feature")
        self.assertEqual(args.head_repository, "owner/repo")
        self.assertEqual(args.repository, Path("repo"))
        self.assertEqual(args.output, Path("pr.patch"))
        self.assertEqual(args.max_diff_bytes, 1024)
        self.assertEqual(args.max_diff_lines, 10)
        self.assertEqual(args.max_diff_files, 2)
        self.assertEqual(args.max_diff_hunks, 3)

    def test_cli_version_returns_metadata_version(self):
        expected = importlib.metadata.version("review-sensei")
        stdout = io.StringIO()
        with redirect_stderr(io.StringIO()):
            with patch("sys.stdout", stdout):
                status = main(["--version"])
        self.assertEqual(status, 0)
        self.assertEqual(stdout.getvalue().strip(), expected)

    def test_cli_prepare_diff_version_returns_metadata_version(self):
        expected = importlib.metadata.version("review-sensei")
        stdout = io.StringIO()
        with redirect_stderr(io.StringIO()):
            with patch("sys.stdout", stdout):
                status = main(["prepare-diff", "--version"])
        self.assertEqual(status, 0)
        self.assertEqual(stdout.getvalue().strip(), expected)

    def test_cli_prepare_diff_requires_refs(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = main(["prepare-diff", "--base-ref", "main"])
        self.assertEqual(status, 1)
        self.assertIn("--base-ref and --head-ref are required", stderr.getvalue())

    def test_cli_requires_diff_for_review(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = main([])
        self.assertEqual(status, 1)
        self.assertIn("--diff is required", stderr.getvalue())

    def test_parser_accepts_category_and_stage_directories(self):
        args = _parser().parse_args(
            [
                "--diff",
                "review.patch",
                "--categories-dir",
                "categories",
                "--stages-dir",
                "stages",
                "--context-root",
                "target-checkout",
            ]
        )

        self.assertEqual(args.categories_dir, Path("categories"))
        self.assertEqual(args.stages_dir, Path("stages"))
        self.assertEqual(args.context_root, Path("target-checkout"))

    def test_parser_accepts_fixture_response(self):
        args = _parser().parse_args(
            ["--diff", "review.patch", "--fixture-response", "response.json"]
        )

        self.assertEqual(args.fixture_response, Path("response.json"))

    def test_fixture_response_is_rejected_for_non_fixture_provider(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = main(
                [
                    "--diff",
                    "review.patch",
                    "--fixture-response",
                    "response.json",
                    "--provider",
                    "ollama",
                ]
            )

        self.assertEqual(status, 1)
        self.assertIn(
            "--fixture-response is only valid with --provider fixture",
            stderr.getvalue(),
        )

    def test_fixture_provider_requires_fixture_response(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = main(["--diff", "review.patch", "--provider", "fixture"])

        self.assertEqual(status, 1)
        self.assertIn(
            "--provider fixture requires --fixture-response", stderr.getvalue()
        )

    def test_fixture_cli_does_not_lookup_api_key_environment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            diff_path = root / "review.patch"
            diff_path.write_text(DIFF, encoding="utf-8")
            response_path = root / "response.json"
            response_path.write_text('{"summary":"ok","comments":[]}', encoding="utf-8")
            output_path = root / "review.json"

            class Registry:
                def __init__(self):
                    self.created = []

                def create(self, settings):
                    self.created.append(settings)
                    return FakeProvider()

            registry = Registry()
            with patch.dict("os.environ", {"OLLAMA_API_KEY": "secret-key"}):
                with patch("review_sensei.cli.default_registry", return_value=registry):
                    status = main(
                        [
                            "--diff",
                            str(diff_path),
                            "--provider",
                            "fixture",
                            "--fixture-response",
                            str(response_path),
                            "--model",
                            "fixture-v1",
                            "--no-learning-proposals",
                            "--output",
                            str(output_path),
                        ]
                    )

        self.assertEqual(status, 0)
        self.assertEqual(registry.created[0].api_key, None)
        self.assertEqual(registry.created[0].fixture_response, response_path)

    def test_evaluate_live_requires_allow_live_model_before_provider_creation(self):
        created = []

        class Registry:
            def create(self, settings):
                created.append(settings)
                return FakeProvider()

        stderr = io.StringIO()
        with patch("review_sensei.cli.default_registry", return_value=Registry()):
            with redirect_stderr(stderr):
                status = main(
                    [
                        "evaluate",
                        "--mode",
                        "live",
                        "--corpus",
                        "missing.json",
                        "--provider-version",
                        "1.0",
                    ]
                )

        self.assertEqual(status, 1)
        self.assertEqual(created, [])
        self.assertIn("--mode live requires --allow-live-model", stderr.getvalue())

    def test_evaluate_live_remote_requires_data_egress_before_provider_creation(self):
        created = []

        class Registry:
            def create(self, settings):
                created.append(settings)
                return FakeProvider()

        stderr = io.StringIO()
        with patch("review_sensei.cli.default_registry", return_value=Registry()):
            with redirect_stderr(stderr):
                status = main(
                    [
                        "evaluate",
                        "--mode",
                        "live",
                        "--corpus",
                        "missing.json",
                        "--provider-version",
                        "1.0",
                        "--allow-live-model",
                        "--base-url",
                        "https://ollama.example.com/api",
                    ]
                )

        self.assertEqual(status, 1)
        self.assertEqual(created, [])
        self.assertIn("--allow-data-egress", stderr.getvalue())

    def test_evaluate_fixture_rejects_live_acknowledgement_flags(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = main(
                [
                    "evaluate",
                    "--mode",
                    "fixture",
                    "--corpus",
                    "missing.json",
                    "--allow-live-model",
                ]
            )

        self.assertEqual(status, 1)
        self.assertIn(
            "--allow-live-model is only valid with --mode live",
            stderr.getvalue(),
        )

    def test_cli_loads_lens_documents_and_learnings_from_the_trusted_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            diff_path = root / "review.patch"
            diff_path.write_text(DIFF, encoding="utf-8")
            output_path = root / "review.json"
            categories_dir = root / "categories"
            stages_dir = root / "stages"
            learnings_dir = root / ".github" / "review-sensei" / "learnings"
            categories_dir.mkdir()
            stages_dir.mkdir()
            learnings_dir.mkdir(parents=True)
            (root / "architecture.md").write_text(
                "Dependencies point inward.",
                encoding="utf-8",
            )
            (categories_dir / "architecture.json").write_text(
                json.dumps(
                    {
                        "id": "architecture",
                        "title": "Architecture",
                        "focus": ["Dependency direction"],
                        "context": {
                            "learnings": {"categories": ["architecture"]},
                            "documents": [
                                {"path": "architecture.md", "required": True}
                            ],
                        },
                    }
                ),
                encoding="utf-8",
            )
            (stages_dir / "review.json").write_text(
                json.dumps(
                    {
                        "name": "Architecture review",
                        "category_ids": ["architecture"],
                        "outputs": ["summary"],
                        "prompt_template": (
                            "{review_categories}\n{review_context}\n{diff}"
                        ),
                    }
                ),
                encoding="utf-8",
            )
            (learnings_dir / "architecture.json").write_text(
                json.dumps(
                    {
                        "id": "architecture-rule",
                        "title": "Architecture rule",
                        "rule": "Dependencies point inward.",
                        "category": "architecture",
                    }
                ),
                encoding="utf-8",
            )
            provider = FakeProvider()
            with patch(
                "review_sensei.cli.default_registry",
                return_value=FakeRegistry(provider),
            ):
                status = main(
                    [
                        "--diff",
                        str(diff_path),
                        "--learning-root",
                        str(root),
                        "--categories-dir",
                        str(categories_dir),
                        "--stages-dir",
                        str(stages_dir),
                        "--output",
                        str(output_path),
                    ]
                )

        self.assertEqual(status, 0)
        self.assertIn("architecture.md", provider.requests[0].prompt)
        self.assertIn("architecture-rule", provider.requests[0].prompt)
        self.assertEqual(provider.requests[0].prompt.count("architecture-rule"), 1)

    def test_categories_directory_requires_a_stages_directory(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = main(
                [
                    "--diff",
                    "unused.patch",
                    "--categories-dir",
                    "categories",
                ]
            )

        self.assertEqual(status, 1)
        self.assertIn("--categories-dir requires --stages-dir", stderr.getvalue())

    def test_cli_rejects_oversized_diff_before_provider_creation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "oversized.patch"
            path.write_bytes(b"x" * (1_048_576 + 1))
            created = []

            class Registry:
                def create(self, settings):
                    created.append(settings)
                    return FakeProvider()

            stderr = io.StringIO()
            with patch("review_sensei.cli.default_registry", return_value=Registry()):
                with redirect_stderr(stderr):
                    status = main(["--diff", str(path)])
            self.assertEqual(status, 1)
            self.assertEqual(created, [])
            self.assertNotIn("x" * 20, stderr.getvalue())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
