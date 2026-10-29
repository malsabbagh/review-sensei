"""Inactive native protocol contracts use the installed paired packages."""

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
    raise ImportError(
        "native group qualification requires the installed hosted adapter"
    )


def load_tests(loader, tests, pattern):
    modules = [path.stem for path in sorted(_TESTS.glob("test_native_*.py"))]
    modules.extend(("test_publication_staging", "test_packed_comment_census"))
    for module in modules:
        tests.addTests(loader.loadTestsFromModule(importlib.import_module(module)))
    return tests


def setUpModule():
    global _network
    _network = patch(
        "socket.socket.connect", side_effect=AssertionError("live network forbidden")
    )
    _network.start()


def tearDownModule():
    _network.stop()
