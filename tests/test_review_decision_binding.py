"""Decision identity, extension registration and public receipt boundaries."""

import copy
import unittest
from dataclasses import replace

from test_review_decision_engine import (
    DecisionFixture,
    ReviewFixture,
    configuration,
    diff_for,
)

from review_sensei.budgets import ReviewWorkBudgets
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.decision_configuration import DecisionConfiguration
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewRequest, ReviewResult
from review_sensei.outcomes import ResourceBudget
from review_sensei.providers.decision import DecisionRegistry
from review_sensei.schemas import validate_public_document
from review_sensei.service import ReviewService, review_work_policy_digest
from review_sensei.stages import Stage
from review_sensei.verifier import prepare_publishable_review


class ReviewDecisionBindingTests(unittest.TestCase):
    def run_review(self):
        return ReviewService(
            ReviewFixture(),
            stages=(Stage("review", "Review {diff}", ("summary", "comments")),),
            decision_configuration=configuration(),
            decision_provider=DecisionFixture(),
        ).run(ReviewRequest(diff=diff_for("src/app.py")))

    def test_legacy_and_unified_transaction_policy_bind_decision_settings(self):
        for mode in ("legacy", "unified"):
            work = ReviewWorkBudgets(mode=mode)
            budget = ResourceBudget.create()
            disabled = review_work_policy_digest(work, budget)
            first = review_work_policy_digest(work, budget, decision=configuration())
            second = review_work_policy_digest(
                work, budget, decision=configuration(model="other-model")
            )
            self.assertNotEqual(disabled, first)
            self.assertNotEqual(first, second)
            service = ReviewService(
                ReviewFixture(),
                work_budgets=work,
                budget=budget,
                decision_configuration=configuration(),
                decision_provider=DecisionFixture(),
            )
            self.assertEqual(service.work_policy_digest, first)

    def test_public_schema_roundtrip_and_receipt_changes_bind_content(self):
        result = self.run_review().result
        document = result.to_dict()
        validate_public_document(document, "review-result")
        self.assertEqual(
            ReviewResult.from_dict(document).content_digest(), result.content_digest()
        )
        changed = copy.deepcopy(document)
        changed["decision_pass"]["elapsed_ms"] += 1
        self.assertNotEqual(
            ReviewResult.from_dict(changed).content_digest(), result.content_digest()
        )
        changed["decision_pass"]["candidates"][0]["action"] = "delete"
        with self.assertRaises(ReviewInputError):
            ReviewResult.from_dict(changed)

    def test_publication_keeps_decision_provenance_and_original_blocker(self):
        original = self.run_review().result
        prepared = prepare_publishable_review(
            original, convergence_policy=ReviewConvergencePolicy(mode="legacy")
        ).result
        self.assertEqual(prepared.decision_pass, original.decision_pass)
        self.assertEqual(prepared.comments, original.comments)
        self.assertTrue(prepared.comments[0].blocks_approval)

    def test_receipt_construction_owns_nested_input(self):
        original = self.run_review().result
        for route in ("constructor", "from_dict"):
            with self.subTest(route=route):
                document = copy.deepcopy(original.to_dict())
                receipt = document["decision_pass"]
                result = (
                    replace(original, decision_pass=receipt)
                    if route == "constructor"
                    else ReviewResult.from_dict(document)
                )
                expected = copy.deepcopy(result.to_dict())
                digest = result.content_digest()
                receipt["candidates"][0]["action"] = "delete"
                receipt["models"][0]["actual_model"] = "mutated-model"
                self.assertEqual(result.to_dict(), expected)
                self.assertEqual(result.content_digest(), digest)
                validate_public_document(result.to_dict(), "review-result")

    def test_receipt_serialization_owns_nested_output(self):
        result = self.run_review().result
        expected = copy.deepcopy(result.to_dict())
        digest = result.content_digest()
        document = result.to_dict()
        document["decision_pass"]["candidates"][0]["action"] = "delete"
        document["decision_pass"]["models"][0]["actual_model"] = "mutated-model"
        self.assertEqual(result.to_dict(), expected)
        self.assertEqual(result.content_digest(), digest)
        validate_public_document(result.to_dict(), "review-result")

    def test_registered_extension_uses_same_policy_without_builtin_changes(self):
        settings = DecisionConfiguration(
            enabled=True,
            provider="extension",
            model="decision-v1",
            base_url="http://127.0.0.1:1234/choice",
        )
        registry = DecisionRegistry()
        provider = DecisionFixture()
        provider.name = "extension"
        registry.register("extension", lambda selected, env: provider)
        service = ReviewService(
            ReviewFixture(),
            stages=(Stage("review", "Review {diff}", ("summary", "comments")),),
            decision_configuration=settings,
            decision_provider=registry.create(settings, {}),
        )
        run = service.run(ReviewRequest(diff=diff_for("src/app.py")))
        self.assertEqual(run.result.decision_pass["status"], "complete")
        self.assertEqual(run.outcome.provider_calls, 2)
        with self.assertRaises(ValueError):
            registry.register("extension", lambda selected, env: provider)

    def test_incomplete_receipt_cannot_be_reconstructed_as_complete(self):
        result = self.run_review().result
        receipt = copy.deepcopy(result.decision_pass)
        receipt["status"] = "incomplete"
        with self.assertRaises(ReviewInputError):
            replace(result, decision_pass=receipt)
