"""Cleanup belongs to the current run; separate release cannot restore authority."""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import timedelta
from unittest.mock import Mock

from review_sensei_github import cli
from review_sensei_github.release_reservation import release_own_reservation
from review_sensei_github.session_ledger import (
    GitHubIssueCommentSessionLedger,
    parse_session_comment,
    render_session_comment,
)

from review_sensei.errors import ReviewInputError
from review_sensei.session import SessionIdentity, SessionRecord
from tests.fake_github_http import json_response, make_http
from tests.session_fixture_clock import SESSION_FIXTURE_NOW, SessionFixtureClockMixin
from tests.test_api_work_persistence import HEAD
from tests.test_api_work_persistence import IDENTITY as CURRENT_IDENTITY
from tests.test_review_run import EntryFixture

FIXED_NOW = SESSION_FIXTURE_NOW
IDENTITY = SessionIdentity("owner/repo", 136, repository_id=99)
RUN_ID = "10000000001"


class ReleaseReservationTests(SessionFixtureClockMixin, unittest.TestCase):
    def _retired(self, *, run_id=RUN_ID, head_sha="a" * 40):
        broker, http = Mock(), Mock()
        with self.assertRaisesRegex(
            ReviewInputError, "separate reservation cleanup is retired"
        ):
            release_own_reservation(
                broker=broker,
                http=http,
                repository="owner/repo",
                repository_id=99,
                pull_request=136,
                head_sha=head_sha,
                run_id=run_id,
                oidc_token="untrusted-old-selector",
            )
        self.assertEqual(broker.mock_calls, [])
        self.assertEqual(http.mock_calls, [])

    def test_releases_own_reservation(self):
        """The owning process closes only its proven pre-entry analysis abort."""
        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(directory)
            admitted = []

            def refuse_before_entry():
                record = parse_session_comment(
                    f.comment["body"], identity=CURRENT_IDENTITY
                )
                admitted.append(record)
                self.assertEqual(
                    record.reservation_owner,
                    {"run_id": str(f.run_id), "head_sha": HEAD},
                )
                self.assertEqual(record.transaction.phase, "analysis")
                raise ReviewInputError("known input refusal before operation entry")

            f.before_entry = refuse_before_entry
            self.assertEqual(f.invoke(), 1, f.error)
            record = parse_session_comment(f.comment["body"], identity=CURRENT_IDENTITY)
            self.assertIsNone(record.reservation_id)
            self.assertIsNone(record.reservation_owner)
            self.assertIsNone(record.transaction)
            self.assertIsNotNone(record.api_run_accounting["analysis_abort"])
            self.assertEqual(record.failed_attempts, 1)
            self.assertEqual(record.failed_attempts_head_sha, HEAD)
            self.assertEqual(
                record.completed_initial_reviews, admitted[0].completed_initial_reviews
            )
            self.assertEqual(
                record.completed_verification_rounds,
                admitted[0].completed_verification_rounds,
            )
            self.assertEqual(f.provider_calls, 0)
            before = len(f.trace)
            self.assertEqual(f.invoke(), 1)
            self.assertEqual(len(f.trace), before)
            self.assertEqual(
                parse_session_comment(
                    f.comment["body"], identity=CURRENT_IDENTITY
                ).failed_attempts,
                1,
            )

    def test_analysis_checkpoint_is_released(self):
        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(directory)

            def cancel_before_entry():
                raise KeyboardInterrupt()

            f.before_entry = cancel_before_entry
            self.assertEqual(f.invoke(), 130, f.error)
            record = parse_session_comment(f.comment["body"], identity=CURRENT_IDENTITY)
            self.assertIsNone(record.transaction)
            self.assertIsNone(record.reservation_id)
            self.assertIsNotNone(record.api_run_accounting["analysis_abort"])
            self.assertEqual(record.failed_attempts, 0)
            self.assertEqual(f.provider_calls, 0)
            self._retired(run_id=str(f.run_id), head_sha=HEAD)

    def test_owner_from_a_different_head_is_a_noop(self):
        """An old external cleanup selector cannot exchange any capability."""
        for head in ("a" * 40, "b" * 40):
            with self.subTest(head=head):
                self._retired(head_sha=head)

    def test_not_owner_is_a_noop(self):
        for owner in (RUN_ID, "99999999999"):
            with self.subTest(owner=owner):
                self._retired(run_id=owner)

    def test_load_failure_is_not_reported_as_absence(self):
        # The ordinary injected native reader still distinguishes expiry from
        # absence. The retired cleanup path must not turn either into authority.
        expired = SessionRecord.create(
            IDENTITY,
            now=FIXED_NOW - timedelta(days=40),
            expires_at=FIXED_NOW - timedelta(days=10),
        )
        body = render_session_comment(
            repository_id=99, pull_request=136, record=expired
        )
        http, calls = make_http(
            [
                json_response(
                    [
                        {
                            "id": 7,
                            "body": body,
                            "user": {
                                "id": 999,
                                "login": "reviewsensei[bot]",
                                "type": "Bot",
                            },
                        }
                    ]
                )
            ]
        )
        ledger = GitHubIssueCommentSessionLedger(
            http, token="injected-read", app_slug="reviewsensei[bot]"
        )
        loaded = ledger.load(IDENTITY, now=FIXED_NOW)
        self.assertEqual(loaded.status, "expired")
        self.assertNotEqual(loaded.status, "missing")
        self.assertTrue(all(method == "GET" for method, _url, _body in calls))
        self._retired()

    def test_no_reservation_is_a_noop(self):
        self._retired()
        errors = io.StringIO()
        with redirect_stderr(errors):
            # Even malformed/missing old selectors cannot create a second
            # cleanup process or parse an event into authority.
            self.assertEqual(cli.main(["release-reservation"]), 1)
            self.assertEqual(cli.main(["release-reservation", "--run-id", RUN_ID]), 1)
        self.assertIn("separate cleanup is retired", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
