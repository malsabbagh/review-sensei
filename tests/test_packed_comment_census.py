"""Cold discovery includes thirteen old slots and interrupted current staging."""

import copy
import json
import random
import unittest
from urllib.parse import urlsplit

from fake_github_http import json_response
from review_sensei_github.packed_comment_census import (
    METADATA_QUERY,
    CommentAllocationPlan,
    fetch_packed_comments,
    wire,
)
from review_sensei_github.quarantine_capacity import full_review_comment_tail
from review_sensei_github.quarantine_census import QUERY, is_readonly_census
from test_quarantine_census import IDENTITY, CensusBackend

from review_sensei.errors import ReviewInputError


class PackedBackend(CensusBackend):
    def __init__(self, bodies):
        super().__init__(0)
        self.rows = [self.node(number, body) for number, body in enumerate(bodies)]
        self.physical = []
        self.mutate_closing = None

    def opened(self, request, timeout):
        assert urlsplit(request.full_url).path == "/graphql"
        assert is_readonly_census(request, "https://api.github.test")
        assert len(request.data) <= 8192
        payload = json.loads(request.data)
        self.physical.append(payload)
        if payload["query"] == QUERY:
            return super().opened(request, timeout)
        assert payload["query"] == METADATA_QUERY
        if self.mutate_closing and len(self.physical) > 1:
            self.mutate_closing(self.rows)
        rows = [
            {key: copy.deepcopy(value) for key, value in row.items() if key != "body"}
            for row in self.rows
        ]
        return json_response(
            {
                "data": {
                    "repository": {
                        "databaseId": 123,
                        "nameWithOwner": "owner/repo",
                        "pullRequest": {
                            "number": 7,
                            "headRefOid": "a" * 40,
                            "baseRefOid": "b" * 40,
                            "f": {
                                "totalCount": len(rows),
                                "nodes": rows[:100],
                                "pageInfo": {
                                    "hasNextPage": len(rows) > 100,
                                    "endCursor": "opaque:100" if rows else None,
                                },
                            },
                            "l": {
                                "totalCount": len(rows),
                                "nodes": rows[-100:],
                                "pageInfo": {"hasPreviousPage": len(rows) > 100},
                            },
                        },
                    }
                }
            }
        )


class PackedCommentCensusTests(unittest.TestCase):
    def test_all_thirteen_old_slots_and_unhinted_maximum_current_tail_are_read_cold(
        self,
    ):
        current = full_review_comment_tail()
        old = (32768,) * 9 + (65536,) * 4
        self.assertEqual(len(current), 30)
        self.assertEqual(len(old), 13)
        # Full admitted bodies, with no result reference or terminal hint.
        bounds = (*old, *current)
        for order in (
            tuple(bounds),
            tuple(reversed(bounds)),
            tuple(random.Random(17).sample(bounds, len(bounds))),
        ):
            bodies = ["x" * (bound - wire({"body": ""})) for bound in order]
            backend = PackedBackend(["known root"] * 5 + bodies)
            known = {int(row["fullDatabaseId"]): 65536 for row in backend.rows[:5]}
            plan = CommentAllocationPlan(known, tuple(bounds))
            for _ in range(2):
                backend.physical.clear()
                ledger = backend.ledger(cached_body="stale terminal-only hint")
                actual = fetch_packed_comments(ledger, IDENTITY, plan=plan)
                self.assertEqual(
                    [row["body"] for row in actual],
                    [row["body"] for row in backend.rows],
                )
                self.assertLessEqual(len(backend.physical), 10)
                self.assertEqual(backend.physical[0]["query"], METADATA_QUERY)
                self.assertEqual(backend.physical[-1]["query"], METADATA_QUERY)

    def test_escaped_bodies_and_metadata_membership_are_fresh_and_fail_closed(self):
        bodies = ["\\" * 20000, "\U0001f4a5" * 7000, 'quoted " } [ { ' * 1200]
        backend = PackedBackend(bodies)
        plan = CommentAllocationPlan({}, tuple(wire({"body": body}) for body in bodies))
        self.assertEqual(
            len(fetch_packed_comments(backend.ledger(), IDENTITY, plan=plan)), 3
        )
        self.assertEqual(len(backend.physical), 3)
        for mutation in (
            lambda rows: rows[0].update(updatedAt="2026-10-29T01:00:00Z"),
            lambda rows: rows.pop(),
            lambda rows: rows.append(copy.deepcopy(rows[0])),
        ):
            changed = PackedBackend(bodies)
            changed.mutate_closing = mutation
            with self.subTest(mutation=mutation), self.assertRaises(ReviewInputError):
                fetch_packed_comments(changed.ledger(), IDENTITY, plan=plan)

    def test_discovery_excess_body_growth_and_shared_budget_do_not_acquire_capacity(
        self,
    ):
        backend = PackedBackend(["x" * 1000] * 20)
        for plan, maximum in (
            (CommentAllocationPlan({}, (2000,) * 19), 10),
            (CommentAllocationPlan({}, (500,) * 20), 10),
            (CommentAllocationPlan({}, (2000,) * 20), 2),
        ):
            with (
                self.subTest(plan=plan, maximum=maximum),
                self.assertRaises(ReviewInputError),
            ):
                fetch_packed_comments(
                    backend.ledger(), IDENTITY, plan=plan, max_requests=maximum
                )
        immutable = {1: 2000}
        plan = CommentAllocationPlan(immutable, ())
        immutable[1] = 999999
        self.assertEqual(plan.known[1], 2000)

    def test_complete_two_hundred_item_metadata_union_has_exact_overlap(self):
        for count in (0, 1, 99, 100, 101, 199, 200):
            backend = PackedBackend(["small"] * count)
            plan = CommentAllocationPlan({}, (100,) * count)
            self.assertEqual(
                len(fetch_packed_comments(backend.ledger(), IDENTITY, plan=plan)), count
            )
            self.assertLessEqual(len(backend.physical), 4)
        backend = PackedBackend(["small"] * 201)
        with self.assertRaises(ReviewInputError):
            fetch_packed_comments(
                backend.ledger(), IDENTITY, plan=CommentAllocationPlan({}, (100,) * 200)
            )
