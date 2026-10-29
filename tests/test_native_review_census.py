"""Physical ten-call opening/body/middle/closing walk, without publication."""

import copy
import json
import unittest

import test_native_review_groups as grouping_fixture
from fake_github_http import json_response
from review_sensei_github.http import GitHubHttp
from review_sensei_github.native_review_census import (
    CLOSE_MIDDLE_QUERY,
    CLOSE_QUERY,
    MAX_EXISTING_ROOT_IDS_BYTES,
    OLD_QUERY,
    OPEN_QUERY,
    PART_MIDDLE_QUERY,
    PART_OLD_QUERY,
    PART_ROOTS_QUERY,
    ROOT_METADATA_BYTES,
    ROOT_OPEN_QUERY,
    closing_envelope_bound,
    read_publication_union,
    source_query_variables,
)
from review_sensei_github.native_review_groups import (
    PART_QUERY,
    NativeInline,
    NativeReviewPlan,
    NativeWalkBudget,
    wire_bytes,
)
from review_sensei_github.quarantine_census import is_readonly_census

from review_sensei.errors import ReviewInputError
from review_sensei.presentation import MAINTAINER_RESPONSE_INSTRUCTIONS


class NativeCensusBackend:
    def __init__(self):
        _, assembled = grouping_fixture.maximum_mixture()
        self.plan = NativeReviewPlan.prepare(
            repository_id=1,
            pull_request=1,
            base_sha="a" * 40,
            head_sha="b" * 40,
            transaction_id="c" * 64,
            result_sha256="d" * 64,
            items=tuple(
                NativeInline.from_prepared(comment, body, anchor=anchor)
                for comment, _, body, anchor in assembled.inline_comments
            ),
        )
        fixture = grouping_fixture.NativeReviewPlanTests()
        self.group_payloads = [
            fixture.response(self.plan, ordinal)
            for ordinal in range(len(self.plan.groups))
        ]
        self.main_id = 2**63 - 1000
        self.main_body = "Main review.\n\n" + MAINTAINER_RESPONSE_INSTRUCTIONS
        self.reviews = [
            {
                "i": "part:" + str(ordinal),
                "n": response["data"]["node"]["n"],
                "s": "COMMENTED",
                "a": {"t": "Bot", "n": 999},
                "c": {"o": self.plan.head_sha},
                "x": {"totalCount": len(self.plan.groups[ordinal])},
            }
            for ordinal, response in enumerate(self.group_payloads)
        ]
        self.reviews.append(
            {
                "i": "main",
                "n": str(self.main_id),
                "s": "COMMENTED",
                "a": {"t": "Bot", "n": 999},
                "c": {"o": self.plan.head_sha},
                "x": {"totalCount": 0},
            }
        )
        self.roots, self.old_rows, self.old_nodes = [], [], {}
        for ordinal in range(14):
            identifier = 1_000_000 + ordinal
            comment = {
                "n": str(identifier),
                "b": "Old complete explanation. " * 50,
                "p": "f.py",
                "l": 1,
                "o": 1,
                "a": {"t": "Bot", "n": 999},
                "c": {"o": "a" * 40},
                "z": {"o": "a" * 40},
                "r": {"n": "99"},
                "q": None,
                "u": "2026-10-28T00:00:00Z",
            }
            self.old_rows.append(comment)
            self.old_nodes[identifier] = "old:" + str(identifier)
            self.roots.append(self.root(comment))
        for response in self.group_payloads:
            for comment in response["data"]["node"]["x"]["nodes"]:
                self.roots.append(self.root(comment))
        self.calls = []
        self.change = None
        self.http = GitHubHttp(api_url="https://api.github.test", opener=self.opened)

    @staticmethod
    def root(comment):
        return {
            "s": False,
            "d": "RIGHT",
            "t": "LINE",
            "x": {
                "totalCount": 1,
                "nodes": [
                    {
                        key: copy.deepcopy(comment[key])
                        for key in ("n", "r", "a", "c", "q", "u")
                    }
                ],
            },
        }

    def scope(self):
        return {
            "n": self.plan.pull_request,
            "h": self.plan.head_sha,
            "b": self.plan.base_sha,
        }

    def window(self, rows, *, first=False, last=False):
        page = (
            {
                "hasNextPage": len(self.roots) > 100,
                "endCursor": "cursor:100" if self.roots else None,
            }
            if first
            else {"hasPreviousPage": len(self.roots) > 100}
            if last
            else {"hasNextPage": True}
        )
        return {
            "totalCount": len(self.roots),
            "nodes": copy.deepcopy(rows),
            "pageInfo": page,
        }

    def opened(self, request, timeout):
        assert is_readonly_census(request, self.http.api_url)
        assert len(request.data) <= 8192
        value = json.loads(request.data)
        query, variables = value["query"], value["variables"]
        self.calls.append(query)
        data = {}
        if query in {PART_QUERY, PART_MIDDLE_QUERY, PART_ROOTS_QUERY, PART_OLD_QUERY}:
            ordinal = int(variables["id"].split(":")[1])
            data.update(copy.deepcopy(self.group_payloads[ordinal]["data"]))
        if query == OPEN_QUERY:
            pr = {
                **self.scope(),
                "r": {
                    "totalCount": len(self.reviews),
                    "nodes": copy.deepcopy(self.reviews[:100]),
                    "pageInfo": {"hasNextPage": len(self.reviews) > 100},
                },
                "v": {
                    "totalCount": len(self.reviews),
                    "nodes": copy.deepcopy(self.reviews[-100:]),
                    "pageInfo": {"hasPreviousPage": len(self.reviews) > 100},
                },
            }
            data["repository"] = {"n": self.plan.repository_id, "p": pr}
        if query in {ROOT_OPEN_QUERY, PART_ROOTS_QUERY, OLD_QUERY}:
            data["repository"] = {
                "n": self.plan.repository_id,
                "p": {
                    **self.scope(),
                    "f": self.window(self.roots[:100], first=True),
                    "l": self.window(self.roots[-100:], last=True),
                },
            }
        if query == PART_MIDDLE_QUERY:
            data["repository"] = {
                "n": self.plan.repository_id,
                "p": {
                    **self.scope(),
                    "m": self.window(self.roots[100 : 100 + variables["first"]]),
                },
            }
        if query in {CLOSE_QUERY, CLOSE_MIDDLE_QUERY}:
            pr = {
                **self.scope(),
                "r": {
                    "totalCount": len(self.reviews),
                    "nodes": [
                        {"n": row["n"], "s": row["s"]} for row in self.reviews[:100]
                    ],
                    "pageInfo": {"hasNextPage": len(self.reviews) > 100},
                },
                "v": {
                    "totalCount": len(self.reviews),
                    "nodes": [
                        {"n": row["n"], "s": row["s"]} for row in self.reviews[-100:]
                    ],
                    "pageInfo": {"hasPreviousPage": len(self.reviews) > 100},
                },
                "f": self.window(self.roots[: variables["front"]], first=True),
                "l": self.window(self.roots[-variables["back"] :], last=True),
            }
            if query == CLOSE_MIDDLE_QUERY:
                pr["m"] = self.window(self.roots[100 : 100 + variables["first"]])
            data["repository"] = {"n": self.plan.repository_id, "p": pr}
            data["frames"] = [
                {
                    "n": str(self.main_id),
                    "b": self.main_body,
                    "s": "COMMENTED",
                    "a": {"t": "Bot", "n": 999},
                    "c": {"o": self.plan.head_sha},
                    "p": {
                        "n": self.plan.pull_request,
                        "r": {"n": self.plan.repository_id},
                    },
                    "x": {"totalCount": 0},
                },
                *copy.deepcopy(getattr(self, "approval_rows", [])),
            ]
        if query in {OLD_QUERY, PART_OLD_QUERY}:
            if query == PART_OLD_QUERY:
                data["repository"] = {"n": self.plan.repository_id, "p": self.scope()}
            data["old"] = [
                {
                    "i": self.old_nodes[int(row["n"])],
                    **self.root(row),
                    "x": {"totalCount": 1, "nodes": [copy.deepcopy(row)]},
                }
                for row in self.old_rows
            ]
        result = {"data": data}
        if self.change:
            self.change(query, result)
        response = json_response(result)
        assert len(response.body) <= 512 * 1024
        return response

    def read(self):
        self.budget = NativeWalkBudget()
        return read_publication_union(
            self.http,
            token="synthetic",
            repository="owner/repo",
            plan=self.plan,
            producer_id=999,
            group_review_ids=tuple(
                int(row["n"]) for row in self.reviews[: len(self.plan.groups)]
            ),
            main_review_id=self.main_id,
            main_body=self.main_body,
            main_state="COMMENTED",
            old_root_nodes=self.old_nodes,
            budget=self.budget,
        )


