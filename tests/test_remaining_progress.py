"""Productive remaining windows progress without erasing true failure charges."""

import json
import unittest
from dataclasses import replace
from types import SimpleNamespace

from review_sensei.coverage import CoverageManifest, FileCoverage
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewResult
from review_sensei.remaining_progress import execution_receipt, remaining_progress_proof
from review_sensei.rescan import AuthorizedRescan, rescan_source
from review_sensei.session import (
    InMemorySessionLedger,
    checkpoint_review_analysis,
    complete_review_publication,
    prepare_review_transaction,
    session_reservation_id,
)
from tests import test_review_transaction as tx


def _prepare(ledger, request=None):
    return prepare_review_transaction(
        ledger,
        tx.IDENTITY,
        tx.POLICY,
        reservation_id=session_reservation_id(
            repository=tx.IDENTITY.repository,
            pull_request=tx.IDENTITY.pull_request,
            head_sha=tx.HEAD_SHA,
            kind="publish",
        ),
        head_sha=tx.HEAD_SHA,
        base_sha=tx.BASE_SHA,
        configuration_digest=tx.CONFIGURATION_DIGEST,
        evidence_digest=tx.EVIDENCE_DIGEST,
        rescan_request=request,
    )


def _seed(ledger=None, *, result):
    ledger = ledger or InMemorySessionLedger()
    bound = checkpoint_review_analysis(ledger, tx.IDENTITY, _prepare(ledger), result)
    complete_review_publication(ledger, tx.IDENTITY, bound.transaction, published=True)
    return ledger, bound


def _request(ledger, request_id=10):
    return AuthorizedRescan(
        "workflow_dispatch",
        request_id,
        rescan_source(ledger.load(tx.IDENTITY).record),
        5,
    )


fixture = SimpleNamespace(seed=_seed, prepare=_prepare, request=_request)


def result(count, *, total=42, bounded=True, comments=()):
    paths = tuple(f"src/{index}.py" for index in range(total))
    value = ReviewResult(
        summary="Synthetic bounded window.",
        comments=comments,
        provider="fixture",
        model="fixture",
        review_status="partial",
        continuation_scope_digest="a" * 64,
        coverage=CoverageManifest(
            files=tuple(
                FileCoverage(path, "reviewed")
                if index < count
                else FileCoverage(path, "budget-exhausted", "provider-call-budget")
                for index, path in enumerate(paths)
            ),
            enumerated_paths=paths,
        ),
    )

    return (
        replace(
            value,
            bounded_work_receipt=execution_receipt(
                value, execution_identity="a" * 32, provider_calls=1
            ),
        )
        if bounded
        else value
    )


