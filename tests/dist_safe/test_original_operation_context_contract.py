"""Exercise actual owned-operation context and wall-time recovery after install."""

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
    raise ImportError("operation context requires the installed hosted adapter")

sys.path.insert(0, str(_TESTS.parent))
import test_handle_aware_operation_entry as entry_contract  # noqa: E402
import test_operation_wall_clock as clock_contract  # noqa: E402


class InstalledHandleAwareOperationEntryTests(
    entry_contract.HandleAwareOperationEntryTests
):
    pass


class InstalledOperationWallClockTests(clock_contract.OperationWallClockTests):
    pass


def setUpModule():
    global _network
    _network = patch(
        "socket.socket.connect", side_effect=AssertionError("live network forbidden")
    )
    _network.start()


def tearDownModule():
    _network.stop()
