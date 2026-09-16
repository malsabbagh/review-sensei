import importlib.util
import json
import tempfile
import unittest
from collections import UserDict
from pathlib import Path

from review_sensei.errors import ReviewInputError
from review_sensei.evaluation import (
    PromotionRecord,
    _is_fixture_alias,
    engine_digest,
    promotion_record_from_reports,
    prompt_digest,
    require_supported_promotion,
    validate_profile_promotion,
    validate_promotion_against_report,
    validate_promotion_record,
)
from review_sensei.release_manifest import (
    _constraint_parts,
    _range_contains,
    _version_parts,
    validate_compatibility_manifest,
)

SHA = "a" * 64


class PromotionAndReleaseTests(unittest.TestCase):
    def test_fixture_promotion_is_rejected(self) -> None:
        with self.assertRaises(ReviewInputError):
            PromotionRecord(
                SHA,
                SHA,
                SHA,
                SHA,
                "fixture",
                "fixture-v1",
                "r1",
                3,
                "2026-01-01",
                {},
            )

    def test_fixture_provider_class_alias_is_rejected_for_support(self) -> None:
        with self.assertRaises(ReviewInputError):
            PromotionRecord(
                SHA,
                SHA,
                SHA,
                SHA,
                "FixtureProvider",
                "fixture-v1",
                "r1",
                3,
                "2026-01-01",
                {"seed": "fixed"},
            )

    def test_supported_promotion_requires_reproducibility_settings(self) -> None:
        with self.assertRaises(ReviewInputError):
            PromotionRecord(
                SHA,
                SHA,
                SHA,
                SHA,
                "ollama",
                "model",
                "r1",
                3,
                "2026-01-01",
                {},
            )

    def test_mapping_reproducibility_is_copied_safely(self) -> None:
        source = UserDict({"seed": "fixed"})
        record = PromotionRecord(
            SHA,
            SHA,
            SHA,
            SHA,
            "ollama",
            "model",
            "r1",
            3,
            "2026-01-01",
            source,
        )
        source["seed"] = "mutated"
        self.assertEqual(record.to_dict()["reproducibility"], {"seed": "fixed"})

    def test_profile_promotion_rejects_fixture_and_mismatched_identity(self) -> None:
        live = PromotionRecord(
            SHA,
            SHA,
            SHA,
            SHA,
            "openai-compatible",
            "gpt-4o-mini",
            "fp_test",
            3,
            "2026-01-01",
            {"seed": "fixed"},
        )
        fixture_path = (
            Path(__file__).resolve().parent
            / "fixtures/schemas/golden/evaluation-report.json"
        )
        remote_report = json.loads(fixture_path.read_text(encoding="utf-8"))
        remote_report["run"]["mode"] = "live"
        remote_report["run"]["provider"] = "openai-compatible"
        remote_report["run"]["model"] = "gpt-4o-mini"
        remote_report["run"]["endpoint_scope"] = "remote"
        remote_report["run"]["invocation_id"] = "a" * 32
        validate_profile_promotion("fast-triage", live, [remote_report])
        with self.assertRaisesRegex(
            ReviewInputError, "requires live evaluation reports"
        ):
            validate_profile_promotion("fast-triage", live)
        with self.assertRaisesRegex(ReviewInputError, "does not match profile"):
            validate_profile_promotion("local-private", live)
        loopback_report = json.loads(fixture_path.read_text(encoding="utf-8"))
        loopback_report["run"]["mode"] = "live"
        loopback_report["run"]["provider"] = "openai-compatible"
        loopback_report["run"]["model"] = "gpt-4o-mini"
        loopback_report["run"]["endpoint_scope"] = "loopback"
        loopback_report["run"]["invocation_id"] = "b" * 32
        with self.assertRaisesRegex(ReviewInputError, "endpoint scope does not match"):
            validate_profile_promotion("fast-triage", live, [loopback_report])
        with self.assertRaisesRegex(ReviewInputError, "fixture-only"):
            validate_profile_promotion(
                "local-private",
                PromotionRecord(
                    SHA,
                    SHA,
                    SHA,
                    SHA,
                    "fixture",
                    "fixture-v1",
                    "r1",
                    1,
                    "2026-01-01",
                    {"seed": "fixed"},
                    status="insufficient",
                ),
            )

    def test_invalid_reproducibility_shape_fails_with_review_input_error(self) -> None:
        with self.assertRaises(ReviewInputError):
            PromotionRecord(
                SHA,
                SHA,
                SHA,
                SHA,
                "ollama",
                "model",
                "r1",
                3,
                "2026-01-01",
                [],
            )

    def test_worker_range_wildcards_use_component_bounds(self) -> None:
        self.assertEqual(
            _constraint_parts("1.x"),
            [(">=", (1, 0, 0)), ("<", (2, 0, 0))],
        )
        self.assertEqual(
            _constraint_parts("1.2.x"),
            [(">=", (1, 2, 0)), ("<", (1, 3, 0))],
        )
        self.assertTrue(_range_contains("1.9.9", "1.x"))
        self.assertFalse(_range_contains("2.0.0", "1.x"))
        self.assertTrue(_range_contains("1.2.9", "1.2.x"))
        self.assertFalse(_range_contains("1.3.0", "1.2.x"))

    def test_worker_range_wildcard_at_zero_is_explicitly_unbounded(self) -> None:
        self.assertEqual(_constraint_parts("x"), [(">=", (0, 0, 0))])
        self.assertTrue(_range_contains("99.99.99", "x"))

    def test_worker_range_rejects_mixed_wildcard_components(self) -> None:
        with self.assertRaises(ValueError):
            _constraint_parts("1.x.0")
        with self.assertRaises(ValueError):
            _range_contains("1.0.0", "x.1")

    def test_worker_range_rejects_empty_or_consecutive_alternatives(self) -> None:
        for expression in (">=1.0.0 ||", ">=1.0.0 || || >=2.0.0"):
            with self.assertRaises(ValueError):
                _range_contains("1.0.0", expression)

    def test_worker_range_rejects_wildcards_after_an_explicit_operator(self) -> None:
        for expression in (">=1.x", ">1.*", "~1.x"):
            with self.subTest(expression=expression):
                with self.assertRaises(ValueError):
                    _range_contains("1.0.0", expression)

    def test_worker_range_rejects_separated_operators(self) -> None:
        with self.assertRaises(ValueError):
            _range_contains("1.0.0", "> = 1.0.0")

    def test_worker_range_rejects_bare_whitespace_constraints(self) -> None:
        for expression in ("1.0.0 2.0.0", ">=1.0.0 2.0.0"):
            with self.assertRaises(ValueError):
                _range_contains("1.0.0", expression)

    def test_worker_range_allows_explicitly_operator_separated_constraints(
        self,
    ) -> None:
        self.assertTrue(_range_contains("1.5.0", ">=1.0.0 <2.0.0"))
        self.assertFalse(_range_contains("2.0.0", ">=1.0.0 <2.0.0"))

    def test_worker_range_operator_boundaries(self) -> None:
        self.assertTrue(_range_contains("1.2.3", "^1.2.3"))
        self.assertTrue(_range_contains("1.9.9", "^1.2.3"))
        self.assertFalse(_range_contains("2.0.0", "^1.2.3"))
        self.assertTrue(_range_contains("0.0.3", "^0.0.3"))
        self.assertFalse(_range_contains("0.0.4", "^0.0.3"))
        self.assertTrue(_range_contains("1.2.3", "~1.2.3"))
        self.assertTrue(_range_contains("1.2.9", "~1.2.3"))
        self.assertFalse(_range_contains("1.3.0", "~1.2.3"))

    def test_non_supported_statuses_allow_one_observed_run(self) -> None:
        for status in ("insufficient", "unsupported"):
            record = PromotionRecord(
                SHA,
                SHA,
                SHA,
                SHA,
                "ollama",
                "model",
                "r1",
                1,
                "2026-01-01",
                {"reason": status},
                status=status,
            )
            self.assertEqual(record.to_dict()["status"], status)

    def test_fixture_alias_matching_is_exact_after_normalization(self) -> None:
        self.assertTrue(_is_fixture_alias("Fixture Provider"))
        self.assertFalse(_is_fixture_alias("fixture-provider-extra"))

    def test_document_validation_rejects_supported_fixture_provider(self) -> None:
        value = {
            "schema_version": "1.0",
            "engine_digest": SHA,
            "prompt_digest": SHA,
            "configuration_digest": SHA,
            "corpus_digest": SHA,
            "provider": "fixture",
            "model": "fixture-v1",
            "observed_revision": "r1",
            "run_count": 3,
            "evaluated_at": "2026-01-01",
            "reproducibility": {"seed": "fixed"},
            "status": "supported",
            "rollback_decision": "revert-to-baseline",
        }
        with self.assertRaises(ReviewInputError):
            validate_promotion_record(value)

    def test_manifest_rejects_duplicate_npm_artifact_names(self) -> None:
        artifact = {"name": "x", "version": "1.0.0", "sha256": SHA}
        value = {
            "schema_version": "1.0",
            "release": "1.0.0",
            "workflow_commit": "c" * 40,
            "compatible_worker_range": ">=1.0.0",
            "provenance": "github-artifact-attestation",
            "artifacts": {
                "workflow": artifact,
                "python": artifact,
                "npm": [artifact, {**artifact, "sha256": "b" * 64}],
                "schemas_version": "1.0",
                "worker": artifact,
            },
        }
        with self.assertRaises(ReviewInputError):
            validate_compatibility_manifest(value)

    def test_manifest_rejects_non_semver_artifact_version(self) -> None:
        artifact = {"name": "x", "version": "release", "sha256": SHA}
        value = {
            "schema_version": "1.0",
            "release": "1.0.0",
            "workflow_commit": "c" * 40,
            "compatible_worker_range": ">=1.0.0",
            "provenance": "github-artifact-attestation",
            "artifacts": {
                "workflow": artifact,
                "python": artifact,
                "npm": [artifact],
                "schemas_version": "1.0",
                "worker": artifact,
            },
        }
        with self.assertRaises(ReviewInputError):
            validate_compatibility_manifest(value)

    def test_worker_version_parser_rejects_wildcards_and_partial_versions(self) -> None:
        for version in ("x", "1.x", "1.2"):
            with self.assertRaises(ValueError):
                _version_parts(version)

    def test_manifest_rejects_mismatched_release_versions(self) -> None:
        artifact = {"name": "x", "version": "1.0.0", "sha256": SHA}
        value = {
            "schema_version": "1.0",
            "release": "1.0.0",
            "workflow_commit": "c" * 40,
            "compatible_worker_range": ">=1",
            "provenance": "github-artifact-attestation",
            "artifacts": {
                "workflow": artifact,
                "python": artifact,
                "npm": [artifact],
                "schemas_version": "1.0",
                "worker": {**artifact, "version": "2.0.0"},
            },
        }
        with self.assertRaises(ReviewInputError):
            validate_compatibility_manifest(value)

    def test_manifest_rejects_range_that_excludes_worker(self) -> None:
        artifact = {"name": "x", "version": "1.0.0", "sha256": SHA}
        value = {
            "schema_version": "1.0",
            "release": "1.0.0",
            "workflow_commit": "c" * 40,
            "compatible_worker_range": ">=2.0.0",
            "provenance": "github-artifact-attestation",
            "artifacts": {
                "workflow": artifact,
                "python": artifact,
                "npm": [artifact],
                "schemas_version": "1.0",
                "worker": artifact,
            },
        }
        with self.assertRaises(ReviewInputError):
            validate_compatibility_manifest(value)

    def test_manifest_rejects_malformed_range(self) -> None:
        artifact = {"name": "x", "version": "1.0.0", "sha256": SHA}
        value = {
            "schema_version": "1.0",
            "release": "1.0.0",
            "workflow_commit": "c" * 40,
            "compatible_worker_range": "not-a-range",
            "provenance": "github-artifact-attestation",
            "artifacts": {
                "workflow": artifact,
                "python": artifact,
                "npm": [artifact],
                "schemas_version": "1.0",
                "worker": artifact,
            },
        }
        with self.assertRaises(ReviewInputError):
            validate_compatibility_manifest(value)


