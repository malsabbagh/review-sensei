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
    MIDDLE_QUERY,
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
    def __init__(self, *, items=None, old_count=14):
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
            )
            if items is None
            else items,
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
        for ordinal in range(old_count):
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
        from review_sensei_github.native_review_census import (
            ROOT_ID_QUERIES,
            ROOT_IDS_FORWARD_QUERY,
            ROOT_IDS_QUERY,
            root_ids_base_query,
        )

        actual_query = query
        if not hasattr(self, "requests"):
            self.requests = []
        self.requests.append(copy.deepcopy(value))
        query = root_ids_base_query(query)
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
        if query in {PART_MIDDLE_QUERY, MIDDLE_QUERY}:
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
        if actual_query in {*ROOT_ID_QUERIES, ROOT_IDS_QUERY, ROOT_IDS_FORWARD_QUERY}:
            pr = data.setdefault(
                "repository", {"n": self.plan.repository_id, "p": self.scope()}
            )["p"]
            ids = getattr(self, "thread_ids", {})

            def id_window(rows, *, first, offset=0):
                nodes = []
                for row in rows:
                    root = int(row["x"]["nodes"][0]["n"])
                    nodes.append(
                        {
                            "i": ids.get(
                                root, self.old_nodes.get(root, "thread:" + str(root))
                            ),
                            "x": {"nodes": [{"n": str(root)}]},
                        }
                    )
                count = len(self.roots)
                page = (
                    {
                        "hasNextPage": offset + len(rows) < count,
                        "endCursor": "ids:" + str(offset + len(rows)) if rows else None,
                    }
                    if first
                    else {"hasPreviousPage": len(rows) < count}
                )
                return {"totalCount": count, "nodes": nodes, "pageInfo": page}

            if actual_query == ROOT_IDS_QUERY:
                pr["g"] = id_window(self.roots[:100], first=True)
                pr["j"] = id_window(self.roots[-100:], first=False)
            elif actual_query in ROOT_ID_QUERIES and ROOT_ID_QUERIES[actual_query][1]:
                pr["g"] = id_window(self.roots[-variables["u"] :], first=False)
            else:
                offset = (
                    0
                    if variables["v"] is None
                    else int(variables["v"].rsplit(":", 1)[1])
                )
                pr["g"] = id_window(
                    self.roots[offset : offset + variables["u"]],
                    first=True,
                    offset=offset,
                )
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
    def test_direct_custom_reader_cannot_supply_an_unproved_native_shortcut(self):
        from unittest.mock import Mock

        from review_sensei_github.publication import ReviewApprovalFinalizer

        custom = Mock()
        custom.native_thread_nodes.return_value = ()
        finalizer = ReviewApprovalFinalizer(
            http=GitHubHttp(),
            human_inventory_reader=custom,
        )
        self.assertIsNone(
            finalizer._native_thread_nodes(
                token="synthetic",
                repository="owner/repo",
                pull_request=1,
                head_sha="b" * 40,
                app_slug="reviewsensei[bot]",
            )
        )
        custom.native_thread_nodes.assert_not_called()

    def test_scoped_native_shortcut_requires_the_source_owned_proof_reader(self):
        from unittest.mock import Mock

        from review_sensei_github.default_human_reader import (
            DefaultHumanInventoryReader,
        )
        from review_sensei_github.publication import _ScopedHumanInventoryReader

        from review_sensei.bounded_evidence import EvidenceReadBudget

        factory = Mock()
        scoped = _ScopedHumanInventoryReader(
            factory,
            token="synthetic",
            repository="owner/repo",
            pull_request=1,
            head_sha="b" * 40,
            app_slug="reviewsensei[bot]",
            budget=EvidenceReadBudget(),
        )
        self.assertIsNone(scoped.native_thread_nodes())
        factory.assert_not_called()
        custom = Mock()
        custom.native_thread_nodes.return_value = ({"unproved": "partial view"},)
        scoped.reader = custom
        self.assertIsNone(scoped.native_thread_nodes())
        custom.native_thread_nodes.assert_not_called()
        original = DefaultHumanInventoryReader.__new__(DefaultHumanInventoryReader)
        original._native_root_read = None
        scoped.reader = original
        self.assertIsNone(scoped.native_thread_nodes())
        factory.assert_not_called()

    def test_root_id_first_last_overlap_and_middle_cover_boundary_counts(self):
        from review_sensei_github.native_review_census import _RootIds

        for count in (99, 100, 101, 199, 200, 201, 250, 300):
            with self.subTest(count=count):
                rows = [
                    {"i": "opaque:" + str(i), "x": {"nodes": [{"n": str(i + 1)}]}}
                    for i in range(count)
                ]
                ids = _RootIds(count, {})

                def page(selected, *, first, end=100):
                    return {
                        "totalCount": count,
                        "nodes": selected,
                        "pageInfo": (
                            {
                                "hasNextPage": end < count,
                                "endCursor": "cursor:" + str(end),
                            }
                            if first
                            else {"hasPreviousPage": count > 100}
                        ),
                    }

                ids.accept(
                    page(rows[:100], first=True, end=min(100, count)),
                    first=True,
                    requested=100,
                )
                ids.accept(page(rows[-100:], first=False), first=False, requested=100)
                if ids.missing:
                    missing = ids.missing
                    ids.accept(
                        page(rows[100 : 100 + missing], first=True, end=100 + missing),
                        first=True,
                        requested=missing,
                    )
                self.assertEqual(ids.missing, 0)
                self.assertEqual(
                    ids.ids, {i + 1: row["i"] for i, row in enumerate(rows)}
                )

    def test_native_root_refreshes_spend_the_original_shared_request_budget(self):
        from review_sensei_github.native_review_census import (
            refresh_native_thread_nodes,
        )
        from review_sensei_github.publication_recovery import publication_http

        from review_sensei.bounded_evidence import EvidenceReadBudget
        from review_sensei.errors import EvidenceBudgetExhausted

        backend = NativeCensusBackend()
        budget = EvidenceReadBudget()
        backend.http = publication_http(backend.http, budget)
        evidence = backend.read()
        initial = len(backend.calls)
        self.assertEqual(budget.calls, initial)
        remaining = budget.ordinary_remaining()
        for _ in range(remaining):
            refresh_native_thread_nodes(
                backend.http,
                token="synthetic",
                repository="owner/repo",
                evidence=evidence,
                app_slug="reviewsensei[bot]",
                producer_id=999,
            )
        self.assertEqual(budget.calls, initial + remaining)
        self.assertEqual(len(backend.calls), budget.calls)
        with self.assertRaises(EvidenceBudgetExhausted):
            refresh_native_thread_nodes(
                backend.http,
                token="synthetic",
                repository="owner/repo",
                evidence=evidence,
                app_slug="reviewsensei[bot]",
                producer_id=999,
            )
        self.assertEqual(len(backend.calls), initial + remaining)
        self.assertEqual(evidence.physical_reads, initial)

    def test_native_root_view_retains_the_budget_that_funded_its_read(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from review_sensei_github.default_human_reader import (
            DefaultHumanInventoryReader,
        )

        from review_sensei.bounded_evidence import EvidenceReadBudget

        reader = DefaultHumanInventoryReader.__new__(DefaultHumanInventoryReader)
        original = EvidenceReadBudget()
        reader.budget = original
        reader.store = SimpleNamespace(
            http=GitHubHttp(),
            token="synthetic",
            context=SimpleNamespace(budget=original),
        )
        reader.repository = "owner/repo"
        reader.app_slug = "reviewsensei[bot]"
        evidence = object()
        with (
            patch("review_sensei_github.native_union.native_union_frame_document"),
            patch(
                "review_sensei_github.native_manifest.NativePublicationReader.for_review",
                return_value=lambda _receipt: evidence,
            ),
        ):
            reader.native_union_reader({"body": "native-frame"})(None)
        # Rebinding both visible references must not manufacture the original
        # operation's budget identity or authorize a fresh closing request.
        reader.budget = reader.store.context.budget = EvidenceReadBudget()
        reader.store.context.producer_id = 999
        with (
            patch(
                "review_sensei_github.native_review_census.refresh_native_thread_nodes"
            ) as refresh,
            self.assertRaisesRegex(ReviewInputError, "original budget changed"),
        ):
            reader.native_thread_nodes()
        refresh.assert_not_called()

    def native_frontier_reader(self):
        from types import SimpleNamespace
        from unittest.mock import Mock

        from review_sensei_github.caller_operation import CallerOperation
        from review_sensei_github.default_human_reader import (
            DefaultHumanInventoryReader,
        )

        from review_sensei.bounded_evidence import EvidenceReadBudget, canonical_bytes

        reader = DefaultHumanInventoryReader.__new__(DefaultHumanInventoryReader)
        budget, evidence, review = (
            EvidenceReadBudget(),
            object(),
            {"body": "native-frame"},
        )
        reader.budget = budget
        reader.store = SimpleNamespace(
            http=GitHubHttp(),
            token="synthetic",
            context=SimpleNamespace(budget=budget),
            transaction_after_read=Mock(),
            revalidate_snapshot=Mock(
                side_effect=ReviewInputError("fresh context required")
            ),
        )
        reader.document, reader.inventory = {}, object()
        reader._owned_review = lambda _review: True
        reader.native_union_reader = lambda _review: None
        reader._native_root_read = (canonical_bytes(review), evidence)
        reader._native_root_read_budget = budget
        caller = SimpleNamespace(
            _block="hosted-review",
            _root=object(),
            _active_operation=CallerOperation(10),
            observations=[],
        )
        reader._native_root_frontier = (
            caller,
            caller._block,
            caller._root,
            caller._active_operation,
            len(caller.observations),
            evidence,
        )
        return reader, caller, review

    def test_native_adjacent_frontier_is_consumed_before_a_failed_proof_check(self):
        from unittest.mock import patch

        reader, caller, review = self.native_frontier_reader()
        with (
            patch(
                "review_sensei_github.caller_operation.caller_for_http",
                return_value=caller,
            ),
            patch(
                "review_sensei_github.native_review_census.require_native_root_evidence",
                side_effect=ReviewInputError("invalid proof"),
            ),
            self.assertRaisesRegex(ReviewInputError, "invalid proof"),
        ):
            reader._original_review(review, [review])
        self.assertIsNone(reader._native_root_frontier)
        reader.store.revalidate_snapshot.assert_not_called()

    def test_native_frontier_rejects_rebound_budget_equal_operation_and_intervening_io(
        self,
    ):
        from unittest.mock import patch

        from review_sensei.bounded_evidence import EvidenceReadBudget

        for changed in ("budget", "operation", "request", "root"):
            reader, caller, review = self.native_frontier_reader()
            if changed == "budget":
                reader.budget = reader.store.context.budget = EvidenceReadBudget()
            elif changed == "operation":
                caller._active_operation = copy.copy(caller._active_operation)
            elif changed == "root":
                # Equality is not identity: a separately installed root must
                # take the ordinary fresh authentication path.
                caller._root = {"same": "value"}
                frontier = list(reader._native_root_frontier)
                frontier[2] = {"same": "value"}
                reader._native_root_frontier = tuple(frontier)
            else:
                caller.observations.append(object())
            with (
                self.subTest(changed=changed),
                patch(
                    "review_sensei_github.caller_operation.caller_for_http",
                    return_value=caller,
                ),
                patch(
                    "review_sensei_github.native_review_census.require_native_root_evidence",
                    side_effect=AssertionError("stale frontier reused"),
                ) as require,
                self.assertRaisesRegex(ReviewInputError, "fresh context required"),
            ):
                reader._original_review(review, [review])
            require.assert_not_called()
            self.assertIsNone(reader._native_root_frontier)

    def test_issued_evidence_does_not_keep_its_transport_or_caller_alive(self):
        import gc
        import tempfile
        import weakref
        from pathlib import Path

        from review_sensei_github.api_run_requests import ApiRunRequestTransport
        from test_api_work_persistence import PLAN, RUN

        def read(directory, metered):
            backend = NativeCensusBackend()
            caller = None
            if metered:
                caller = ApiRunRequestTransport(
                    directory=Path(directory),
                    run=RUN,
                    plan=PLAN,
                    attempt=1,
                    opener=backend.opened,
                )
                caller._quarantine_census_api_url = backend.http.api_url
                backend.http = GitHubHttp(
                    api_url=backend.http.api_url, opener=caller.open
                )
            evidence = backend.read()
            return (
                evidence,
                weakref.ref(backend.http),
                weakref.ref(backend),
                None if caller is None else weakref.ref(caller),
            )

        for metered in (False, True):
            with (
                self.subTest(metered=metered),
                tempfile.TemporaryDirectory() as directory,
            ):
                evidence, transport, backend, caller = read(directory, metered)
                gc.collect()
                self.assertTrue(evidence.root_bodies)
                self.assertIsNone(transport())
                self.assertIsNone(backend())
                if caller is not None:
                    self.assertIsNone(caller())

    def test_root_id_join_covers_every_part_count_and_root_order(self):
        items = NativeCensusBackend().plan.items
        for expected, count in enumerate((0, 1, 32, 48, 64, 80, 96, 160, 250)):
            for reverse in (False, True):
                with self.subTest(parts=expected, reverse=reverse):
                    backend = NativeCensusBackend(items=items[:count], old_count=50)
                    self.assertEqual(len(backend.plan.groups), expected)
                    if reverse:
                        backend.roots.reverse()
                    backend.thread_ids = {
                        int(row["x"]["nodes"][0]["n"]): (
                            "opaque-" + row["x"]["nodes"][0]["n"]
                        ).ljust(2048, "X")
                        for row in backend.roots
                        if int(row["x"]["nodes"][0]["n"]) not in backend.old_nodes
                    }
                    evidence = backend.read()
                    self.assertEqual(
                        dict(evidence.thread_ids),
                        {
                            **backend.old_nodes,
                            **backend.thread_ids,
                        },
                    )
                    self.assertEqual(len(evidence.root_bodies), count + 50)
                    self.assertLessEqual(evidence.physical_reads, 10)

    def test_root_consumer_rejects_foreign_transport_or_credential_before_read(self):
        from review_sensei_github.native_review_census import (
            refresh_native_thread_nodes,
        )

        backend = NativeCensusBackend()
        evidence = backend.read()
        initial = len(backend.calls)
        for http, token in ((backend.http, "foreign"), (GitHubHttp(), "synthetic")):
            with self.subTest(token=token), self.assertRaises(ReviewInputError):
                refresh_native_thread_nodes(
                    http,
                    token=token,
                    repository="owner/repo",
                    evidence=evidence,
                    app_slug="reviewsensei[bot]",
                    producer_id=999,
                )
        self.assertEqual(len(backend.calls), initial)

    def test_three_public_consumers_use_the_complete_maximum_root_view(self):
        from review_sensei_github.native_review_census import (
            refresh_native_thread_nodes,
        )
        from review_sensei_github.publication import (
            ReviewApprovalFinalizer,
            ReviewPublisher,
        )
        from review_sensei_github.thread_resolution import FindingThreadResolver

        backend = NativeCensusBackend(old_count=50)
        for root in backend.roots:
            root["s"] = True
        backend.root = lambda row: {**NativeCensusBackend.root(row), "s": True}
        evidence = backend.read()
        self.assertEqual(len(evidence.root_bodies), 300)
        initial = len(backend.calls)
        scope = dict(
            token="synthetic",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha="b" * 40,
            base_sha="a" * 40,
            app_slug="reviewsensei[bot]",
        )

        def roots(**kwargs):
            return refresh_native_thread_nodes(
                backend.http,
                token="synthetic",
                repository="owner/repo",
                evidence=evidence,
                app_slug="reviewsensei[bot]",
                producer_id=999,
            )

        publisher = ReviewPublisher(http=backend.http)
        publisher.finalizer._native_thread_nodes = roots
        suppression = publisher._published_finding_suppression(**scope)
        self.assertEqual(
            set(suppression.by_instance), {item.instance for item in backend.plan.items}
        )
        finalizer = ReviewApprovalFinalizer(http=backend.http)
        finalizer._native_thread_nodes = roots
        blocking, resolved = finalizer._scan_blocking_threads(**scope)
        self.assertFalse(blocking)
        self.assertEqual(resolved, {item.instance for item in backend.plan.items})
        cleanup = FindingThreadResolver(backend.http).resolve(
            **scope,
            instances=frozenset(item.instance for item in backend.plan.items),
            fence=lambda: self.fail(
                "already resolved roots must not gain mutation authority"
            ),
            skip_resolved=True,
            root_reader=roots,
        )
        self.assertEqual(
            set(cleanup.resolved), {item.instance for item in backend.plan.items}
        )
        self.assertFalse(cleanup.unconfirmed)
        # A successful complete proof is shared only inside this operation;
        # each consumer still performs its own fresh complete closing fence.
        self.assertEqual(len(backend.calls) - initial, 3)
        self.assertEqual(evidence.physical_reads, 10)

    def test_root_consumer_rejects_changed_or_reconstructed_evidence_before_read(self):
        from dataclasses import replace

        from review_sensei_github.native_review_census import (
            refresh_native_thread_nodes,
        )

        backend = NativeCensusBackend()
        evidence = backend.read()
        initial = len(backend.calls)

        def read(value):
            return refresh_native_thread_nodes(
                backend.http,
                token="synthetic",
                repository="owner/repo",
                evidence=value,
                app_slug="reviewsensei[bot]",
                producer_id=999,
            )

        with self.assertRaises(ReviewInputError):
            read(replace(evidence))
        evidence.root_bodies[0]["b"] += " Changed after complete proof."
        with self.assertRaises(ReviewInputError):
            read(evidence)
        self.assertEqual(len(backend.calls), initial)

    def test_root_consumer_closing_rejects_changed_body_version_head_or_main(self):
        from review_sensei_github.native_review_census import (
            refresh_native_thread_nodes,
        )

        for changed in ("version", "head", "main"):
            backend = NativeCensusBackend()
            evidence = backend.read()

            def change(query, response):
                pr = response["data"]["repository"]["p"]
                if changed == "version":
                    pr["f"]["nodes"][0]["x"]["nodes"][0]["u"] = "2026-10-29T00:00:00Z"
                elif changed == "head":
                    pr["h"] = "e" * 40
                else:
                    response["data"]["frames"][0]["b"] += " Changed."

            backend.change = change
            with self.subTest(changed=changed), self.assertRaises(ReviewInputError):
                refresh_native_thread_nodes(
                    backend.http,
                    token="synthetic",
                    repository="owner/repo",
                    evidence=evidence,
                    app_slug="reviewsensei[bot]",
                    producer_id=999,
                )

    def test_root_id_fragment_envelope_covers_full_existing_field_widths(self):
        from review_sensei_github.native_review_census import (
            ROOT_ID_FRAME_BYTES,
            ROOT_ID_ITEM_BYTES,
        )

        frame = {
            "data": {
                "repository": {
                    "n": str(2**63 - 1),
                    "p": {
                        "n": 2**31 - 1,
                        "h": "h" * 40,
                        "b": "b" * 40,
                        "g": {
                            "totalCount": 300,
                            "pageInfo": {"hasNextPage": False, "endCursor": "c" * 2048},
                            "nodes": [],
                        },
                    },
                }
            }
        }
        row = {"i": "i" * 2048, "x": {"nodes": [{"n": str(2**63 - 1)}]}}
        self.assertLessEqual(wire_bytes(frame), ROOT_ID_FRAME_BYTES)
        self.assertLessEqual(wire_bytes(row) + 1, ROOT_ID_ITEM_BYTES)

    def test_complete_union_returns_real_opaque_ids_and_every_body_within_ten_reads(
        self,
    ):
        from review_sensei_github.native_review_census import (
            root_id_schedule_certificate,
        )
        from review_sensei_github.native_review_groups import full_inline_envelope

        backend = NativeCensusBackend(old_count=50)
        backend.thread_ids = {
            int(row["x"]["nodes"][0]["n"]): (
                "opaque-" + row["x"]["nodes"][0]["n"]
            ).ljust(2048, "X")
            for row in backend.roots
            if int(row["x"]["nodes"][0]["n"]) not in backend.old_nodes
        }
        evidence = backend.read()
        expected = {**backend.old_nodes, **backend.thread_ids}
        self.assertEqual(dict(evidence.thread_ids), expected)
        self.assertEqual(len(evidence.root_bodies), len(backend.roots))
        self.assertEqual(evidence.physical_reads, 10)
        self.assertEqual(len(backend.requests), 10)
        bound = full_inline_envelope()
        proof = root_id_schedule_certificate(
            bound["selected_total_bytes"],
            bound["largest_selected_item_bytes"],
            source_bytes=bound["accepted_source_bytes"],
            findings=bound["findings"],
        )
        self.assertGreaterEqual(
            proof["eight_body_id_capacity"] + proof["closing_id_capacity"], 300
        )
        self.assertGreaterEqual(proof["one_capped_body_id_capacity"], 300)
        self.assertGreaterEqual(proof["seven_body_id_capacity"], 74)

    def test_root_id_join_rejects_a_reparented_or_duplicate_opaque_identity(self):
        for duplicate in (False, True):
            backend = NativeCensusBackend()

            def change(query, response):
                pr = response.get("data", {}).get("repository", {}).get("p", {})
                window = pr.get("g")
                if window and len(window["nodes"]) >= 2:
                    if duplicate:
                        window["nodes"][1]["i"] = window["nodes"][0]["i"]
                    else:
                        window["nodes"][0]["x"]["nodes"][0]["n"] = "999999"

            backend.change = change
            with self.subTest(duplicate=duplicate), self.assertRaises(ReviewInputError):
                backend.read()

    def test_opaque_id_closing_query_keeps_full_original_request_widths(self):
        from review_sensei_github.native_review_census import _with_root_ids

        query = _with_root_ids(CLOSE_MIDDLE_QUERY, tail=True)
        variables = {
            "owner": "o" * 256,
            "name": "n" * 256,
            "number": 2**31 - 1,
            "front": 100,
            "back": 100,
            "frames": ["a" * 2048, "b" * 2048],
            "after": "c" * 2048,
            "first": 100,
            "u": 45,
        }
        self.assertTrue(source_query_variables(query, variables))
        self.assertLessEqual(wire_bytes({"query": query, "variables": variables}), 8192)
        variables["u"] = 101
        self.assertFalse(source_query_variables(query, variables))

    def test_all_augmented_packets_fit_maximum_original_variable_widths(self):
        from review_sensei_github.native_review_census import ROOT_ID_QUERIES

        roots = [f"{number:02}" + "x" * 25 for number in range(49)] + ["49" + "x" * 60]
        self.assertEqual(wire_bytes(roots), MAX_EXISTING_ROOT_IDS_BYTES)
        for query, (base, tail) in ROOT_ID_QUERIES.items():
            variables = {
                "owner": "o" * 256,
                "name": "n" * 256,
                "number": 2**31 - 1,
                "u": 100,
            }
            if not tail:
                variables["v"] = "v" * 2048
            if base in {
                PART_QUERY,
                PART_OLD_QUERY,
                PART_ROOTS_QUERY,
                PART_MIDDLE_QUERY,
            }:
                variables["id"] = "i" * 2048
            if base == PART_OLD_QUERY:
                variables["roots"] = roots
            if base in {PART_MIDDLE_QUERY, CLOSE_MIDDLE_QUERY}:
                variables.update(first=100, after="a" * 2048)
            if base in {CLOSE_QUERY, CLOSE_MIDDLE_QUERY}:
                variables.update(front=100, back=100, frames=["f" * 2048, "g" * 2048])
            with self.subTest(base=base):
                self.assertTrue(source_query_variables(query, variables))
                self.assertLessEqual(
                    wire_bytes({"query": query, "variables": variables}), 8192
                )

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
