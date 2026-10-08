from __future__ import annotations

import json
import unittest
from dataclasses import replace

from review_sensei.budgets import ProviderCapabilities, ReviewWorkBudgets
from review_sensei.configuration import parse_configuration_text
from review_sensei.errors import ProviderError, ReviewFormatError, ReviewInputError
from review_sensei.evidence import EvidenceBundle, EvidenceRecord, EvidenceSnapshot
from review_sensei.execution import execute_plan
from review_sensei.human_assessment import HumanReviewFinding, PendingHumanReview
from review_sensei.models import ProviderRequest, ProviderResponse, ReviewRequest
from review_sensei.outcomes import ResourceBudget, ResourceBudgetTracker
from review_sensei.planning import WorkRequirement, plan_work
from review_sensei.reassessment_work import reassess, validate_work
from review_sensei.service import ReviewService
from review_sensei.stages import Stage
from review_sensei.validation import ReviewLimits


class ReviewWorkBudgetTests(unittest.TestCase):
    def test_configuration_preserves_legacy_default_and_exposes_opt_in_targets(self):
        default = parse_configuration_text("schema: 1\n")
        self.assertEqual(default.advanced.review_work.mode, "legacy")
        unified = parse_configuration_text(
            "schema: 1\nadvanced:\n  review_work:\n    mode: unified\n    batch_diff_bytes: 65536\n"
        )
        self.assertEqual(unified.advanced.review_work.mode, "unified")
        self.assertEqual(unified.advanced.review_work.batch_diff_bytes, 65536)

    def test_unknown_provider_does_not_expand_reassessment_prompt(self):
        budgets = ReviewWorkBudgets(mode="unified")
        effective = budgets.effective(
            limits=ReviewLimits(), resource=ResourceBudget.create(), mode="reassessment"
        )
        self.assertEqual(effective.batch_prompt_bytes, 48 * 1024)
        self.assertEqual(effective.batch_output_bytes, 16 * 1024)
        self.assertFalse(effective.provider_qualified)

    def test_qualified_capacity_is_intersected_with_public_and_run_limits(self):
        budgets = ReviewWorkBudgets(mode="unified")
        capabilities = ProviderCapabilities(
            context_tokens=200_000,
            max_output_tokens=4096,
            qualified=True,
            provider_name="fixture",
            model="fixture",
        )
        effective = budgets.effective(
            limits=ReviewLimits(max_diff_bytes=64 * 1024),
            resource=ResourceBudget.create(max_prompt_bytes=96 * 1024),
            mode="reassessment",
            capabilities=capabilities,
        )
        self.assertEqual(effective.batch_diff_bytes, 64 * 1024)
        self.assertEqual(effective.batch_prompt_bytes, 96 * 1024)
        self.assertTrue(effective.provider_qualified)

    def test_nonintegers_and_expanded_public_ceilings_are_rejected(self):
        for value in (True, 0, -1, 1_048_577):
            with self.subTest(value=value), self.assertRaises(ReviewInputError):
                ReviewWorkBudgets(batch_diff_bytes=value)


class EvidenceTests(unittest.TestCase):
    def test_conflicting_duplicate_patch_cannot_become_complete_evidence(self):
        snapshot = EvidenceSnapshot(base_sha="a" * 40, head_sha="b" * 40)
        first = EvidenceRecord("src/a.py", "@@ -1 +1 @@\n-old\n+new", snapshot)
        changed = EvidenceRecord("src/a.py", "@@ -1 +1 @@\n-old\n+other", snapshot)
        with self.assertRaises(ReviewInputError):
            EvidenceBundle(snapshot, (first, changed))

    def test_record_identity_binds_exact_snapshot_and_patch(self):
        first = EvidenceSnapshot(base_sha="a" * 40, head_sha="b" * 40)
        other = EvidenceSnapshot(base_sha="a" * 40, head_sha="c" * 40)
        record = EvidenceRecord("src/a.py", "@@ -1 +1 @@\n-old\n+new", first)
        self.assertNotEqual(
            record.evidence_id,
            EvidenceRecord("src/a.py", record.patch, other).evidence_id,
        )

    def test_incomplete_patch_is_never_complete_required_evidence(self):
        snapshot = EvidenceSnapshot(base_sha="a" * 40, head_sha="b" * 40)
        record = EvidenceRecord("src/a.py", "@@ -1 +1 @@\n-old", snapshot)
        self.assertFalse(record.complete)


