"""Exercise complete partitioned partial-resolution delivery after installation."""

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
    raise ImportError("rich publication requires the installed hosted adapter")

sys.path.insert(0, str(_TESTS.parent))
import test_rich250_late_feedback as writer_contract  # noqa: E402


class InstalledRichPartialResolutionTests(writer_contract.Rich250LateFeedbackTests):
    pass


def setUpModule():
    global _network
    _network = patch(
        "socket.socket.connect", side_effect=AssertionError("live network forbidden")
    )
    _network.start()


def tearDownModule():
    _network.stop()
