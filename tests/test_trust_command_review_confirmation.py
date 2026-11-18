"""Original trust pricing funds its read but does not admit a business route."""

import tempfile
import unittest
from unittest.mock import patch
from urllib.request import Request

from review_sensei_github.caller_operation import CallerOperation
from review_sensei_github.publication import _command_review_confirmation

from review_sensei.errors import EvidenceBudgetExhausted, ReviewInputError
from tests.test_run_authority import NOW
from tests.test_trust_command_receipt import ReceiptFixture


class LostPrewriteFixture(ReceiptFixture):
    lose_read = False

    def open(self, request, timeout):
        if self.lose_read and request.get_method() == "GET":
            raise OSError("synthetic prewrite read response loss")
        return super().open(request, timeout)


class ReviewConfirmationTests(unittest.TestCase):
    def setUp(self):
        clock = patch("time.time", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def fixture(self, fixture_type=ReceiptFixture):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        value = fixture_type(directory.name)
        value.initialize()
        return value

    def test_source_profile_keeps_whole_tail_and_actual_read_debit(self):
        for native_union, tail in ((False, 22), (True, 35)):
            with self.subTest(native_union=native_union):
                f = self.fixture()
                before = f.caller.usable
                with _command_review_confirmation(
                    f.caller, key="confirmation", native_union=native_union
                ) as operation:
                    self.assertEqual(
                        (operation.capacity, operation.write_tail), (tail + 1, tail)
                    )
                    # This real charged read exercises the exact prewrite debit
                    # branch; it is NOT claimed to be a newly admitted refresh.
                    with f.caller.open(
                        Request("https://api.github.test/repos/owner/repo/pulls/7"),
                        timeout=30,
                    ) as response:
                        response.read(65536)
                    self.assertEqual(operation.remaining, tail)
                    self.assertEqual(f.caller.usable, before - 1)
                    with self.assertRaisesRegex(
                        ReviewInputError, "complete route admission"
                    ):
                        f.caller.open(
                            Request(
                                "https://api.github.test/repos/owner/repo/pulls/7/reviews/42",
                                data=b'{"body":"x"}',
                                method="PUT",
                            ),
                            timeout=30,
                        )
                    self.assertFalse(operation.write_attempted)
                    self.assertEqual(f.caller.usable, before - 1)
                self.assertFalse(any(method == "PUT" for method, _, _ in f.native))
                self.assertEqual(f.caller.accounting.configuration.limit, 512)

    def test_one_short_refuses_before_allocating_or_dispatching(self):
        f = self.fixture()
        while f.caller.usable > 22:
            with f.caller.open(
                Request("https://api.github.test/repos/owner/repo/pulls/7"), timeout=30
            ) as response:
                response.read(65536)
        count = len(f.caller.observations)
        with self.assertRaises(EvidenceBudgetExhausted):
            _command_review_confirmation(f.caller, key="unfunded", native_union=False)
        self.assertEqual(len(f.caller.observations), count)
        self.assertFalse(
            any(
                operation.key == "unfunded"
                for operation, _ in f.caller._issued_operations
            )
        )

    def test_lost_prewrite_response_retains_physical_unknown_and_stops(self):
        f = self.fixture(LostPrewriteFixture)
        before, pending = f.caller.usable, f.caller.accounting.uncertain
        f.lose_read = True
        with self.assertRaises(OSError):
            with _command_review_confirmation(
                f.caller, key="lost-prewrite", native_union=False
            ) as operation:
                f.caller.open(
                    Request("https://api.github.test/repos/owner/repo/pulls/7"),
                    timeout=30,
                )
        self.assertEqual(operation.remaining, 22)
        self.assertFalse(operation.write_attempted)
        self.assertEqual(f.caller.usable, before - 1)
        self.assertEqual(f.caller.accounting.uncertain, pending + 1)
        self.assertFalse(any(method == "PUT" for method, _, _ in f.native))

    def test_full_confirmation_remains_after_verification_and_unknown_write(self):
        for tail in (22, 35):
            operation = CallerOperation(tail + 1, tail)
            operation.consume(mutation=False)
            operation.consume(mutation=True)
            for _ in range(tail - 1):
                operation.consume(mutation=False)
            self.assertEqual(operation.remaining, 0)
            self.assertFalse(operation.confirmed)
            with self.assertRaises(EvidenceBudgetExhausted):
                operation.consume(mutation=False)
            with self.assertRaises(EvidenceBudgetExhausted):
                CallerOperation(tail, tail).consume(mutation=False)