class SharedWorkTests(unittest.TestCase):
    def test_adapter_structural_and_nontransient_errors_stay_pending_and_charged(self):
        bundle, requirements, resource, budgets, render = self.fixture()
        plan = plan_work(
            "reassessment",
            bundle,
            requirements[:1],
            budgets=budgets,
            render=render,
            authority_digest="f" * 64,
        )
        for error, diagnostic in (
            (ReviewFormatError("synthetic format failure"), "invalid_provider_output"),
            (ReviewInputError("synthetic input failure"), "invalid_provider_output"),
            (ProviderError("synthetic provider failure"), "provider_failed"),
        ):
            with self.subTest(error=type(error).__name__):

                class Provider:
                    name = "fixture"
                    model = "fixture"
                    calls = 0

                    def complete(self, request):
                        self.calls += 1
                        raise error

                provider = Provider()
                tracker = ResourceBudgetTracker(resource)
                result = execute_plan(
                    plan,
                    provider=provider,
                    render=render,
                    validate=lambda response, batch: response.text,
                    tracker=tracker,
                    budgets=budgets,
                    correction="Return strict JSON.",
                )
                self.assertEqual(result.pending, (("0", diagnostic),))
                self.assertEqual(provider.calls, 1)
                self.assertEqual(tracker.provider_calls, 1)
                self.assertGreater(tracker.prompt_bytes, 0)

    def test_continuation_reuses_completed_batch_without_resetting_budget(self):
        bundle, requirements, resource, budgets, render = self.fixture()
        initial_bundle = EvidenceBundle(bundle.snapshot, bundle.records[:1])
        first_plan = plan_work(
            "reassessment",
            initial_bundle,
            requirements,
            budgets=budgets,
            render=render,
            authority_digest="f" * 64,
        )

        class Provider:
            name = "fixture"
            model = "fixture"

            def __init__(self):
                self.calls = 0

            def complete(self, request):
                self.calls += 1
                return ProviderResponse("{}", self.name, self.model)

        provider = Provider()
        tracker = ResourceBudgetTracker(resource)
        first = execute_plan(
            first_plan,
            provider=provider,
            render=render,
            validate=lambda response, batch: response.text,
            tracker=tracker,
            budgets=budgets,
        )
        self.assertEqual(first.pending_ids, ("1",))
        self.assertEqual(provider.calls, 1)
        expanded_plan = plan_work(
            "reassessment",
            bundle,
            requirements,
            budgets=budgets,
            render=render,
            authority_digest="f" * 64,
        )
        resumed = execute_plan(
            expanded_plan,
            provider=provider,
            render=render,
            validate=lambda response, batch: response.text,
            tracker=tracker,
            budgets=budgets,
            prior=first,
            revalidate_cached=lambda value, batch: value == "{}",
        )
        self.assertFalse(resumed.pending)
        self.assertEqual(provider.calls, 2)
        self.assertEqual(tracker.provider_calls, 2)
        with self.assertRaises(ReviewInputError):
            execute_plan(
                expanded_plan,
                provider=provider,
                render=render,
                validate=lambda response, batch: response.text,
                tracker=ResourceBudgetTracker(resource),
                budgets=budgets,
                prior=first,
                revalidate_cached=lambda value, batch: True,
            )
        self.assertEqual(provider.calls, 2)

    def test_stale_authority_cannot_reuse_prior_decisions(self):
        bundle, requirements, resource, budgets, render = self.fixture()
        provider = AssessingProvider()
        tracker = ResourceBudgetTracker(resource)
        plan = plan_work(
            "reassessment",
            bundle,
            requirements,
            budgets=budgets,
            render=render,
            authority_digest="f" * 64,
        )
        # Budget exhaustion produces a valid trace without a provider call.
        tracker.provider_calls = resource.max_provider_calls
        prior = execute_plan(
            plan,
            provider=provider,
            render=render,
            validate=lambda response, batch: response.text,
            tracker=tracker,
            budgets=budgets,
        )
        changed = plan_work(
            "reassessment",
            bundle,
            requirements,
            budgets=budgets,
            render=render,
            authority_digest="a" * 64,
        )
        with self.assertRaises(ReviewInputError):
            execute_plan(
                changed,
                provider=provider,
                render=render,
                validate=lambda response, batch: response.text,
                tracker=tracker,
                budgets=budgets,
                prior=prior,
                revalidate_cached=lambda value, batch: True,
            )
        self.assertFalse(provider.calls)

    def test_cross_file_requirement_is_atomic_and_never_split_to_fit(self):
        bundle, _, resource, budgets, render = self.fixture()
        required = WorkRequirement(
            "cross-file", tuple(record.evidence_id for record in bundle.records)
        )
        plan = plan_work(
            "reassessment", bundle, (required,), budgets=budgets, render=render
        )
        self.assertFalse(plan.batches)
        self.assertEqual(
            plan.unprocessed, (("cross-file", "required-evidence-oversized"),)
        )

    def test_budget_change_changes_identity_and_cannot_execute_old_plan(self):
        bundle, requirements, resource, budgets, render = self.fixture()
        first = plan_work(
            "reassessment", bundle, requirements, budgets=budgets, render=render
        )
        changed = replace(budgets, max_total_prompt_bytes=1000)
        second = plan_work(
            "reassessment", bundle, requirements, budgets=changed, render=render
        )
        self.assertNotEqual(first.plan_id, second.plan_id)
        with self.assertRaises(ReviewInputError):
            execute_plan(
                first,
                provider=AssessingProvider(),
                render=render,
                validate=lambda response, batch: response.text,
                tracker=ResourceBudgetTracker(resource),
                budgets=changed,
            )

    def test_malformed_outputs_consume_correction_budget_and_remain_pending(self):
        bundle, requirements, resource, budgets, render = self.fixture()
        plan = plan_work(
            "reassessment",
            bundle,
            requirements,
            budgets=budgets,
            render=render,
            correction="Return strict JSON.",
        )

        class Provider:
            name = "fixture"
            model = "fixture"

            def __init__(self):
                self.calls = []

            def complete(self, request):
                self.calls.append(request)
                return ProviderResponse("malformed", self.name)

        provider = Provider()
        tracker = ResourceBudgetTracker(resource)

        def invalid(response, batch):
            raise ReviewInputError("invalid fixture")

        run = execute_plan(
            plan,
            provider=provider,
            render=render,
            validate=invalid,
            tracker=tracker,
            budgets=budgets,
            correction="Return strict JSON.",
        )
        self.assertFalse(run.completed)
        self.assertEqual(run.pending_ids, ("0", "1"))
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(tracker.structural_retries, 1)
        self.assertNotIn("malformed", provider.calls[-1].prompt)
        self.assertLessEqual(provider.calls[-1].timeout_seconds, 120)

    def test_deadline_and_total_prompt_budget_stop_before_dispatch(self):
        bundle, requirements, resource, budgets, render = self.fixture()

        class Provider:
            name = "fixture"
            model = "fixture"

            def complete(self, request):
                raise AssertionError("No dispatch is permitted")

        for timeout, limit in ((0, 1000), (120000, 1)):
            with self.subTest(timeout=timeout, limit=limit):
                effective = replace(
                    budgets,
                    max_total_prompt_bytes=limit,
                    resource_budget=replace(resource, timeout_ms=timeout),
                )
                plan = plan_work(
                    "reassessment",
                    bundle,
                    requirements,
                    budgets=effective,
                    render=render,
                )
                run = execute_plan(
                    plan,
                    provider=Provider(),
                    render=render,
                    validate=lambda response, batch: response.text,
                    tracker=ResourceBudgetTracker(
                        replace(resource, timeout_ms=timeout)
                    ),
                    budgets=effective,
                )
                self.assertFalse(run.completed)
                self.assertEqual(run.pending_ids, ("0", "1"))

    def fixture(self):
        snapshot = EvidenceSnapshot(base_sha="a" * 40, head_sha="b" * 40)
        records = tuple(
            EvidenceRecord(f"src/{name}.py", "@@ -1 +1 @@\n-old\n+new", snapshot)
            for name in ("a", "b")
        )
        bundle = EvidenceBundle(snapshot, records)
        requirements = tuple(
            WorkRequirement(str(index), (record.evidence_id,))
            for index, record in enumerate(records)
        )
        resource = ResourceBudget.create(max_provider_calls=2)
        budgets = ReviewWorkBudgets(mode="unified", batch_diff_bytes=40).effective(
            limits=ReviewLimits(), resource=resource, mode="reassessment"
        )

        def render(batch):
            return ProviderRequest(
                prompt="assess " + str(batch.requirement_ids),
                max_prompt_bytes=budgets.batch_prompt_bytes,
                max_response_bytes=budgets.batch_output_bytes,
            )

        return bundle, requirements, resource, budgets, render

    def test_same_planner_preserves_unprocessed_identities_and_deterministic_order(
        self,
    ):
        bundle, requirements, resource, budgets, render = self.fixture()
        first = plan_work(
            "reassessment", bundle, requirements, budgets=budgets, render=render
        )
        second = plan_work(
            "reassessment",
            bundle,
            tuple(reversed(requirements)),
            budgets=budgets,
            render=render,
        )
        self.assertEqual(first.plan_id, second.plan_id)
        self.assertEqual(
            [batch.requirement_ids for batch in first.batches], [("0",), ("1",)]
        )
        self.assertEqual(first.unprocessed, ())

    def test_transport_failures_consume_shared_call_budget_and_leave_later_work_pending(
        self,
    ):
        bundle, requirements, resource, budgets, render = self.fixture()
        plan = plan_work(
            "reassessment", bundle, requirements, budgets=budgets, render=render
        )

        class Provider:
            name = "fake"
            model = "fake"

            def __init__(self):
                self.calls = 0

            def complete(self, request):
                self.calls += 1
                if self.calls == 1:
                    raise ProviderError("temporary", transient=True)
                return ProviderResponse("{}", self.name, self.model)

        provider = Provider()
        tracker = ResourceBudgetTracker(resource, sleeper=lambda _: None)
        run = execute_plan(
            plan,
            provider=provider,
            render=render,
            validate=lambda response, batch: response.text,
            tracker=tracker,
            budgets=budgets,
        )
        self.assertEqual(provider.calls, 2)
        self.assertEqual(tracker.provider_calls, 2)
        self.assertEqual(len(run.completed), 1)
        self.assertEqual(run.pending_ids, ("1",))


