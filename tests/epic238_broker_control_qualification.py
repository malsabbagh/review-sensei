"""Explicit production-broker control proof; synthetic external transports only.

Run python -m tests.epic238_broker_control_qualification after pinned Worker npm
installation. This does not qualify a selected CLI/queue lifecycle or deployment.
"""

from __future__ import annotations

import copy
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from review_sensei_github.broker_client import BrokerClient
from review_sensei_github.broker_control import BrokerDispatchAccounting
from review_sensei_github.errors import GitHubBrokerClientError
from review_sensei_github.run_authority import parse_authority

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.broker_diagnostics import exception_broker_diagnostic
from review_sensei.errors import ReviewInputError
from tests.epic238_public_dispatch_support import (
    PhysicalDispatchTrace,
    ProductionBrokerBridge,
)


class BrokerControlQualification(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="selected-broker-control-")
        self.trace = PhysicalDispatchTrace(Path(self.temporary.name))
        self.bridge = ProductionBrokerBridge(Path(self.temporary.name), self.trace)
        self.request = self.bridge.call("configure_feedback")["result"]
        parse_authority(self.request)
        self.budget = EvidenceReadBudget()
        self.operation_id = "c" * 64

    def tearDown(self):
        self.bridge.close()
        self.temporary.cleanup()

    def client(self, maximum=13, *, authority=None):
        return BrokerClient(
            broker_url="https://broker.github.test/api/github/token",
            opener=self.trace.opener(self.bridge.open, origin="caller-broker"),
            control=BrokerDispatchAccounting(
                self.budget, self.operation_id, maximum, 1
            ),
            authority=self.request if authority is None else authority,
        )

    def oidc(self, client):
        with patch.dict(
            os.environ,
            {
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic",
                "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.github.test/fixture-oidc?fixture=1",
            },
            clear=True,
        ):
            return client.request_oidc_token()

    def test_real_signed_grant_and_verification_each_match_physical_caller_trace(self):
        client = self.client()
        token = self.oidc(client)
        grant = client.authorize_session_mutation(
            token,
            repository_id=987654321,
            pull_request=7,
            head_sha="a" * 40,
            session_attestation=self.request,
        )
        self.assertEqual(self.budget.calls, 2)
        internals = self.bridge.internal_dispatches()["transport"]
        self.assertEqual(len(self.trace.attempts()), self.budget.calls + len(internals))
        self.assertGreater(len(internals), self.budget.calls)
        self.assertEqual(
            sum(item["origin"] == "caller-broker" for item in self.trace.attempts()),
            self.budget.calls,
        )
        self.assertEqual(
            client.verify_session_grant(grant.grant, grant.attestation),
            grant.attestation,
        )
        self.assertEqual(self.budget.calls, 3)
        self.assertEqual(
            len(self.trace.attempts()),
            self.budget.calls + len(self.bridge.internal_dispatches()["transport"]),
        )
        self.assertEqual(
            sum(item["origin"] == "caller-broker" for item in self.trace.attempts()),
            self.budget.calls,
        )
        self.assertEqual(grant.attestation["version"], 3)
        self.assertNotIn("mutation", grant.attestation)
        self.assertIs(client.control.budget, self.budget)

    def test_cleanup_exchange_uses_production_admission_and_original_dispatch_budget(
        self,
    ):
        client = self.client(10)
        token = self.oidc(client)
        deadline = self.budget.wall_deadline_ms
        before = self.budget.calls
        self.assertEqual(
            client.exchange(token, capability="thread_resolve"), "synthetic-scoped"
        )
        self.assertEqual(self.budget.calls, before + 1)
        self.assertEqual(
            sum(item["origin"] != "broker-internal" for item in self.trace.attempts()),
            self.budget.calls,
        )
        self.assertEqual(self.budget.wall_deadline_ms, deadline)
        attempts = self.trace.attempts()
        self.assertTrue(
            any(item["path"] == "/repos/acme/widgets/installation" for item in attempts)
        )
        self.assertTrue(
            any(
                item["path"] == "/app/installations/2468/access_tokens"
                for item in attempts
            )
        )
        # Exhaust the original allowance, then prove the new capability cannot
        # dispatch or renew its liability/deadline through another exchange.
        while self.budget.ordinary_remaining() > 0:
            self.budget.consume()
        physical = len(self.trace.attempts())
        with self.assertRaises(ReviewInputError):
            client.exchange(token, capability="thread_resolve")
        self.assertEqual(len(self.trace.attempts()), physical)
        self.assertEqual(self.budget.wall_deadline_ms, deadline)

    def test_public_verify_with_durable_decisions_and_real_broker_cleanup_issuance(
        self,
    ):
        """Real admission/physical accounting; explicit local ledger authority.

        External GitHub state/OIDC signing are synthetic. This is not proof of
        live App grants or the separate hosted source-attestation boundary.
        """
        from review_sensei_github.application import (
            GitHubApplication,
            GitHubWriteOptions,
        )
        from review_sensei_github.http import GitHubHttp
        from review_sensei_github.publication import ReviewPublisher

        from review_sensei.session import LocalSessionLedger
        from tests.test_command_capacity_recovery import APP, HEAD, CommandCapacityState

        state = CommandCapacityState(
            complete=True,
            schema="3",
            inline_range=(0, 21),
            human_count=21,
            padded=False,
        )
        state.resolved_threads.clear()
        # Derive the completion target from the resolver's own candidate
        # predicate so this qualification tracks the invariant, not a fixed
        # decided-thread count.
        decided = sum(
            entry.needs_human and not entry.body_placed for entry in state.entries
        )
        self.assertEqual(decided, 21, "the decided-thread fixture contract changed")
        state.record(self.temporary.name)
        self.bridge.call(
            "set_snapshot",
            snapshot={
                "repository": "owner/repo",
                "repositoryId": 1,
                "number": 1,
                "head": HEAD,
            },
        )
        self.bridge.oidc_overrides = {
            "repository": "owner/repo",
            "repository_owner": "owner",
            "repository_id": "1",
        }
        progress = []
        for attempt in range(25):
            self.budget = EvidenceReadBudget()
            deadline = self.budget.wall_deadline_ms
            physical_before = len(self.trace.attempts())
            authority = {
                **self.request,
                "purpose": "review",
                "repository": "owner/repo",
                "repository_id": 1,
                "pull_request": 1,
                "head_sha": HEAD,
                "source": {
                    "kind": "pull_request",
                    "id": 1,
                    "updated_at": None,
                    "body_sha256": None,
                },
            }
            client = self.client(10, authority=authority)
            oidc = self.oidc(client)
            http = GitHubHttp(
                api_url="https://api.github.test",
                opener=self.trace.opener(state.open, origin="host-github"),
            )
            app = GitHubApplication(
                broker=client,
                http=http,
                reviewer=ReviewPublisher(http=http),
                learner=None,
                replier=None,
                session_ledger=LocalSessionLedger(Path(self.temporary.name) / "ledger"),
            )
            app.reviewer.finalizer.human_inventory_reader_factory = None
            outcome = app.apply_maintainer_command(
                options=GitHubWriteOptions(github_writes=True),
                oidc_token=oidc,
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha=HEAD,
                app_slug=APP,
                actor_login="alice",
                association="OWNER",
                body="@reviewsensei verify",
                command_budget=self.budget,
            )
            physical = sum(
                item["origin"] != "broker-internal"
                for item in self.trace.attempts()[physical_before:]
            )
            self.assertLessEqual(physical, self.budget.calls)
            self.assertLessEqual(self.budget.calls, 64)
            self.assertEqual(self.budget.wall_deadline_ms, deadline)
            progress.append(len(state.resolved_threads))
            if attempt:
                # Cleanup only moves forward. Below the completion target every
                # attempt must resolve at least one more decided thread; an
                # attempt that no longer progresses may only revisit the
                # already-complete set while approval catches up.
                self.assertGreaterEqual(
                    progress[-1], progress[-2], (progress, outcome.summary)
                )
                if progress[-1] < decided:
                    self.assertGreater(
                        progress[-1], progress[-2], (progress, outcome.summary)
                    )
            # Approval is funded before cleanup can finish. Later supported
            # verifies must keep reconciling the remaining decided threads
            # without submitting a second native approval.
            if (
                any(review["state"] == "APPROVED" for review in state.reviews)
                and len(state.resolved_threads) >= decided
            ):
                break
        else:
            self.fail(
                f"cleanup and approval did not converge in 25 attempts: {progress}"
            )
        self.assertEqual(len(state.resolved_threads), 21, (progress, outcome.summary))
        self.assertEqual(
            len(state.resolved_threads), decided, (progress, outcome.summary)
        )
        self.assertEqual(
            sum(review["state"] == "APPROVED" for review in state.reviews), 1
        )

    def test_real_http_redirects_cannot_hide_an_unfunded_second_dispatch(self):
        for site in ("rest", "git", "jwks"):
            with self.subTest(site=site):
                reply = self.bridge.call("redirect_probe", site=site)
                self.assertEqual(reply["status"], 200, reply["result"])
                result = reply["result"]
                self.assertEqual(result["requests"], ["/redirect"])
                self.assertEqual(result["category"], "broker_control_redirect_refused")

    def test_real_http_wait_is_cancelled_at_original_control_deadline(self):
        reply = self.bridge.call("redirect_probe", site="rest", blocked=True)
        self.assertEqual(reply["status"], 200, reply["result"])
        result = reply["result"]
        self.assertEqual(result["category"], "broker_control_deadline_exhausted")
        self.assertEqual(result["requests"], ["/redirect"])
        self.assertTrue(result["aborted"])
        self.assertTrue(result["deadline_expired"])
        self.assertEqual(result["dispatches"], 1)
        self.assertEqual(result["original_deadline"], result["final_deadline"])
        self.assertLess(result["elapsed_ms"], 10_000)

    def test_controlled_python_redirect_refusal_preserves_uncontrolled_library_transport(
        self,
    ):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                requests.append(self.path)
                if self.path.startswith("/redirect"):
                    self.send_response(302)
                    self.send_header("Location", "/destination")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"value":"synthetic"}')

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                with patch.dict(
                    os.environ,
                    {
                        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic",
                        "ACTIONS_ID_TOKEN_REQUEST_URL": f"http://127.0.0.1:{server.server_port}/redirect?fixture=1",
                    },
                    clear=True,
                ):
                    self.assertEqual(BrokerClient().request_oidc_token(), "synthetic")
                    self.assertEqual(len(requests), 2)
                    requests.clear()
                    client = BrokerClient(
                        control=BrokerDispatchAccounting(self.budget, "a" * 64, 1, 1)
                    )
                    with self.assertRaises(GitHubBrokerClientError):
                        client.request_oidc_token()
            finally:
                server.shutdown()
                worker.join(timeout=2)
        self.assertEqual(len(requests), 1)
        self.assertNotIn("/destination", requests)
        self.assertEqual(self.budget.calls, 1)

    def test_exhausted_server_subrequest_ceiling_refuses_before_an_unfunded_fetch(self):
        client = self.client(1)
        token = self.oidc(client)
        with self.assertRaises(GitHubBrokerClientError) as raised:
            client.authorize_session_mutation(
                token,
                repository_id=987654321,
                pull_request=7,
                head_sha="a" * 40,
                session_attestation=self.request,
            )
        self.assertEqual(
            exception_broker_diagnostic(raised.exception).code,
            "broker_control_budget_exhausted",
        )
        self.assertEqual(self.budget.calls, 2)
        self.assertEqual(len(self.trace.attempts()), 3)

    def test_obsolete_nested_budget_authority_refuses_before_dispatch(self):
        client = self.client()
        token = self.oidc(client)
        changed = copy.deepcopy(self.request)
        changed["mutation"] = {
            "read_accounting": {
                "schema_version": "1.0",
                "calls": 0,
                "deadline_unix_ms": self.budget.wall_deadline_ms + 1,
            }
        }
        with self.assertRaises(GitHubBrokerClientError):
            client.authorize_session_mutation(
                token,
                repository_id=987654321,
                pull_request=7,
                head_sha="a" * 40,
                session_attestation=changed,
            )
        self.assertEqual(self.budget.calls, 1)
        self.assertEqual(len(self.trace.attempts()), 1)


if __name__ == "__main__":
    unittest.main()
