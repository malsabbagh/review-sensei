"""Quarantine contracts exercise only the installed core and hosted adapter."""

import importlib
import sys
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
    raise ImportError("quarantine qualification requires the installed hosted adapter")


def load_tests(loader, tests, pattern):
    for path in sorted(_TESTS.glob("test_quarantine*.py")):
        tests.addTests(loader.loadTestsFromModule(importlib.import_module(path.stem)))
    return tests


def setUpModule():
    global _network
    _network = patch(
        "socket.socket.connect", side_effect=AssertionError("live network forbidden")
    )
    _network.start()


def tearDownModule():
    _network.stop()
