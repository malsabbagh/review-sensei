"""Explicit production-broker control proof; synthetic external transports only.

Run python -m tests.epic238_broker_control_qualification after pinned Worker npm
installation. This does not qualify a selected CLI/queue lifecycle or deployment.
"""

from __future__ import annotations

import copy
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from review_sensei_github.broker_client import BrokerClient
from review_sensei_github.broker_control import BrokerDispatchAccounting
from review_sensei_github.errors import GitHubBrokerClientError

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.broker_diagnostics import exception_broker_diagnostic
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
        self.budget = EvidenceReadBudget()
        self.request["mutation"]["read_accounting"]["deadline_unix_ms"] = (
            self.budget.wall_deadline_ms
        )

    def tearDown(self):
        self.bridge.close()
        self.temporary.cleanup()

    def client(self, maximum=13):
        return BrokerClient(
            broker_url="https://broker.github.test/api/github/token",
            opener=self.trace.opener(self.bridge.open, origin="caller-broker"),
            control=BrokerDispatchAccounting(
                self.budget, self.request["mutation"]["operation_id"], maximum, 1
            ),
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

    def test_real_signed_grant_and_verification_each_fit_prepaid_physical_trace(self):
        client = self.client()
        token = self.oidc(client)
        grant = client.authorize_session_mutation(
            token,
            repository_id=987654321,
            pull_request=7,
            head_sha="a" * 40,
            session_attestation=self.request,
        )
        self.assertEqual(self.budget.calls, 15)
        self.assertEqual(len(self.trace.attempts()), 15)
        self.assertEqual(
            client.verify_session_grant(grant.grant, grant.attestation),
            grant.attestation,
        )
        self.assertEqual(self.budget.calls, 17)
        self.assertEqual(len(self.trace.attempts()), 17)
        self.assertEqual(
            grant.attestation["mutation"]["read_accounting"]["deadline_unix_ms"],
            self.budget.wall_deadline_ms,
        )

    def test_real_http_redirects_cannot_hide_an_unfunded_second_dispatch(self):
        for site in ("rest", "git", "jwks"):
            with self.subTest(site=site):
                result = self.bridge.call("redirect_probe", site=site)["result"]
                self.assertEqual(result["requests"], ["/redirect"])
                self.assertEqual(result["category"], "broker_control_redirect_refused")

    def test_real_http_wait_is_cancelled_at_original_control_deadline(self):
        result = self.bridge.call("redirect_probe", site="rest", blocked=True)["result"]
        self.assertEqual(result["category"], "transport_refused")
        self.assertEqual(result["requests"], ["/redirect"])

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
        self.assertEqual(self.budget.calls, 3)
        self.assertEqual(len(self.trace.attempts()), 3)

    def test_mutation_deadline_cannot_be_renewed_independently_of_control_budget(self):
        client = self.client()
        token = self.oidc(client)
        changed = copy.deepcopy(self.request)
        changed["mutation"]["read_accounting"]["deadline_unix_ms"] += 1
        with self.assertRaises(GitHubBrokerClientError):
            client.authorize_session_mutation(
                token,
                repository_id=987654321,
                pull_request=7,
                head_sha="a" * 40,
                session_attestation=changed,
            )
        self.assertEqual(self.budget.calls, 15)
        self.assertEqual(len(self.trace.attempts()), 2)


if __name__ == "__main__":
    unittest.main()
