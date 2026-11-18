"""Pure conditional representation controls, never a live LAST invocation."""

import unittest
from dataclasses import asdict, replace
from unittest.mock import patch

from review_sensei_github import quarantine_command_finalization as finalization
from review_sensei_github import quarantine_last_manifest as manifest

from review_sensei import session
from review_sensei.api_work import (
    ApiExecutionOwner,
    ApiRunAccounting,
    ApiRunIdentity,
    FrozenApiConfiguration,
    PendingApiRequest,
    StartupPlan,
    StartupSlot,
    initial_accounting,
)
from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError


def state():
    value = initial_accounting(
        ApiRunIdentity(123, 456),
        FrozenApiConfiguration("a" * 40, "b" * 64, 512),
        ApiExecutionOwner(1, "command", 0),
        StartupPlan(tuple(StartupSlot("command", n, ("auth",)) for n in (1, 2))),
        prior_nonstartup_liability_upper_bound=0,
    )
    value = value.reserve("old", 2).dispatch("old", PendingApiRequest("old", 99))
    value = (
        value.reserve("ordinary", 105).reserve("terminal", 50).reserve("successor", 190)
    )
    return replace(
        value,
        action={
            "phase": "pending",
            "execution_started": False,
            "result": None,
            "outcome": None,
            "intent": {
                "purpose": "command",
                "run_id": 456,
                "owner": asdict(value.owner),
                "head_sha": "a" * 40,
                "configuration_digest": value.configuration.digest,
                "operation_key": "b" * 64,
                "source_root": "c" * 64,
                "source": {
                    "kind": "issue_comment",
                    "id": 1,
                    "updated_at": "2026-01-01T00:00:00Z",
                    "body_sha256": "d" * 64,
                },
            },
        },
    )


