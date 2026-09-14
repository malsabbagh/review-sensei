import json
import tempfile
import unittest
from itertools import repeat
from pathlib import Path

from review_sensei.diagnostics import build_plan, run_doctor
from review_sensei.errors import ReviewInputError
from review_sensei.patches import PatchSuggestion, create_patch_suggestion

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


class PatchSuggestionTests(unittest.TestCase):
    SNAPSHOT_MODES = {"src/app.py": "100644"}

    def test_only_confirmed_findings_can_create_suggestions(self):
        suggestion = create_patch_suggestion(
            {"id": "finding-1", "status": "confirmed"},
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
                {"id": "finding-1", "status": "rejected"},
                patch=DIFF,
                base_sha="a" * 40,
                head_sha="b" * 40,
                allowed_paths=("src/app.py",),
            )
        with self.assertRaises(ReviewInputError):
            create_patch_suggestion(
                {"id": "finding-1", "status": "confirmed"},
                patch=DIFF,
                base_sha="a" * 40,
                head_sha="b" * 40,
                allowed_paths=("src/other.py",),
            )

    def test_mode_less_patch_requires_exact_regular_file_snapshot_modes(self):
        with self.assertRaisesRegex(
            ReviewInputError, "trusted regular-file snapshot mode provenance"
        ):
            create_patch_suggestion(
                {"id": "finding-1", "status": "confirmed"},
                patch=DIFF,
                base_sha="a" * 40,
                head_sha="b" * 40,
                allowed_paths=("src/app.py",),
            )
        with self.assertRaisesRegex(ReviewInputError, "cover exactly"):
            create_patch_suggestion(
                {"id": "finding-1", "status": "confirmed"},
                patch=DIFF,
                base_sha="a" * 40,
                head_sha="b" * 40,
                allowed_paths=("src/app.py",),
                snapshot_modes={},
            )
        with self.assertRaisesRegex(ReviewInputError, "regular files"):
            create_patch_suggestion(
                {"id": "finding-1", "status": "confirmed"},
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
                {"id": "finding-1", "status": "confirmed"},
                patch=symlink_diff,
                base_sha="a" * 40,
                head_sha="b" * 40,
                allowed_paths=("src/link",),
                snapshot_modes={"src/link": "120000"},
            )

    def test_unbounded_iterables_are_rejected_after_bounded_inspection(self):
        with self.assertRaisesRegex(ReviewInputError, "allowed_paths"):
            create_patch_suggestion(
                {"id": "finding-1", "status": "confirmed"},
                patch=DIFF,
                base_sha="a" * 40,
                head_sha="b" * 40,
                allowed_paths=repeat("src/app.py"),
                snapshot_modes=self.SNAPSHOT_MODES,
            )
        with self.assertRaisesRegex(ReviewInputError, "metadata"):
            create_patch_suggestion(
                {"id": "finding-1", "status": "confirmed"},
                patch=DIFF,
                base_sha="a" * 40,
                head_sha="b" * 40,
                allowed_paths=("src/app.py",),
                snapshot_modes=self.SNAPSHOT_MODES,
                assumptions=repeat("documented"),
            )
        with self.assertRaisesRegex(ReviewInputError, "metadata"):
            create_patch_suggestion(
                {"id": "finding-1", "status": "confirmed"},
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
                        {"id": "finding-1", "status": "confirmed"},
                        patch=f"{header}\n{DIFF}",
                        base_sha="a" * 40,
                        head_sha="b" * 40,
                        allowed_paths=("src/app.py",),
                    )

    def test_textual_binary_phrases_are_not_treated_as_binary_markers(self):
        textual = DIFF.replace("+change", "+Binary files is a documentation phrase")
        textual = textual.replace(" keep", " GIT binary patch is documented here")
        suggestion = create_patch_suggestion(
            {"id": "finding-1", "status": "confirmed"},
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


if __name__ == "__main__":
    unittest.main()
