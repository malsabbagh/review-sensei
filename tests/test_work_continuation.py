from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from review_sensei.budgets import ReviewWorkBudgets
from review_sensei.human_assessment import PendingHumanReview
from review_sensei.models import ProviderResponse, ReviewRequest
from review_sensei.outcomes import ResourceBudget, ResourceBudgetTracker
from review_sensei.reassessment_work import reassess, validate_work
from review_sensei.service import ReviewService
from review_sensei.stages import Stage
from review_sensei.work_recovery import WorkRecoveryStore
from tests import test_review_work
from tests.test_review_work import HUMAN, AssessingProvider


class ContextProvider(AssessingProvider):
    def __init__(self, *, repeat=False, path="src/1.py", kind="joint"):
        super().__init__()
        self.repeat = repeat
        self.path = path
        self.kind = kind

    def complete(self, request):
        response = super().complete(request)
        value = json.loads(response.text)
        if len(self.calls) == 1 or self.repeat:
            reference = value["assessments"][0]["fingerprint"]
            value["assessments"] = []
            value["context_requests"] = [
                {
                    "reference": reference,
                    "kind": self.kind,
                    "required_paths": ["src/0.py", self.path],
                    "reason": "The caller must also reject remote requests before transmitting data.",
                }
            ]
        return ProviderResponse(json.dumps(value), self.name, self.model)


