"""Class identity is classification DATA, never an omission receipt."""

import unittest
from unittest.mock import patch

from review_sensei_github import (
    api_run_requests,
    review_run,
    session_ledger,
)
from review_sensei_github import (
    empty_original_admission as admission,
)

from review_sensei.errors import ReviewInputError
from review_sensei.session import LocalSessionLedger


class EmptyOriginalLedgerClassifierTests(unittest.TestCase):
    def test_exact_original_classes_are_data_only(self):
        for cls in (
            session_ledger.GitHubIssueCommentSessionLedger,
            api_run_requests.ApiRunSessionLedger,
            review_run.ReviewRunLedger,
            review_run.ActionRunLedger,
        ):
            with self.subTest(cls=cls.__name__):
                value = object.__new__(cls)
                self.assertTrue(admission.original_hosted_ledger(value))
                self.assertFalse(admission.consume(object(), value, None, object()))

    def test_local_custom_and_hostile_metaclass_do_not_classify(self):
        callbacks = []

        class Meta(type):
            def __eq__(self, other):
                callbacks.append("equality")
                raise AssertionError("callback")

            def __hash__(self):
                callbacks.append("hash")
                raise AssertionError("callback")

        class Fake(metaclass=Meta):
            pass

        class Custom(api_run_requests.ApiRunSessionLedger):
            pass

        for value in (
            object.__new__(LocalSessionLedger),
            object.__new__(Custom),
            Fake(),
        ):
            self.assertFalse(admission.original_hosted_ledger(value))
        self.assertEqual(callbacks, [])

    def test_local_fallback_and_class_ignore_unrelated_factory(self):
        with patch.object(review_run, "GitHubHttp", object()):
            self.assertFalse(
                admission.original_hosted_ledger(object.__new__(LocalSessionLedger))
            )
            self.assertTrue(
                admission.original_hosted_ledger(
                    object.__new__(review_run.ReviewRunLedger)
                )
            )

    def test_replaced_class_export_refuses_without_callback(self):
        callbacks = []

        class Poison:
            def __getattribute__(self, name):
                callbacks.append(name)
                raise AssertionError("callback")

        value = object.__new__(review_run.ReviewRunLedger)
        with patch.object(review_run, "ReviewRunLedger", Poison()):
            self.assertFalse(admission.original_hosted_ledger(value))
        self.assertEqual(callbacks, [])

    def test_own_source_is_checked_before_replaced_helper(self):
        callbacks = []

        def replaced(*args, **kwargs):
            callbacks.append(True)
            raise AssertionError("callback")

        with patch.object(admission, "require_original_class_identity", replaced):
            with self.assertRaises(ReviewInputError):
                admission.original_hosted_ledger(object())
        self.assertEqual(callbacks, [])
