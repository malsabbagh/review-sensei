"""Independent installed-wheel acceptance for all hosted GraphQL consumers.

No live network, provider or writes. These targeted consumer seams make the
bounded reader's exact cursor and result-retention contract observable.
"""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import review_sensei_github
from review_sensei_github.errors import GitHubHTTPError, GitHubPublicationError
from review_sensei_github.http import GitHubHttp
from review_sensei_github.publication import (
    ReviewApprovalFinalizer,
    ReviewPublisher,
    finding_instance_marker,
    finding_marker,
)
from review_sensei_github.thread_resolution import FindingThreadResolver

import review_sensei
from review_sensei.context import finding_lifecycle_for_comment
from review_sensei.human_assessment import finding_instance_fingerprint
from review_sensei.models import ReviewComment, ReviewResult

HEAD, BASE, APP = "b" * 40, "a" * 40, "review-sensei[bot]"
SCOPE = dict(
    token="fixture",
    repository="owner/repo",
    repository_id=1,
    pull_request=1,
    head_sha=HEAD,
    base_sha=BASE,
    app_slug=APP,
)


class Response:
    def __init__(self, value):
        self.body = json.dumps(value, separators=(",", ":")).encode()
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size):
        return self.body[:size]


def owned_nodes(count, *, padding=8000):
    nodes, instances = [], []
    for i in range(count):
        comment = ReviewComment(
            path="src/app.py",
            line=i + 1,
            body=f"Unique concern {i}",
            blocking=True,
            symbol=f"operation_{i}",
            defect_kind=f"case-{i}",
        )
        result = ReviewResult(
            summary="Fixture", comments=(comment,), provider="fixture"
        )
        concern = finding_lifecycle_for_comment(comment).fingerprint
        instance = finding_instance_fingerprint(comment)
        body = (
            "x" * padding
            + "\n"
            + finding_instance_marker(comment)
            + "\n"
            + finding_marker(
                repository_id=1,
                pull_request=1,
                head_sha=HEAD,
                base_sha=BASE,
                result=result,
                blocking=True,
                fingerprint=concern,
            )
        )
        nodes.append(
            {
                "id": f"thread-{i}",
                "isResolved": True,
                "comments": {
                    "nodes": [
                        {
                            "databaseId": i + 1,
                            "body": body,
                            "author": {"login": APP, "__typename": "Bot"},
                            "opaque": {"full_record": i, "unicode": "é"},
                        }
                    ]
                },
            }
        )
        instances.append(instance)
    return nodes, frozenset(instances)


class GraphQLServer:
    def __init__(self, nodes):
        self.nodes = nodes
        self.calls = []
        self.http = GitHubHttp(api_url="https://api.github.test", opener=self.open)

    def open(self, request, timeout):
        body = json.loads(request.data)
        self.calls.append(copy.deepcopy(body))
        if request.method != "POST" or not body["query"].lstrip().startswith("query "):
            raise AssertionError(
                "unexpected mutation/read outside bounded GraphQL query"
            )
        first, cursor = body["variables"]["first"], body["variables"]["after"]
        # Deliberately opaque cursor: clients must preserve it, not derive offsets.
        offset = 0 if cursor is None else int(cursor.split("|")[1])
        selected = self.nodes[offset : offset + first]
        end = offset + len(selected)
        return Response(
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "nodes": selected,
                                "pageInfo": {
                                    "hasNextPage": end < len(self.nodes),
                                    "endCursor": f"cursor|{end}|opaque=="
                                    if end < len(self.nodes)
                                    else None,
                                },
                            }
                        }
                    }
                }
            }
        )

    @property
    def cursors(self):
        return [(c["variables"]["first"], c["variables"]["after"]) for c in self.calls]


