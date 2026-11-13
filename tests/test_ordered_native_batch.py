"""A preflighted cleanup prefix cannot skip a failed target then dispatch later."""

import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from review_sensei_github.http import GitHubHttp
from review_sensei_github.ordered_native_batch import (
    bind_ordered_batch,
    issue_ordered_batch,
)
from review_sensei_github.thread_resolution import FindingThreadResolver

from review_sensei.errors import ReviewInputError
from tests import test_thread_cleanup_capability as cleanup


class OrderedNativeBatchTests(unittest.TestCase):
    def journal(self):
        return SimpleNamespace(
            _record=SimpleNamespace(to_dict=lambda: {"root": "original"}),
            operation_id="original",
            head_sha="b" * 40,
        )

    def requests(self):
        return tuple(
            (
                "POST",
                "/graphql",
                {
                    "operationName": "ResolveReviewThread",
                    "query": "resolveReviewThread",
                    "variables": {"input": {"threadId": name}},
                },
            )
            for name in ("one", "two", "three")
        )

    def test_issued_approved_exact_order_only_and_closed_remaining_target(self):
        requests = self.requests()
        witness = issue_ordered_batch(self.journal(), requests)
        with self.assertRaises(ReviewInputError):
            bind_ordered_batch(witness, requests)
        witness.approve()
        self.assertIsNone(bind_ordered_batch(copy.copy(witness), requests))
        bind_ordered_batch(witness, requests)
        with self.assertRaises(ReviewInputError):
            witness.before_request(*requests[1])
        self.assertTrue(witness.before_request(*requests[0]))
        witness.confirm_current()
        witness.stop()
        with self.assertRaises(ReviewInputError):
            witness.before_request(*requests[1])
        self.assertFalse(witness.before_request("PATCH", "/check-runs/1", {}))
        self.assertEqual(witness.confirmed_prefix, 1)

    def test_mutated_progress_or_scope_never_refines_the_prefix(self):
        for name, changed in (
            ("confirmed_prefix", 1),
            ("_approved", True),
            ("_bound", True),
            ("_closed", True),
            ("_inflight", True),
            ("requests", (b"forged",)),
            ("operation_id", "forged"),
        ):
            with self.subTest(name=name):
                witness = issue_ordered_batch(self.journal(), self.requests())
                setattr(witness, name, changed)
                with self.assertRaises(ReviewInputError):
                    witness.approve()
                with self.assertRaises(ReviewInputError):
                    witness.before_request(*self.requests()[0])

    def test_resolver_stops_after_first_unconfirmed_and_preserves_resolved_suffix(self):
        for first_success in (False, True):
            issued = []
            attempted = []
            original = FindingThreadResolver.resolve

            def preflight(http, requests, *, native_calls):
                witness = issue_ordered_batch(self.journal(), requests).approve()
                issued.append((witness, requests))
                return witness

            def resolve(resolver, **arguments):
                return original(resolver, **arguments, batch_fence_calls=0)

            def mutation(resolver, *, token, thread_id, observer):
                witness, requests = issued[0]
                request = next(
                    r
                    for r in requests
                    if r[2]["variables"]["input"]["threadId"] == thread_id
                )
                witness.before_request(*request)
                attempted.append(thread_id)
                return first_success and len(attempted) == 1

            with (
                patch.object(
                    GitHubHttp, "preflight_native_batch", preflight, create=True
                ),
                patch.object(FindingThreadResolver, "resolve", resolve),
                patch.object(FindingThreadResolver, "_resolve_confirmed", mutation),
            ):
                state, result, calls, acquired = (
                    cleanup.CleanupCapabilityTests().resolve(
                        resolved=() if first_success else (102,),
                        fence=lambda state: True,
                    )
                )
            self.assertEqual(len(attempted), 2 if first_success else 1)
            self.assertEqual(len(result.unconfirmed), 2)
            self.assertEqual(len(result.resolved), 1)
            self.assertEqual(issued[0][0].confirmed_prefix, 1 if first_success else 0)
            self.assertTrue(issued[0][0]._closed)
