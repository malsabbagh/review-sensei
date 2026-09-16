import threading
import time
import unittest

from review_sensei import ConcurrencyGroup as ExportedConcurrencyGroup
from review_sensei import ReviewConcurrencyPlan as ExportedReviewConcurrencyPlan
from review_sensei.concurrency import (
    ConcurrencyGroup,
    ProviderAdmission,
    ReviewConcurrencyPlan,
)
from review_sensei.errors import AdmissionCancelled, AdmissionRejected, ReviewInputError
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

    def test_python_review_keys_are_pr_scoped_sha_free_and_latest_wins(self):
        first_sha = "a" * 40
        second_sha = "b" * 40
        plan = ReviewConcurrencyPlan.for_pull_request("acme/api", 7)

        self.assertEqual(plan.workflow_key, "review-sensei:review:8:acme/api:1:7")
        self.assertTrue(plan.workflow.cancel_in_progress)
        self.assertEqual(plan.workflow.max_active, 1)
        self.assertNotIn(first_sha, plan.workflow_key)
        self.assertNotIn(second_sha, plan.workflow_key)
        self.assertNotIn("head_sha", plan.workflow_key)
        self.assertNotIn(first_sha, plan.provider_key or "")
        same_pr_later_head = ReviewConcurrencyPlan.for_pull_request("acme/api", 7)
        self.assertEqual(plan.workflow_key, same_pr_later_head.workflow_key)


