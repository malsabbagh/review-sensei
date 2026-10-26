import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

from review_sensei_github import (
    GitHubApplication,
    GitHubPublicationError,
    GitHubWriteOptions,
    PreparedConversation,
    PublicationResult,
    ReplyResult,
)
from review_sensei_github.operation_comments import ResultPartStore
from review_sensei_github.operation_host import GitHubCommentOperationStore

from review_sensei import ProviderResponse
from review_sensei.baseline import ReviewBaseline, baseline_history_document
from review_sensei.context import ReviewContextCacheKey, finding_lifecycle_for_comment
from review_sensei.convergence import BlockerCandidate, ReviewConvergencePolicy
from review_sensei.disposition import apply_session_command, parse_maintainer_command
from review_sensei.errors import ProviderError, ReviewInputError
from review_sensei.models import (
    ConversationContext,
    ConversationMessage,
    ConversationReply,
    LearningProposal,
    ReviewComment,
    ReviewResult,
)
from review_sensei.outcomes import (
    RecoveryArtifact,
    ResourceBudget,
    ResourceBudgetTracker,
)
from review_sensei.session import (
    InMemorySessionLedger,
    SessionIdentity,
    SessionRecord,
)
from tests.conversation_deadline_contract import Clock, Provider
from tests.test_operation_entry import MemoryComments


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

    def test_gate_reevaluation_failure_is_an_evaluation_failure(self):
        from review_sensei.disposition import MaintainerCommandResult

        record = SessionRecord.create(
            SessionIdentity("owner/repo", 1, repository_id=1),
            now=datetime(2026, 9, 19, tzinfo=timezone.utc),
        )
        self.reviewer.finalizer = SimpleNamespace(
            refinalize_with_dispositions=lambda **kwargs: PublicationResult(
                status="gate_reevaluation_failed"
            )
        )
        updated = self.application._refinalize_command(
            MaintainerCommandResult(
                action="verify",
                applied=True,
                operator_paused=False,
                summary="overrides for this commit are loaded",
            ),
            oidc_token="oidc",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha="a" * 40,
            app_slug="review-sensei[bot]",
            actor="alice",
            record=record,
        )
        self.assertTrue(updated.evaluation_failed)
        self.assertIn("gate re-evaluation failed", updated.summary)

    def test_withheld_approval_is_not_an_evaluation_failure(self):
        from review_sensei.disposition import MaintainerCommandResult

        record = SessionRecord.create(
            SessionIdentity("owner/repo", 1, repository_id=1),
            now=datetime(2026, 9, 19, tzinfo=timezone.utc),
        )
        self.reviewer.finalizer = SimpleNamespace(
            refinalize_with_dispositions=lambda **kwargs: PublicationResult(
                status="approval_withheld",
                diagnostic="human_adjudication_open",
            )
        )
        updated = self.application._refinalize_command(
            MaintainerCommandResult(
                action="verify",
                applied=True,
                operator_paused=False,
                summary="overrides for this commit are loaded",
            ),
            oidc_token="oidc",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha="a" * 40,
            app_slug="review-sensei[bot]",
            actor="alice",
            record=record,
        )
        self.assertFalse(updated.evaluation_failed)
        self.assertIn("approval withheld (human_adjudication_open)", updated.summary)
        self.assertNotIn("original media confirmation", updated.summary)

    def test_verify_explains_partial_review_and_separate_media_finalization(self):
        from review_sensei.disposition import MaintainerCommandResult

        record = SessionRecord.create(
            SessionIdentity("owner/repo", 1, repository_id=1),
            now=datetime(2026, 9, 19, tzinfo=timezone.utc),
        )
        publication = PublicationResult(
            status="approval_withheld", diagnostic="review_incomplete"
        )
        self.reviewer.finalizer = SimpleNamespace(
            refinalize_with_dispositions=lambda **kwargs: publication
        )
        updated = self.application._refinalize_command(
            MaintainerCommandResult(
                action="verify",
                applied=True,
                operator_paused=False,
                summary="overrides for this commit are loaded",
            ),
            oidc_token="oidc",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha="a" * 40,
            app_slug="review-sensei[bot]",
            actor="alice",
            record=record,
        )
        self.assertFalse(updated.evaluation_failed)
        self.assertIn("approval withheld (review_incomplete)", updated.summary)
        self.assertIn("review evidence is still incomplete", updated.summary)
        self.assertIn("verify rechecks finding decisions", updated.summary)
        self.assertIn("original media confirmation", updated.summary)
        self.assertIn("current head/base and retained receipts", updated.summary)
        self.assertNotIn("approval submitted", updated.summary)
        self.assertEqual(publication.status, "approval_withheld")
        self.assertEqual(record.dispositions, ())

    def test_persistent_command_capacity_failure_does_not_request_identical_retry(self):
        from review_sensei.disposition import MaintainerCommandResult

        record = SessionRecord.create(
            SessionIdentity("owner/repo", 1, repository_id=1),
            now=datetime(2026, 9, 19, tzinfo=timezone.utc),
        )
        for diagnostic in (
            "command_body_capacity_exceeded",
            "command_body_capacity_unverifiable",
        ):
            with self.subTest(diagnostic=diagnostic):
                self.reviewer.finalizer = SimpleNamespace(
                    refinalize_with_dispositions=lambda **kwargs: PublicationResult(
                        status="gate_reevaluation_failed", diagnostic=diagnostic
                    )
                )
                updated = self.application._refinalize_command(
                    MaintainerCommandResult(
                        action="verify",
                        applied=True,
                        operator_paused=False,
                        summary="overrides for this commit are loaded",
                    ),
                    oidc_token="oidc",
                    repository="owner/repo",
                    repository_id=1,
                    pull_request=1,
                    head_sha="a" * 40,
                    app_slug="review-sensei[bot]",
                    actor="alice",
                    record=record,
                )
                self.assertTrue(updated.evaluation_failed)
                self.assertIn("decisions retained", updated.summary)
                self.assertIn(diagnostic, updated.summary)
                self.assertNotIn("@reviewsensei verify", updated.summary)
                self.assertEqual(record.dispositions, ())

    def test_refinalize_exception_is_an_evaluation_failure(self):
        from review_sensei.disposition import MaintainerCommandResult

        record = SessionRecord.create(
            SessionIdentity("owner/repo", 1, repository_id=1),
            now=datetime(2026, 9, 19, tzinfo=timezone.utc),
        )
        self.reviewer.finalizer = SimpleNamespace(
            refinalize_with_dispositions=lambda **kwargs: PublicationResult(
                status="approved"
            )
        )
        with patch.object(
            self.application.broker,
            "exchange",
            side_effect=RuntimeError("broker down"),
        ):
            updated = self.application._refinalize_command(
                MaintainerCommandResult(
                    action="verify",
                    applied=True,
                    operator_paused=False,
                    summary="overrides for this commit are loaded",
                ),
                oidc_token="oidc",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha="a" * 40,
                app_slug="review-sensei[bot]",
                actor="alice",
                record=record,
            )
        self.assertTrue(updated.evaluation_failed)
        self.assertIn("gate re-evaluation failed", updated.summary)

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
            result=ReviewResult(
                summary="Summary.",
                comments=(),
                provider="fixture",
                review_status="complete",
            ),
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
        # The review gate and the review itself are separate authorities, so
        # publishing a review exchanges both narrow capabilities.
        self.assertEqual(
            [capability for _, capability in self.broker.exchanges],
            ["review_publish", "check_publish", "learning_write", "issue_reply"],
        )
        self.assertEqual(self.broker.requested, 1)
        self.assertEqual(self.reviewer.calls[0]["token"], "capability-review_publish")
        self.assertEqual(
            self.reviewer.calls[0]["check_token"], "capability-check_publish"
        )
        self.assertTrue(self.reviewer.calls[0]["auto_approve"])
        self.assertEqual(self.learner.calls[0]["token"], "capability-learning_write")
        self.assertEqual(self.replier.calls[0]["token"], "capability-issue_reply")

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

    def test_publish_review_rejects_current_key_for_different_publication_identity(
        self,
    ):
        current_key = ReviewContextCacheKey(
            repository="other/repo",
            pull_request=1,
            base_sha="b" * 40,
            head_sha="a" * 40,
            engine="fixture",
            model="fixture-model",
            profile="default",
            stage_digest="1" * 64,
            context_digest="2" * 64,
            learning_digest="3" * 64,
        )
        with self.assertRaisesRegex(
            GitHubPublicationError, "does not match publication identity"
        ):
            self.application.publish_review(
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
                diff="diff",
                app_slug="review-sensei[bot]",
                current_key=current_key,
            )
        self.assertEqual(self.broker.requested, 0)
        self.assertEqual(self.broker.exchanges, [])
        self.assertEqual(self.reviewer.calls, [])

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
            # Pin the low-level compatibility path now that the omitted
            # runtime policy resolves to merge-focused.
            convergence_policy=ReviewConvergencePolicy(mode="legacy"),
        )
        self.assertEqual(outcome.status, "published")
        forwarded = self.reviewer.calls[-1]["authorized_dispositions"]
        self.assertEqual(len(forwarded), 1)
        self.assertEqual(forwarded[0].fingerprint, fingerprint)

    def test_old_hosted_attestations_cannot_replace_current_action_authority(self):
        for version in (1, 2, 3):
            with (
                self.subTest(version=version),
                mock.patch.object(
                    self.application,
                    "_session_ledger_for_token",
                    side_effect=AssertionError("old writer must not be resolved"),
                ),
            ):
                with self.assertRaisesRegex(
                    GitHubPublicationError, "current metered command lifecycle"
                ):
                    self.application.apply_maintainer_command(
                        options=GitHubWriteOptions(
                            github_writes=True, github_session_ledger=True
                        ),
                        oidc_token=None,
                        repository="owner/repo",
                        repository_id=99,
                        pull_request=136,
                        head_sha="b" * 40,
                        body="@sensei review pause",
                        actor_login="alice",
                        association="MEMBER",
                        app_slug="reviewsensei[bot]",
                        source_comment_id=71,
                        session_attestation={"version": version, "actor": "alice"},
                    )
                self.assertEqual(self.broker.requested, 0)
                self.assertEqual(self.broker.exchanges, [])
                self.assertEqual(self.reviewer.calls, [])

    def test_handler_fails_closed_on_prefilter_over_accepted_bodies(self):
        # The caller prefilter is deliberately wider than the authoritative
        # parser (ADR 0050), so it routes bodies to the command job that the
        # parser then refuses: as conversational syntax (the mention-casing,
        # second-mention, and Unicode separator shapes) or as a disposition
        # reason that breaks the parser's bound. Both refusal shapes must stop
        # before the broker exchange, and the reason-bound refusals must be
        # the documented ones rather than any error the handler happens to
        # raise.
        write_options = {
            "options": GitHubWriteOptions(
                github_writes=True, github_session_ledger=True
            ),
            "oidc_token": "caller-oidc",
            "repository": "owner/repo",
            "repository_id": 99,
            "pull_request": 136,
            "head_sha": "b" * 40,
            "actor_login": "alice",
            "association": "MEMBER",
            "app_slug": "reviewsensei[bot]",
            "source_comment_id": 71,
        }
        for body, refusal in (
            ("@Sensei review pause", None),
            ("@sensei review pause\n@sensei review pause", None),
            ("@sensei review\u2003reenroll", None),
            ('@sensei dismiss abcd1234abcd1234 --reason ""', "requires a reason"),
            (
                "@sensei dismiss abcd1234abcd1234 --reason " + "x" * 513,
                "exceeds the bound",
            ),
        ):
            with self.subTest(body=body):
                if refusal is None:
                    result = self.application.apply_maintainer_command(
                        body=body, **write_options
                    )
                    self.assertEqual(result.summary, "not-a-command")
                    self.assertFalse(result.applied)
                else:
                    # The CLI maps this refusal to a non-zero exit, so the
                    # workflow fails before reaching the broker.
                    with self.assertRaisesRegex(ReviewInputError, refusal):
                        self.application.apply_maintainer_command(
                            body=body, **write_options
                        )
                self.assertEqual(self.broker.exchanges, [])

    def test_hosted_command_requires_current_action_before_missing_source_lookup(self):
        with self.assertRaisesRegex(
            GitHubPublicationError, "current metered command lifecycle"
        ):
            self.application.apply_maintainer_command(
                options=GitHubWriteOptions(
                    github_writes=True, github_session_ledger=True
                ),
                oidc_token=None,
                repository="owner/repo",
                repository_id=99,
                pull_request=136,
                head_sha="b" * 40,
                body="@sensei review pause",
                actor_login="alice",
                association="MEMBER",
                app_slug="reviewsensei[bot]",
            )
        self.assertEqual(self.broker.requested, 0)
        self.assertEqual(self.broker.exchanges, [])

    def test_unverified_attestation_scope_cannot_authorize_a_hosted_command(self):
        scope = dict(
            repository="owner/repo",
            repository_id=99,
            pull_request=136,
            head_sha="b" * 40,
            source_comment_id=71,
        )
        # Current scope is bound by VerifiedRunAction. Old attestation dictionaries
        # cannot authorize even an otherwise matching delivery or a changed target.
        for changed in (
            {},
            {"pull_request": 137},
            {"head_sha": "c" * 40},
            {"repository": "other/repo"},
            {"source_comment_id": 72},
        ):
            with self.subTest(changed=changed):
                with self.assertRaisesRegex(
                    GitHubPublicationError, "current metered command lifecycle"
                ):
                    self.application.apply_maintainer_command(
                        options=GitHubWriteOptions(
                            github_writes=True, github_session_ledger=True
                        ),
                        oidc_token=None,
                        **(scope | changed),
                        body="@sensei review pause",
                        actor_login="alice",
                        association="MEMBER",
                        app_slug="reviewsensei[bot]",
                        session_attestation={"version": 3, **scope},
                    )
                self.assertEqual(self.broker.requested, 0)
                self.assertEqual(self.broker.exchanges, [])

    def test_duck_typed_current_action_cannot_authorize_untrusted_actor_fields(self):
        # A caller-provided object with the right-looking fields/method is not a
        # process-owned VerifiedRunAction. Its require method must never run.
        for actor_type, association in (
            ("Bot", "OWNER"),
            ("User", "NONE"),
            ("User", "MEMBER"),
        ):
            with self.subTest(actor_type=actor_type, association=association):
                forged = SimpleNamespace(
                    document={
                        "version": 3,
                        "actor": {
                            "login": "alice",
                            "type": actor_type,
                            "association": association,
                        },
                    },
                    require=mock.Mock(
                        side_effect=AssertionError("unverified authority")
                    ),
                )
                with self.assertRaisesRegex(
                    GitHubPublicationError, "current command authority is invalid"
                ):
                    self.application.apply_maintainer_command(
                        options=GitHubWriteOptions(
                            github_writes=True, github_session_ledger=True
                        ),
                        oidc_token=None,
                        repository="owner/repo",
                        repository_id=99,
                        pull_request=136,
                        head_sha="b" * 40,
                        body="@sensei review pause",
                        actor_login="alice",
                        actor_type=actor_type,
                        association=association,
                        app_slug="reviewsensei[bot]",
                        source_comment_id=71,
                        current_action=forged,
                    )
                forged.require.assert_not_called()
                self.assertEqual(self.broker.requested, 0)
                self.assertEqual(self.broker.exchanges, [])

    def test_local_continue_unpauses_without_a_round_grant(self):
        ledger = InMemorySessionLedger()
        application = GitHubApplication(
            broker=self.broker,
            http=None,
            reviewer=self.reviewer,
            learner=self.learner,
            replier=self.replier,
            session_ledger=ledger,
        )
        identity = SessionIdentity("owner/repo", 136, repository_id=99)

        def deliver(source_comment_id, body="@sensei review continue"):
            return application.apply_maintainer_command(
                options=GitHubWriteOptions(github_writes=True),
                oidc_token="caller-oidc",
                repository="owner/repo",
                repository_id=99,
                pull_request=136,
                head_sha="b" * 40,
                body=body,
                actor_login="alice",
                actor_type="User",
                association="MEMBER",
                app_slug="reviewsensei[bot]",
                source_comment_id=source_comment_id,
            )

        paused = deliver(70, body="@sensei review pause")
        self.assertTrue(paused.applied)
        self.assertTrue(ledger.load(identity).record.operator_paused)
        first = deliver(71)
        self.assertTrue(first.applied)
        self.assertEqual(
            first.summary, "automated review may continue; rounds are uncapped"
        )
        resumed = ledger.load(identity).record
        self.assertFalse(resumed.operator_paused)
        self.assertEqual(resumed.continuation_grants, ())
        replay = deliver(71)
        self.assertTrue(replay.applied)
        self.assertEqual(ledger.load(identity).record.generation, resumed.generation)
        distinct = deliver(72)
        self.assertTrue(distinct.applied)
        self.assertEqual(ledger.load(identity).record.generation, resumed.generation)
        retired = deliver(73, body="@sensei review continue --rounds 1")
        self.assertFalse(retired.applied)
        self.assertEqual(retired.summary, "not-a-command")
        after = ledger.load(identity).record
        self.assertEqual(after.generation, resumed.generation)
        self.assertEqual(after.continuation_grants, ())
        self.assertFalse(after.operator_paused)
        self.assertEqual(self.broker.requested, 0)
        self.assertEqual(self.broker.exchanges, [("caller-oidc", "review_publish")] * 4)

    def test_hosted_command_rejects_an_injected_local_ledger(self):
        application = GitHubApplication(
            broker=self.broker,
            http=None,
            reviewer=self.reviewer,
            learner=self.learner,
            replier=self.replier,
            session_ledger=InMemorySessionLedger(),
        )

        with self.assertRaisesRegex(GitHubPublicationError, "injected local"):
            application.apply_maintainer_command(
                options=GitHubWriteOptions(
                    github_writes=True, github_session_ledger=True
                ),
                oidc_token="caller-oidc",
                repository="owner/repo",
                repository_id=99,
                pull_request=136,
                head_sha="b" * 40,
                body="@sensei review pause",
                actor_login="mallory",
                actor_type="User",
                association="MEMBER",
                app_slug="reviewsensei[bot]",
            )

    def test_hosted_status_does_not_read_an_injected_local_ledger(self):
        application = GitHubApplication(
            broker=self.broker,
            http=None,
            reviewer=self.reviewer,
            learner=self.learner,
            replier=self.replier,
            session_ledger=InMemorySessionLedger(),
        )

        with self.assertRaisesRegex(GitHubPublicationError, "requires HTTP"):
            application.apply_maintainer_command(
                options=GitHubWriteOptions(
                    github_writes=True, github_session_ledger=True
                ),
                oidc_token="caller-oidc",
                repository="owner/repo",
                repository_id=99,
                pull_request=136,
                head_sha="b" * 40,
                body="@sensei review status",
                actor_login="mallory",
                actor_type="User",
                association="MEMBER",
                app_slug="reviewsensei[bot]",
            )
        self.assertEqual(self.broker.exchanges, [("caller-oidc", "review_status")])

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
        legacy_kwargs["convergence_policy"] = ReviewConvergencePolicy(mode="legacy")
        legacy_outcome = legacy_application.publish_review(**legacy_kwargs)
        self.assertEqual(legacy_outcome.status, "published")
        self.assertEqual(len(legacy_reviewer.calls), 1)
        self.assertIsNone(legacy_reviewer.calls[0]["baseline"])
        self.assertIsNone(legacy_reviewer.calls[0]["current_key"])
        self.assertIsNone(legacy_reviewer.calls[0]["changed_paths"])
        self.assertEqual(legacy_reviewer.calls[0]["related_paths"], ())
        self.assertEqual(legacy_reviewer.calls[0]["evidence_confirmed_concerns"], ())

        with patch(
            "review_sensei_github.application.read_session_baseline",
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

    def test_command_routed_to_reply_posts_guidance_without_calling_the_provider(self):
        provider = RecordingProvider()
        calls = []

        def complete(request):
            calls.append(request)
            raise AssertionError("provider must not run")

        provider.complete = complete  # type: ignore[method-assign]
        self.replier.prepare_context = lambda **kwargs: PreparedConversation(
            source_kind="issue",
            source_comment_id=10,
            source_updated_at="updated",
            head_sha="a" * 40,
            root_comment_id=10,
            context=ConversationContext(
                messages=(
                    ConversationMessage(
                        author="alice",
                        body=(
                            "@reviewsensei override RS-AAAAAA RS-BBBBBB acceptable risk"
                        ),
                        created_at="updated",
                    ),
                ),
                pull_request_number=1,
                head_sha="a" * 40,
            ),
        )
        outcome = self.application.generate_and_publish_reply(
            options=GitHubWriteOptions(github_writes=True, mention_replies=True),
            oidc_token="provided-oidc",
            read_token="read-token",
            repository="owner/repo",
            pull_request=1,
            source_comment_id=10,
            source_updated_at="updated",
            expected_head_sha=None,
            reply_provider=provider,
            model="fixture-model",
            app_slug="review-sensei[bot]",
            root_comment_id=10,
            source_kind="issue",
        )
        self.assertEqual(outcome.status, "replied")
        self.assertEqual(calls, [])
        self.assertEqual(self.replier.reaction_calls, [])
        self.assertIn(
            "predates multi-finding overrides",
            self.replier.calls[0]["reply"].body,
        )

    def test_single_command_routed_to_reply_is_not_called_a_stale_caller(self):
        self.replier.prepare_context = lambda **kwargs: PreparedConversation(
            source_kind="issue",
            source_comment_id=10,
            source_updated_at="updated",
            head_sha="a" * 40,
            root_comment_id=10,
            context=ConversationContext(
                messages=(
                    ConversationMessage(
                        author="alice",
                        body="@reviewsensei verify",
                        created_at="updated",
                    ),
                ),
                pull_request_number=1,
                head_sha="a" * 40,
            ),
        )
        outcome = self.application.generate_and_publish_reply(
            options=GitHubWriteOptions(github_writes=True, mention_replies=True),
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
        self.assertNotIn(
            "predates multi-finding overrides",
            self.replier.calls[0]["reply"].body,
        )

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

        with self.assertRaisesRegex(
            ProviderError, "conversation provider transport or completion failed"
        ):
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

    def _discussion(self, provider, **kwargs):
        return self.application.generate_and_publish_reply(
            options=GitHubWriteOptions(github_writes=True, mention_replies=True),
            oidc_token="synthetic-oidc",
            read_token="synthetic-read",
            repository="owner/repo",
            pull_request=1,
            source_comment_id=10,
            source_updated_at="updated",
            expected_head_sha=None,
            reply_provider=provider,
            model=provider.model,
            app_slug="review-sensei[bot]",
            root_comment_id=10,
            source_kind="issue",
            **kwargs,
        )

    def test_ordinary_discussion_context_and_operation_consume_original_deadline(self):
        clock = Clock()
        trackers = []

        def factory(budget):
            value = ResourceBudgetTracker(budget, monotonic=clock, sleeper=clock.sleep)
            trackers.append(value)
            return value

        prepare = self.replier.prepare_context

        def prepare_context(**kwargs):
            clock.value += 0.3
            return prepare(**kwargs)

        def enter(**kwargs):
            clock.value += 0.2
            return kwargs["execute"]()

        def malformed(request):
            clock.value += 0.2
            return "{"

        self.replier.prepare_context = prepare_context
        provider = Provider([malformed, '{"body":"ok"}'])
        with (
            patch(
                "review_sensei_github.application.ResourceBudgetTracker",
                side_effect=factory,
            ),
            patch.object(self.application, "_through_operation", side_effect=enter),
        ):
            reply = self._discussion(
                provider, budget=ResourceBudget.create(timeout_ms=1000)
            )
        self.assertEqual(reply.status, "replied")
        self.assertEqual(len(trackers), 1)
        self.assertAlmostEqual(provider.requests[0].timeout_seconds, 0.5)
        self.assertAlmostEqual(provider.requests[1].timeout_seconds, 0.3)
        self.assertEqual(trackers[0].provider_calls, 2)
        self.assertEqual(len(self.replier.calls), 1)

    def test_ordinary_discussion_expired_during_context_never_generates_or_publishes(
        self,
    ):
        clock = Clock()
        prepare = self.replier.prepare_context

        def prepare_context(**kwargs):
            clock.value += 1.1
            return prepare(**kwargs)

        self.replier.prepare_context = prepare_context
        provider = Provider(['{"body":"must not run"}'])
        with (
            patch(
                "review_sensei_github.application.ResourceBudgetTracker",
                side_effect=lambda budget: ResourceBudgetTracker(
                    budget, monotonic=clock
                ),
            ),
            self.assertRaises(ReviewInputError) as raised,
        ):
            self._discussion(provider, budget=ResourceBudget.create(timeout_ms=1000))
        self.assertEqual(raised.exception.diagnostic, "deadline_exceeded")
        self.assertEqual(provider.requests, [])
        self.assertEqual(self.replier.calls, [])
        self.assertEqual(
            [operation for operation, _ in self.replier.reaction_calls],
            ["add", "remove"],
        )

    def test_ordinary_discussion_call_exhaustion_and_cancellation_never_publish(self):
        for action, error, budget in (
            ("{", ReviewInputError, ResourceBudget.create(max_provider_calls=1)),
            (KeyboardInterrupt(), KeyboardInterrupt, ResourceBudget.create()),
        ):
            with self.subTest(error=error):
                self.replier.calls.clear()
                self.replier.reaction_calls.clear()
                provider = Provider([action])
                with self.assertRaises(error):
                    self._discussion(provider, budget=budget)
                self.assertEqual(len(provider.requests), 1)
                self.assertEqual(self.replier.calls, [])
                self.assertEqual(
                    [operation for operation, _ in self.replier.reaction_calls],
                    ["add", "remove"],
                )

    def _operation_parts(self):
        comments = MemoryComments()
        store = GitHubCommentOperationStore(comments, app_user_id=42)
        parts = ResultPartStore(comments, app_user_id=42)
        return store, parts

    def test_reply_reassessment_verify_and_media_use_one_entry(self):
        store, parts = self._operation_parts()
        calls = {"n": 0}

        class CountingProvider(RecordingProvider):
            def complete(self, request):
                calls["n"] += 1
                return super().complete(request)

        common = dict(
            operation_entry="enabled",
            repository_id=7,
            app_id=99,
            run_id="100",
            run_attempt="1",
            operation_store=store,
            operation_parts=parts,
        )
        first = self.application.generate_and_publish_reply(
            options=GitHubWriteOptions(github_writes=True, mention_replies=True),
            oidc_token="provided-oidc",
            read_token="read-token",
            repository="owner/repo",
            pull_request=11,
            source_comment_id=10,
            source_updated_at="updated",
            expected_head_sha=None,
            reply_provider=CountingProvider(),
            model="fixture-model",
            app_slug="review-sensei[bot]",
            root_comment_id=10,
            source_kind="issue",
            **common,
        )
        second = self.application.generate_and_publish_reply(
            options=GitHubWriteOptions(github_writes=True, mention_replies=True),
            oidc_token="provided-oidc",
            read_token="read-token",
            repository="owner/repo",
            pull_request=11,
            source_comment_id=10,
            source_updated_at="updated",
            expected_head_sha=None,
            reply_provider=CountingProvider(),
            model="fixture-model",
            app_slug="review-sensei[bot]",
            root_comment_id=10,
            source_kind="issue",
            **common,
        )
        self.assertEqual(first.status, "replied")
        self.assertEqual(second.status, "replied")
        self.assertEqual(calls["n"], 1)

        self.application.session_ledger = InMemorySessionLedger()
        verify_store, verify_parts = self._operation_parts()
        verify_common = dict(common)
        verify_common["operation_store"] = verify_store
        verify_common["operation_parts"] = verify_parts
        with patch(
            "review_sensei.disposition.apply_session_command",
            wraps=apply_session_command,
        ) as verify_calls:
            verify = self.application.apply_maintainer_command(
                options=GitHubWriteOptions(github_writes=True),
                oidc_token=None,
                repository="owner/repo",
                pull_request=11,
                head_sha="a" * 40,
                body="@reviewsensei verify",
                actor_login="alice",
                association="OWNER",
                app_slug="reviewsensei[bot]",
                source_comment_id=10,
                **verify_common,
            )
            again = self.application.apply_maintainer_command(
                options=GitHubWriteOptions(github_writes=True),
                oidc_token=None,
                repository="owner/repo",
                pull_request=11,
                head_sha="a" * 40,
                body="@reviewsensei verify",
                actor_login="alice",
                association="OWNER",
                app_slug="reviewsensei[bot]",
                source_comment_id=10,
                **verify_common,
            )
        self.assertEqual(verify_calls.call_count, 1)
        self.assertEqual(again.summary, verify.summary)

        class Media:
            def __init__(self):
                self.calls = []

            def confirm(self, **kwargs):
                self.calls.append("confirm")
                return kwargs

            def publish_mixed_approval(self, **kwargs):
                self.calls.append("publish")
                return type(
                    "Assessment",
                    (),
                    {"approval": type("Approval", (), {"approved": True})()},
                )()

        media = Media()
        media_store, media_parts = self._operation_parts()
        media_common = dict(common)
        media_common["operation_store"] = media_store
        media_common["operation_parts"] = media_parts
        approved = self.application.apply_maintainer_command(
            options=GitHubWriteOptions(github_writes=True),
            oidc_token=None,
            repository="owner/repo",
            pull_request=11,
            head_sha="a" * 40,
            body="@reviewsensei I reviewed the media files and I approve",
            actor_login="alice",
            association="OWNER",
            app_slug="reviewsensei[bot]",
            source_comment_id=10,
            media_publisher=media,
            review_result=object(),
            request_comment_id=4,
            **media_common,
        )
        replayed = self.application.apply_maintainer_command(
            options=GitHubWriteOptions(github_writes=True),
            oidc_token=None,
            repository="owner/repo",
            pull_request=11,
            head_sha="a" * 40,
            body="@reviewsensei I reviewed the media files and I approve",
            actor_login="alice",
            association="OWNER",
            app_slug="reviewsensei[bot]",
            source_comment_id=10,
            media_publisher=media,
            review_result=object(),
            request_comment_id=4,
            **media_common,
        )
        self.assertEqual(media.calls, ["confirm", "publish"])
        self.assertTrue(approved.applied)
        self.assertTrue(replayed.applied)


if __name__ == "__main__":
    unittest.main()
