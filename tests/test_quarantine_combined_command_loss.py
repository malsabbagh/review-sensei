"""Plain command loss mechanisms compose without claiming guarded admission."""

import json
import unittest
from dataclasses import replace
from urllib.parse import urlsplit

import test_quarantine_command_checkpoint as checkpoint_tests


class CombinedCommandLossTests(unittest.TestCase):
    def test_checkpoint_and_receipt_losses_keep_exact_terminal_result(self):
        from review_sensei_github import quarantine_command_execution

        fixtures = []
        leases = []
        rejected_forgery = []

        class CombinedFixture(checkpoint_tests.AllCheckpointLossFixture):
            def __init__(self, directory, fault=None):
                super().__init__(directory, fault)
                self.receipt_attempts = 0
                self.receipt_reads = 0
                fixtures.append(self)

            def external(self, request, timeout):
                path = urlsplit(request.full_url).path
                if (
                    leases
                    and request.get_method() == "GET"
                    and path == "/repos/owner/repo/issues/7/comments"
                ):
                    self.receipt_reads += 1
                body = json.loads(request.data).get("body", "") if request.data else ""
                receipt = (
                    request.get_method() in {"POST", "PATCH"}
                    and "reviewsensei:command-receipt:v1" in body
                )
                if receipt:
                    self.receipt_attempts += 1
                response = super().external(request, timeout)
                if receipt:
                    raise TimeoutError("synthetic stored receipt response loss")
                return response

        def observe(frame, event, arg):
            if (
                frame.f_code
                is not quarantine_command_execution.require_receipt_phase.__code__
            ):
                return None
            if event == "return" and arg is not None:
                leases.append(arg)
                from review_sensei.errors import ReviewInputError

                run, authority = frame.f_locals["run"], frame.f_locals["authority"]
                state = run.caller._state
                ordinary = next(
                    b for b in state.blocks if b.block_id == run.caller._block
                )
                self.assertGreaterEqual(len(ordinary.pending), 4)
                stolen = ordinary.pending[0]
                forged = replace(
                    state,
                    blocks=tuple(
                        replace(b, pending=b.pending[1:]) if b is ordinary else b
                        for b in state.blocks
                    ),
                    completed=(*state.completed, stolen),
                )
                self.assertEqual(forged.remaining, state.remaining)
                self.assertEqual(
                    forged.blocks[0].undispatched, state.blocks[0].undispatched
                )
                try:
                    run.caller._state = forged
                    with self.assertRaisesRegex(
                        ReviewInputError, "root or owner changed"
                    ):
                        quarantine_command_execution.validate_receipt_phase(
                            arg, run, authority
                        )
                    rejected_forgery.append(True)
                finally:
                    run.caller._state = state
                quarantine_command_execution.validate_receipt_phase(arg, run, authority)
            return observe

        # The shared original-boundary assertions require four ordinary and one
        # terminal checkpoint, one PATCH each, all five lost final GETs, the
        # authenticated durable terminal action and exact native command receipt.
        checkpoint_tests.CommandCheckpointTests().exercise(
            tracer=observe, fixture_type=CombinedFixture
        )
        self.assertEqual(len(fixtures), 1)
        self.assertEqual(len(leases), 1)
        self.assertEqual(rejected_forgery, [True])
        fixture = fixtures[0]
        self.assertEqual(fixture.receipt_reads, 2)
        self.assertEqual(fixture.receipt_attempts, 1)
        self.assertGreaterEqual(fixture.accounted.accounting.uncertain, 6)
        self.assertEqual(fixture.accounted.accounting.remaining, 190)
