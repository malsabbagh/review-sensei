"""Renderer lookup follows only the registered lifecycle adapter identity."""

import unittest
from types import SimpleNamespace

from review_sensei_github import checkpoint_result_store as formats
from review_sensei_github.finding_lifecycle import (
    FindingLifecycleJournal,
    _JournalTransport,
)
from review_sensei_github.http import GitHubHttp

from review_sensei.errors import ReviewInputError
from review_sensei.session import SessionIdentity


class CheckpointFormatTransportTests(unittest.TestCase):
    def setUp(self):
        self.http = GitHubHttp()
        self.identity = SessionIdentity("owner/repo", 7, 123)
        self.journal = FindingLifecycleJournal(
            SimpleNamespace(),
            None,
            self.identity,
            operation_id="test",
            base_sha="a" * 40,
            head_sha="b" * 40,
            policy_digest="c" * 64,
            configuration_digest="d" * 64,
        )
        self.adapter = _JournalTransport(self.journal, self.http)
        self.transaction = SimpleNamespace(
            transaction_id="test", result_sha256="e" * 64
        )
        self.result = SimpleNamespace(transaction=self.transaction)
        # This unit tests identity plumbing, not capture/authentication or a
        # publication grant. Actual descriptor qualification remains separate.
        formats._READ_FORMATS[self.http] = {
            ("test", "e" * 64): "bounded-inline-groups-v1"
        }

    def tearDown(self):
        formats._READ_FORMATS.pop(self.http, None)

    def test_registered_original_adapter_preserves_selected_format(self):
        self.assertTrue(formats.uses_grouped_publication(self.http, self.result))
        self.assertTrue(formats.uses_grouped_publication(self.adapter, self.result))
        nested = _JournalTransport(self.journal, self.adapter)
        self.assertTrue(formats.uses_grouped_publication(nested, self.result))
        self.assertIsNone(
            formats.publication_format(self.adapter, SimpleNamespace(transaction=None))
        )
        self.assertIsNone(
            formats.publication_format(
                self.adapter,
                SimpleNamespace(
                    transaction=SimpleNamespace(
                        transaction_id="other", result_sha256="e" * 64
                    )
                ),
            )
        )

    def test_hot_budgeted_delegate_preserves_budget_and_legacy_clear(self):
        from review_sensei_github.command_finalization_plan import _underlying_http
        from review_sensei_github.publication_recovery import PublicationBudgetHttp

        from review_sensei.bounded_evidence import EvidenceReadBudget

        budget = EvidenceReadBudget()
        delegate = PublicationBudgetHttp(self.http, budget)
        adapter = _JournalTransport(self.journal, delegate)
        before = dict(vars(budget))
        self.assertIsNone(self.journal._native_budget)
        self.assertTrue(formats.uses_grouped_publication(adapter, self.result))
        self.assertIs(delegate.publication_budget, budget)
        self.assertEqual(vars(budget), before)
        # Format lookup did not weaken the independent command-budget guard.
        with self.assertRaisesRegex(ReviewInputError, "accounting transport"):
            _underlying_http(adapter, budget=None)
        store = formats.CheckpointResultStore(
            object(),
            app_user_id=999,
            http=adapter,
            identity=self.identity,
            transaction=self.transaction,
            app_id=999,
            compact_write=False,
        )
        self.assertEqual(store._decode(b"{}"), b"{}")
        self.assertFalse(formats.uses_grouped_publication(adapter, self.result))
        self.assertFalse(formats.uses_grouped_publication(self.http, self.result))
        self.assertEqual(vars(budget), before)

    def test_known_budget_wrapper_cycle_refuses(self):
        from review_sensei_github.publication_recovery import PublicationBudgetHttp

        from review_sensei.bounded_evidence import EvidenceReadBudget

        delegate = PublicationBudgetHttp(self.http, EvidenceReadBudget())
        delegate.transport = delegate
        with self.assertRaisesRegex(ReviewInputError, "chain is unqualified"):
            formats.publication_format(delegate, self.result)

    def test_copy_and_unregistered_adapter_do_not_inherit_identity(self):
        for adapter in (object.__new__(_JournalTransport),):
            vars(adapter).update(vars(self.adapter))
            with self.subTest(adapter=adapter), self.assertRaises(ReviewInputError):
                formats.publication_format(adapter, self.result)

    def test_delegate_and_request_replacement_refuse(self):
        self.adapter.delegate = GitHubHttp()
        with self.assertRaises(ReviewInputError):
            formats.publication_format(self.adapter, self.result)
        self.adapter.delegate = self.http
        self.adapter.request = self.http.request
        with self.assertRaises(ReviewInputError):
            formats.publication_format(self.adapter, self.result)

    def test_cyclic_delegate_refuses(self):
        self.adapter.delegate = self.adapter
        with self.assertRaises(ReviewInputError):
            formats.publication_format(self.adapter, self.result)

    def test_raw_legacy_decode_through_adapter_clears_same_identity(self):
        store = formats.CheckpointResultStore(
            object(),
            app_user_id=999,
            http=self.adapter,
            identity=self.identity,
            transaction=self.transaction,
            app_id=999,
            compact_write=False,
        )
        self.assertEqual(store._decode(b"{}"), b"{}")
        self.assertFalse(formats.uses_grouped_publication(self.http, self.result))
        self.assertFalse(formats.uses_grouped_publication(self.adapter, self.result))