class RemainingProgressTests(unittest.TestCase):
    def test_more_than_32_productive_windows_do_not_consume_failure_allowance(self):
        ledger, prior = fixture.seed(result=result(1))
        self.assertEqual(ledger.load(tx.IDENTITY).record.failed_attempts, 1)
        for count in range(2, 40):
            request = replace(fixture.request(ledger, count + 10), mode="remaining")
            prepared = fixture.prepare(ledger, request)
            self.assertTrue(prepared.decision.admit, prepared.decision)
            bound = checkpoint_review_analysis(
                ledger, tx.IDENTITY, prepared, result(count)
            )
            stored = complete_review_publication(
                ledger, tx.IDENTITY, bound.transaction, published=True
            )
            self.assertEqual(stored.failed_attempts, 1)
            self.assertEqual(stored.completed_initial_reviews, 0)
            proof = stored.rescan_state["receipts"][-1]["progress"]
            self.assertEqual(proof["reviewed_after"], count)
            self.assertEqual(proof["child_digest"], bound.content_digest())
        self.assertEqual(len(stored.rescan_state["receipts"]), 6)

    def test_no_progress_and_real_failure_with_progress_are_charged(self):
        for child in (result(1), result(2, bounded=False)):
            with self.subTest(child=child):
                ledger, _ = fixture.seed(result=result(1))
                prepared = fixture.prepare(
                    ledger, replace(fixture.request(ledger), mode="remaining")
                )
                checkpoint_review_analysis(ledger, tx.IDENTITY, prepared, child)
                self.assertEqual(ledger.load(tx.IDENTITY).record.failed_attempts, 2)

    def test_replaced_units_scope_or_dropped_obligations_cannot_earn_progress(self):
        parent = result(1)
        child = result(2)
        self.assertIsNotNone(remaining_progress_proof(parent, child))

        def reseal(value):
            clean = replace(value, bounded_work_receipt=None)
            return replace(
                clean,
                bounded_work_receipt=execution_receipt(
                    clean, execution_identity="b" * 32, provider_calls=1
                ),
            )

        changes = (
            result(2, bounded=False),
            reseal(
                replace(
                    child, bounded_work_receipt=None, continuation_scope_digest="b" * 64
                )
            ),
            reseal(
                replace(
                    child,
                    bounded_work_receipt=None,
                    coverage=replace(
                        child.coverage,
                        files=tuple(
                            replace(item, path="different.py") if index == 0 else item
                            for index, item in enumerate(child.coverage.files)
                        ),
                        enumerated_paths=(
                            "different.py",
                            *child.coverage.enumerated_paths[1:],
                        ),
                    ),
                )
            ),
            result(1),
        )
        for changed in changes:
            with self.subTest(digest=changed.content_digest()):
                self.assertIsNone(remaining_progress_proof(parent, changed))
        from review_sensei.models import ReviewComment

        obligation = ReviewComment(
            path="src/0.py", line=1, body="Retain the unresolved authority concern."
        )
        self.assertIsNone(
            remaining_progress_proof(result(1, comments=(obligation,)), child)
        )

    def test_failure_exhaustion_still_blocks_a_new_remaining_event(self):
        ledger, _ = fixture.seed(result=result(1))
        for request_id in range(10, 15):
            prepared = fixture.prepare(
                ledger, replace(fixture.request(ledger, request_id), mode="remaining")
            )
            bound = checkpoint_review_analysis(
                ledger, tx.IDENTITY, prepared, result(2, bounded=False)
            )
            complete_review_publication(
                ledger, tx.IDENTITY, bound.transaction, published=True
            )
        self.assertEqual(ledger.load(tx.IDENTITY).record.failed_attempts, 6)
        denied = fixture.prepare(
            ledger, replace(fixture.request(ledger, 16), mode="remaining")
        )
        self.assertFalse(denied.decision.admit)
        self.assertEqual(ledger.load(tx.IDENTITY).record.failed_attempts, 6)

    def test_bounded_host_metadata_roundtrip_and_digest_binding(self):
        value = result(2)
        self.assertEqual(
            ReviewResult.from_dict(
                json.loads(json.dumps(value.to_dict(), sort_keys=True))
            ).content_digest(),
            value.content_digest(),
        )
        self.assertNotEqual(
            replace(value, bounded_work_receipt=None).content_digest(),
            value.content_digest(),
        )
        with self.assertRaises(ReviewInputError):
            replace(value, bounded_work_receipt=1)
        with self.assertRaises(ReviewInputError):
            replace(value, review_status="complete")