def _make_report(
    *,
    mode: str = "live",
    provider: str = "ollama",
    model: str = "qwen3.5:4b",
    passed: bool = True,
    elapsed_total_ms: int = 1,
    provider_version: str | None = "local-ollama-1",
    engine: str | None = None,
    prompt: str | None = None,
    configuration: str | None = None,
    corpus: str | None = None,
    endpoint_scope: str = "loopback",
    invocation_id: str | None = None,
) -> dict:
    prompt_value = prompt if prompt is not None else prompt_digest()
    failures = [] if passed else ["actionable_precision_minimum"]
    return {
        "schema_version": "1.0",
        "corpus": {
            "id": "review-sensei-synthetic-v1",
            "version": "1.0",
            "sha256": corpus if corpus is not None else "d" * 64,
        },
        "run": {
            "mode": mode,
            "review_sensei_version": "0.1.1",
            "provider": provider,
            "provider_version": provider_version,
            "model": model,
            "endpoint_scope": endpoint_scope,
            "invocation_id": invocation_id or f"run-{elapsed_total_ms}",
            "engine_digest": engine if engine is not None else engine_digest(),
            "prompt_digest": prompt_value,
            "package_stage_digest": prompt_value,
            "configuration_digest": (
                configuration if configuration is not None else "c" * 64
            ),
        },
        "configuration": {"review_configuration_id": "packaged-defaults-v1"},
        "privacy": {"status": "clean", "scanned_inventory_count": 1},
        "cases": [
            {
                "id": "example",
                "kind": "quality",
                "category": "correctness",
                "status": "passed" if passed else "failed",
                "expected_matches": 1,
                "actual_matches": 1 if passed else 0,
                "false_positives": 0 if passed else 1,
                "location_valid": True,
                "category_valid": True,
                "elapsed_ms": elapsed_total_ms,
                "provider_calls": 1,
                "prompt_bytes": 0,
                "response_bytes": 0,
            }
        ],
        "metrics": {
            "provider_calls": 1,
            "prompt_bytes": 0,
            "response_bytes": 0,
            "token_proxy_4_bytes": 0,
            "elapsed_total_ms": elapsed_total_ms,
            "elapsed_mean_ms": float(elapsed_total_ms),
            "elapsed_p95_ms": float(elapsed_total_ms),
        },
        "deterministic": {
            "exact_fixture_result_rate": 1.0 if passed else 0.0,
            "expected_contract_rejection_rate": 1.0,
        },
        "quality": {
            "actionable_precision": 1.0 if passed else 0.0,
            "false_positive_rate": 0.0 if passed else 1.0,
            "expected_finding_recall": 1.0 if passed else 0.0,
            "location_validity": 1.0,
            "category_coverage": 1.0,
        },
        "threshold_failures": failures,
        "passed": passed,
    }


