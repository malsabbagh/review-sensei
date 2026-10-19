"""Fixed session fixtures must work before and after their wall-date expiry."""

import importlib
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from review_sensei import session


class SessionFixtureClockTests(unittest.TestCase):
    def test_public_flows_ignore_wall_date_but_still_reject_expired_sessions(self):
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
        original = session._aware_now
        for wall_date in (
            datetime(2020, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 10, 19, 12, 1, tzinfo=timezone.utc),
            datetime(2099, 1, 1, tzinfo=timezone.utc),
        ):
            with self.subTest(wall_date=wall_date):
                suite = unittest.TestSuite()
                for name in cases:
                    module_name, class_name, method = name.split(".")
                    prefix = f"{__package__}." if __package__ else ""
                    module = importlib.import_module(prefix + module_name)
                    suite.addTest(getattr(module, class_name)(method))
                with patch.object(
                    session,
                    "_aware_now",
                    side_effect=lambda now=None: original(
                        wall_date if now is None else now
                    ),
                ) as wall_clock:
                    result = unittest.TestResult()
                    suite.run(result)
                    self.assertEqual(result.testsRun, len(cases))
                    self.assertEqual(result.errors + result.failures, [])
                    self.assertIs(session._aware_now, wall_clock)
                self.assertIs(session._aware_now, original)
