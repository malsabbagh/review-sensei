import json
import shutil
import tempfile
import unittest
from itertools import repeat
from pathlib import Path
from unittest.mock import patch

from review_sensei.diagnostics import (
    DiagnosticCheck,
    build_plan,
    render_diagnostic,
    run_doctor,
)
from review_sensei.errors import ReviewInputError
from review_sensei.patches import (
    MAX_PATCH_FILES,
    MAX_PATCH_METADATA_BYTES,
    MAX_PATCH_METADATA_ITEMS,
    PatchSuggestion,
    create_patch_suggestion,
)

DIFF = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1,2 @@
 keep
+change
"""


class DiagnosticsTests(unittest.TestCase):
    def test_doctor_is_bounded_and_reports_unknown_network(self):
        report = run_doctor()
        self.assertEqual(report["schema_version"], "v1")
        self.assertEqual(report["status"], "unknown")
        self.assertTrue(any(check["name"] == "network" for check in report["checks"]))

    def test_plan_never_enables_writes_or_provider_calls(self):
        report = build_plan(diff=DIFF, repository="owner/repo", pull_request=3)
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["operations"]["provider_calls"], 0)
        self.assertEqual(report["operations"]["github_writes"], 0)

    def test_plan_counts_changed_lines_and_bounds_stage_iterables(self):
        diff = DIFF.replace("+1,2", "+1,3").replace("+change", "+change\n+another")
        report = build_plan(diff=diff, stages=("review",))
        self.assertEqual(report["diff"]["changed_lines"], 2)
        with self.assertRaisesRegex(ReviewInputError, "too many"):
            build_plan(stages=repeat("review"))

    def test_doctor_matches_runner_when_stages_need_missing_catalog(self):
        stage = {
            "name": "Configured",
            "category_ids": ["correctness"],
            "outputs": ["summary"],
            "prompt_template": "Review {diff}",
        }
        with tempfile.TemporaryDirectory() as temporary:
            stages_dir = Path(temporary)
            (stages_dir / "configured.json").write_text(
                json.dumps(stage), encoding="utf-8"
            )
            report = run_doctor(stages_dir=stages_dir)
            stages_check = next(
                check for check in report["checks"] if check["name"] == "stages"
            )
            self.assertEqual(stages_check["status"], "action")

    def test_doctor_reports_configured_directory_states_and_provider_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = root / "missing"
            empty_stages = root / "empty-stages"
            empty_categories = root / "empty-categories"
            empty_stages.mkdir()
            empty_categories.mkdir()
            with patch.dict("os.environ", {"REVIEWSENSEI_PROVIDER_MODE": "invalid"}):
                report = run_doctor(
                    stages_dir=missing,
                    categories_dir=missing,
                    context_root=missing,
                    include_network=True,
                )
            self.assertEqual(report["status"], "action")
            self.assertTrue(
                any(check["name"] == "provider-mode" for check in report["checks"])
            )
            empty_report = run_doctor(
                stages_dir=empty_stages, categories_dir=empty_categories
            )
            self.assertTrue(
                all(
                    check["status"] == "action"
                    for check in empty_report["checks"]
                    if check["name"] in {"stages", "categories"}
                )
            )

    def test_doctor_validates_configured_packaged_shapes(self):
        source_root = Path(__file__).parents[1] / "src" / "review_sensei"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            categories_dir = root / "categories"
            stages_dir = root / "stages"
            context_dir = root / "context"
            categories_dir.mkdir()
            stages_dir.mkdir()
            context_dir.mkdir()
            for path in (source_root / "default_categories").glob("*.json"):
                shutil.copyfile(path, categories_dir / path.name)
            shutil.copyfile(
                source_root / "default_stages" / "01-default-review.json",
                stages_dir / "01-default-review.json",
            )
            report = run_doctor(
                stages_dir=stages_dir,
                categories_dir=categories_dir,
                context_root=context_dir,
            )
            self.assertEqual(report["status"], "unknown")
            self.assertTrue(
                all(
                    check["status"] == "pass"
                    for check in report["checks"]
                    if check["name"] in {"stages", "categories", "context"}
                )
            )
            (categories_dir / "broken.json").write_text("{}", encoding="utf-8")
            broken = run_doctor(categories_dir=categories_dir)
            category_check = next(
                check for check in broken["checks"] if check["name"] == "categories"
            )
            self.assertEqual(category_check["status"], "action")
            with patch("review_sensei.diagnostics.load_stages_from_dir") as load_stages:
                skipped = run_doctor(
                    stages_dir=stages_dir, categories_dir=categories_dir
                )
            self.assertTrue(
                all(call.args[0] != stages_dir for call in load_stages.call_args_list)
            )
            stages_check = next(
                check for check in skipped["checks"] if check["name"] == "stages"
            )
            self.assertEqual(stages_check["status"], "action")
            self.assertIn("categories configuration failed", stages_check["detail"])

    def test_plan_rejects_invalid_identity_and_provider_inputs(self):
        with self.assertRaises(ReviewInputError):
            build_plan(repository=1)  # type: ignore[arg-type]
        with self.assertRaises(ReviewInputError):
            build_plan(repository=" ")
        with self.assertRaises(ReviewInputError):
            build_plan(repository="x" * 513)
        for value in (True, 0, "1"):
            with self.subTest(value=value), self.assertRaises(ReviewInputError):
                build_plan(pull_request=value)  # type: ignore[arg-type]
        for value in (1, " "):
            with self.subTest(value=value), self.assertRaises(ReviewInputError):
                build_plan(title=value)  # type: ignore[arg-type]
        with self.assertRaises(ReviewInputError):
            build_plan(title="x" * 4097)
        with self.assertRaises(ReviewInputError):
            build_plan(stages=None)  # type: ignore[arg-type]
        with self.assertRaises(ReviewInputError):
            build_plan(stages=("",))
        with self.assertRaises(ReviewInputError):
            build_plan(stages=("same", "same"))
        with self.assertRaises(ReviewInputError):
            build_plan(provider_mode="invalid")
        with patch.dict("os.environ", {"REVIEWSENSEI_PROVIDER_MODE": "invalid"}):
            with self.assertRaises(ReviewInputError):
                build_plan()
        with self.assertRaisesRegex(ReviewInputError, "diff must be a string"):
            build_plan(diff=b"not-a-string")  # type: ignore[arg-type]

    def test_render_diagnostic_supports_json_and_all_human_fields(self):
        check = DiagnosticCheck("sample", "pass", "ok")
        document = {
            "status": "pass",
            "version": None,
            "checks": [check.to_dict()],
            "provider_mode": "local",
            "stages": ["one"],
            "operations": {"provider_calls": 0, "github_writes": 0},
        }
        rendered = render_diagnostic(document)
        self.assertIn("status: pass", rendered)
        self.assertIn("version: unknown", rendered)
        self.assertIn("operations: provider_calls=0", rendered)
        self.assertEqual(render_diagnostic(document, as_json=True)[0], "{")
        self.assertIn("unknown", render_diagnostic({}))


class PatchSuggestionTests(unittest.TestCase):
    SNAPSHOT_MODES = {"src/app.py": "100644"}
    FINDING = {"id": "finding-1", "status": "confirmed", "path": "src/app.py"}

    def test_only_confirmed_findings_can_create_suggestions(self):
        suggestion = create_patch_suggestion(
            self.FINDING,
            patch=DIFF,
            base_sha="a" * 40,
            head_sha="b" * 40,
            allowed_paths=("src/app.py",),
            snapshot_modes=self.SNAPSHOT_MODES,
        )
        self.assertEqual(suggestion.affected_paths, ("src/app.py",))
        self.assertEqual(suggestion.snapshot_modes, (("src/app.py", "100644"),))
        self.assertEqual(suggestion.to_dict()["snapshot_modes"], self.SNAPSHOT_MODES)
        self.assertFalse(suggestion.to_dict()["accepted"])

    def test_unverified_or_out_of_scope_suggestions_fail_closed(self):
        with self.assertRaises(ReviewInputError):
            create_patch_suggestion(
                {"id": "finding-1", "status": "rejected", "path": "src/app.py"},
                patch=DIFF,
                base_sha="a" * 40,
                head_sha="b" * 40,
                allowed_paths=("src/app.py",),
            )
        with self.assertRaisesRegex(ReviewInputError, "exactly match the finding"):
            create_patch_suggestion(
                self.FINDING,
                patch=DIFF,
                base_sha="a" * 40,
                head_sha="b" * 40,
                allowed_paths=("src/other.py",),
            )

    def test_allowed_paths_must_exactly_match_changed_paths(self):
        with self.assertRaisesRegex(ReviewInputError, "exactly match the validated"):
            create_patch_suggestion(
                {**self.FINDING, "affected_paths": ("src/app.py", "src/extra.py")},
                patch=DIFF,
                base_sha="a" * 40,
                head_sha="b" * 40,
                allowed_paths=("src/app.py", "src/extra.py"),
                snapshot_modes=self.SNAPSHOT_MODES,
            )

    def test_changed_paths_are_validated_as_repository_paths(self):
        traversal_patch = DIFF.replace("src/app.py", "src/../evil.py")
        with self.assertRaises(ReviewInputError):
            create_patch_suggestion(
                {"id": "finding-1", "status": "confirmed", "path": "src/../evil.py"},
                patch=traversal_patch,
                base_sha="a" * 40,
                head_sha="b" * 40,
                allowed_paths=("src/../evil.py",),
                snapshot_modes={"src/../evil.py": "100644"},
            )

    def test_mode_less_patch_requires_exact_regular_file_snapshot_modes(self):
        with self.assertRaisesRegex(
            ReviewInputError, "trusted regular-file snapshot mode provenance"
        ):
            create_patch_suggestion(
                self.FINDING,
                patch=DIFF,
                base_sha="a" * 40,
                head_sha="b" * 40,
                allowed_paths=("src/app.py",),
            )
        with self.assertRaisesRegex(ReviewInputError, "cover exactly"):
            create_patch_suggestion(
                self.FINDING,
                patch=DIFF,
                base_sha="a" * 40,
                head_sha="b" * 40,
                allowed_paths=("src/app.py",),
                snapshot_modes={},
            )
        with self.assertRaisesRegex(ReviewInputError, "regular files"):
            create_patch_suggestion(
                self.FINDING,
                patch=DIFF,
                base_sha="a" * 40,
                head_sha="b" * 40,
                allowed_paths=("src/app.py",),
                snapshot_modes={"src/app.py": "120000"},
            )

    def test_snapshot_modes_cannot_authorize_a_symlink_patch(self):
        symlink_diff = DIFF.replace("src/app.py", "src/link").replace(
            "src/app.py", "src/link"
        )
        with self.assertRaisesRegex(ReviewInputError, "regular files"):
            create_patch_suggestion(
                {"id": "finding-1", "status": "confirmed", "path": "src/link"},
                patch=symlink_diff,
                base_sha="a" * 40,
                head_sha="b" * 40,
                allowed_paths=("src/link",),
                snapshot_modes={"src/link": "120000"},
            )

    def test_unbounded_iterables_are_rejected_after_bounded_inspection(self):
        with self.assertRaisesRegex(ReviewInputError, "allowed_paths"):
            create_patch_suggestion(
                self.FINDING,
                patch=DIFF,
                base_sha="a" * 40,
                head_sha="b" * 40,
                allowed_paths=repeat("src/app.py"),
                snapshot_modes=self.SNAPSHOT_MODES,
            )
        with self.assertRaisesRegex(ReviewInputError, "metadata"):
            create_patch_suggestion(
                self.FINDING,
                patch=DIFF,
                base_sha="a" * 40,
                head_sha="b" * 40,
                allowed_paths=("src/app.py",),
                snapshot_modes=self.SNAPSHOT_MODES,
                assumptions=repeat("documented"),
            )
        with self.assertRaisesRegex(ReviewInputError, "metadata"):
            create_patch_suggestion(
                self.FINDING,
                patch=DIFF,
                base_sha="a" * 40,
                head_sha="b" * 40,
                allowed_paths=("src/app.py",),
                snapshot_modes=self.SNAPSHOT_MODES,
                validation=repeat("not executed"),
            )

    def test_all_git_symlink_mode_headers_are_rejected_structurally(self):
        mode_headers = (
            "new file mode 120000",
            "old file mode 120000",
            "old mode 120000",
            "new mode 120000",
            "deleted file mode 120000",
            "index 1111111..2222222 120000",
        )
        for header in mode_headers:
            with self.subTest(header=header):
                with self.assertRaisesRegex(
                    ReviewInputError, "symlink patches are not supported"
                ):
                    create_patch_suggestion(
                        self.FINDING,
                        patch=f"{header}\n{DIFF}",
                        base_sha="a" * 40,
                        head_sha="b" * 40,
                        allowed_paths=("src/app.py",),
                    )

    def test_textual_binary_phrases_are_not_treated_as_binary_markers(self):
        textual = DIFF.replace("+change", "+Binary files is a documentation phrase")
        textual = textual.replace(" keep", " GIT binary patch is documented here")
        suggestion = create_patch_suggestion(
            self.FINDING,
            patch=textual,
            base_sha="a" * 40,
            head_sha="b" * 40,
            allowed_paths=("src/app.py",),
            snapshot_modes=self.SNAPSHOT_MODES,
        )
        self.assertEqual(suggestion.affected_paths, ("src/app.py",))

    def test_direct_patch_suggestions_must_declare_exact_changed_paths(self):
        with self.assertRaisesRegex(ReviewInputError, "match the paths changed"):
            PatchSuggestion(
                finding_id="finding-1",
                base_sha="a" * 40,
                head_sha="b" * 40,
                patch=DIFF,
                affected_paths=("src/other.py",),
                snapshot_modes=(("src/other.py", "100644"),),
            )

    def test_patch_helper_iterables_and_metadata_fail_closed(self):
        finding = self.FINDING
        with self.assertRaises(ReviewInputError):
            create_patch_suggestion(
                finding,
                patch=DIFF,
                base_sha="a" * 40,
                head_sha="b" * 40,
                allowed_paths=1,  # type: ignore[arg-type]
                snapshot_modes=self.SNAPSHOT_MODES,
            )
        with self.assertRaises(ReviewInputError):
            create_patch_suggestion(
                finding,
                patch=DIFF,
                base_sha="a" * 40,
                head_sha="b" * 40,
                allowed_paths=(1,),  # type: ignore[tuple-item]
                snapshot_modes=self.SNAPSHOT_MODES,
            )
        with self.assertRaises(ReviewInputError):
            create_patch_suggestion(
                finding,
                patch=DIFF,
                base_sha="a" * 40,
                head_sha="b" * 40,
                allowed_paths=(),
                snapshot_modes=self.SNAPSHOT_MODES,
            )
        with self.assertRaises(ReviewInputError):
            create_patch_suggestion(
                {**finding, "id": "f" * 257},
                patch=DIFF,
                base_sha="a" * 40,
                head_sha="b" * 40,
                allowed_paths=("src/app.py",),
                snapshot_modes=self.SNAPSHOT_MODES,
            )
        for label in ("assumptions", "validation"):
            with self.subTest(label=label), self.assertRaises(ReviewInputError):
                create_patch_suggestion(
                    finding,
                    patch=DIFF,
                    base_sha="a" * 40,
                    head_sha="b" * 40,
                    allowed_paths=("src/app.py",),
                    snapshot_modes=self.SNAPSHOT_MODES,
                    **{label: 1},  # type: ignore[arg-type]
                )
        with self.assertRaises(ReviewInputError):
            create_patch_suggestion(
                finding,
                patch=DIFF,
                base_sha="a" * 40,
                head_sha="b" * 40,
                allowed_paths=("src/app.py",),
                snapshot_modes=self.SNAPSHOT_MODES,
                assumptions=("x" * (MAX_PATCH_METADATA_BYTES + 1),),
            )
        with self.assertRaises(ReviewInputError):
            create_patch_suggestion(
                finding,
                patch=DIFF,
                base_sha="a" * 40,
                head_sha="b" * 40,
                allowed_paths=("src/app.py",),
                snapshot_modes=self.SNAPSHOT_MODES,
                assumptions=repeat("x", MAX_PATCH_METADATA_ITEMS + 1),
            )

    def test_patch_snapshot_mode_and_constructor_boundaries(self):
        finding = self.FINDING
        common = {
            "patch": DIFF,
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "allowed_paths": ("src/app.py",),
        }
        with self.assertRaises(ReviewInputError):
            create_patch_suggestion(
                **common, snapshot_modes={"src/app.py": "bad"}, finding=finding
            )
        with self.assertRaises(ReviewInputError):
            create_patch_suggestion(
                **common, snapshot_modes={1: "100644"}, finding=finding
            )  # type: ignore[dict-item]
        with self.assertRaises(ReviewInputError):
            create_patch_suggestion(
                **common, snapshot_modes={"src/app.py": "120000"}, finding=finding
            )
        too_many_modes = {
            f"src/{index}.py": "100644" for index in range(MAX_PATCH_FILES + 1)
        }
        with self.assertRaises(ReviewInputError):
            create_patch_suggestion(
                **common, snapshot_modes=too_many_modes, finding=finding
            )

        valid = create_patch_suggestion(
            finding,
            patch=DIFF,
            base_sha="a" * 40,
            head_sha="b" * 40,
            allowed_paths=("src/app.py",),
            snapshot_modes=self.SNAPSHOT_MODES,
        )
        variants = (
            {"finding_id": "", "base_sha": valid.base_sha},
            {"finding_id": valid.finding_id, "base_sha": "bad"},
            {"finding_id": valid.finding_id, "patch": ""},
            {"finding_id": valid.finding_id, "affected_paths": ()},
            {
                "finding_id": valid.finding_id,
                "affected_paths": ("src/app.py", "src/app.py"),
            },
            {"finding_id": valid.finding_id, "snapshot_modes": ()},
            {
                "finding_id": valid.finding_id,
                "snapshot_modes": (
                    ("src/app.py", "100644"),
                    ("src/other.py", "100644"),
                ),
            },
            {"finding_id": valid.finding_id, "assumptions": (1,)},
            {"finding_id": valid.finding_id, "validation": ("",)},
            {"finding_id": valid.finding_id, "accepted": True},
        )
        for changes in variants:
            values = {
                "finding_id": valid.finding_id,
                "base_sha": valid.base_sha,
                "head_sha": valid.head_sha,
                "patch": valid.patch,
                "affected_paths": valid.affected_paths,
                "snapshot_modes": valid.snapshot_modes,
            }
            values.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ReviewInputError):
                PatchSuggestion(**values)


if __name__ == "__main__":
    unittest.main()
