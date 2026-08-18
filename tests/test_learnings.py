import json
import tempfile
import unittest
from pathlib import Path

from review_sensei.errors import LearningLoadError
from review_sensei.learnings import load_repository_learnings


class RepositoryLearningTests(unittest.TestCase):
    def test_loads_active_entries_and_selects_by_changed_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / ".github" / "review-sensei" / "learnings"
            directory.mkdir(parents=True)
            (directory / "architecture.json").write_text(
                json.dumps(
                    {
                        "id": "service-boundary",
                        "title": "Keep provider calls behind adapters",
                        "rule": "Business logic must not import provider SDKs.",
                        "scope": ["src/**"],
                        "rationale": "Provider changes should not alter review behavior.",
                        "category": "architecture",
                        "source": "https://github.com/example/repo/pull/12",
                    }
                ),
                encoding="utf-8",
            )
            (directory / "global.json").write_text(
                json.dumps(
                    {
                        "id": "no-secrets",
                        "title": "Never commit secrets",
                        "rule": "Do not add credentials or private keys to the repository.",
                        "scope": ["*"],
                        "rationale": "Review output must preserve repository security.",
                    }
                ),
                encoding="utf-8",
            )
            (directory / "retired.json").write_text(
                json.dumps(
                    {
                        "id": "old-rule",
                        "title": "Retired rule",
                        "rule": "This entry should not reach future prompts.",
                        "scope": ["*"],
                        "status": "retired",
                    }
                ),
                encoding="utf-8",
            )

            store = load_repository_learnings(root)

            self.assertEqual(
                {entry.id for entry in store.entries},
                {"service-boundary", "no-secrets"},
            )
            self.assertEqual(
                [entry.id for entry in store.for_paths(["src/service.py"])],
                ["no-secrets", "service-boundary"],
            )
            self.assertEqual(
                [entry.id for entry in store.for_paths(["tests/test_service.py"])],
                ["no-secrets"],
            )

    def test_rejects_invalid_learning_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / ".github" / "review-sensei" / "learnings"
            directory.mkdir(parents=True)
            (directory / "invalid.json").write_text(
                json.dumps(
                    {
                        "id": "../../escape",
                        "title": "Invalid",
                        "rule": "Invalid scope.",
                        "scope": ["src/**"],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(LearningLoadError):
                load_repository_learnings(root)
