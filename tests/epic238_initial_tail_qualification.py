"""Current initial admission through actual CLI and production Worker policy.

The retired phase-grant storage benchmark is not a current admission path.
Only external HTTP/inference are synthetic; caller dispatches and the Worker's
signed authority/service ceilings remain real and independently accounted.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from urllib.parse import urlsplit

from review_sensei_github.api_run_requests import ApiRunSessionLedger
from review_sensei_github.session_ledger import (
    GitHubIssueCommentSessionLedger,
    parse_session_comment,
)

from review_sensei.errors import ReviewInputError
from tests.api_work_worker_qualification import WorkerEntryFixture
from tests.test_api_work_lifecycle import caller_measurement
from tests.test_api_work_persistence import HEAD, IDENTITY


class InitialWorkerFixture(WorkerEntryFixture):
    def __init__(self, directory):
        super().__init__(directory)
        self.admission_writes = []

    def external(self, request, timeout):
        if request.method == "PATCH" and urlsplit(request.full_url).path.endswith(
            "/issues/comments/1000000"
        ):
            candidate = parse_session_comment(
                json.loads(request.data)["body"], identity=IDENTITY
            )
            previous = parse_session_comment(self.comment["body"], identity=IDENTITY)
            if previous.reservation_id is None and candidate.reservation_id is not None:
                self.admission_writes.append(candidate)
        return super().external(request, timeout)


class InitialTailQualification(unittest.TestCase):
    def test_current_owned_admission_is_durable_before_provider_and_completes_once(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            f = InitialWorkerFixture(directory)
            observed = []
            try:

                def before_provider():
                    root = parse_session_comment(f.comment["body"], identity=IDENTITY)
                    self.assertEqual(root.transaction.phase, "analysis")
                    self.assertEqual(
                        root.transaction.reservation_id, root.reservation_id
                    )
                    self.assertEqual(root.transaction.head_sha, HEAD)
                    self.assertEqual(len(f.admission_writes), 1)
                    self.assertEqual(
                        f.admission_writes[0].reservation_id, root.reservation_id
                    )
                    self.assertIsInstance(f.selected.ledger, ApiRunSessionLedger)
                    self.assertIs(
                        f.selected.ledger.evidence_budget, f.accounted.budget_view
                    )
                    self.assertTrue(f.accounted._operation_entry_started)
                    self.assertEqual(
                        f.accounted.accounting.configuration.commit, f.policy
                    )
                    observed.append(root.transaction.transaction_id)

                f.before_provider = before_provider
                self.assertEqual(f.invoke(), 0, f.error)
                self.assertEqual(f.provider_calls, 1)
                self.assertEqual(len(observed), 1)
                final = parse_session_comment(f.comment["body"], identity=IDENTITY)
                self.assertEqual(final.transaction.transaction_id, observed[0])
                self.assertEqual(final.transaction.phase, "publication_succeeded")
                self.assertIsNone(final.reservation_id)
                self.assertEqual(
                    f.accounted.accounting.terminal_transaction, observed[0]
                )
                self.assertEqual(
                    sum(review["state"] == "APPROVED" for review in f.reviews), 1
                )
                measured = caller_measurement(f)
                self.assertEqual(measured["configured_limit"], 512)
                self.assertEqual(
                    measured["physical_attempts"], len(f.accounted.observations)
                )
                self.assertLessEqual(measured["physical_attempts"], 512)
                receipts = f.worker_observations
                self.assertEqual(
                    sum(r["observed_dispatches"] for r in receipts),
                    len(f.bridge.internal_dispatches()["transport"]),
                )
                self.assertTrue(
                    all(
                        r["observed_dispatches"] <= r["service_ceiling"] <= 60
                        for r in receipts
                    )
                )
                self.assertGreater(
                    sum(r["observed_dispatches"] for r in receipts), len(receipts)
                )
                self.assertEqual(
                    sum(path == "/api/github/token" for _, _, path in f.trace),
                    len(receipts),
                )
            finally:
                f.bridge.close()

    def test_cancelled_provider_entry_keeps_original_unknown_charge_and_refuses_new_run(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            f = InitialWorkerFixture(directory)
            try:

                def interrupted():
                    raise KeyboardInterrupt()

                f.before_provider = interrupted
                self.assertEqual(f.invoke(), 130, f.error)
                self.assertEqual(f.provider_calls, 1)
                original = parse_session_comment(f.comment["body"], identity=IDENTITY)
                self.assertIsNone(f.accounted.accounting.analysis_abort)
                self.assertTrue(f.accounted._provider_effect_unresolved)
                f.before_provider = None
                f.next_run()
                f.bridge.oidc_overrides["run_id"] = str(f.run_id)
                self.assertEqual(f.invoke(), 1)
                self.assertEqual(f.provider_calls, 1)
                self.assertEqual(
                    parse_session_comment(f.comment["body"], identity=IDENTITY),
                    original,
                )
                self.assertFalse(f.reviews)
                self.assertEqual(len(f.accounted.observations), len(f.trace))
            finally:
                f.bridge.close()

    def test_retired_phase_grant_constructor_refuses_without_dispatch(self):
        from review_sensei_github.http import GitHubHttp

        def refused(*args, **kwargs):
            self.fail("retired phase authority must fail before external dispatch")

        with self.assertRaisesRegex(
            ReviewInputError, "broker-bound phase ledgers are retired"
        ):
            GitHubIssueCommentSessionLedger(
                GitHubHttp(opener=refused),
                token="synthetic",
                app_slug="reviewsensei[bot]",
                broker=object(),
                session_grant="retired",
                session_attestation={"version": 2},
            )


if __name__ == "__main__":
    unittest.main()