class ProviderAdmissionTests(unittest.TestCase):
    def test_admission_types_are_exported_from_the_public_package(self):
        from review_sensei import AdmissionCancelled as ExportedCancelled
        from review_sensei import AdmissionLease as ExportedLease
        from review_sensei import AdmissionOutcome as ExportedOutcome
        from review_sensei import AdmissionRejected as ExportedRejected
        from review_sensei import ProviderAdmission as ExportedAdmission
        from review_sensei.concurrency import (
            AdmissionLease,
            AdmissionOutcome,
            ProviderAdmission,
        )
        from review_sensei.errors import AdmissionCancelled, AdmissionRejected

        self.assertIs(ExportedAdmission, ProviderAdmission)
        self.assertIs(ExportedLease, AdmissionLease)
        self.assertIs(ExportedOutcome, AdmissionOutcome)
        self.assertIs(ExportedRejected, AdmissionRejected)
        self.assertIs(ExportedCancelled, AdmissionCancelled)

    def test_configured_bound_is_enforced(self):
        group = ConcurrencyGroup(key="review-sensei:provider:4:test:8:acme/api:1:7")
        admission = ProviderAdmission(group, max_waiters=0)

        first = admission.try_acquire()
        second = admission.try_acquire()
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(admission.active, 1)
        self.assertEqual(admission.waiters, 0)
        with self.assertRaises(AdmissionRejected):
            admission.acquire()
        self.assertEqual(admission.waiters, 0)
        assert first is not None
        first.release()
        self.assertEqual(admission.active, 0)

    def test_concurrent_release_is_idempotent(self):
        group = ConcurrencyGroup(key="review-sensei:provider:4:test:8:acme/api:1:12")
        admission = ProviderAdmission(group, max_waiters=0)
        lease = admission.acquire()
        errors: list[BaseException] = []

        def release_once() -> None:
            try:
                lease.release()
            except BaseException as exc:  # pragma: no cover - test diagnostics
                errors.append(exc)

        threads = [threading.Thread(target=release_once) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(admission.active, 0)
        recovered = admission.try_acquire()
        self.assertIsNotNone(recovered)
        assert recovered is not None
        recovered.release()
        self.assertEqual(admission.active, 0)

    def test_context_manager_release_after_manual_release_is_idempotent(self):
        group = ConcurrencyGroup(key="review-sensei:provider:4:test:8:acme/api:1:13")
        admission = ProviderAdmission(group, max_waiters=0)
        with admission.acquire() as lease:
            self.assertEqual(admission.active, 1)
            lease.release()
            self.assertEqual(admission.active, 0)
        self.assertEqual(admission.active, 0)
        recovered = admission.try_acquire()
        self.assertIsNotNone(recovered)
        assert recovered is not None
        recovered.release()

    def test_cancelled_and_failed_work_release_the_slot(self):
        group = ConcurrencyGroup(key="review-sensei:provider:4:test:8:acme/api:1:8")
        admission = ProviderAdmission(group, max_waiters=1)

        with self.assertRaises(RuntimeError):
            with admission.acquire():
                self.assertEqual(admission.active, 1)
                raise RuntimeError("boom")
        self.assertEqual(admission.active, 0)

        with self.assertRaises(KeyboardInterrupt):
            with admission.acquire():
                raise KeyboardInterrupt()
        self.assertEqual(admission.active, 0)

        retry = admission.try_acquire()
        self.assertIsNotNone(retry)
        assert retry is not None
        retry.release(outcome="cancelled")
        self.assertEqual(admission.active, 0)

    def test_waiters_cannot_accumulate_without_bound(self):
        group = ConcurrencyGroup(key="review-sensei:provider:4:test:8:acme/api:1:9")
        admission = ProviderAdmission(group, max_waiters=1)
        held = admission.acquire()
        cancel = threading.Event()
        errors: list[BaseException] = []

        def wait_for_slot() -> None:
            try:
                admission.acquire(timeout=2, cancel_event=cancel)
                errors.append(AssertionError("waiter was admitted"))
            except AdmissionCancelled:
                return
            except BaseException as exc:  # pragma: no cover - test diagnostics
                errors.append(exc)

        waiter = threading.Thread(target=wait_for_slot)
        waiter.start()
        deadline = time.monotonic() + 2
        while admission.waiters != 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(admission.waiters, 1)
        with self.assertRaises(AdmissionRejected):
            admission.acquire(timeout=0.05)
        self.assertEqual(admission.waiters, 1)
        cancel.set()
        waiter.join(timeout=2)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(admission.waiters, 0)
        held.release()
        self.assertEqual(admission.active, 0)
        recovered = admission.try_acquire()
        self.assertIsNotNone(recovered)
        assert recovered is not None
        recovered.release()

    def test_timeout_and_capacity_reject_do_not_underflow_waiters(self):
        group = ConcurrencyGroup(key="review-sensei:provider:4:test:8:acme/api:1:14")
        admission = ProviderAdmission(group, max_waiters=1)
        held = admission.acquire()
        with self.assertRaises(AdmissionRejected):
            admission.acquire(timeout=0.05)
        self.assertEqual(admission.waiters, 0)
        with self.assertRaises(AdmissionRejected):
            admission.acquire(timeout=0.05)
        self.assertEqual(admission.waiters, 0)
        held.release()
        self.assertEqual(admission.active, 0)

    def test_cancelled_waiter_is_not_granted_a_freed_slot(self):
        group = ConcurrencyGroup(key="review-sensei:provider:4:test:8:acme/api:1:15")
        admission = ProviderAdmission(group, max_waiters=1)
        held = admission.acquire()
        cancel = threading.Event()
        granted: list[object] = []
        errors: list[BaseException] = []

        def wait_for_slot() -> None:
            try:
                granted.append(admission.acquire(cancel_event=cancel))
            except AdmissionCancelled:
                return
            except BaseException as exc:  # pragma: no cover - test diagnostics
                errors.append(exc)

        waiter = threading.Thread(target=wait_for_slot)
        waiter.start()
        deadline = time.monotonic() + 2
        while admission.waiters != 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(admission.waiters, 1)
        cancel.set()
        held.release()
        waiter.join(timeout=2)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(granted, [])
        self.assertEqual(errors, [])
        self.assertEqual(admission.waiters, 0)
        self.assertEqual(admission.active, 0)
        recovered = admission.try_acquire()
        self.assertIsNotNone(recovered)
        assert recovered is not None
        recovered.release()

    def test_unrelated_pull_requests_do_not_share_an_in_process_lock(self):
        first_plan = ReviewConcurrencyPlan.for_pull_request("acme/api", 1)
        second_plan = ReviewConcurrencyPlan.for_pull_request("acme/api", 2)
        self.assertIsNotNone(first_plan.provider)
        self.assertIsNotNone(second_plan.provider)
        assert first_plan.provider is not None
        assert second_plan.provider is not None
        first = ProviderAdmission(first_plan.provider)
        second = ProviderAdmission(second_plan.provider)
        lease_one = first.try_acquire()
        lease_two = second.try_acquire()
        self.assertIsNotNone(lease_one)
        self.assertIsNotNone(lease_two)
        self.assertEqual(first.active, 1)
        self.assertEqual(second.active, 1)
        assert lease_one is not None
        assert lease_two is not None
        lease_one.release()
        lease_two.release()

    def test_admission_logs_are_metadata_only(self):
        group = ConcurrencyGroup(key="review-sensei:provider:4:test:8:acme/api:2:10")
        admission = ProviderAdmission(group, max_waiters=0)
        with self.assertLogs("review_sensei.concurrency", level="INFO") as captured:
            lease = admission.acquire()
            self.assertIsNone(admission.try_acquire())
            lease.release()
        log_text = "\n".join(captured.output)
        self.assertIn("status=admitted", log_text)
        self.assertIn("status=released", log_text)
        self.assertIn("status=rejected_capacity", log_text)
        self.assertIn("key=review-sensei:provider:4:test:8:acme/api:2:10", log_text)
        self.assertNotIn("prompt", log_text)
        self.assertNotIn("diff", log_text)
        self.assertNotIn("def ", log_text)

    def test_admission_rejects_invalid_waiter_bounds(self):
        group = ConcurrencyGroup(key="review-sensei:provider:test")
        with self.assertRaises(ReviewInputError):
            ProviderAdmission(group, max_waiters=-1)
        with self.assertRaises(ReviewInputError):
            ProviderAdmission(group, max_waiters=True)
        with self.assertRaises(ReviewInputError):
            ProviderAdmission(group, max_waiters=10_000)
