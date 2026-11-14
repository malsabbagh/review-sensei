"""Optional capacity selection must not seal an unrelated ordinary publisher."""

import unittest
from unittest.mock import patch

from review_sensei_github.http import GitHubHttp
from review_sensei_github.publication import ReviewPublisher, _BudgetedReviewPagination
from review_sensei_github.publication_recovery import PublicationBudgetHttp
from review_sensei_github.quarantine_scan_capacity import capacity_review_walk

from review_sensei.bounded_evidence import EvidenceReadBudget


class ReviewCapacityNonmemberTests(unittest.TestCase):
    def test_original_plain_http_is_inactive_with_unrelated_publisher_override(self):
        http = GitHubHttp()
        with patch.object(
            ReviewPublisher,
            "publish",
            side_effect=AssertionError("unrelated publisher called"),
        ) as override:
            self.assertIsNone(
                capacity_review_walk(http, "/repos/owner/repo/issues/comments")
            )
            override.assert_not_called()

    def test_original_wrapper_chain_is_inactive_with_unrelated_publisher_override(self):
        budget = EvidenceReadBudget()
        http = _BudgetedReviewPagination(
            PublicationBudgetHttp(GitHubHttp(), budget), budget
        )
        with patch.object(
            ReviewPublisher,
            "publish",
            side_effect=AssertionError("unrelated publisher called"),
        ) as override:
            self.assertIsNone(
                capacity_review_walk(http, "/repos/owner/repo/pulls/7/reviews")
            )
            override.assert_not_called()

    def installed_transport(self):
        from urllib.request import OpenerDirector

        from fake_github_http import json_response
        from review_sensei_github.quarantine_capacity import install_capacity
        from test_quarantine_activation import IDENTITY
        from test_quarantine_capacity import no_reserve

        class Ledger:
            def _cached_comments(self):
                return []

        ledger = Ledger()
        ledger.http = GitHubHttp()
        ledger._quarantine_native_comment_bounds = None
        ledger._quarantine_native_tail_parts = 2
        ledger._quarantine_origin_reader = None
        ledger._observed_consumed_grants = ()
        ledger._inventory_complete = True
        ledger.app_slug = "reviewsensei"
        surfaces = no_reserve()
        surfaces["evidence_parts"] = dict(surfaces["issue_comments"])
        receipt = {
            "schema_version": "quarantine-body-limit-isolation-v1",
            "qualification": {"exposure": {"surfaces": surfaces}},
        }
        from review_sensei_github import capacity_transport_membership as membership

        budget = EvidenceReadBudget()
        ledger.pre_wrapper = _BudgetedReviewPagination(
            PublicationBudgetHttp(ledger.http, budget), budget
        )
        self.assertTrue(membership.never_installed(ledger.pre_wrapper))
        calls = []

        def external(opener, request, data=None, timeout=None):
            calls.append(request.full_url)
            if not request.full_url.startswith(
                "https://api.github.com/repos/owner/repo/pulls/7/"
            ):
                raise AssertionError("unexpected external URL")
            return json_response([])

        with patch.object(OpenerDirector, "open", external):
            guard = install_capacity(
                ledger,
                IDENTITY,
                receipt,
                token="synthetic",
                future_parts=2,
                future_bytes=65536,
            )
        self.assertEqual(len(calls), 2)
        return ledger, guard

    def test_genuine_installation_cannot_be_hidden_by_removed_or_replaced_fields(self):
        from review_sensei_github import capacity_transport_membership as membership

        from review_sensei.errors import ReviewInputError

        ledger, guard = self.installed_transport()
        http = ledger.http
        self.assertFalse(membership.never_installed(http))
        for replacement in (None, object()):
            with self.subTest(replacement=type(replacement).__name__):
                http._quarantine_capacity_guard = replacement
                try:
                    self.assertFalse(membership.never_installed(http))
                    with self.assertRaises(ReviewInputError):
                        capacity_review_walk(http, "/repos/owner/repo/pulls/7/reviews")
                finally:
                    http._quarantine_capacity_guard = guard
        reader = guard.comment_reader
        guard.comment_reader = None
        try:
            with self.assertRaises(ReviewInputError):
                capacity_review_walk(http, "/repos/owner/repo/pulls/7/reviews")
        finally:
            guard.comment_reader = reader

    def test_later_base_installation_revokes_wrapper_negative_result(self):
        from review_sensei_github import capacity_transport_membership as membership

        ledger, guard = self.installed_transport()
        wrapper = ledger.pre_wrapper
        self.assertFalse(membership.never_installed(wrapper))
        del ledger.http._quarantine_capacity_guard
        try:
            self.assertFalse(membership.never_installed(wrapper))
        finally:
            ledger.http._quarantine_capacity_guard = guard

    def test_wrapper_copy_delegate_change_and_external_registration_refuse(self):
        import copy

        from review_sensei_github import capacity_transport_membership as membership

        from review_sensei.errors import ReviewInputError

        wrapper = _BudgetedReviewPagination(GitHubHttp(), EvidenceReadBudget())
        self.assertTrue(membership.never_installed(wrapper))
        copied = copy.copy(wrapper)
        self.assertFalse(membership.never_installed(copied))
        with patch.object(
            ReviewPublisher, "publish", side_effect=AssertionError("callback")
        ):
            with self.assertRaises(ReviewInputError):
                capacity_review_walk(copied, "/repos/owner/repo/pulls/7/reviews")
        original = wrapper.transport
        wrapper.transport = GitHubHttp()
        try:
            with self.assertRaises(ReviewInputError):
                membership.never_installed(wrapper)
        finally:
            wrapper.transport = original
        with self.assertRaises(ReviewInputError):
            membership.register_wrapper(wrapper)
        with self.assertRaises(ReviewInputError):
            membership.register_install(original, object())

    def test_registry_and_weakref_poison_execute_no_callbacks(self):
        import inspect
        import weakref

        from review_sensei_github import capacity_transport_membership as membership

        from review_sensei.errors import ReviewInputError

        wrapper = _BudgetedReviewPagination(GitHubHttp(), EvidenceReadBudget())
        find = inspect.getclosurevars(membership.never_installed).nonlocals["find"]
        checked = inspect.getclosurevars(find).nonlocals["checked"]
        tables = inspect.getclosurevars(checked).nonlocals["tables"]
        table = tables[1]
        key = id(wrapper)
        original = table[key]
        calls = []

        class Bomb:
            def __call__(self, *args):
                calls.append("call")
                raise AssertionError("callback")

            def __hash__(self):
                calls.append("hash")
                raise AssertionError("callback")

            def __eq__(self, other):
                calls.append("eq")
                raise AssertionError("callback")

        for row in (Bomb(), (key, Bomb(), original[2], original[3])):
            table[key] = row
            try:
                with self.assertRaises(ReviewInputError):
                    membership.never_installed(wrapper)
            finally:
                table[key] = original
        saved = dict(table)
        table.clear()
        try:
            with self.assertRaises(ReviewInputError):
                membership.never_installed(wrapper)
        finally:
            table.update(saved)
        with patch.object(weakref, "ref", Bomb()):
            with self.assertRaises(ReviewInputError):
                membership.never_installed(wrapper)
        self.assertEqual(calls, [])

    def test_replaced_membership_export_is_rejected_before_callback(self):
        from review_sensei_github import capacity_transport_membership as membership

        from review_sensei.errors import ReviewInputError

        with patch.object(
            membership, "never_installed", side_effect=AssertionError("callback")
        ) as fake:
            with self.assertRaises(ReviewInputError):
                capacity_review_walk(GitHubHttp(), "/repos/owner/repo/pulls/7/reviews")
            fake.assert_not_called()

    def test_registry_helper_replacement_refuses_before_callback(self):
        from review_sensei_github import capacity_transport_membership as membership

        from review_sensei.errors import ReviewInputError

        calls = []

        def fake(*args):
            calls.append("fake")
            return None

        function = membership.never_installed
        cell = dict(zip(function.__code__.co_freevars, function.__closure__))["find"]
        original = cell.cell_contents
        cell.cell_contents = fake
        try:
            with self.assertRaises(ReviewInputError):
                membership.never_installed(GitHubHttp())
        finally:
            cell.cell_contents = original
        self.assertEqual(calls, [])
