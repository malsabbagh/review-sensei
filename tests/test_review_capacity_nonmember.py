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
        callbacks = []

        class FakeGuard:
            def __getattr__(self, name):
                callbacks.append(name)
                raise AssertionError("callback")

        for replacement in (None, object(), FakeGuard()):
            with self.subTest(replacement=type(replacement).__name__):
                http._quarantine_capacity_guard = replacement
                try:
                    self.assertFalse(membership.never_installed(http))
                    with self.assertRaises(ReviewInputError):
                        capacity_review_walk(http, "/repos/owner/repo/pulls/7/reviews")
                finally:
                    http._quarantine_capacity_guard = guard
        self.assertEqual(callbacks, [])
        reader = guard.comment_reader
        guard.comment_reader = None
        try:
            with self.assertRaises(ReviewInputError):
                capacity_review_walk(http, "/repos/owner/repo/pulls/7/reviews")
            with self.assertRaises(ReviewInputError):
                capacity_review_walk(http, "/repos/owner/repo/issues/comments")
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
        quick = inspect.getclosurevars(find).nonlocals["quick"]
        state = inspect.getclosurevars(quick).nonlocals["state"]
        retired = inspect.getclosurevars(quick).nonlocals["retired"]
        table = state[0][1]
        key = (1, id(weakref.ref(wrapper)))
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

        for row in (Bomb(), (1, id(wrapper), Bomb(), original[3])):
            table[key] = row
            saved_retired = set(retired)
            retired.update((0, -n) for n in range(1, 100))
            try:
                with self.assertRaises(ReviewInputError):
                    membership.never_installed(wrapper)
            finally:
                table[key] = original
                retired.clear()
                retired.update(saved_retired)
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

    def test_compaction_preserves_live_members_and_rejects_rewind_clear_and_remint(
        self,
    ):
        import gc
        import inspect

        from review_sensei_github import capacity_transport_membership as membership

        from review_sensei.errors import ReviewInputError

        ledger, guard = self.installed_transport()
        original = ledger.http._quarantine_capacity_guard
        del ledger.http._quarantine_capacity_guard
        find = inspect.getclosurevars(membership.never_installed).nonlocals["find"]
        quick = inspect.getclosurevars(find).nonlocals["quick"]
        state = inspect.getclosurevars(quick).nonlocals["state"]
        retired = inspect.getclosurevars(quick).nonlocals["retired"]
        old = state[0]
        try:
            for _ in range(300):
                temporary = _BudgetedReviewPagination(
                    GitHubHttp(), EvidenceReadBudget()
                )
            del temporary
            gc.collect()
            retired.update((0, -n) for n in range(1, max(100, len(state[0][1]))))
            self.assertFalse(membership.never_installed(ledger.pre_wrapper))
            latest = state[0]
            self.assertIsNot(latest, old)
            live = sum(row[2]() is not None for row in latest[1].values())
            self.assertLessEqual(len(latest[1]), 2 * live + 64)
            self.assertTrue(any(row[2]() is ledger.http for row in latest[1].values()))
            state[0] = old
            try:
                with self.assertRaises(ReviewInputError):
                    membership.never_installed(ledger.http)
                old[0].cache_clear()
                old[1].clear()
                with self.assertRaises(ReviewInputError):
                    membership.never_installed(ledger.http)
                with self.assertRaises(Exception):
                    old[0]((-1, 0))
                with self.assertRaises(Exception):
                    old[0].__wrapped__((-1, 0))
            finally:
                state[0] = latest
            self.assertFalse(membership.never_installed(ledger.http))
        finally:
            ledger.http._quarantine_capacity_guard = original

    def test_exception_after_revocation_leaves_only_fail_closed_old_epoch(self):
        import inspect
        import sys

        from review_sensei_github import capacity_transport_membership as membership

        from review_sensei.errors import ReviewInputError

        find = inspect.getclosurevars(membership.never_installed).nonlocals["find"]
        quick = inspect.getclosurevars(find).nonlocals["quick"]
        compact = inspect.getclosurevars(find).nonlocals["compact"]
        state = inspect.getclosurevars(quick).nonlocals["state"]
        retired = inspect.getclosurevars(quick).nonlocals["retired"]
        original = state[0]
        saved = []
        lines, first = inspect.getsourcelines(compact)
        line = first + next(
            i for i, text in enumerate(lines) if "state[0] = replacement" in text
        )

        def trace(frame, event, arg):
            if (
                event == "line"
                and frame.f_code is compact.__code__
                and frame.f_lineno == line
            ):
                saved.append(frame.f_locals["replacement"])
                raise RuntimeError("injected after revocation")
            return trace

        retired.update((0, -n) for n in range(1, max(100, len(original[1]))))
        previous = sys.gettrace()
        try:
            sys.settrace(trace)
            with self.assertRaisesRegex(RuntimeError, "after revocation"):
                membership.never_installed(GitHubHttp())
        finally:
            sys.settrace(previous)
        self.assertEqual(len(saved), 1)
        self.assertIs(state[0], original)
        with self.assertRaises(ReviewInputError):
            membership.never_installed(GitHubHttp())
        # Test-only restoration to the fully constructed authentic replacement;
        # production does not expose reset/recovery of a revoked registry.
        state[0] = saved[0]
        retired.clear()
        self.assertTrue(membership.never_installed(GitHubHttp()))

    def test_fabricated_epoch_cannot_replace_original_root_without_callbacks(self):
        import inspect

        from review_sensei_github import capacity_transport_membership as membership

        from review_sensei.errors import ReviewInputError

        find = inspect.getclosurevars(membership.never_installed).nonlocals["find"]
        quick = inspect.getclosurevars(find).nonlocals["quick"]
        state = inspect.getclosurevars(quick).nonlocals["state"]
        original = state[0]
        calls = []

        def forged(key):
            calls.append(key)
            return original[2]

        fake = type(original[0])(forged, None, False, membership._cache_statistics)
        fake.__wrapped__ = original[3]
        state[0] = (fake, *original[1:])
        try:
            with self.assertRaises(ReviewInputError):
                membership.never_installed(GitHubHttp())
        finally:
            state[0] = original
        self.assertEqual(calls, [])

    def test_root_clear_cannot_remint_in_fresh_process(self):
        import subprocess
        import sys

        code = """
import inspect
from review_sensei.errors import ReviewInputError
from review_sensei_github import capacity_transport_membership as m
from review_sensei_github.http import GitHubHttp
find=inspect.getclosurevars(m.never_installed).nonlocals['find']
quick=inspect.getclosurevars(find).nonlocals['quick']
root=inspect.getclosurevars(quick).nonlocals['root']
root.cache_clear()
for operation in (lambda:m.never_installed(GitHubHttp()), root, root.__wrapped__):
    try: operation()
    except ReviewInputError: pass
    else: raise AssertionError('root cache reminted')
"""
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_many_wrapper_lifetimes_have_bounded_live_storage(self):
        import gc
        import inspect

        from review_sensei_github import capacity_transport_membership as membership

        find = inspect.getclosurevars(membership.never_installed).nonlocals["find"]
        quick = inspect.getclosurevars(find).nonlocals["quick"]
        state = inspect.getclosurevars(quick).nonlocals["state"]
        keep = [
            _BudgetedReviewPagination(GitHubHttp(), EvidenceReadBudget())
            for _ in range(80)
        ]
        epochs = set()
        for _ in range(2000):
            temporary = _BudgetedReviewPagination(GitHubHttp(), EvidenceReadBudget())
            epochs.add(id(state[0]))
        del temporary
        gc.collect()
        self.assertTrue(membership.never_installed(keep[0]))
        rows = state[0][1]
        live = sum(row[2]() is not None for row in rows.values())
        self.assertLessEqual(len(rows), 2 * live + 64)
        # ID reuse does not force an O(live) rebuild on each construction.
        self.assertLess(len(epochs), 100)
        self.assertTrue(all(membership.never_installed(value) for value in keep))
