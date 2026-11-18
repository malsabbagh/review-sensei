"""Pure negative filters never authenticate supplied scope/receipt models."""

import copy
import unittest
from dataclasses import replace

from review_sensei_github.ordinary_drift_read_contract import (
    DriftReadScope,
    DriftReadTarget,
    classify_read,
    read_limits,
    require_complete_read,
)
from review_sensei_github.publication import _REVIEW_THREADS_QUERY

from review_sensei.errors import ReviewInputError


class OrdinaryDriftReadContractTests(unittest.TestCase):
    def setUp(self):
        self.scope = DriftReadScope(
            repository="owner/repo",
            pull_request=7,
            head_sha="a" * 40,
            thread_query=_REVIEW_THREADS_QUERY,
            inline_comment_ids=frozenset({201, 202}),
            check_run_id=31,
            review_id=41,
            review_comment_count=4,
        )
        self.thread_body = {
            "operationName": "ReviewThreads",
            "query": _REVIEW_THREADS_QUERY,
            "variables": {
                "owner": "owner",
                "name": "repo",
                "number": 7,
                "after": None,
                "first": 100,
            },
        }

    @staticmethod
    def threads(count=1):
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [
                                {
                                    "id": str(n),
                                    "comments": {"nodes": [{"body": "text"}]},
                                }
                                for n in range(count)
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        }

    def test_role_limits_are_detached_fourteen_read_accounting_only(self):
        expected = {
            "checks": 3,
            "check": 1,
            "pr": 2,
            "threads": 4,
            "reviews": 1,
            "inline_comment": 1,
            "review": 1,
            "review_comments": 1,
        }
        self.assertEqual(read_limits(), expected)
        self.assertEqual(sum(read_limits().values()), 14)
        changed = read_limits()
        changed["checks"] = 1000
        self.assertEqual(read_limits(), expected)

    def test_exact_positive_source_shapes_cover_every_distinct_role(self):
        cases = (
            (
                f"/repos/owner/repo/commits/{'a' * 40}/check-runs?check_name=ReviewSensei&per_page=100",
                "checks",
                {"check_runs": [], "total_count": 0},
            ),
            ("/repos/owner/repo/check-runs/31", "check", {"id": 31}),
            ("/repos/owner/repo/pulls/7", "pr", {"number": 7}),
            ("/repos/owner/repo/pulls/7/reviews?per_page=100&page=1", "reviews", []),
            ("/repos/owner/repo/pulls/comments/201", "inline_comment", {"id": 201}),
            ("/repos/owner/repo/pulls/7/reviews/41", "review", {"id": 41}),
            (
                "/repos/owner/repo/pulls/7/reviews/41/comments?page=1&per_page=100",
                "review_comments",
                [{} for _ in range(4)],
            ),
        )
        for path, role, payload in cases:
            with self.subTest(role=role):
                target = classify_read(self.scope, "GET", path)
                self.assertEqual(target.role, role)
                require_complete_read(target, 200, payload)
        target = classify_read(self.scope, "POST", "/graphql", self.thread_body)
        self.assertEqual(target.role, "threads")
        require_complete_read(target, 200, self.threads(100))

    def test_foreign_paths_parameters_mutations_and_unbound_ids_refuse(self):
        paths = (
            "https://api.github.test/repos/owner/repo/pulls/7",
            "/repos/other/repo/pulls/7",
            "/repos/owner/repo/pulls/8",
            "/repos/owner/repo/pulls/7?",
            "/repos/owner/repo/pulls/7#",
            "/repos/owner/repo/pulls/7?x=1",
            "/repos/owner/repo/pulls/comments/203",
            "/repos/owner/repo/pulls/comments/0201",
            "/repos/owner/repo/check-runs/32",
            "/repos/owner/repo/pulls/7/reviews/42",
            "/repos/owner/repo/pulls/7/reviews/42/comments?per_page=100&page=1",
            "/repos/owner/repo/pulls/7/reviews?per_page=100&page=2",
            "/repos/owner/repo/pulls/7/reviews?per_page=100&page=1&page=1",
            "/repos/owner/repo/pulls/7/reviews?per_page=100&%70age=1",
            "/repos/owner/repo/pulls/7/reviews?per_page=50&page=1",
            f"/repos/owner/repo/commits/{'b' * 40}/check-runs?check_name=ReviewSensei&per_page=100",
            f"/repos/owner/repo/commits/{'a' * 40}/check-runs?check_name=ReviewSensei&per_page=100&filter=all",
        )
        for path in paths:
            with self.subTest(path=path), self.assertRaises(ReviewInputError):
                classify_read(self.scope, "GET", path)
        for method in ("POST", "PATCH", "PUT", "DELETE"):
            with self.subTest(method=method), self.assertRaises(ReviewInputError):
                classify_read(self.scope, method, "/repos/owner/repo/pulls/7")
        with self.assertRaises(ReviewInputError):
            classify_read(self.scope, "GET", "/repos/owner/repo/pulls/7", {})
        unbound = replace(
            self.scope, review_id=None, review_comment_count=None, check_run_id=None
        )
        for path in (
            "/repos/owner/repo/check-runs/31",
            "/repos/owner/repo/pulls/7/reviews/41/comments?per_page=100&page=1",
        ):
            with self.assertRaises(ReviewInputError):
                classify_read(unbound, "GET", path)
        missing_count = replace(self.scope, review_comment_count=None)
        with self.assertRaises(ReviewInputError):
            classify_read(
                missing_count,
                "GET",
                "/repos/owner/repo/pulls/7/reviews/41/comments?per_page=100&page=1",
            )

    def test_exact_query_and_variable_identity_not_query_prefix(self):
        variants = []
        for query in (
            _REVIEW_THREADS_QUERY + "\nquery Foreign { viewer { login } }",
            "mutation ReviewThreads { deleteThing }",
            _REVIEW_THREADS_QUERY + " ",
        ):
            variants.append({**self.thread_body, "query": query})
        variants.extend(
            (
                {**self.thread_body, "operationName": "Foreign"},
                {**self.thread_body, "extensions": {}},
            )
        )
        for key, value in (
            ("owner", "other"),
            ("number", 8),
            ("after", "cursor"),
            ("first", 50),
            ("first", True),
        ):
            variants.append(
                {
                    **self.thread_body,
                    "variables": {**self.thread_body["variables"], key: value},
                }
            )
        variants.append(
            {
                **self.thread_body,
                "variables": {**self.thread_body["variables"], "extra": 1},
            }
        )
        for body in variants:
            with self.subTest(body=body), self.assertRaises(ReviewInputError):
                classify_read(self.scope, "POST", "/graphql", body)
        with self.assertRaises(ReviewInputError):
            replace(self.scope, thread_query="mutation ReviewThreads { deleteThing }")
        with self.assertRaises(ReviewInputError):
            replace(
                self.scope,
                thread_query=_REVIEW_THREADS_QUERY
                + " query Other { viewer { login } }",
            )

    def test_every_denial_and_incomplete_collection_holds_without_warning(self):
        targets = (
            (DriftReadTarget("checks"), {"check_runs": [], "total_count": 0}),
            (DriftReadTarget("reviews"), []),
            (DriftReadTarget("review_comments", 41, 4), [{} for _ in range(4)]),
            (DriftReadTarget("threads"), self.threads()),
        )
        for target, payload in targets:
            for status in (True, 201, 401, 403, 404, 429, 500):
                with (
                    self.subTest(role=target.role, status=status),
                    self.assertRaises(ReviewInputError),
                ):
                    require_complete_read(target, status, payload)
        for payload in (
            {"check_runs": []},
            {"check_runs": [], "total_count": False},
            {"check_runs": [], "total_count": 1},
            {"check_runs": [{} for _ in range(100)], "total_count": 100},
            {"check_runs": ()},
        ):
            with self.subTest(payload=payload), self.assertRaises(ReviewInputError):
                require_complete_read(DriftReadTarget("checks"), 200, payload)
        for payload in (None, {}, (), [{} for _ in range(100)]):
            with self.assertRaises(ReviewInputError):
                require_complete_read(DriftReadTarget("reviews"), 200, payload)
        for count in (0, 3, 5, 100):
            with self.assertRaises(ReviewInputError):
                require_complete_read(
                    DriftReadTarget("review_comments", 41, 4),
                    200,
                    [{} for _ in range(count)],
                )

    def test_graphql_requires_complete_expected_and_nested_page_shapes(self):
        variants = [
            None,
            {},
            {"data": None},
            {**self.threads(), "errors": [{"message": "failed"}]},
            self.threads(101),
        ]
        for page in (
            {},
            {"hasNextPage": True, "endCursor": "next"},
            {"hasNextPage": False},
            {"hasNextPage": 0, "endCursor": None},
        ):
            value = self.threads()
            value["data"]["repository"]["pullRequest"]["reviewThreads"]["pageInfo"] = (
                page
            )
            variants.append(value)
        for nested in ({"hasNextPage": True}, {"hasPreviousPage": True}, {}):
            value = self.threads()
            value["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"][0][
                "comments"
            ]["pageInfo"] = nested
            variants.append(value)
        roots = self.threads()
        roots["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"][0][
            "comments"
        ]["nodes"].append({})
        variants.append(roots)
        cycle = self.threads()
        cycle["cycle"] = cycle
        variants.append(cycle)
        for value in variants:
            with (
                self.subTest(value=repr(value)[:80]),
                self.assertRaises(ReviewInputError),
            ):
                require_complete_read(DriftReadTarget("threads"), 200, value)

    def test_object_identity_and_mutated_filter_values_refuse(self):
        for role, expected in (
            ("pr", 7),
            ("check", 31),
            ("review", 41),
            ("inline_comment", 201),
        ):
            field = "number" if role == "pr" else "id"
            for value in ({}, {field: True}, {field: expected + 1}, None):
                with (
                    self.subTest(role=role, value=value),
                    self.assertRaises(ReviewInputError),
                ):
                    require_complete_read(DriftReadTarget(role, expected), 200, value)
        changed = copy.copy(self.scope)
        object.__setattr__(changed, "inline_comment_ids", {201})
        with self.assertRaises(ReviewInputError):
            classify_read(changed, "GET", "/repos/owner/repo/pulls/comments/201")
        for changes in (
            {"pull_request": True},
            {"repository": "../repo"},
            {"head_sha": "a" * 39},
            {"check_run_id": 0},
            {"review_comment_count": 100},
        ):
            with self.subTest(changes=changes), self.assertRaises(ReviewInputError):
                replace(self.scope, **changes)


if __name__ == "__main__":
    unittest.main()
