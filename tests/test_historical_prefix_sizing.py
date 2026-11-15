"""Callback-free DATA checks, not an issued historical invocation or lease."""

import copy
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from review_sensei_github import historical_prefix_sizing as sizing
from review_sensei_github import quarantine_historical_execution as execution
from review_sensei_github.api_run_requests import ApiRunRequestTransport
from review_sensei_github.finding_lifecycle import FindingLifecycleJournal
from review_sensei_github.review_run import ActionRunLedger

from review_sensei.errors import ReviewInputError
from review_sensei.session import SessionIdentity, SessionRecord


def data_owners():
    record = SessionRecord.create(
        SessionIdentity("owner/repo", 1, repository_id=7),
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    caller = object.__new__(ApiRunRequestTransport)
    caller._effect_record = record
    caller._root = record.record_sha256
    ledger = object.__new__(ActionRunLedger)
    ledger.caller = caller
    journal = object.__new__(FindingLifecycleJournal)
    journal.ledger = ledger
    journal._record = record
    journal.identity = record.identity()
    return journal, ledger, caller, record


class HistoricalPrefixSizingTests(unittest.TestCase):
    def test_plain_snapshot_is_not_preflight_or_execution_authority(self):
        journal, ledger, caller, record = data_owners()
        result = sizing._owned_inputs(journal)
        self.assertEqual(result[:3], (ledger, caller, record))
        self.assertIsInstance(result[3], bytes)
        with self.assertRaises(ReviewInputError):
            sizing.retained_for_candidate(journal, {})
        with self.assertRaises(ReviewInputError):
            execution.retained_prefix_source(journal)

    def test_shadowed_record_serializer_refuses_without_callback(self):
        journal, _ledger, _caller, record = data_owners()
        calls = []

        def hostile():
            calls.append("serializer")
            raise AssertionError("serializer executed")

        object.__setattr__(record, "to_dict", hostile)
        with self.assertRaises(ReviewInputError):
            sizing._owned_inputs(journal)
        self.assertEqual(calls, [])

    def test_hostile_nested_record_values_refuse_without_callback(self):
        journal, _ledger, _caller, record = data_owners()
        calls = []

        class Meta(type):
            def __eq__(cls, other):
                calls.append("type equality")
                return False

        class Poison(metaclass=Meta):
            def __getattribute__(self, name):
                calls.append(name)
                raise AssertionError(name)

        object.__setattr__(record, "dispositions", ({"value": Poison()},))
        with self.assertRaises(ReviewInputError):
            sizing._owned_inputs(journal)
        self.assertEqual(calls, [])

    def test_foreign_owner_and_nonstring_namespace_keys_are_not_followed(self):
        calls = []

        class Poison:
            def __getattribute__(self, name):
                calls.append(name)
                raise AssertionError(name)

        for field in ("journal", "ledger", "caller"):
            journal, ledger, _caller, _record = data_owners()
            if field == "journal":
                journal = Poison()
            elif field == "ledger":
                journal.ledger = Poison()
            else:
                ledger.caller = Poison()
            with self.subTest(field=field), self.assertRaises(ReviewInputError):
                sizing._owned_inputs(journal)
        journal, _ledger, _caller, _record = data_owners()
        journal.__dict__[1] = "not a field"
        with self.assertRaises(ReviewInputError):
            sizing._owned_inputs(journal)
        self.assertEqual(calls, [])

    def test_same_shaped_copy_and_changed_root_do_not_match_original_owner(self):
        journal, _ledger, caller, record = data_owners()
        journal._record = copy.copy(record)
        with self.assertRaises(ReviewInputError):
            sizing._owned_inputs(journal)
        journal._record = record
        caller._root = "f" * 64
        with self.assertRaises(ReviewInputError):
            sizing._owned_inputs(journal)

    def test_hostile_root_and_identity_equalities_are_never_invoked(self):
        calls = []

        class Poison:
            def __eq__(self, other):
                calls.append("equality")
                raise AssertionError("equality executed")

            def __getattribute__(self, name):
                calls.append(name)
                raise AssertionError(name)

        for fault in ("root", "identity", "identity_field"):
            journal, _ledger, caller, _record = data_owners()
            if fault == "root":
                caller._root = Poison()
            elif fault == "identity":
                journal.identity = Poison()
            else:
                object.__setattr__(journal.identity, "repository", Poison())
            with self.subTest(fault=fault), self.assertRaises(ReviewInputError):
                sizing._owned_inputs(journal)
        self.assertEqual(calls, [])

    def test_replaced_snapshot_and_foreign_source_are_callback_free(self):
        from review_sensei_github import historical_record_snapshot

        calls = []

        def hostile(*args):
            calls.append(args)

        journal, _ledger, _caller, _record = data_owners()
        with patch.object(historical_record_snapshot, "_record_snapshot", hostile):
            with self.assertRaises(ReviewInputError):
                sizing._owned_inputs(journal)
        self.assertEqual(calls, [])

        class Poison:
            def __getattribute__(self, name):
                calls.append(name)
                raise AssertionError(name)

        with self.assertRaises(ReviewInputError):
            sizing.retained_for_candidate(Poison(), Poison())
        with self.assertRaises(ReviewInputError):
            execution.retained_prefix_source(Poison())
        self.assertEqual(calls, [])
