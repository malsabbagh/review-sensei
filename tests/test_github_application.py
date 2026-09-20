import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from review_sensei import ProviderResponse
from review_sensei.baseline import ReviewBaseline, baseline_history_document
from review_sensei.context import ReviewContextCacheKey, finding_lifecycle_for_comment
from review_sensei.convergence import BlockerCandidate, ReviewConvergencePolicy
from review_sensei.disposition import apply_session_command, parse_maintainer_command
from review_sensei.errors import ReviewInputError
from review_sensei.hosting.github import (
    GitHubApplication,
    GitHubPublicationError,
    GitHubWriteOptions,
    PreparedConversation,
    PublicationResult,
    ReplyResult,
)
from review_sensei.models import (
    ConversationContext,
    ConversationMessage,
    ConversationReply,
    LearningProposal,
    ReviewComment,
    ReviewResult,
)
from review_sensei.outcomes import RecoveryArtifact
from review_sensei.session import InMemorySessionLedger, SessionIdentity


class RecordingBroker:
    def __init__(self):
        self.requested = 0
        self.exchanges = []

    def request_oidc_token(self):
        self.requested += 1
        return "oidc-token"

    def exchange(self, token, *, capability=None):
        self.exchanges.append((token, capability))
        return f"capability-{capability}"


class RecordingReviewer:
    def __init__(self):
        self.calls = []

    def publish(self, **kwargs):
        self.calls.append(kwargs)
        return PublicationResult(status="published", review_id=1)


class RecordingLearner:
    def __init__(self):
        self.calls = []

    def propose(self, **kwargs):
        self.calls.append(kwargs)
        return type("LearningResult", (), {"status": "created"})()

    def propose_batch(self, **kwargs):
        self.batch_calls = getattr(self, "batch_calls", [])
        self.batch_calls.append(kwargs)
        return type("LearningResult", (), {"status": "created"})()


class RecordingReplier:
    def __init__(self):
        self.calls = []
        self.reaction_calls = []

    def add_processing_reaction(self, **kwargs):
        self.reaction_calls.append(("add", kwargs))
        return type("Reaction", (), {"reaction_id": 99})()

    def remove_processing_reaction(self, **kwargs):
        self.reaction_calls.append(("remove", kwargs))

    def publish(self, **kwargs):
        self.calls.append(kwargs)
        return ReplyResult(status="replied", comment_id=2)

    def prepare_context(self, **kwargs):
        self.prepare_calls = getattr(self, "prepare_calls", [])
        self.prepare_calls.append(kwargs)
        return PreparedConversation(
            source_kind="issue",
            source_comment_id=10,
            source_updated_at="updated",
            head_sha="a" * 40,
            root_comment_id=10,
            context=ConversationContext(
                messages=(
                    ConversationMessage(
                        author="alice",
                        body="@sensei explain",
                        created_at="updated",
                    ),
                ),
                pull_request_number=1,
                head_sha="a" * 40,
            ),
        )


class RecordingProvider:
    name = "fixture"
    model = "fixture-model"

    def complete(self, request):
        return ProviderResponse(
            text='{"body":"Here is the bounded answer."}',
            provider=self.name,
            model=self.model,
        )


