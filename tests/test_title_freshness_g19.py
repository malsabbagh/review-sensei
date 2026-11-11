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

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.context import (
    IncrementalReviewPlan,
    ReviewContextCache,
    build_review_context_cache_key,
)
from review_sensei.errors import ReviewInputError
from review_sensei.models import ProviderRequest, ProviderResponse, ReviewRequest
from review_sensei.operational_boundaries import (
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
            pull = json.loads(response.read(524288))
            pull["title"] = self.live_title
            return json_response(pull)
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
            self.assertEqual(f_inf.selected.title, "Captured at admission")
            self.assertEqual(f_inf.provider_calls, 1)
            self.assertIn("Captured at admission", f_inf.captured_prompts[0])
            self.assertNotIn("Edited mid-inference", f_inf.captured_prompts[0])

        # Timing 3 in hosted fixture: live PR title mutates right before publication.
        with tempfile.TemporaryDirectory() as directory:
            f_pub = _TitledEntryFixture(directory, live_title="Captured at admission")

            def mutate_before_publication() -> None:
                f_pub.live_title = "Edited right before publication"

            f_pub.before_publication = mutate_before_publication
            self.assertEqual(f_pub.invoke_with_optional_title(), 0, f_pub.error)
            record = parse_session_comment(f_pub.comment["body"], identity=IDENTITY)
            assert record is not None and record.transaction is not None
            self.assertEqual(record.transaction.phase, "publication_succeeded")

        attribution = verify_published_title_attribution(
            evaluated_title="Captured at admission",
            live_title="Edited right before publication",
            base_sha=BASE,
            head_sha=HEAD,
        )
        self.assertFalse(attribution.evaluates_live_title)
        self.assertFalse(attribution.invalidates_settled_approval)
        self.assertIn(
            "later PR title edits were not analyzed", attribution.attribution_note
        )

        same_attr = verify_published_title_attribution(
            evaluated_title="Captured at admission",
            live_title="Captured at admission",
            base_sha=BASE,
            head_sha=HEAD,
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
            self.assertIsNotNone(cache.get(key_edited))
            self.assertEqual(
                service.context_cache_key(req_initial),
                service.context_cache_key(req_edited),
            )

            decision = evaluate_title_freshness(
                phase="after_publication",
                captured_title="Initial title v1",
                live_title=edited_title,
            )
            self.assertTrue(decision.incremental_cache_valid)
            self.assertFalse(decision.requires_paid_reinference)
            self.assertFalse(decision.invalidates_settled_approval)

            # Incremental pass on a successor head after title edit reuses baseline with 0 calls!
            req_next_head = replace(req_edited, head_sha="e" * 40)
            incremental = IncrementalReviewPlan(
                previous_key=key_initial,
                reviewed_paths=(),
                context_complete=True,
                generation=1,
            )
            inc_run = service.run(req_next_head, incremental=incremental)
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

        # Also prove full hosted `rescan` recovery on EntryFixture after PR title rename!
        with tempfile.TemporaryDirectory() as directory:
            hosted, _source = partial_fixture(directory)
            hosted.provider_comments = []
            original_ext = hosted.external

            def renamed_external(req_obj: Any, timeout: Any) -> Any:
                resp = original_ext(req_obj, timeout)
                if req_obj.full_url.endswith("/pulls/7"):
                    pull = json.loads(resp.read(524288))
                    pull["title"] = "Renamed PR title for hosted rescan"
                    return json_response(pull)
                return resp

            hosted.external = renamed_external  # type: ignore[method-assign]
            request(hosted, 15, "rescan")
            self.assertEqual(hosted.invoke(), 0, hosted.error)
            settled = parse_session_comment(hosted.comment["body"], identity=IDENTITY)
            assert settled is not None and settled.transaction is not None
            self.assertEqual(settled.transaction.phase, "publication_succeeded")
            self.assertEqual(
                hosted.selected.title, "Renamed PR title for hosted rescan"
            )
            self.assertEqual(len(settled.dispositions), 20)

    def test_title_freshness_validation_guards_fail_closed(self) -> None:
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
