"""Qualify lifecycle observations against both installed packages."""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packaging_guard import assert_distribution_import, is_disarmed  # noqa: E402

assert_distribution_import()

import review_sensei_github  # noqa: E402

if not is_disarmed() and not Path(
    review_sensei_github.__file__
).resolve().is_relative_to(Path(sys.prefix).resolve()):
    raise ImportError("lifecycle qualification requires the installed hosted adapter")

import test_review_lifecycle_diagnostics as contract  # noqa: E402


class InstalledReviewLifecycleDiagnosticsTests(
    contract.ReviewLifecycleDiagnosticsTests
):
    pass


class InstalledDiagnosticOutputBoundaryTests(contract.DiagnosticOutputBoundaryTests):
    pass


def setUpModule():
    global _network
    _network = patch(
        "socket.socket.connect", side_effect=AssertionError("live network forbidden")
    )
    _network.start()


def tearDownModule():
    _network.stop()
