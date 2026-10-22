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
    def test_repeated_scope_request_from_split_file_remains_pending_once(self):
        class Provider:
            name = "fixture"
            model = "fixture"
            calls = 0

            def complete(self, request):
                self.calls += 1
                result = {
                    "summary": "Review the cross-file relationship.",
                    "comments": [],
                }
                if "src/0.py" in request.prompt:
                    result["context_requests"] = [
                        {
                            "reference": "src/0.py",
                            "kind": "joint",
                            "required_paths": ["src/1.py"],
                            "reason": "Review the shared relationship.",
                        }
                    ]
                return ProviderResponse(json.dumps(result), self.name)

        diff = "diff --git a/src/0.py b/src/0.py\n--- a/src/0.py\n+++ b/src/0.py\n"
        diff += "@@ -1 +1 @@\n-old\n+" + "a" * 100 + "\n"
        diff += "@@ -10 +10 @@\n-old\n+" + "b" * 100 + "\n"
        diff += "diff --git a/src/1.py b/src/1.py\n--- a/src/1.py\n+++ b/src/1.py\n@@ -1 +1 @@\n-old\n+new\n"
        provider = Provider()
        run = ReviewService(
            provider,
            stages=(Stage("review", "Review {diff}", ("summary", "comments")),),
            work_budgets=ReviewWorkBudgets(mode="unified", batch_diff_bytes=256),
        ).run(ReviewRequest(diff=diff))
        self.assertEqual(run.outcome.status, "partial")
        self.assertEqual(provider.calls, 3)
        self.assertFalse(run.result.coverage.fully_reviewed)

    def test_multiple_discovery_requests_cover_all_references_without_pr_metadata(self):
        from dataclasses import replace

        from review_sensei.evidence import EvidenceBundle

        pending, bundle = test_review_work.UnifiedAdaptersTests().fixture((100, 100))
        snapshot = replace(bundle.snapshot, repository=None, pull_request=None)
        bundle = EvidenceBundle(
            snapshot,
            tuple(replace(record, snapshot=snapshot) for record in bundle.records),
        )

        class ScopeProvider(AssessingProvider):
            def complete(self, request):
                value = json.loads(super().complete(request).text)
                value["context_requests"] = [
                    {
                        "reference": item["fingerprint"],
                        "kind": "discovery",
                        "required_paths": ["src/0.py", "src/1.py"],
                        "reason": "Review the shared relationship.",
                    }
                    for item in reversed(value["assessments"])
                ]
                value["assessments"] = []
                return ProviderResponse(json.dumps(value), self.name)

        class FullProvider:
            name = "fixture"
            model = "fixture"

            def __init__(self):
                self.requests = []

            def complete(self, request):
                self.requests.append(request)
                return ProviderResponse(
                    '{"summary":"Reviewed both paths.","comments":[]}', self.name
                )

        full = FullProvider()
        policy = ReviewWorkBudgets(mode="unified")
        tracker = ResourceBudgetTracker(ResourceBudget.create())
        work = reassess(
            provider=ScopeProvider(),
            pending=pending,
            bundle=bundle,
            source_body=HUMAN,
            authority_digest="f" * 64,
            work_budgets=policy,
            tracker=tracker,
            broader_service=ReviewService(
                full,
                stages=(Stage("review", "Review {diff}", ("summary", "comments")),),
                work_budgets=policy,
            ),
        )
        self.assertEqual(len(full.requests), 1)
        self.assertIn("src/0.py", full.requests[0].prompt)
        self.assertIn("src/1.py", full.requests[0].prompt)
        self.assertEqual(tracker.provider_calls, 2)
        self.assertIsNotNone(work.discovery.result)
        self.assertFalse(work.reply.decisions)
        self.assertEqual(len(work.inventory.pending), 2)
        self.assertTrue(
            all(
                item.evidence_paths == ("src/0.py", "src/1.py")
                for item in work.inventory.pending
            )
        )

    def test_retained_coverage_uses_closed_approval_vocabulary(self):
        from dataclasses import replace

        from review_sensei_github.approval import (
            approval_eligibility_from_result,
        )

        from review_sensei.models import ReviewComment, ReviewResult

        result = ReviewResult(
            summary="Review this boundary.",
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
        for state in (None, "reviewed", "partial", "incomplete", "unknown"):
            with self.subTest(state=state):
                retained = replace(
                    prior, facts=replace(prior.facts, coverage_blocker=state)
                )
                merged = approval_eligibility_from_result(
                    result, **arguments, retained_eligibility=retained
                )
                self.assertEqual(
                    merged.facts.coverage_blocker,
                    None if state in (None, "reviewed") else state,
                )
                self.assertFalse(
                    merged.evaluate(
                        app_authored=False, has_open_review_threads=False
                    ).approved
                )

    def test_failed_enumeration_is_explicit_and_cannot_clear_findings(self):
        from review_sensei.evidence import EvidenceBundle

        pending, bundle = self.fixture()
        failed = EvidenceBundle(bundle.snapshot, (), enumeration_complete=False)
        provider = AssessingProvider()
        work = reassess(
            provider=provider,
            pending=pending,
            bundle=failed,
            source_body=HUMAN,
            authority_digest="f" * 64,
            work_budgets=ReviewWorkBudgets(mode="unified"),
        )
        self.assertFalse(provider.calls)
        self.assertFalse(work.reply.decisions)
        self.assertEqual(work.inventory.apply(work.reply.decisions), pending)
        self.assertIn("enumeration is incomplete", work.reply.body)

    def test_repeated_discovery_finding_keeps_prior_cross_file_requirements(self):
        from dataclasses import replace

        from review_sensei_github.approval import (
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
        from review_sensei_github.publication import ReviewPublisher

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
        from review_sensei_github import GitHubApplication, GitHubWriteOptions
        from review_sensei_github.conversation import ConversationPublisher
        from review_sensei_github.errors import GitHubBrokerClientError
        from review_sensei_github.publication import (
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
        from review_sensei_github.publication import (
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
        from review_sensei_github.publication import ReviewPublisher

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

    def test_high_uptime_hosted_scope_retains_pending_authority_and_never_approves(
        self,
    ):
        from unittest.mock import patch

        from review_sensei_github.http import GitHubHttp

        start = float.fromhex("0x1.ffff100000003p+22")
        self.assertGreater(start + 60 - start, 60)
        original = GitHubHttp.load_review_evidence

        def high_uptime_load(http, **kwargs):
            return original(http, **kwargs, monotonic=lambda: start)

        with patch.object(GitHubHttp, "load_review_evidence", high_uptime_load):
            self.test_hosted_scope_writes_richer_pending_authority_and_never_approves()

    def test_hosted_scope_writes_richer_pending_authority_and_never_approves(self):
        from review_sensei_github.publication import (
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
        self.assertEqual(
            broker.capabilities, ["issue_reply", "review_publish", "check_publish"]
        )

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

    def test_multi_window_progression_9_and_33_provider_calls_complete_without_reanalyzing_prefix(
        self,
    ):
        from review_sensei.validation import ReviewLimits, TotalWorkBudget

        def build_multi_file_diff(file_count: int) -> str:
            parts: list[str] = []
            for idx in range(file_count):
                body_lines = "\n".join(
                    f"+value_{idx}_{line} = {line}  # padding text to keep file in single chunk"
                    for line in range(12)
                )
                parts.append(
                    f"diff --git a/src/mod_{idx:02d}.py b/src/mod_{idx:02d}.py\n"
                    f"new file mode 100644\n"
                    f"index 0000000..2222222\n"
                    f"--- /dev/null\n"
                    f"+++ b/src/mod_{idx:02d}.py\n"
                    f"@@ -0,0 +1,12 @@\n"
                    f"{body_lines}\n"
                )
            return "".join(parts)

        class CountingChunkProvider:
            name = "fixture"
            model = "fixture"

            def __init__(self) -> None:
                self.calls = 0
                self.seen_Files: list[str] = []

            def complete(self, request):
                self.calls += 1
                for idx in range(40):
                    marker = f"b/src/mod_{idx:02d}.py"
                    if marker in request.prompt:
                        self.seen_Files.append(f"src/mod_{idx:02d}.py")
                return ProviderResponse(
                    json.dumps(
                        {
                            "summary": f"Window chunk {self.calls} reviewed.",
                            "comments": [],
                        }
                    ),
                    self.name,
                )

        limits = ReviewLimits(
            max_diff_bytes=950,
            max_diff_lines=25,
            max_diff_files=1,
            max_diff_hunks=1,
        )
        work_budget = TotalWorkBudget(
            max_total_diff_bytes=60_000,
            max_total_diff_lines=2_000,
            max_total_files=64,
            max_total_hunks=64,
            max_chunks=8,
            max_provider_calls=8,
        )

        for total_files, expected_windows in ((9, 2), (33, 5)):
            with self.subTest(total_files=total_files):
                diff_text = build_multi_file_diff(total_files)
                provider = CountingChunkProvider()
                service = ReviewService(
                    provider,
                    stages=(
                        Stage("review", "Review:\n{diff}", ("summary", "comments")),
                    ),
                    budget=ResourceBudget.create(max_provider_calls=8),
                )
                prior_result = None
                windows_run = 0
                while True:
                    windows_run += 1
                    run = service.run(
                        ReviewRequest(
                            diff=diff_text,
                            limits=limits,
                            work_budget=work_budget,
                            orchestrate_large_changes=True,
                        ),
                        prior_partial=prior_result,
                    )
                    self.assertIsNotNone(run.result)
                    prior_result = run.result
                    if run.outcome.status == "reviewed":
                        break
                    self.assertEqual(run.outcome.status, "partial")
                    self.assertLess(windows_run, expected_windows)

                self.assertEqual(windows_run, expected_windows)
                self.assertEqual(provider.calls, total_files)
                self.assertEqual(
                    provider.seen_Files,
                    [f"src/mod_{idx:02d}.py" for idx in range(total_files)],
                )
                self.assertEqual(prior_result.review_status, "complete")
                self.assertTrue(prior_result.coverage.fully_reviewed)

    def test_six_partial_windows_plus_authorized_successor_grant_proceeds_on_seventh_window(
        self,
    ):
        from review_sensei.convergence import ReviewConvergencePolicy
        from review_sensei.coverage import CoverageManifest, FileCoverage, HunkCoverage
        from review_sensei.disposition import (
            MaintainerCommand,
            apply_session_command,
        )
        from review_sensei.models import ReviewResult
        from review_sensei.rescan import AuthorizedRescan
        from review_sensei.session import (
            InMemorySessionLedger,
            SessionIdentity,
            checkpoint_review_analysis,
            complete_review_publication,
            prepare_review_transaction,
            prepare_session_round,
        )

        ledger = InMemorySessionLedger()
        identity = SessionIdentity(
            repository="owner/repo", pull_request=7, repository_id=100
        )
        base_sha = "1" * 40
        head_sha = "2" * 40
        policy = ReviewConvergencePolicy()
        config_digest = "4" * 64
        evidence_digest = "5" * 64

        last_published_tx = None
        for window_idx in range(6):
            rescan_req = None
            if window_idx > 0:
                rescan_req = AuthorizedRescan(
                    kind="issue_comment",
                    request_id=window_idx,
                    source=last_published_tx,
                    review_id=100 + window_idx,
                )
            prepared = prepare_review_transaction(
                ledger,
                identity,
                policy,
                reservation_id=f"{window_idx + 1:064x}",
                base_sha=base_sha,
                head_sha=head_sha,
                configuration_digest=config_digest,
                evidence_digest=evidence_digest,
                rescan_request=rescan_req,
            )
            self.assertTrue(prepared.decision.admit)
            tx = prepared.transaction
            self.assertIsNotNone(tx)
            partial_res = ReviewResult(
                summary=f"Partial window {window_idx + 1}.",
                comments=(),
                provider="fixture",
                review_status="partial",
                coverage=CoverageManifest(
                    files=(
                        FileCoverage("src/done.py", "reviewed"),
                        FileCoverage(
                            "src/pending.py",
                            "budget-exhausted",
                            "provider-call-budget",
                        ),
                    ),
                    hunks=(
                        HunkCoverage(1, "src/done.py", "reviewed"),
                        HunkCoverage(
                            2,
                            "src/pending.py",
                            "budget-exhausted",
                            "provider-call-budget",
                        ),
                    ),
                    enumeration_complete=True,
                    enumerated_paths=("src/done.py", "src/pending.py"),
                ),
            )
            bound = checkpoint_review_analysis(ledger, identity, prepared, partial_res)
            published_record = complete_review_publication(
                ledger, identity, bound.transaction, published=True
            )
            last_published_tx = published_record.transaction

        exhausted_record = ledger.load(identity).record
        self.assertEqual(exhausted_record.failed_attempts, 6)

        blocked_rescan = AuthorizedRescan(
            kind="issue_comment",
            request_id=99,
            source=last_published_tx,
            review_id=199,
        )
        blocked_prep = prepare_session_round(
            ledger,
            identity,
            policy,
            reservation_id="9" * 64,
            head_sha=head_sha,
            rescan_request=blocked_rescan,
        )
        self.assertFalse(blocked_prep.decision.admit)
        self.assertEqual(
            blocked_prep.decision.handoff_reason, "failed-attempt-budget-exhausted"
        )

        # Collaborator continue only unpauses and does not restore eligibility
        _, collab_outcome = apply_session_command(
            ledger,
            identity,
            MaintainerCommand(
                action="continue",
                actor="collab",
                head_sha=head_sha,
                command_id="cmd-collab",
            ),
            actor_role="COLLABORATOR",
        )
        self.assertFalse(collab_outcome.applied)
        self.assertIn("failed-attempt budget remains exhausted", collab_outcome.summary)

        # Maintainer/admin continue grants bounded successor continuation
        granted_record, maint_outcome = apply_session_command(
            ledger,
            identity,
            MaintainerCommand(
                action="continue",
                actor="admin-user",
                head_sha=head_sha,
                command_id="cmd-maint-grant",
            ),
            actor_role="maintain",
        )
        self.assertTrue(maint_outcome.applied)
        self.assertIn("successor continuation granted", maint_outcome.summary)
        self.assertEqual(granted_record.failed_attempts, 6)

        # Duplicate/concurrent successor grant is idempotent
        dup_record, dup_outcome = apply_session_command(
            ledger,
            identity,
            MaintainerCommand(
                action="reenroll",
                actor="admin-user",
                head_sha=head_sha,
                command_id="cmd-maint-dup",
            ),
            actor_role="admin",
        )
        self.assertFalse(dup_outcome.applied)
        self.assertEqual(dup_record.generation, granted_record.generation)

        # 7th window now admits and consumes the single successor grant
        seventh_prep = prepare_session_round(
            ledger,
            identity,
            policy,
            reservation_id="7" * 64,
            head_sha=head_sha,
        )
        self.assertTrue(seventh_prep.decision.admit)
        self.assertIsNotNone(seventh_prep.reservation_id)
        retained_prior = ledger.load_retained_checkpoint_result(
            identity, head_sha=head_sha
        )
        self.assertIsNotNone(retained_prior)
        self.assertEqual(retained_prior.review_status, "partial")

    def test_changed_head_cannot_inherit_stale_partial_coverage_and_no_progress_suspends_with_remedy(
        self,
    ):
        from review_sensei.coverage import CoverageManifest, FileCoverage, HunkCoverage
        from review_sensei.errors import ReviewInputError
        from review_sensei.models import ReviewResult
        from review_sensei.session import ReviewTransaction
        from review_sensei.validation import ReviewLimits, TotalWorkBudget

        diff_text = (
            "diff --git a/src/a.py b/src/a.py\n"
            "new file mode 100644\n"
            "index 0000000..2222222\n"
            "--- /dev/null\n"
            "+++ b/src/a.py\n"
            "@@ -0,0 +1,5 @@\n"
            "+a1 = 1\n+a2 = 2\n+a3 = 3\n+a4 = 4\n+a5 = 5\n"
            "diff --git a/src/b.py b/src/b.py\n"
            "new file mode 100644\n"
            "index 0000000..2222222\n"
            "--- /dev/null\n"
            "+++ b/src/b.py\n"
            "@@ -0,0 +1,5 @@\n"
            "+b1 = 1\n+b2 = 2\n+b3 = 3\n+b4 = 4\n+b5 = 5\n"
        )
        prior_partial = ReviewResult(
            summary="Reviewed src/a.py.",
            comments=(),
            provider="fixture",
            review_status="partial",
            coverage=CoverageManifest(
                files=(
                    FileCoverage("src/a.py", "reviewed"),
                    FileCoverage(
                        "src/b.py", "budget-exhausted", "provider-call-budget"
                    ),
                ),
                hunks=(
                    HunkCoverage(1, "src/a.py", "reviewed"),
                    HunkCoverage(
                        2,
                        "src/b.py",
                        "budget-exhausted",
                        "provider-call-budget",
                    ),
                ),
                enumeration_complete=True,
                enumerated_paths=("src/a.py", "src/b.py"),
            ),
            transaction=ReviewTransaction.create(
                repository="owner/repo",
                pull_request=1,
                base_sha="1" * 40,
                head_sha="2" * 40,
                policy_digest="3" * 64,
                configuration_digest="4" * 64,
                evidence_digest="5" * 64,
                reservation_id="6" * 64,
                generation=2,
            ),
        )

        class FailingProvider:
            name = "fixture"
            model = "fixture"

            def complete(self, request):
                return ProviderResponse("not valid json", self.name)

        service = ReviewService(
            FailingProvider(),
            stages=(Stage("review", "Review:\n{diff}", ("summary", "comments")),),
        )
        # Stale head is rejected immediately
        with self.assertRaisesRegex(
            ReviewInputError, "does not match current base or head"
        ):
            service.run(
                ReviewRequest(
                    diff=diff_text,
                    base_sha="1" * 40,
                    head_sha="9" * 40,
                    orchestrate_large_changes=True,
                ),
                prior_partial=prior_partial,
            )

        # Matching head with failing chunk produces no-progress diagnostic and remedy
        no_prog_run = service.run(
            ReviewRequest(
                diff=diff_text,
                base_sha="1" * 40,
                head_sha="2" * 40,
                limits=ReviewLimits(
                    max_diff_bytes=200,
                    max_diff_lines=12,
                    max_diff_files=1,
                    max_diff_hunks=1,
                ),
                work_budget=TotalWorkBudget(
                    max_total_diff_bytes=5000,
                    max_total_diff_lines=100,
                    max_total_files=10,
                    max_total_hunks=10,
                    max_chunks=8,
                    max_provider_calls=8,
                ),
                orchestrate_large_changes=True,
            ),
            prior_partial=prior_partial,
        )
        self.assertEqual(no_prog_run.outcome.status, "partial")
        self.assertEqual(no_prog_run.outcome.diagnostic, "no-progress")
        self.assertIn(
            "accept-coverage-uncertainty --reason",
            no_prog_run.result.summary,
        )

    def test_cli_invocations_reuse_retained_partial_across_ledger_and_multistage_windows(
        self,
    ):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from unittest.mock import patch

        from review_sensei.cli import main as cli_main
        from review_sensei.errors import ReviewInputError
        from review_sensei.models import ReviewResult
        from review_sensei.session import LocalSessionLedger, SessionIdentity
        from review_sensei.validation import ReviewLimits, TotalWorkBudget

        diff_parts = []
        for idx in range(9):
            diff_parts.append(
                f"diff --git a/src/mod_{idx}.py b/src/mod_{idx}.py\n"
                "new file mode 100644\n"
                "index 0000000..1111111\n"
                "--- /dev/null\n"
                f"+++ b/src/mod_{idx}.py\n"
                "@@ -0,0 +1,5 @@\n"
                f"+v{idx}_1 = 1\n+v{idx}_2 = 2\n+v{idx}_3 = 3\n+v{idx}_4 = 4\n+v{idx}_5 = 5\n"
            )
        nine_file_diff = "".join(diff_parts)

        # 1. Two separate CLI invocations against the same durable ledger
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            diff_path = root / "nine.diff"
            diff_path.write_text(nine_file_diff, encoding="utf-8")
            cfg_path = root / ".reviewsensei.yml"
            cfg_path.write_text(
                "schema: 1\n"
                "inference:\n"
                "  backend: local-ollama\n"
                "  model: qwen3.5:4b\n"
                "advanced:\n"
                "  large_changes:\n"
                "    orchestrate: true\n"
                "  resources:\n"
                "    max_provider_calls: 8\n",
                encoding="utf-8",
            )
            fixture_resp = root / "resp.json"
            fixture_resp.write_text(
                '{"summary":"Chunk clean.","comments":[]}', encoding="utf-8"
            )
            ledger_dir = root / "ledger"
            out1 = root / "out1.json"
            out2 = root / "out2.json"

            cli_args_base = [
                "--config",
                str(cfg_path),
                "--diff",
                str(diff_path),
                "--provider",
                "fixture",
                "--fixture-response",
                str(fixture_resp),
                "--repository",
                "owner/repo",
                "--pull-request",
                "42",
                "--base-sha",
                "1" * 40,
                "--head-sha",
                "2" * 40,
                "--transaction",
                "--session-ledger",
                str(ledger_dir),
                "--orchestrate-large-changes",
                "--format",
                "json",
                "--no-learning-proposals",
            ]
            small_limits = ReviewLimits(
                max_diff_bytes=64 * 1024,
                max_diff_lines=1600,
                max_diff_files=1,
                max_diff_hunks=1,
            )
            with (
                patch("review_sensei.cli.DEFAULT_REVIEW_LIMITS", small_limits),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                rc1 = cli_main([*cli_args_base, "--output", str(out1)])
            self.assertEqual(rc1, 2)
            res1 = ReviewResult.from_dict(json.loads(out1.read_text(encoding="utf-8")))
            self.assertEqual(res1.review_status, "partial")
            assert res1.coverage is not None
            self.assertEqual(
                sum(1 for f in res1.coverage.files if f.outcome == "reviewed"), 8
            )

            # Commit window 1's publication in the durable ledger as hosted publication does
            from review_sensei.session import complete_review_publication

            ledger = LocalSessionLedger(ledger_dir)
            ident = SessionIdentity(repository="owner/repo", pull_request=42)
            assert res1.transaction is not None
            complete_review_publication(ledger, ident, res1.transaction, published=True)

            # Second separate CLI invocation loads retained partial from ledger and finishes file 8
            with (
                patch("review_sensei.cli.DEFAULT_REVIEW_LIMITS", small_limits),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                rc2 = cli_main([*cli_args_base, "--output", str(out2)])
            self.assertEqual(rc2, 0)
            res2 = ReviewResult.from_dict(json.loads(out2.read_text(encoding="utf-8")))
            self.assertEqual(res2.review_status, "complete")
            assert res2.coverage is not None
            self.assertTrue(res2.coverage.fully_reviewed)
            self.assertEqual(
                [f.path for f in res2.coverage.files if f.outcome == "reviewed"],
                [f"src/mod_{i}.py" for i in range(9)],
            )

            # Serialized checkpoint round-trip with unchanged vs changed work policy
            roundtrip_partial = ReviewResult.from_dict(res1.to_dict())
            svc_same = ReviewService(
                type(
                    "P",
                    (),
                    {
                        "name": "fixture",
                        "model": "fixture",
                        "complete": lambda self, req: ProviderResponse(
                            '{"summary":"ok","comments":[]}', "fixture"
                        ),
                    },
                )(),
                stages=(Stage("review", "Review:\n{diff}", ("summary", "comments")),),
                work_budgets=ReviewWorkBudgets(mode="unified"),
            )
            from review_sensei.service import _remember_result_work_policy

            _remember_result_work_policy(roundtrip_partial, svc_same.work_policy_digest)
            assert roundtrip_partial.transaction is not None
            ok_rt = svc_same.run(
                ReviewRequest(
                    diff=nine_file_diff,
                    base_sha="1" * 40,
                    head_sha="2" * 40,
                    orchestrate_large_changes=True,
                ),
                prior_partial=roundtrip_partial,
                expected_policy_digest=roundtrip_partial.transaction.policy_digest,
                expected_configuration_digest=roundtrip_partial.transaction.configuration_digest,
            )
            self.assertEqual(ok_rt.result.review_status, "complete")

            svc_diff_policy = ReviewService(
                type(
                    "P",
                    (),
                    {
                        "name": "fixture",
                        "model": "fixture",
                        "complete": lambda self, req: ProviderResponse(
                            '{"summary":"ok","comments":[]}', "fixture"
                        ),
                    },
                )(),
                stages=(Stage("review", "Review:\n{diff}", ("summary", "comments")),),
                work_budgets=ReviewWorkBudgets(mode="unified", discovery_Visual_units=1)
                if hasattr(ReviewWorkBudgets, "discovery_Visual_units")
                else ReviewWorkBudgets(mode="legacy"),
            )
            with self.assertRaisesRegex(ReviewInputError, "policy does not match"):
                svc_diff_policy.run(
                    ReviewRequest(
                        diff=nine_file_diff,
                        base_sha="1" * 40,
                        head_sha="2" * 40,
                        orchestrate_large_changes=True,
                    ),
                    prior_partial=roundtrip_partial,
                )

        # 2. Multi-stage (2 stages, 9 batches, 8 calls per window) completes all stages per scheduled batch
        stage_calls: list[str] = []

        class TwoStageProvider:
            name = "fixture"
            model = "fixture"

            def complete(self, request):
                tag = "FIRST" if "FIRST_STAGE" in request.prompt else "SECOND"
                for idx in range(9):
                    if f"src/mod_{idx}.py" in request.prompt:
                        stage_calls.append(f"{tag}:{idx}")
                        break
                return ProviderResponse('{"summary":"Clean.","comments":[]}', self.name)

        two_stage_service = ReviewService(
            TwoStageProvider(),
            stages=(
                Stage("first", "FIRST_STAGE\n{diff}", ("summary", "comments")),
                Stage("second", "SECOND_STAGE\n{diff}", ("summary", "comments")),
            ),
            work_budgets=ReviewWorkBudgets(mode="unified"),
        )
        req_9 = ReviewRequest(
            diff=nine_file_diff,
            base_sha="1" * 40,
            head_sha="2" * 40,
            limits=ReviewLimits(
                max_diff_bytes=220,
                max_diff_lines=12,
                max_diff_files=1,
                max_diff_hunks=1,
            ),
            work_budget=TotalWorkBudget(
                max_total_diff_bytes=10000,
                max_total_diff_lines=200,
                max_total_files=20,
                max_total_hunks=20,
                max_chunks=8,
                max_provider_calls=8,
            ),
            orchestrate_large_changes=True,
        )
        w1 = two_stage_service.run(req_9)
        self.assertEqual(w1.result.review_status, "partial")
        # Window 1 runs 4 batches across both FIRST and SECOND stages (8 calls total)
        self.assertEqual(
            stage_calls,
            [
                "FIRST:0",
                "FIRST:1",
                "FIRST:2",
                "FIRST:3",
                "SECOND:0",
                "SECOND:1",
                "SECOND:2",
                "SECOND:3",
            ],
        )
        w2 = two_stage_service.run(req_9, prior_partial=w1.result)
        self.assertEqual(w2.result.review_status, "partial")
        # Window 2 advances to batches 4..7 across both stages
        self.assertEqual(
            stage_calls[8:],
            [
                "FIRST:4",
                "FIRST:5",
                "FIRST:6",
                "FIRST:7",
                "SECOND:4",
                "SECOND:5",
                "SECOND:6",
                "SECOND:7",
            ],
        )
        w3 = two_stage_service.run(req_9, prior_partial=w2.result)
        self.assertEqual(w3.result.review_status, "complete")
        assert w3.result.coverage is not None
        self.assertTrue(w3.result.coverage.fully_reviewed)
        self.assertEqual(stage_calls[16:], ["FIRST:8", "SECOND:8"])


if __name__ == "__main__":
    unittest.main()