class NativeReviewCensusTests(unittest.TestCase):
    def test_actual_closing_request_fits_with_maximum_ids_cursors_and_names(self):
        roots = [f"{number:02}" + "x" * 25 for number in range(49)] + ["49" + "x" * 60]
        variables = {
            "owner": "x" * 256,
            "name": "y" * 256,
            "number": 2**31 - 1,
            "front": 100,
            "back": 100,
            "frames": ["x" * 2048, "z" * 2048],
            "first": 100,
            "after": "y" * 2048,
        }
        self.assertEqual(wire_bytes(roots), MAX_EXISTING_ROOT_IDS_BYTES)
        self.assertTrue(source_query_variables(CLOSE_MIDDLE_QUERY, variables))
        # GitHubHttp uses these exact separators on its physical request.
        encoded = json.dumps(
            {"query": CLOSE_MIDDLE_QUERY, "variables": variables}, separators=(",", ":")
        ).encode()
        self.assertLessEqual(len(encoded), 8192)
        old_variables = {
            "owner": "x" * 256,
            "name": "y" * 256,
            "number": 2**31 - 1,
            "id": "x" * 2048,
            "roots": roots,
        }
        self.assertTrue(source_query_variables(PART_OLD_QUERY, old_variables))
        self.assertLessEqual(
            wire_bytes({"query": PART_OLD_QUERY, "variables": old_variables}), 8192
        )

    def test_200_reviews_with_2048_byte_node_ids_keep_the_same_ten_call_walk(self):
        backend = NativeCensusBackend()
        while len(backend.reviews) < 200:
            ordinal = len(backend.reviews)
            backend.reviews.append(
                {
                    "i": "implicit:" + str(ordinal),
                    "n": str(10000 + ordinal),
                    "s": "COMMENTED",
                    "a": {"t": "Bot", "n": 999},
                    "c": {"o": backend.plan.head_sha},
                    "x": {
                        "totalCount": 1,
                        "nodes": [{"n": str(20000 + ordinal), "q": {"n": "1000000"}}],
                    },
                }
            )
        for row in backend.reviews:
            row["i"] = row["i"] + ":" + "x" * (2047 - len(row["i"]))
        evidence = backend.read()
        self.assertEqual(evidence.physical_reads, 10)
        self.assertEqual(len(evidence.roots), 264)
        self.assertEqual(backend.calls.count(PART_ROOTS_QUERY), 1)
        self.assertEqual(backend.calls.count(PART_MIDDLE_QUERY), 1)

    def test_97_review_metadata_rows_include_reply_associated_reviews_without_extra_body_calls(
        self,
    ):
        backend = NativeCensusBackend()
        while len(backend.reviews) < 97:
            ordinal = len(backend.reviews)
            backend.reviews.append(
                {
                    "i": "implicit:" + str(ordinal),
                    "n": str(10000 + ordinal),
                    "s": "COMMENTED",
                    "a": {"t": "Bot", "n": 999},
                    "c": {"o": backend.plan.head_sha},
                    "x": {
                        "totalCount": 1,
                        "nodes": [{"n": str(20000 + ordinal), "q": {"n": "1000000"}}],
                    },
                }
            )
        result = backend.read()
        self.assertEqual(result.physical_reads, 10)
        self.assertEqual(len(result.groups), 8)

    def test_eight_complete_bodies_and_264_roots_finish_in_ten_physical_calls(self):
        backend = NativeCensusBackend()
        self.assertEqual(len(backend.plan.groups), 8)
        evidence = backend.read()
        self.assertEqual(evidence.physical_reads, 10)
        self.assertEqual(len(evidence.groups), 8)
        self.assertEqual(len(evidence.roots), 264)
        self.assertEqual(len(evidence.old_root_bodies), 14)
        self.assertEqual(backend.calls.count(PART_MIDDLE_QUERY), 1)
        self.assertEqual(backend.calls[-1], CLOSE_MIDDLE_QUERY)
        with self.assertRaises(ReviewInputError):
            backend.budget.request(
                backend.http,
                token="synthetic",
                query=OPEN_QUERY,
                variables={"owner": "owner", "name": "repo", "number": 1},
            )
        self.assertEqual(len(backend.calls), 10)

    def test_existing_bodies_share_smallest_packet_and_remain_version_fenced(self):
        backend = NativeCensusBackend()
        backend.read()
        self.assertEqual(backend.calls.count(PART_OLD_QUERY), 1)
        self.assertEqual(backend.calls[1], PART_OLD_QUERY)
        backend = NativeCensusBackend()
        backend.change = lambda query, value: (
            value["data"]["old"].pop() if query == PART_OLD_QUERY else None
        )
        with self.assertRaises(ReviewInputError):
            backend.read()
        backend = NativeCensusBackend()
        backend.change = lambda query, value: (
            value["data"]["old"][0]["x"]["nodes"][0].update(u="2026-10-27T00:00:00Z")
            if query == PART_OLD_QUERY
            else None
        )
        with self.assertRaises(ReviewInputError):
            backend.read()

    def test_actual_approval_body_is_returned_and_changed_scope_refuses(self):
        for changed in (False, True):
            backend = NativeCensusBackend()
            backend.reviews.append(
                {
                    "i": "approval",
                    "n": "9000",
                    "s": "APPROVED",
                    "a": {"t": "Bot", "n": 999},
                    "c": {"o": backend.plan.head_sha},
                    "x": {"totalCount": 0, "nodes": []},
                }
            )
            approval = {
                "n": "9000",
                "b": "Exact original approval marker.",
                "s": "APPROVED",
                "a": {"t": "Bot", "n": 999},
                "c": {"o": backend.plan.head_sha},
                "p": {
                    "n": backend.plan.pull_request,
                    "r": {"n": backend.plan.repository_id},
                },
                "x": {"totalCount": 0},
            }
            backend.approval_rows = [approval]
            if changed:
                approval["c"]["o"] = "d" * 40
                with self.assertRaises(ReviewInputError):
                    backend.read()
            else:
                evidence = backend.read()
                self.assertEqual(evidence.approvals, (approval,))
                self.assertEqual(evidence.physical_reads, 10)

    def test_joint_closing_and_selected_metadata_bounds_fit_source_maxima(self):
        maximum = {
            "s": False,
            "d": "RIGHT",
            "t": "LINE",
            "x": {
                "totalCount": 1000,
                "nodes": [
                    {
                        "n": str(2**63 - 1),
                        "r": {"n": str(2**63 - 1)},
                        "a": {"t": "Bot", "n": 2**63 - 1},
                        "c": {"o": "f" * 40},
                        "q": None,
                        "u": "9" * 64,
                    }
                ],
            },
        }
        self.assertLessEqual(wire_bytes(maximum), ROOT_METADATA_BYTES)
        self.assertLess(closing_envelope_bound(), 512 * 1024)

    def test_closing_membership_body_head_and_parent_edits_never_become_evidence(self):
        changes = [
            lambda value: value["data"]["frames"][0].update(b="changed"),
            lambda value: value["data"]["repository"]["p"].update(h="e" * 40),
            lambda value: value["data"]["repository"]["p"]["m"]["nodes"][0]["x"][
                "nodes"
            ][0]["r"].update(n="100"),
            lambda value: value["data"]["repository"]["p"]["f"]["nodes"][0]["x"][
                "nodes"
            ][0].update(n="12345"),
        ]
        for change in changes:
            backend = NativeCensusBackend()
            backend.change = lambda query, value: (
                change(value) if query == CLOSE_MIDDLE_QUERY else None
            )
            with self.subTest(change=change), self.assertRaises(ReviewInputError):
                backend.read()
            self.assertLessEqual(len(backend.calls), 10)