class PromotionEvidenceBindingTests(unittest.TestCase):
    def test_incomplete_records_fail_closed(self) -> None:
        complete = {
            "schema_version": "1.0",
            "engine_digest": SHA,
            "prompt_digest": SHA,
            "configuration_digest": SHA,
            "corpus_digest": SHA,
            "provider": "ollama",
            "model": "model",
            "observed_revision": "r1",
            "run_count": 3,
            "evaluated_at": "2026-01-01",
            "reproducibility": {"seed": "fixed"},
            "status": "supported",
            "rollback_decision": "revert-to-baseline",
        }
        for missing in ("rollback_decision", "status", "reproducibility"):
            with self.subTest(missing=missing):
                value = dict(complete)
                value.pop(missing)
                with self.assertRaises(ReviewInputError):
                    validate_promotion_record(value)
        with self.assertRaises(ReviewInputError):
            PromotionRecord(
                SHA,
                SHA,
                SHA,
                SHA,
                "ollama",
                "model",
                "r1",
                2,
                "2026-01-01",
                {"seed": "fixed"},
                status="supported",
            )
        with self.assertRaises(ReviewInputError):
            PromotionRecord(
                "not-a-digest",
                SHA,
                SHA,
                SHA,
                "ollama",
                "model",
                "r1",
                3,
                "2026-01-01",
                {"seed": "fixed"},
            )

    def test_three_live_reports_mint_supported_promotion(self) -> None:
        reports = [_make_report(elapsed_total_ms=index) for index in (1, 2, 3)]
        record = promotion_record_from_reports(
            reports,
            observed_revision="local-ollama-1",
            reproducibility={"seed": "fixed", "temperature": 0},
            evaluated_at="2026-09-16T00:00:00Z",
        )
        self.assertEqual(record.status, "supported")
        self.assertEqual(record.run_count, 3)
        self.assertEqual(record.engine_digest, engine_digest())
        self.assertEqual(record.prompt_digest, prompt_digest())
        self.assertEqual(record.provider, "ollama")
        self.assertEqual(record.model, "qwen3.5:4b")
        for report in reports:
            validate_promotion_against_report(record, report)
        self.assertEqual(
            require_supported_promotion(record, reports).status, "supported"
        )

    def test_require_supported_promotion_reads_digests_from_reports(self) -> None:
        digest = "e" * 64
        prompt = "f" * 64
        reports = [
            _make_report(elapsed_total_ms=index, engine=digest, prompt=prompt)
            for index in (1, 2, 3)
        ]
        record = promotion_record_from_reports(
            reports,
            observed_revision="local-ollama-1",
            reproducibility={"seed": "fixed"},
            evaluated_at="2026-01-01T00:00:00Z",
        )
        self.assertEqual(record.engine_digest, digest)
        self.assertNotEqual(record.engine_digest, engine_digest())
        self.assertEqual(
            require_supported_promotion(record, reports).engine_digest, digest
        )

    def test_constructed_fixture_reports_cannot_mint_supported(self) -> None:
        reports = [
            _make_report(
                mode="fixture",
                provider="fixture",
                model="fixture-v1",
                provider_version=None,
                endpoint_scope="none",
                elapsed_total_ms=index,
            )
            for index in (1, 2, 3)
        ]
        record = promotion_record_from_reports(
            reports,
            observed_revision="fixture-v1",
            reproducibility={"seed": "fixed"},
            evaluated_at="2026-01-01T00:00:00Z",
        )
        self.assertEqual(record.status, "insufficient")
        with self.assertRaises(ReviewInputError):
            promotion_record_from_reports(
                reports,
                observed_revision="fixture-v1",
                reproducibility={"seed": "fixed"},
                evaluated_at="2026-01-01T00:00:00Z",
                status="supported",
            )
        with self.assertRaises(ReviewInputError):
            require_supported_promotion(record, reports)

    def test_failing_live_reports_are_unsupported(self) -> None:
        reports = [
            _make_report(passed=False, elapsed_total_ms=index) for index in (1, 2, 3)
        ]
        record = promotion_record_from_reports(
            reports,
            observed_revision="local-ollama-1",
            reproducibility={"seed": "fixed"},
            evaluated_at="2026-01-01T00:00:00Z",
        )
        self.assertEqual(record.status, "unsupported")
        with self.assertRaises(ReviewInputError):
            promotion_record_from_reports(
                reports,
                observed_revision="local-ollama-1",
                reproducibility={"seed": "fixed"},
                evaluated_at="2026-01-01T00:00:00Z",
                status="supported",
            )

    def test_duplicate_reports_are_not_independent(self) -> None:
        report = _make_report(elapsed_total_ms=4)
        with self.assertRaises(ReviewInputError):
            promotion_record_from_reports(
                (report, report, report),
                observed_revision="local-ollama-1",
                reproducibility={"seed": "fixed"},
                evaluated_at="2026-01-01T00:00:00Z",
            )

    def test_digest_or_identity_mismatch_fails_closed(self) -> None:
        reports = [_make_report(elapsed_total_ms=index) for index in (1, 2, 3)]
        record = promotion_record_from_reports(
            reports,
            observed_revision="local-ollama-1",
            reproducibility={"seed": "fixed"},
            evaluated_at="2026-01-01T00:00:00Z",
        )
        tampered = json.loads(json.dumps(reports[0]))
        tampered["run"]["engine_digest"] = "e" * 64
        with self.assertRaises(ReviewInputError):
            validate_promotion_against_report(record, tampered)
        other = _make_report(elapsed_total_ms=9, configuration="f" * 64)
        with self.assertRaises(ReviewInputError):
            promotion_record_from_reports(
                [*reports[:2], other],
                observed_revision="local-ollama-1",
                reproducibility={"seed": "fixed"},
                evaluated_at="2026-01-01T00:00:00Z",
            )
        with self.assertRaises(ReviewInputError):
            require_supported_promotion(record, reports[:2])

    def test_two_live_reports_are_insufficient(self) -> None:
        reports = [_make_report(elapsed_total_ms=index) for index in (1, 2)]
        record = promotion_record_from_reports(
            reports,
            observed_revision="local-ollama-1",
            reproducibility={"seed": "fixed"},
            evaluated_at="2026-01-01T00:00:00Z",
        )
        self.assertEqual(record.status, "insufficient")
        self.assertEqual(record.run_count, 2)

    def test_missing_report_engine_digest_fails_closed(self) -> None:
        report = _make_report(elapsed_total_ms=1)
        report["run"].pop("engine_digest")
        with self.assertRaises(ReviewInputError):
            promotion_record_from_reports(
                [report],
                observed_revision="local-ollama-1",
                reproducibility={"seed": "fixed"},
                evaluated_at="2026-01-01T00:00:00Z",
            )

    def test_elapsed_only_copies_are_not_independent(self) -> None:
        reports = [
            _make_report(elapsed_total_ms=index, invocation_id="same-run")
            for index in (1, 2, 3)
        ]
        with self.assertRaises(ReviewInputError):
            promotion_record_from_reports(
                reports,
                observed_revision="local-ollama-1",
                reproducibility={"seed": "fixed"},
                evaluated_at="2026-01-01T00:00:00Z",
            )

    def test_distinct_invocation_ids_are_independent_with_identical_timing(
        self,
    ) -> None:
        reports = [
            _make_report(elapsed_total_ms=7, invocation_id=f"live-{index}")
            for index in (1, 2, 3)
        ]
        record = promotion_record_from_reports(
            reports,
            observed_revision="local-ollama-1",
            reproducibility={"seed": "fixed"},
            evaluated_at="2026-01-01T00:00:00Z",
        )
        self.assertEqual(record.status, "supported")
        self.assertEqual(
            require_supported_promotion(record, reports).status, "supported"
        )

    def test_missing_invocation_id_fails_closed(self) -> None:
        report = _make_report(elapsed_total_ms=1)
        report["run"].pop("invocation_id")
        with self.assertRaises(ReviewInputError):
            promotion_record_from_reports(
                [report],
                observed_revision="local-ollama-1",
                reproducibility={"seed": "fixed"},
                evaluated_at="2026-01-01T00:00:00Z",
            )

    def test_explicit_status_must_match_inferred_evidence(self) -> None:
        reports = [_make_report(elapsed_total_ms=index) for index in (1, 2, 3)]
        with self.assertRaises(ReviewInputError):
            promotion_record_from_reports(
                reports,
                observed_revision="local-ollama-1",
                reproducibility={"seed": "fixed"},
                evaluated_at="2026-01-01T00:00:00Z",
                status="insufficient",
            )

    def test_observed_revision_is_checked_for_non_supported_records(self) -> None:
        reports = [_make_report(elapsed_total_ms=index) for index in (1, 2)]
        record = promotion_record_from_reports(
            reports,
            observed_revision="local-ollama-1",
            reproducibility={"seed": "fixed"},
            evaluated_at="2026-01-01T00:00:00Z",
        )
        self.assertEqual(record.status, "insufficient")
        mismatched = PromotionRecord(
            engine_digest=record.engine_digest,
            prompt_digest=record.prompt_digest,
            configuration_digest=record.configuration_digest,
            corpus_digest=record.corpus_digest,
            provider=record.provider,
            model=record.model,
            observed_revision="other-revision",
            run_count=record.run_count,
            evaluated_at=record.evaluated_at,
            reproducibility=record.reproducibility,
            status=record.status,
            rollback_decision=record.rollback_decision,
        )
        with self.assertRaises(ReviewInputError):
            validate_promotion_against_report(mismatched, reports[0])

    def test_operator_script_emits_and_validates_without_live_flags(self) -> None:
        script = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "validate_promotion_record.py"
        )
        spec = importlib.util.spec_from_file_location(
            "validate_promotion_record_script", script
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            report_paths = []
            for index in (1, 2, 3):
                path = root / f"live-{index}.json"
                path.write_text(
                    json.dumps(_make_report(elapsed_total_ms=index)), encoding="utf-8"
                )
                report_paths.append(path)
            output = root / "promotion.json"
            self.assertEqual(
                module.main(
                    [
                        "emit",
                        "--report",
                        str(report_paths[0]),
                        "--report",
                        str(report_paths[1]),
                        "--report",
                        str(report_paths[2]),
                        "--observed-revision",
                        "local-ollama-1",
                        "--evaluated-at",
                        "2026-09-16T00:00:00Z",
                        "--reproducibility-json",
                        '{"seed":"fixed"}',
                        "--output",
                        str(output),
                    ]
                ),
                0,
            )
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8"))["status"], "supported"
            )
            self.assertEqual(
                module.main(
                    [
                        "validate",
                        "--require-supported",
                        "--record",
                        str(output),
                        "--report",
                        str(report_paths[0]),
                        "--report",
                        str(report_paths[1]),
                        "--report",
                        str(report_paths[2]),
                    ]
                ),
                0,
            )

    def test_operator_script_emits_from_reproducibility_file(self) -> None:
        script = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "validate_promotion_record.py"
        )
        spec = importlib.util.spec_from_file_location(
            "validate_promotion_record_script_file", script
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            report_paths = []
            for index in (1, 2, 3):
                path = root / f"live-{index}.json"
                path.write_text(
                    json.dumps(_make_report(elapsed_total_ms=index)), encoding="utf-8"
                )
                report_paths.append(path)
            settings = root / "reproducibility.json"
            settings.write_text('{"seed":"fixed","temperature":0}\n', encoding="utf-8")
            output = root / "promotion.json"
            self.assertEqual(
                module.main(
                    [
                        "emit",
                        "--report",
                        str(report_paths[0]),
                        "--report",
                        str(report_paths[1]),
                        "--report",
                        str(report_paths[2]),
                        "--observed-revision",
                        "local-ollama-1",
                        "--evaluated-at",
                        "2026-09-16T00:00:00Z",
                        "--reproducibility-file",
                        str(settings),
                        "--output",
                        str(output),
                    ]
                ),
                0,
            )
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8"))["reproducibility"],
                {"seed": "fixed", "temperature": 0},
            )


if __name__ == "__main__":
    unittest.main()
