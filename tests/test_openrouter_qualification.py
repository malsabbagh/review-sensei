"""OpenRouter qualification harness negative-case tests.

These tests prove promotion gates reject fixture-only or incomplete OpenRouter
evidence. They do not contact live providers or fabricate operator evidence.
"""

from __future__ import annotations

import unittest

from review_sensei.errors import ReviewInputError
from review_sensei.evaluation import (
    engine_digest,
    promotion_record_from_reports,
    prompt_digest,
    require_supported_promotion,
)
from review_sensei.openrouter_qualification import (
    INITIAL_OPENROUTER_QUALIFICATION_TARGET,
    OpenRouterQualificationRecord,
    OpenRouterQualificationTarget,
    qualification_record_from_reports,
    require_supported_openrouter_qualification,
    routing_policy_digest,
    validate_openrouter_qualification_against_report,
    validate_openrouter_qualification_record,
)
from review_sensei.providers.openrouter import OpenRouterRoutingPolicy
from review_sensei.schemas import validate_public_document

SHA = "a" * 64
TARGET = INITIAL_OPENROUTER_QUALIFICATION_TARGET
POLICY_DIGEST = routing_policy_digest(TARGET.routing_policy())
REPRO = {"temperature": 0}
EVALUATED_AT = "2026-09-16T00:00:00Z"