class WorkContinuationTests(unittest.TestCase):
    def test_repeated_discovery_finding_keeps_prior_cross_file_requirements(self):
        from dataclasses import replace

        from review_sensei.hosting.github.approval import (
            approval_eligibility_from_result,
        )
        from review_sensei.models import ReviewComment, ReviewResult

        result = ReviewResult(
            summary="Review this data boundary.",
            provider="fixture",
            comments=(
                ReviewComment(
                    path="src/0.py",
                    line=1,
                    body="Confirm this boundary.",
                    needs_human=True,
                ),
            ),
        )
        arguments = dict(
            head_sha="b" * 40, base_sha="a" * 40, enabled=True, app_authored=False
        )
        prior = approval_eligibility_from_result(result, **arguments)
        finding = replace(
            prior.human_review.findings[0], required_paths=("src/0.py", "src/1.py")
        )
        prior = replace(
            prior, human_review=replace(prior.human_review, findings=(finding,))
        )
        merged = approval_eligibility_from_result(
            result, **arguments, retained_eligibility=prior
        )
        self.assertEqual(merged.human_review.findings, (finding,))
        self.assertTrue(merged.facts.has_human_adjudication_findings)

    def test_unknown_or_duplicate_finding_output_never_gets_semantic_retry(self):
        pending, bundle = self.fixture()

        class InvalidIdentityProvider(AssessingProvider):
            def __init__(self, duplicate):
                super().__init__()
                self.duplicate = duplicate

            def complete(self, request):
                value = json.loads(super().complete(request).text)
                if self.duplicate:
                    value["assessments"].append(value["assessments"][0])
                else:
                    value["assessments"][0]["fingerprint"] = "a" * 64
                return ProviderResponse(json.dumps(value), self.name)

        for duplicate in (False, True):
            with self.subTest(duplicate=duplicate):
                provider = InvalidIdentityProvider(duplicate)
                work = reassess(
                    provider=provider,
                    pending=pending,
                    bundle=bundle,
                    source_body=HUMAN,
                    authority_digest="f" * 64,
                    work_budgets=ReviewWorkBudgets(mode="unified"),
                )
                self.assertEqual(len(provider.calls), 1)
                self.assertFalse(work.reply.decisions)
                self.assertEqual(
                    work.execution.pending_ids, (pending.pending[0].fingerprint,)
                )

    def test_normal_full_replay_cannot_drop_retained_human_obligations(self):
        from review_sensei.hosting.github.publication import ReviewPublisher
        from review_sensei.models import ReviewResult
        from tests import test_human_assessment

        state = test_human_assessment.State()
        publisher = ReviewPublisher(http=state.http)
        arguments = dict(
            token="synthetic-review",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha=test_human_assessment.HEAD,
            base_branch="main",
            base_sha=test_human_assessment.BASE,
            result=ReviewResult(
                summary="The broader scope was reviewed.",
                comments=(),
                provider="fixture",
                review_status="complete",
            ),
            diff="diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n"
            + test_human_assessment.DIFF
            + "\n",
            app_slug=test_human_assessment.APP,
        )
        publisher.publish(
            **arguments,
            retained_eligibility=state.eligibility,
            continuation_digest="f" * 64,
            source_guard=lambda: True,
        )
        publisher.publish(**arguments)
        self.assertEqual(state.events(), ["COMMENT"])

    def test_full_discovery_receipts_restart_with_no_new_inference(self):
        pending, bundle = self.fixture()

        class FullProvider:
            name = "fixture"
            model = "fixture"

            def __init__(self):
                self.calls = 0

            def complete(self, request):
                self.calls += 1
                return ProviderResponse(
                    '{"summary":"Reviewed.","comments":[]}', self.name
                )

        with tempfile.TemporaryDirectory() as root:
            store = WorkRecoveryStore(
                Path(root),
                key=b"k" * 32,
                artifacts="diagnostics",
                now=lambda: datetime(2026, 10, 8, tzinfo=timezone.utc),
            )
            request = ReviewRequest(
                diff="".join(record.diff for record in bundle.records)
            )
            provider = FullProvider()
            service = ReviewService(
                provider,
                stages=(Stage("review", "Review {diff}", ("summary", "comments")),),
                work_budgets=ReviewWorkBudgets(mode="unified"),
            )
            first = service.run(request, work_recovery=store)
            restarted = service.run(request, work_recovery=store)
            self.assertEqual(provider.calls, 1)
            self.assertEqual(first.result, restarted.result)
            self.assertEqual(restarted.outcome.provider_calls, 1)

    def test_hosted_broader_discovery_publishes_new_result_with_prior_identity(self):
        from review_sensei.hosting.github import GitHubApplication, GitHubWriteOptions
        from review_sensei.hosting.github.conversation import ConversationPublisher
        from review_sensei.hosting.github.errors import GitHubBrokerClientError
        from review_sensei.hosting.github.publication import (
            ReviewPublisher,
            approval_eligibility_from_body,
        )
        from tests import test_human_assessment

        state = test_human_assessment.State()
        state.files = [
            {
                "filename": path,
                "patch": test_human_assessment.DIFF,
                "additions": 1,
                "deletions": 1,
            }
            for path in ("src/app.py", "src/other.py")
        ]

        class Broker(test_human_assessment.Broker):
            def exchange(self, token, *, capability):
                if capability == "check_publish":
                    raise GitHubBrokerClientError("synthetic unavailable check grant")
                return super().exchange(token, capability=capability)

        class ScopeProvider(AssessingProvider):
            def complete(self, request):
                value = json.loads(super().complete(request).text)
                value["context_requests"] = [
                    {
                        "reference": value["assessments"][0]["fingerprint"],
                        "kind": "discovery",
                        "required_paths": ["src/app.py", "src/other.py"],
                        "reason": "Review the complete caller and callee scope.",
                    }
                ]
                value["assessments"] = []
                return ProviderResponse(json.dumps(value), self.name)

        class FullProvider:
            name = "fixture"
            model = "fixture"

            def complete(self, request):
                return ProviderResponse(
                    '{"summary":"The broader scope was reviewed.","comments":[]}',
                    self.name,
                )

        policy = ReviewWorkBudgets(mode="unified")
        provider = ScopeProvider()
        app = GitHubApplication(
            broker=Broker(),
            http=state.http,
            reviewer=ReviewPublisher(http=state.http),
            learner=None,
            replier=ConversationPublisher(http=state.http),
        )
        outcome = app.generate_and_publish_reply(
            options=GitHubWriteOptions(
                github_writes=True, mention_replies=True, auto_review=True
            ),
            oidc_token="synthetic",
            read_token="read",
            repository="owner/repo",
            pull_request=1,
            source_comment_id=10,
            source_updated_at="updated",
            expected_head_sha=test_human_assessment.HEAD,
            reply_provider=provider,
            model="fixture",
            app_slug=test_human_assessment.APP,
            root_comment_id=10,
            source_kind="issue",
            work_budgets=policy,
            broader_service=ReviewService(
                FullProvider(),
                stages=(Stage("review", "Review {diff}", ("summary", "comments")),),
                work_budgets=policy,
            ),
        )
        self.assertEqual(outcome.assessment_status, "broader_review_published")
        latest = approval_eligibility_from_body(state.reviews[-1]["body"])
        self.assertNotEqual(latest.result_digest, state.eligibility.result_digest)
        self.assertEqual(
            latest.human_review.pending[0].fingerprint,
            state.eligibility.human_review.pending[0].fingerprint,
        )
        self.assertEqual(
            latest.human_review.pending[0].evidence_paths,
            ("src/app.py", "src/other.py"),
        )
        self.assertEqual(state.events(), ["COMMENT", "COMMENT"])
        self.assertEqual(state.reply_count, 1)

    def test_broader_result_retains_exact_prior_authority_and_replays_once(self):
        from review_sensei.hosting.github.publication import (
            ReviewPublisher,
            approval_eligibility_from_body,
        )
        from review_sensei.models import ReviewResult
        from tests import test_human_assessment

        state = test_human_assessment.State(
            test_human_assessment.prior(blocking=True, qualification="missing")
        )
        publisher = ReviewPublisher(http=state.http)
        result = ReviewResult(
            summary="The broader scope was reviewed.",
            comments=(),
            provider="fixture",
            review_status="complete",
        )
        arguments = dict(
            token="synthetic-review",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha=test_human_assessment.HEAD,
            base_branch="main",
            base_sha=test_human_assessment.BASE,
            result=result,
            diff="diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n"
            + test_human_assessment.DIFF
            + "\n",
            app_slug=test_human_assessment.APP,
            retained_eligibility=state.eligibility,
            continuation_digest="f" * 64,
            source_guard=lambda: True,
        )
        published = publisher.publish(**arguments)
        self.assertEqual(published.status, "published")
        latest = approval_eligibility_from_body(state.reviews[-1]["body"])
        self.assertNotEqual(latest.result_digest, state.eligibility.result_digest)
        self.assertEqual(latest.human_review, state.eligibility.human_review)
        self.assertTrue(latest.facts.has_blocking_findings)
        self.assertEqual(latest.facts.qualification, "missing")
        self.assertEqual(state.events(), ["REQUEST_CHANGES"])
        replay = publisher.publish(**arguments)
        self.assertEqual(replay.status, "already_published")
        self.assertEqual(state.events(), ["REQUEST_CHANGES"])

    def test_broader_result_with_changed_source_cannot_publish(self):
        from review_sensei.hosting.github.publication import ReviewPublisher
        from review_sensei.models import ReviewResult
        from tests import test_human_assessment

        state = test_human_assessment.State()
        outcome = ReviewPublisher(http=state.http).publish(
            token="synthetic-review",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha=test_human_assessment.HEAD,
            base_branch="main",
            base_sha=test_human_assessment.BASE,
            result=ReviewResult(
                summary="Reviewed.",
                comments=(),
                provider="fixture",
                review_status="complete",
            ),
            diff="",
            app_slug=test_human_assessment.APP,
            retained_eligibility=state.eligibility,
            continuation_digest="f" * 64,
            source_guard=lambda: False,
        )
        self.assertEqual(outcome.status, "skipped_edited_source")
        self.assertFalse(state.events())

    def test_broader_discovery_stays_separate_and_cannot_clear_prior_by_omission(self):
        pending, bundle = self.fixture()
        provider = ContextProvider(kind="discovery")

        class FullProvider:
            name = "fixture"
            model = "fixture"

            def complete(self, request):
                return ProviderResponse(
                    '{"summary":"Reviewed.","comments":[]}', self.name
                )

        policy = ReviewWorkBudgets(mode="unified")
        service = ReviewService(
            FullProvider(),
            stages=(Stage("review", "Review {diff}", ("summary", "comments")),),
            work_budgets=policy,
        )
        tracker = ResourceBudgetTracker(ResourceBudget.create(max_provider_calls=2))
        work = reassess(
            provider=provider,
            pending=pending,
            bundle=bundle,
            source_body=HUMAN,
            authority_digest="f" * 64,
            work_budgets=policy,
            tracker=tracker,
            broader_service=service,
        )
        self.assertEqual(tracker.provider_calls, 2)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(work.discovery.outcome.status, "reviewed")
        self.assertEqual(work.execution.pending_ids, (pending.findings[0].fingerprint,))
        self.assertFalse(work.reply.decisions)
        self.assertTrue(work.inventory.pending)

    def test_reassessment_scope_receipts_restart_without_another_provider_call(self):
        pending, bundle = self.fixture()
        policy = ReviewWorkBudgets(mode="unified")
        with tempfile.TemporaryDirectory() as root:
            store = WorkRecoveryStore(
                Path(root),
                key=b"k" * 32,
                artifacts="diagnostics",
                now=lambda: datetime(2026, 10, 8, tzinfo=timezone.utc),
            )
            first = reassess(
                provider=ContextProvider(),
                pending=pending,
                bundle=bundle,
                source_body=HUMAN,
                authority_digest="f" * 64,
                work_budgets=policy,
                recovery=store,
            )
            provider = ContextProvider()
            resumed = reassess(
                provider=provider,
                pending=pending,
                bundle=bundle,
                source_body=HUMAN,
                authority_digest="f" * 64,
                work_budgets=policy,
                recovery=store,
            )
            self.assertFalse(provider.calls)
            self.assertEqual(first.reply, resumed.reply)
            self.assertEqual(first.inventory, resumed.inventory)

    def test_hosted_scope_writes_richer_pending_authority_and_never_approves(self):
        from review_sensei.hosting.github.publication import (
            approval_eligibility_from_body,
        )
        from tests import test_human_assessment

        state = test_human_assessment.State()
        state.files = [
            {
                "filename": "src/app.py",
                "patch": test_human_assessment.DIFF,
                "additions": 1,
                "deletions": 1,
            },
            {
                "filename": "src/other.py",
                "patch": test_human_assessment.DIFF,
                "additions": 1,
                "deletions": 1,
            },
        ]

        class Provider(AssessingProvider):
            def complete(self, request):
                response = super().complete(request)
                value = json.loads(response.text)
                value["context_requests"] = [
                    {
                        "reference": value["assessments"][0]["fingerprint"],
                        "kind": "discovery",
                        "required_paths": ["src/app.py", "src/other.py"],
                        "reason": "Broader discovery is needed to verify the caller.",
                    }
                ]
                value["assessments"] = []
                return ProviderResponse(json.dumps(value), self.name)

        provider = Provider()
        outcome, broker = state.application_reply(
            provider, work_budgets=ReviewWorkBudgets(mode="unified")
        )
        self.assertEqual(len(provider.calls), 1)
        self.assertNotEqual(outcome.approval_status, "approved")
        self.assertEqual(state.events(), ["COMMENT"])
        persisted = approval_eligibility_from_body(state.reviews[-1]["body"])
        self.assertEqual(persisted.to_dict()["schema_version"], "2.0")
        self.assertEqual(
            persisted.human_review.pending[0].evidence_paths,
            ("src/app.py", "src/other.py"),
        )
        self.assertEqual(broker.capabilities, ["issue_reply", "review_publish"])

    def test_discovery_joint_requirement_uses_same_remaining_call_envelope(self):
        pending, bundle = self.fixture()

        class Provider:
            name = "fixture"
            model = "fixture"

            def __init__(self):
                self.calls = 0

            def complete(self, request):
                self.calls += 1
                value = {"summary": "Reviewed.", "comments": []}
                if self.calls == 1:
                    value["context_requests"] = [
                        {
                            "reference": "src/0.py",
                            "kind": "joint",
                            "required_paths": ["src/0.py", "src/1.py"],
                            "reason": "Check the caller and callee relationship together.",
                        }
                    ]
                return ProviderResponse(json.dumps(value), self.name)

        for limit, expected_calls, expected_status in (
            (1, 1, "partial"),
            (3, 2, "reviewed"),
        ):
            with self.subTest(limit=limit):
                provider = Provider()
                service = ReviewService(
                    provider,
                    stages=(Stage("review", "Review {diff}", ("summary", "comments")),),
                    work_budgets=ReviewWorkBudgets(mode="unified"),
                    budget=ResourceBudget.create(max_provider_calls=limit),
                )
                result = service.run(
                    ReviewRequest(diff="".join(item.diff for item in bundle.records))
                )
                self.assertEqual(provider.calls, expected_calls)
                self.assertEqual(result.outcome.status, expected_status)

    def fixture(self):
        inventory, bundle = test_review_work.UnifiedAdaptersTests().fixture((100, 100))
        return PendingHumanReview(inventory.base_sha, inventory.findings[:1]), bundle

    def test_one_joint_wave_reuses_loaded_evidence_and_preserves_identity(self):
        pending, bundle = self.fixture()
        provider = ContextProvider()
        tracker = ResourceBudgetTracker(ResourceBudget.create())
        work = reassess(
            provider=provider,
            pending=pending,
            bundle=bundle,
            source_body=HUMAN,
            authority_digest="f" * 64,
            work_budgets=ReviewWorkBudgets(mode="unified"),
            tracker=tracker,
        )
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(tracker.provider_calls, 2)
        self.assertEqual(
            work.inventory.findings[0].fingerprint, pending.findings[0].fingerprint
        )
        self.assertEqual(
            work.inventory.findings[0].evidence_paths, ("src/0.py", "src/1.py")
        )
        self.assertFalse(work.inventory.apply(work.reply.decisions).pending)
        validate_work(
            work,
            pending=pending,
            bundle=bundle,
            source_body=HUMAN,
            authority_digest="f" * 64,
        )

    def test_second_scope_wave_and_unapproved_path_cannot_clear_finding(self):
        for provider, calls in (
            (ContextProvider(repeat=True), 2),
            (ContextProvider(path="private/key.py"), 1),
        ):
            with self.subTest(path=provider.path, repeat=provider.repeat):
                pending, bundle = self.fixture()
                work = reassess(
                    provider=provider,
                    pending=pending,
                    bundle=bundle,
                    source_body=HUMAN,
                    authority_digest="f" * 64,
                    work_budgets=ReviewWorkBudgets(mode="unified"),
                )
                self.assertEqual(len(provider.calls), calls)
                self.assertEqual(
                    work.inventory.apply(work.reply.decisions).pending,
                    work.inventory.pending,
                )

    def test_continuation_does_not_mint_calls_when_original_envelope_exhausted(self):
        pending, bundle = self.fixture()
        provider = ContextProvider()
        work = reassess(
            provider=provider,
            pending=pending,
            bundle=bundle,
            source_body=HUMAN,
            authority_digest="f" * 64,
            work_budgets=ReviewWorkBudgets(mode="unified"),
            tracker=ResourceBudgetTracker(ResourceBudget.create(max_provider_calls=1)),
        )
        self.assertEqual(len(provider.calls), 1)
        self.assertTrue(work.inventory.pending)
        self.assertFalse(work.reply.decisions)


if __name__ == "__main__":
    unittest.main()
