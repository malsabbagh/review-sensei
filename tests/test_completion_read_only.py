"""The completion evidence reader may dispatch only its two fixed queries."""

import json
import unittest
from copy import deepcopy

from review_sensei_github.http import GitHubHttp
from review_sensei_github.review_completion_recovery import (
    _ReadOnlyHttp,
    _review_dates,
    _threads,
)

from review_sensei.errors import ReviewInputError
from review_sensei.session import SessionIdentity

THREAD_QUERY = (
    "query ReviewCompletionThreads($owner:String!,$name:String!,$number:Int!,$after:String){"
    "repository(owner:$owner,name:$name){pullRequest(number:$number){"
    "headRefOid baseRefOid reviewThreads(first:100,after:$after){"
    "pageInfo{hasNextPage endCursor}nodes{id isResolved comments(first:1){nodes{"
    "databaseId body createdAt updatedAt author{login __typename}}}}}}}}"
)
DATES_QUERY = (
    "query ReviewCompletionReviewDates($ids:[ID!]!){nodes(ids:$ids){"
    "...on PullRequestReview{databaseId createdAt updatedAt lastEditedAt commit{oid}}}}"
)


class RecordingHttp(GitHubHttp):
    def __init__(self, responses=()):
        super().__init__(api_url="https://example.test")
        self.calls = []
        self.responses = iter(responses)

    def request(self, method, path, *, token, body=None, timeout_seconds=None):
        self.calls.append((method, path, deepcopy(body), timeout_seconds))
        return next(self.responses, (200, {}))


def thread_request():
    return {
        "operationName": "ReviewCompletionThreads",
        "query": THREAD_QUERY,
        "variables": {"owner": "owner", "name": "repo", "number": 7, "after": None},
    }


def date_request():
    return {
        "operationName": "ReviewCompletionReviewDates",
        "query": DATES_QUERY,
        "variables": {"ids": ["node-1", "node-2"]},
    }


