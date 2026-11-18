"""Source-custody negatives, separate from real ordinary-route qualification."""

import copy
import unittest
from types import SimpleNamespace

from review_sensei_github import ordinary_publication_reads as reads
from review_sensei_github.finding_lifecycle import FindingLifecycleJournal
from review_sensei_github.http import GitHubHttp
from review_sensei_github.publication import ReviewPublisher

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.errors import ReviewInputError


class OrdinaryPublicationReadCustodyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if reads._SOURCE is None:
            reads._install_publication_source(ReviewPublisher)

    def fixture(self):
        http = GitHubHttp()
        publisher = ReviewPublisher(http=http)
        journal = object.__new__(FindingLifecycleJournal)
        journal.reviewer = publisher
        journal.ledger = object()
        journal.identity = SimpleNamespace(repository="owner/repo", pull_request=1)
        journal.operation_id = "operation"
        journal.head_sha = "a" * 40
        journal.base_sha = "b" * 40
        journal._native_budget = EvidenceReadBudget()
        journal._readback_http = http
        return publisher, journal, http

    def test_context_restores_and_expires_without_requests(self):
        publisher, journal, http = self.fixture()
        with reads.observe_publication(publisher, journal) as observation:
            observation._require(journal, publisher)
            self.assertIsNot(publisher.http, http)
            self.assertIsNot(journal._readback_http, http)
        self.assertIs(publisher.http, http)
        self.assertIs(journal._readback_http, http)
        with self.assertRaises(ReviewInputError):
            observation._require(journal, publisher)

    def test_copied_observer_and_scope_mutation_fail(self):
        publisher, journal, _ = self.fixture()
        with reads.observe_publication(publisher, journal) as observation:
            with self.assertRaises(ReviewInputError):
                copy.copy(observation)._require(journal, publisher)
            journal.head_sha = "c" * 40
            with self.assertRaises(ReviewInputError):
                observation._require(journal, publisher)

    def test_unissued_witness_and_custom_producer_fail(self):
        publisher, journal, _ = self.fixture()
        witness = reads.OrdinaryPublicationReadWitness(reads._ROLES, 16)
        with self.assertRaises(ReviewInputError):
            witness.require(journal, publisher)
        publisher._publish_impl = lambda *args, **kwargs: None
        with self.assertRaises(ReviewInputError):
            with reads.observe_publication(publisher, journal):
                self.fail("custom producer must not obtain a source observer")

    def test_replaced_transport_and_installer_retry_fail(self):
        publisher, journal, _ = self.fixture()
        with reads.observe_publication(publisher, journal) as observation:
            publisher.http = GitHubHttp()
            with self.assertRaises(ReviewInputError):
                observation._require(journal, publisher)
        with self.assertRaises(ReviewInputError):
            reads._install_publication_source(ReviewPublisher)

    def test_lost_review_comments_role_is_exact_and_priced_twice(self):
        publisher, journal, _ = self.fixture()
        with reads.observe_publication(publisher, journal) as observation:
            prefix = f"/repos/{journal.identity.repository}/pulls/{journal.identity.pull_request}/reviews/"
            self.assertEqual(
                observation.role(
                    "GET", prefix + "123/comments?per_page=100&page=1", None
                ),
                "review_comments",
            )
            for path in (
                prefix + "0/comments",
                prefix + "-1/comments",
                prefix + "123/comments/extra",
                prefix.replace("/reviews/", "/other/") + "123/comments",
            ):
                self.assertIsNone(observation.role("GET", path, None))
        self.assertEqual(dict(reads._ROLES)["review_comments"], 2)
        self.assertEqual(sum(count for _, count in reads._ROLES), 16)

    def test_wrapper_delegate_observer_and_request_are_pinned(self):
        for field, replacement in (
            ("transport", GitHubHttp()),
            ("observation", object()),
            ("request", lambda *args, **kwargs: (200, [])),
        ):
            publisher, journal, _ = self.fixture()
            with self.subTest(field=field):
                with reads.observe_publication(publisher, journal) as observation:
                    setattr(publisher.http, field, replacement)
                    with self.assertRaises(ReviewInputError):
                        observation._require(journal, publisher)

    def test_private_issued_identity_cannot_be_reset_by_public_field(self):
        # Component-only setup of the post-qualification registry. This is not
        # source admission or a substitute for the actual hosted route tests.
        publisher, journal, _ = self.fixture()
        with reads.observe_publication(publisher, journal) as observation:
            witness = reads.OrdinaryPublicationReadWitness(reads._ROLES, 16)
            reads._ISSUED_WITNESS[observation] = witness
            observation.witness = witness
            observation._require(journal, publisher)
            observation.witness = None
            with self.assertRaises(ReviewInputError):
                observation._require(journal, publisher)

    def test_observer_method_replacement_is_not_qualified(self):
        publisher, journal, _ = self.fixture()
        with reads.observe_publication(publisher, journal) as observation:
            observation.read = lambda *args: (200, [])
            with self.assertRaises(ReviewInputError):
                reads._Observation._require(observation, journal, publisher)

    def test_opaque_callbacks_refuse_at_entry_and_after_observation(self):
        for target, name in (
            ("finalizer", "_operation_observer"),
            ("finalizer", "_thread_cleanup_token_provider"),
            ("publisher", "_pending_thread_cleanup"),
        ):
            publisher, journal, _ = self.fixture()
            owner = publisher.finalizer if target == "finalizer" else publisher
            with self.subTest(target=target, name=name):
                with reads.observe_publication(publisher, journal) as observation:
                    setattr(owner, name, object())
                    with self.assertRaisesRegex(ReviewInputError, "callback"):
                        observation._require(journal, publisher)
                with self.assertRaisesRegex(ReviewInputError, "callback"):
                    with reads.observe_publication(publisher, journal):
                        self.fail("opaque callback cannot enter source-bound route")

    def test_configured_unused_publisher_provider_is_not_invoked(self):
        publisher, journal, _ = self.fixture()
        calls = []
        publisher._thread_cleanup_token_provider = lambda: calls.append("called")
        with reads.observe_publication(publisher, journal) as observation:
            observation._require(journal, publisher)
            self.assertEqual(calls, [])
            publisher._pending_thread_cleanup = lambda: None
            with self.assertRaisesRegex(ReviewInputError, "callback"):
                observation._require(journal, publisher)
        self.assertEqual(calls, [])

    def test_foreign_or_copied_transport_cannot_supply_an_observation(self):
        publisher, journal, http = self.fixture()
        called = []
        foreign = SimpleNamespace(request=lambda *a, **k: called.append(True))
        with reads.observe_publication(publisher, journal) as observation:
            reads._REMAINING[observation] = {"pr": 1, "reviews": 1}
            for transport in (foreign, copy.copy(http)):
                with self.subTest(transport=type(transport).__name__):
                    with self.assertRaisesRegex(
                        ReviewInputError, "transport is unbound"
                    ):
                        observation.read(
                            transport, "GET", "/repos/owner/repo/pulls/1", {}
                        )
                    self.assertEqual(reads._OBSERVED[observation], {})
                    self.assertEqual(
                        reads._REMAINING[observation], {"pr": 1, "reviews": 1}
                    )
                    self.assertIsNone(reads.issued_witness(journal))
        self.assertEqual(called, [])
