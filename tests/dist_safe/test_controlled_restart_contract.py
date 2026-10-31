"""Exercise controlled restart against installed core and adapter packages."""

import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TESTS))
from packaging_guard import assert_distribution_import, is_disarmed  # noqa: E402

assert_distribution_import()
import review_sensei_github  # noqa: E402

if not is_disarmed() and not Path(
    review_sensei_github.__file__
).resolve().is_relative_to(Path(sys.prefix).resolve()):
    raise ImportError("controlled restart requires the installed hosted adapter")


class _NetworkFreeSuite(unittest.TestSuite):
    def run(self, result, debug=False):
        with patch(
            "socket.socket.connect",
            side_effect=AssertionError("live network forbidden"),
        ):
            return super().run(result, debug=debug)


def load_tests(loader, tests, pattern):
    suite = _NetworkFreeSuite()
    for module in (
        "test_controlled_restart",
        "test_controlled_restart_journeys",
        "test_controlled_restart_handoff",
        "test_controlled_restart_late_binding",
        "test_controlled_restart_installer",
        "test_controlled_restart_caller_output",
        "test_controlled_restart_caller_journey",
    ):
        suite.addTests(loader.loadTestsFromModule(importlib.import_module(module)))
    return suite
