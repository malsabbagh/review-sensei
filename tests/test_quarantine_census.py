"""Closed read-only cursor census preserves complete data and physical bounds."""

import copy
import json
import unittest
from types import SimpleNamespace
from urllib.request import Request

from fake_github_http import json_response
from review_sensei_github.http import GitHubHttp
from review_sensei_github.quarantine_census import (
    QUERY,
    fetch_comments,
    is_readonly_census,
)

from review_sensei.errors import ReviewInputError
from review_sensei.session import SessionIdentity

IDENTITY = SessionIdentity("owner/repo", 7, 123)


class CensusBackend:
    def __init__(self, count=5, body_size=20):
        self.rows = [self.node(number, "x" * body_size) for number in range(count)]
        self.calls = []
        self.change = None

    def node(self, number, body):
        return {
            "fullDatabaseId": str(6_445_232_169 + number),
            "body": body,
            "createdAt": "2026-10-28T00:00:00Z",
            "updatedAt": "2026-10-28T00:00:00Z",
            "author": {
                "__typename": "Bot",
                "login": "reviewsensei",
                "databaseId": 317_764_649,
            },
            "pullRequest": {
                "number": 7,
                "repository": {"databaseId": 123, "nameWithOwner": "owner/repo"},
            },
        }

    def opened(self, request, timeout):
        payload = json.loads(request.data)
        assert payload["query"] == QUERY
        variables = payload["variables"]
        self.calls.append(copy.deepcopy(variables))
        offset = int(variables["after"].split(":")[1]) if variables["after"] else 0
        nodes = copy.deepcopy(self.rows[offset : offset + variables["first"]])
        end = offset + len(nodes)
        connection = {
            "totalCount": len(self.rows),
            "nodes": nodes,
            "pageInfo": {
                "hasNextPage": end < len(self.rows),
                "endCursor": f"opaque:{end}" if nodes else None,
            },
        }
        value = {
            "data": {
                "repository": {
                    "databaseId": 123,
                    "nameWithOwner": "owner/repo",
                    "pullRequest": {"number": 7, "comments": connection},
                }
            }
        }
        if self.change:
            self.change(value, connection, len(self.calls))
        return json_response(value)

    def ledger(self, *, cached_body=None):
        cached = [
            {
                "id": int(row["fullDatabaseId"]),
                "body": row["body"] if cached_body is None else cached_body,
            }
            for row in self.rows
        ]
        return SimpleNamespace(
            http=GitHubHttp(api_url="https://api.github.test", opener=self.opened),
            token="synthetic",
            app_slug="reviewsensei[bot]",
            _fence_page_size=100,
            _cached_comments=lambda: cached,
        )


