"""Defining-source seals reject ordinary class and instance substitutions."""

import copy
import unittest
from unittest.mock import patch

from review_sensei_github.ordinary_drift_plan import _require_methods

from review_sensei.errors import ReviewInputError
from review_sensei.presentation import FindingView


class OrdinaryDriftSourceSealTests(unittest.TestCase):
    def test_original_presentation_class_and_copy_remain_qualified(self):
        value = object.__new__(FindingView)
        _require_methods(value)
        _require_methods(copy.copy(value))

    def test_changed_presentation_constructor_is_not_called_or_blessed(self):
        calls = []
        value = object.__new__(FindingView)
        with patch.object(
            FindingView, "__init__", lambda *args, **kwargs: calls.append(1)
        ):
            with self.assertRaises(ReviewInputError):
                _require_methods(value)
        self.assertEqual(calls, [])
        _require_methods(value)

    def test_instance_copy_protocol_is_rejected_before_callback(self):
        calls = []
        value = object.__new__(FindingView)
        object.__setattr__(value, "__reduce_ex__", lambda *args: calls.append(1))
        with self.assertRaisesRegex(ReviewInputError, "copy protocol"):
            _require_methods(value)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
