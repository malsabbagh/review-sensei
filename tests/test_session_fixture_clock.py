"""Public fixture setup and explicit-date expiry are separate contracts."""

import importlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from review_sensei_github import session_ledger

from review_sensei import session
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import require_module_seal
from tests.fake_github_http import json_response, make_http


class SessionFixtureClockTests(unittest.TestCase):
    def test_public_flows_keep_each_fixture_scoped_setup(self):
        cases = (
            "test_automation_admission.GitHubHandoffPublicationTests."
            "test_failed_attempt_handoff_does_not_publish",
            "test_convergence_evaluation.ShadowObservationTests."
            "test_shadow_does_not_skip_legacy_publication",
            "test_explicit_rescan.RescanHostTests."
            "test_changed_snapshot_rescan_reviews_full_diff_without_incremental_reuse",
            "test_partial_review_transaction.PartialCheckpointTests."
            "test_partial_keeps_last_complete_baseline_and_next_head_admission",
            "test_release_reservation.ReleaseReservationTests.test_releases_own_reservation",
            "test_release_reservation.ReleaseReservationTests."
            "test_load_failure_is_not_reported_as_absence",
            "test_review_transaction.ReviewTransactionTests."
            "test_result_checkpoint_counts_once_and_replays",
            "test_session_ledger.DiagnosticSessionTests."
            "test_doctor_and_plan_display_local_session",
        )
        # These are the unchanged public-flow bodies with their own setup.
        # No claim is made that hosted system time is simulated. In particular,
        # the original hosted reservation fixture must not inherit a fake
        # session function from this outer orchestration test.
        original = session._aware_now
        suite = unittest.TestSuite()
        for name in cases:
            module_name, class_name, method = name.split(".")
            prefix = f"{__package__}." if __package__ else ""
            module = importlib.import_module(prefix + module_name)
            suite.addTest(getattr(module, class_name)(method))
        result = unittest.TestResult()
        suite.run(result)
        self.assertEqual(result.testsRun, len(cases))
        self.assertEqual(result.errors + result.failures, [])
        self.assertIs(session._aware_now, original)


class ExplicitSessionDateTests(unittest.TestCase):
    DATES = (
        datetime(2020, 1, 1, tzinfo=timezone.utc),
        datetime(2026, 10, 19, 12, 1, tzinfo=timezone.utc),
        datetime(2099, 1, 1, tzinfo=timezone.utc),
    )
    IDENTITY = session.SessionIdentity("owner/repo", 136, repository_id=99)

    def setUp(self):
        self.original_clock = session._aware_now
        self.original_code = self.original_clock.__code__
        require_module_seal(session)
        require_module_seal(session_ledger)

    def tearDown(self):
        self.assertIs(session._aware_now, self.original_clock)
        self.assertIs(session._aware_now.__code__, self.original_code)
        require_module_seal(session)
        require_module_seal(session_ledger)

    def test_original_local_date_boundaries_never_read_expiry_as_missing_or_admit(self):
        for boundary in self.DATES:
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                ledger = session.LocalSessionLedger(root)
                self.assertEqual(
                    ledger.load(self.IDENTITY, now=boundary).status, "missing"
                )
                record = ledger.initialize(
                    self.IDENTITY,
                    now=boundary - timedelta(days=1),
                    expires_at=boundary,
                )
                before = {
                    str(p.relative_to(root)): p.read_bytes()
                    for p in root.rglob("*")
                    if p.is_file()
                }
                for delta, expected in ((-1, "ok"), (0, "expired"), (1, "expired")):
                    now = boundary + timedelta(seconds=delta)
                    self.assertEqual(record.expired(now=now), expected == "expired")
                    self.assertEqual(
                        ledger.load(self.IDENTITY, now=now).status, expected
                    )
                    if expected == "expired":
                        with self.assertRaisesRegex(ReviewInputError, "expired"):
                            session.prepare_session_round(
                                ledger,
                                self.IDENTITY,
                                ReviewConvergencePolicy(),
                                reservation_id="a" * 64,
                                head_sha="b" * 40,
                                now=now,
                            )
                self.assertEqual(
                    {
                        str(p.relative_to(root)): p.read_bytes()
                        for p in root.rglob("*")
                        if p.is_file()
                    },
                    before,
                )

    def test_original_http_date_boundaries_preserve_expired_refusal_without_writes(
        self,
    ):
        for boundary in self.DATES:
            record = session.SessionRecord.create(
                self.IDENTITY,
                now=boundary - timedelta(days=1),
                expires_at=boundary,
            )
            body = session_ledger.render_session_comment(
                repository_id=99, pull_request=136, record=record
            )
            row = {
                "id": 7,
                "body": body,
                "user": {"id": 999, "login": "reviewsensei[bot]", "type": "Bot"},
            }
            for delta, expected in ((-1, "ok"), (0, "expired"), (1, "expired")):
                with self.subTest(boundary=boundary, delta=delta):
                    http, calls = make_http(
                        [json_response([row]), json_response([row])]
                    )
                    ledger = session_ledger.GitHubIssueCommentSessionLedger(
                        http, token="injected-read", app_slug="reviewsensei[bot]"
                    )
                    now = boundary + timedelta(seconds=delta)
                    self.assertEqual(
                        ledger.load(self.IDENTITY, now=now).status, expected
                    )
                    if expected == "expired":
                        with self.assertRaisesRegex(ReviewInputError, "expired"):
                            session.prepare_session_round(
                                ledger,
                                self.IDENTITY,
                                ReviewConvergencePolicy(),
                                reservation_id="a" * 64,
                                head_sha="b" * 40,
                                now=now,
                            )
                    self.assertTrue(calls)
                    self.assertTrue(
                        all(method == "GET" for method, _url, _body in calls)
                    )
