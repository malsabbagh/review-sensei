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

    def test_preparation_rejects_both_replaced_aliases_without_calling_them(self):
        import sys
        import tempfile
        from unittest.mock import patch

        from review_sensei_github import (
            api_run_requests,
            quarantine_historical_execution,
        )
        from test_quarantine_historical_execution import GuardedHistoricalFixture
        from test_quarantine_journey import registered_resource
        from test_quarantine_published_tail_journey import BROKER_QUALIFICATIONS

        for alias in ("_bind_namespace_confirmation", "_namespace_confirm_response"):
            with (
                self.subTest(alias=alias),
                tempfile.TemporaryDirectory() as directory,
                patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
            ):
                fixture = GuardedHistoricalFixture(directory)
                _, registration = fixture.original_origins(
                    failed_count=1, published_count=1
                )
                with registered_resource([registration]):
                    self.assertEqual(fixture.invoke(), 0, fixture.error)
                    origin, inventory = next(
                        (key, value)
                        for key, value in fixture.selected.ledger._quarantine_authenticated.inventories.items()
                        if value["assessments"]
                    )
                    fixture.quarantine_predecessor_comment_id = None
                    fixture.select_command(
                        "@reviewsensei dismiss "
                        + inventory["assessments"][0]["instance"]
                        + ' --reason "quarantine:'
                        + origin
                        + ' Exact source disproves this historical concern."',
                        new_run=True,
                    )
                    original = getattr(api_run_requests, alias)
                    fake_calls, injected, activated, refusals = [], [], [], []

                    def fake(*args):
                        fake_calls.append(args)

                    def observe(frame, event, result):
                        if (
                            event == "return"
                            and frame.f_code is namespace.prepare.__code__
                            and result is not None
                        ):
                            injected.append(result)
                            setattr(api_run_requests, alias, fake)
                        if (
                            event == "return"
                            and frame.f_code
                            is quarantine_historical_execution.activate.__code__
                            and result is not None
                        ):
                            activated.append(result)

                    def trace(frame, event, arg):
                        if (
                            frame.f_globals is vars(api_run_requests)
                            and frame.f_code.co_name == "open"
                        ):
                            if (
                                event == "exception"
                                and str(arg[1])
                                == "caller namespace confirmation alias changed"
                            ):
                                refusals.append(arg[1])
                            return trace
                        return None

                    previous = sys.getprofile()
                    previous_trace = sys.gettrace()
                    try:
                        sys.setprofile(observe)
                        sys.settrace(trace)
                        code = fixture.invoke_command()
                    finally:
                        sys.settrace(previous_trace)
                        sys.setprofile(previous)
                        setattr(api_run_requests, alias, original)
                    self.assertEqual(len(injected), 1)
                    self.assertEqual(code, 1, fixture.error)
                    self.assertEqual(fake_calls, [])
                    self.assertEqual(activated, [])
                    self.assertEqual(fixture.native_puts, 0)
                    self.assertEqual(len(refusals), 1)
                    self.assertIn("broker.transport_phase=request_open", fixture.error)
                    self.assertIn(
                        "broker.transport_failure=invalid_response", fixture.error
                    )
                    self.assertFalse(
                        any(row["state"] == "APPROVED" for row in fixture.reviews)
                    )
                    self.assertFalse(
                        any(
                            "reviewsensei:command-receipt:v1" in row.get("body", "")
                            for row in fixture.extra_comments.values()
                        )
                    )
