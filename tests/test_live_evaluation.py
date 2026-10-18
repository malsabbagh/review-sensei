"""Packaged-corpus evaluator controls with synthetic, socket-free providers."""

import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from review_sensei.errors import ReviewInputError
from review_sensei.evaluation import (
    evaluate_fixture,
    evaluate_live,
    load_corpus,
    promotion_record_from_reports,
    require_supported_promotion,
)
from review_sensei.models import ProviderResponse


class OracleProvider:
    name = "controlled-live-oracle"
    model = "independent-live-model"

    def __init__(self, corpus, *, bad=False):
        self.corpus = corpus
        self.quality = [
            case for case in corpus.document["cases"] if case["kind"] == "quality"
        ]
        self.calls = 0
        self.bad = bad

    def complete(self, request):
        case = self.quality[self.calls]
        self.calls += 1
        text = self.corpus.read_asset(
            case["response_path"], maximum=1048576, label="synthetic response"
        )
        document = json.loads(text)
        document["summary"] = "Independent live wording."
        if self.bad:
            document["comments"] = []
        return ProviderResponse(json.dumps(document), self.name, model=self.model)


class LiveEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.corpus = load_corpus(
            Path(__file__).resolve().parents[1] / "evaluation/v1/corpus.json"
        )

    def live(self, *, bad=False):
        provider = OracleProvider(self.corpus, bad=bad)
        report = evaluate_live(
            self.corpus,
            provider,
            provider_version="synthetic-attestation",
            model=provider.model,
            endpoint_scope="remote",
        )
        return report, provider

    def promote(self, report):
        reports = [copy.deepcopy(report) for _ in range(3)]
        for index, item in enumerate(reports):
            item["run"]["invocation_id"] = f"synthetic-independent-{index}"
        return self.checked_record(reports)

    def checked_record(self, reports):
        record = promotion_record_from_reports(
            reports,
            observed_revision="synthetic-attestation",
            evaluated_at="2026-10-18T00:00:00Z",
            reproducibility={"synthetic": True},
            status="supported",
        )
        return require_supported_promotion(record, reports)

    def test_perfect_live_quality_and_same_corpus_fixture_control_can_pass(self):
        report, provider = self.live()
        self.assertTrue(report["passed"], report["threshold_failures"])
        self.assertEqual(report["quality"]["category_coverage"], 1)
        self.assertEqual(report["quality"]["expected_finding_recall"], 1)
        self.assertEqual(report["run"]["provider"], provider.name)
        self.assertEqual(report["run"]["model"], provider.model)
        self.assertEqual(report["metrics"]["provider_calls"], provider.calls)
        self.assertEqual(provider.calls, 5)
        control = report["evaluation_control"]
        self.assertEqual(control["fixture"]["provider"], "fixture")
        self.assertEqual(control["fixture"]["model"], "fixture-v1")
        self.assertNotEqual(
            control["fixture"]["configuration_digest"],
            report["run"]["configuration_digest"],
        )
        self.assertEqual(
            len(control["fixture"]["cases"]), len(self.corpus.document["cases"])
        )
        self.assertEqual(report["deterministic"], control["fixture"]["deterministic"])
        independent = [report, self.live()[0], self.live()[0]]
        self.assertEqual(len({item["run"]["invocation_id"] for item in independent}), 3)
        self.assertEqual(self.checked_record(independent).status, "supported")

    def test_bad_live_quality_cannot_be_rescued_by_passing_fixture_control(self):
        report, _ = self.live(bad=True)
        self.assertFalse(report["passed"])
        self.assertEqual(report["deterministic"]["exact_fixture_result_rate"], 1)
        self.assertIn("expected_finding_recall_minimum", report["threshold_failures"])
        self.assertTrue(evaluate_fixture(self.corpus)["passed"])
        with self.assertRaises(ReviewInputError):
            self.promote(report)

    def test_failed_fixture_control_blocks_perfect_live_quality(self):
        fixture = evaluate_fixture(self.corpus)
        fixture["cases"][0]["status"] = "failed"
        fixture["deterministic"]["exact_fixture_result_rate"] = 0.8
        fixture["quality"]["category_coverage"] = 0.75
        fixture["passed"] = False
        fixture["threshold_failures"] = ["exact_fixture_result_rate"]
        with patch("review_sensei.evaluation.evaluate_fixture", return_value=fixture):
            report, _ = self.live()
        self.assertFalse(report["passed"])
        self.assertEqual(report["quality"]["category_coverage"], 1)
        self.assertIn("exact_fixture_result_rate", report["threshold_failures"])
        with self.assertRaises(ReviewInputError):
            self.promote(report)

    def test_supported_gate_rejects_missing_partial_and_fabricated_control(self):
        report, _ = self.live()
        mutations = (
            lambda value: value.pop("evaluation_control"),
            lambda value: value["cases"].pop(),
            lambda value: value["evaluation_control"]["fixture"]["cases"].pop(),
            lambda value: value["evaluation_control"]["fixture"].update(
                engine_digest="f" * 64
            ),
            lambda value: value["evaluation_control"]["fixture"]["cases"][0].update(
                status="failed"
            ),
            lambda value: value["cases"][0].update(execution_complete=False),
            lambda value: value["cases"][0].update(provider_calls=0),
            lambda value: value["quality"].update(expected_finding_recall=0),
            lambda value: value["cases"][0].update(expected_matches=0),
            lambda value: value["evaluation_control"]["fixture"].update(
                provider="controlled-live-oracle"
            ),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=index):
                modified = copy.deepcopy(report)
                mutate(modified)
                with self.assertRaises(ReviewInputError):
                    self.promote(modified)

    def test_non_fixture_control_identity_is_rejected_before_live_calls(self):
        fixture = evaluate_fixture(self.corpus)
        fixture["run"]["provider"] = "controlled-live-oracle"
        provider = OracleProvider(self.corpus)
        with patch("review_sensei.evaluation.evaluate_fixture", return_value=fixture):
            with self.assertRaisesRegex(ReviewInputError, "local fixture identity"):
                evaluate_live(
                    self.corpus,
                    provider,
                    provider_version="synthetic-attestation",
                    model=provider.model,
                    endpoint_scope="remote",
                )
        self.assertEqual(provider.calls, 0)

    def test_fixture_output_and_thresholds_remain_unchanged(self):
        fixture = evaluate_fixture(self.corpus)
        self.assertTrue(fixture["passed"])
        self.assertNotIn("evaluation_control", fixture)
        report, _ = self.live()
        self.assertEqual(
            report["evaluation_control"]["thresholds"]["deterministic"],
            self.corpus.document["deterministic_thresholds"],
        )
        self.assertEqual(
            report["evaluation_control"]["thresholds"]["quality"],
            self.corpus.document["quality_thresholds"],
        )


if __name__ == "__main__":
    unittest.main()
