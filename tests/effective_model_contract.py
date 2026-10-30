"""Execution identity regressions shared by source and installed-wheel lanes."""

import unittest
from dataclasses import replace

from review_sensei import (
    IncrementalReviewPlan,
    ProviderResponse,
    ReviewRequest,
    ReviewService,
)
from review_sensei.context import build_review_context_cache_key

_DIFF = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"


class _Provider:
    name = "synthetic"
    allow_model_override = True

    def __init__(self, model):
        self.model = model
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        return ProviderResponse(
            text='{"summary":"ok","comments":[]}', provider=self.name, model=self.model
        )


class EffectiveModelContractTests(unittest.TestCase):
    def request(self):
        return ReviewRequest(
            diff=_DIFF,
            repository="owner/repo",
            pull_request_number=1,
            base_sha="a" * 40,
            head_sha="b" * 40,
        )

    def test_omitted_model_change_requires_real_fallback_dispatch(self):
        first = _Provider("model-a")
        service = ReviewService(first)
        request = self.request()
        self.assertEqual(service.run(request).outcome.status, "reviewed")
        key = service.context_cache_key(request)
        self.assertIsNotNone(key)
        second = _Provider("model-b")
        run = ReviewService(second).run(
            replace(request, head_sha="c" * 40),
            incremental=IncrementalReviewPlan(previous_key=key, reviewed_paths=()),
        )
        self.assertEqual(run.result.coverage_mode, "fallback-full")
        self.assertGreater(second.calls, 0)

    def test_same_effective_model_supports_no_call_skip(self):
        provider = _Provider("model-a")
        service = ReviewService(provider)
        request = self.request()
        key = service.context_cache_key(request)
        run = service.run(
            replace(request, head_sha="c" * 40),
            incremental=IncrementalReviewPlan(previous_key=key, reviewed_paths=()),
        )
        self.assertEqual(run.result.coverage_mode, "incremental")
        self.assertEqual(provider.calls, 0)

    def test_override_policy_ignores_only_unused_defaults(self):
        request = replace(self.request(), model="explicit")
        first, second = _Provider("model-a"), _Provider("model-b")
        self.assertEqual(
            ReviewService(first).context_cache_key(request),
            ReviewService(second).context_cache_key(request),
        )
        first.allow_model_override = second.allow_model_override = False
        self.assertNotEqual(
            ReviewService(first).context_cache_key(request),
            ReviewService(second).context_cache_key(request),
        )
        self.assertEqual(
            ReviewService(first).context_cache_key(request).model, "model-a"
        )

    def test_stage_routing_and_mapping_order(self):
        from review_sensei import Stage

        stages = (
            Stage("first", "{diff}", ("summary",)),
            Stage("second", "{diff}", ("summary", "comments")),
        )
        default, routed = _Provider("model-a"), _Provider("routed-model")
        request = self.request()
        first = ReviewService(
            default, stages=stages, stage_providers={"first": default, "second": routed}
        )
        reordered = ReviewService(
            default, stages=stages, stage_providers={"second": routed, "first": default}
        )
        self.assertEqual(
            first.context_cache_key(request), reordered.context_cache_key(request)
        )
        original = first.context_cache_key(request)
        routed.model = "changed-model"
        self.assertNotEqual(original, first.context_cache_key(request))
        routed.model = "routed-model"
        routed.name = "other-adapter"
        self.assertNotEqual(original, first.context_cache_key(request))

    def test_unknown_fixed_model_cannot_reuse_or_populate_cache(self):
        from review_sensei.context import ReviewContextCache

        request = self.request()
        previous = ReviewService(_Provider("model-a")).context_cache_key(request)
        provider = _Provider(None)
        provider.allow_model_override = False
        cache = ReviewContextCache()
        service = ReviewService(provider, cache=cache)
        request = replace(request, model="ignored", head_sha="c" * 40)
        self.assertIsNone(service.context_cache_key(request))
        run = service.run(
            request,
            incremental=IncrementalReviewPlan(previous_key=previous, reviewed_paths=()),
        )
        self.assertEqual(run.result.coverage_mode, "fallback-full")
        self.assertGreater(provider.calls, 0)
        self.assertEqual(len(cache._keys), 0)

    def test_override_and_budget_wrapper_resolve_same_identity(self):
        from review_sensei.service import _BudgetedProvider, _CallBudget

        default, override = _Provider("model-a"), _Provider("model-b")
        override.allow_model_override = False
        service = ReviewService(default)
        request = replace(self.request(), model="ignored")
        wrapped = _BudgetedProvider(override, budget=_CallBudget(max_calls=3))
        self.assertFalse(wrapped.allow_model_override)
        expected = ReviewService(override).context_cache_key(request)
        self.assertEqual(
            expected, service.context_cache_key(request, provider_override=override)
        )
        self.assertEqual(
            expected, service.context_cache_key(request, provider_override=wrapped)
        )
        run = service.run(request, current_key=expected, provider_override=wrapped)
        self.assertEqual(run.outcome.status, "reviewed")

    def test_pure_helper_requires_known_model_and_exact_stage_bindings(self):
        from review_sensei import Stage
        from review_sensei.errors import ContextLoadError

        stage = Stage(
            "first", "{diff}", ("summary", "comments"), provider_profile="fast-triage"
        )
        request = self.request()
        self.assertIsNone(
            build_review_context_cache_key(
                request, provider_name="synthetic", stages=(stage,)
            )
        )
        request = replace(request, model="explicit")
        self.assertIsNone(
            build_review_context_cache_key(
                request, provider_name="synthetic", stages=(stage,)
            )
        )
        for bindings in (
            {},
            {"extra": ("synthetic", "model")},
            {"first": ("synthetic", "model"), "extra": ("synthetic", "model")},
        ):
            with self.assertRaises(ContextLoadError):
                build_review_context_cache_key(
                    request,
                    provider_name="synthetic",
                    stages=(stage,),
                    stage_provider_bindings=bindings,
                )
        self.assertIsNotNone(
            build_review_context_cache_key(
                request,
                provider_name="synthetic",
                stages=(stage,),
                stage_provider_bindings={"first": ("synthetic", "model")},
            )
        )

    def test_unified_and_legacy_use_effective_model_resolver(self):
        from review_sensei.budgets import ReviewWorkBudgets

        request = self.request()
        for mode in ("legacy", "unified"):
            with self.subTest(mode=mode):
                work = ReviewWorkBudgets(mode=mode)
                first = ReviewService(_Provider("model-a"), work_budgets=work)
                key = first.context_cache_key(request)
                second = _Provider("model-b")
                service = ReviewService(second, work_budgets=work)
                run = service.run(
                    replace(request, head_sha="c" * 40),
                    incremental=IncrementalReviewPlan(
                        previous_key=key, reviewed_paths=()
                    ),
                )
                self.assertEqual(run.result.coverage_mode, "fallback-full")
                self.assertGreater(second.calls, 0)

    def test_legacy_default_key_is_not_upgraded_or_reused(self):
        from review_sensei.context import stage_configuration_digest

        request = self.request()
        provider = _Provider("default")
        service = ReviewService(provider)
        current = service.context_cache_key(request)
        legacy = replace(
            current, stage_digest=stage_configuration_digest(service.stages)
        )
        self.assertNotEqual(current.stage_digest, legacy.stage_digest)
        run = service.run(
            replace(request, head_sha="c" * 40),
            incremental=IncrementalReviewPlan(previous_key=legacy, reviewed_paths=()),
        )
        self.assertEqual(run.result.coverage_mode, "fallback-full")
        self.assertGreater(provider.calls, 0)
        self.assertEqual(len(current.__dict__), 10)
        self.assertEqual(
            legacy.stage_digest, stage_configuration_digest(service.stages)
        )

    def test_old_and_new_baseline_keys_round_trip_without_new_fields(self):
        from review_sensei.baseline import (
            baseline_complete_document,
            baseline_from_history_document,
            baseline_from_review,
        )
        from review_sensei.context import stage_configuration_digest
        from review_sensei.convergence import ReviewConvergencePolicy

        service = ReviewService(_Provider("model-a"))
        request = self.request()
        current = service.context_cache_key(request)
        result = service.run(request).result
        for key in (
            current,
            replace(
                current,
                stage_digest=stage_configuration_digest(service.stages),
                model="default",
            ),
        ):
            with self.subTest(model=key.model):
                baseline = baseline_from_review(
                    result, cache_key=key, policy=ReviewConvergencePolicy()
                )
                document = baseline_complete_document(baseline)
                restored = baseline_from_history_document(document)
                self.assertEqual(restored.cache_key, key)
                self.assertEqual(baseline_complete_document(restored), document)
                self.assertEqual(set(document["cache_key"]), set(current.__dict__))

    def test_invalidated_coverage_retains_same_scope_obligations(self):
        from review_sensei.context import FindingLifecycle, stage_configuration_digest

        request = self.request()
        initial = ReviewService(_Provider("model-a"))
        key = initial.context_cache_key(request)
        prior = FindingLifecycle(
            "f" * 64, "still-present", concern="e" * 64, path="a.py"
        )
        legacy = replace(key, stage_digest=stage_configuration_digest(initial.stages))
        for previous, model in ((legacy, "model-a"), (key, "model-b"), (key, None)):
            with self.subTest(model=model, legacy=previous == legacy):
                provider = _Provider(model)
                provider.allow_model_override = False
                run = ReviewService(provider).run(
                    replace(request, head_sha="c" * 40),
                    incremental=IncrementalReviewPlan(
                        previous_key=previous,
                        previous_findings=(prior,),
                        reviewed_paths=(),
                        evidence_confirmed_concerns=("e" * 64,),
                    ),
                )
                self.assertEqual(run.result.coverage_mode, "fallback-full")
                self.assertEqual(run.result.finding_lifecycles[0].state, "uncertain")
                self.assertGreater(provider.calls, 0)
                self.assertEqual(
                    run.result.finding_lifecycles[0].fingerprint, prior.fingerprint
                )
