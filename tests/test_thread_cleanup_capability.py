"""Separate, lazy cleanup authority; all external inputs are synthetic."""

import json
import tempfile
import unittest
from pathlib import Path

from review_sensei_github.application import GitHubApplication, GitHubWriteOptions
from review_sensei_github.broker_client import BrokerClient
from review_sensei_github.broker_control import BrokerDispatchAccounting
from review_sensei_github.errors import GitHubBrokerClientError
from review_sensei_github.http import GitHubHttp
from review_sensei_github.operation_diagnostics import OperationObserver
from review_sensei_github.publication import ReviewPublisher
from review_sensei_github.publication_recovery import publication_http
from review_sensei_github.thread_resolution import FindingThreadResolver

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.errors import ReviewInputError
from review_sensei.session import LocalSessionLedger
from tests.fake_github_http import json_response
from tests.test_command_capacity_recovery import (
    APP,
    BASE,
    HEAD,
    CommandCapacityState,
)
from tests.test_github_broker_client import FakeResponse


class CleanupCapabilityTests(unittest.TestCase):
    def resolve(
        self,
        *,
        acquire=None,
        fence=None,
        resolved=(),
        rewrite=None,
        observer=None,
        budget=None,
        instances=None,
    ):
        state = CommandCapacityState(
            schema="1", inline_range=(0, 3), human_count=3, padded=False
        )
        state.resolved_threads = set(resolved)
        calls, acquisitions = [], []

        def provider():
            acquisitions.append(len(calls))
            return acquire(state) if acquire else "synthetic-cleanup"

        def opener(request, timeout):
            body = json.loads(request.data) if request.data else None
            calls.append(
                (
                    request.method,
                    request.full_url,
                    body,
                    request.get_header("Authorization"),
                )
            )
            if rewrite:
                response = rewrite(state, request, body)
                if response is not None:
                    return json_response(*response)
            return state.open(request, timeout)

        http = GitHubHttp(api_url="https://api.github.test", opener=opener)
        if budget is not None:
            http = publication_http(http, budget)
        outcome = FindingThreadResolver(http).resolve(
            token="synthetic-review",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha=HEAD,
            base_sha=BASE,
            app_slug=APP,
            instances=frozenset(e.instance for e in state.entries[:3])
            if instances is None
            else instances,
            fence=(lambda: fence(state)) if fence else lambda: True,
            skip_resolved=True,
            cleanup_token_provider=provider,
            observer=observer,
        )
        return state, outcome, calls, acquisitions

    def test_one_lazy_acquisition_and_only_mutation_uses_cleanup_token(self):
        state, result, calls, acquired = self.resolve()
        self.assertFalse(result.unconfirmed)
        self.assertEqual(state.resolved_threads, {100, 101, 102})
        self.assertEqual(len(acquired), 1)
        self.assertGreater(acquired[0], 1)  # Discovery and authenticated root first.
        for _, _, body, auth in calls:
            self.assertEqual(
                auth,
                "Bearer synthetic-cleanup"
                if body and body.get("operationName") == "ResolveReviewThread"
                else "Bearer synthetic-review",
            )

    def test_no_issuance_for_resolved_foreign_or_stale_candidates(self):
        def foreign(state, request, body):
            if body and body.get("operationName") == "FindingResolutionThreads":
                # Obtain a fresh synthetic discovery response, alter only ownership.
                response = state.open(request, 30)
                status, payload = response.status, json.loads(response.read(512 * 1024))
                nodes = payload["data"]["repository"]["pullRequest"]["reviewThreads"][
                    "nodes"
                ]
                for node in nodes:
                    node["comments"]["nodes"][0]["author"] = {
                        "login": "foreign",
                        "__typename": "Bot",
                    }
                return payload, status

        for kwargs in (
            {"resolved": (100, 101, 102)},
            {"fence": lambda state: False},
            {"rewrite": foreign},
        ):
            with self.subTest(kwargs=tuple(kwargs)):
                _, _, calls, acquired = self.resolve(**kwargs)
                self.assertEqual(acquired, [])
                self.assertFalse(
                    any(
                        body and body.get("operationName") == "ResolveReviewThread"
                        for _, _, body, _ in calls
                    )
                )

    def test_no_issuance_or_discovery_for_no_decided_instances(self):
        _, result, calls, issued = self.resolve(instances=frozenset())
        self.assertFalse(result.unconfirmed)
        self.assertEqual((calls, issued), ([], []))

    def test_original_deadline_or_call_exhaustion_during_acquisition_refuses_mutation(
        self,
    ):
        for exhaustion in ("deadline", "calls"):
            clock = [0.0]
            budget = EvidenceReadBudget(clock=lambda: clock[0])
            deadline = budget.wall_deadline_ms

            def acquire(state):
                if exhaustion == "deadline":
                    clock[0] = 60.0
                else:
                    while budget.calls < 60:
                        budget.consume()
                return "synthetic-cleanup"

            _, result, calls, issued = self.resolve(acquire=acquire, budget=budget)
            self.assertEqual(len(issued), 1)
            self.assertEqual(len(result.unconfirmed), 3)
            self.assertEqual(budget.wall_deadline_ms, deadline)
            self.assertLessEqual(budget.calls, 64)
            self.assertFalse(
                any(
                    b and b.get("operationName") == "ResolveReviewThread"
                    for _, _, b, _ in calls
                )
            )

    def test_issuance_failure_stops_without_fallback_or_repeated_acquisition(self):
        for fault in ("refused", "empty", "budget"):

            def acquire(state):
                if fault == "refused":
                    raise GitHubBrokerClientError("synthetic refusal")
                if fault == "budget":
                    raise ReviewInputError("partition shared request budget exhausted")
                return ""

            with self.subTest(fault=fault):
                state, result, calls, acquired = self.resolve(acquire=acquire)
                self.assertEqual(len(acquired), 1)
                self.assertEqual(len(result.unconfirmed), 3)
                self.assertFalse(state.resolved_threads)
                self.assertFalse(
                    any(
                        body and body.get("operationName") == "ResolveReviewThread"
                        for _, _, body, _ in calls
                    )
                )

    def test_authority_and_root_rechecked_after_acquisition(self):
        for change in ("authority", "root"):
            changed = [False]

            def acquire(state):
                changed[0] = True
                return "synthetic-cleanup"

            def rewrite(state, request, body):
                if (
                    change == "root"
                    and changed[0]
                    and "/pulls/comments/" in request.full_url
                ):
                    return {"body": "edited"}, 200

            with self.subTest(change=change):
                state, result, calls, acquired = self.resolve(
                    acquire=acquire,
                    fence=lambda state: (
                        not changed[0] if change == "authority" else True
                    ),
                    rewrite=rewrite,
                )
                self.assertEqual(len(acquired), 1)
                self.assertFalse(state.resolved_threads)
                self.assertEqual(len(result.unconfirmed), 3)

    def test_denial_and_uncertain_readback_use_cleanup_token_and_stop(self):
        from review_sensei_github.errors import GitHubHTTPTransientError

        for uncertain in (False, True):

            def rewrite(state, request, body):
                op = body.get("operationName") if body else None
                if op == "ResolveReviewThread":
                    if uncertain:
                        raise GitHubHTTPTransientError("synthetic lost response")
                    return {"errors": [{"type": "FORBIDDEN"}]}, 200
                if op == "ResolutionReadback":
                    return {"errors": [{"type": "FORBIDDEN"}]}, 200

            with self.subTest(uncertain=uncertain):
                _, result, calls, _ = self.resolve(rewrite=rewrite)
                self.assertEqual(len(result.unconfirmed), 3)
                operations = [
                    (b["operationName"], auth)
                    for _, _, b, auth in calls
                    if b
                    and b.get("operationName")
                    in {"ResolveReviewThread", "ResolutionReadback"}
                ]
                self.assertEqual(
                    [op for op, _ in operations],
                    ["ResolveReviewThread", "ResolutionReadback"]
                    if uncertain
                    else ["ResolveReviewThread"],
                )
                self.assertTrue(
                    all(auth == "Bearer synthetic-cleanup" for _, auth in operations)
                )

    def test_post_acquisition_root_failure_is_attributed_to_revalidation(self):
        issued = [False]
        observer = OperationObserver()

        def acquire(state):
            issued[0] = True
            return "synthetic-cleanup"

        def rewrite(state, request, body):
            if issued[0] and "/pulls/comments/" in request.full_url:
                return {"body": "edited"}, 403

        _, result, _, _ = self.resolve(
            acquire=acquire, rewrite=rewrite, observer=observer
        )
        self.assertEqual(len(result.unconfirmed), 3)
        self.assertEqual(observer.report().cleanup.first.stage, "cleanup.root-readback")


