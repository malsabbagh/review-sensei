"""Qualify current review, command and reply lifecycles against installed wheels."""

import os
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
    raise ImportError("current run qualification requires the installed hosted adapter")

import test_action_reply_entry as reply_contract  # noqa: E402
import test_action_run as command_contract  # noqa: E402
import test_review_run as review_contract  # noqa: E402
import test_review_run_rescan as rescan_contract  # noqa: E402


class InstalledReviewRunEntryTests(review_contract.ReviewRunEntryTests):
    pass


class InstalledConsecutiveReviewRunTests(review_contract.ConsecutiveReviewRunTests):
    pass


class InstalledCurrentCommandEntryTests(command_contract.CurrentCommandEntryTests):
    pass


class InstalledActionReplyEntryTests(reply_contract.ActionReplyEntryTests):
    pass


class InstalledRetainedEntryTests(rescan_contract.RetainedEntryTests):
    pass


def setUpModule():
    global _network, _environment
    _network = patch(
        "socket.socket.connect", side_effect=AssertionError("live network forbidden")
    )
    # Hard-kill children inherit only extracted fixture paths; their application
    # imports must still resolve from the installed wheels under sys.prefix.
    _environment = patch.dict(os.environ, {"PYTHONPATH": str(_TESTS)})
    _network.start()
    _environment.start()


def tearDownModule():
    _environment.stop()
    _network.stop()