class LastConditionalProjectionTests(unittest.TestCase):
    def test_conditional_labels_and_all_old_liabilities_preserved(self):
        original = state()
        data = manifest.conditional_terminal_data(
            original, ordinary="ordinary", terminal="terminal", sequence=80
        )
        self.assertIs(data["admission"], False)
        self.assertIs(data["runtime_fence_installed"], False)
        self.assertNotIn("all_growth_roots_fit", data)
        self.assertEqual(len(data["unproved_premises"]), 6)
        projected = ApiRunAccounting.from_document(data["accounting"])
        self.assertEqual(projected.remaining, original.remaining)
        self.assertEqual(projected.blocks[0], original.blocks[0])
        self.assertEqual(projected.blocks[-1], original.blocks[-1])
        self.assertEqual(projected.blocks[2].undispatched, 34)
        self.assertEqual(len(projected.blocks[2].pending), 16)
        self.assertEqual(len(projected.blocks[1].pending), 105)
        original.require_successor(projected)

    def test_bad_scope_and_accounting_shadow_refuse_before_callback(self):
        calls = []
        value = state()
        object.__setattr__(value, "to_document", lambda: calls.append(True))
        with self.assertRaises(ReviewInputError):
            manifest.conditional_terminal_data(
                value, ordinary="ordinary", terminal="terminal", sequence=80
            )
        self.assertEqual(calls, [])
        for value in (
            replace(state(), action=None),
            state().dispatch("terminal", PendingApiRequest("early", 7)),
        ):
            with self.assertRaises(ReviewInputError):
                manifest.conditional_terminal_data(
                    value, ordinary="ordinary", terminal="terminal", sequence=80
                )

    def test_hostile_graph_and_nonoriginal_cap_refuse(self):
        calls = []

        class Bomb:
            def __getattribute__(self, name):
                calls.append(name)
                raise AssertionError(name)

        value = state()
        object.__setattr__(value.blocks[0].pending[0], "request_id", Bomb())
        with self.assertRaises(ReviewInputError):
            manifest.conditional_terminal_data(
                value, ordinary="ordinary", terminal="terminal", sequence=80
            )
        self.assertEqual(calls, [])
        original = state()
        widened = replace(
            original, configuration=replace(original.configuration, limit=1024)
        )
        with self.assertRaises(ReviewInputError):
            manifest.conditional_terminal_data(
                widened, ordinary="ordinary", terminal="terminal", sequence=80
            )

    def test_original_size_refusal_is_labelled_not_a_record(self):
        value = state().reserve("bulk", 160)
        for n in range(160):
            value = value.dispatch(
                "bulk", PendingApiRequest(str(n).zfill(128), 9007199254740991)
            )
        # Add another legal old domain to exceed the fixed root cap.
        for n in range(105):
            value = value.dispatch(
                "ordinary", PendingApiRequest("x" + str(n).zfill(127), 9007199254740991)
            )
        record = session.SessionRecord.create(
            session.SessionIdentity("owner/repo", 7, 123)
        )
        raw = record.to_dict()
        raw["api_run_accounting"] = value.to_document()
        raw["record_sha256"] = session._digest_payload(
            {k: v for k, v in raw.items() if k != "record_sha256"}
        )
        with self.assertRaises(ReviewInputError) as caught:
            session.SessionRecord.from_dict(raw)
        # unittest clears traceback; obtain the original exception directly.
        try:
            session.SessionRecord.from_dict(raw)
        except ReviewInputError as error:
            before = canonical_bytes(raw)
            result = manifest._failed_size_data(error, raw, before)
            raw["generation"] += 1
            with self.assertRaisesRegex(ReviewInputError, "input changed"):
                manifest._failed_size_data(error, raw, before)
            # Recomputing supplied DATA cannot conceal drift of the actual
            # original failed record retained by its traceback.
            with self.assertRaisesRegex(ReviewInputError, "failed record changed"):
                manifest._failed_size_data(error, raw, canonical_bytes(raw))
            with self.assertRaisesRegex(ReviewInputError, "lacks original render"):
                manifest.root_size_refusal(error, raw, before)
        self.assertEqual(
            str(caught.exception), "session record exceeds the configured size limit"
        )
        self.assertFalse(result["validated"])
        self.assertEqual(result["limit_bytes"], session.MAX_SESSION_RECORD_BYTES)
        self.assertGreater(result["root_bytes"], result["limit_bytes"])
        self.assertIsNone(result["comment_body_bytes"])
        with self.assertRaisesRegex(ReviewInputError, "exceeds the configured size"):
            finalization._root_growth_measurement(record, value.to_document(), None, {})

    def test_fabricated_and_non_size_errors_not_swallowed(self):
        for message in (
            "session record exceeds the configured size limit",
            "session record integrity check failed",
        ):
            error = ReviewInputError(message)
            with self.assertRaises(ReviewInputError) as caught:
                manifest._failed_size_data(
                    error,
                    {"large": "x" * 40000},
                    canonical_bytes({"large": "x" * 40000}),
                )
            self.assertIs(caught.exception, error)
        with patch.object(
            manifest, "accounting_bytes", lambda value: self.fail("callback")
        ):
            with self.assertRaises(ReviewInputError):
                manifest.conditional_terminal_data(
                    object(), ordinary="ordinary", terminal="terminal", sequence=80
                )

    def test_diagnostic_cannot_be_selected_by_external_caller(self):
        with self.assertRaisesRegex(ReviewInputError, "lacks original measurement"):
            finalization._root_growth_measurement(None, None, None, _diagnostic=True)

    def test_hostile_namespace_parameters_never_hash_compare_or_convert(self):
        calls = []

        class Bomb:
            def __hash__(self):
                calls.append("hash")
                raise AssertionError("hash")

            def __eq__(self, other):
                calls.append("eq")
                raise AssertionError("eq")

            def __bool__(self):
                calls.append("bool")
                raise AssertionError("bool")

        for key in ("ordinary", "terminal", "sequence"):
            arguments = dict(ordinary="ordinary", terminal="terminal", sequence=80)
            arguments[key] = Bomb()
            with self.assertRaises(ReviewInputError):
                manifest.conditional_terminal_data(state(), **arguments)
        for changes in (
            {"sequence": True},
            {"terminal": "ordinary"},
            {"ordinary": ""},
            {"terminal": "x" * 129},
        ):
            arguments = dict(ordinary="ordinary", terminal="terminal", sequence=80)
            arguments.update(changes)
            with self.assertRaises(ReviewInputError):
                manifest.conditional_terminal_data(state(), **arguments)
        self.assertEqual(calls, [])
