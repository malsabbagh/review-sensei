"""Real grouped prebusiness prefix; no grouped review execution is admitted."""

import copy
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from review_sensei_github.quarantine_published_tail import BROKER_QUALIFICATIONS
from review_sensei_github.session_ledger import parse_session_comment
from test_quarantine_activation import registered_resource
from test_quarantine_published_tail_journey import NativePublishedTailJourneyFixture

from review_sensei import publication_intent_fusion
from review_sensei.session import SessionIdentity

IDENTITY = SessionIdentity("owner/repo", 7, repository_id=123)


class LostProgressFixture(NativePublishedTailJourneyFixture):
    loss = None
    lost_candidate = None

    def external(self, request, timeout):
        path = urlsplit(request.full_url).path
        if (
            self.loss is not None
            and self.lost_candidate is None
            and request.get_method() == "PATCH"
            and "/issues/comments/" in path
        ):
            payload = json.loads(request.data)
            candidate = parse_session_comment(
                payload.get("body", ""), identity=IDENTITY
            )
            if (
                candidate is not None
                and candidate.transaction is not None
                and candidate.transaction.phase == "publication_failed"
                and candidate.convergence_history is not None
                and candidate.convergence_history.get("progress")
                and "blocker_set_sha256"
                in candidate.convergence_history["progress"][-1]
            ):
                self.lost_candidate = candidate
                if self.loss == "stored":
                    super().external(request, timeout)
                else:
                    self._meter(request)
                raise TimeoutError("synthetic lost joint progress acknowledgement")
        return super().external(request, timeout)


class GroupedProgressFusionTests(unittest.TestCase):
    def exercise(self, loss):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            fixture = LostProgressFixture(directory)
            old, registration = fixture.original_origins()
            prior = copy.deepcopy(old.api_run_accounting)
            reviews, checks = (
                copy.deepcopy(fixture.reviews),
                copy.deepcopy(fixture.check_runs),
            )
            fixture.loss = loss
            fixture.provider_comments = [
                {
                    "path": "src/app.py",
                    "line": 3,
                    "side": "RIGHT",
                    "body": "Current published assessment 0 requires independent human judgment.",
                    "severity": "high",
                    "blocking": False,
                    "needs_human": True,
                }
            ]
            transfers = []

            def observe(frame, event, value):
                if (
                    event == "return"
                    and frame.f_code
                    is publication_intent_fusion.confirmed_grouped_progress.__code__
                ):
                    transfers.append(value)

            with registered_resource([registration]):
                previous = sys.getprofile()
                try:
                    sys.setprofile(observe)
                    rc = fixture.invoke()
                finally:
                    sys.setprofile(previous)
            self.assertEqual(rc, 1)
            self.assertIsNotNone(fixture.lost_candidate, fixture.error)
            self.assertGreater(fixture.accounted.accounting.uncertain, 0)
            self.assertEqual(fixture.accounted.accounting.configuration.limit, 512)
            self.assertEqual(old.api_run_accounting, prior)
            self.assertEqual(fixture.reviews, reviews)
            self.assertEqual(fixture.check_runs, checks)
            if loss == "stored":
                self.assertEqual(transfers.count(True), 1, fixture.error)
                self.assertIn("cannot fund the complete operation", fixture.error)
            else:
                self.assertNotIn(True, transfers)
                self.assertNotIn("cannot fund the complete operation", fixture.error)

    def test_stored_joint_ack_loss_reconciles_before_metadata_transfer(self):
        self.exercise("stored")

    def test_absent_joint_ack_loss_never_yields_progress_metadata(self):
        self.exercise("absent")
