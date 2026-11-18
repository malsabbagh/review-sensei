"""Historical default-only admission uses the original installed profile reader."""

import unittest

from review_sensei_github import api_run_requests, trust_effects
from review_sensei_github.quarantine_historical_execution import (
    _require_default_profile,
)

from review_sensei.errors import ReviewInputError


class Caller:
    pass


class HistoricalTrustSourceTests(unittest.TestCase):
    def test_replaced_profile_export_cannot_hide_an_opted_profile(self):
        original = trust_effects.verification_profile
        called = []
        try:
            trust_effects.verification_profile = lambda caller: called.append(caller)
            with self.assertRaisesRegex(ReviewInputError, "profile source changed"):
                _require_default_profile(Caller())
            self.assertEqual(called, [])
        finally:
            trust_effects.verification_profile = original

    def test_mutated_original_profile_code_is_rejected_before_execution(self):
        original = api_run_requests._original_trust_verification_profile
        code = original.__code__
        names = code.co_freevars
        source = "def outer():\n" + "".join(f"    {name} = None\n" for name in names)
        source += (
            "    def fake(caller):\n        captured = ("
            + ",".join(names)
            + ",)\n        return None\n    return fake\n"
        )
        namespace = {}
        exec(source, namespace)
        try:
            original.__code__ = namespace["outer"]().__code__
            with self.assertRaises(ReviewInputError):
                _require_default_profile(Caller())
        finally:
            original.__code__ = code
        _require_default_profile(Caller())

    def test_profile_appearing_after_initial_check_is_revalidated(self):
        caller = Caller()
        original = api_run_requests._original_trust_verification_profile
        cells = dict(
            zip(original.__code__.co_freevars, original.__closure__, strict=True)
        )
        profiles = cells["_profiles"].cell_contents
        _require_default_profile(caller)
        try:
            # A foreign profile is intentionally invalid. A previous None result
            # must not be cached and bypass the original caller/profile validator.
            profiles[caller] = object()
            with self.assertRaisesRegex(
                ReviewInputError, "trust effect source binding"
            ):
                _require_default_profile(caller)
        finally:
            profiles.pop(caller, None)
        _require_default_profile(caller)
