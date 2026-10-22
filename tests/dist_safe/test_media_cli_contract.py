"""Exercise the supported media CLI using only installed engine/hosted wheels."""

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
    raise ImportError("media CLI requires the installed hosted adapter")

sys.path.insert(0, str(_TESTS.parent))
import test_media_cli_activation as media_contract  # noqa: E402


class InstalledMediaCLIActivationTests(media_contract.MediaCLIActivationTests):
    """Ordinary synthetic transport; production broker is qualified separately."""


def setUpModule():
    global _network
    _network = patch(
        "socket.socket.connect", side_effect=AssertionError("live network forbidden")
    )
    _network.start()


def tearDownModule():
    _network.stop()