class ResolutionServer(GraphQLServer):
    """One candidate at the end of a large history; a lost mutation echo."""

    def __init__(self, nodes, *, changed_root=False):
        super().__init__(nodes)
        self.events = []
        self.mutations = []
        self.changed_root = changed_root

    def open(self, request, timeout):
        from urllib.parse import urlsplit

        body = json.loads(request.data) if request.data else None
        operation = body.get("operationName") if body else "RootRead"
        self.events.append(operation)
        if operation == "RootRead":
            identifier = int(urlsplit(request.full_url).path.rsplit("/", 1)[1])
            root = self.nodes[identifier - 1]["comments"]["nodes"][0]
            return Response(
                {
                    "id": identifier,
                    "body": root["body"] + ("edited" if self.changed_root else ""),
                    "pull_request_url": "https://api.github.test/repos/owner/repo/pulls/1",
                    "user": {"login": APP, "type": "Bot"},
                }
            )
        if operation == "ResolveReviewThread":
            thread_id = body["variables"]["input"]["threadId"]
            self.mutations.append(thread_id)
            self.nodes[int(thread_id.split("-")[1])]["isResolved"] = True
            # Real mutation was accepted; the response doesn't confirm it.
            return Response({"data": {}})
        if operation == "ResolutionReadback":
            thread_id = body["variables"]["id"]
            return Response({"data": {"node": {"id": thread_id, "isResolved": True}}})
        return super().open(request, timeout)


CONSUMERS = ("suppression", "blocking", "open", "resolution")


def consume(kind, server, instances):
    if kind == "suppression":
        return ReviewPublisher(http=server.http)._published_finding_suppression(**SCOPE)
    if kind == "blocking":
        return ReviewApprovalFinalizer(http=server.http)._scan_blocking_threads(**SCOPE)
    if kind == "open":
        return ReviewPublisher(http=server.http)._has_open_review_threads(
            token=SCOPE["token"], repository=SCOPE["repository"], pull_request=1
        )
    return FindingThreadResolver(server.http).resolve(
        **SCOPE, instances=instances, fence=lambda: True, skip_resolved=True
    )


