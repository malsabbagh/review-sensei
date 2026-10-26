"""Opt-in actual command CLI + production Worker with fake external HTTP only.

Requires the pinned Worker dependencies and Node>=22.15. No sockets, provider
inference, deployment, real credentials or incoming grant/body rewriting.
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from review_sensei_github.session_ledger import parse_session_comment

from review_sensei.api_work import ApiRunAccounting
from tests.epic238_public_dispatch_support import (
    PhysicalDispatchTrace,
    ProductionBrokerBridge,
)
from tests.fake_github_http import json_response
from tests.test_action_run import CommandEntryFixture
from tests.test_api_work_persistence import BASE, IDENTITY


class ProductionCommandEntryFixture(CommandEntryFixture):
    def __init__(self, directory, *, limit=512):
        super().__init__(directory, limit=limit)
        self.worker_trace = PhysicalDispatchTrace(Path(directory))
        self.bridge = ProductionBrokerBridge(Path(directory), self.worker_trace)
        self.worker_receipts = []
        self.worker_app_ids = []
        self.verified_grants = 0

    def external(self, request, timeout):
        host, path = (
            urlsplit(request.full_url).hostname,
            urlsplit(request.full_url).path,
        )
        if host not in {"oidc.github.test", "broker.github.test"}:
            return super().external(request, timeout)
        self._meter(request)
        if host == "oidc.github.test":
            result = self.bridge.call(
                "oidc",
                overrides={
                    "repository": "owner/repo",
                    "repository_id": "123",
                    "repository_owner": "owner",
                    "run_id": str(self.run_id),
                    "run_attempt": str(self.attempt),
                    "event_name": "issue_comment",
                    "actor": "alice",
                    "actor_id": "42",
                    "workflow_ref": "owner/repo/.github/workflows/review-sensei-review.yml@refs/heads/main",
                },
            )
            return json_response({"value": result["result"]})
        # Independently configure fake GitHub state. No incoming authority field
        # supplies the expected source URL, actor, repository or revision.
        self.bridge.call(
            "set_snapshot",
            snapshot={
                "repository": "owner/repo",
                "repositoryId": 123,
                "number": 7,
                "base": BASE,
                "head": self.live_head,
                "comments": ([self.comment] if self.comment else [])
                + list(self.extra_comments.values()),
                "reviews": self.reviews,
            },
        )
        self.bridge.call(
            "set_source",
            source={
                "id": self.SOURCE_ID,
                "body": self.source["body"],
                "updatedAt": self.source["updated_at"],
                "login": "alice",
                "authorId": 42,
                "userType": "User",
                "association": "OWNER",
                "url": "https://api.github.test/repos/owner/repo/issues/7",
                "status": 200,
            },
        )
        payload = json.loads(request.data)
        self.broker_requests.append(copy.deepcopy(payload))
        action = "verify" if path.endswith("/session-grant") else "exchange"
        response = self.bridge.call(action, body=payload)
        value = response["result"]
        receipt = value.get("control_receipt")
        if receipt is not None:
            self.worker_receipts.append(
                {"capability": payload.get("capability", "grant_verify"), **receipt}
            )
        if value.get("app_id") is not None:
            self.worker_app_ids.append(value["app_id"])
        if action == "verify" and response["status"] == 200:
            self.verified_grants += 1
        return json_response(value, response["status"])


class ProductionCommandEntryQualification(unittest.TestCase):
    def test_pause_uses_actual_v3_worker_auth_and_trusted_app_identity_at_512(self):
        with tempfile.TemporaryDirectory() as directory:
            f = ProductionCommandEntryFixture(directory)
            try:
                self.assertEqual(f.invoke_command(), 0, (f.error, f.exception))
                root = parse_session_comment(f.comment["body"], identity=IDENTITY)
                state = ApiRunAccounting.from_document(root.api_run_accounting)
                self.assertTrue(root.operator_paused)
                self.assertEqual(state.action["phase"], "terminal")
                self.assertEqual(state.configuration.limit, 512)
                self.assertEqual(f.verified_grants, 1)
                self.assertEqual(f.worker_app_ids, [12345])
                self.assertEqual(f.selected.ledger.authenticated_app_id, 12345)
                self.assertEqual(f.provider_calls, 0)
                self.assertEqual(f.business_writes, 1)
                self.assertEqual(len(f.accounted.observations), len(f.trace))
                self.assertTrue(
                    all(
                        request["session_attestation"]["version"] == 3
                        for request in f.broker_requests
                    )
                )
                self.assertEqual(
                    [r["capability"] for r in f.worker_receipts],
                    ["review_actions", "review_session", "grant_verify"],
                )
                internals = f.bridge.internal_dispatches()["transport"]
                self.assertEqual(
                    sum(r["dispatched"] for r in f.worker_receipts), len(internals)
                )
                self.assertGreater(len(internals), len(f.worker_receipts))
                self.assertEqual(
                    sum(
                        path.endswith("/token")
                        and "/api/github/" in path
                        or path.endswith("/session-grant")
                        for _, _, path in f.trace
                    ),
                    len(f.worker_receipts),
                )
                self.assertEqual(
                    state.configuration.limit,
                    state.startup.liability
                    + state.prior_nonstartup_liability
                    + state.confirmed
                    + state.uncertain
                    + state.reserved_undispatched
                    + state.remaining,
                )
            finally:
                f.bridge.close()


if __name__ == "__main__":
    unittest.main()
