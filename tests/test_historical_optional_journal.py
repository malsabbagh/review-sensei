"""Optional Action-run hooks leave exact original callerless ledgers alone.

No ledger initialization, hosted fixture, command or finalizer is executed.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from review_sensei_github import (
    api_run_requests,
    quarantine_command_checkpoint,
    review_run,
    session_ledger,
)
from review_sensei_github import (
    quarantine_historical_execution as historical,
)

from review_sensei import session
from review_sensei.errors import ReviewInputError


class HistoricalOptionalJournalTests(unittest.TestCase):
    def test_original_callerless_ledgers_decline_without_initialization(self):
        for cls in (
            session.LocalSessionLedger,
            session_ledger.GitHubIssueCommentSessionLedger,
        ):
            with self.subTest(ledger=cls.__name__):
                ledger = object.__new__(cls)
                journal = SimpleNamespace(ledger=ledger)
                self.assertTrue(historical._original_callerless_ledger(ledger))
                self.assertFalse(historical.bind_journal(journal))
                self.assertFalse(historical.require_journal(journal))

    def test_actual_optional_phase_yields_for_original_local_ledger(self):
        ledger = object.__new__(session.LocalSessionLedger)
        journal = SimpleNamespace(
            ledger=ledger, reviewer=object(), _record=object(), identity=object()
        )
        with quarantine_command_checkpoint.phase(journal):
            self.assertFalse(historical.require_journal(journal))
        self.assertEqual(vars(ledger), {})

    def test_hosted_run_ledgers_do_not_gain_missing_caller_fallback(self):
        for cls in (
            api_run_requests.ApiRunSessionLedger,
            review_run.ReviewRunLedger,
            review_run.ActionRunLedger,
        ):
            with self.subTest(ledger=cls.__name__):
                ledger = object.__new__(cls)
                self.assertFalse(historical._original_callerless_ledger(ledger))
                with self.assertRaises(AttributeError):
                    historical.bind_journal(SimpleNamespace(ledger=ledger))

    def test_subclass_and_hostile_metaclass_do_not_classify(self):
        calls = []

        class Custom(session.LocalSessionLedger):
            pass

        class Meta(type):
            def __hash__(self):
                calls.append("hash")
                raise AssertionError("callback")

            def __eq__(self, other):
                calls.append("eq")
                raise AssertionError("callback")

        class Hostile(metaclass=Meta):
            pass

        for value in (object.__new__(Custom), Hostile()):
            self.assertFalse(historical._original_callerless_ledger(value))
        self.assertEqual(calls, [])

    def test_replaced_class_export_is_not_an_inactive_exception(self):
        original = object.__new__(session.LocalSessionLedger)
        with patch.object(session, "LocalSessionLedger", object()):
            self.assertFalse(historical._original_callerless_ledger(original))
            with self.assertRaises(AttributeError):
                historical.bind_journal(SimpleNamespace(ledger=original))

    def test_own_source_checked_before_replaced_classifier_dependency(self):
        calls = []

        def changed(*args, **kwargs):
            calls.append(True)
            raise AssertionError("callback")

        with patch.object(historical, "require_original_class_identity", changed):
            with self.assertRaises(ReviewInputError):
                historical._original_callerless_ledger(object())
        self.assertEqual(calls, [])
