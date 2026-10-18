"""Synthetic retained-report routing regressions; no live support claims."""

import copy
import unittest

from review_sensei.errors import ReviewInputError
from review_sensei.openrouter_qualification import (
    CURRENT_HOSTED_OPENROUTER_QUALIFICATION_TARGET,
    INITIAL_OPENROUTER_QUALIFICATION_TARGET,
    evidence_reference_digest,
    load_supported_openrouter_qualification,
    qualification_record_from_reports,
    routing_policy_digest,
)
from review_sensei.providers.openrouter import OpenRouterRoutingPolicy
from review_sensei.schemas import validate_public_document
from tests.test_openrouter_qualification import (
    EVALUATED_AT,
    REPRO,
    _make_openrouter_report,
    _retained_artifacts,
)


class QualificationRoutingEvidenceTests(unittest.TestCase):
    def reports(self, target):
        reports = [
            _make_openrouter_report(
                model=target.model, invocation_id=f"synthetic-routing-{index}"
            )
            for index in range(3)
        ]
        for report in reports:
            report["run"]["openrouter_routing_policy_digest"] = (
                target.routing_policy_digest()
            )
            report["run"]["openrouter_base_url"] = target.base_url
        return reports

    def mint(self, reports, target):
        return qualification_record_from_reports(
            reports,
            target=target,
            observed_revision="openrouter-rev-1",
            reproducibility=REPRO,
            evaluated_at=EVALUATED_AT,
            report_artifacts=_retained_artifacts(reports),
            status="supported",
        )

    def test_matching_retained_routing_qualifies_only_the_declared_target(self):
        for target in (
            INITIAL_OPENROUTER_QUALIFICATION_TARGET,
            CURRENT_HOSTED_OPENROUTER_QUALIFICATION_TARGET,
        ):
            with self.subTest(model=target.model):
                reports = self.reports(target)
                published = self.mint(reports, target).to_dict()
                checked = load_supported_openrouter_qualification(
                    published,
                    reports,
                    report_artifacts=_retained_artifacts(reports),
                    expected_target=target,
                    expected_evaluated_at=EVALUATED_AT,
                    expected_reproducibility=REPRO,
                )
                self.assertEqual(checked.status, "supported")
                self.assertEqual(checked.qualification_target, target)

    def test_unbound_or_mismatched_routing_cannot_mint_supported(self):
        target = CURRENT_HOSTED_OPENROUTER_QUALIFICATION_TARGET
        for key, value in (
            ("openrouter_routing_policy_digest", None),
            ("openrouter_base_url", None),
            ("openrouter_routing_policy_digest", "f" * 64),
            (
                "openrouter_routing_policy_digest",
                routing_policy_digest(OpenRouterRoutingPolicy("morph/variant")),
            ),
            (
                "openrouter_routing_policy_digest",
                routing_policy_digest(OpenRouterRoutingPolicy("deepseek")),
            ),
            ("openrouter_base_url", target.base_url + "/variant"),
            ("openrouter_base_url", "https://openrouter.ai/api/v1/chat/completions"),
        ):
            with self.subTest(key=key, value=value):
                reports = self.reports(target)
                for report in reports:
                    if value is None:
                        del report["run"][key]
                    else:
                        report["run"][key] = value
                with self.assertRaisesRegex(ReviewInputError, "routing|base URL"):
                    self.mint(reports, target)

    def test_loader_rejects_rebound_suffix_artifacts_even_with_matching_references(
        self,
    ):
        target = CURRENT_HOSTED_OPENROUTER_QUALIFICATION_TARGET
        reports = self.reports(target)
        document = self.mint(reports, target).to_dict()
        for report in reports:
            report["run"]["openrouter_routing_policy_digest"] = routing_policy_digest(
                OpenRouterRoutingPolicy("morph/variant")
            )
        artifacts = _retained_artifacts(reports)
        document["evidence_references"] = [
            evidence_reference_digest(artifact) for artifact in artifacts
        ]
        with self.assertRaisesRegex(ReviewInputError, "routing"):
            load_supported_openrouter_qualification(
                document,
                reports,
                report_artifacts=artifacts,
                expected_target=target,
                expected_evaluated_at=EVALUATED_AT,
                expected_reproducibility=REPRO,
            )

    def test_legacy_reports_validate_but_cannot_newly_authorize_unbound_routing(self):
        target = INITIAL_OPENROUTER_QUALIFICATION_TARGET
        reports = self.reports(target)
        legacy = copy.deepcopy(reports)
        for report in legacy:
            del report["run"]["openrouter_routing_policy_digest"]
            del report["run"]["openrouter_base_url"]
            validate_public_document(report, "evaluation-report")
        with self.assertRaisesRegex(ReviewInputError, "routing"):
            self.mint(legacy, target)


if __name__ == "__main__":
    unittest.main()
