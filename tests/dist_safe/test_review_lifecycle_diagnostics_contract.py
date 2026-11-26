"""Qualify lifecycle observations against both installed packages."""

import io
import json
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packaging_guard import assert_distribution_import, is_disarmed  # noqa: E402

assert_distribution_import()

import review_sensei_github  # noqa: E402
from review_sensei_github import review_lifecycle_diagnostics  # noqa: E402

from review_sensei.errors import ReviewInputError  # noqa: E402

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


class HostedLifecycleDiagnosticsGap2ContractTests(unittest.TestCase):
    def test_publication_incomplete_ambiguous_thread_resolution_and_failure_kind(
        self,
    ):
        stream = io.StringIO()
        with (
            redirect_stderr(stream),
            review_lifecycle_diagnostics.lifecycle("review") as current,
        ):
            review_lifecycle_diagnostics.publication(
                {
                    "status": "publication_incomplete",
                    "publication_status": "publication_incomplete",
                    "thread_resolution": "unconfirmed",
                    "diagnostic": "publication_ambiguous",
                }
            )
            review_lifecycle_diagnostics.classified_failure(
                review_lifecycle_diagnostics.exception_kind(
                    ReviewInputError("synthetic-private-detail")
                )
            )
            current.returned(1)
        lines = [
            line[len("review-sensei: lifecycle ") :]
            for line in stream.getvalue().splitlines()
            if line.startswith("review-sensei: lifecycle ")
        ]
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertEqual(
            payload["publication"]["publication_status"], "publication_incomplete"
        )
        self.assertEqual(payload["publication"]["thread_resolution"], "unconfirmed")
        self.assertEqual(payload["publication"]["diagnostic"], "publication_ambiguous")
        self.assertEqual(payload["primary_exception_kind"], "input_or_protocol")
        self.assertNotIn("synthetic-private-detail", lines[0])


def setUpModule():
    global _network
    _network = patch(
        "socket.socket.connect", side_effect=AssertionError("live network forbidden")
    )
    _network.start()


def tearDownModule():
    _network.stop()