class RemainingCrashAccountingTests(unittest.TestCase):
    def test_local_crash_reconstructs_same_accounting_after_child_replaces_latest(self):
        import tempfile
        from pathlib import Path

        from review_sensei.session import (
            LocalSessionLedger,
            load_review_transaction_for_publication,
        )

        with tempfile.TemporaryDirectory() as directory:
            ledger, _ = fixture.seed(
                LocalSessionLedger(Path(directory)), result=result(1)
            )
            prepared = fixture.prepare(
                ledger, replace(fixture.request(ledger), mode="remaining")
            )
            child = result(2)
            preview = replace(
                child,
                transaction=replace(
                    prepared.transaction.with_result(child.content_digest()),
                    generation=prepared.transaction.generation + 1,
                ),
            )
            # Crash after immutable artifact retention but before checkpoint CAS.
            ledger.retain_checkpoint_result(preview)
            fresh = LocalSessionLedger(Path(directory))
            stored = load_review_transaction_for_publication(
                fresh,
                tx.IDENTITY,
                preview,
                base_sha=tx.BASE_SHA,
                head_sha=tx.HEAD_SHA,
                policy_digest=tx.POLICY.digest(),
                configuration_digest=tx.CONFIGURATION_DIGEST,
                evidence_digest=tx.EVIDENCE_DIGEST,
            )
            self.assertEqual(stored.failed_attempts, 1)
            self.assertEqual(
                stored.rescan_state["receipts"][-1]["progress"]["reviewed_after"], 2
            )
            replay = load_review_transaction_for_publication(
                fresh,
                tx.IDENTITY,
                preview,
                base_sha=tx.BASE_SHA,
                head_sha=tx.HEAD_SHA,
                policy_digest=tx.POLICY.digest(),
                configuration_digest=tx.CONFIGURATION_DIGEST,
                evidence_digest=tx.EVIDENCE_DIGEST,
            )
            self.assertEqual(stored.to_dict(), replay.to_dict())

    def test_missing_parent_refuses_without_charging_different_accounting(self):
        from unittest.mock import patch

        ledger, _ = fixture.seed(result=result(1))
        prepared = fixture.prepare(
            ledger, replace(fixture.request(ledger), mode="remaining")
        )
        before = ledger.load(tx.IDENTITY).record.to_dict()
        with patch.object(ledger, "load_retained_checkpoint_result", return_value=None):
            with self.assertRaisesRegex(ReviewInputError, "no accounting fallback"):
                checkpoint_review_analysis(ledger, tx.IDENTITY, prepared, result(2))
        self.assertEqual(before, ledger.load(tx.IDENTITY).record.to_dict())


class ExecutionCauseTests(unittest.TestCase):
    def test_lost_attempt_accounting_can_never_turn_a_failed_call_into_progress(self):
        from review_sensei.execution import WorkExecution, execute_plan
        from review_sensei.models import ProviderResponse
        from review_sensei.outcomes import ResourceBudgetTracker
        from review_sensei.planning import plan_work
        from tests.test_review_work import SharedWorkTests

        bundle, requirements, resource, budgets, render = SharedWorkTests().fixture()
        plan = plan_work(
            "reassessment", bundle, requirements, budgets=budgets, render=render
        )

        class Provider:
            name = model = "fixture"
            calls = 0

            def complete(self, request):
                self.calls += 1
                return ProviderResponse(
                    "valid" if self.calls == 1 else "invalid", self.name
                )

        def validate(response, batch):
            if response.text == "invalid":
                raise ReviewInputError("Synthetic invalid output")
            return response.text

        failed = execute_plan(
            plan,
            provider=Provider(),
            render=render,
            validate=validate,
            tracker=ResourceBudgetTracker(resource),
            budgets=budgets,
            correction="Repair once",
        )
        self.assertTrue(failed.attempt_accounting_complete)
        self.assertFalse(failed.clean_bounded_outcome)
        self.assertTrue(
            any(reason == "provider_call_limit" for _, reason in failed.pending)
        )
        # Exact current legacy recovery constructor loses attempts and liability.
        recovered = WorkExecution(
            failed.plan, failed.completed, failed.pending, failed.tracker_identity
        )
        self.assertFalse(recovered.clean_bounded_outcome)
        # A true fresh no-dispatch call limit is distinguishable.
        clean = replace(
            failed,
            attempted_ids=(),
            unknown_response_bytes=0,
            inflight_response_bytes=0,
            attempt_accounting_complete=True,
        )
        self.assertTrue(clean.clean_bounded_outcome)
        self.assertFalse(
            replace(clean, attempt_accounting_complete=False).clean_bounded_outcome
        )

    def test_real_legacy_and_unified_service_never_credit_failure_on_last_call(self):
        from review_sensei.models import ProviderResponse
        from review_sensei.outcomes import ResourceBudget
        from tests.test_work_continuation import DirectPartialScopeTests

        fixture = DirectPartialScopeTests()
        request = fixture.request()
        # Three chunks leave one normal bounded stop after a good first call.
        request = replace(
            request,
            diff=request.diff
            + "diff --git "
            + request.diff.split("diff --git ")[1].replace("mod_0", "mod_2"),
        )
        for mode in ("legacy", "unified"):

            class Provider:
                name = model = "fixture"
                calls = 0

                def complete(self, request):
                    self.calls += 1
                    return ProviderResponse(
                        '{"summary":"Reviewed.","comments":[]}'
                        if self.calls == 1
                        else "invalid",
                        self.name,
                    )

            service = fixture.service(Provider(), mode=mode)
            service.budget = ResourceBudget.create(max_provider_calls=2)
            run = service.run(request)
            self.assertIsNotNone(run.result)
            self.assertIsNone(run.result.bounded_work_receipt)