class HostedGraphQLQualificationTests(unittest.TestCase):
    def assert_full(self, kind, value, instances):
        if kind == "suppression":
            self.assertEqual(set(value.by_instance), instances)
            self.assertEqual(len(value.by_fingerprint), len(instances))
            self.assertTrue(
                all(v.resolved and v.blocking for v in value.by_instance.values())
            )
        elif kind == "blocking":
            self.assertEqual(value, (False, instances))
        elif kind == "open":
            self.assertIs(value, False)
        else:
            self.assertEqual(set(value.resolved), instances)
            self.assertEqual(len(value.resolved), len(instances))
            self.assertEqual(value.unconfirmed, ())

    def test_all_four_consumers_retain_100_and_250_large_owned_current_roots(self):
        for count in (100, 250):
            for kind in CONSUMERS:
                with self.subTest(count=count, consumer=kind):
                    nodes, instances = owned_nodes(count)
                    original = copy.deepcopy(nodes)
                    server = GraphQLServer(nodes)
                    self.assert_full(kind, consume(kind, server, instances), instances)
                    self.assertEqual(nodes, original)
                    self.assertEqual(server.cursors[:2], [(100, None), (50, None)])
                    self.assertEqual(len(server.calls), 1 + count // 50)
                    self.assertLessEqual(len(server.calls), 10)

    def test_all_four_consumers_retry_nonempty_opaque_cursor_without_loss(self):
        for kind in CONSUMERS:
            with self.subTest(consumer=kind):
                nodes, instances = owned_nodes(250, padding=1)
                for node in nodes[100:200]:
                    node["comments"]["nodes"][0]["body"] += "x" * 8000
                server = GraphQLServer(nodes)
                self.assert_full(kind, consume(kind, server, instances), instances)
                self.assertEqual(
                    server.cursors,
                    [
                        (100, None),
                        (100, "cursor|100|opaque=="),
                        (50, "cursor|100|opaque=="),
                        (50, "cursor|150|opaque=="),
                        (50, "cursor|200|opaque=="),
                    ],
                )

    def test_all_four_consumers_refuse_size_one_overflow_at_original_cursor(self):
        for kind in CONSUMERS:
            with self.subTest(consumer=kind):
                nodes, instances = owned_nodes(101, padding=1)
                nodes[100]["comments"]["nodes"][0]["body"] += "x" * 600_000
                server = GraphQLServer(nodes)
                with self.assertRaises((GitHubHTTPError, GitHubPublicationError)):
                    consume(kind, server, instances)
                self.assertEqual(
                    server.cursors,
                    [(100, None)]
                    + [(size, "cursor|100|opaque==") for size in (100, 50, 25, 5, 1)],
                )

    def test_all_four_consumers_share_ten_physical_attempts_under_repeated_overflow(
        self,
    ):
        for kind in CONSUMERS:
            with self.subTest(consumer=kind):
                nodes, instances = owned_nodes(250, padding=100_000)
                server = GraphQLServer(nodes)
                with self.assertRaises((GitHubHTTPError, GitHubPublicationError)):
                    consume(kind, server, instances)
                self.assertEqual(len(server.calls), 10)
                self.assertEqual(
                    server.cursors[:4], [(size, None) for size in (100, 50, 25, 5)]
                )
                self.assertTrue(all(size == 5 for size, _ in server.cursors[4:]))

    def test_last_owned_root_is_resolved_once_only_after_full_large_discovery(self):
        for count in (100, 250):
            with self.subTest(count=count):
                nodes, _instances = owned_nodes(count)
                nodes[-1]["isResolved"] = False
                from review_sensei_github.publication import finding_instance_from_body

                target = finding_instance_from_body(
                    nodes[-1]["comments"]["nodes"][0]["body"]
                )
                server = ResolutionServer(nodes)
                arguments = dict(
                    **SCOPE,
                    instances=frozenset({target}),
                    fence=lambda: True,
                    skip_resolved=True,
                )
                outcome = FindingThreadResolver(server.http).resolve(**arguments)
                self.assertEqual(outcome.resolved, (target,))
                self.assertEqual(outcome.unconfirmed, ())
                discovery_calls = 1 + count // 50
                self.assertEqual(
                    server.events[:discovery_calls],
                    ["FindingResolutionThreads"] * discovery_calls,
                )
                self.assertEqual(
                    server.events[discovery_calls:],
                    ["RootRead", "ResolveReviewThread", "ResolutionReadback"],
                )
                self.assertEqual(server.mutations, [f"thread-{count - 1}"])
                replay = FindingThreadResolver(server.http).resolve(**arguments)
                self.assertEqual(replay.resolved, (target,))
                self.assertEqual(replay.unconfirmed, ())
                self.assertEqual(server.mutations, [f"thread-{count - 1}"])

    def test_changed_last_root_in_large_history_never_mutates(self):
        nodes, _instances = owned_nodes(250)
        nodes[-1]["isResolved"] = False
        from review_sensei_github.publication import finding_instance_from_body

        target = finding_instance_from_body(nodes[-1]["comments"]["nodes"][0]["body"])
        server = ResolutionServer(nodes, changed_root=True)
        outcome = FindingThreadResolver(server.http).resolve(
            **SCOPE,
            instances=frozenset({target}),
            fence=lambda: True,
            skip_resolved=True,
        )
        self.assertEqual(outcome.resolved, ())
        self.assertEqual(outcome.unconfirmed, (target,))
        self.assertEqual(server.mutations, [])


def setUpModule():
    global _network
    for package in (review_sensei, review_sensei_github):
        if (
            not Path(package.__file__)
            .resolve()
            .is_relative_to(Path(sys.prefix).resolve())
        ):
            raise AssertionError(f"installed distribution required: {package.__name__}")
    _network = patch(
        "socket.socket.connect", side_effect=AssertionError("live network forbidden")
    )
    _network.start()


def tearDownModule():
    _network.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
