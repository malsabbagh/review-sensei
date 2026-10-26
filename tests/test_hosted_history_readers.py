"""Public hosted entry points over the captured PR316 history size distribution."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import test_activation_tail as tail_fixture
import test_default_publication_cli_journey as cli_fixture
from fake_github_http import json_response
from review_sensei_github.application import (
    GitHubWriteOptions,
)
from review_sensei_github.errors import (
    GitHubHTTPError,
    GitHubHTTPPaginationLimitError,
    GitHubHTTPResponseTooLargeError,
    GitHubPublicationError,
)
from review_sensei_github.http import GitHubHttp
from review_sensei_github.operation_entry import (
    HostedProviderResult,
    run_actions_analysis,
)
from review_sensei_github.release_reservation import release_own_reservation
from review_sensei_github.session_ledger import render_session_comment
from review_sensei_github.thread_resolution import FindingThreadResolver
from test_publication_recovery import application, checkpoint, publish, recover

from review_sensei.errors import ReviewInputError
from review_sensei.session import SessionIdentity, SessionRecord

IDENTITY = SessionIdentity("owner/repo", 1, repository_id=1)
HEAD = "b" * 40
APP = "review-sensei[bot]"


def captured_shape():
    shape = json.loads(
        (
            Path(__file__).parent / "fixtures/regressions/pr316-history-shape.json"
        ).read_text(encoding="utf-8")
    )
    items = []
    for index, size in enumerate(shape["record_sizes"]):
        item = {
            "id": 100 + index,
            "user": {"id": 99, "login": APP, "type": "Bot"},
            "body": "x" * shape["body_sizes"][index],
            "issue_url": "https://api.github.test/repos/owner/repo/issues/1",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "opaque": {"padding": ""},
        }
        item["opaque"]["padding"] = "m" * (size - encoded_size(item))
        assert encoded_size(item) == size
        items.append(item)
    assert encoded_size(items) == shape["captured_response_bytes"]
    return items


def encoded_size(value):
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode())


def captured_reviews():
    shape = json.loads(
        (
            Path(__file__).parent / "fixtures/regressions/pr316-history-shape.json"
        ).read_text()
    )
    reviews = []
    for i, size in enumerate(shape["review_record_sizes"]):
        item = {
            "id": 4000 + i,
            "user": {"id": 99, "login": APP, "type": "Bot"},
            "commit_id": "c" * 40,
            "state": "COMMENTED",
            "body": "x" * min(60000, size - 1000),
            "opaque": {"padding": ""},
        }
        item["opaque"]["padding"] = "m" * (size - encoded_size(item))
        reviews.append(item)
    assert encoded_size(reviews) == shape["review_response_bytes"]
    return reviews


class HistoryServer:
    def __init__(self, items):
        self.items = items
        self.calls = []
        self.http = GitHubHttp(api_url="https://api.github.test", opener=self.open)

    def open(self, request, timeout):
        url = urlsplit(request.full_url)
        if request.method == "GET" and url.path.endswith("/pulls/1/reviews"):
            query = parse_qs(url.query)
            size, page = (
                int(query.get("per_page", [100])[0]),
                int(query.get("page", [1])[0]),
            )
            self.review_page_reads.append((size, page))
            self.calls.append(("GET", url.path, None))
            return json_response(self.reviews[(page - 1) * size : page * size])
        query = parse_qs(url.query)
        self.calls.append((request.method, url.path, query))
        if request.method != "GET":
            raise AssertionError("read-only history test attempted mutation")
        size = int(query.get("per_page", [100])[0])
        page = int(query.get("page", [1])[0])
        return json_response(self.items[(page - 1) * size : page * size])


class LargeJourneyState(cli_fixture.JourneyState):
    def __init__(self):
        super().__init__()
        self.history = captured_shape()
        self.reviews = []
        self.page_reads = []
        self.review_page_reads = []
        self.session_comment = {
            "id": 600,
            "user": self.app_user,
            "issue_url": "https://api.github.test/repos/owner/repo/issues/1",
            "created_at": "2026-10-20T00:00:00Z",
            "updated_at": "2026-10-20T00:00:00Z",
            "body": render_session_comment(
                repository_id=1,
                pull_request=1,
                record=SessionRecord.create(IDENTITY),
            ),
        }

    def open(self, request, timeout):
        url = urlsplit(request.full_url)
        if request.method == "GET" and url.path.endswith("/issues/1/comments"):
            query = parse_qs(url.query)
            size, page = (
                int(query.get("per_page", [100])[0]),
                int(query.get("page", [1])[0]),
            )
            self.page_reads.append((size, page))
            self.calls.append(("GET", url.path, None))
            items = [*self.history, self.session_comment, *self.parts]
            if "since" in query:
                items = [
                    item
                    for item in items
                    if item.get("updated_at", "9999") >= query["since"][0]
                ]
            return json_response(items[(page - 1) * size : page * size])
        if request.method == "GET" and url.path.endswith("/pulls/1/reviews"):
            query = parse_qs(url.query)
            size, page = int(query["per_page"][0]), int(query["page"][0])
            self.review_page_reads.append((size, page))
            self.calls.append(("GET", url.path, None))
            return json_response(self.reviews[(page - 1) * size : page * size])
        return super().open(request, timeout)


class Broker:
    def __init__(self):
        self.calls = 0

    def open_session(self, *args, **kwargs):
        self.calls += 1
        return SimpleNamespace(token="fixture-token", state="known")

    def open_session_release(self, *args, **kwargs):
        return SimpleNamespace(
            token="fixture-token",
            reservation_owner={"run_id": "10000000001", "head_sha": HEAD},
        )


class CurrentLargeEntryFixture:
    """Construct actual current entry, preserving captured external wire objects."""

    @staticmethod
    def create(directory, items=None, *, limit=512, reviews=None):
        from test_review_run import EntryFixture

        class LargeEntry(EntryFixture):
            def __init__(self):
                super().__init__(directory, limit=limit)
                self.comment_page_reads = []
                self.review_page_reads = []

            def external(self, request, timeout):
                path = urlsplit(request.full_url).path
                query = parse_qs(urlsplit(request.full_url).query)
                if request.method == "GET" and path.endswith("/issues/7/comments"):
                    assert (
                        len(self.accounted.observations)
                        == sum(a == self.attempt for a, _, _ in self.trace) + 1
                    )
                    self.trace.append((self.attempt, request.method, path))
                    size, page = (
                        int(query.get("per_page", [100])[0]),
                        int(query.get("page", [1])[0]),
                    )
                    self.comment_page_reads.append((size, page))
                    rows = ([self.comment] if self.comment else []) + list(
                        self.extra_comments.values()
                    )
                    since = query.get("since", [None])[0]
                    if since:
                        rows = [
                            r
                            for r in rows
                            if not isinstance(r.get("updated_at"), str)
                            or r["updated_at"] >= since
                        ]
                    return json_response(rows[(page - 1) * size : page * size])
                if request.method == "GET" and path.endswith("/pulls/7/reviews"):
                    assert (
                        len(self.accounted.observations)
                        == sum(a == self.attempt for a, _, _ in self.trace) + 1
                    )
                    self.trace.append((self.attempt, request.method, path))
                    size, page = (
                        int(query.get("per_page", [100])[0]),
                        int(query.get("page", [1])[0]),
                    )
                    self.review_page_reads.append((size, page))
                    return json_response(self.reviews[(page - 1) * size : page * size])
                result = super().external(request, timeout)
                if self.comment is not None:
                    self.comment["updated_at"] = "2026-10-26T00:00:00Z"
                return result

        f = LargeEntry()
        for item in items if items is not None else captured_shape():
            f.extra_comments[item["id"]] = {
                **json.loads(json.dumps(item)),
                "issue_url": "https://api.github.test/repos/owner/repo/issues/7",
            }
        f.reviews = json.loads(json.dumps(reviews or []))
        return f


def current_history_fixture(directory, items=None):
    """Real current caller ledger with synthetic large native history."""
    f = tail_fixture.CurrentHostedFixture(directory)
    raw = f.caller._opener
    f.urls = []
    f.service.comment["updated_at"] = "2026-10-26T00:00:00Z"

    def recorded(request, timeout):
        f.urls.append((request.method, request.full_url))
        path = urlsplit(request.full_url).path
        if request.method == "GET" and path.endswith("/issues/7/comments"):
            assert (
                len(f.caller.observations)
                == sum(a == f.service.attempt for a, _, _ in f.service.trace) + 1
            )
            f.service.trace.append((f.service.attempt, request.method, path))
            query = parse_qs(urlsplit(request.full_url).query)
            size, page = (
                int(query.get("per_page", [100])[0]),
                int(query.get("page", [1])[0]),
            )
            rows = [f.service.comment] + list(f.service.extra_comments.values())
            since = query.get("since", [None])[0]
            if since:
                rows = [
                    r
                    for r in rows
                    if not isinstance(r.get("updated_at"), str)
                    or r["updated_at"] >= since
                ]
            return json_response(rows[(page - 1) * size : page * size])
        result = raw(request, timeout)
        f.service.comment["updated_at"] = "2026-10-26T00:00:00Z"
        return result

    f.caller._opener = recorded
    for item in items if items is not None else captured_shape():
        item = {**item, "id": item["id"] + 5000}
        f.service.extra_comments[item["id"]] = item
    return f


class HostedHistoryReaderTests(unittest.TestCase):
    def test_current_review_entry_keeps_77_findings_and_result_with_both_large_histories(
        self,
    ):
        from review_sensei_github.session_ledger import parse_session_comment
        from test_api_work_persistence import IDENTITY as CURRENT_IDENTITY

        for extra_review in (False, True):
            with (
                self.subTest(near_capacity=extra_review),
                tempfile.TemporaryDirectory() as directory,
            ):
                reviews = captured_reviews()
                if extra_review:
                    reviews.append({**reviews[0], "id": 4999, "body": "x" * 60000})
                    self.assertGreater(encoded_size(reviews), 512 * 1024)
                f = CurrentLargeEntryFixture.create(directory, reviews=reviews)
                original_history = copy.deepcopy(f.extra_comments)
                f.provider_comments = [
                    {
                        "path": "src/app.py",
                        "line": 2,
                        "side": "RIGHT",
                        "body": f"Nonblocking observation {i}.",
                        "severity": "low",
                        "blocking": False,
                        "needs_human": False,
                    }
                    for i in range(77)
                ]
                self.assertEqual(f.invoke(), 0, f.error)
                current = parse_session_comment(
                    f.comment["body"], identity=CURRENT_IDENTITY
                )
                self.assertEqual(current.transaction.phase, "publication_succeeded")
                from review_sensei_github.publication import parse_finding_index

                from review_sensei.human_assessment import finding_instance_fingerprint
                from review_sensei.models import ReviewResult

                result = ReviewResult.from_dict(
                    json.loads((f.root / "review.json").read_text())
                )
                indexes = [
                    index
                    for r in f.reviews
                    if (
                        index := parse_finding_index(
                            r["body"], repository_id=123, pull_request=7, head_sha=HEAD
                        )
                    )
                    is not None
                ]
                self.assertEqual(len(result.comments), 77)
                self.assertEqual(len(indexes), 1)
                self.assertEqual(len(indexes[0].entries), 77)
                self.assertEqual(
                    {entry.instance for entry in indexes[0].entries},
                    {finding_instance_fingerprint(c) for c in result.comments},
                )
                complete_text = "\n".join(r["body"] for r in f.reviews) + "\n".join(
                    c["body"] for c in f.inline
                )
                for comment in result.comments:
                    self.assertIn(comment.body, complete_text)
                self.assertNotIn("omitted for size", complete_text)
                self.assertFalse(
                    any(
                        entry.blocking or entry.needs_human
                        for entry in indexes[0].entries
                    )
                )
                self.assertEqual(f.provider_calls, 1)
                self.assertEqual(sum(r["state"] == "APPROVED" for r in f.reviews), 1)
                self.assertTrue(any(size < 100 for size, _ in f.comment_page_reads))
                if extra_review:
                    self.assertTrue(any(size < 100 for size, _ in f.review_page_reads))
                for key, value in original_history.items():
                    self.assertEqual(f.extra_comments[key], value)
                self.assertEqual(len(f.accounted.observations), len(f.trace))

    def test_current_native_checkpoint_binds_adaptive_pages_and_original_accounting(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            f = current_history_fixture(directory)
            before = len(f.service.trace)
            saved = f.checkpoint(1)
            self.assertTrue(saved.operator_paused)
            self.assertGreater(len(f.service.trace) - before, 4)
            self.assertTrue(any("per_page=20" in url for _, url in f.urls))
            self.assertEqual(
                f.caller.accounting.confirmed, len(f.service.trace) - before
            )
            self.assertEqual(f.caller.accounting.uncertain, 0)
            f.assert_physical_accounting(self)

    def server(self):
        items = captured_shape()
        # Replace one filler by a real current ledger. All other metadata,
        # including the much larger original-sized history entries, survives.
        items[0]["body"] = render_session_comment(
            repository_id=1,
            pull_request=1,
            record=SessionRecord.create(IDENTITY),
        )
        return HistoryServer(items)

    def test_current_startup_resolves_native_history_after_v3_scope(self):
        from review_sensei_github.api_run_requests import ApiRunSessionLedger

        with tempfile.TemporaryDirectory() as directory:
            f = CurrentLargeEntryFixture.create(directory)

            def observe():
                self.assertIsInstance(f.selected.ledger, ApiRunSessionLedger)
                self.assertEqual(f.selected.ledger.authenticated_app_id, 999)
                self.assertEqual(f.selected.session_authority["version"], 3)

            f.before_provider = observe
            self.assertEqual(f.invoke(), 0, f.error)
            self.assertEqual(f.provider_calls, 1)
            self.assertTrue(any(size < 100 for size, _ in f.comment_page_reads))
            self.assertEqual(len(f.accounted.observations), len(f.trace))

    def test_separate_cleanup_refuses_before_broker_or_history_http(self):
        server, broker = self.server(), Broker()
        with self.assertRaisesRegex(
            ReviewInputError, "separate reservation cleanup is retired"
        ):
            release_own_reservation(
                broker=broker,
                http=server.http,
                oidc_token="fixture-oidc",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha=HEAD,
                run_id="10000000001",
            )
        self.assertEqual(server.calls, [])
        self.assertEqual(broker.calls, 0)

    def test_default_list_reader_returns_every_full_captured_shape_object(self):
        items = captured_shape()
        server = HistoryServer(items)
        result = server.http.paginate(path="/history", token="fixture-token")
        self.assertEqual(result, items)
        self.assertLessEqual(len(server.calls), 10)

    def test_thread_reconciliation_discovers_large_roots_before_any_mutation(self):
        nodes = [
            {
                "id": str(i),
                "isResolved": False,
                "comments": {
                    "nodes": [
                        {
                            "databaseId": i + 1,
                            "body": "x" * 8000,
                            "author": {"login": "another-app", "__typename": "Bot"},
                        }
                    ]
                },
            }
            for i in range(100)
        ]
        calls = []

        def opener(request, timeout):
            body = json.loads(request.data)
            self.assertEqual(body["operationName"], "FindingResolutionThreads")
            variables = body["variables"]
            first, after = variables.get("first", 100), int(variables["after"] or 0)
            selected = nodes[after : after + first]
            calls.append((first, after))
            return json_response(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "nodes": selected,
                                    "pageInfo": {
                                        "hasNextPage": after + len(selected)
                                        < len(nodes),
                                        "endCursor": str(after + len(selected)),
                                    },
                                }
                            }
                        }
                    }
                }
            )

        result = FindingThreadResolver(
            GitHubHttp(api_url="https://api.github.test", opener=opener)
        ).resolve(
            token="fixture",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha=HEAD,
            base_sha="a" * 40,
            app_slug=APP,
            instances=frozenset({"c" * 64}),
            fence=lambda: True,
        )
        self.assertEqual(result.resolved, ())
        self.assertEqual(calls, [(100, 0), (50, 0), (50, 50)])

    def test_library_large_history_crosses_operation_checkpoint_publication_and_replay(
        self,
    ):
        self.journey(captured_shape(), findings=77)
        template = captured_shape()[0]
        for count in (100, 250):
            with self.subTest(history=count):
                self.journey(
                    [
                        {
                            **template,
                            "id": 1000 + i,
                            "body": f"History {i}: " + "x" * 1200,
                            "opaque": {"retained": i},
                        }
                        for i in range(count)
                    ],
                    recover_budget=count == 250,
                )

    def journey(self, history, *, recover_budget=False, findings=3):
        state = LargeJourneyState()
        state.history = history
        original_history = json.loads(json.dumps(history))
        from review_sensei_github.session_ledger import GitHubIssueCommentSessionLedger

        ledger = GitHubIssueCommentSessionLedger(
            state.http, token="fixture-token", app_slug=APP
        )
        result, key = checkpoint(ledger, count=findings)
        payload = json.dumps(result.to_dict(), sort_keys=True).encode()
        executions = []

        def execute():
            executions.append(1)
            return payload

        arguments = dict(
            http=state.http,
            token="fixture",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            run_id="10000000001",
            run_attempt="1",
            app_id=99,
            reservation_id="fixture-analysis",
            attestation_digest="a" * 64,
            execute=execute,
            encode=lambda value: value,
            delivery_id="fixture-analysis",
            grant="fixture-analysis",
        )
        admitted = run_actions_analysis(**arguments)
        self.assertIsInstance(admitted, HostedProviderResult)
        self.assertEqual(executions, [1])
        app = application(state, ledger)
        options = GitHubWriteOptions(
            auto_review=True,
            github_writes=True,
            complete_inventory=True,
        )
        try:
            outcome = publish(app, result, key, options=options)
        except ReviewInputError as exc:
            if not recover_budget:
                raise
            self.assertIn("shared request budget", str(exc))
            self.assertEqual(
                ledger.load(IDENTITY).record.transaction.phase, "publication_failed"
            )
            # A fresh publication invocation recovers the exact retained result;
            # it cannot run the provider again or duplicate an accepted review.
            app = application(state, ledger)
            restored = recover(app, ledger)
            self.assertEqual(restored.content_digest(), result.content_digest())
            outcome = publish(app, restored, key, options=options)
        self.assertIn(outcome.status, {"published", "already_published"})
        loaded = ledger.load(IDENTITY)
        self.assertEqual(loaded.record.transaction.phase, "publication_succeeded")
        self.assertIsNone(loaded.record.reservation_id)
        # Terminal library publication proves no reservation survives without
        # invoking a separate hosted cleanup process.
        replay = run_actions_analysis(**arguments)
        self.assertIsInstance(replay, HostedProviderResult)
        self.assertEqual(executions, [1])
        self.assertEqual(sum(r["state"] == "APPROVED" for r in state.reviews), 1)
        self.assertEqual(state.history, original_history)

    def test_250_large_history_operation_replay_keeps_single_execution(self):
        state = LargeJourneyState()
        template = captured_shape()[0]
        state.history = [
            {**template, "id": 1000 + i, "body": "x" * 8000, "opaque": {"keep": i}}
            for i in range(250)
        ]
        executions = []
        arguments = dict(
            http=state.http,
            token="fixture",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            run_id="10000000001",
            run_attempt="1",
            app_id=99,
            reservation_id="fixture-analysis",
            attestation_digest="a" * 64,
            execute=lambda: executions.append(1) or b'{"accepted":true}',
            encode=lambda value: value,
            delivery_id="fixture-analysis",
            grant="fixture-analysis",
        )
        self.assertIsInstance(run_actions_analysis(**arguments), HostedProviderResult)
        self.assertIsInstance(run_actions_analysis(**arguments), HostedProviderResult)
        self.assertEqual(executions, [1])

    def test_default_reader_refuses_oversized_individual_and_exhausted_walk(self):
        for items, exception, attempts in (
            ([{"body": "x" * 600_000}], GitHubHTTPResponseTooLargeError, 6),
            ([{"body": "x" * 100_000}] * 250, GitHubHTTPPaginationLimitError, 10),
        ):
            with self.subTest(exception=exception.__name__):
                server = HistoryServer(items)
                with self.assertRaises(exception):
                    server.http.paginate(path="/history", token="fixture")
                self.assertEqual(len(server.calls), attempts)
                self.assertTrue(all(method == "GET" for method, *_ in server.calls))

    def test_default_reader_does_not_return_partial_inventory_on_late_failure(self):
        for failure in ("invalid", "denied", "cancelled"):
            with self.subTest(failure=failure):
                calls = []

                def open_page(request, timeout):
                    calls.append(request.full_url)
                    if len(calls) == 1:
                        return json_response([{"id": i} for i in range(100)])
                    if failure == "cancelled":
                        raise KeyboardInterrupt
                    return json_response(
                        {"message": "unavailable"}, 403 if failure == "denied" else 200
                    )

                http = GitHubHttp(opener=open_page)
                with self.assertRaises(
                    KeyboardInterrupt if failure == "cancelled" else GitHubHTTPError
                ):
                    http.paginate(path="/history", token="fixture")
                self.assertEqual(len(calls), 2)

    def test_thread_reader_refuses_oversized_items_budget_and_invalid_pages_before_mutation(
        self,
    ):
        for kind, attempts in (
            ("oversized", 5),
            ("budget", 10),
            ("too-many", 1),
            ("denied", 1),
            ("cursor", 2),
        ):
            with self.subTest(kind=kind):
                calls = []

                def opener(request, timeout):
                    body = json.loads(request.data)
                    self.assertTrue(body["query"].lstrip().startswith("query "))
                    variables = body["variables"]
                    calls.append(variables)
                    node = {
                        "id": "thread",
                        "isResolved": False,
                        "comments": {
                            "nodes": [
                                {
                                    "databaseId": 1,
                                    "body": "x"
                                    * (600_000 if kind == "oversized" else 1),
                                    "author": {
                                        "login": "another-app",
                                        "__typename": "Bot",
                                    },
                                }
                            ]
                        },
                    }
                    return json_response(
                        {
                            "data": {
                                "repository": {
                                    "pullRequest": {
                                        "reviewThreads": {
                                            "nodes": [node]
                                            * (101 if kind == "too-many" else 1),
                                            "pageInfo": {
                                                "hasNextPage": kind != "too-many",
                                                "endCursor": "same"
                                                if kind == "cursor"
                                                else str(len(calls)),
                                            },
                                        }
                                    }
                                }
                            }
                        },
                        403 if kind == "denied" else 200,
                    )

                with self.assertRaises((GitHubHTTPError, GitHubPublicationError)):
                    FindingThreadResolver(GitHubHttp(opener=opener)).resolve(
                        token="fixture",
                        repository="owner/repo",
                        repository_id=1,
                        pull_request=1,
                        head_sha=HEAD,
                        base_sha="a" * 40,
                        app_slug=APP,
                        instances=frozenset({"c" * 64}),
                        fence=lambda: True,
                    )
                self.assertEqual(len(calls), attempts)
                if kind == "oversized":
                    self.assertEqual([v["first"] for v in calls], [100, 50, 25, 5, 1])
                    self.assertEqual({v["after"] for v in calls}, {None})

    def test_current_native_history_growth_exhausts_finite_scan_before_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            f = current_history_fixture(directory)
            first = next(iter(f.service.extra_comments.values()))
            first["body"] = "x" * 600000
            before = len(f.service.trace)
            with self.assertRaises(ReviewInputError):
                f.checkpoint()
            used = len(f.service.trace)
            self.assertLessEqual(used - before, 7)
            self.assertTrue(all(m == "GET" for _, m, _ in f.service.trace[before:]))
            self.assertTrue(any("per_page=1" in url for _, url in f.urls))
            with self.assertRaisesRegex(ReviewInputError, "cannot be replenished"):
                f.checkpoint()
            self.assertEqual(len(f.service.trace), used)
            f.assert_physical_accounting(self)

    def test_current_remote_ledger_reaches_single_item_pages_without_refilling_allowance(
        self,
    ):
        items = [
            {**item, "opaque": {"padding": "m" * 300000}}
            for item in captured_shape()[:2]
        ]
        with tempfile.TemporaryDirectory() as directory:
            f = current_history_fixture(directory, items)
            self.assertEqual(f.ledger.load(f.identity).status, "ok")
            self.assertTrue(any("per_page=1&" in url for _, url in f.urls))
            self.assertEqual(f.caller.accounting.confirmed, len(f.urls))
            f.assert_physical_accounting(self)

    def test_history_reader_keeps_medium_page_capacity_and_exact_unaligned_offsets(
        self,
    ):
        for width in (16_000, 20_000):
            with self.subTest(body_bytes=width):
                items = [{"id": i, "body": "x" * width} for i in range(100)]
                server = HistoryServer(items)
                self.assertEqual(
                    server.http.paginate(path="/history", token="fixture"), items
                )
                self.assertLessEqual(len(server.calls), 10)
        items = [
            {"id": i, "body": "small" if i < 25 else "x" * 40_000} for i in range(50)
        ]
        server = HistoryServer(items)
        self.assertEqual(server.http.paginate(path="/history", token="fixture"), items)
        self.assertEqual(
            [(int(q["per_page"][0]), int(q["page"][0])) for _, _, q in server.calls],
            [(100, 1), (50, 1), (25, 1), (25, 2), (5, 6), (10, 4), (10, 5), (50, 2)],
        )