HUMAN = "This path is intentionally local-only and rejects remote requests before transmitting data."


def large_patch(size):
    text = "return reject_remote_requests(data) " + "x" * size
    return "@@ -1 +1 @@\n-old\n+" + text


class AssessingProvider:
    name = "fixture"
    model = "fixture"

    def __init__(self):
        self.calls = []

    def complete(self, request):
        self.calls.append(request)
        value, _ = json.JSONDecoder().raw_decode(
            request.prompt[request.prompt.index('{"head_sha"') :]
        )
        decisions = [
            {
                "fingerprint": item["fingerprint"],
                "decision": "dismissed",
                "rationale": "The current file patch rejects remote requests before any transmission.",
                "human_evidence": "This path is intentionally local-only",
                "diff_evidence": "reject_remote_requests(data)",
            }
            for item in value["pending"]
        ]
        for decision, item in zip(decisions, value["pending"]):
            if "required_paths" in item:
                decision["related_diff_evidence"] = [
                    {"path": path, "excerpt": "reject_remote_requests(data)"}
                    for path in item["required_paths"]
                    if path != item["path"]
                ]
        return ProviderResponse(
            json.dumps(
                {"body": "Supported by the current evidence.", "assessments": decisions}
            ),
            self.name,
            self.model,
        )


