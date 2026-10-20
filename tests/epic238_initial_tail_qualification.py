"""Compare independent original-operation storage segments with production TS.

This proves physical savings, not complete CLI/context/provider/publication cost.
"""

from __future__ import annotations

import copy
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from review_sensei_github.broker_client import BrokerClient
from review_sensei_github.broker_control import BrokerDispatchAccounting
from review_sensei_github.session_ledger import GitHubIssueCommentSessionLedger

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.feedback import FeedbackReference, FeedbackSelection, FeedbackSource
from review_sensei.session import SessionIdentity
from tests import test_review_transaction as fixture
from tests.epic238_public_dispatch_support import (
    PhysicalDispatchTrace,
    ProductionBrokerBridge,
)
from tests.fake_github_http import json_response
from tests.test_activation_tail import OPERATION, prepare_queue, seal_accounting
from tests.test_authenticated_partitions import GitHubState
from tests.test_initial_assessment_tail import admitted_handle


class InitialTailQualification(unittest.TestCase):
    def segment(self, coalesced):
        with TemporaryDirectory(prefix="initial-assessment-physical-") as directory:
            directory = Path(directory)
            trace = PhysicalDispatchTrace(directory)
            bridge = ProductionBrokerBridge(directory, trace)
            identity = SessionIdentity("acme/widgets", 7, repository_id=987654321)
            try:
                request = bridge.call("configure_feedback")["result"]
                complete = json.loads(
                    (
                        Path(__file__).parent
                        / "fixtures/feedback-session-attestation.json"
                    ).read_text()
                )["selection"]
                source = complete["sources"][0]
                selection = FeedbackSelection(
                    complete["repository"],
                    complete["pull_request"],
                    complete["base_sha"],
                    complete["head_sha"],
                    FeedbackReference(**complete["trigger"]),
                    (
                        FeedbackSource(
                            FeedbackReference(
                                **{
                                    key: source[key]
                                    for key in ("kind", "comment_id", "updated_at")
                                }
                            ),
                            source["author"],
                            source["author_id"],
                            source["association"],
                            source["body"],
                        ),
                    ),
                    tuple(complete["target_ids"]),
                )
                request["feedback"] = selection.authorization_document()
                with (
                    patch.object(fixture, "IDENTITY", identity),
                    patch.object(fixture, "BASE_SHA", selection.base_sha),
                    patch.object(fixture, "HEAD_SHA", selection.head_sha),
                ):
                    state = GitHubState()
                    current = state.ledger().initialize(identity, now=fixture.NOW)
                    state.calls.clear()  # Enrollment precedes this original operation.
                    opened = state.http.opener

                    def transport(req, timeout):
                        if req.full_url.endswith("/pulls/7"):
                            state.calls.append((req.method, req.full_url))
                            return json_response({"head": {"sha": selection.head_sha}})
                        return opened(req, timeout)

                    state.http.opener = trace.opener(transport, origin="caller-github")
                    budget = EvidenceReadBudget()
                    handle = admitted_handle(budget, OPERATION, identity)
                    client = BrokerClient(
                        broker_url="https://broker.github.test/api/github/token",
                        opener=trace.opener(bridge.open, origin="caller-broker"),
                        control=BrokerDispatchAccounting(
                            budget, OPERATION["operation_id"], 12, 1
                        ),
                    )
                    with patch.dict(
                        os.environ,
                        {
                            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic",
                            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.github.test/fixture-oidc?fixture=1",
                        },
                        clear=True,
                    ):
                        oidc = client.request_oidc_token()

                    def issue(root, reason):
                        value = copy.deepcopy(request)
                        value["mutation"].update(
                            **OPERATION,
                            reservation_id=handle.binding,
                            root_digest=root.record_sha256,
                            root_generation=root.generation,
                            reason=reason,
                            request_digest="f" * 64
                            if reason == "admission-dispatch"
                            else None,
                            dispatch_digest="d" * 64
                            if reason == "admission-dispatch"
                            else None,
                            read_accounting=budget.snapshot(),
                        )
                        return client.authorize_session_mutation(
                            oidc,
                            repository_id=identity.repository_id,
                            pull_request=7,
                            head_sha=selection.head_sha,
                            session_attestation=value,
                        )

                    grant = issue(
                        current, "admission-dispatch" if coalesced else "admission"
                    )
                    ledger = GitHubIssueCommentSessionLedger(
                        state.http,
                        token=grant.token,
                        app_slug="sensei[bot]",
                        broker=client,
                        session_grant=grant.grant,
                        session_attestation=grant.attestation,
                        head_sha=selection.head_sha,
                        evidence_budget=budget,
                        enable_partition_writes=True,
                    )
                    if coalesced:
                        prepare = prepare_queue(ledger)
                        result = ledger.admit_with_tail(
                            identity,
                            lambda reserved: prepare(reserved).evolve(
                                generation=reserved.generation, now=fixture.NOW
                            ),
                            slot="verification",
                            expected_generation=0,
                            operation_binding=OPERATION,
                            attempt_reservation_id=handle.binding,
                            seal_accounting=seal_accounting,
                            max_scan_pages=1,
                            now=fixture.NOW,
                            witness=handle,
                        )
                    else:
                        current = ledger.reserve_for_tail(
                            identity,
                            operation_binding=OPERATION,
                            slot="verification",
                            reservation_id=handle.binding,
                            expected_generation=0,
                            max_scan_pages=1,
                            head_sha=selection.head_sha,
                            now=fixture.NOW,
                            witness=handle,
                        )
                        grant = issue(current, "admission-dispatch")
                        ledger.bind_tail_grant(
                            identity,
                            grant,
                            operation_binding=OPERATION,
                            attempt_reservation_id=handle.binding,
                            max_scan_pages=1,
                            now=fixture.NOW,
                            witness=handle,
                        )
                        result = ledger.replace_with_tail(
                            identity,
                            prepare_queue(ledger),
                            operation_binding=OPERATION,
                            attempt_reservation_id=handle.binding,
                            seal_accounting=seal_accounting,
                            max_scan_pages=1,
                            now=fixture.NOW,
                            witness=handle,
                        )
                    self.assertEqual(result.reservation_id, handle.binding)
                    self.assertEqual(
                        result.assessment_queue["active_operation"]["read_accounting"][
                            "calls"
                        ],
                        budget.calls,
                    )
                    self.assertLessEqual(len(trace.attempts()), budget.calls)
                    self.assertEqual(result.generation, 1 if coalesced else 2)
                    return {
                        "physical": len(trace.attempts()),
                        "liability": budget.calls,
                        "writes": sum(method == "PATCH" for method, _ in state.calls),
                    }
            finally:
                bridge.close()

    def test_one_actual_mutation_saves_twenty_one_physical_dispatches(self):
        separate, coalesced = self.segment(False), self.segment(True)
        self.assertEqual(separate, {"physical": 42, "liability": 43, "writes": 2})
        self.assertEqual(coalesced, {"physical": 21, "liability": 21, "writes": 1})
        self.assertEqual(separate["physical"] - coalesced["physical"], 21)


if __name__ == "__main__":
    unittest.main()