class CleanupBrokerResponseTests(unittest.TestCase):
    def test_new_capability_requires_matching_response_label(self):
        for label in ("thread_resolve", "review_publish", None):

            def opener(request, timeout):
                self.assertEqual(
                    json.loads(request.data),
                    {"oidc_token": "synthetic", "capability": "thread_resolve"},
                )
                return FakeResponse(
                    json.dumps(
                        {"token": "synthetic-cleanup", "capability": label}
                    ).encode()
                )

            client = BrokerClient(opener=opener)
            if label == "thread_resolve":
                self.assertEqual(
                    client.exchange("synthetic", capability=label), "synthetic-cleanup"
                )
            else:
                with self.assertRaises(GitHubBrokerClientError):
                    client.exchange("synthetic", capability="thread_resolve")


class CleanupCommandCompositionTests(unittest.TestCase):
    def test_missing_or_mismatched_original_broker_accounting_never_issues_cleanup(
        self,
    ):
        from types import SimpleNamespace

        from review_sensei.disposition import MaintainerCommandResult

        for mismatch in (False, True):
            budget = EvidenceReadBudget()
            requested = []

            def exchange(request, timeout):
                body = json.loads(request.data)
                requested.append(body["capability"])
                return FakeResponse(
                    json.dumps(
                        {
                            "token": "synthetic",
                            "capability": body["capability"],
                            **(
                                {
                                    "control_receipt": {
                                        **body["control_budget"],
                                        "dispatched": 1,
                                    }
                                }
                                if "control_budget" in body
                                else {}
                            ),
                        }
                    ).encode()
                )

            broker = BrokerClient(
                opener=exchange,
                control=BrokerDispatchAccounting(EvidenceReadBudget(), "e" * 64, 10, 1)
                if mismatch
                else None,
            )

            def refinalize(**kwargs):
                kwargs["thread_cleanup_token_provider"](budget)
                self.fail("unaccounted cleanup acquired authority")

            reviewer = SimpleNamespace(
                finalizer=SimpleNamespace(refinalize_with_dispositions=refinalize)
            )
            app = GitHubApplication(
                broker=broker, http=None, reviewer=reviewer, learner=None, replier=None
            )
            result = app._refinalize_command(
                MaintainerCommandResult(
                    action="verify",
                    applied=True,
                    operator_paused=False,
                    summary="decisions retained",
                ),
                oidc_token="synthetic",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha=HEAD,
                app_slug=APP,
                actor="alice",
                record=None,
            )
            self.assertEqual(requested, ["review_publish", "check_publish"])
            self.assertTrue(result.evaluation_failed)
            self.assertIn("decisions retained", result.summary)

    def test_larger_ledger_makes_bounded_progress_without_renewing_a_request(self):
        state = CommandCapacityState(
            complete=True,
            schema="3",
            inline_range=(0, 21),
            human_count=21,
            padded=False,
        )
        state.resolved_threads.clear()
        progress = []
        with tempfile.TemporaryDirectory() as directory:
            state.record(directory)
            for attempt in range(25):
                budget = EvidenceReadBudget()
                deadline = budget.wall_deadline_ms

                def exchange(request, timeout):
                    payload = json.loads(request.data)
                    control = payload["control_budget"]
                    self.assertEqual(control["deadline_unix_ms"], deadline)
                    return FakeResponse(
                        json.dumps(
                            {
                                "token": "synthetic-" + payload["capability"],
                                "capability": payload["capability"],
                                "control_receipt": {**control, "dispatched": 6},
                            }
                        ).encode()
                    )

                broker = BrokerClient(
                    opener=exchange,
                    control=BrokerDispatchAccounting(
                        budget, f"{attempt + 1:064x}", 10, 1
                    ),
                )
                app = GitHubApplication(
                    broker=broker,
                    http=state.http,
                    reviewer=ReviewPublisher(http=state.http),
                    learner=None,
                    replier=None,
                    session_ledger=LocalSessionLedger(Path(directory) / "ledger"),
                )
                app.reviewer.finalizer.human_inventory_reader_factory = None
                result = app.apply_maintainer_command(
                    options=GitHubWriteOptions(github_writes=True),
                    oidc_token="synthetic",
                    repository="owner/repo",
                    repository_id=1,
                    pull_request=1,
                    head_sha=HEAD,
                    app_slug=APP,
                    actor_login="alice",
                    association="OWNER",
                    body="@reviewsensei verify",
                    command_budget=budget,
                )
                progress.append(len(state.resolved_threads))
                self.assertEqual(budget.wall_deadline_ms, deadline)
                self.assertLessEqual(budget.calls, 64)
                if any(r["state"] == "APPROVED" for r in state.reviews):
                    break
                self.assertTrue(
                    attempt == 0 or progress[-1] > progress[-2],
                    (progress, result.summary),
                )
        self.assertEqual(len(state.resolved_threads), 21, (progress, result.summary))
        self.assertEqual(
            sum(r["state"] == "APPROVED" for r in state.reviews),
            1,
            (progress, result.summary),
        )

    def test_command_finalization_uses_original_accounting_and_keeps_tokens_separate(
        self,
    ):
        state = CommandCapacityState(
            complete=True, schema="1", inline_range=(0, 1), human_count=1, padded=False
        )
        state.resolved_threads.clear()
        budget = EvidenceReadBudget()
        original_deadline = budget.wall_deadline_ms
        exchanges, requests = [], []

        def exchange(request, timeout):
            body = json.loads(request.data)
            name, control = body["capability"], body["control_budget"]
            exchanges.append((name, budget.calls))
            self.assertEqual(control["deadline_unix_ms"], original_deadline)
            return FakeResponse(
                json.dumps(
                    {
                        "token": "synthetic-" + name,
                        "capability": name,
                        "control_receipt": {**control, "dispatched": 6},
                    }
                ).encode()
            )

        def open_request(request, timeout):
            body = json.loads(request.data) if request.data else None
            requests.append((request.method, body, request.get_header("Authorization")))
            return state.open(request, timeout)

        broker = BrokerClient(
            opener=exchange, control=BrokerDispatchAccounting(budget, "c" * 64, 10, 1)
        )
        http = GitHubHttp(api_url="https://api.github.test", opener=open_request)
        app = GitHubApplication(
            broker=broker,
            http=http,
            reviewer=ReviewPublisher(http=http),
            learner=None,
            replier=None,
        )
        with tempfile.TemporaryDirectory() as directory:
            state.record(directory)
            app.session_ledger = LocalSessionLedger(Path(directory) / "ledger")
            # The fixture carries an independently bound legacy inventory.
            app.reviewer.finalizer.human_inventory_reader_factory = None
            result = app.apply_maintainer_command(
                options=GitHubWriteOptions(github_writes=True),
                oidc_token="synthetic",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha=HEAD,
                app_slug=APP,
                actor_login="alice",
                association="OWNER",
                body="@reviewsensei verify",
                command_budget=budget,
            )
        self.assertEqual(
            [name for name, _ in exchanges],
            ["review_publish", "review_publish", "check_publish", "thread_resolve"],
            (result.summary, budget.calls, len(requests)),
        )
        self.assertEqual(budget.wall_deadline_ms, original_deadline)
        self.assertLessEqual(budget.calls, 64)
        self.assertEqual(state.resolved_threads, {100})
        self.assertEqual(
            sum(r["state"] == "APPROVED" for r in state.reviews), 0, result
        )
        self.assertIn('"approval_state":"not-observed"', result.summary)
        self.assertIn('"next_action":"retry_verify"', result.summary)
        # Cleanup made durable progress but left no room for approval. A new
        # supported verify observes the resolved thread and acquires no new
        # privileged token. It never enlarges either request's allowance.
        budget = EvidenceReadBudget()
        original_deadline = budget.wall_deadline_ms
        app.broker = BrokerClient(
            opener=exchange, control=BrokerDispatchAccounting(budget, "d" * 64, 10, 1)
        )
        before = len(exchanges)
        with tempfile.TemporaryDirectory() as directory:
            state.record(directory)
            app.session_ledger = LocalSessionLedger(Path(directory) / "ledger")
            replay = app.apply_maintainer_command(
                options=GitHubWriteOptions(github_writes=True),
                oidc_token="synthetic",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha=HEAD,
                app_slug=APP,
                actor_login="alice",
                association="OWNER",
                body="@reviewsensei verify",
                command_budget=budget,
            )
        self.assertEqual(
            sum(r["state"] == "APPROVED" for r in state.reviews), 1, replay
        )
        self.assertNotIn("thread_resolve", [name for name, _ in exchanges[before:]])
        self.assertLessEqual(budget.calls, 64)
        for _, body, auth in requests:
            if body and body.get("operationName") == "ResolveReviewThread":
                self.assertEqual(auth, "Bearer synthetic-thread_resolve")
            elif auth == "Bearer synthetic-thread_resolve":
                self.fail("cleanup token leaked into ordinary publication/read path")