class ActualProductiveWindowTests(unittest.TestCase):
    def test_real_service_40_windows_retain_every_validated_chunk_without_failure_reset(
        self,
    ):
        from review_sensei.budgets import ReviewWorkBudgets
        from review_sensei.models import ProviderResponse, ReviewRequest
        from review_sensei.outcomes import ResourceBudget
        from review_sensei.service import ReviewService
        from review_sensei.stages import Stage
        from review_sensei.validation import ReviewLimits

        class Provider:
            name = model = "fixture"
            calls = 0

            def complete(self, request):
                self.calls += 1
                return ProviderResponse(
                    '{"summary":"Validated synthetic chunk.","comments":[]}', self.name
                )

        provider = Provider()
        diff = "".join(
            f"diff --git a/src/mod_{i}.py b/src/mod_{i}.py\n--- a/src/mod_{i}.py\n+++ b/src/mod_{i}.py\n@@ -1 +1 @@\n-old\n+safe\n"
            for i in range(40)
        )
        request = ReviewRequest(
            diff=diff,
            base_sha=tx.BASE_SHA,
            head_sha=tx.HEAD_SHA,
            model="fixture",
            orchestrate_large_changes=True,
            limits=ReviewLimits(
                max_diff_bytes=220,
                max_diff_lines=12,
                max_diff_files=1,
                max_diff_hunks=1,
            ),
        )
        service = ReviewService(
            provider,
            stages=(Stage("review", "Review {diff}", ("summary", "comments")),),
            work_budgets=ReviewWorkBudgets(mode="legacy"),
            budget=ResourceBudget.create(max_provider_calls=1),
        )
        first = service.run(request).result
        self.assertIsNotNone(first.bounded_work_receipt)
        ledger, prior = fixture.seed(result=first)
        for window in range(2, 41):
            prepared = fixture.prepare(
                ledger, replace(fixture.request(ledger, window + 10), mode="remaining")
            )
            self.assertTrue(prepared.decision.admit)
            child = service.run(request, prior_partial=prior).result
            bound = checkpoint_review_analysis(ledger, tx.IDENTITY, prepared, child)
            record = complete_review_publication(
                ledger, tx.IDENTITY, bound.transaction, published=True
            )
            self.assertEqual(record.failed_attempts, 1)
            self.assertEqual(provider.calls, window)
            self.assertEqual(
                sum(item.outcome == "reviewed" for item in child.coverage.files), window
            )
            prior = bound
        self.assertTrue(prior.coverage.fully_reviewed)
        self.assertEqual(prior.review_status, "complete")
        self.assertEqual(record.completed_initial_reviews, 1)