class UnifiedAdaptersTests(unittest.TestCase):
    def test_citation_failure_has_no_semantic_correction_retry(self):
        pending, bundle = self.fixture((100,))

        class Provider(AssessingProvider):
            def complete(self, request):
                response = super().complete(request)
                payload = json.loads(response.text)
                payload["assessments"][0]["diff_evidence"] = (
                    "invented unsupported quote"
                )
                return ProviderResponse(json.dumps(payload), self.name)

        provider = Provider()
        run = reassess(
            provider=provider,
            pending=pending,
            bundle=bundle,
            source_body=HUMAN,
            authority_digest="f" * 64,
            work_budgets=ReviewWorkBudgets(mode="unified"),
        )
        self.assertEqual(len(provider.calls), 1)
        self.assertFalse(run.execution.completed)
        self.assertEqual(pending.apply(run.reply.decisions), pending)

    def test_invalid_json_gets_one_bounded_structural_correction(self):
        pending, bundle = self.fixture((100,))

        class Provider(AssessingProvider):
            def complete(self, request):
                if not self.calls:
                    self.calls.append(request)
                    return ProviderResponse("{", self.name)
                return super().complete(request)

        provider = Provider()
        run = reassess(
            provider=provider,
            pending=pending,
            bundle=bundle,
            source_body=HUMAN,
            authority_digest="f" * 64,
            work_budgets=ReviewWorkBudgets(mode="unified"),
        )
        self.assertEqual(len(provider.calls), 2)
        self.assertFalse(pending.apply(run.reply.decisions).pending)

    def test_v2_cross_file_inventory_keeps_exact_identity_and_complete_group(self):
        pending, bundle = self.fixture((100, 100))
        finding = replace(
            pending.findings[0],
            required_paths=tuple(record.path for record in bundle.records),
        )
        pending = PendingHumanReview(pending.base_sha, (finding,))
        self.assertEqual(PendingHumanReview.from_dict(pending.to_dict()), pending)
        self.assertEqual(pending.to_dict()["schema_version"], "2")
        provider = AssessingProvider()
        run = reassess(
            provider=provider,
            pending=pending,
            bundle=bundle,
            source_body=HUMAN,
            authority_digest="f" * 64,
            work_budgets=ReviewWorkBudgets(mode="unified"),
        )
        self.assertFalse(pending.apply(run.reply.decisions).pending)
        self.assertEqual(run.reply.decisions[0].fingerprint, finding.fingerprint)
        self.assertEqual(len(run.execution.completed[0].batch.records), 2)
        self.assertEqual(len(run.reply.decisions[0].related_diff_evidence), 1)

    def test_cross_file_citations_and_all_complete_files_are_mandatory(self):
        for missing in ("patch", "citation"):
            with self.subTest(missing=missing):
                pending, bundle = self.fixture((100, 100))
                finding = replace(
                    pending.findings[0],
                    required_paths=tuple(record.path for record in bundle.records),
                )
                pending = PendingHumanReview(pending.base_sha, (finding,))
                if missing == "patch":
                    bundle = EvidenceBundle(bundle.snapshot, bundle.records[:1])

                class Provider(AssessingProvider):
                    def complete(self, request):
                        response = super().complete(request)
                        payload = json.loads(response.text)
                        for decision in payload["assessments"]:
                            decision.pop("related_diff_evidence", None)
                        return ProviderResponse(json.dumps(payload), self.name)

                provider = Provider()
                run = reassess(
                    provider=provider,
                    pending=pending,
                    bundle=bundle,
                    source_body=HUMAN,
                    authority_digest="f" * 64,
                    work_budgets=ReviewWorkBudgets(mode="unified"),
                )
                self.assertEqual(
                    pending.apply(run.reply.decisions).pending, pending.pending
                )
                self.assertFalse(run.execution.completed)

    def test_configured_output_cap_reduces_finding_batch_size(self):
        pending, bundle = self.fixture((100,) * 5)
        provider = AssessingProvider()
        provider.max_output_tokens = 2048
        run = reassess(
            provider=provider,
            pending=pending,
            bundle=bundle,
            source_body=HUMAN,
            authority_digest="f" * 64,
            work_budgets=ReviewWorkBudgets(mode="unified"),
        )
        self.assertEqual(
            [len(batch.batch.requirements) for batch in run.execution.completed], [3, 2]
        )
        self.assertTrue(
            all(request.max_output_tokens == 2048 for request in provider.calls)
        )

    def test_complete_hunk_with_missing_later_changes_is_incomplete(self):
        pending, bundle = self.fixture((100,))
        truncated = replace(
            bundle.records[0], expected_additions=2, expected_deletions=2
        )
        bundle = EvidenceBundle(bundle.snapshot, (truncated,))
        provider = AssessingProvider()
        run = reassess(
            provider=provider,
            pending=pending,
            bundle=bundle,
            source_body=HUMAN,
            authority_digest="f" * 64,
            work_budgets=ReviewWorkBudgets(mode="unified"),
        )
        self.assertFalse(provider.calls)
        self.assertEqual(pending.apply(run.reply.decisions).pending, pending.pending)

    def test_transport_enumeration_failure_keeps_all_findings_pending(self):
        pending, bundle = self.fixture((100,))
        bundle = EvidenceBundle(bundle.snapshot, (), enumeration_complete=False)
        provider = AssessingProvider()
        run = reassess(
            provider=provider,
            pending=pending,
            bundle=bundle,
            source_body=HUMAN,
            authority_digest="f" * 64,
            work_budgets=ReviewWorkBudgets(mode="unified"),
        )
        self.assertFalse(provider.calls)
        self.assertEqual(run.execution.pending_ids, (pending.findings[0].fingerprint,))

    def fixture(self, sizes):
        snapshot = EvidenceSnapshot("owner/repo", 1, "a" * 40, "b" * 40)
        records = tuple(
            EvidenceRecord(f"src/{index}.py", large_patch(size), snapshot)
            for index, size in enumerate(sizes)
        )
        pending = PendingHumanReview(
            snapshot.base_sha,
            tuple(
                HumanReviewFinding(
                    f"{index:064x}", record.path, "Confirm local-only behavior."
                )
                for index, record in enumerate(records)
            ),
        )
        return pending, EvidenceBundle(snapshot, records)

    def test_77_kib_and_more_than_128_kib_use_complete_file_batches(self):
        for sizes in ((8500,) * 9, (17000,) * 9):
            with self.subTest(sizes=sizes):
                pending, bundle = self.fixture(sizes)
                provider = AssessingProvider()
                run = reassess(
                    provider=provider,
                    pending=pending,
                    bundle=bundle,
                    source_body=HUMAN,
                    authority_digest="f" * 64,
                    work_budgets=ReviewWorkBudgets(mode="unified"),
                )
                self.assertGreater(len(provider.calls), 1)
                self.assertFalse(run.execution.pending)
                self.assertFalse(pending.apply(run.reply.decisions).pending)
                self.assertTrue(
                    all(
                        len(call.prompt.encode()) <= 48 * 1024
                        for call in provider.calls
                    )
                )
                for batch in run.execution.completed:
                    self.assertTrue(
                        all(record.complete for record in batch.batch.records)
                    )

    def test_oversized_single_file_does_not_clear_that_finding(self):
        pending, bundle = self.fixture((80000, 500))
        provider = AssessingProvider()
        run = reassess(
            provider=provider,
            pending=pending,
            bundle=bundle,
            source_body=HUMAN,
            authority_digest="f" * 64,
            work_budgets=ReviewWorkBudgets(mode="unified"),
        )
        self.assertEqual(run.execution.pending_ids, (pending.findings[0].fingerprint,))
        self.assertEqual(
            pending.apply(run.reply.decisions).pending, (pending.findings[0],)
        )
        self.assertEqual(len(provider.calls), 1)

    def test_publisher_validation_rejects_changed_snapshot_and_decision_tampering(self):
        pending, bundle = self.fixture((100,))
        run = reassess(
            provider=AssessingProvider(),
            pending=pending,
            bundle=bundle,
            source_body=HUMAN,
            authority_digest="f" * 64,
            work_budgets=ReviewWorkBudgets(mode="unified"),
        )
        for body, authority in ((HUMAN + " edited", "a" * 64), (HUMAN, "a" * 64)):
            with self.assertRaises(ReviewInputError):
                validate_work(
                    run,
                    pending=pending,
                    bundle=bundle,
                    source_body=body,
                    authority_digest=authority,
                )
        tampered = replace(run, reply=replace(run.reply, decisions=()))
        with self.assertRaises(ReviewInputError):
            validate_work(
                tampered,
                pending=pending,
                bundle=bundle,
                source_body=HUMAN,
                authority_digest="f" * 64,
            )

    def test_discovery_uses_shared_batches_and_reports_unprocessed_files(self):
        pending, bundle = self.fixture((100, 100))

        class Provider:
            name = "fixture"
            model = "fixture"

            def __init__(self):
                self.calls = []

            def complete(self, request):
                self.calls.append(request)
                return ProviderResponse(
                    '{"summary":"Reviewed.","comments":[]}', self.name, self.model
                )

        provider = Provider()
        stage = Stage(
            name="review",
            prompt_template="Review {diff}",
            outputs=("summary", "comments"),
        )
        service = ReviewService(
            provider,
            stages=(stage,),
            budget=ResourceBudget.create(max_provider_calls=1),
            work_budgets=ReviewWorkBudgets(mode="unified", batch_diff_bytes=300),
        )
        request = ReviewRequest(
            diff="".join(record.diff for record in bundle.records),
            base_sha=bundle.snapshot.base_sha,
            head_sha=bundle.snapshot.head_sha,
        )
        run = service.run(request)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(run.outcome.status, "partial")
        self.assertFalse(run.result.coverage.fully_reviewed)

    def test_discovery_splits_exhaustive_hunks_but_not_oversized_hunk(self):
        snapshot = EvidenceSnapshot(base_sha="a" * 40, head_sha="b" * 40)
        patch = (
            "@@ -1 +1 @@\n-old\n+" + "a" * 100 + "\n@@ -3 +3 @@\n-old\n+" + "b" * 100
        )
        record = EvidenceRecord("src/a.py", patch, snapshot)

        class Provider:
            name = "fixture"
            model = "fixture"

            def complete(self, request):
                return ProviderResponse(
                    '{"summary":"Reviewed.","comments":[]}', self.name
                )

        stage = Stage(
            name="review",
            prompt_template="Review {diff}",
            outputs=("summary", "comments"),
        )
        service = ReviewService(
            Provider(),
            stages=(stage,),
            work_budgets=ReviewWorkBudgets(mode="unified", batch_diff_bytes=250),
        )
        run = service.run(ReviewRequest(diff=record.diff))
        self.assertEqual(run.outcome.provider_calls, 2)
        self.assertEqual(run.outcome.status, "reviewed")
        self.assertTrue(run.result.coverage.fully_reviewed)


if __name__ == "__main__":
    unittest.main()