class QuarantineCensusTests(unittest.TestCase):
    def test_complete_bodies_big_numeric_ids_and_numeric_bot_identity_are_not_replaced_by_hints(
        self,
    ):
        backend = CensusBackend(16, 40_000)
        ledger = backend.ledger(cached_body="old hint")
        rows = fetch_comments(ledger, IDENTITY)
        self.assertEqual(len(rows), 16)
        self.assertEqual(rows[0]["id"], 6_445_232_169)
        self.assertEqual(rows[0]["body"], "x" * 40_000)
        self.assertEqual(
            rows[0]["user"],
            {"type": "Bot", "login": "reviewsensei[bot]", "id": 317_764_649},
        )
        self.assertGreater(len(backend.calls), 1)
        # A deleted comment cannot survive the prior observed byte-size hint.
        backend.rows.pop()
        self.assertEqual(len(fetch_comments(ledger, IDENTITY)), 15)

    def test_oversize_hint_exposes_only_counts_and_ignores_quoted_braces(self):
        from review_sensei_github.errors import GitHubHTTPResponseTooLargeError
        from review_sensei_github.http import _completed_json_objects

        raw = json.dumps(
            {"body": 'quoted } { \\" and slash \\\\', "nested": {"value": "}"}}
        ).encode()
        self.assertEqual(_completed_json_objects(raw), 2)
        backend = CensusBackend(16, 40_000)
        ledger = backend.ledger(cached_body="old hint")
        with self.assertRaises(GitHubHTTPResponseTooLargeError) as caught:
            ledger.http.request(
                "POST",
                "/graphql",
                token="synthetic",
                body={
                    "query": QUERY,
                    "variables": {
                        "owner": "owner",
                        "name": "repo",
                        "number": 7,
                        "first": 100,
                        "after": None,
                    },
                },
            )
        error = caught.exception
        self.assertGreater(error.completed_json_objects, 0)
        self.assertFalse(
            any(isinstance(value, (bytes, bytearray)) for value in vars(error).values())
        )
        self.assertNotIn("xxxx", str(error))
        self.assertEqual(len(fetch_comments(ledger, IDENTITY)), 16)
        # The oversized prefix yields only a size hint. Both subsequent pages
        # still return complete, freshly fetched objects under the byte cap.
        self.assertEqual(len(backend.calls), 4)

    def test_errors_null_partial_foreign_duplicate_and_invalid_pagination_refuse(self):
        cases = (
            "errors",
            "null",
            "foreign-repo",
            "foreign-pr",
            "wrong-count",
            "duplicate",
            "null-node",
            "bool-id",
            "missing-cursor",
            "short-page",
            "changed-count",
            "repeated-cursor",
        )
        for case in cases:
            with self.subTest(case=case):
                backend = CensusBackend(5, 200_000)

                def change(value, connection, call):
                    if case == "errors":
                        value["errors"] = [{"message": "partial"}]
                    elif case == "null":
                        value["data"]["repository"] = None
                    elif case == "foreign-repo":
                        value["data"]["repository"]["databaseId"] = 124
                    elif case == "foreign-pr":
                        connection["nodes"][0]["pullRequest"]["number"] = 8
                    elif case == "wrong-count":
                        connection["totalCount"] = 0
                    elif case == "duplicate":
                        connection["nodes"][1]["fullDatabaseId"] = connection["nodes"][
                            0
                        ]["fullDatabaseId"]
                    elif case == "null-node":
                        connection["nodes"][0] = None
                    elif case == "bool-id":
                        connection["nodes"][0]["fullDatabaseId"] = True
                    elif case == "missing-cursor":
                        connection["pageInfo"]["endCursor"] = None
                    elif case == "short-page":
                        connection["nodes"].pop()
                    elif case == "changed-count" and call > 1:
                        connection["totalCount"] += 1
                    elif case == "repeated-cursor" and call > 1:
                        connection["pageInfo"]["endCursor"] = "opaque:2"

                backend.change = change
                with self.assertRaises(ReviewInputError):
                    fetch_comments(backend.ledger(), IDENTITY)
                self.assertLessEqual(len(backend.calls), 10)

    def test_ten_physical_attempts_never_expand_to_complete_an_unfunded_census(self):
        backend = CensusBackend(12, 400_000)
        with self.assertRaisesRegex(ReviewInputError, "physical request bound"):
            fetch_comments(backend.ledger(), IDENTITY)
        self.assertEqual(len(backend.calls), 10)

    def test_only_the_exact_source_query_and_closed_variables_are_readonly(self):
        variables = {
            "owner": "owner",
            "name": "repo",
            "number": 7,
            "first": 100,
            "after": None,
        }

        def request(body, url="https://api.github.test/graphql", method="POST"):
            return Request(
                url,
                data=body if isinstance(body, bytes) else json.dumps(body).encode(),
                method=method,
            )

        body = {"query": QUERY, "variables": variables}
        self.assertTrue(is_readonly_census(request(body), "https://api.github.test"))
        for value in (
            {**body, "query": QUERY + " mutation {danger}"},
            {**body, "operationName": "extra"},
            [body],
            {**body, "variables": {**variables, "first": True}},
            {**body, "variables": {**variables, "owner": "owner){danger}"}},
            {**body, "variables": {**variables, "after": "x" * 2049}},
        ):
            self.assertFalse(
                is_readonly_census(request(value), "https://api.github.test")
            )
        duplicate = (
            b'{"query":"mutation {danger}","query":'
            + json.dumps(QUERY).encode()
            + b',"variables":'
            + json.dumps(variables).encode()
            + b"}"
        )
        self.assertFalse(
            is_readonly_census(request(duplicate), "https://api.github.test")
        )
        for url in (
            "https://foreign.test/graphql",
            "https://api.github.test/graphql?extra=1",
            "https://api.github.test/repos/owner/repo",
        ):
            self.assertFalse(
                is_readonly_census(request(body, url), "https://api.github.test")
            )
        self.assertFalse(
            is_readonly_census(request(body, method="GET"), "https://api.github.test")
        )
        self.assertFalse(is_readonly_census(request(body), None))
