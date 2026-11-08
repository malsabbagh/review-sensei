"""Hosted supersession obtains separate authority on its original envelope."""

import unittest
from unittest.mock import patch

from review_sensei_github.application import GitHubApplication, GitHubWriteOptions
from review_sensei_github.broker_client import BrokerClient
from review_sensei_github.errors import GitHubBrokerClientError
from review_sensei_github.http import GitHubHttp
from review_sensei_github.publication import PublicationResult, ReviewPublisher
from test_github_application import RecordingBroker

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.models import ReviewResult


class HostedCleanupCapabilityTests(unittest.TestCase):
    def publish(self, broker, budget):
        publisher = ReviewPublisher(http=GitHubHttp())
        application = GitHubApplication(
            broker=broker, http=None, reviewer=publisher, learner=None, replier=None
        )
        callbacks = []

        def publish(operation, **kwargs):
            callbacks.append(operation._thread_cleanup_token_provider)
            return PublicationResult(status="published", review_id=1)

        with patch.object(ReviewPublisher, "publish", publish):
            application.publish_review(
                options=GitHubWriteOptions(auto_review=True, github_writes=True),
                oidc_token="synthetic-oidc",
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
                diff="",
                app_slug="review-sensei[bot]",
                publication_budget=budget,
            )
        self.assertIsNone(publisher._thread_cleanup_token_provider)
        return callbacks[0]

    def test_cleanup_capability_is_lazy_and_bound_to_same_oidc(self):
        broker = RecordingBroker()
        budget = EvidenceReadBudget()
        acquire = self.publish(broker, budget)
        self.assertEqual(
            [cap for _, cap in broker.exchanges], ["review_publish", "check_publish"]
        )
        self.assertEqual(acquire(), "capability-thread_resolve")
        self.assertEqual(broker.exchanges[-1], ("synthetic-oidc", "thread_resolve"))

    def test_real_broker_requires_original_accounting_before_cleanup_mint(self):
        from review_sensei_github.broker_control import BrokerDispatchAccounting

        for mismatch in (False, True):
            with self.subTest(mismatch=mismatch):
                budget = EvidenceReadBudget()
                broker = BrokerClient(broker_url="https://broker.test/token")
                if mismatch:
                    broker.control = BrokerDispatchAccounting(
                        EvidenceReadBudget(), "a" * 64, 10, 10
                    )
                with patch.object(
                    BrokerClient, "exchange", return_value="token"
                ) as mint:
                    acquire = self.publish(broker, budget)
                    before = mint.call_count
                    with self.assertRaisesRegex(GitHubBrokerClientError, "accounting"):
                        acquire()
                    self.assertEqual(mint.call_count, before)

    def test_matching_broker_keeps_original_meter_for_cleanup_exchange(self):
        from review_sensei_github.broker_control import BrokerDispatchAccounting

        budget = EvidenceReadBudget()
        broker = BrokerClient(
            broker_url="https://broker.test/token",
            control=BrokerDispatchAccounting(budget, "a" * 64, 10, 10),
        )
        seen = []

        def exchange(client, oidc, *, capability=None):
            self.assertIs(client.control.budget, budget)
            self.assertEqual(client.before_request, budget.consume)
            before = budget.ordinary_remaining()
            client.before_request()
            self.assertEqual(budget.ordinary_remaining(), before - 1)
            seen.append((oidc, capability))
            return "synthetic-token"

        with patch.object(BrokerClient, "exchange", exchange):
            acquire = self.publish(broker, budget)
            acquire()
        self.assertEqual(seen[-1], ("synthetic-oidc", "thread_resolve"))
