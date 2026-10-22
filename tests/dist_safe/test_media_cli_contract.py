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

    def tearDown(self):
        super().tearDown()
        if is_disarmed():
            return
        prefix = Path(sys.prefix).resolve()
        for name, module in tuple(sys.modules.items()):
            if name.split(".", 1)[0] not in {
                "review_sensei",
                "review_sensei_github",
            }:
                continue
            location = getattr(module, "__file__", None)
            if location is not None:
                self.assertTrue(
                    Path(location).resolve().is_relative_to(prefix),
                    f"media journey imported checkout runtime: {name}: {location}",
                )


def setUpModule():
    global _network
    _network = patch(
        "socket.socket.connect", side_effect=AssertionError("live network forbidden")
    )
    _network.start()


def tearDownModule():
    _network.stop()
