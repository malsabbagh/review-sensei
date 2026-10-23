"""Recorded-decision capacity recovery against installed distributions."""

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
    raise ImportError("command capacity recovery requires the installed GitHub adapter")

sys.path.insert(0, str(_TESTS.parent))
import test_command_capacity_recovery  # noqa: E402
import test_disposition_snapshots  # noqa: E402
import test_publication_future_capacity  # noqa: E402


class InstalledCommandCapacityRecoveryTests(
    test_command_capacity_recovery.CommandCapacityRecoveryTests
):
    pass


class InstalledDecisionSnapshotTests(
    test_disposition_snapshots.DispositionSnapshotReaderTests
):
    pass


class InstalledFutureDispositionFrameTests(
    test_publication_future_capacity.FutureDispositionFrameTests
):
    pass


def setUpModule():
    global _network
    _network = patch(
        "socket.socket.connect", side_effect=AssertionError("live network forbidden")
    )
    _network.start()


def tearDownModule():
    _network.stop()