def _make_openrouter_report(
    *,
    mode: str = "live",
    model: str = TARGET.model,
    passed: bool = True,
    elapsed_total_ms: int = 1,
    provider_version: str | None = "openrouter-rev-1",
    endpoint_scope: str = "remote",
    invocation_id: str | None = None,
    engine: str | None = None,
    prompt: str | None = None,
    configuration: str | None = None,
    corpus: str | None = None,
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
            "provider": "openrouter",
            "provider_version": provider_version,
            "model": model,
            "endpoint_scope": endpoint_scope,
            "invocation_id": invocation_id or f"or-run-{elapsed_total_ms}",
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


def _insufficient_harness_record() -> dict:
    return {
        "schema_version": "1.0",
        "engine_digest": SHA,
        "prompt_digest": SHA,
        "configuration_digest": SHA,
        "corpus_digest": SHA,
        "provider": "openrouter",
        "model": TARGET.model,
        "observed_revision": "pending-operator-live-evidence",
        "run_count": 1,
        "evaluated_at": EVALUATED_AT,
        "reproducibility": REPRO,
        "status": "insufficient",
        "rollback_decision": "hold",
        "qualification_target": TARGET.to_dict(),
        "evidence_references": [],
        "limitations": [
            "Harness-only record; live operator evidence is required before supported status."
        ],
    }


class OpenRouterQualificationHarnessTests(unittest.TestCase):
    def test_initial_target_uses_allowlisted_endpoint_and_routing_digest(self) -> None:
        policy = OpenRouterRoutingPolicy(upstream_provider=TARGET.upstream_provider)
        self.assertEqual(
            routing_policy_digest(policy),
            TARGET.routing_policy_digest(),
        )
        self.assertEqual(TARGET.provider, "openrouter")
        self.assertEqual(TARGET.model, "anthropic/claude-3.5-haiku")

    def test_harness_record_schema_validates_without_live_evidence(self) -> None:
        value = _insufficient_harness_record()
        validate_public_document(value, "openrouter-qualification")
        record = validate_openrouter_qualification_record(value)
        self.assertEqual(record.status, "insufficient")
        self.assertEqual(record.evidence_references, ())

    def test_fixture_openrouter_reports_cannot_mint_supported(self) -> None:
        reports = [
            _make_openrouter_report(
                mode="fixture",
                provider_version=None,
                endpoint_scope="none",
                elapsed_total_ms=index,
                invocation_id=f"fx-{index}",
            )
            for index in (1, 2, 3)
        ]
        record = qualification_record_from_reports(
            reports,
            target=TARGET,
            observed_revision="fixture-v1",
            reproducibility=REPRO,
            evaluated_at=EVALUATED_AT,
        )
        self.assertEqual(record.status, "insufficient")
        with self.assertRaises(ReviewInputError):
            qualification_record_from_reports(
                reports,
                target=TARGET,
                observed_revision="fixture-v1",
                reproducibility=REPRO,
                evaluated_at=EVALUATED_AT,
                status="supported",
            )
        with self.assertRaises(ReviewInputError):
            require_supported_openrouter_qualification(record, reports)

    def test_two_live_reports_are_insufficient(self) -> None:
        reports = [
            _make_openrouter_report(
                elapsed_total_ms=index, invocation_id=f"live-{index}"
            )
            for index in (1, 2)
        ]
        record = qualification_record_from_reports(
            reports,
            target=TARGET,
            observed_revision="openrouter-rev-1",
            reproducibility=REPRO,
            evaluated_at=EVALUATED_AT,
        )
        self.assertEqual(record.status, "insufficient")
        self.assertEqual(record.promotion.run_count, 2)
        with self.assertRaises(ReviewInputError):
            require_supported_openrouter_qualification(record, reports)

    def test_loopback_endpoint_scope_is_rejected(self) -> None:
        report = _make_openrouter_report(endpoint_scope="loopback")
        with self.assertRaisesRegex(ReviewInputError, "remote endpoint_scope"):
            qualification_record_from_reports(
                [report],
                target=TARGET,
                observed_revision="openrouter-rev-1",
                reproducibility=REPRO,
                evaluated_at=EVALUATED_AT,
            )

    def test_mismatched_model_is_rejected(self) -> None:
        report = _make_openrouter_report(model="openai/gpt-4o-mini")
        with self.assertRaisesRegex(ReviewInputError, "model does not match target"):
            qualification_record_from_reports(
                [report],
                target=TARGET,
                observed_revision="openrouter-rev-1",
                reproducibility=REPRO,
                evaluated_at=EVALUATED_AT,
            )

    def test_failing_live_reports_are_unsupported(self) -> None:
        reports = [
            _make_openrouter_report(
                passed=False, elapsed_total_ms=index, invocation_id=f"f-{index}"
            )
            for index in (1, 2, 3)
        ]
        record = qualification_record_from_reports(
            reports,
            target=TARGET,
            observed_revision="openrouter-rev-1",
            reproducibility=REPRO,
            evaluated_at=EVALUATED_AT,
        )
        self.assertEqual(record.status, "unsupported")
        with self.assertRaises(ReviewInputError):
            qualification_record_from_reports(
                reports,
                target=TARGET,
                observed_revision="openrouter-rev-1",
                reproducibility=REPRO,
                evaluated_at=EVALUATED_AT,
                status="supported",
            )

    def test_duplicate_invocation_ids_are_not_independent(self) -> None:
        reports = [
            _make_openrouter_report(
                elapsed_total_ms=index,
                invocation_id="same-openrouter-run",
            )
            for index in (1, 2, 3)
        ]
        with self.assertRaisesRegex(ReviewInputError, "independent"):
            qualification_record_from_reports(
                reports,
                target=TARGET,
                observed_revision="openrouter-rev-1",
                reproducibility=REPRO,
                evaluated_at=EVALUATED_AT,
            )

    def test_supported_status_requires_three_evidence_references_in_record(
        self,
    ) -> None:
        reports = [
            _make_openrouter_report(elapsed_total_ms=index, invocation_id=f"ok-{index}")
            for index in (1, 2, 3)
        ]
        promotion = promotion_record_from_reports(
            reports,
            observed_revision="openrouter-rev-1",
            reproducibility=REPRO,
            evaluated_at=EVALUATED_AT,
        )
        self.assertEqual(promotion.status, "supported")
        with self.assertRaisesRegex(ReviewInputError, "evidence references"):
            OpenRouterQualificationRecord(
                promotion=promotion,
                qualification_target=TARGET,
                evidence_references=(),
                limitations=("test limitation",),
            )

    def test_three_independent_live_reports_can_build_supported_record(self) -> None:
        reports = [
            _make_openrouter_report(
                elapsed_total_ms=index, invocation_id=f"live-{index}"
            )
            for index in (1, 2, 3)
        ]
        record = qualification_record_from_reports(
            reports,
            target=TARGET,
            observed_revision="openrouter-rev-1",
            reproducibility=REPRO,
            evaluated_at=EVALUATED_AT,
        )
        self.assertEqual(record.status, "supported")
        self.assertEqual(len(record.evidence_references), 3)
        validate_public_document(record.to_dict(), "openrouter-qualification")
        self.assertEqual(
            require_supported_openrouter_qualification(record, reports).status,
            "supported",
        )

    def test_generic_promotion_gate_also_rejects_fixture_openrouter_reports(
        self,
    ) -> None:
        reports = [
            _make_openrouter_report(
                mode="fixture",
                model="fixture-v1",
                provider_version=None,
                endpoint_scope="none",
                elapsed_total_ms=index,
                invocation_id=f"fx-{index}",
            )
            for index in (1, 2, 3)
        ]
        record = promotion_record_from_reports(
            reports,
            observed_revision="fixture-v1",
            reproducibility=REPRO,
            evaluated_at=EVALUATED_AT,
        )
        self.assertEqual(record.status, "insufficient")
        with self.assertRaises(ReviewInputError):
            require_supported_promotion(record, reports)

    def test_routing_policy_digest_mismatch_fails_validation(self) -> None:
        value = _insufficient_harness_record()
        value["qualification_target"]["routing_policy_digest"] = "f" * 64
        with self.assertRaisesRegex(ReviewInputError, "routing_policy_digest"):
            validate_openrouter_qualification_record(value)

    def test_validate_openrouter_qualification_against_report_checks_remote_scope(
        self,
    ) -> None:
        live = _make_openrouter_report(endpoint_scope="remote")
        loopback = _make_openrouter_report(
            endpoint_scope="loopback", invocation_id="loop-1"
        )
        record = qualification_record_from_reports(
            [live],
            target=TARGET,
            observed_revision="openrouter-rev-1",
            reproducibility=REPRO,
            evaluated_at=EVALUATED_AT,
        )
        validate_openrouter_qualification_against_report(record, live)
        with self.assertRaisesRegex(ReviewInputError, "remote endpoint_scope"):
            validate_openrouter_qualification_against_report(record, loopback)

    def test_non_allowlisted_base_url_is_rejected(self) -> None:
        with self.assertRaisesRegex(ReviewInputError, "allowlisted"):
            OpenRouterQualificationTarget(
                model=TARGET.model,
                upstream_provider=TARGET.upstream_provider,
                base_url="https://evil.example/api/v1",
            )


if __name__ == "__main__":
    unittest.main()
