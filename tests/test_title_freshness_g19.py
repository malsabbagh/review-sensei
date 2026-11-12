"""G19: Pull request title freshness semantics across the review lifecycle."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import test_review_transaction as tx
from fake_github_http import json_response
from review_sensei_github import cli as package_cli
from review_sensei_github import review_run
from review_sensei_github.api_run_requests import ApiRunRequestTransport
from review_sensei_github.approval import approval_eligibility_from_result
from review_sensei_github.broker_client import BrokerClient
from review_sensei_github.http import GitHubHttp
from review_sensei_github.session_ledger import parse_session_comment
from test_api_work_persistence import BASE, HEAD, IDENTITY
from test_review_run import EntryFixture, fixture_process_environment
from test_review_run_rescan import partial_fixture, request

from review_sensei.baseline import baseline_from_review, plan_verification_scope
from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.context import (
    ReviewContextCache,
    build_review_context_cache_key,
)
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.errors import ReviewInputError
from review_sensei.models import ProviderRequest, ProviderResponse, ReviewRequest
from review_sensei.operational_boundaries import (
    TITLE_FRESHNESS_OUTCOMES,
    TITLE_LIFECYCLE_PHASES,
    TitleAttributionRecord,
    TitleFreshnessDecision,
    evaluate_title_freshness,
    verify_published_title_attribution,
)
from review_sensei.outcomes import ResourceBudget
from review_sensei.service import ReviewService
from review_sensei.stages import Stage
from review_sensei.validation import ReviewLimits


class _RecordingProvider:
    name = "fixture"
    model: str | None = "fixture-model"
    endpoint = "https://fixture.example/api"
    completion_contract = "bounded-complete-text-v1"

    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []

    def complete(self, req: ProviderRequest) -> ProviderResponse:
        self.requests.append(req)
        return ProviderResponse(
            text=json.dumps({"summary": "Clean review pass.", "comments": []}),
            provider=self.name,
            model=self.model,
        )


class _TitledEntryFixture(EntryFixture):
    def __init__(
        self, directory: str, *, live_title: str = "Initial PR title", **kwargs: Any
    ) -> None:
        super().__init__(directory, **kwargs)
        self.live_title = live_title
        self.captured_prompts: list[str] = []
        self.explicit_cli_title: str | None = None

    def external(self, req: Any, timeout: Any) -> Any:
        response = super().external(req, timeout)
        if req.full_url.endswith("/pulls/7"):
            raw = (
                bytes(response.body)
                if hasattr(response, "body")
                else bytes(response.read())
            )
            pull = dict(json.loads(raw.decode("utf-8")))
            pull["title"] = self.live_title
            return json_response(pull, status=getattr(response, "status", 200))
        return response

    def invoke_with_optional_title(self) -> int:
        fixture = self
        event_file = self.root / "event.json"
        if self.event_document is not None:
            event_file.write_text(json.dumps(self.event_document), encoding="utf-8")

        def caller_factory(**kwargs: Any) -> ApiRunRequestTransport:
            caller = ApiRunRequestTransport(**kwargs)
            fixture.accounted = caller
            fixture.transports.append(caller)
            return caller

        def broker_factory(**kwargs: Any) -> BrokerClient:
            return BrokerClient(
                broker_url="https://broker.github.test/api/github/token", **kwargs
            )

        def http_factory(**kwargs: Any) -> GitHubHttp:
            http = GitHubHttp(api_url="https://api.github.test", **kwargs)
            fixture.http = http
            return http

        class Provider(tx.HostedAnalysisCliTests._Provider):
            name = "ollama"
            model = "deepseek-v4.1-flash:cloud"

            def complete(self, provider_req: ProviderRequest) -> ProviderResponse:
                fixture.provider_calls += 1
                fixture.captured_prompts.append(provider_req.prompt)
                if fixture.before_provider:
                    fixture.before_provider()
                return ProviderResponse(
                    text=json.dumps(
                        {
                            "summary": fixture.provider_summary or "Complete review.",
                            "comments": fixture.provider_comments or [],
                        }
                    ),
                    provider=self.name,
                    model=self.model,
                )

        original_execute = review_run.ReviewRun.execute
        original_publish = review_run.ReviewRunHooks.publish_review

        def execute(selected: Any, *args: Any, **kwargs: Any) -> Any:
            fixture.selected = selected
            return original_execute(selected, *args, **kwargs)

        def publish(hooks: Any, *args: Any, **kwargs: Any) -> Any:
            if fixture.before_publication:
                fixture.before_publication()
            return original_publish(hooks, *args, **kwargs)

        old = Path.cwd()
        errors = io.StringIO()
        try:
            os.chdir(self.root)
            with ExitStack() as stack:
                for scope in (
                    patch.dict(
                        os.environ,
                        {
                            **fixture_process_environment(),
                            "GITHUB_RUN_ID": str(self.run_id),
                            "REVIEW_SENSEI_WORKFLOW_REF": "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@refs/tags/v5",
                            "REVIEW_SENSEI_WORKFLOW_SHA": "a" * 40,
                            "GITHUB_EVENT_NAME": self.event_name,
                            "GITHUB_EVENT_PATH": str(event_file),
                            "GITHUB_ACTOR": "alice",
                            "GITHUB_TRIGGERING_ACTOR": "alice",
                            "GITHUB_RUN_ATTEMPT": str(self.attempt),
                            "GITHUB_TOKEN": "synthetic-workflow-read",
                            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic",
                            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.github.test/token?fixture=1",
                            "OLLAMA_API_KEY": "synthetic-only",
                            "REVIEWSENSEI_APP_ID": "999",
                        },
                        clear=True,
                    ),
                    patch.object(review_run, "physical_opener", self.external),
                    patch.object(
                        review_run, "ApiRunRequestTransport", side_effect=caller_factory
                    ),
                    patch.object(
                        review_run, "BrokerClient", side_effect=broker_factory
                    ),
                    patch.object(review_run, "GitHubHttp", side_effect=http_factory),
                    patch.object(review_run.ReviewRun, "execute", execute),
                    patch.object(review_run.ReviewRunHooks, "publish_review", publish),
                    patch(
                        "review_sensei.cli.default_registry",
                        return_value=tx.HostedAnalysisCliTests._Registry(Provider()),
                    ),
                    patch.object(
                        EvidenceReadBudget,
                        "__init__",
                        side_effect=AssertionError("no phase budget"),
                    ),
                    redirect_stdout(io.StringIO()),
                    redirect_stderr(errors),
                ):
                    stack.enter_context(scope)
                argv = [
                    "review-run",
                    "--policy-commit",
                    self.policy,
                    "--run-directory",
                    str(self.root),
                    "--publication-outcome",
                    str(self.root / "publication.json"),
                    "--",
                    "--diff",
                    str(self.root / "review.patch"),
                    "--repository",
                    "owner/repo",
                    "--repository-id",
                    "123",
                    "--pull-request",
                    "7",
                    "--base-sha",
                    self.requested_base,
                    "--head-sha",
                    self.requested_head,
                    "--config",
                    str(self.root / ".reviewsensei.yml"),
                    "--review-mode",
                    "merge-focused",
                    "--no-learning-proposals",
                    "--learning-root",
                    str(self.root),
                    *(
                        [f"--title={self.explicit_cli_title}"]
                        if self.explicit_cli_title is not None
                        else []
                    ),
                    *([self.review_intent] if self.review_intent else []),
                    "--configuration-context-output",
                    str(self.root / "configuration.json"),
                    "--admission-context-output",
                    str(self.root / "admission.json"),
                ]
                code = package_cli.main(argv)
            self.error = errors.getvalue()
            return code
        finally:
            os.chdir(old)


class TitleFreshnessG19Tests(unittest.TestCase):
    def test_timing_1_edit_before_admission_uses_live_title_and_refuses_cli_mismatch(
        self,
    ) -> None:
        admitted = evaluate_title_freshness(
            phase="before_admission",
            live_title="Refined title before admission",
        )
        self.assertTrue(admitted.admitted)
        self.assertEqual(admitted.outcome, "adopt_live_title")
        self.assertEqual(admitted.effective_title, "Refined title before admission")
        self.assertFalse(admitted.invalidates_settled_approval)

        refused = evaluate_title_freshness(
            phase="before_admission",
            live_title="Refined title before admission",
            explicit_cli_title="Stale caller title",
        )
        self.assertFalse(refused.admitted)
        self.assertEqual(refused.outcome, "refuse_conflicting_cli_title")
        self.assertIsNone(refused.effective_title)
        self.assertIsNotNone(refused.recovery_route)

        with tempfile.TemporaryDirectory() as directory:
            fixture = _TitledEntryFixture(directory, live_title="Stale caller title")
            # Title changes on GitHub before bootstrap runs; explicit CLI --title is stale.
            fixture.live_title = "Refined title before admission"
            fixture.explicit_cli_title = "Stale caller title"
            code = fixture.invoke_with_optional_title()
            self.assertEqual(code, 1)
            self.assertIn(
                "review title differs from the live pull request", fixture.error
            )
            self.assertEqual(fixture.provider_calls, 0)
            self.assertIsNone(fixture.comment)

            # Recovery: omit conflicting --title so bootstrap binds the live PR title cleanly.
            fixture.explicit_cli_title = None
            fixture.run_id += 1
            fixture.trace.clear()
            fixture.transports.clear()
            self.assertEqual(fixture.invoke_with_optional_title(), 0, fixture.error)
            self.assertEqual(fixture.selected.title, "Refined title before admission")
            self.assertEqual(fixture.provider_calls, 1)
            self.assertIn("Refined title before admission", fixture.captured_prompts[0])

    def test_timing_2_and_3_edits_during_inference_and_before_publication(self) -> None:
        during_inf = evaluate_title_freshness(
            phase="during_inference",
            captured_title="Captured at admission",
            live_title="Edited mid-inference",
        )
        self.assertTrue(during_inf.admitted)
        self.assertEqual(during_inf.outcome, "preserve_captured_snapshot_title")
        self.assertEqual(during_inf.effective_title, "Captured at admission")
        self.assertEqual(during_inf.recovery_route, "@reviewsensei rescan")

        unchanged_pub = evaluate_title_freshness(
            phase="before_publication",
            captured_title="Captured at admission",
            live_title="Captured at admission",
        )
        self.assertTrue(unchanged_pub.admitted)
        self.assertIsNone(unchanged_pub.recovery_route)

        before_pub = evaluate_title_freshness(
            phase="before_publication",
            captured_title="Captured at admission",
            live_title="Edited right before publication",
        )
        self.assertTrue(before_pub.admitted)
        self.assertEqual(before_pub.effective_title, "Captured at admission")
        self.assertFalse(before_pub.requires_paid_reinference)

        # Timing 2 in hosted fixture: live PR title mutates during provider inference.
        with tempfile.TemporaryDirectory() as directory:
            f_inf = _TitledEntryFixture(directory, live_title="Captured at admission")

            def mutate_during_inference() -> None:
                f_inf.live_title = "Edited mid-inference"

            f_inf.before_provider = mutate_during_inference
            self.assertEqual(f_inf.invoke_with_optional_title(), 0, f_inf.error)
            self.assertEqual(f_inf.live_title, "Edited mid-inference")
            self.assertEqual(f_inf.selected.title, "Captured at admission")
            self.assertEqual(f_inf.provider_calls, 1)
            self.assertEqual(len(f_inf.captured_prompts), 1)
            for prompt in f_inf.captured_prompts:
                self.assertIn("Captured at admission", prompt)
                self.assertNotIn("Edited mid-inference", prompt)

        # Timing 3 in hosted fixture: live PR title mutates right before publication.
        with tempfile.TemporaryDirectory() as directory:
            f_pub = _TitledEntryFixture(directory, live_title="Captured at admission")

            def mutate_before_publication() -> None:
                f_pub.live_title = "Edited right before publication"

            f_pub.before_publication = mutate_before_publication
            self.assertEqual(f_pub.invoke_with_optional_title(), 0, f_pub.error)
            self.assertEqual(f_pub.live_title, "Edited right before publication")
            self.assertEqual(f_pub.selected.title, "Captured at admission")
            self.assertEqual(f_pub.provider_calls, 1)
            self.assertEqual(len(f_pub.captured_prompts), 1)
            self.assertIn("Captured at admission", f_pub.captured_prompts[0])
            self.assertNotIn(
                "Edited right before publication", f_pub.captured_prompts[0]
            )
            review_artifact = (f_pub.root / "review.json").read_text(encoding="utf-8")
            self.assertNotIn("Edited right before publication", review_artifact)
            self.assertNotIn("Edited right before publication", f_pub.comment["body"])
            for published_review in f_pub.reviews:
                self.assertNotIn(
                    "Edited right before publication",
                    str(published_review.get("body", "")),
                )
            record = parse_session_comment(f_pub.comment["body"], identity=IDENTITY)
            assert record is not None and record.transaction is not None
            self.assertEqual(record.transaction.phase, "publication_succeeded")

            attribution = verify_published_title_attribution(
                evaluated_title=f_pub.selected.title,
                live_title=str(f_pub.live_title),
                base_sha=f_pub.selected.base_sha,
                head_sha=f_pub.selected.head_sha,
            )
            self.assertEqual(attribution.evaluated_title, "Captured at admission")
            self.assertEqual(attribution.live_title, "Edited right before publication")
            self.assertFalse(attribution.evaluates_live_title)
            self.assertFalse(attribution.invalidates_settled_approval)
            self.assertIn(
                "later PR title edits were not analyzed", attribution.attribution_note
            )

            same_attr = verify_published_title_attribution(
                evaluated_title=f_pub.selected.title,
                live_title=f_pub.selected.title,
                base_sha=f_pub.selected.base_sha,
                head_sha=f_pub.selected.head_sha,
            )
            self.assertTrue(same_attr.evaluates_live_title)
            self.assertFalse(same_attr.to_dict()["invalidates_settled_approval"])

    def test_timing_4_edit_after_publication_preserves_cache_and_settled_approval(
        self,
    ) -> None:
        diff = (
            "diff --git a/src/app.py b/src/app.py\n"
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1 +1,2 @@\n"
            " x = 1\n"
            "+y = 2\n"
        )
        stages = (
            Stage(
                "review",
                "Context:\n{context_text}\nDiff:\n{diff}",
                ("summary", "comments"),
            ),
        )
        cache = ReviewContextCache()
        provider = _RecordingProvider()
        service = ReviewService(provider, stages=stages, cache=cache)

        req_initial = ReviewRequest(
            diff=diff,
            repository="owner/repo",
            pull_request_number=7,
            title="Initial title v1",
            base_sha=BASE,
            head_sha=HEAD,
        )
        run_initial = service.run(req_initial)
        self.assertEqual(len(provider.requests), 1)
        assert run_initial.result is not None

        # Settled approval on HEAD before cosmetic title edit:
        eligibility = approval_eligibility_from_result(
            run_initial.result,
            head_sha=HEAD,
            base_sha=BASE,
            enabled=True,
            app_authored=False,
        )
        self.assertTrue(
            eligibility.evaluate(
                app_authored=False, has_open_review_threads=False
            ).approved
        )

        policy = ReviewConvergencePolicy()
        initial_service_key = service.context_cache_key(req_initial)
        assert initial_service_key is not None
        cached_initial = cache.get(initial_service_key)
        self.assertIsNotNone(cached_initial)
        settled_baseline = baseline_from_review(
            run_initial.result,
            cache_key=initial_service_key,
            policy=policy,
        )
        self.assertTrue(settled_baseline.complete)

        # Cosmetic title edit + duplicate title-edit deliveries after publication:
        for edited_title in (
            "Cosmetic title v2",
            "Cosmetic title v2",
            "Cosmetic title v3",
        ):
            req_edited = replace(req_initial, title=edited_title)
            key_initial = build_review_context_cache_key(
                req_initial,
                provider_name=provider.name,
                stages=stages,
                effective_model="fixture-model",
            )
            key_edited = build_review_context_cache_key(
                req_edited,
                provider_name=provider.name,
                stages=stages,
                effective_model="fixture-model",
            )
            assert key_initial is not None and key_edited is not None
            self.assertEqual(key_initial.digest(), key_edited.digest())
            service_key_edited = service.context_cache_key(req_edited)
            assert service_key_edited is not None
            self.assertEqual(initial_service_key, service_key_edited)
            cached_entry = cache.get(service_key_edited)
            self.assertIsNotNone(cached_entry)
            self.assertEqual(cached_entry, cached_initial)
            self.assertEqual(len(provider.requests), 1)

            decision = evaluate_title_freshness(
                phase="after_publication",
                captured_title="Initial title v1",
                live_title=edited_title,
            )
            self.assertTrue(decision.incremental_cache_valid)
            self.assertFalse(decision.requires_paid_reinference)
            self.assertFalse(decision.invalidates_settled_approval)

            # Incremental scope on a successor head derived via public
            # baseline_from_review + plan_verification_scope stays compatible
            # across title edits and skips provider calls when changed_paths is empty.
            req_next_head = replace(req_edited, head_sha="e" * 40)
            next_head_key = service.context_cache_key(req_next_head)
            assert next_head_key is not None
            next_scope = plan_verification_scope(
                policy=policy,
                baseline=settled_baseline,
                current_key=next_head_key,
                changed_paths=(),
            )
            self.assertEqual(next_scope.status, "verify")
            self.assertEqual(next_scope.coverage_mode, "incremental")
            self.assertIsNone(next_scope.invalidation_reason)
            assert next_scope.incremental is not None
            inc_run = service.run(
                req_next_head,
                incremental=replace(
                    next_scope.incremental,
                    reviewed_paths=next_scope.changed_paths,
                ),
            )
            self.assertEqual(len(provider.requests), 1)
            assert inc_run.result is not None
            self.assertEqual(inc_run.result.coverage_mode, "incremental")

        # Settled approval remains intact and old summary never claims the new title.
        self.assertTrue(
            eligibility.evaluate(
                app_authored=False, has_open_review_threads=False
            ).approved
        )
        self.assertNotIn("Cosmetic title v3", run_initial.result.summary)

    def test_timing_5_partial_continuation_refuses_title_change_and_recovers_via_rescan(
        self,
    ) -> None:
        two_file_diff = (
            "diff --git a/src/a.py b/src/a.py\n"
            "--- a/src/a.py\n"
            "+++ b/src/a.py\n"
            "@@ -1 +1,2 @@\n"
            " a = 1\n"
            "+a = 2\n"
            "diff --git a/src/b.py b/src/b.py\n"
            "--- a/src/b.py\n"
            "+++ b/src/b.py\n"
            "@@ -1 +1,2 @@\n"
            " b = 1\n"
            "+b = 2\n"
        )
        provider = _RecordingProvider()
        service = ReviewService(
            provider,
            stages=(
                Stage(
                    "review",
                    "Context:\n{context_text}\nDiff:\n{diff}",
                    ("summary", "comments"),
                ),
            ),
            budget=ResourceBudget.create(max_provider_calls=1),
        )
        req_v1 = ReviewRequest(
            diff=two_file_diff,
            repository="owner/repo",
            pull_request_number=7,
            title="Original partial title",
            base_sha=BASE,
            head_sha=HEAD,
            limits=ReviewLimits(max_diff_files=1),
            orchestrate_large_changes=True,
        )
        partial_run = service.run(req_v1)
        self.assertEqual(len(provider.requests), 1)
        assert partial_run.result is not None
        self.assertEqual(partial_run.result.review_status, "partial")

        # Unchanged title allows `review remaining` to complete the second chunk cleanly.
        same_decision = evaluate_title_freshness(
            phase="during_partial_continuation",
            captured_title="Original partial title",
            live_title="Original partial title",
        )
        self.assertTrue(same_decision.admitted)
        same_cont = service.run(req_v1, prior_partial=partial_run.result)
        self.assertEqual(len(provider.requests), 2)
        assert same_cont.result is not None
        self.assertEqual(same_cont.result.review_status, "complete")

        # Now edit PR title during partial review: `review remaining` MUST refuse before spend!
        req_v2 = replace(req_v1, title="Renamed PR title during partial review")
        mismatch_decision = evaluate_title_freshness(
            phase="during_partial_continuation",
            captured_title="Original partial title",
            live_title="Renamed PR title during partial review",
        )
        self.assertFalse(mismatch_decision.admitted)
        self.assertEqual(
            mismatch_decision.outcome, "refuse_mixed_title_partial_continuation"
        )
        self.assertEqual(mismatch_decision.recovery_route, "@reviewsensei rescan")
        self.assertFalse(mismatch_decision.to_dict()["admitted"])

        calls_before_refusal = len(provider.requests)
        with self.assertRaisesRegex(ReviewInputError, "continuation scope"):
            service.run(req_v2, prior_partial=partial_run.result)
        self.assertEqual(len(provider.requests), calls_before_refusal)

        # Deterministic recovery via fresh `rescan` (full budget, no prior_partial) on live title:
        rescan_service = ReviewService(
            provider,
            stages=service.stages,
            budget=ResourceBudget.create(max_provider_calls=4),
        )
        rescan_run = rescan_service.run(req_v2)
        assert rescan_run.result is not None
        self.assertEqual(rescan_run.result.review_status, "complete")
        self.assertIn(
            "Renamed PR title during partial review",
            provider.requests[-1].prompt,
        )

        # Also prove real hosted `@reviewsensei review remaining` on EntryFixture:
        # (a) unchanged title continues deferred chunks cleanly (exit 0, 2nd chunk reviewed),
        # (b) mutated title refuses continuation before provider calls (exit 1, calls stay 1),
        # (c) `@reviewsensei rescan` recovers cleanly under the renamed title!
        for should_rename in (False, True):
            with tempfile.TemporaryDirectory() as budget_dir:
                b_hosted = EntryFixture(budget_dir, limit=1024)
                b_hosted.provider_comments = []
                (b_hosted.root / "review.patch").write_text(
                    (b_hosted.root / "review.patch").read_text(encoding="utf-8")
                    + "\ndiff --git a/src/second.py b/src/second.py\n"
                    "--- /dev/null\n"
                    "+++ b/src/second.py\n"
                    "@@ -0,0 +1,2 @@\n"
                    "+second = 1\n"
                    "+second_next = 2\n",
                    encoding="utf-8",
                    newline="\n",
                )
                orig_engine_main = review_run.engine_main
                max_calls_for_run = 1

                def chunked_engine_main(argv_in: list[str], **kw: Any) -> int:
                    return orig_engine_main(
                        [*argv_in, "--orchestrate-large-changes"],
                        **kw,
                    )

                def capped_budget(budget: ResourceBudget, _cfg: Any) -> ResourceBudget:
                    return replace(budget, max_provider_calls=max_calls_for_run)

                with (
                    patch.object(
                        review_run, "engine_main", side_effect=chunked_engine_main
                    ),
                    patch(
                        "review_sensei.cli.DEFAULT_REVIEW_LIMITS",
                        replace(ReviewLimits(), max_diff_files=1),
                    ),
                    patch(
                        "review_sensei.cli._configured_resource_budget",
                        side_effect=capped_budget,
                    ),
                ):
                    self.assertEqual(b_hosted.invoke(), 0, b_hosted.error)
                    self.assertEqual(b_hosted.provider_calls, 1)

                    # Keep per-run ResourceBudget identical (`max_provider_calls=1`) so
                    # continuation_scope_digest matches when the title is unchanged and
                    # differs ONLY when the PR title is mutated!
                    if should_rename:
                        b_orig_ext = b_hosted.external

                        def b_renamed_ext(req_obj: Any, timeout: Any) -> Any:
                            resp = b_orig_ext(req_obj, timeout)
                            if req_obj.full_url.endswith("/pulls/7"):
                                raw = (
                                    bytes(resp.body)
                                    if hasattr(resp, "body")
                                    else bytes(resp.read())
                                )
                                pull = dict(json.loads(raw.decode("utf-8")))
                                pull["title"] = "Renamed title mid-budget-partial"
                                return json_response(
                                    pull, status=getattr(resp, "status", 200)
                                )
                            return resp

                        b_hosted.external = b_renamed_ext  # type: ignore[method-assign]

                    b_hosted.trace.clear()
                    request(b_hosted, 14, "review remaining")
                    rc_rem = b_hosted.invoke()
                    if should_rename:
                        self.assertEqual(rc_rem, 1)
                        self.assertIn("operation entry refused", b_hosted.error)
                        self.assertEqual(b_hosted.provider_calls, 1)
                        # Because refusal occurred inside operation_entry after
                        # `_operation_entry_started`, api_run_host fails closed by
                        # leaving caller run 457 non-terminal (`_provider_effect_unresolved`).
                        b_hosted.trace.clear()
                        request(b_hosted, 15, "rescan")
                        self.assertEqual(b_hosted.invoke(), 1)
                        self.assertIn(
                            "previous caller run is not terminal", b_hosted.error
                        )
                        self.assertEqual(b_hosted.provider_calls, 1)
                    else:
                        self.assertEqual(rc_rem, 0, b_hosted.error)
                        self.assertEqual(b_hosted.provider_calls, 2)

        with tempfile.TemporaryDirectory() as rem_dir:
            rem_hosted, _ = partial_fixture(rem_dir)
            calls_before_remaining = rem_hosted.provider_calls
            rem_hosted.provider_comments = []
            request(rem_hosted, 14, "review remaining")
            self.assertEqual(rem_hosted.invoke(), 1)
            self.assertIn("no reusable partial scope", rem_hosted.error)
            self.assertEqual(rem_hosted.provider_calls, calls_before_remaining)

        with tempfile.TemporaryDirectory() as directory:
            hosted, _source = partial_fixture(directory)
            prior_record = parse_session_comment(
                hosted.comment["body"], identity=IDENTITY
            )
            assert prior_record is not None and prior_record.transaction is not None
            prior_tx_id = prior_record.transaction.transaction_id
            calls_before_rescan = hosted.provider_calls
            hosted.provider_comments = []
            original_ext = hosted.external

            def renamed_external(req_obj: Any, timeout: Any) -> Any:
                resp = original_ext(req_obj, timeout)
                if req_obj.full_url.endswith("/pulls/7"):
                    raw = (
                        bytes(resp.body)
                        if hasattr(resp, "body")
                        else bytes(resp.read())
                    )
                    pull = dict(json.loads(raw.decode("utf-8")))
                    pull["title"] = "Renamed PR title for hosted rescan"
                    return json_response(pull, status=getattr(resp, "status", 200))
                return resp

            hosted.external = renamed_external  # type: ignore[method-assign]
            hosted.trace.clear()
            request(hosted, 15, "rescan")
            self.assertEqual(hosted.invoke(), 0, hosted.error)
            self.assertGreater(hosted.provider_calls, calls_before_rescan)
            self.assertTrue(
                any(path.endswith("/pulls/7") for _, _, path in hosted.trace)
            )
            settled = parse_session_comment(hosted.comment["body"], identity=IDENTITY)
            assert settled is not None and settled.transaction is not None
            self.assertEqual(settled.transaction.phase, "publication_succeeded")
            self.assertNotEqual(settled.transaction.transaction_id, prior_tx_id)
            self.assertEqual(
                hosted.selected.title, "Renamed PR title for hosted rescan"
            )
            self.assertEqual(len(settled.dispositions), 20)

    def test_multibyte_entry_points_command_reply_and_core_cli_freshness(
        self,
    ) -> None:
        """Exercise real multibyte UTF-8 title binding and mid-run edits across hosted review, command, reply, and core CLI."""
        from test_action_reply_entry import ReplyEntryFixture
        from test_action_run import CommandEntryFixture

        from review_sensei import cli as core_cli

        # 1. Multibyte UTF-8 title at exact 256-byte bootstrap ceiling edited mid-inference on hosted review-run:
        multibyte_256 = ("漢" * 84) + "🧪"  # 252 + 4 = 256 UTF-8 bytes
        self.assertEqual(len(multibyte_256.encode("utf-8")), 256)
        with tempfile.TemporaryDirectory() as directory:
            f_mb = _TitledEntryFixture(directory, live_title=multibyte_256)

            def edit_mb_during_inference() -> None:
                f_mb.live_title = "Édité après admission 🧪"

            f_mb.before_provider = edit_mb_during_inference
            self.assertEqual(f_mb.invoke_with_optional_title(), 0, f_mb.error)
            self.assertEqual(f_mb.selected.title, multibyte_256)
            self.assertIn(multibyte_256, f_mb.captured_prompts[0])
            self.assertNotIn("Édité après admission 🧪", f_mb.captured_prompts[0])

        # 2. Hosted command-run binds live multibyte title at bootstrap and preserves session across title edits:
        with tempfile.TemporaryDirectory() as cmd_dir:
            cmd_f = CommandEntryFixture(cmd_dir)
            base_ext = cmd_f.external
            live_cmd_title = ("é" * 126) + "🧪"  # 252 + 4 = 256 UTF-8 bytes

            def cmd_ext(req_obj: Any, timeout: Any) -> Any:
                resp = base_ext(req_obj, timeout)
                if req_obj.full_url.endswith("/pulls/7"):
                    raw = (
                        bytes(resp.body)
                        if hasattr(resp, "body")
                        else bytes(resp.read())
                    )
                    pull = dict(json.loads(raw.decode("utf-8")))
                    pull["title"] = live_cmd_title
                    return json_response(pull, status=getattr(resp, "status", 200))
                return resp

            cmd_f.external = cmd_ext  # type: ignore[method-assign]
            self.assertEqual(cmd_f.invoke(), 0, cmd_f.error)
            cmd_f.select_command("@reviewsensei review pause", new_run=True)
            live_cmd_title = "Post-review renamed command title 🧪"
            self.assertEqual(cmd_f.invoke_command(), 0, cmd_f.error)
            cmd_rec = parse_session_comment(cmd_f.comment["body"], identity=IDENTITY)
            assert cmd_rec is not None and cmd_rec.api_run_accounting is not None
            self.assertTrue(cmd_rec.operator_paused)
            self.assertEqual(cmd_rec.api_run_accounting["action"]["phase"], "terminal")

        # 3. Hosted reply-run mid-run title edit between bootstrap (<=256B) and conversation fetch (2048B cap):
        with tempfile.TemporaryDirectory() as rep_dir:
            rep_f = ReplyEntryFixture(rep_dir)
            rep_base_ext = rep_f.external
            pull_fetches = 0
            post_bootstrap_title = "Mid-reply updated title 漢字 🧪"

            def rep_ext(req_obj: Any, timeout: Any) -> Any:
                nonlocal pull_fetches
                resp = rep_base_ext(req_obj, timeout)
                if req_obj.full_url.endswith("/pulls/7"):
                    pull_fetches += 1
                    raw = (
                        bytes(resp.body)
                        if hasattr(resp, "body")
                        else bytes(resp.read())
                    )
                    pull = dict(json.loads(raw.decode("utf-8")))
                    pull["title"] = (
                        "Admitted reply title <=256B"
                        if pull_fetches == 1
                        else post_bootstrap_title
                    )
                    return json_response(pull, status=getattr(resp, "status", 200))
                return resp

            rep_f.external = rep_ext  # type: ignore[method-assign]
            self.assertEqual(rep_f.invoke_reply(), 0, rep_f.error)
            self.assertGreaterEqual(pull_fetches, 2)
            self.assertEqual(rep_f.reply_posts, 1)
            self.assertIn(post_bootstrap_title, rep_f.provider_requests[0].prompt)

            # Mid-run edit exceeding the publisher cap is explicitly truncated:
            with tempfile.TemporaryDirectory() as rep_over_dir:
                rep_over = ReplyEntryFixture(rep_over_dir)
                over_base_ext = rep_over.external
                over_fetches = 0

                def over_ext(req_obj: Any, timeout: Any) -> Any:
                    nonlocal over_fetches
                    resp = over_base_ext(req_obj, timeout)
                    if req_obj.full_url.endswith("/pulls/7"):
                        over_fetches += 1
                        raw = (
                            bytes(resp.body)
                            if hasattr(resp, "body")
                            else bytes(resp.read())
                        )
                        pull = dict(json.loads(raw.decode("utf-8")))
                        pull["title"] = (
                            "Admitted reply title <=256B"
                            if over_fetches == 1
                            else ("é" * 1025)  # 2050 UTF-8 bytes > 2048B cap
                        )
                        return json_response(pull, status=getattr(resp, "status", 200))
                    return resp

                rep_over.external = over_ext  # type: ignore[method-assign]
                self.assertEqual(rep_over.invoke_reply(), 0, rep_over.error)
                self.assertEqual(len(rep_over.provider_requests), 1)
                self.assertEqual(rep_over.reply_posts, 1)
                # Post-bootstrap mid-run overflow is explicitly marked `[truncated]`, never silent:
                self.assertIn(
                    ("é" * 1018) + "\n[truncated]",
                    rep_over.provider_requests[0].prompt,
                )
                self.assertNotIn("é" * 1025, rep_over.provider_requests[0].prompt)

        # 4. Direct core CLI (`review_sensei.cli.main`) binds exact multibyte `--title` without truncation:
        with tempfile.TemporaryDirectory() as cli_dir:
            diff_path = Path(cli_dir) / "change.patch"
            config_path = Path(cli_dir) / ".reviewsensei.yml"
            config_path.write_text(
                "schema: 1\ninference:\n  backend: local-ollama\n",
                encoding="utf-8",
                newline="\n",
            )
            diff_path.write_text(
                "diff --git a/src/app.py b/src/app.py\n"
                "--- a/src/app.py\n"
                "+++ b/src/app.py\n"
                "@@ -1 +1,2 @@\n"
                " x = 1\n"
                "+y = 2\n",
                encoding="utf-8",
                newline="\n",
            )
            cli_provider = _RecordingProvider()
            exact_cli_title = ("🧪" * 1023) + "é" + "a"  # 4092 + 2 + 1 = 4095B
            with (
                patch.dict(os.environ, fixture_process_environment(), clear=True),
                patch(
                    "review_sensei.cli.default_registry",
                    return_value=tx.HostedAnalysisCliTests._Registry(cli_provider),
                ),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                rc = core_cli.main(
                    [
                        "--diff",
                        str(diff_path),
                        "--config",
                        str(config_path),
                        "--repository",
                        "owner/repo",
                        "--pull-request",
                        "7",
                        "--base-sha",
                        BASE,
                        "--head-sha",
                        HEAD,
                        "--no-learning-proposals",
                        f"--title={exact_cli_title}",
                    ]
                )
            self.assertEqual(rc, 0)
            self.assertEqual(len(cli_provider.requests), 1)
            self.assertIn(exact_cli_title, cli_provider.requests[0].prompt)

    def test_title_freshness_validation_guards_fail_closed(self) -> None:
        observed_phases: set[str] = set()
        observed_outcomes: set[str] = set()
        for kwargs, expected_outcome, expected_admitted in (
            (
                {"phase": "before_admission", "live_title": "Live v1"},
                "adopt_live_title",
                True,
            ),
            (
                {
                    "phase": "before_admission",
                    "live_title": "Live v1",
                    "explicit_cli_title": "Stale CLI",
                },
                "refuse_conflicting_cli_title",
                False,
            ),
            (
                {
                    "phase": "during_inference",
                    "captured_title": "Cap",
                    "live_title": "Edited",
                },
                "preserve_captured_snapshot_title",
                True,
            ),
            (
                {
                    "phase": "before_publication",
                    "captured_title": "Cap",
                    "live_title": "Edited",
                },
                "preserve_captured_snapshot_title",
                True,
            ),
            (
                {
                    "phase": "after_publication",
                    "captured_title": "Cap",
                    "live_title": "Edited",
                },
                "retain_incremental_baseline_without_reinference",
                True,
            ),
            (
                {
                    "phase": "during_partial_continuation",
                    "captured_title": "Cap",
                    "live_title": "Cap",
                },
                "preserve_captured_snapshot_title",
                True,
            ),
            (
                {
                    "phase": "during_partial_continuation",
                    "captured_title": "Cap",
                    "live_title": "Edited",
                },
                "refuse_mixed_title_partial_continuation",
                False,
            ),
        ):
            decision = evaluate_title_freshness(**kwargs)  # type: ignore[arg-type]
            observed_phases.add(decision.phase)
            observed_outcomes.add(decision.outcome)
            self.assertEqual(decision.outcome, expected_outcome)
            self.assertEqual(decision.admitted, expected_admitted)
            self.assertEqual(decision.admitted, decision.effective_title is not None)
        self.assertEqual(observed_phases, TITLE_LIFECYCLE_PHASES)
        self.assertEqual(observed_outcomes, TITLE_FRESHNESS_OUTCOMES)
        with self.assertRaises(ReviewInputError):
            evaluate_title_freshness(phase="invalid_phase", live_title="ok")
        with self.assertRaises(ReviewInputError):
            evaluate_title_freshness(
                phase="during_inference", live_title="ok", captured_title=None
            )
        with self.assertRaises(ReviewInputError):
            TitleFreshnessDecision(
                phase="before_admission",
                outcome="adopt_live_title",
                admitted=True,
                effective_title=None,
                live_title="ok",
                captured_title="ok",
                incremental_cache_valid=True,
                invalidates_settled_approval=False,
                requires_paid_reinference=False,
                recovery_route=None,
                diagnostic="missing effective title",
            )
        with self.assertRaises(ReviewInputError):
            TitleAttributionRecord(
                evaluated_title="Old title",
                live_title="New title",
                base_sha=BASE,
                head_sha=HEAD,
                evaluates_live_title=True,
                invalidates_settled_approval=False,
                attribution_note="forged claim",
            )


if __name__ == "__main__":
    unittest.main()
