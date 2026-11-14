"""Only original response objects can witness the already paid preparation."""

import copy
import json
import types
import unittest

import test_historical_invocation_census as census_tests
from review_sensei_github import quarantine_command_namespace as namespace

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError


class ConfirmationReceiptTests(unittest.TestCase):
    def test_original_receipts_reject_direct_copied_reconstructed_and_tampered_calls(
        self,
    ):
        bound, confirmed, prepared = [], [], []

        def observe(frame, event, result):
            if event != "return":
                return
            values = frame.f_locals
            if (
                frame.f_code is namespace.bind_response_confirmation.__code__
                and "closure" in values
            ):
                caller, function = values["caller"], values["function"]
                pending, observation = values["pending"], values["observation"]
                wrapper, response = values["wrapper"], values["response"]
                args = (
                    caller,
                    function,
                    pending,
                    observation,
                    response,
                    values["context"],
                )
                with self.assertRaisesRegex(
                    ReviewInputError, "original source changed"
                ):
                    namespace.bind_response_confirmation(*args)
                with self.assertRaisesRegex(ReviewInputError, "original nested source"):
                    namespace.confirm_response(caller, function, pending, observation)
                if not bound:
                    original_state = caller._state
                    original_observation = copy.deepcopy(observation)

                    def reset():
                        caller._state = original_state
                        observation.clear()
                        observation.update(copy.deepcopy(original_observation))

                    hook = namespace.confirm_response
                    original_code = hook.__code__
                    # Preserve closure arity while changing the captured code
                    # object, as external response code can do in place.
                    hook.__code__ = original_code.replace(co_name="forged_confirmation")
                    try:
                        with self.assertRaisesRegex(
                            ReviewInputError, "confirmation source changed"
                        ):
                            wrapper.read(512 * 1024 + 1)
                    finally:
                        hook.__code__ = original_code
                        reset()
                    copied = copy.copy(wrapper)
                    try:
                        with self.assertRaisesRegex(
                            ReviewInputError, "object or closure changed"
                        ):
                            copied.read(512 * 1024 + 1)
                    finally:
                        reset()
                    cells = tuple(
                        (lambda value: lambda: value)(cell.cell_contents).__closure__[0]
                        for cell in function.__closure__
                    )
                    reconstructed = types.FunctionType(
                        function.__code__, function.__globals__, closure=cells
                    )
                    dict(zip(function.__code__.co_freevars, cells, strict=True))[
                        "confirmed"
                    ].cell_contents = reconstructed
                    copied = copy.copy(wrapper)
                    copied._confirmed = reconstructed
                    try:
                        with self.assertRaisesRegex(
                            ReviewInputError, "unissued or replayed"
                        ):
                            copied.read(512 * 1024 + 1)
                    finally:
                        reset()
                    observation["phase"] = "invented-response-phase"
                    try:
                        with self.assertRaisesRegex(
                            ReviewInputError, "observation changed"
                        ):
                            wrapper.read(512 * 1024 + 1)
                    finally:
                        reset()
                    self.assertFalse(wrapper._read)
                    self.assertIs(caller._state, original_state)
                bound.append(observation["request_id"])
            if (
                frame.f_code is namespace.confirm_response.__code__
                and "expected" in values
                and values["pending"].request_id
                in values["entry"]["confirmed_receipts"]
            ):
                caller, entry = values["caller"], values["entry"]
                receipts = dict(entry["confirmed_receipts"])
                with self.assertRaisesRegex(ReviewInputError, "original nested source"):
                    namespace.confirm_response(
                        caller,
                        values["function"],
                        values["pending"],
                        values["observation"],
                    )
                self.assertEqual(entry["confirmed_receipts"], receipts)
                confirmed.append(values["pending"].request_id)
            if (
                frame.f_code is namespace.capture_paid_prefix.__code__
                and "projected" in values
            ):
                entry = values["entry"]
                self.assertFalse(entry["recording_confirmations"])
                self.assertEqual(len(entry["confirmed_receipts"]), 5)
                for identifier, (observation, raw, deadline, status, broker) in entry[
                    "confirmed_receipts"
                ].items():
                    self.assertEqual(raw, canonical_bytes(observation))
                    self.assertEqual(identifier, observation["request_id"])
                    self.assertEqual(deadline, observation["deadline_unix_ms"])
                    self.assertEqual(status, 200)
                    self.assertEqual(broker, observation["mutation"])
                self.assertEqual(
                    len(json.loads(entry["paid_projection"])["completed"]), 35
                )
                prepared.append(entry)

        census_tests.HistoricalInvocationCensusTests().exercise(extra_observer=observe)
        self.assertEqual(len(bound), 5)
        self.assertEqual(confirmed, bound)
        self.assertEqual(len(prepared), 1)
