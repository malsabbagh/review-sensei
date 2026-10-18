"""Offline runtime #115 controls; synthetic reports are not live qualification."""

import json
import unittest
from dataclasses import replace

from review_sensei_github import ReviewPublisher
from review_sensei_github.publication import (
    ReviewApprovalFinalizer,
    approval_eligibility_from_body,
)

from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.evaluation import engine_digest, prompt_digest
from review_sensei.models import (
    build_transaction_configuration_context,
    transaction_stage_identity,
)
from review_sensei.service import DEFAULT_STAGES
from tests.test_github_publication import (
    DIFF,
    clean_result,
    graphql_review_threads_response,
    json_response,
    make_http,
    posted_events,
    pr_payload,
    review_payloads,
)
from tests.test_openrouter_qualification import (
    EVALUATED_AT,
    REPRO,
    TARGET,
    _retained_artifacts,
    _supported_live_reports,
    _supported_record,
)


def runtime_context():
    stages, categories = transaction_stage_identity(DEFAULT_STAGES)
    return build_transaction_configuration_context(
        provider={
            "name": "openrouter",
            "profile": None,
            "base_url": TARGET.base_url,
            "timeout_seconds": 60,
            "max_output_tokens": 4096,
            "allow_custom_endpoint": False,
            "openrouter_policy": dict(TARGET.routing_policy().identity_fields()),
        },
        model=TARGET.model,
        stages=stages,
        category_policy=categories,
        publication_mode="merge-focused",
        orchestration_enabled=False,
    )


def synthetic_authority(**changes):
    from review_sensei_github.qualification import TrustedOpenRouterQualification

    reports = _supported_live_reports()
    artifacts = _retained_artifacts(reports)
    args = dict(
        qualification_document=json.dumps(
            _supported_record(reports, artifacts).to_dict()
        ).encode(),
        report_artifacts=artifacts,
        expected_target=TARGET,
        expected_evaluated_at=EVALUATED_AT,
        expected_reproducibility=REPRO,
        expected_engine_digest=engine_digest(),
        expected_prompt_digest=prompt_digest(),
        expected_evaluation_configuration_digest="c" * 64,
        expected_corpus_digest="d" * 64,
        runtime_configuration=runtime_context(),
    )
    args.update(changes)
    return TrustedOpenRouterQualification(**args)


