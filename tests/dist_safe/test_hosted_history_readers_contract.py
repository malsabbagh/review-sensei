"""Hosted history qualification against exact installed engine and adapter wheels."""

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
    raise ImportError("hosted history requires the installed GitHub adapter")

sys.path.insert(0, str(_TESTS.parent))
import test_hosted_history_readers as _contract  # noqa: E402


class InstalledHostedHistoryReaderTests(_contract.HostedHistoryReaderTests):
    pass


def setUpModule():
    global _network
    _network = patch(
        "socket.socket.connect", side_effect=AssertionError("live network forbidden")
    )
    _network.start()


def tearDownModule():
    _network.stop()
