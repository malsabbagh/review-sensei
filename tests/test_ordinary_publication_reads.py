"""Source-custody negatives, separate from real ordinary-route qualification."""

import copy
import unittest
from types import SimpleNamespace

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.errors import ReviewInputError
from review_sensei_github import ordinary_publication_reads as reads
from review_sensei_github.finding_lifecycle import FindingLifecycleJournal
from review_sensei_github.http import GitHubHttp
from review_sensei_github.publication import ReviewPublisher


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
        witness = reads.OrdinaryPublicationReadWitness(reads._ROLES, 14)
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