class TrustedQualificationTests(unittest.TestCase):
    def publish(
        self,
        *,
        authority=True,
        context=True,
        responses=None,
        readback=True,
        checks=None,
        **overrides,
    ):
        http, calls = make_http(
            responses
            if responses is not None
            else [
                json_response(pr_payload(head_sha="b" * 40)),
                json_response([]),
                json_response(pr_payload(head_sha="b" * 40)),
                json_response({"id": 5}, 200),
                json_response(pr_payload(head_sha="b" * 40)),
                graphql_review_threads_response(),
                json_response(pr_payload(head_sha="b" * 40)),
                json_response([]),
                json_response({"id": 6}, 200),
            ],
            review_readback=readback,
            routes=() if checks is None else ((checks.matches, checks.respond),),
        )
        args = dict(
            token="token",
            repository="owner/repo",
            repository_id=1,
            pull_request=2,
            head_sha="b" * 40,
            base_branch="main",
            base_sha="a" * 40,
            result=replace(clean_result(), provider="openrouter", model=TARGET.model),
            diff=DIFF,
            app_slug="reviewsensei[bot]",
            convergence_policy=ReviewConvergencePolicy(mode="merge-focused"),
            allow_retired_legacy_policy=True,
            configuration_context=runtime_context() if context is True else context,
        )
        if checks is not None:
            args["check_token"] = "check-token"
        args.update(overrides)
        publisher = ReviewPublisher(
            http=http,
            qualification_authority=synthetic_authority()
            if authority is True
            else authority,
        )
        return publisher.publish(**args), calls

    def test_checked_matching_retained_evidence_can_approve(self):
        outcome, calls = self.publish()
        self.assertEqual(outcome.status, "published")
        self.assertEqual(posted_events(calls), ["COMMENT", "APPROVE"])
        eligibility = approval_eligibility_from_body(review_payloads(calls)[0]["body"])
        self.assertEqual(eligibility.facts.provider, "openrouter")
        self.assertEqual(eligibility.facts.qualification, "qualified")
        self.assertEqual(len(eligibility.facts.qualification_binding), 64)
        self.assertNotIn("live-1", json.dumps(eligibility.to_dict()))

    def test_legacy_accepting_facts_cannot_authorize_delayed_finalization(self):
        from tests.test_github_publication import eligibility_document

        original = eligibility_document(head_sha="b" * 40).to_dict()
        original["facts"]["schema_version"] = "1"
        original["facts"].pop("provider", None)
        original["facts"].pop("qualification_binding", None)
        from review_sensei_github.approval import ReviewApprovalEligibility

        eligibility = ReviewApprovalEligibility.from_dict(original)
        http, calls = make_http([])
        final = ReviewApprovalFinalizer(http=http).finalize(
            token="token",
            repository="owner/repo",
            pull_request=2,
            head_sha="b" * 40,
            app_slug="reviewsensei[bot]",
            eligibility=eligibility,
        )
        self.assertEqual(final.status, "approval_withheld")
        self.assertEqual(calls, [])

    def test_missing_or_mismatched_runtime_context_and_caller_claims_withhold(self):
        for authority, context, claim in (
            (None, True, "qualified"),
            (True, None, "qualified"),
            (True, {}, "not-required"),
            (True, {**runtime_context(), "model": TARGET.model + ":free"}, "qualified"),
        ):
            with self.subTest(authority=authority, context=context, claim=claim):
                outcome, calls = self.publish(
                    authority=authority, context=context, qualification=claim
                )
                self.assertEqual(outcome.status, "published")
                self.assertEqual(posted_events(calls), ["COMMENT"])
                self.assertEqual(outcome.diagnostic, "check_permission")
                eligibility = approval_eligibility_from_body(
                    review_payloads(calls)[0]["body"]
                )
                self.assertNotEqual(eligibility.facts.qualification, "qualified")
                self.assertIsNone(eligibility.facts.qualification_binding)

    def test_authority_rejects_missing_duplicate_fixture_and_mutated_evidence(self):
        from review_sensei.errors import ReviewInputError
        from tests.test_openrouter_qualification import _make_openrouter_report

        reports = _supported_live_reports()
        artifacts = _retained_artifacts(reports)
        fixture_reports = [
            _make_openrouter_report(mode="fixture", elapsed_total_ms=i)
            for i in (1, 2, 3)
        ]
        for changes in (
            {"report_artifacts": artifacts[:2]},
            {"report_artifacts": [artifacts[0]] * 3},
            {"report_artifacts": _retained_artifacts(fixture_reports)},
            {"report_artifacts": [b"{}", *artifacts[1:]]},
            {"qualification_document": b"{}"},
            {"qualification_document": b"not JSON"},
            {"expected_engine_digest": "1" * 64},
            {"expected_prompt_digest": "2" * 64},
            {"expected_evaluation_configuration_digest": "3" * 64},
            {"expected_corpus_digest": "4" * 64},
            {"expected_reproducibility": {"temperature": 1}},
            {"expected_evaluated_at": "2026-09-17T00:00:00Z"},
        ):
            with self.subTest(changes=tuple(changes)):
                with self.assertRaises(ReviewInputError):
                    synthetic_authority(**changes)

    def test_runtime_target_endpoint_policy_and_suffix_do_not_inherit_support(self):
        from review_sensei.errors import ReviewInputError
        from review_sensei.openrouter_qualification import (
            CURRENT_HOSTED_OPENROUTER_QUALIFICATION_TARGET,
        )

        for field, changed in (
            ("base_url", "https://other.example.com/api/v1"),
            ("name", "compatible"),
            (
                "openrouter_policy",
                {
                    **dict(TARGET.routing_policy().identity_fields()),
                    "allow_fallbacks": True,
                },
            ),
        ):
            context = runtime_context()
            context["provider"][field] = changed
            with self.subTest(field=field), self.assertRaises(ReviewInputError):
                synthetic_authority(runtime_configuration=context)
        with self.assertRaises(ReviewInputError):
            synthetic_authority(
                runtime_configuration={
                    **runtime_context(),
                    "model": TARGET.model + ":free",
                }
            )
        with self.assertRaises(ReviewInputError):
            synthetic_authority(
                expected_target=CURRENT_HOSTED_OPENROUTER_QUALIFICATION_TARGET
            )

    def finalize(self, eligibility, *, authority=True, **changes):
        from review_sensei_github.publication import approval_eligibility_marker

        from tests.test_github_publication import published_review

        http, calls = make_http(
            [
                json_response(pr_payload(head_sha="b" * 40)),
                graphql_review_threads_response(),
                json_response(pr_payload(head_sha="b" * 40)),
                json_response(
                    [
                        published_review(
                            marker=approval_eligibility_marker(eligibility),
                            head_sha="b" * 40,
                            state="COMMENTED",
                        )
                    ]
                ),
                json_response({"id": 9}, 200),
            ]
        )
        args = dict(
            token="token",
            repository="owner/repo",
            pull_request=2,
            head_sha="b" * 40,
            app_slug="reviewsensei[bot]",
            eligibility=eligibility,
            require_persisted=True,
        )
        args.update(changes)
        finalizer = ReviewApprovalFinalizer(
            http=http,
            qualification_authority=synthetic_authority()
            if authority is True
            else authority,
        )
        return finalizer.finalize(**args), calls

    def test_separate_finalizer_rechecks_matching_retained_evidence(self):
        _, calls = self.publish()
        eligibility = approval_eligibility_from_body(review_payloads(calls)[0]["body"])
        final, calls = self.finalize(eligibility)
        self.assertEqual(final.status, "approved")
        self.assertEqual(posted_events(calls), ["APPROVE"])

    def test_delayed_finalizer_withholds_missing_changed_and_forged_binding(self):
        _, calls = self.publish()
        eligibility = approval_eligibility_from_body(review_payloads(calls)[0]["body"])
        changed_context = runtime_context()
        changed_context["provider"]["timeout_seconds"] = 30
        for authority, value, args in (
            (None, eligibility, {}),
            (
                synthetic_authority(runtime_configuration=changed_context),
                eligibility,
                {},
            ),
            (
                True,
                replace(
                    eligibility,
                    facts=replace(eligibility.facts, qualification_binding="0" * 64),
                ),
                {},
            ),
            (True, replace(eligibility, result_digest="e" * 64), {}),
            (True, eligibility, {"pull_request": 3}),
        ):
            with self.subTest(args=args, authority=authority is None):
                final, calls = self.finalize(value, authority=authority, **args)
                self.assertEqual(final.status, "approval_withheld")
                self.assertEqual(final.diagnostic, "qualification_unverified")
                self.assertEqual(posted_events(calls), [])

    def test_replay_rechecks_proof_and_does_not_upgrade_unbound_publication(self):
        from tests.test_github_publication import published_review

        _, calls = self.publish()
        body = review_payloads(calls)[0]["body"]
        for context, expected in ((True, ["APPROVE"]), (None, [])):
            responses = [
                json_response(pr_payload(head_sha="b" * 40)),
                json_response(
                    [
                        published_review(
                            marker=body, head_sha="b" * 40, state="COMMENTED"
                        )
                    ]
                ),
                json_response(pr_payload(head_sha="b" * 40)),
                graphql_review_threads_response(),
                json_response(pr_payload(head_sha="b" * 40)),
                json_response(
                    [
                        published_review(
                            marker=body, head_sha="b" * 40, state="COMMENTED"
                        )
                    ]
                ),
                json_response({"id": 8}, 200),
            ]
            outcome, calls = self.publish(context=context, responses=responses)
            self.assertEqual(outcome.status, "already_published")
            self.assertEqual(posted_events(calls), expected)
        _, calls = self.publish(authority=None)
        unbound_body = review_payloads(calls)[0]["body"]
        outcome, calls = self.publish(
            responses=[
                json_response(pr_payload(head_sha="b" * 40)),
                json_response(
                    [
                        published_review(
                            marker=unbound_body, head_sha="b" * 40, state="COMMENTED"
                        )
                    ]
                ),
            ]
        )
        self.assertEqual(outcome.status, "already_published")
        self.assertEqual(posted_events(calls), [])

    def test_new_non_openrouter_and_opt_out_behavior_remains_supported(self):
        for provider in ("ollama", "compatible"):
            outcome, calls = self.publish(
                authority=None,
                context=None,
                result=replace(clean_result(), provider=provider),
            )
            self.assertEqual(outcome.status, "published")
            self.assertEqual(posted_events(calls), ["COMMENT", "APPROVE"])
        for policy in ("advisory", "blocking"):
            outcome, calls = self.publish(reviews_policy=policy)
            self.assertEqual(outcome.status, "published")
            self.assertNotIn("APPROVE", posted_events(calls))

    def test_constructor_rejects_boolean_or_duck_typed_qualification_authority(self):
        from review_sensei.errors import ReviewInputError

        class Unchecked:
            def authorizes(self, *args, **kwargs):
                return True

        http, _ = make_http([])
        for value in (True, "qualified", Unchecked()):
            with self.subTest(value=type(value)), self.assertRaises(ReviewInputError):
                ReviewPublisher(http=http, qualification_authority=value)

    def test_application_and_model_free_recovery_use_the_same_trusted_context(self):
        from datetime import datetime, timedelta, timezone

        from review_sensei_github import GitHubApplication, GitHubWriteOptions

        from review_sensei.outcomes import RecoveryArtifact
        from tests.test_github_application import RecordingBroker
        from tests.test_github_publication import FakeCheckRuns

        result = replace(clean_result(), provider="openrouter", model=TARGET.model)
        for recovery, context in ((False, True), (True, True), (True, None)):
            with self.subTest(recovery=recovery, context=context):
                checks = FakeCheckRuns()
                http, calls = make_http(
                    [
                        json_response(pr_payload(head_sha="b" * 40)),
                        json_response([]),
                        json_response(pr_payload(head_sha="b" * 40)),
                        json_response({"id": 5}, 200),
                        json_response(pr_payload(head_sha="b" * 40)),
                        graphql_review_threads_response(),
                        json_response(pr_payload(head_sha="b" * 40)),
                        json_response([]),
                        json_response({"id": 6}, 200),
                    ],
                    routes=((checks.matches, checks.respond),),
                )
                app = GitHubApplication(
                    broker=RecordingBroker(),
                    http=http,
                    reviewer=ReviewPublisher(
                        http=http, qualification_authority=synthetic_authority()
                    ),
                    learner=None,
                    replier=None,
                )
                args = dict(
                    options=GitHubWriteOptions(github_writes=True, auto_review=True),
                    oidc_token="oidc",
                    repository="owner/repo",
                    repository_id=1,
                    pull_request=2,
                    head_sha="b" * 40,
                    base_branch="main",
                    base_sha="a" * 40,
                    diff=DIFF,
                    app_slug="reviewsensei[bot]",
                    configuration_context=runtime_context() if context else None,
                )
                if recovery:
                    args["artifact"] = RecoveryArtifact.create(
                        repository="owner/repo",
                        pull_request_number=2,
                        base_sha="a" * 40,
                        head_sha="b" * 40,
                        result=result.to_dict(),
                        expires_at=(
                            datetime.now(timezone.utc) + timedelta(hours=1)
                        ).isoformat(),
                    )
                    outcome = app.recover_review(**args)
                else:
                    args["result"] = result
                    outcome = app.publish_review(**args)
                self.assertEqual(outcome.status, "published")
                self.assertEqual(
                    posted_events(calls),
                    ["COMMENT", "APPROVE"] if context else ["COMMENT"],
                )
                self.assertEqual(checks.conclusions, [None, "success"])

    def test_named_profile_is_bound_by_effective_context_without_status_trust(self):
        context = runtime_context()
        context["provider"]["profile"] = "openrouter-sonnet"
        outcome, calls = self.publish(
            authority=synthetic_authority(runtime_configuration=context),
            context=context,
            qualification="unverified",
        )
        self.assertEqual(outcome.status, "published")
        self.assertEqual(posted_events(calls), ["COMMENT", "APPROVE"])

    def test_qualified_evidence_never_overrides_partial_or_blocking_obligations(self):
        outcome, calls = self.publish(
            result=replace(
                clean_result(),
                provider="openrouter",
                model=TARGET.model,
                review_status="partial",
            )
        )
        self.assertEqual(outcome.status, "published")
        self.assertNotIn("APPROVE", posted_events(calls))
        _, calls = self.publish()
        eligibility = approval_eligibility_from_body(review_payloads(calls)[0]["body"])
        blocked = replace(
            eligibility, facts=replace(eligibility.facts, has_blocking_findings=True)
        )
        final, calls = self.finalize(blocked)
        self.assertEqual(final.status, "approval_withheld")
        self.assertEqual(posted_events(calls), [])

    def test_live_open_thread_and_stale_base_still_withhold_qualified_review(self):
        from tests.test_github_publication import blocking_thread_node

        responses = [
            json_response(pr_payload(head_sha="b" * 40)),
            json_response([]),
            json_response(pr_payload(head_sha="b" * 40)),
            json_response({"id": 5}, 200),
            json_response(pr_payload(head_sha="b" * 40)),
            graphql_review_threads_response(nodes=(blocking_thread_node(),)),
            json_response(pr_payload(head_sha="b" * 40)),
        ]
        outcome, calls = self.publish(responses=responses)
        self.assertEqual(outcome.status, "published")
        self.assertEqual(posted_events(calls), ["COMMENT"])
        _, calls = self.publish()
        eligibility = approval_eligibility_from_body(review_payloads(calls)[0]["body"])
        changed_base = pr_payload(head_sha="b" * 40)
        changed_base["base"]["sha"] = "c" * 40
        http, calls = make_http([json_response(changed_base)])
        outcome = ReviewApprovalFinalizer(
            http=http, qualification_authority=synthetic_authority()
        ).finalize(
            token="token",
            repository="owner/repo",
            pull_request=2,
            head_sha="b" * 40,
            app_slug="reviewsensei[bot]",
            eligibility=eligibility,
        )
        self.assertEqual(outcome.status, "approval_withheld")
        self.assertEqual(posted_events(calls), [])

    def test_authority_snapshots_inputs_and_detects_policy_mismatch(self):
        context = runtime_context()
        authority = synthetic_authority(runtime_configuration=context)
        context["provider"]["timeout_seconds"] = 1
        _, calls = self.publish(authority=authority)
        self.assertEqual(posted_events(calls), ["COMMENT", "APPROVE"])
        _, calls = self.publish(authority=authority, context=context)
        self.assertEqual(posted_events(calls), ["COMMENT"])
        _, calls = self.publish(
            convergence_policy=ReviewConvergencePolicy(mode="strict")
        )
        self.assertEqual(posted_events(calls), ["COMMENT"])

    def test_versioned_provider_facts_roundtrip_and_legacy_does_not_gain_exemption(
        self,
    ):
        from review_sensei_github.approval import ApprovalFacts

        from review_sensei.errors import ReviewInputError

        _, calls = self.publish()
        eligibility = approval_eligibility_from_body(review_payloads(calls)[0]["body"])
        facts = eligibility.facts.to_dict()
        self.assertEqual(facts["schema_version"], "2")
        self.assertEqual(ApprovalFacts.from_dict(facts), eligibility.facts)
        for malformed in (
            {**facts, "schema_version": "1"},
            {key: value for key, value in facts.items() if key != "provider"},
            {**facts, "provider": "ollama"},
        ):
            with self.assertRaises(ReviewInputError):
                ApprovalFacts.from_dict(malformed)

    def test_detached_caller_cannot_forge_v2_provider_exemption_without_app_readback(
        self,
    ):
        from tests.fake_github_http import make_http as raw_http
        from tests.test_github_publication import eligibility_document, published_review

        exempt = eligibility_document(head_sha="b" * 40)
        _, calls = self.publish(authority=None)
        unqualified_body = review_payloads(calls)[0]["body"]
        for persisted in (
            [],
            [
                published_review(
                    marker=unqualified_body, head_sha="b" * 40, state="COMMENTED"
                )
            ],
        ):
            http, calls = raw_http(
                [
                    json_response(pr_payload(head_sha="b" * 40)),
                    graphql_review_threads_response(),
                    json_response(pr_payload(head_sha="b" * 40)),
                    json_response(persisted),
                ]
            )
            final = ReviewApprovalFinalizer(http=http).finalize(
                token="token",
                repository="owner/repo",
                pull_request=2,
                head_sha="b" * 40,
                app_slug="reviewsensei[bot]",
                eligibility=exempt,
                require_persisted=False,
            )
            self.assertEqual(final.status, "approval_withheld")
            self.assertEqual(posted_events(calls), [])

    def test_first_publication_requires_its_actual_app_owned_readback(self):
        from tests.test_github_publication import FakeCheckRuns

        for provider in ("ollama", "openrouter"):
            checks = FakeCheckRuns()
            outcome, calls = self.publish(
                readback=False,
                checks=checks,
                result=replace(
                    clean_result(),
                    provider=provider,
                    model=TARGET.model if provider == "openrouter" else None,
                ),
            )
            self.assertEqual(outcome.status, "published")
            self.assertEqual(posted_events(calls), ["COMMENT"])
            self.assertEqual(outcome.diagnostic, "approval_withheld")
            self.assertEqual(checks.conclusions, [None, "success"])

    def test_repeated_bound_delivery_reconciles_existing_approval_without_post(self):
        from tests.test_github_publication import published_review

        _, calls = self.publish()
        writes = review_payloads(calls)
        existing = [
            published_review(
                marker=write["body"],
                head_sha="b" * 40,
                state="COMMENTED" if write["event"] == "COMMENT" else "APPROVED",
            )
            for write in writes
        ]
        outcome, calls = self.publish(
            responses=[
                json_response(pr_payload(head_sha="b" * 40)),
                json_response(existing),
                json_response(pr_payload(head_sha="b" * 40)),
                graphql_review_threads_response(),
                json_response(pr_payload(head_sha="b" * 40)),
                json_response(existing),
            ]
        )
        self.assertEqual(outcome.status, "already_published")
        self.assertEqual(posted_events(calls), [])

    def test_authority_changed_during_reads_is_rechecked_before_approval_write(self):
        from tests.fake_github_http import make_http as raw_http
        from tests.test_github_publication import published_review

        _, calls = self.publish()
        body = review_payloads(calls)[0]["body"]
        eligibility = approval_eligibility_from_body(body)
        changed_context = runtime_context()
        changed_context["provider"]["timeout_seconds"] = 30
        changed_authority = synthetic_authority(runtime_configuration=changed_context)

        def reviews(request):
            return request.method == "GET" and "/reviews?" in request.full_url

        def respond(request):
            finalizer.qualification_authority = changed_authority
            return json_response(
                [published_review(marker=body, head_sha="b" * 40, state="COMMENTED")]
            )

        http, calls = raw_http(
            [
                json_response(pr_payload(head_sha="b" * 40)),
                graphql_review_threads_response(),
                json_response(pr_payload(head_sha="b" * 40)),
            ],
            routes=((reviews, respond),),
        )
        finalizer = ReviewApprovalFinalizer(
            http=http, qualification_authority=synthetic_authority()
        )
        outcome = finalizer.finalize(
            token="token",
            repository="owner/repo",
            pull_request=2,
            head_sha="b" * 40,
            app_slug="reviewsensei[bot]",
            eligibility=eligibility,
        )
        self.assertEqual(outcome.status, "approval_withheld")
        self.assertEqual(outcome.diagnostic, "qualification_unverified")
        self.assertEqual(posted_events(calls), [])

    def test_authority_constructor_rejects_missing_provider_before_indexing(self):
        from review_sensei.errors import ReviewInputError

        missing_provider = runtime_context()
        del missing_provider["provider"]
        missing_name = runtime_context()
        del missing_name["provider"]["name"]
        for context in (
            {},
            missing_provider,
            missing_name,
            {**runtime_context(), "provider": None},
            {**runtime_context(), "provider": []},
        ):
            with self.subTest(context=context), self.assertRaises(ReviewInputError):
                synthetic_authority(runtime_configuration=context)

    def test_valid_input_binding_cannot_override_conflicting_persisted_binding(self):
        from review_sensei_github.publication import approval_eligibility_marker

        from tests.fake_github_http import make_http as raw_http
        from tests.test_github_publication import published_review

        _, calls = self.publish()
        eligibility = approval_eligibility_from_body(review_payloads(calls)[0]["body"])
        conflicting = replace(
            eligibility,
            facts=replace(eligibility.facts, qualification_binding="0" * 64),
        )
        http, calls = raw_http(
            [
                json_response(pr_payload(head_sha="b" * 40)),
                graphql_review_threads_response(),
                json_response(pr_payload(head_sha="b" * 40)),
                json_response(
                    [
                        published_review(
                            marker=approval_eligibility_marker(conflicting),
                            head_sha="b" * 40,
                            state="COMMENTED",
                        )
                    ]
                ),
            ]
        )
        outcome = ReviewApprovalFinalizer(
            http=http, qualification_authority=synthetic_authority()
        ).finalize(
            token="token",
            repository="owner/repo",
            pull_request=2,
            head_sha="b" * 40,
            app_slug="reviewsensei[bot]",
            eligibility=eligibility,
        )
        self.assertEqual(outcome.status, "approval_withheld")
        self.assertEqual(outcome.diagnostic, "approval_withheld")
        self.assertEqual(posted_events(calls), [])

    def test_public_replay_cannot_mint_authority_for_legacy_comment_without_eligibility(
        self,
    ):
        from tests.test_github_publication import published_review

        _, calls = self.publish()
        body = review_payloads(calls)[0]["body"]
        legacy = "\n".join(
            line
            for line in body.splitlines()
            if not line.startswith("<!-- reviewsensei:eligibility:")
        )
        self.assertIsNone(approval_eligibility_from_body(legacy))
        existing = [
            published_review(marker=legacy, head_sha="b" * 40, state="COMMENTED")
        ]
        outcome, calls = self.publish(
            responses=[
                json_response(pr_payload(head_sha="b" * 40)),
                json_response(existing),
                json_response(pr_payload(head_sha="b" * 40)),
                graphql_review_threads_response(),
                json_response(pr_payload(head_sha="b" * 40)),
                json_response(existing),
            ]
        )
        self.assertEqual(outcome.status, "already_published")
        self.assertEqual(posted_events(calls), [])
        self.assertIsNotNone(outcome.diagnostic)

    def test_non_openrouter_qualified_claim_keeps_supported_approval_policy(self):
        for provider in ("ollama", "compatible"):
            outcome, calls = self.publish(
                authority=None,
                context=None,
                result=replace(clean_result(), provider=provider),
                qualification="qualified",
            )
            self.assertEqual(outcome.status, "published")
            self.assertEqual(posted_events(calls), ["COMMENT", "APPROVE"])
            eligibility = approval_eligibility_from_body(
                review_payloads(calls)[0]["body"]
            )
            self.assertEqual(eligibility.facts.provider, provider)
            self.assertEqual(eligibility.facts.qualification, "qualified")
            self.assertIsNone(eligibility.facts.qualification_binding)
