"""Installed public assessment composition; synthetic HTTP and comment stores.

All four shared fixture modules are deliberately declared in the distribution
contract. Adding the sdist root admits the tests namespace, never src/.
"""

import sys
from pathlib import Path
from unittest.mock import patch

_TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TESTS))
from packaging_guard import assert_distribution_import  # noqa: E402

assert_distribution_import()

import review_sensei_github  # noqa: E402

if (
    not Path(review_sensei_github.__file__)
    .resolve()
    .is_relative_to(Path(sys.prefix).resolve())
):
    raise ImportError("large lifecycle requires the installed hosted adapter")

sys.path.insert(0, str(_TESTS.parent))
from tests import test_large_public_lifecycle as _contract  # noqa: E402


class InstalledLargePublicLifecycleTests(_contract.LargePublicLifecycleTests):
    pass


def setUpModule():
    global _network
    _network = patch(
        "socket.socket.connect", side_effect=AssertionError("live network forbidden")
    )
    _network.start()


def tearDownModule():
    _network.stop()
