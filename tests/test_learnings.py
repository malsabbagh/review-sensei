import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from review_sensei.errors import LearningLoadError
from review_sensei.learnings import (
    LearningFeedback,
    LearningStore,
    build_learning_diagnostic_report,
    learning_digest,
    load_learning_feedback,
    load_repository_learnings,
    summarize_learning_feedback,
)
from review_sensei.models import LearningEntry


def _write_entry(directory: Path, name: str, payload: dict[str, object]) -> None:
    (directory / name).write_text(json.dumps(payload), encoding="utf-8")


class RepositoryLearningTests(unittest.TestCase):
    def test_loads_active_entries_and_selects_by_changed_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / ".github" / "review-sensei" / "learnings"
            directory.mkdir(parents=True)
            _write_entry(
                directory,
                "architecture.json",
                {
                    "id": "service-boundary",
                    "title": "Keep provider calls behind adapters",
                    "rule": "Business logic must not import provider SDKs.",
                    "scope": ["src/**"],
                    "rationale": "Provider changes should not alter review behavior.",
                    "category": "architecture",
                    "source": "https://github.com/example/repo/pull/12",
                },
            )
            _write_entry(
                directory,
                "global.json",
                {
                    "id": "no-secrets",
                    "title": "Never commit secrets",
                    "rule": "Do not add credentials or private keys to the repository.",
                    "scope": ["*"],
                    "rationale": "Review output must preserve repository security.",
                },
            )
            _write_entry(
                directory,
                "retired.json",
                {
                    "id": "old-rule",
                    "title": "Retired rule",
                    "rule": "This entry should not reach future prompts.",
                    "scope": ["*"],
                    "status": "retired",
                },
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

    def test_existing_files_load_without_lifecycle_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / ".github" / "review-sensei" / "learnings"
            directory.mkdir(parents=True)
            _write_entry(
                directory,
                "legacy.json",
                {
                    "id": "legacy-rule",
                    "title": "Legacy",
                    "rule": "Keep the previous contract.",
                    "scope": ["*"],
                },
            )

            store = load_repository_learnings(root)

        entry = store.entries[0]
        self.assertEqual(entry.status, "active")
        self.assertIsNone(entry.owner)
        self.assertIsNone(entry.reviewed_at)
        self.assertEqual(entry.supersedes, ())

    def test_superseded_entries_are_not_selected_and_remain_traceable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / ".github" / "review-sensei" / "learnings"
            directory.mkdir(parents=True)
            _write_entry(
                directory,
                "old.json",
                {
                    "id": "old-boundary",
                    "title": "Old boundary",
                    "rule": "Use the previous adapter rule.",
                    "scope": ["src/**"],
                    "status": "superseded",
                    "superseded_by": "new-boundary",
                    "provenance": "docs/architecture.md",
                    "reviewed_at": "2024-01-01T00:00:00Z",
                },
            )
            _write_entry(
                directory,
                "new.json",
                {
                    "id": "new-boundary",
                    "title": "New boundary",
                    "rule": "Keep provider calls behind adapters.",
                    "scope": ["src/**"],
                    "supersedes": ["old-boundary"],
                    "owner": "maintainers",
                },
            )

            store = load_repository_learnings(root)

        self.assertEqual([entry.id for entry in store.entries], ["new-boundary"])
        self.assertEqual(
            [entry.id for entry in store.for_paths(["src/service.py"])],
            ["new-boundary"],
        )
        self.assertEqual(
            {entry.id: entry.superseded_by for entry in store.all_entries},
            {"new-boundary": None, "old-boundary": "new-boundary"},
        )

    def test_duplicate_ids_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / ".github" / "review-sensei" / "learnings"
            directory.mkdir(parents=True)
            payload = {
                "id": "duplicate",
                "title": "Duplicate",
                "rule": "One id only.",
                "scope": ["*"],
            }
            _write_entry(directory, "one.json", payload)
            _write_entry(directory, "two.json", payload)

            with self.assertRaises(LearningLoadError):
                load_repository_learnings(root)

    def test_scope_changes_are_reflected_in_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / ".github" / "review-sensei" / "learnings"
            directory.mkdir(parents=True)
            payload = {
                "id": "python-only",
                "title": "Python only",
                "rule": "Keep adapters isolated.",
                "scope": ["src/**"],
            }
            _write_entry(directory, "python.json", payload)
            store = load_repository_learnings(root)
            self.assertEqual(
                [entry.id for entry in store.for_paths(["docs/readme.md"])],
                [],
            )
            payload["scope"] = ["docs/**"]
            _write_entry(directory, "python.json", payload)
            updated = load_repository_learnings(root)
            self.assertEqual(
                [entry.id for entry in updated.for_paths(["docs/readme.md"])],
                ["python-only"],
            )
            self.assertNotEqual(
                learning_digest(store.all_entries),
                learning_digest(updated.all_entries),
            )

    def test_rejects_invalid_learning_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / ".github" / "review-sensei" / "learnings"
            directory.mkdir(parents=True)
            _write_entry(
                directory,
                "invalid.json",
                {
                    "id": "../../escape",
                    "title": "Invalid",
                    "rule": "Invalid scope.",
                    "scope": ["src/**"],
                },
            )

            with self.assertRaises(LearningLoadError):
                load_repository_learnings(root)

    def test_feedback_summary_does_not_treat_absence_as_approval(self):
        records = (
            LearningFeedback("provider-boundary", "finding-1", "useful"),
            LearningFeedback("provider-boundary", "finding-2", "incorrect"),
        )
        summary = summarize_learning_feedback(
            records,
            known_learning_ids=("provider-boundary", "unused-rule"),
        )
        self.assertEqual(summary["record_count"], 2)
        self.assertTrue(summary["absence_is_not_approval"])
        self.assertFalse(summary["trusted_for_review"])
        self.assertEqual(
            summary["known_learning_ids_without_feedback"], ["unused-rule"]
        )
        self.assertEqual(summary["by_outcome"]["unverified"], 0)

    def test_load_learning_feedback_validates_schema_and_bounds(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "feedback.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "records": [
                            {
                                "learning_id": "provider-boundary",
                                "finding_id": "finding-1",
                                "outcome": "obsolete",
                                "note": "Replaced by a newer rule.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            records = load_learning_feedback(path)
        self.assertEqual(records[0].outcome, "obsolete")
        self.assertEqual(records[0].to_dict()["note"], "Replaced by a newer rule.")

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bad.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "records": [{"outcome": "useful"}],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(LearningLoadError):
                load_learning_feedback(path)

    def _write_feedback(self, directory: Path, document: object) -> Path:
        path = directory / "feedback.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_load_learning_feedback_rejects_unsupported_schema_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self._write_feedback(
                Path(temporary),
                {
                    "schema_version": "2.0",
                    "records": [
                        {
                            "learning_id": "provider-boundary",
                            "finding_id": "finding-1",
                            "outcome": "useful",
                        }
                    ],
                },
            )
            with self.assertRaises(LearningLoadError):
                load_learning_feedback(path)

    def test_schema_version_recheck_holds_without_the_schema_layer(self):
        # The schema's const makes the loader's own re-check unreachable for
        # valid input, so bypass the validator to prove the branch is a real
        # defense if that layer is ever changed or stubbed.
        with tempfile.TemporaryDirectory() as temporary:
            path = self._write_feedback(
                Path(temporary),
                {
                    "schema_version": "2.0",
                    "records": [
                        {
                            "learning_id": "provider-boundary",
                            "finding_id": "finding-1",
                            "outcome": "useful",
                        }
                    ],
                },
            )
            with patch(
                "review_sensei.learnings.validate_public_document",
                return_value=None,
            ):
                with self.assertRaisesRegex(
                    LearningLoadError, "schema_version is unsupported"
                ):
                    load_learning_feedback(path)

    def test_load_learning_feedback_rejects_non_object_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self._write_feedback(
                Path(temporary),
                {"schema_version": "1.0", "records": ["provider-boundary"]},
            )
            with self.assertRaises(LearningLoadError):
                load_learning_feedback(path)

    def test_byte_bounds_reject_multibyte_values_within_maxlength(self):
        # 256 characters satisfies the schema's maxLength but exceeds the
        # authoritative 256-byte bound once a 4-byte character is included.
        finding_id = ("a" * 255) + "\U0001f600"
        self.assertEqual(len(finding_id), 256)
        self.assertGreater(len(finding_id.encode("utf-8")), 256)
        with tempfile.TemporaryDirectory() as temporary:
            path = self._write_feedback(
                Path(temporary),
                {
                    "schema_version": "1.0",
                    "records": [
                        {
                            "learning_id": "provider-boundary",
                            "finding_id": finding_id,
                            "outcome": "useful",
                        }
                    ],
                },
            )
            with self.assertRaisesRegex(LearningLoadError, "UTF-8 bytes"):
                load_learning_feedback(path)

        note = ("a" * 511) + "\U0001f600"
        self.assertEqual(len(note), 512)
        self.assertGreater(len(note.encode("utf-8")), 512)
        with tempfile.TemporaryDirectory() as temporary:
            path = self._write_feedback(
                Path(temporary),
                {
                    "schema_version": "1.0",
                    "records": [
                        {
                            "learning_id": "provider-boundary",
                            "finding_id": "finding-1",
                            "outcome": "useful",
                            "note": note,
                        }
                    ],
                },
            )
            with self.assertRaisesRegex(LearningLoadError, "UTF-8 bytes"):
                load_learning_feedback(path)

    def test_from_dict_does_not_coerce_or_drop_non_string_fields(self):
        with self.assertRaises(LearningLoadError):
            LearningFeedback.from_dict(
                {
                    "learning_id": "provider-boundary",
                    "finding_id": 7,
                    "outcome": "useful",
                }
            )
        with self.assertRaises(LearningLoadError):
            LearningFeedback.from_dict(
                {
                    "learning_id": "provider-boundary",
                    "finding_id": "finding-1",
                    "outcome": "useful",
                    "note": {"nested": "value"},
                }
            )

    def test_feedback_summary_marks_known_id_scope_when_no_store_loaded(self):
        records = (LearningFeedback("provider-boundary", "finding-1", "useful"),)
        unset = summarize_learning_feedback(records)
        self.assertEqual(unset["known_learning_ids_scope"], "unset")
        self.assertEqual(unset["known_learning_ids_without_feedback"], [])
        loaded = summarize_learning_feedback(records, known_learning_ids=("other",))
        self.assertEqual(loaded["known_learning_ids_scope"], "store")
        self.assertEqual(loaded["known_learning_ids_without_feedback"], ["other"])

    def test_selection_digest_tracks_only_the_review_time_selection(self):
        active = LearningEntry(
            id="provider-boundary",
            title="Provider boundary",
            rule="Keep provider calls behind adapters.",
        )
        retired = LearningEntry(
            id="retired-boundary",
            title="Retired boundary",
            rule="Old rule.",
            status="superseded",
            superseded_by="provider-boundary",
        )
        baseline = LearningStore((active,))
        with_retired = LearningStore((active, retired))
        # Editing or adding a retired entry must not invalidate cache state.
        self.assertEqual(baseline.selection_digest, with_retired.selection_digest)
        self.assertNotEqual(
            learning_digest(baseline.all_entries),
            learning_digest(with_retired.all_entries),
        )
        # Editing an entry that can reach a review must invalidate it.
        changed = LearningStore(
            (
                LearningEntry(
                    id="provider-boundary",
                    title="Provider boundary",
                    rule="Keep provider calls behind ports.",
                ),
            )
        )
        self.assertNotEqual(baseline.selection_digest, changed.selection_digest)
        self.assertEqual(
            baseline.selection_digest, learning_digest(baseline.selectable_entries)
        )

    def test_diagnostic_report_is_advisory_and_does_not_mutate(self):
        store = LearningStore(
            (
                LearningEntry(
                    id="expired",
                    title="Expired",
                    rule="Rule",
                    expires_at="2020-01-01T00:00:00Z",
                ),
                LearningEntry(
                    id="missing",
                    title="Missing superseder",
                    rule="Rule",
                    status="superseded",
                    superseded_by="not-present",
                ),
            )
        )
        before = [entry.to_dict() for entry in store.all_entries]
        report = build_learning_diagnostic_report(
            store, now=datetime(2025, 1, 1, tzinfo=timezone.utc)
        )
        self.assertFalse(report["automatic_mutation"])
        self.assertTrue(report["human_decision_required"])
        codes = {item["code"] for item in report["diagnostics"]}
        self.assertIn("stale", codes)
        self.assertIn("missing-superseder", codes)
        self.assertEqual([entry.to_dict() for entry in store.all_entries], before)


if __name__ == "__main__":
    unittest.main()
