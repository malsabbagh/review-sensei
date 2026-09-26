"""The documented local review contract against the installed wheel.

This is the dist-safe lane's evidence that a fresh installation reviews a
supplied diff with no GitHub Actions identity, OIDC token endpoint, broker,
pull-request identity, or hosted ledger.  The diff, the fixture response, and
the temporary state directory are generated here, so the lane loads only
packaged or generated assets.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))

from packaging_guard import assert_distribution_import  # noqa: E402

assert_distribution_import()

from review_sensei.cli import main  # noqa: E402
from review_sensei.schemas import validate_public_document  # noqa: E402

DIFF = """diff --git a/src/pagination.py b/src/pagination.py
index 1111111..2222222 100644
--- a/src/pagination.py
+++ b/src/pagination.py
@@ -1,3 +1,4 @@
 def page_bounds(page, page_size):
     start = (page - 1) * page_size
-    return start, start + page_size
+    end = start + page_size - 2
+    return start, end
"""

BLOCKING_RESPONSE = {
    "summary": (
        "The pagination slice end is off by one, so the last item of every "
        "page is dropped."
    ),
    "comments": [
        {
            "path": "src/pagination.py",
            "line": 3,
            "side": "RIGHT",
            "body": (
                "The slice end must be start plus page_size; subtracting two "
                "drops the final item."
            ),
            "blocking": True,
            "severity": "high",
            "category": "correctness",
            "fix_effort": "trivial",
        }
    ],
    "learning_proposals": [],
}

CLEAN_RESPONSE = {
    "summary": "The pagination slice bounds stay inside the requested page.",
    "comments": [],
}

HOST_ENVIRONMENT_PREFIXES = (
    "ACTIONS_",
    "GH_",
    "GITHUB_",
    "OLLAMA_",
    "OPENAI_",
    "OPENROUTER_",
    "REVIEWSENSEI_",
    "RUNNER_",
)


class LocalReviewContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)
        self.diff_path = self.root / "review.patch"
        self.diff_path.write_text(DIFF, encoding="utf-8")

    def response_path(self, payload: dict[str, object]) -> Path:
        path = self.root / "response.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def run_review(
        self,
        *arguments: object,
        environment: dict[str, str] | None = None,
    ) -> tuple[int, str, str]:
        argv = [str(argument) for argument in arguments]
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.dict(os.environ, environment or {}, clear=environment is not None),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            status = main(argv)
        return status, stdout.getvalue(), stderr.getvalue()

    def base_arguments(self, payload: dict[str, object]) -> list[object]:
        return [
            "--diff",
            self.diff_path,
            "--provider",
            "fixture",
            "--fixture-response",
            self.response_path(payload),
            "--no-learning-proposals",
        ]

    def clean_environment(self) -> dict[str, str]:
        environment = {
            "HOME": str(self.root / "home"),
            "PATH": os.environ.get("PATH", ""),
        }
        for name in ("LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT"):
            value = os.environ.get(name)
            if value:
                environment[name] = value
        leaked = sorted(
            name
            for name in environment
            if name == "CI" or name.startswith(HOST_ENVIRONMENT_PREFIXES)
        )
        if leaked:
            raise AssertionError(f"clean environment leaked host variables: {leaked}")
        return environment

    def test_default_output_is_readable_text_and_exits_one(self):
        status, stdout, stderr = self.run_review(
            *self.base_arguments(BLOCKING_RESPONSE)
        )

        self.assertEqual(status, 1)
        self.assertTrue(stdout.startswith("ReviewSensei review: complete"))
        self.assertIn("src/pagination.py:3", stdout)
        self.assertIn("[required fix]", stdout)
        with self.assertRaises(ValueError):
            json.loads(stdout)
        self.assertIn("review-sensei: reason=required-fixes-remain", stderr)

    def test_markdown_and_json_formats_are_explicit(self):
        with self.subTest(format="markdown"):
            status, stdout, _ = self.run_review(
                *self.base_arguments(BLOCKING_RESPONSE),
                "--format",
                "markdown",
            )
            self.assertEqual(status, 1)
            self.assertIn("### Required fixes", stdout)
            self.assertIn("**src/pagination.py:3**", stdout)
            self.assertNotIn("ReviewSensei review: complete", stdout)

        with self.subTest(format="json"):
            status, stdout, stderr = self.run_review(
                *self.base_arguments(BLOCKING_RESPONSE),
                "--format",
                "json",
            )
            self.assertEqual(status, 1)
            document = json.loads(stdout)
            validate_public_document(document, "review-result")
            self.assertEqual(document["review_status"], "complete")
            self.assertEqual(len(document["comments"]), 1)
            self.assertNotIn("review-sensei:", stdout)
            self.assertIn("review-sensei:", stderr)

    def test_clean_review_exits_zero_without_a_reason(self):
        status, stdout, stderr = self.run_review(*self.base_arguments(CLEAN_RESPONSE))

        self.assertEqual(status, 0)
        self.assertTrue(stdout)
        self.assertNotIn("reason=", stderr)

    def test_invalid_input_exits_two_with_a_structured_reason(self):
        status, stdout, stderr = self.run_review(
            "--diff",
            self.root / "missing.patch",
            "--provider",
            "fixture",
            "--fixture-response",
            self.response_path(CLEAN_RESPONSE),
        )

        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("reason=invalid-input", stderr)

    def test_review_runs_without_host_identity(self):
        status, stdout, _ = self.run_review(
            *self.base_arguments(BLOCKING_RESPONSE),
            environment=self.clean_environment(),
        )

        self.assertEqual(status, 1)
        self.assertTrue(stdout)


if __name__ == "__main__":
    unittest.main()
