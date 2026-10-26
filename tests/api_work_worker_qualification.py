"""Opt-in production Worker composition; only external HTTP is synthetic.

Run with Node >=22.15 and locked Worker dependencies installed. No sockets,
credentials, service deployment, live grants or synthetic control receipts.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from review_sensei_github.run_authority import parse_authority

from tests.epic238_public_dispatch_support import (
    PhysicalDispatchTrace,
    ProductionBrokerBridge,
)
from tests.fake_github_http import json_response
from tests.test_api_work_persistence import BASE
from tests.test_review_run import EntryFixture


class WorkerEntryFixture(EntryFixture):
    def __init__(self, directory):
        super().__init__(directory)
        self.worker_trace = PhysicalDispatchTrace(Path(directory))
        self.bridge = ProductionBrokerBridge(Path(directory), self.worker_trace)
        self.bridge.oidc_overrides = {
            "repository": "owner/repo",
            "repository_id": "123",
            "repository_owner": "owner",
            "run_id": "456",
            "run_attempt": "1",
            "event_name": "pull_request",
            "sub": "repo:owner/repo:pull_request",
            "workflow_ref": "owner/repo/.github/workflows/review-sensei-review.yml@refs/heads/main",
        }
        self.worker_ceiling = None

    def external(self, request, timeout):
        host = urlsplit(request.full_url).hostname
        if host not in {"oidc.github.test", "broker.github.test"}:
            return super().external(request, timeout)
        self.assert_metered(request)
        if host == "oidc.github.test":
            result = self.bridge.call("oidc", overrides=self.bridge.oidc_overrides)
            return json_response({"value": result["result"]})
        self.bridge.call(
            "set_snapshot",
            snapshot={
                "repository": "owner/repo",
                "repositoryId": 123,
                "base": BASE,
                "head": self.live_head,
                "reviews": self.reviews,
                "comments": ([self.comment] if self.comment else [])
                + list(self.extra_comments.values()),
            },
        )
        payload = json.loads(request.data)
        # The actual caller constructs the current envelope. The bridge must
        # never upgrade an obsolete input into current authority on its behalf.
        authority = parse_authority(payload["session_attestation"])
        assert authority["version"] == 3 and authority["purpose"] == "review"
        if self.worker_ceiling is not None:
            payload["control_budget"]["max_dispatches"] = self.worker_ceiling
        result = self.bridge.call("exchange", body=payload)
        receipt = result["result"].get("control_receipt")
        if receipt:
            self.worker_observations.append(
                {
                    "capability": payload["capability"],
                    "service_ceiling": receipt["max_dispatches"],
                    "observed_dispatches": receipt["dispatched"],
                }
            )
        return json_response(result["result"], result["status"])

    def assert_metered(self, request):
        assert (
            len(self.accounted.observations)
            == sum(a == self.attempt for a, _, _ in self.trace) + 1
        )
        self.trace.append(
            (self.attempt, request.get_method(), urlsplit(request.full_url).path)
        )


class ProductionWorkerCallerTests(unittest.TestCase):
    def test_actual_entry_authenticates_worker_and_never_charges_its_fanout(self):
        for human in (False, True):
            with self.subTest(human=human), tempfile.TemporaryDirectory() as directory:
                f = WorkerEntryFixture(directory)
                try:
                    self.assertEqual(f.invoke(human=human), 0, f.error)
                    self.assertEqual(f.provider_calls, 1)
                    self.assertEqual(
                        any(r["state"] == "APPROVED" for r in f.reviews), not human
                    )
                    internals = f.bridge.internal_dispatches()["transport"]
                    receipts = f.worker_observations
                    self.assertEqual(
                        sum(r["observed_dispatches"] for r in receipts), len(internals)
                    )
                    self.assertGreater(len(internals), len(receipts))
                    self.assertEqual(len(f.accounted.observations), len(f.trace))
                    self.assertEqual(
                        sum(p == "/api/github/token" for _, _, p in f.trace),
                        len(receipts),
                    )
                    state = f.accounted.accounting
                    self.assertEqual(
                        state.configuration.limit,
                        state.startup.liability
                        + state.prior_nonstartup_liability
                        + state.confirmed
                        + state.uncertain
                        + state.reserved_undispatched
                        + state.remaining,
                    )
                    self.assertEqual(
                        {r["capability"] for r in receipts},
                        {
                            "review_actions",
                            "review_session",
                            "review_publish",
                            "check_publish",
                        },
                    )
                finally:
                    f.bridge.close()

    def test_wrong_workflow_and_exhausted_worker_ceiling_refuse_before_admission(self):
        for refusal in ("workflow", "ceiling"):
            with (
                self.subTest(refusal=refusal),
                tempfile.TemporaryDirectory() as directory,
            ):
                f = WorkerEntryFixture(directory)
                try:
                    if refusal == "workflow":
                        f.bridge.oidc_overrides["job_workflow_sha"] = "c" * 40
                    else:
                        f.worker_ceiling = 1
                    self.assertEqual(f.invoke(), 1)
                    self.assertEqual(f.provider_calls, 0)
                    self.assertIsNone(f.comment)
                    self.assertFalse(f.reviews)
                    self.assertEqual(
                        sum(p == "/api/github/token" for _, _, p in f.trace), 1
                    )
                finally:
                    f.bridge.close()
