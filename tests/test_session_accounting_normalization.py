"""Session construction retains full accounting validation and fresh values."""

import unittest
from copy import deepcopy
from dataclasses import fields, replace

from review_sensei.api_work import MAX_API_REQUEST_LIMIT, PendingApiRequest
from review_sensei.errors import ReviewInputError
from review_sensei.session import SessionIdentity, SessionRecord
from tests.test_api_work import accounting


class SessionAccountingNormalizationTests(unittest.TestCase):
    def record(self, version="caller-requests-v3"):
        state = accounting().reserve("review", 6)
        unknown = PendingApiRequest("unknown", MAX_API_REQUEST_LIMIT)
        completed = PendingApiRequest("completed", 321)
        state = state.dispatch("review", unknown)
        state = state.dispatch("review", completed).confirm(completed)
        # V3 remains reader-only; construct its retained data after the original
        # writer transitions, without trying to authorize a V3 dispatch.
        state = replace(state, version=version)
        return SessionRecord.create(
            SessionIdentity("owner/repo", 7, 123),
            api_run_accounting=state.to_document(),
        )

    def test_all_versions_retain_unknowns_and_funding_without_sharing_input(self):
        for version in (
            "caller-requests-v1",
            "caller-requests-v2",
            "caller-requests-v3",
        ):
            with self.subTest(version=version):
                original = self.record(version)
                document = deepcopy(original.to_dict())
                restored = SessionRecord.from_dict(document)
                self.assertEqual(restored.to_dict(), original.to_dict())
                document["api_run_accounting"]["blocks"][0]["pending"].clear()
                self.assertEqual(restored.to_dict(), original.to_dict())
                self.assertEqual(
                    restored.api_run_accounting["blocks"][0]["pending"],
                    [
                        {
                            "request_id": "unknown",
                            "deadline_unix_ms": MAX_API_REQUEST_LIMIT,
                        }
                    ],
                )

    def test_invalid_accounting_and_unbound_terminal_transaction_still_refuse(self):
        original = self.record()
        for mutation in (
            lambda d: d["api_run_accounting"].update(terminal_transaction="d" * 64),
            lambda d: d["api_run_accounting"]["blocks"][0].update(undispatched=-1),
            lambda d: d["api_run_accounting"]["identity"].update(repository_id=456),
        ):
            with self.subTest(mutation=mutation):
                document = deepcopy(original.to_dict())
                mutation(document)
                with self.assertRaises(ReviewInputError):
                    SessionRecord.from_dict(document)

    def test_subclass_callback_keeps_the_original_post_identity_validation(self):
        mutated = []

        class MutatingRecord(SessionRecord):
            def __getattribute__(self, name):
                value = super().__getattribute__(name)
                if name == "_digest_shape" and not mutated:
                    current = super().__getattribute__("api_run_accounting")
                    if current is not None:
                        mutated.append(True)
                        current["terminal_transaction"] = "invalid-terminal"
                return value

        original = self.record()
        arguments = {
            field.name: getattr(original, field.name)
            for field in fields(original)
            if field.init
        }
        with self.assertRaises(ReviewInputError):
            MutatingRecord(**arguments)
        self.assertEqual(mutated, [True])

    def test_from_dict_refuses_semantic_overdraft_even_with_subclass_constructor(self):
        class UncheckedRecord(SessionRecord):
            def __post_init__(self, _digest_shape_input):
                pass

        for record_type in (SessionRecord, UncheckedRecord):
            with self.subTest(record_type=record_type):
                document = deepcopy(self.record().to_dict())
                state = document["api_run_accounting"]
                # A structurally valid integer still violates the original
                # complete-run conservation constraint.
                state["blocks"][0]["undispatched"] = state["configuration"]["limit"]
                with self.assertRaises(ReviewInputError):
                    record_type.from_dict(document)

    def test_callback_mapping_keeps_preconstruction_accounting_validation(self):
        class MappingWithCallback(dict):
            def get(self, key, default=None):
                value = super().get(key, default)
                if key == "api_run_accounting" and value is not None:
                    value["blocks"][0]["undispatched"] = -1
                return value

        with self.assertRaises(ReviewInputError):
            SessionRecord.from_dict(MappingWithCallback(self.record().to_dict()))
