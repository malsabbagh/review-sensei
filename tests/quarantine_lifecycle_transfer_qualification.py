"""Actual own-decision custody and post-observation native drift controls."""

import copy
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from review_sensei_github import quarantine_lifecycle_transfer as transfer
from review_sensei_github import quarantine_pr_observation as observation

from tests import quarantine_worker_qualification as worker


class QuarantineLifecycleTransferTests(unittest.TestCase):
    exercise_counts = worker.QuarantineProductionJourneyTests.exercise_counts

    def test_copied_actual_decision_transfer_refuses_before_refinalization(self):
        created = []
        reached = []
        original = transfer.consume

        class Fixture(worker.ProductionQuarantineJourneyFixture):
            def __init__(self, directory):
                super().__init__(directory)
                created.append(self)

        def copied(token, ledger, identity):
            reached.append(len(created[0].trace))
            try:
                return original(copy.copy(token), ledger, identity)
            except Exception as error:
                reached.append(str(error))
                raise

        with patch.object(transfer, "consume", copied):
            with self.assertRaisesRegex(AssertionError, "decision 1"):
                self.exercise_counts(1, 1, fixture_type=Fixture)
        self.assertTrue(reached)
        fixture = created[0]
        self.assertFalse(any(row.get("state") == "APPROVED" for row in fixture.reviews))
        later = fixture.trace[reached[0] :]
        self.assertFalse(
            any(
                method in {"POST", "PUT", "PATCH"}
                and ("/reviews" in path or "/check-runs" in path)
                for _, method, path in later
            )
        )
        self.assertIn("unissued or reused", reached[1])

    def test_head_change_after_shared_response_is_seen_by_later_fresh_fence(self):
        created = []

        class Fixture(worker.ProductionQuarantineJourneyFixture):
            def __init__(self, directory):
                super().__init__(directory)
                self.commands = 0
                self.active_pr_reads = 0
                self.changed_at = None
                created.append(self)

            def invoke_command(self, *args, **kwargs):
                self.commands += 1
                self.active_pr_reads = 0
                return super().invoke_command(*args, **kwargs)

            def external(self, request, timeout):
                response = super().external(request, timeout)
                if (
                    self.commands == 2
                    and request.method == "GET"
                    and urlsplit(request.full_url).path == "/repos/owner/repo/pulls/7"
                    and observation._ACTIVE.get() is not None
                ):
                    self.active_pr_reads += 1
                    if self.active_pr_reads == 2:
                        # Response bytes are already serialized. Do not move the
                        # source change before the observation under test.
                        self.live_head = "e" * 40
                        self.changed_at = len(self.trace)
                return response

        with self.assertRaisesRegex(AssertionError, "decision 2"):
            self.exercise_counts(1, 1, fixture_type=Fixture)
        fixture = created[0]
        self.assertIsNotNone(fixture.changed_at)
        self.assertFalse(any(row.get("state") == "APPROVED" for row in fixture.reviews))
        later = fixture.trace[fixture.changed_at :]
        self.assertTrue(
            any(
                method == "GET" and path.endswith("/pulls/7")
                for _, method, path in later
            )
        )
        self.assertFalse(
            any(
                method in {"POST", "PUT", "PATCH"}
                and ("/reviews" in path or "/check-runs" in path)
                for _, method, path in later
            )
        )
