"""Public hosted entry points over the captured PR316 history size distribution."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from review_sensei_github.application import (
    GitHubWriteOptions,
    resolve_hosted_session_ledger,
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

from review_sensei.errors import ReviewInputError
from review_sensei.session import SessionIdentity, SessionRecord
from tests import test_activation_tail as tail_fixture
from tests import test_default_publication_cli_journey as cli_fixture
from tests.fake_github_http import json_response
from tests.test_publication_recovery import application, checkpoint, publish, recover

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


class HostedHistoryReaderTests(unittest.TestCase):
    def test_actual_cli_keeps_77_findings_and_accepted_operation_with_both_large_histories(
        self,
    ):
        for extra_review in (False, True):
            with self.subTest(near_capacity=extra_review):
                case = cli_fixture.DefaultPublicationCliJourneyTests()
                state = LargeJourneyState()
                state.reviews = captured_reviews()
                if extra_review:
                    state.reviews.append(
                        {**state.reviews[0], "id": 4999, "body": "x" * 60_000}
                    )
                    self.assertGreater(encoded_size(state.reviews), 512 * 1024)
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    result, _ = case.setup_files(root)
                    case.configuration(root, "complete")
                    case.accept_analysis(state, result)
                    code, output, error = case.invoke(root, state, actions=True)
                    self.assertEqual(code, 0, (output, error))
                    case.assert_complete_publication(state, result)
                    if extra_review:
                        self.assertTrue(
                            any(size < 100 for size, _ in state.review_page_reads)
                        )

    def test_sealed_tail_binds_smaller_fence_pages_and_original_accounting(self):
        case = tail_fixture.HostedTailTests()
        state, broker, ledger = case.setup_host()
        for item in captured_shape():
            item = {
                **item,
                "id": item["id"] + 5000,
                "created_at": "2020-01-01T00:00:01Z",
                "updated_at": "2020-01-01T00:00:01Z",
            }
            state.comments[item["id"]] = item
        case.reserve(ledger)
        case.fresh(broker, ledger, pages=2)
        saved = case.activate(ledger, pages=2)
        self.assertIsNotNone(saved.assessment_queue)
        self.assertEqual(
            ledger.evidence_budget.calls, len(state.calls) + len(broker.calls)
        )
        self.assertEqual(
            saved.assessment_queue["active_operation"]["read_accounting"]["calls"],
            ledger.evidence_budget.calls,
        )
        self.assertLessEqual(ledger.evidence_budget.calls, 64)
        self.assertTrue(
            any("per_page=20" in url and "since=" in url for _, url in state.calls)
        )

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

    def test_startup_resolves_real_ledger_after_broker_accepts_large_history(self):
        server, broker = self.server(), Broker()
        ledger = resolve_hosted_session_ledger(
            broker=broker,
            http=server.http,
            oidc_token="fixture-oidc",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha=HEAD,
            app_slug=APP,
        )
        self.assertEqual(broker.calls, 1)
        loaded = ledger.load(IDENTITY)
        self.assertEqual(loaded.status, "ok")
        self.assertEqual(loaded.record.generation, 0)
        self.assertTrue(all(method == "GET" for method, *_ in server.calls))

    def test_cleanup_discovers_no_reservation_without_mutation(self):
        server = self.server()
        outcome = release_own_reservation(
            broker=Broker(),
            http=server.http,
            oidc_token="fixture-oidc",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha=HEAD,
            run_id="10000000001",
        )
        self.assertEqual(outcome, "no-reservation")
        self.assertTrue(all(method == "GET" for method, *_ in server.calls))

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

    def test_large_history_crosses_startup_operation_checkpoint_publication_and_replay(
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
        ledger = resolve_hosted_session_ledger(
            broker=Broker(),
            http=state.http,
            oidc_token="fixture-oidc",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha=HEAD,
            app_slug=APP,
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
        self.assertEqual(
            release_own_reservation(
                broker=Broker(),
                http=state.http,
                oidc_token="fixture-oidc",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha=HEAD,
                run_id="10000000001",
            ),
            "no-reservation",
        )
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
            ([{"body": "x" * 600_000}], GitHubHTTPResponseTooLargeError, 5),
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

    def test_sealed_tail_growth_refuses_without_unplanned_smaller_reads_or_activation(
        self,
    ):
        case = tail_fixture.HostedTailTests()
        state, broker, ledger = case.setup_host()
        history = captured_shape()
        for item in history:
            item = {
                **item,
                "id": item["id"] + 5000,
                "created_at": "2020-01-01T00:00:01Z",
                "updated_at": "2020-01-01T00:00:01Z",
            }
            state.comments[item["id"]] = item
        case.reserve(ledger)
        case.fresh(broker, ledger, pages=2)
        issue = ledger.evidence_budget.reserve_tail
        tickets = []

        def grow_after_seal(plan):
            ticket = issue(plan)
            tickets.append(ticket)
            state.comments[history[0]["id"] + 5000]["body"] = "x" * 600_000
            return ticket

        start = len(state.calls)
        with patch.object(
            ledger.evidence_budget, "reserve_tail", side_effect=grow_after_seal
        ):
            with self.assertRaises(GitHubPublicationError):
                case.activate(ledger, pages=2)
        self.assertEqual(len(tickets), 1)
        tail_calls = state.calls[start:]
        self.assertTrue(all(method != "PATCH" for method, _ in tail_calls))
        self.assertFalse(
            any("per_page=5&" in url or "per_page=1&" in url for _, url in tail_calls)
        )
        self.assertIsNone(ledger._activation_ticket)

    def test_remote_ledger_reaches_single_item_pages_without_refilling_allowance(self):
        case = tail_fixture.HostedTailTests()
        state, broker, ledger = case.setup_host()
        for item in captured_shape()[:2]:
            item = {
                **item,
                "id": item["id"] + 5000,
                "opaque": {"padding": "m" * 300_000},
            }
            state.comments[item["id"]] = item
        self.assertEqual(
            ledger.load(
                tail_fixture.fixture.IDENTITY, now=tail_fixture.fixture.NOW
            ).status,
            "ok",
        )
        self.assertTrue(any("per_page=1&" in url for _, url in state.calls))
        self.assertEqual(
            ledger.evidence_budget.calls, len(state.calls) + len(broker.calls)
        )
        self.assertLessEqual(ledger.evidence_budget.calls, 64)
