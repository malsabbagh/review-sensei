"""Capacity certificates are checked against fresh real bounded readers."""

import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fake_github_http import json_response
from review_sensei_github.http import GitHubHttp, _completed_comment_nodes
from review_sensei_github.operation_comments import GitHubIssueCommentOperationPort
from review_sensei_github.quarantine_capacity import (
    NativeCapacityGuard,
    full_review_comment_tail,
)
from review_sensei_github.quarantine_census import fetch_comments
from review_sensei_github.quarantine_published_tail import BROKER_QUALIFICATIONS
from test_quarantine_activation import IDENTITY, registered_resource
from test_quarantine_capacity import no_reserve
from test_quarantine_census import CensusBackend
from test_quarantine_published_tail_journey import PublishedTailJourneyFixture

from review_sensei.errors import ReviewInputError


class ColdCapacityReaderTests(unittest.TestCase):
    def test_unreadable_unknown_full_review_envelope_refuses_before_creation_effects(
        self,
    ):
        # The supported envelope is exercised by the positive heavy14 journey.
        # An extra carrier exceeding one complete response must still refuse
        # before creating a root, staging a part or invoking the provider.
        bounds = full_review_comment_tail()
        self.assertEqual(len(bounds), 31)
        self.assertGreater(sum(bounds), 1_500_000)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            fixture = PublishedTailJourneyFixture(directory)
            old, registration = fixture.original_origins()
            before = copy.deepcopy(fixture.extra_comments)
            predecessor, calls = fixture.comment["body"], fixture.provider_calls
            start = len(fixture.trace)
            with (
                registered_resource([registration]),
                patch(
                    "review_sensei_github.quarantine_capacity.full_review_comment_tail",
                    return_value=(*bounds, 512 * 1024),
                ),
            ):
                self.assertEqual(fixture.invoke(), 1, fixture.error)
            self.assertIn("capacity", fixture.error)
            self.assertEqual(fixture.provider_calls, calls)
            self.assertEqual(fixture.comment["body"], predecessor)
            self.assertEqual(fixture.extra_comments, before)
            self.assertIsNone(old.api_run_accounting["terminal_transaction"])
            self.assertFalse(
                any(
                    method in {"POST", "PATCH", "PUT"} and path.startswith("/repos/")
                    for _, method, path in fixture.trace[start:]
                )
            )

    def test_certified_cursor_restarts_cold_for_escaped_unicode_and_adversarial_order(
        self,
    ):
        large = "x" * 58_001
        for bodies in (
            [large] * 35,
            ["\\" * 58_001] * 8,
            ["small"] * 80 + [large] * 12,
            [large, "small"] * 30,
            ["\U0001f4a5" * 43_000] * 5,
            ["tiny"] * 100,
        ):
            with self.subTest(count=len(bodies), first_size=len(bodies[0])):
                backend = CensusBackend(0)
                backend.rows = [backend.node(i, body) for i, body in enumerate(bodies)]
                for _ in range(2):
                    ledger = backend.ledger(cached_body="stale")
                    ledger._quarantine_capacity_cursor = True
                    ledger._quarantine_census_sizes = {None: 1}
                    backend.calls.clear()
                    self.assertEqual(
                        [row["body"] for row in fetch_comments(ledger, IDENTITY)],
                        bodies,
                    )
                    self.assertLessEqual(len(backend.calls), 10)
                    seen = set()
                    for call in backend.calls:
                        if call["after"] not in seen:
                            self.assertEqual(call["first"], 100)
                            seen.add(call["after"])

    def test_admitted_ascii_escaped_clustered_and_interleaved_rows_are_cold_readable(
        self,
    ):
        large = "x" * 58001
        escaped = "x" + "\\" * 58000
        cases = (
            [large] * 35,
            [escaped] * 8,
            ["small"] * 15 + [large] * 8 + ["tail"] * 15,
            [large, "small"] * 20,
            ["\U0001f4a5" * 43000] * 5,
            ['quoted \\" braces {} []'] * 100,
        )
        for bodies in cases:
            with self.subTest(first_size=len(bodies[0]), count=len(bodies)):
                rows, reads = [], []

                def opener(request, timeout):
                    if request.get_method() == "POST":
                        row = {
                            "id": len(rows) + 1,
                            **json.loads(request.data),
                            "user": {"id": 999, "type": "Bot"},
                            "issue_url": "https://api.github.test/repos/owner/repo/issues/7",
                        }
                        rows.append(row)
                        return json_response(row, 201)
                    query = parse_qs(urlsplit(request.full_url).query)
                    size, page = int(query["per_page"][0]), int(query["page"][0])
                    reads.append((size, page))
                    return json_response(rows[(page - 1) * size : page * size])

                http = GitHubHttp(api_url="https://api.github.test", opener=opener)
                guard = NativeCapacityGuard(identity=IDENTITY, reserve=no_reserve())
                http._quarantine_capacity_guard = guard
                for body in bodies:
                    before = len(rows)
                    try:
                        http.request(
                            "POST",
                            "/repos/owner/repo/issues/7/comments",
                            token="synthetic",
                            body={"body": body},
                        )
                    except ReviewInputError:
                        self.assertEqual(len(rows), before)
                        break
                self.assertTrue(rows)
                # No live guard, observation wrapper or warmed size profile is
                # inherited by either independent consumer.
                fresh = GitHubHttp(api_url="https://api.github.test", opener=opener)
                restored = fresh.paginate(
                    path="/repos/owner/repo/issues/7/comments", token="synthetic"
                )
                self.assertEqual(restored, rows)
                self.assertLessEqual(len(reads), 10)
                reads.clear()
                port = GitHubIssueCommentOperationPort(
                    fresh,
                    token="synthetic",
                    repository="owner/repo",
                    pull_request=7,
                    app_user_id=999,
                    strict_association=True,
                )
                self.assertEqual(len(port.list_comments()), len(rows))
                self.assertLessEqual(len(reads), 10)

    def test_prepared_whole_tail_refusal_changes_no_usage_or_promises(self):
        guard = NativeCapacityGuard(
            identity=IDENTITY,
            reserve=no_reserve(),
            future_parts=1,
            future_bytes=32768,
            comment_bounds=[32768],
            cursor=True,
        )
        before = copy.deepcopy(
            (guard.stats, guard.comment_bounds, guard.future_parts, guard.future_bytes)
        )
        with self.assertRaises(ReviewInputError):
            guard.admit_comment_batch(["\\" * 200000] * 20)
        self.assertEqual(
            (guard.stats, guard.comment_bounds, guard.future_parts, guard.future_bytes),
            before,
        )

    def test_unknown_create_consumes_the_prepared_tail_once(self):
        guard = NativeCapacityGuard(
            identity=IDENTITY,
            reserve=no_reserve(),
            future_parts=1,
            future_bytes=32768,
            comment_bounds=[32768],
            cursor=True,
        )
        path = "/repos/owner/repo/issues/7/comments"
        guard.before_request("POST", path, {"body": "prepared carrier"})
        charged = copy.deepcopy(guard.stats)
        self.assertEqual(guard.future_parts, 0)
        self.assertEqual(guard.comment_bounds, [])
        # No response exists. Neither an absent read nor the attempted repeat
        # can restore the consumed promise or its charged positive growth.
        with self.assertRaises(ReviewInputError):
            guard.before_request("POST", path, {"body": "prepared carrier"})
        self.assertEqual(guard.stats, charged)
        self.assertEqual(guard.comment_bounds, [])

    def test_prepared_tail_cannot_acquire_unreserved_mandatory_parts(self):
        guard = NativeCapacityGuard(
            identity=IDENTITY,
            reserve=no_reserve(),
            future_parts=1,
            future_bytes=32768,
            comment_bounds=[32768],
            cursor=True,
            complete_tail=True,
        )
        before = copy.deepcopy((guard.stats, guard.comment_bounds))
        with self.assertRaisesRegex(ReviewInputError, "admitted complete transaction"):
            guard.admit_comment_batch(["one", "two"])
        self.assertEqual((guard.stats, guard.comment_bounds), before)

    def test_repeated_preparation_does_not_replenish_an_unknown_publication(self):
        guard = NativeCapacityGuard(
            identity=IDENTITY, reserve=no_reserve(), cursor=True, closure_wire=200
        )
        guard.admit_publication(review_wire=1000, inline_wires=[])
        body = {"body": "x" * 500}
        guard.before_request("POST", "/repos/owner/repo/pulls/7/reviews", body)
        remaining = copy.deepcopy(guard.review_bounds)
        usage = copy.deepcopy(guard.stats)
        guard.admit_publication(review_wire=1000, inline_wires=[])
        self.assertEqual(guard.review_bounds, remaining)
        with self.assertRaises(ReviewInputError):
            guard.before_request("POST", "/repos/owner/repo/pulls/7/reviews", body)
        self.assertEqual(guard.stats, usage)

    def test_full_tail_refuses_unplanned_publication_before_dispatch(self):
        guard = NativeCapacityGuard(
            identity=IDENTITY,
            reserve=no_reserve(),
            cursor=True,
            closure_wire=200,
            complete_tail=True,
        )
        before = copy.deepcopy((guard.stats, guard.review_bounds, guard.inline_bounds))
        with self.assertRaises(ReviewInputError):
            guard.admit_publication(review_wire=1000, inline_wires=[])
        self.assertEqual(
            (guard.stats, guard.review_bounds, guard.inline_bounds), before
        )

    def test_prefix_hint_counts_nodes_without_accepting_data_or_counting_children(self):
        nodes = [
            {"body": 'quoted nodes: [{}] \\"', "author": None, "nested": {"x": {}}},
            {"body": "\U0001f4a5", "author": {"id": 1}, "nested": {"x": {}}},
        ]
        payload = json.dumps({"data": {"nodes": nodes}}, separators=(",", ":")).encode()
        self.assertEqual(_completed_comment_nodes(payload), 2)
        second = payload.index(b"},{") + 2
        self.assertEqual(_completed_comment_nodes(payload[:second]), 1)
        escaped_key = payload.replace(b'"nodes":', b'"\\u006eodes"' + b" " * 200 + b":")
        self.assertEqual(_completed_comment_nodes(escaped_key), 2)


if __name__ == "__main__":
    unittest.main()
