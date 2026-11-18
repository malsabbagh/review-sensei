"""Actual own-decision custody and post-observation native drift controls."""

import copy
import sys
import unittest
from urllib.parse import urlsplit

from review_sensei_github import quarantine_lifecycle_transfer as transfer
from review_sensei_github import quarantine_pr_observation as observation

from review_sensei.errors import ReviewInputError
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

        def observe(frame, event, _result):
            if event != "call" or frame.f_code is not original.__code__:
                return
            # Observe the actual issued consumer without replacing a sealed
            # source. A monkeypatched consumer is rejected before this boundary.
            self.assertIs(frame.f_globals, vars(transfer))
            token, ledger, identity = (
                frame.f_locals[key] for key in ("token", "ledger", "identity")
            )
            self.assertIn(token, transfer._ISSUED)
            self.assertIsNotNone(transfer._ISSUED[token]["confirmed"])
            boundary = len(created[0].trace)
            reached.append(boundary)
            with self.assertRaisesRegex(
                ReviewInputError, "unissued or reused"
            ) as error:
                original(copy.copy(token), ledger, identity)
            self.assertEqual(len(created[0].trace), boundary)
            self.assertIn(token, transfer._ISSUED)
            self.assertNotIn(ledger, transfer._NAMESPACE_RESTORE)
            reached.append(str(error.exception))
            # The real copied-token refusal terminates this attempted handoff;
            # do not let the suspended original-token call finalize instead.
            raise error.exception

        previous_profile = sys.getprofile()
        try:
            sys.setprofile(observe)
            with self.assertRaisesRegex(AssertionError, "decision 1"):
                self.exercise_counts(1, 1, fixture_type=Fixture)
        finally:
            sys.setprofile(previous_profile)
        self.assertEqual(len(reached), 2)
        self.assertIs(transfer.consume, original)
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
