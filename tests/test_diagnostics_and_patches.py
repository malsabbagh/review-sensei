import json
import unittest
from pathlib import Path

from review_sensei.diagnostics import build_plan, run_doctor
from review_sensei.errors import ReviewInputError
from review_sensei.patches import create_patch_suggestion


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


class PatchSuggestionTests(unittest.TestCase):
    def test_only_confirmed_findings_can_create_suggestions(self):
        suggestion = create_patch_suggestion(
            {"id": "finding-1", "status": "confirmed"},
            patch=DIFF,
            base_sha="a" * 40,
            head_sha="b" * 40,
            allowed_paths=("src/app.py",),
        )
        self.assertEqual(suggestion.affected_paths, ("src/app.py",))
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


if __name__ == "__main__":
    unittest.main()
