"""Offline regression for the #497 publication-time workflow lookup outage.

Source boundary: GitHub Actions run 45379928785, job 138419030766. The
logged cause was a Git HTTP failure followed by an anonymous REST tag-ref
403 with zero core requests left. The job reported nonterminal publication;
its accounting log was not complete historical effect proof. The test uses
only synthetic identities, responses and a fresh in-memory GitHub fixture.
It does not attempt to establish or change the historical PR's outcome.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.parse import urlsplit

from review_sensei_github.operation_host import RESULT_COMMENT_MARKER
from review_sensei_github.session_ledger import parse_session_comment
from test_api_work_persistence import IDENTITY
from test_review_run import EntryFixture

from review_sensei.api_work import ApiRunAccounting
from review_sensei.broker_diagnostics import BrokerDiagnostic

OUTAGE_LOOKUP = {
    "phase": "rest_ref",
    "failure": "http",
    "git_failure": "http",
    "rate_limit_limit": 60,
    "rate_limit_remaining": 0,
    "rate_limit_resource": "core",
}


class _PublicationLookupFixture(EntryFixture):
    def __init__(self, directory: str, *, broker_status: int) -> None:
        super().__init__(directory)
        self.broker_status = broker_status
        self.previous_conclusion = "failure"
        self.publish_exchanges: list[int] = []

    def external(self, request, timeout):
        url = urlsplit(request.full_url)
        if (
            url.hostname == "broker.github.test"
            and url.path == "/api/github/token"
            and request.get_method() == "POST"
            and json.loads(request.data).get("capability") == "review_publish"
        ):
            self.publish_exchanges.append(self.attempt)
            if self.attempt == 1:
                # Record the refused exchange in the same physical-call trace
                # as the requests handled by the underlying offline fixture.
                self.trace.append((self.attempt, request.get_method(), url.path))
                diagnostic = BrokerDiagnostic(
                    "github_workflow_tag_unavailable"
                    if self.broker_status == 403
                    else "github_workflow_lookup_temporary"
                )
                body = {
                    "error": "capability_not_issued",
                    "diagnostic": {
                        "version": 1,
                        "code": diagnostic.code,
                        "stage": "workflow_identity",
                        "action": diagnostic.action,
                        "upstream_status": 403,
                    },
                    "lookup": OUTAGE_LOOKUP,
                }
                raise HTTPError(
                    request.full_url,
                    self.broker_status,
                    "synthetic workflow identity lookup outage",
                    {},
                    io.BytesIO(json.dumps(body).encode("utf-8")),
                )
        return super().external(request, timeout)


class PublicationAuthorityRecoveryTests(unittest.TestCase):
    def _outage(self, directory, broker_status):
        fixture = _PublicationLookupFixture(directory, broker_status=broker_status)
        self.assertEqual(fixture.invoke(human=True), 1, fixture.error)
        self.assertEqual(fixture.publish_exchanges, [1])
        self.assertEqual(fixture.provider_calls, 1)
        code = (
            "github_workflow_tag_unavailable"
            if broker_status == 403
            else "github_workflow_lookup_temporary"
        )
        self.assertIn(f"broker.code={code} ", fixture.error)
        self.assertIn("broker.stage=workflow_identity", fixture.error)
        self.assertIn("broker.upstream_status=403", fixture.error)
        self.assertIn("broker.lookup_phase=rest_ref", fixture.error)
        self.assertIn("broker.lookup_git_failure=http", fixture.error)
        self.assertIn("broker.lookup_rate_limit_remaining=0", fixture.error)
        self.assertEqual(fixture.reviews, [])
        self.assertEqual(fixture.inline, [])
        self.assertEqual(fixture.check_runs, [])
        self.assertFalse(fixture.accounted._external_business_write_attempted)
        record = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
        self.assertIsNotNone(record)
        self.assertIsNotNone(record.transaction)
        self.assertEqual(record.transaction.phase, "publication_pending")
        self.assertIsNotNone(record.transaction.result_sha256)
        self.assertIsNone(record.reservation_id)
        accounting = ApiRunAccounting.from_document(record.api_run_accounting)
        self.assertIsNone(accounting.terminal_transaction)
        outcome = json.loads((fixture.root / "publication.json").read_text())
        self.assertEqual(outcome["status"], "publication_incomplete")
        self.assertIsNone(outcome["review_id"])
        self.assertFalse(outcome["accounting_terminal"])
        return fixture, record, accounting

    def test_outage_preserves_result_and_qualified_successor_publishes_once(self):
        for broker_status in (403, 503):
            with (
                self.subTest(broker_status=broker_status),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture, before, previous = self._outage(directory, broker_status)
                for name in (
                    "review.json",
                    "configuration.json",
                    "admission.json",
                    "publication.json",
                ):
                    # Diagnostic output from the stopped executor must not act
                    # as authority. Leave the trusted checkout inputs intact.
                    (fixture.root / name).write_text("untrusted", encoding="utf-8")
                fixture.attempt = 2
                self.assertEqual(fixture.invoke(human=True), 0, fixture.error)
                self.assertEqual(fixture.publish_exchanges, [1, 2])
                self.assertEqual(fixture.provider_calls, 1)
                self.assertEqual(len(fixture.reviews), 1)
                self.assertNotEqual(fixture.reviews[0]["state"], "APPROVED")
                self.assertEqual(len(fixture.check_runs), 1)
                self.assertEqual(
                    [
                        attempt
                        for attempt, method, path in fixture.trace
                        if method == "POST" and path.endswith("/pulls/7/reviews")
                    ],
                    [2],
                )
                after = parse_session_comment(
                    fixture.comment["body"], identity=IDENTITY
                )
                self.assertEqual(after.transaction.phase, "publication_succeeded")
                self.assertEqual(
                    after.transaction.transaction_id, before.transaction.transaction_id
                )
                self.assertEqual(
                    after.transaction.result_sha256, before.transaction.result_sha256
                )
                accounting = ApiRunAccounting.from_document(after.api_run_accounting)
                self.assertEqual(
                    accounting.terminal_transaction, before.transaction.transaction_id
                )
                self.assertEqual(accounting.identity, previous.identity)
                self.assertEqual(accounting.configuration, previous.configuration)
                self.assertEqual(accounting.startup, previous.startup)
                self.assertEqual(accounting.owner.run_attempt, 2)
                for block in previous.blocks:
                    self.assertIn(block, accounting.blocks)

    def test_restored_lookup_cannot_replace_missing_pr_checkpoint_with_local_result(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            fixture, before, _ = self._outage(directory, 503)
            ids = [
                comment_id
                for comment_id, comment in fixture.extra_comments.items()
                if RESULT_COMMENT_MARKER in comment["body"]
            ]
            self.assertTrue(ids)
            for comment_id in ids:
                del fixture.extra_comments[comment_id]
            # The old local result still exists; only the authoritative PR
            # carriers are missing. A healthy broker cannot fill that hole.
            self.assertTrue((fixture.root / "review.json").is_file())
            fixture.attempt = 2
            self.assertEqual(fixture.invoke(human=True), 1, fixture.error)
            self.assertIn("historical result bytes do not exist", fixture.error)
            self.assertEqual(fixture.publish_exchanges, [1])
            self.assertEqual(fixture.provider_calls, 1)
            self.assertEqual(fixture.reviews, [])
            self.assertEqual(fixture.check_runs, [])
            after = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            self.assertEqual(after.transaction, before.transaction)
            self.assertIsNone(after.api_run_accounting["terminal_transaction"])

    def test_restored_lookup_does_not_override_an_active_predecessor(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, before, _ = self._outage(directory, 503)
            fixture.attempt = 2
            fixture.previous_active = True
            self.assertEqual(fixture.invoke(human=True), 1, fixture.error)
            self.assertEqual(fixture.publish_exchanges, [1])
            self.assertEqual(fixture.provider_calls, 1)
            self.assertEqual(fixture.reviews, [])
            self.assertEqual(fixture.check_runs, [])
            after = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            self.assertEqual(after, before)
