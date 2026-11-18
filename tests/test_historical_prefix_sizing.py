"""Callback-free DATA checks, not an issued historical invocation or lease."""

import copy
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from review_sensei_github import finding_lifecycle as journal_source
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
    def fixed_update_data(self, count=2):
        head = "a" * 40
        review = ["PUT", "/repos/owner/repo/pulls/7/reviews/102", {"body": "complete"}]
        check = ["PATCH", "/repos/owner/repo/check-runs/502", {"conclusion": "success"}]
        approval = [
            "POST",
            "/repos/owner/repo/pulls/7/reviews",
            {"event": "APPROVE", "commit_id": head},
        ]
        commitment = {
            "check_run_id": 502,
            "head_sha": head,
            "app_id": 999,
            "app_slug": "reviewsensei",
        }
        payload = dict(
            repository="owner/repo",
            pull_request=7,
            head_sha=head,
            review_before={"id": 102},
            review_update=review,
            check_request=check,
            conditional_approval=approval,
            cleanup_requests=[],
        )
        retained = {"effects": [{"status": "confirmed", "retained": "unchanged"}]}
        effects = copy.deepcopy(retained["effects"])
        confirmations = [
            dict(id=102, producer_id=999, app_id=None, mutation=None),
            dict(id=502, producer_id=None, app_id=999, mutation=None),
        ]
        for index, request in enumerate((review, check, approval)[: count + 1]):
            identity = journal_source._request_identity(request[2])
            effect = dict(
                method=request[0],
                path=request[1],
                request=identity,
                request_sha256=journal_source._digest(
                    dict(method=request[0], path=request[1], request=identity)
                ),
                status="confirmed" if index < count else "pending",
                response=[200, confirmations[index]] if index < count else None,
            )
            if index == 1:
                effect["check_update_precondition"] = commitment
            effects.append(effect)
        return (
            retained,
            {"effects": effects},
            payload,
            "github-bot:999",
            {"review": None, "check": commitment},
        )

    def test_fixed_updates_match_only_detached_constant_receipts(self):
        for count in (1, 2):
            args = self.fixed_update_data(count)
            self.assertTrue(sizing._fixed_command_updates(*args))
        # A matching DATA predicate is still not original sizing authority.
        with self.assertRaises(ReviewInputError):
            sizing.retained_for_candidate(data_owners()[0], args[1])

    def test_fixed_update_prefix_refuses_unknown_or_changed_confirmations(self):
        for index in (1, 2):
            for field, value in (
                ("id", 987),
                ("producer_id", 123),
                ("app_id", 123),
                ("mutation", {}),
            ):
                args = self.fixed_update_data()
                args[1]["effects"][index]["response"][1][field] = value
                with self.subTest(index=index, field=field):
                    self.assertFalse(sizing._fixed_command_updates(*args))
            for field, value in (
                ("status", "pending"),
                ("response", [201, {}]),
                ("request_sha256", "f" * 64),
                ("method", "POST"),
            ):
                args = self.fixed_update_data()
                args[1]["effects"][index][field] = value
                with self.subTest(index=index, field=field):
                    self.assertFalse(sizing._fixed_command_updates(*args))

    def test_fixed_update_prefix_preserves_source_requests_and_old_history(self):
        for fault in ("old", "request", "cleanup", "check", "next", "extra", "unknown"):
            args = self.fixed_update_data()
            candidate, payload = args[1], args[2]
            if fault == "old":
                candidate["effects"][0]["retained"] = "changed"
            elif fault == "request":
                payload["review_update"][2]["body"] = "changed"
            elif fault == "cleanup":
                payload["cleanup_requests"].append(["POST", "/graphql", {}])
            elif fault == "check":
                args[4]["check"] = None
            elif fault == "next":
                candidate["effects"][-1]["path"] += "/other"
            elif fault == "extra":
                candidate["effects"].append(copy.deepcopy(candidate["effects"][-1]))
            else:
                candidate["effects"][1]["unexpected"] = True
            with self.subTest(fault=fault):
                self.assertFalse(sizing._fixed_command_updates(*args))

    def test_fixed_update_prefix_rejects_hostile_data_without_callback(self):
        calls = []

        class Poison:
            def __eq__(self, other):
                calls.append("equality")
                raise AssertionError("unexpected callback")

        args = self.fixed_update_data()
        args[1]["effects"][1]["response"] = Poison()
        with self.assertRaises(ReviewInputError):
            sizing._fixed_command_updates(*args)
        self.assertEqual(calls, [])

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
