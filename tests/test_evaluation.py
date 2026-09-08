import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from review_sensei.errors import ReviewInputError
from review_sensei.evaluation import (
    ExpectedFinding,
    MeasuredProvider,
    _one_to_one_matches,
    build_report,
    endpoint_scope,
    evaluate_fixture,
    load_corpus,
)
from review_sensei.models import ReviewComment
from review_sensei.providers.fixture import FixtureProvider


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_utf8(path: Path, value: str) -> None:
    """Write fixture bytes without platform-specific newline translation."""

    path.write_bytes(value.encode("utf-8"))


def _write_corpus(
    root: Path,
    *,
    response_text: str = '{"summary":"ok","comments":[]}',
    expected_result_text: str | None = None,
    include_secret: bool = False,
    extra_unindexed: str | None = None,
) -> Path:
    diffs = root / "diffs"
    responses = root / "responses"
    expected = root / "expected"
    diffs.mkdir()
    responses.mkdir()
    expected.mkdir()
    (root / "sample-repository" / "base").mkdir(parents=True)
    (root / "sample-repository" / "head").mkdir(parents=True)
    diff = (
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1 +1,2 @@\n"
        " keep\n"
        "+change\n"
    )
    _write_utf8(diffs / "example.patch", diff)
    _write_utf8(responses / "example.json", response_text)
    if expected_result_text is None:
        expected_result_text = (
            '{"summary":"ok","comments":[],"provider":"fixture",'
            '"model":"fixture-v1","learning_proposals":[]}'
        )
    _write_utf8(expected / "example.review.json", expected_result_text)
    _write_utf8(
        root / "README.md",
        "synthetic corpus" + (" PRIVATE_DIFF_MARKER" if include_secret else ""),
    )
    if extra_unindexed:
        _write_utf8(root / extra_unindexed, "extra")
    files = [
        {
            "path": "corpus.json",
            "sha256": "0" * 64,
            "purpose": "manifest",
        },
        {
            "path": "diffs/example.patch",
            "sha256": _sha256(diff),
            "purpose": "diff",
        },
        {
            "path": "responses/example.json",
            "sha256": _sha256(response_text),
            "purpose": "response",
        },
        {
            "path": "expected/example.review.json",
            "sha256": _sha256(expected_result_text),
            "purpose": "expected result",
        },
        {
            "path": "README.md",
            "sha256": _sha256(
                "synthetic corpus" + (" PRIVATE_DIFF_MARKER" if include_secret else "")
            ),
            "purpose": "readme",
        },
    ]
    corpus = {
        "schema_version": "1.0",
        "corpus_id": "review-sensei-synthetic-v1",
        "version": "1.0",
        "description": "Synthetic regression corpus.",
        "license": "CC0-1.0",
        "provenance": {"synthetic": True, "statement": "All contents are synthetic."},
        "files": files,
        "sample_repository": {
            "base_root": "sample-repository/base",
            "head_root": "sample-repository/head",
        },
        "review_configuration": {"id": "packaged-defaults-v1"},
        "deterministic_thresholds": {
            "exact_fixture_result_rate": 1.0,
            "expected_contract_rejection_rate": 1.0,
        },
        "quality_thresholds": {
            "actionable_precision_minimum": 0.75,
            "false_positive_rate_maximum": 0.25,
            "expected_finding_recall_minimum": 0.75,
            "location_validity_minimum": 1.0,
            "category_coverage_minimum": 0.25,
        },
        "cases": [
            {
                "id": "example",
                "title": "Example case",
                "kind": "quality",
                "category": "correctness",
                "diff_path": "diffs/example.patch",
                "response_path": "responses/example.json",
                "expected_result_path": "expected/example.review.json",
                "expected_error_category": "none",
            }
        ],
    }
    corpus_path = root / "corpus.json"
    _write_utf8(corpus_path, json.dumps(corpus, indent=2))
    corpus["files"][0]["sha256"] = ""
    corpus["files"][0]["sha256"] = hashlib.sha256(
        json.dumps(
            corpus, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
    ).hexdigest()
    _write_utf8(corpus_path, json.dumps(corpus, indent=2))
    return corpus_path


class EvaluationTests(unittest.TestCase):
    def test_load_corpus_validates_inventory_and_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            corpus_path = _write_corpus(Path(temp_dir))

            corpus = load_corpus(corpus_path)

        self.assertEqual(corpus.corpus_id, "review-sensei-synthetic-v1")
        self.assertEqual(len(corpus.files), 5)

    def test_load_corpus_rejects_undeclared_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            corpus_path = _write_corpus(Path(temp_dir), extra_unindexed="extra.txt")

            with self.assertRaises(ReviewInputError):
                load_corpus(corpus_path)

    def test_load_corpus_rejects_sample_repository_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            corpus_path = _write_corpus(Path(temp_dir))
            corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
            corpus["sample_repository"]["base_root"] = "../outside"
            corpus["files"][0]["sha256"] = ""
            corpus["files"][0]["sha256"] = hashlib.sha256(
                json.dumps(
                    corpus,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode("utf-8")
            ).hexdigest()
            _write_utf8(corpus_path, json.dumps(corpus, indent=2))

            with self.assertRaises(ReviewInputError):
                load_corpus(corpus_path)

    def test_load_corpus_rejects_secret_marker_without_echoing_value(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            corpus_path = _write_corpus(Path(temp_dir), include_secret=True)

            with self.assertRaises(ReviewInputError) as raised:
                load_corpus(corpus_path)

        self.assertNotIn("PRIVATE_DIFF_MARKER", str(raised.exception))

    def test_one_to_one_matching_counts_false_positives(self) -> None:
        comments = (
            ReviewComment(
                path="src/app.py",
                line=2,
                body="This is a security finding",
                category="security",
            ),
            ReviewComment(
                path="src/app.py",
                line=2,
                body="This is not expected",
                category="maintainability",
            ),
        )
        expected = [
            ExpectedFinding(
                path="src/app.py",
                line=2,
                category="security",
                body_terms=("security", "finding"),
            )
        ]

        actual, matched, false_positives = _one_to_one_matches(comments, expected)

        self.assertEqual(actual, 1)
        self.assertEqual(matched, 1)
        self.assertEqual(false_positives, 1)

    def test_expected_finding_terms_use_the_same_normalization_as_comments(
        self,
    ) -> None:
        expected = ExpectedFinding.from_dict(
            {
                "path": "src/app.py",
                "line": 2,
                "category": "correctness",
                "body_terms": ["page_size"],
            }
        )

        self.assertEqual(expected.body_terms, ("page", "size"))

    def test_endpoint_scope_classifies_loopback_and_remote(self) -> None:
        self.assertEqual(endpoint_scope(None), "none")
        self.assertEqual(endpoint_scope("http://127.0.0.1:11434/api"), "loopback")
        self.assertEqual(endpoint_scope("https://ollama.com/api"), "remote")

    def test_fixture_evaluation_report_passes_and_has_no_raw_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            corpus = load_corpus(_write_corpus(Path(temp_dir)))

            report = evaluate_fixture(corpus)

        self.assertTrue(report["passed"])
        self.assertEqual(report["deterministic"]["exact_fixture_result_rate"], 1.0)
        self.assertEqual(report["metrics"]["provider_calls"], 1)
        self.assertNotIn("diff", report)
        self.assertNotIn("response", report)

    def test_degraded_report_fails_named_thresholds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            corpus = load_corpus(_write_corpus(Path(temp_dir)))
            measured = MeasuredProvider(
                FixtureProvider(Path(temp_dir) / "missing.json", model="fixture-v1")
            )
            cases = [
                {
                    "id": "example",
                    "kind": "quality",
                    "category": "correctness",
                    "status": "failed",
                    "expected_matches": 1,
                    "actual_matches": 0,
                    "false_positives": 1,
                    "location_valid": False,
                    "category_valid": False,
                    "elapsed_ms": 1,
                    "provider_calls": 1,
                    "prompt_bytes": 4,
                    "response_bytes": 4,
                }
            ]

            report = build_report(
                corpus,
                cases,
                measured,
                mode="fixture",
                provider="fixture",
                provider_version=None,
                model="fixture-v1",
                endpoint_scope="none",
            )

        self.assertFalse(report["passed"])
        self.assertIn("exact_fixture_result_rate", report["threshold_failures"])
        self.assertIn("expected_finding_recall_minimum", report["threshold_failures"])


if __name__ == "__main__":
    unittest.main()
