import unittest

from review_sensei import ProviderResponse
from review_sensei.hosting.github import (
    GitHubApplication,
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
    ReviewResult,
)


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

    def test_enabled_operations_use_narrow_capabilities(self):
        options = GitHubWriteOptions(
            auto_review=True,
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
        self.assertNotIn("auto_approve", self.reviewer.calls[0])
        self.assertEqual(self.learner.calls[0]["token"], "capability-learning_write")
        self.assertEqual(self.replier.calls[0]["token"], "capability-issue_reply")
        self.assertNotIn("auto_approve", self.replier.calls[0])

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
