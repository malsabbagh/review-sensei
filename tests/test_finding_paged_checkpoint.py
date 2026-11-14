"""Bounded full-page lifecycle checkpoints share the original caller funding."""

import copy
import tempfile
import unittest
from unittest.mock import PropertyMock, patch

from review_sensei_github import api_run_requests as adapter
from review_sensei_github.api_run_plan import NATIVE_OPERATION_REQUESTS
from review_sensei_github.errors import GitHubPublicationError

from review_sensei.errors import ReviewInputError
from review_sensei.finding_lifecycle_store import (
    preflight_finding_lifecycle,
    stage_finding_lifecycle,
)
from tests import test_finding_lifecycle_parts as parts_fixtures


class PagedFindingCheckpointTests(unittest.TestCase):
    def fixture(self, directory, *, count=15, body_bytes=59980):
        fixture, record, kwargs = parts_fixtures.CurrentRunFindingPartsTests().fixture(
            directory
        )
        for n in range(count):
            fixture.service.extra_comments[3000000 + n] = {
                "id": 3000000 + n,
                "body": "historical text " + "x" * body_bytes,
                "user": {"id": 42, "login": "maintainer", "type": "User"},
                "issue_url": "https://api.github.test/repos/owner/repo/issues/7",
            }
        # Invalidate only the read cache; authenticate the larger remote history
        # through the unchanged charged loader, never synthesize a proof.
        fixture.ledger._inventory_complete = False
        fixture.ledger._list_refresh = False
        record = fixture.ledger.load(fixture.identity).record
        return fixture, record, kwargs

    def test_source_plan_charges_full_pages_and_keeps_generic_ten(self):
        with tempfile.TemporaryDirectory() as directory:
            # The sealed aligned recipe packs fifteen rows into three pages.
            # Keep this specialization regression above the generic ten-call
            # allowance by requiring four genuine bounded pages instead.
            f, record, kwargs = self.fixture(directory, count=24)
            plan = preflight_finding_lifecycle(f.ledger, record, **kwargs)
            quoted = plan["conditional_calls"]
            before = len(f.caller.observations)
            updated = stage_finding_lifecycle(f.ledger, record, **kwargs)
            attempts = f.caller.observations[before:]
            self.assertGreater(len(attempts), NATIVE_OPERATION_REQUESTS)
            self.assertLessEqual(len(attempts), quoted)
            self.assertEqual(NATIVE_OPERATION_REQUESTS, 10)
            self.assertIsNotNone(updated.finding_lifecycle)
            self.assertNotIn(f.ledger, adapter._PLAIN_FINDING_ACTIVE)
            f.assert_physical_accounting(self)
            self.assertEqual(f.caller.accounting.configuration.limit, 512)

    def test_paged_inline_stage_prices_both_full_fences(self):
        with tempfile.TemporaryDirectory() as directory:
            f, record, kwargs = self.fixture(directory)
            kwargs["document"] = {"small": "exact inline checkpoint"}
            plan = preflight_finding_lifecycle(f.ledger, record, **kwargs)
            self.assertEqual(plan["checkpoints"][0]["parts"], 0)
            self.assertGreater(plan["conditional_calls"], 5)
            before = len(f.caller.observations)
            updated = stage_finding_lifecycle(f.ledger, record, **kwargs)
            self.assertEqual(
                updated.finding_lifecycle["manifest"]["encoding"], "inline-json-v1"
            )
            self.assertLessEqual(
                len(f.caller.observations) - before, plan["conditional_calls"]
            )
            f.assert_physical_accounting(self)

    def test_copied_stale_or_mutated_page_proofs_never_grant_an_operation(self):
        for fault in ("copy", "root", "nested", "mutated"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                f, record, kwargs = self.fixture(directory)
                f.ledger.preflight_finding_lifecycle_retention(
                    f.identity, added_parts=1, added_bytes=16000, checkpoint_parts=(1,)
                )
                plan = adapter._PLAIN_FINDING_PLANS[f.ledger]
                if fault == "copy":
                    plan = copy.copy(plan)
                elif fault == "root":
                    f.caller._root = "f" * 64
                elif fault == "nested":
                    plan.record.api_run_accounting["configuration"]["limit"] += 1
                else:
                    object.__setattr__(plan, "page_size", 100)
                adapter._PLAIN_FINDING_ACTIVE[f.ledger] = (plan, False)
                before = list(f.caller.observations)
                with self.assertRaises(ReviewInputError):
                    f.ledger._take_plain_finding_operation(f.caller, f.identity)
                self.assertEqual(f.caller.observations, before)
                adapter._PLAIN_FINDING_ACTIVE.pop(f.ledger, None)

    def test_specialized_operation_plan_is_one_use(self):
        with tempfile.TemporaryDirectory() as directory:
            f, record, kwargs = self.fixture(directory)
            f.ledger.preflight_finding_lifecycle_retention(
                f.identity, added_parts=1, added_bytes=16000, checkpoint_parts=(1,)
            )
            plan = adapter._PLAIN_FINDING_PLANS[f.ledger]
            adapter._PLAIN_FINDING_ACTIVE[f.ledger] = (plan, False)
            before = list(f.caller.observations)
            self.assertIsNotNone(
                f.ledger._take_plain_finding_operation(f.caller, f.identity)
            )
            with self.assertRaises(ReviewInputError):
                f.ledger._take_plain_finding_operation(f.caller, f.identity)
            self.assertEqual(f.caller.observations, before)
            adapter._PLAIN_FINDING_ACTIVE.pop(f.ledger, None)

    def test_inline_counts_require_original_unconsumed_source_proof(self):
        from review_sensei.finding_lifecycle_store import (
            _consume_checkpoint_pricing,
            _remote_inline_plan,
        )

        for fault in ("copy", "stale", "foreign", "reuse"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                f, record, kwargs = self.fixture(directory)
                plan = f.ledger.preflight_finding_lifecycle_inline(record)
                before = list(f.caller.observations)
                if fault == "copy":
                    plan["checkpoint_pricing"] = copy.copy(plan["checkpoint_pricing"])
                elif fault == "stale":
                    f.caller._root = "f" * 64
                elif fault == "foreign":
                    record = copy.deepcopy(record)
                if fault == "reuse":
                    _consume_checkpoint_pricing(
                        plan["checkpoint_pricing"], f.ledger, record, (0,)
                    )
                with patch.object(
                    f.ledger, "preflight_finding_lifecycle_inline", return_value=plan
                ):
                    with self.assertRaises(ReviewInputError):
                        _remote_inline_plan(f.ledger, record)
                self.assertEqual(f.caller.observations, before)

    def test_one_short_plan_refuses_before_any_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            f, record, kwargs = self.fixture(directory)
            quoted = preflight_finding_lifecycle(f.ledger, record, **kwargs)[
                "conditional_calls"
            ]
            before = list(f.caller.observations)
            # Narrow only available original-tranche funding; never issue a
            # new budget or alter the frozen configured total.
            with patch.object(
                type(f.caller),
                "usable",
                new_callable=PropertyMock,
                return_value=quoted - 1,
            ):
                with self.assertRaises(ReviewInputError):
                    preflight_finding_lifecycle(f.ledger, record, **kwargs)
            self.assertEqual(f.caller.observations, before)
            self.assertEqual(f.caller.accounting.configuration.limit, 512)

    def test_hundred_existing_comments_fit_bounded_pages_not_one_response(self):
        with tempfile.TemporaryDirectory() as directory:
            f, record, kwargs = self.fixture(directory, count=99, body_bytes=1)
            self.assertEqual(len(f.ledger._cached_comments()), 100)
            plan = preflight_finding_lifecycle(f.ledger, record, **kwargs)
            before = len(f.caller.observations)
            updated = stage_finding_lifecycle(f.ledger, record, **kwargs)
            self.assertIsNotNone(updated.finding_lifecycle)
            self.assertLessEqual(
                len(f.caller.observations) - before, plan["conditional_calls"]
            )
            f.assert_physical_accounting(self)

    def test_complete_fresh_fences_reject_growth_deleted_dependency_and_duplicate_root(
        self,
    ):
        from urllib.parse import urlsplit

        for fault in (
            "growth",
            "response-growth",
            "dependency-deletion",
            "duplicate-root",
            "duplicate-appended-id",
            "reordered",
        ):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                f, record, kwargs = self.fixture(directory)
                record = stage_finding_lifecycle(f.ledger, record, **kwargs)
                dependency = int(
                    record.finding_lifecycle["manifest"]["parts"][0]["storage_id"]
                )
                root_before = f.service.comment["body"]
                before_ids = set(f.service.extra_comments)
                original = f.caller._opener
                tripped = []

                def opened(request, timeout):
                    if (
                        not tripped
                        and f.ledger in adapter._PLAIN_FINDING_ACTIVE
                        and request.method == "GET"
                        and urlsplit(request.full_url).path.endswith(
                            "/issues/7/comments"
                        )
                    ):
                        tripped.append(True)
                        if fault == "dependency-deletion":
                            del f.service.extra_comments[dependency]
                        elif fault == "growth":
                            f.service.extra_comments[9000000] = {
                                "id": 9000000,
                                "body": "unexpected growth",
                                "user": {"id": 42, "type": "User"},
                            }
                        elif fault == "duplicate-appended-id":
                            newly_staged = set(f.service.extra_comments) - before_ids
                            self.assertEqual(len(newly_staged), 1)
                            f.service.extra_comments[next(iter(newly_staged))]["id"] = (
                                3000000
                            )
                        elif fault == "response-growth":
                            f.service.extra_comments[3000000]["body"] = "x" * 520000
                        elif fault == "duplicate-root":
                            f.service.extra_comments[3000000] = {
                                **f.service.comment,
                                "id": 3000000,
                            }
                        else:
                            f.service.extra_comments = dict(
                                reversed(list(f.service.extra_comments.items()))
                            )
                    return original(request, timeout)

                f.caller._opener = opened
                kwargs = {**kwargs, "document": {**kwargs["document"], "next": True}}
                expected_error = (
                    GitHubPublicationError
                    if fault == "response-growth"
                    else ReviewInputError
                )
                with self.assertRaises(expected_error):
                    stage_finding_lifecycle(f.ledger, record, **kwargs)
                self.assertTrue(tripped)
                self.assertEqual(f.service.comment["body"], root_before)
                # Already staged immutable parts remain orphans, never removed
                # or attached by the refused checkpoint.
                self.assertTrue(set(f.service.extra_comments) - before_ids)
                self.assertNotIn(f.ledger, adapter._PLAIN_FINDING_ACTIVE)
                f.assert_physical_accounting(self)

    def test_dependency_deleted_after_patch_remains_unconfirmed_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            f, record, kwargs = self.fixture(directory)
            record = stage_finding_lifecycle(f.ledger, record, **kwargs)
            dependency = int(
                record.finding_lifecycle["manifest"]["parts"][0]["storage_id"]
            )
            original = f.caller._opener
            patches = []

            def opened(request, timeout):
                response = original(request, timeout)
                if (
                    f.ledger in adapter._PLAIN_FINDING_ACTIVE
                    and request.method == "PATCH"
                ):
                    patches.append(request.full_url)
                    f.service.extra_comments.pop(dependency, None)
                return response

            f.caller._opener = opened
            kwargs = {**kwargs, "document": {**kwargs["document"], "next": True}}
            with self.assertRaises(ReviewInputError):
                stage_finding_lifecycle(f.ledger, record, **kwargs)
            self.assertEqual(len(patches), 1)
            self.assertNotIn(dependency, f.service.extra_comments)
            self.assertNotIn(f.ledger, adapter._PLAIN_FINDING_ACTIVE)
            f.assert_physical_accounting(self)

    def test_more_than_ten_complete_pages_is_not_a_new_census(self):
        with tempfile.TemporaryDirectory() as directory:
            f, record, kwargs = self.fixture(directory)
            items = copy.deepcopy(f.ledger._cached_comments())
            for n in range(40):
                items.append(
                    {
                        "id": 4000000 + n,
                        "body": "x" * 60000,
                        "user": {"id": 42, "type": "User"},
                    }
                )
            f.ledger._replace_inventory(items)
            before = list(f.caller.observations)
            with self.assertRaisesRegex(ReviewInputError, "page capacity"):
                f.ledger.preflight_finding_lifecycle_retention(
                    f.identity, added_parts=1, added_bytes=16000, checkpoint_parts=(1,)
                )
            self.assertEqual(f.caller.observations, before)