class CompletionReadOnlyTests(unittest.TestCase):
    def test_completion_wrapper_rejects_selected_mutations_and_extra_documents(self):
        original = thread_request()
        attacks = [
            {
                **original,
                "query": 'query benign{viewer{login}} mutation ReviewCompletionThreads{resolveReviewThread(input:{threadId:"x"}){clientMutationId}}',
            },
            {
                **original,
                "query": THREAD_QUERY
                + ' mutation Extra{resolveReviewThread(input:{threadId:"x"}){clientMutationId}}',
            },
            {
                **original,
                "query": THREAD_QUERY.replace(
                    "repository(owner:", "alias:repository(owner:"
                ),
            },
            {**original, "query": DATES_QUERY},
            {**original, "operationName": "ReviewCompletionReviewDates"},
            {**original, "extensions": {}},
        ]
        transport = RecordingHttp()
        http = _ReadOnlyHttp(transport)
        for body in attacks:
            with self.assertRaisesRegex(ReviewInputError, "read-only"):
                http.request("POST", "/graphql", token="synthetic", body=body)
        assert transport.calls == []

    def test_only_the_graphql_post_route_can_forward_completion_queries(self):
        transport = RecordingHttp()
        http = _ReadOnlyHttp(transport)
        for method, path in (
            ("POST", "/graphql?query=extra"),
            ("POST", "/other"),
            ("PATCH", "/graphql"),
            ("PUT", "/graphql"),
            ("DELETE", "/graphql"),
        ):
            with self.assertRaisesRegex(ReviewInputError, "read-only"):
                http.request(method, path, token="synthetic", body=thread_request())
        assert not transport.calls
        http.request("GET", "/repos/owner/repo", token="synthetic")
        assert len(transport.calls) == 1

    def test_forwarded_completion_document_is_detached_from_the_caller(self):
        original = thread_request()

        class MutatingCallerHttp(RecordingHttp):
            def request(self, method, path, *, token, body=None, timeout_seconds=None):
                original["query"] = "mutation ReviewCompletionThreads{deleteRepository}"
                original["variables"]["number"] = 99
                return super().request(
                    method,
                    path,
                    token=token,
                    body=body,
                    timeout_seconds=timeout_seconds,
                )

        transport = MutatingCallerHttp()
        _ReadOnlyHttp(transport).request(
            "POST", "/graphql", token="synthetic", body=original
        )
        assert transport.calls[0][2] == thread_request()

    def test_oversized_objects_are_rejected_before_enumerating_keys(self):
        class NoEnumeration(dict):
            def __iter__(self):
                raise AssertionError("oversized object must not be enumerated")

        outer = NoEnumeration(thread_request(), extra=True)
        inner = thread_request()
        inner["variables"] = NoEnumeration(inner["variables"], extra=True)
        transport = RecordingHttp()
        http = _ReadOnlyHttp(transport)
        for body in (outer, inner):
            with self.assertRaisesRegex(ReviewInputError, "read-only"):
                http.request("POST", "/graphql", token="synthetic", body=body)
        assert not transport.calls

    def test_completion_wrapper_rejects_unexpected_or_malformed_variables(self):
        cases = []
        for key, bad in (
            ("owner", 7),
            ("name", ""),
            ("number", True),
            ("number", 0),
            ("after", 3),
        ):
            body = thread_request()
            body["variables"][key] = bad
            cases.append(body)
        for bad in (
            {},
            {"ids": []},
            {"ids": ["a", "a"]},
            {"ids": [True]},
            {"ids": ["a"] * 101},
            {"ids": ["a"], "extra": 1},
        ):
            body = date_request()
            body["variables"] = bad
            cases.append(body)
        body = thread_request()
        body["variables"]["extra"] = 1
        cases.append(body)
        transport = RecordingHttp()
        http = _ReadOnlyHttp(transport)
        for body in cases:
            with self.assertRaisesRegex(ReviewInputError, "read-only"):
                http.request("POST", "/graphql", token="synthetic", body=body)
        assert transport.calls == []

    def test_completion_query_wire_size_keeps_the_existing_eight_kib_bound(self):
        for kind in ("after", "id"):
            body = thread_request() if kind == "after" else date_request()
            if kind == "after":
                body["variables"]["after"] = ""
            else:
                body["variables"]["ids"] = [""]
            empty_size = len(json.dumps(body, separators=(",", ":")).encode("utf-8"))
            boundary = "x" * (8192 - empty_size)
            if kind == "after":
                body["variables"]["after"] = boundary
            else:
                body["variables"]["ids"] = [boundary]
            transport = RecordingHttp()
            http = _ReadOnlyHttp(transport)
            assert len(json.dumps(body, separators=(",", ":")).encode("utf-8")) == 8192
            http.request("POST", "/graphql", token="synthetic", body=body)
            if kind == "after":
                body["variables"]["after"] += "x"
            else:
                body["variables"]["ids"][0] += "x"
            with self.assertRaisesRegex(ReviewInputError, "read-only"):
                http.request("POST", "/graphql", token="synthetic", body=body)
            assert len(transport.calls) == 1

    def test_completion_readers_send_valid_paginated_threads_and_review_ids(self):
        def page(nodes, more, cursor):
            return (
                200,
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "headRefOid": "h",
                                "baseRefOid": "b",
                                "reviewThreads": {
                                    "nodes": nodes,
                                    "pageInfo": {
                                        "hasNextPage": more,
                                        "endCursor": cursor,
                                    },
                                },
                            }
                        }
                    }
                },
            )

        reviews = [
            {"id": i, "node_id": f"node-{i}", "commit_id": "c"} for i in range(1, 101)
        ]
        dates = [
            {
                "databaseId": r["id"],
                "createdAt": "2026-01-01T00:00:00Z",
                "updatedAt": "2026-01-01T00:00:00Z",
                "lastEditedAt": None,
                "commit": {"oid": "c"},
            }
            for r in reviews
        ]
        transport = RecordingHttp(
            [
                page([{"id": "t1"}], True, "cursor/+=:1"),
                page([{"id": "t2"}], False, None),
                (200, {"data": {"nodes": dates}}),
            ]
        )
        http = _ReadOnlyHttp(transport)
        assert _threads(http, "synthetic", SessionIdentity("owner/repo", 7)) == (
            "h",
            "b",
            [{"id": "t1"}, {"id": "t2"}],
        )
        assert len(_review_dates(http, "synthetic", reviews)) == 100
        assert transport.calls[1][2]["variables"]["after"] == "cursor/+=:1"
        assert transport.calls[2][2]["variables"]["ids"] == [
            r["node_id"] for r in reviews
        ]
