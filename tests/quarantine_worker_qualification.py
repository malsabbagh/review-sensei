"""Opt-in real Worker compatibility; all external transports remain synthetic."""

import json
import tempfile
import unittest
from urllib.parse import urlsplit

import test_quarantine_journey as journey_contract
from fake_github_http import json_response
from review_sensei_github.api_run_host import review_startup_plan
from review_sensei_github.session_ledger import render_session_comment
from test_quarantine_journey import JourneyFixture

from review_sensei.api_work import (
    ApiExecutionOwner,
    ApiRunIdentity,
    FrozenApiConfiguration,
    initial_accounting,
)
from review_sensei.session import SessionRecord
from tests.api_work_worker_qualification import WorkerEntryFixture
from tests.test_api_work_persistence import IDENTITY
from tests.test_quarantine_lineage import lineage_value


class QuarantineWorkerFixture(WorkerEntryFixture):
    def __init__(self, directory):
        super().__init__(directory)
        self.enrollments = []
        self.event_name = "workflow_dispatch"
        self.bridge.oidc_overrides.update(
            {
                "event_name": "workflow_dispatch",
                "workflow_ref": "owner/repo/.github/workflows/review-sensei-quarantine.yml@refs/heads/main",
            }
        )

    def external(self, request, timeout):
        response = super().external(request, timeout)
        if urlsplit(request.full_url).hostname == "broker.github.test":
            body = json.loads(response.body)
            if "session_state" in body:
                self.enrollments.append(body["session_state"])
        return response


class QuarantineWorkerCompatibilityTests(unittest.TestCase):
    def test_production_worker_accepts_dedicated_caller_with_known_or_enrolled_hint_without_bypassing_native_refusal(
        self,
    ):
        for known in (False, True):
            with self.subTest(known=known), tempfile.TemporaryDirectory() as directory:
                fixture = QuarantineWorkerFixture(directory)
                try:
                    state = initial_accounting(
                        ApiRunIdentity(123, 900),
                        FrozenApiConfiguration("c" * 40, "d" * 64, 512),
                        ApiExecutionOwner(1, "hosted", 0),
                        review_startup_plan("hosted"),
                        prior_nonstartup_liability_upper_bound=0,
                    )
                    record = SessionRecord.create(
                        IDENTITY,
                        generation=10,
                        api_run_accounting=state.to_document(),
                        quarantine_lineage=lineage_value(),
                    )
                    body = render_session_comment(
                        repository_id=123, pull_request=7, record=record
                    )
                    fixture.comment = {
                        "id": 1_000_000,
                        "body": body,
                        "user": {
                            "id": 999,
                            "type": "Bot",
                            "login": "reviewsensei[bot]",
                        },
                    }
                    if known:
                        fixture.extra_comments[42] = {
                            "id": 42,
                            "body": "<!-- review-sensei-operation -->",
                            "user": {
                                "id": 999,
                                "type": "Bot",
                                "login": "reviewsensei[bot]",
                            },
                        }
                    self.assertEqual(fixture.invoke(), 1, fixture.error)
                    self.assertEqual(
                        fixture.enrollments, ["known" if known else "enrolled"]
                    )
                    self.assertEqual(fixture.provider_calls, 0)
                    self.assertEqual(fixture.comment["body"], body)
                    self.assertFalse(fixture.reviews)
                    self.assertFalse(
                        any(
                            method != "GET"
                            for _, method, path in fixture.trace
                            if path.startswith("/repos/")
                        )
                    )
                    self.assertTrue(fixture.bridge.internal_dispatches()["transport"])
                finally:
                    fixture.bridge.close()


class ProductionQuarantineJourneyFixture(JourneyFixture):
    def __init__(self, directory):
        from pathlib import Path

        from tests.epic238_public_dispatch_support import (
            PhysicalDispatchTrace,
            ProductionBrokerBridge,
        )

        super().__init__(directory)
        self.worker_trace = PhysicalDispatchTrace(Path(directory))
        self.bridge = ProductionBrokerBridge(Path(directory), self.worker_trace)
        self.real_worker_receipts = []

    def external(self, request, timeout):
        host, path = (
            urlsplit(request.full_url).hostname,
            urlsplit(request.full_url).path,
        )
        if host not in {"oidc.github.test", "broker.github.test"}:
            return super().external(request, timeout)
        self._meter(request)
        if host == "oidc.github.test":
            command = self.command_active
            result = self.bridge.call(
                "oidc",
                overrides={
                    "repository": "owner/repo",
                    "repository_id": "123",
                    "repository_owner": "owner",
                    "run_id": str(self.run_id),
                    "run_attempt": str(self.attempt),
                    "event_name": self.event_name,
                    "actor": "alice",
                    "actor_id": "42" if command else "99",
                    "workflow_ref": "owner/repo/.github/workflows/"
                    + (
                        "review-sensei-quarantine.yml"
                        if self.quarantine_predecessor_comment_id
                        else "review-sensei-review.yml"
                    )
                    + "@refs/heads/main",
                },
            )
            return json_response({"value": result["result"]})
        self.bridge.call(
            "set_snapshot",
            snapshot={
                "repository": "owner/repo",
                "repositoryId": 123,
                "number": 7,
                "base": self.live_base,
                "head": self.live_head,
                "comments": ([self.comment] if self.comment else [])
                + list(self.extra_comments.values()),
                "reviews": self.reviews,
            },
        )
        if self.command_active:
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
        result = self.bridge.call(
            "verify" if path.endswith("/session-grant") else "exchange", body=payload
        )
        if result["result"].get("control_receipt"):
            self.real_worker_receipts.append(result["result"]["control_receipt"])
        return json_response(result["result"], result["status"])


class QuarantineProductionJourneyTests(unittest.TestCase):
    exercise_counts = journey_contract.QuarantineJourneyTests.exercise_counts

    # Only this explicitly selected composition runs through the production
    # Worker; inherited source contracts remain in the normal checkout lane.
    def test_production_worker_composes_with_complete_local_quarantine_journey(self):
        self.exercise_counts(1, 1, fixture_type=ProductionQuarantineJourneyFixture)
