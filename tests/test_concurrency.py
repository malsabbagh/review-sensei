import unittest

from review_sensei import ConcurrencyGroup as ExportedConcurrencyGroup
from review_sensei import ReviewConcurrencyPlan as ExportedReviewConcurrencyPlan
from review_sensei.concurrency import ConcurrencyGroup, ReviewConcurrencyPlan
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewRequest
from review_sensei.service import ReviewService


class FakeProvider:
    name = "fake"
    model = "fake-model"

    def complete(self, request):  # pragma: no cover - not used by these tests
        raise AssertionError("the provider should not be called")


class ReviewConcurrencyPlanTests(unittest.TestCase):
    def test_concurrency_types_are_exported_from_the_public_package(self):
        self.assertIs(ExportedConcurrencyGroup, ConcurrencyGroup)
        self.assertIs(ExportedReviewConcurrencyPlan, ReviewConcurrencyPlan)

    def test_same_pull_request_is_latest_wins_and_provider_serialized(self):
        plan = ReviewConcurrencyPlan.for_pull_request(
            "owner/repository",
            42,
            provider_name="ollama",
        )

        self.assertEqual(
            plan.workflow_key, "review-sensei:review:16:owner/repository:2:42"
        )
        self.assertEqual(
            plan.provider_key,
            "review-sensei:provider:6:ollama:16:owner/repository:2:42",
        )
        self.assertTrue(plan.workflow.cancel_in_progress)
        self.assertEqual(plan.workflow.max_active, 1)
        self.assertFalse(plan.provider.cancel_in_progress)
        self.assertEqual(plan.provider.max_active, 1)

    def test_plan_is_deterministic_and_serializes_both_scopes(self):
        first = ReviewConcurrencyPlan.for_pull_request(" owner/repository ", 42)
        second = ReviewConcurrencyPlan.for_pull_request("owner/repository", 42)

        self.assertEqual(first, second)
        self.assertEqual(
            first.to_dict(),
            {
                "workflow": {
                    "key": "review-sensei:review:16:owner/repository:2:42",
                    "max_active": 1,
                    "cancel_in_progress": True,
                },
                "provider": {
                    "key": "review-sensei:provider:6:ollama:16:owner/repository:2:42",
                    "max_active": 1,
                    "cancel_in_progress": False,
                },
            },
        )

    def test_different_pull_requests_do_not_share_provider_slots(self):
        first = ReviewConcurrencyPlan.for_pull_request("owner/repository", 41)
        second = ReviewConcurrencyPlan.for_pull_request("owner/repository", 42)
        other_repository = ReviewConcurrencyPlan.for_pull_request(
            "other/repository", 41
        )

        self.assertNotEqual(first.workflow_key, second.workflow_key)
        self.assertNotEqual(first.provider_key, second.provider_key)
        self.assertNotEqual(first.provider_key, other_repository.provider_key)

    def test_provider_name_only_changes_the_provider_scope(self):
        ollama = ReviewConcurrencyPlan.for_pull_request(
            "owner/repository",
            42,
            provider_name="ollama",
        )
        fake = ReviewConcurrencyPlan.for_pull_request(
            "owner/repository",
            42,
            provider_name="fake",
        )

        self.assertEqual(ollama.workflow_key, fake.workflow_key)
        self.assertNotEqual(ollama.provider_key, fake.provider_key)

    def test_non_review_trigger_has_an_isolated_workflow_and_no_provider_slot(self):
        plan = ReviewConcurrencyPlan.for_non_review_trigger("owner/repository", 9001)

        self.assertEqual(
            plan.workflow_key, "review-sensei:trigger:16:owner/repository:4:9001"
        )
        self.assertIsNone(plan.provider)
        self.assertIsNone(plan.provider_key)

    def test_non_review_triggers_use_distinct_workflow_groups(self):
        first = ReviewConcurrencyPlan.for_non_review_trigger(
            "owner/repository", "comment-1"
        )
        second = ReviewConcurrencyPlan.for_non_review_trigger(
            "owner/repository", "comment-2"
        )

        self.assertNotEqual(first.workflow_key, second.workflow_key)
        self.assertTrue(first.workflow.cancel_in_progress)
        self.assertEqual(first.workflow.to_dict()["max_active"], 1)

    def test_plan_from_request_requires_repository_and_pull_request_metadata(self):
        with self.assertRaisesRegex(ReviewInputError, "repository metadata"):
            ReviewConcurrencyPlan.for_request(ReviewRequest(diff="change"))

        with self.assertRaisesRegex(ReviewInputError, "pull request number"):
            ReviewConcurrencyPlan.for_request(
                ReviewRequest(diff="change", repository="owner/repository")
            )

    def test_plan_rejects_non_review_request_values(self):
        with self.assertRaisesRegex(ReviewInputError, "ReviewRequest"):
            ReviewConcurrencyPlan.for_request(object())

    def test_plan_rejects_unsafe_or_invalid_pull_request_inputs(self):
        invalid_values = (
            ("owner/repository\nother", 42, "repository"),
            ("owner/repository", 0, "pull_request_number"),
            ("owner/repository", True, "pull_request_number"),
            ("owner/repository", 42, "provider_name"),
        )
        for repository, number, label in invalid_values:
            with self.subTest(repository=repository, number=number, label=label):
                if label == "provider_name":
                    with self.assertRaises(ReviewInputError):
                        ReviewConcurrencyPlan.for_pull_request(
                            repository,
                            number,
                            provider_name="ollama\nother",
                        )
                else:
                    with self.assertRaisesRegex(ReviewInputError, label):
                        ReviewConcurrencyPlan.for_pull_request(repository, number)

    def test_plan_rejects_unsafe_or_invalid_trigger_inputs(self):
        for trigger_id in (0, True, "", "comment id", None):
            with (
                self.subTest(trigger_id=trigger_id),
                self.assertRaises(ReviewInputError),
            ):
                ReviewConcurrencyPlan.for_non_review_trigger(
                    "owner/repository", trigger_id
                )

    def test_concurrency_types_reject_invalid_shapes_and_control_characters(self):
        with self.assertRaises(ReviewInputError):
            ConcurrencyGroup(key="review\nrun")
        with self.assertRaises(ReviewInputError):
            ReviewConcurrencyPlan(workflow="review")
        with self.assertRaises(ReviewInputError):
            ReviewConcurrencyPlan(
                workflow=ConcurrencyGroup(key="review"),
                provider="provider",
            )

    def test_service_exposes_the_provider_specific_plan(self):
        request = ReviewRequest(
            diff="change",
            repository="owner/repository",
            pull_request_number=7,
        )

        plan = ReviewService(FakeProvider()).concurrency_plan(request)

        self.assertEqual(
            plan.provider_key, "review-sensei:provider:4:fake:16:owner/repository:1:7"
        )

    def test_concurrency_group_rejects_invalid_admission_rules(self):
        with self.assertRaises(ReviewInputError):
            ConcurrencyGroup(key="review", max_active=0)
        with self.assertRaises(ReviewInputError):
            ConcurrencyGroup(key="review", cancel_in_progress="yes")

    def test_cross_namespace_collisions_are_prevented(self):
        # Case 1: repository = "owner/a-trigger-b", pull_request_number = 42
        # Workflow key = review-sensei:review:17:owner/a-trigger-b:2:42
        review_plan = ReviewConcurrencyPlan.for_pull_request("owner/a-trigger-b", 42)

        # Case 2: repository = "owner/a", trigger_id = "b-pr-42"
        # Workflow key = review-sensei:trigger:7:owner/a:7:b-pr-42
        trigger_plan = ReviewConcurrencyPlan.for_non_review_trigger(
            "owner/a", "b-pr-42"
        )

        self.assertNotEqual(review_plan.workflow_key, trigger_plan.workflow_key)

    def test_numeric_bounds(self):
        # Huge integer triggers ValueError or creates oversized keys. It must raise ReviewInputError now.
        with self.assertRaises(ReviewInputError):
            ReviewConcurrencyPlan.for_pull_request("owner/repository", 10**4300)

        with self.assertRaises(ReviewInputError):
            ReviewConcurrencyPlan.for_pull_request("owner/repository", 10**10)

        with self.assertRaises(ReviewInputError):
            ReviewConcurrencyPlan.for_non_review_trigger("owner/repository", 10**10)