class GitHubApplicationTests(unittest.TestCase):
    def setUp(self):
        self.broker = RecordingBroker()
        self.reviewer = RecordingReviewer()
        self.learner = RecordingLearner()
        self.replier = RecordingReplier()
        self.application = GitHubApplication(
            broker=self.broker,
            http=None,
            reviewer=self.reviewer,
            learner=self.learner,
            replier=self.replier,
        )

    def test_disabled_options_never_request_or_exchange_capabilities(self):
        options = GitHubWriteOptions()
        self.assertEqual(
            self.application.publish_review(
                options=options,
                oidc_token=None,
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha="a" * 40,
                result=None,
                diff="",
                app_slug="review-sensei[bot]",
            ).status,
            "disabled",
        )
        self.assertEqual(
            self.application.publish_learning(
                options=options,
                oidc_token=None,
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha="a" * 40,
                base_branch="main",
                base_sha="a" * 40,
                proposal=LearningProposal(title="T", rule="R"),
            ).status,
            "disabled",
        )
        self.assertEqual(
            self.application.publish_reply(
                options=options,
                oidc_token=None,
                repository="owner/repo",
                pull_request=1,
                source_comment_id=1,
                source_updated_at="now",
                head_sha="a" * 40,
                reply=ConversationReply.from_dict({"body": "reply"}),
                app_slug="review-sensei[bot]",
                root_comment_id=1,
            ).status,
            "disabled",
        )
        self.assertEqual(self.broker.requested, 0)
        self.assertEqual(self.broker.exchanges, [])

    def test_legacy_policy_rejects_candidate_verification_inputs(self):
        options = GitHubWriteOptions(
            auto_review=True,
            github_writes=True,
        )
        with self.assertRaisesRegex(
            GitHubPublicationError,
            "legacy evidence policy cannot include candidate verification inputs",
        ):
            self.application.publish_review(
                options=options,
                oidc_token=None,
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha="a" * 40,
                base_branch="main",
                base_sha="a" * 40,
                result=ReviewResult(
                    summary="Summary.",
                    comments=(),
                    provider="fixture",
                    review_status="complete",
                ),
                diff="diff",
                app_slug="review-sensei[bot]",
                snapshot={"src/app.py": "content"},
                snapshot_sha256="a" * 64,
            )
        self.assertEqual(self.broker.exchanges, [])

    def test_confirmed_policy_requires_snapshot_before_publisher(self):
        options = GitHubWriteOptions(
            auto_review=True,
            github_writes=True,
        )
        with self.assertRaisesRegex(
            GitHubPublicationError, "confirmed evidence policy requires"
        ):
            self.application.publish_review(
                options=options,
                oidc_token=None,
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha="a" * 40,
                base_branch="main",
                base_sha="a" * 40,
                result=ReviewResult(
                    summary="Summary.",
                    comments=(),
                    provider="fixture",
                    review_status="complete",
                ),
                diff="diff",
                app_slug="review-sensei[bot]",
                evidence_policy="confirmed",
            )
        self.assertEqual(self.broker.exchanges, [])

    def test_enabled_operations_use_narrow_capabilities(self):
        options = GitHubWriteOptions(
            auto_review=True,
            auto_approve=True,
            github_writes=True,
            learning_prs=True,
            mention_replies=True,
        )
        review = self.application.publish_review(
            options=options,
            oidc_token=None,
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha="a" * 40,
            base_branch="main",
            base_sha="a" * 40,
            result=None,
            diff="diff",
            app_slug="review-sensei[bot]",
        )
        learning = self.application.publish_learning(
            options=options,
            oidc_token="provided-oidc",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha="a" * 40,
            base_branch="main",
            base_sha="a" * 40,
            proposal=LearningProposal(title="T", rule="R"),
        )
        reply = self.application.publish_reply(
            options=options,
            oidc_token="provided-oidc",
            repository="owner/repo",
            pull_request=1,
            source_comment_id=1,
            source_updated_at="now",
            head_sha="a" * 40,
            reply=ConversationReply.from_dict({"body": "reply"}),
            app_slug="review-sensei[bot]",
            root_comment_id=1,
            source_kind="issue",
        )

        self.assertEqual(review.status, "published")
        self.assertEqual(learning.status, "created")
        self.assertEqual(reply.status, "replied")
        self.assertEqual(
            [capability for _, capability in self.broker.exchanges],
            ["review_publish", "learning_write", "issue_reply"],
        )
        self.assertEqual(self.broker.requested, 1)
        self.assertEqual(self.reviewer.calls[0]["token"], "capability-review_publish")
        self.assertTrue(self.reviewer.calls[0]["auto_approve"])
        self.assertEqual(self.learner.calls[0]["token"], "capability-learning_write")
        self.assertEqual(self.replier.calls[0]["token"], "capability-issue_reply")
        self.assertTrue(self.replier.calls[0]["auto_approve"])

    def test_publish_review_forwards_blocker_candidates(self):
        options = GitHubWriteOptions(auto_review=True, github_writes=True)
        policy = ReviewConvergencePolicy(
            mode="merge-focused", enforcement="publication"
        )
        facts = (
            BlockerCandidate(
                proposed_blocking=False,
                severity="high",
                has_specific_violation=True,
                has_actionable_remedy=True,
                evidence_locations_validated=True,
                has_failure_condition=True,
                attribution="pr-change",
            ),
        )
        outcome = self.application.publish_review(
            options=options,
            oidc_token=None,
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha="a" * 40,
            base_branch="main",
            base_sha="a" * 40,
            result=ReviewResult(
                summary="Summary.",
                comments=(),
                provider="fixture",
                review_status="complete",
            ),
            diff="diff",
            app_slug="review-sensei[bot]",
            convergence_policy=policy,
            blocker_candidates=facts,
        )
        self.assertEqual(outcome.status, "published")
        self.assertIs(self.reviewer.calls[-1]["convergence_policy"], policy)
        self.assertIs(self.reviewer.calls[-1]["blocker_candidates"], facts)
        self.assertIsNone(self.reviewer.calls[-1]["input_blocker_candidates"])

    def test_publish_review_forwards_persisted_dispositions_for_current_head(self):
        ledger = InMemorySessionLedger()
        identity = SessionIdentity("owner/repo", 1, repository_id=1)
        comment = ReviewComment(
            path="src/app.py",
            line=2,
            body="finding",
            blocking=True,
            severity="high",
            defect_kind="authz-failure",
            fix_effort="small",
        )
        fingerprint = finding_lifecycle_for_comment(comment).fingerprint
        command = parse_maintainer_command(
            f"@sensei accept-risk {fingerprint} --reason accepted launch exception",
            actor="alice",
            head_sha="a" * 40,
        )
        apply_session_command(ledger, identity, command)
        application = GitHubApplication(
            broker=self.broker,
            http=None,
            reviewer=self.reviewer,
            learner=self.learner,
            replier=self.replier,
            session_ledger=ledger,
        )
        outcome = application.publish_review(
            options=GitHubWriteOptions(auto_review=True, github_writes=True),
            oidc_token=None,
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha="a" * 40,
            base_branch="main",
            base_sha="b" * 40,
            result=ReviewResult(
                summary="Summary.",
                comments=(comment,),
                provider="fixture",
                review_status="complete",
            ),
            diff="diff",
            app_slug="review-sensei[bot]",
        )
        self.assertEqual(outcome.status, "published")
        forwarded = self.reviewer.calls[-1]["authorized_dispositions"]
        self.assertEqual(len(forwarded), 1)
        self.assertEqual(forwarded[0].fingerprint, fingerprint)

    def test_publish_review_restores_durable_baseline_for_admission(self):
        ledger = InMemorySessionLedger()
        identity = SessionIdentity("owner/repo", 1, repository_id=1)
        policy = ReviewConvergencePolicy(mode="merge-focused")
        baseline = ReviewBaseline(
            cache_key=ReviewContextCacheKey(
                repository="owner/repo",
                pull_request=1,
                base_sha="b" * 40,
                head_sha="a" * 40,
                engine="fixture",
                model="fixture-model",
                profile="default",
                stage_digest="1" * 64,
                context_digest="2" * 64,
                learning_digest="3" * 64,
            ),
            policy_digest=policy.digest(),
            complete=True,
            coverage_complete=True,
            generation=1,
            reviewed_paths=("src/app.py",),
            related_paths=("src/helper.py",),
        )
        history = {
            "state": "completed",
            "baseline": baseline_history_document(baseline),
            "progress": [{"event": "completed", "generation": 1}],
            "provenance": {"ledger_digest": "0" * 64},
        }
        ledger.initialize(identity)
        ledger.replace(
            identity,
            lambda record: record.evolve(
                completed_initial_reviews=1,
                generation=1,
                convergence_history=history,
            ),
        )
        application = GitHubApplication(
            broker=self.broker,
            http=None,
            reviewer=self.reviewer,
            learner=self.learner,
            replier=self.replier,
            session_ledger=ledger,
        )

        application.publish_review(
            options=GitHubWriteOptions(auto_review=True, github_writes=True),
            oidc_token=None,
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha="a" * 40,
            base_branch="main",
            base_sha="b" * 40,
            result=ReviewResult(
                summary="Summary.",
                comments=(),
                provider="fixture",
                review_status="complete",
            ),
            diff="diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-old\n+new\n",
            app_slug="review-sensei[bot]",
            convergence_policy=policy,
            current_key=baseline.cache_key,
            changed_paths=("src/app.py",),
        )

        forwarded = self.reviewer.calls[-1]
        self.assertEqual(forwarded["baseline"], baseline)
        self.assertEqual(forwarded["current_key"], baseline.cache_key)
        self.assertEqual(forwarded["changed_paths"], ("src/app.py",))
        self.assertEqual(forwarded["related_paths"], ())

        explicit_reviewer = RecordingReviewer()
        explicit_application = GitHubApplication(
            broker=self.broker,
            http=None,
            reviewer=explicit_reviewer,
            learner=self.learner,
            replier=self.replier,
            session_ledger=None,
        )
        explicit_application.publish_review(
            options=GitHubWriteOptions(auto_review=True, github_writes=True),
            oidc_token=None,
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha="a" * 40,
            base_branch="main",
            base_sha="b" * 40,
            result=ReviewResult(
                summary="Summary.",
                comments=(),
                provider="fixture",
                review_status="complete",
            ),
            diff="diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-old\n+new\n",
            app_slug="review-sensei[bot]",
            convergence_policy=policy,
            baseline=baseline,
            current_key=baseline.cache_key,
            changed_paths=("src/app.py",),
            related_paths=("src/explicit.py",),
        )
        self.assertEqual(
            explicit_reviewer.calls[-1]["related_paths"], ("src/explicit.py",)
        )

    def test_publish_review_handoffs_when_durable_baseline_has_no_current_key(self):
        ledger = InMemorySessionLedger()
        identity = SessionIdentity("owner/repo", 1, repository_id=1)
        policy = ReviewConvergencePolicy(mode="merge-focused")
        baseline = ReviewBaseline(
            cache_key=ReviewContextCacheKey(
                repository="owner/repo",
                pull_request=1,
                base_sha="b" * 40,
                head_sha="a" * 40,
                engine="fixture",
                model="fixture-model",
                profile="default",
                stage_digest="1" * 64,
                context_digest="2" * 64,
                learning_digest="3" * 64,
            ),
            policy_digest=policy.digest(),
            complete=True,
            coverage_complete=True,
            generation=1,
            reviewed_paths=("src/app.py",),
        )
        history = {
            "state": "completed",
            "baseline": baseline_history_document(baseline),
            "progress": [{"event": "completed", "generation": 1}],
            "provenance": {"ledger_digest": "0" * 64},
        }
        ledger.initialize(identity)
        ledger.replace(
            identity,
            lambda record: record.evolve(
                completed_initial_reviews=1,
                generation=1,
                convergence_history=history,
            ),
        )
        broker = RecordingBroker()
        reviewer = RecordingReviewer()
        application = GitHubApplication(
            broker=broker,
            http=None,
            reviewer=reviewer,
            learner=self.learner,
            replier=self.replier,
            session_ledger=ledger,
        )

        publish_kwargs = {
            "options": GitHubWriteOptions(
                auto_review=True, github_writes=True, github_session_ledger=True
            ),
            "oidc_token": None,
            "repository": "owner/repo",
            "repository_id": 1,
            "pull_request": 1,
            "head_sha": "a" * 40,
            "base_branch": "main",
            "base_sha": "b" * 40,
            "result": ReviewResult(
                summary="Summary.",
                comments=(),
                provider="fixture",
                review_status="complete",
            ),
            "diff": "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-old\n+new\n",
            "app_slug": "review-sensei[bot]",
            "convergence_policy": policy,
        }
        outcome = application.publish_review(**publish_kwargs)

        self.assertEqual(outcome.status, "handoff")
        self.assertEqual(outcome.diagnostic, "durable_baseline_recovery_required")
        self.assertEqual(reviewer.calls, [])
        self.assertIsNone(ledger.load(identity).record.reservation_id)

        legacy_reviewer = RecordingReviewer()
        legacy_application = GitHubApplication(
            broker=broker,
            http=None,
            reviewer=legacy_reviewer,
            learner=self.learner,
            replier=self.replier,
            session_ledger=ledger,
        )
        legacy_kwargs = dict(publish_kwargs)
        legacy_kwargs.pop("convergence_policy")
        legacy_outcome = legacy_application.publish_review(**legacy_kwargs)
        self.assertEqual(legacy_outcome.status, "published")
        self.assertEqual(len(legacy_reviewer.calls), 1)
        self.assertIsNone(legacy_reviewer.calls[0]["baseline"])
        self.assertIsNone(legacy_reviewer.calls[0]["current_key"])
        self.assertIsNone(legacy_reviewer.calls[0]["changed_paths"])
        self.assertEqual(legacy_reviewer.calls[0]["related_paths"], ())
        self.assertEqual(legacy_reviewer.calls[0]["evidence_confirmed_concerns"], ())

        with patch(
            "review_sensei.hosting.github.application.baseline_from_history_document",
            side_effect=ReviewInputError("corrupt persisted baseline"),
        ):
            malformed = application.publish_review(**publish_kwargs)
        self.assertEqual(malformed.status, "handoff")
        self.assertEqual(malformed.diagnostic, "durable_baseline_recovery_required")
        self.assertEqual(reviewer.calls, [])
        self.assertIsNone(ledger.load(identity).record.reservation_id)

        recovery_history = {
            "state": "recovery-required",
            "progress": [{"event": "recovery-required", "generation": 1}],
            "provenance": {"ledger_digest": "0" * 64},
        }
        ledger.replace(
            identity,
            lambda record: record.evolve(convergence_history=recovery_history),
        )
        recovery = application.publish_review(**publish_kwargs)
        self.assertEqual(recovery.status, "handoff")
        self.assertEqual(recovery.diagnostic, "durable_baseline_recovery_required")
        self.assertEqual(reviewer.calls, [])
        self.assertIsNone(ledger.load(identity).record.reservation_id)

    def test_publish_review_handoffs_when_prior_operator_record_lacks_history(self):
        ledger = InMemorySessionLedger()
        identity = SessionIdentity("owner/repo", 1, repository_id=1)
        ledger.initialize(identity)
        ledger.replace(
            identity,
            lambda record: record.evolve(
                completed_initial_reviews=1,
                generation=1,
            ),
        )
        policy = ReviewConvergencePolicy(mode="merge-focused")
        reviewer = RecordingReviewer()
        application = GitHubApplication(
            broker=self.broker,
            http=None,
            reviewer=reviewer,
            learner=self.learner,
            replier=self.replier,
            session_ledger=ledger,
        )

        outcome = application.publish_review(
            options=GitHubWriteOptions(
                auto_review=True, github_writes=True, github_session_ledger=True
            ),
            oidc_token=None,
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha="a" * 40,
            base_branch="main",
            base_sha="b" * 40,
            result=ReviewResult(
                summary="Summary.",
                comments=(),
                provider="fixture",
                review_status="complete",
            ),
            diff="diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-old\n+new\n",
            app_slug="review-sensei[bot]",
            convergence_policy=policy,
        )

        self.assertEqual(outcome.status, "handoff")
        self.assertEqual(outcome.diagnostic, "durable_baseline_recovery_required")
        self.assertEqual(reviewer.calls, [])
        self.assertIsNone(ledger.load(identity).record.reservation_id)

    def test_recover_review_rejects_operator_modes(self):
        head = "b" * 40
        result = ReviewResult(
            summary="Summary.",
            comments=(
                ReviewComment(path="src/app.py", line=2, body="finding", blocking=True),
            ),
            provider="fixture",
            review_status="complete",
        )
        artifact = RecoveryArtifact.create(
            repository="owner/repo",
            pull_request_number=1,
            base_sha="a" * 40,
            head_sha=head,
            result=result.to_dict(),
            expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        )
        policy = ReviewConvergencePolicy(
            mode="merge-focused", enforcement="publication"
        )
        with self.assertRaisesRegex(GitHubPublicationError, "operator-mode recovery"):
            self.application.recover_review(
                options=GitHubWriteOptions(github_writes=True, auto_review=True),
                oidc_token="oidc",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha=head,
                base_branch="main",
                base_sha="a" * 40,
                artifact=artifact,
                diff="diff --git a/src/app.py b/src/app.py\n",
                app_slug="reviewsensei[bot]",
                convergence_policy=policy,
            )
        self.assertEqual(self.reviewer.calls, [])

    def test_learning_proposals_reuse_one_learning_capability(self):
        options = GitHubWriteOptions(github_writes=True, learning_prs=True)
        result = ReviewResult(
            summary="summary",
            comments=(),
            provider="fixture",
            model="fixture-model",
            learning_proposals=(LearningProposal(title="T", rule="R"),),
        )
        outcomes = self.application.publish_learning_proposals(
            options=options,
            oidc_token="provided-oidc",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha="a" * 40,
            base_branch="main",
            base_sha="a" * 40,
            result=result,
        )
        self.assertEqual([outcome.status for outcome in outcomes], ["created"])
        self.assertEqual(
            [capability for _, capability in self.broker.exchanges],
            ["learning_write"],
        )
        self.assertEqual(len(self.learner.batch_calls), 1)
        self.assertEqual(
            len(self.learner.batch_calls[0]["proposals"]),
            1,
        )

    def test_learning_proposals_without_proposals_do_not_request_capability(self):
        result = ReviewResult(
            summary="summary",
            comments=(),
            provider="fixture",
            model="fixture-model",
        )
        outcomes = self.application.publish_learning_proposals(
            options=GitHubWriteOptions(github_writes=True, learning_prs=True),
            oidc_token=None,
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha="a" * 40,
            base_branch="main",
            base_sha="a" * 40,
            result=result,
        )
        self.assertEqual(outcomes, ())
        self.assertEqual(self.broker.exchanges, [])

    def test_generated_reply_returns_disabled_or_preflight_skip_before_provider(self):
        disabled = self.application.generate_and_publish_reply(
            options=GitHubWriteOptions(),
            oidc_token=None,
            read_token="read-token",
            repository="owner/repo",
            pull_request=1,
            source_comment_id=10,
            source_updated_at="updated",
            expected_head_sha=None,
            reply_provider=RecordingProvider(),
            model="fixture-model",
            app_slug="review-sensei[bot]",
            root_comment_id=10,
        )
        self.assertEqual(disabled.status, "disabled")

        self.replier.prepare_context = lambda **kwargs: ReplyResult(
            status="skipped_no_mention"
        )
        skipped = self.application.generate_and_publish_reply(
            options=GitHubWriteOptions(github_writes=True, mention_replies=True),
            oidc_token=None,
            read_token="read-token",
            repository="owner/repo",
            pull_request=1,
            source_comment_id=10,
            source_updated_at="updated",
            expected_head_sha=None,
            reply_provider=RecordingProvider(),
            model="fixture-model",
            app_slug="review-sensei[bot]",
            root_comment_id=10,
        )
        self.assertEqual(skipped.status, "skipped_no_mention")
        self.assertEqual(self.broker.exchanges, [])

    def test_generated_reply_authorizes_then_uses_reply_capability(self):
        options = GitHubWriteOptions(github_writes=True, mention_replies=True)
        outcome = self.application.generate_and_publish_reply(
            options=options,
            oidc_token="provided-oidc",
            read_token="read-token",
            repository="owner/repo",
            pull_request=1,
            source_comment_id=10,
            source_updated_at="updated",
            expected_head_sha=None,
            reply_provider=RecordingProvider(),
            model="fixture-model",
            app_slug="review-sensei[bot]",
            root_comment_id=10,
            source_kind="issue",
        )
        self.assertEqual(outcome.status, "replied")
        self.assertEqual(
            [capability for _, capability in self.broker.exchanges],
            ["issue_reply"],
        )
        self.assertEqual(self.replier.prepare_calls[0]["token"], "read-token")
        self.assertEqual(
            [operation for operation, _ in self.replier.reaction_calls],
            ["add", "remove"],
        )
        self.assertEqual(
            self.replier.reaction_calls[0][1]["token"], "capability-issue_reply"
        )
        self.assertEqual(self.replier.reaction_calls[1][1]["reaction_id"], 99)

    def test_generated_reply_removes_processing_reaction_when_provider_fails(self):
        class FailingProvider(RecordingProvider):
            def complete(self, request):
                raise RuntimeError("provider unavailable")

        with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
            self.application.generate_and_publish_reply(
                options=GitHubWriteOptions(
                    github_writes=True,
                    mention_replies=True,
                ),
                oidc_token="provided-oidc",
                read_token="read-token",
                repository="owner/repo",
                pull_request=1,
                source_comment_id=10,
                source_updated_at="updated",
                expected_head_sha=None,
                reply_provider=FailingProvider(),
                model="fixture-model",
                app_slug="review-sensei[bot]",
                root_comment_id=10,
                source_kind="issue",
            )

        self.assertEqual(
            [operation for operation, _ in self.replier.reaction_calls],
            ["add", "remove"],
        )
        self.assertEqual(self.replier.calls, [])


if __name__ == "__main__":
    unittest.main()
