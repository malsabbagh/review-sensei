"""Recorded-decision capacity recovery against installed distributions."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TESTS))
from packaging_guard import assert_distribution_import  # noqa: E402

assert_distribution_import()

import review_sensei_github  # noqa: E402
from review_sensei_github.action_receipt import _RECEIPT_PAGE_SIZES  # noqa: E402
from review_sensei_github.publication import parse_finding_index  # noqa: E402
from review_sensei_github.session_ledger import parse_session_comment  # noqa: E402

from review_sensei.disposition import session_dispositions  # noqa: E402
from review_sensei.finding_lifecycle_store import (  # noqa: E402
    _inline_cas_pricing_eligible,
    _inline_checkpoint_read_free,
)

if (
    not Path(review_sensei_github.__file__)
    .resolve()
    .is_relative_to(Path(sys.prefix).resolve())
):
    raise ImportError("command capacity recovery requires the installed GitHub adapter")

sys.path.insert(0, str(_TESTS.parent))
import test_action_run  # noqa: E402
import test_api_work_persistence  # noqa: E402
import test_approval_progress  # noqa: E402
import test_command_capacity_recovery  # noqa: E402
import test_disposition_snapshots  # noqa: E402
import test_publication_future_capacity  # noqa: E402
import test_remaining_progress  # noqa: E402


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


class InstalledRemainingProgressTests(test_remaining_progress.RemainingProgressTests):
    pass


class InstalledRemainingCrashAccountingTests(
    test_remaining_progress.RemainingCrashAccountingTests
):
    pass


class InstalledActualRemainingWindowTests(
    test_remaining_progress.ActualProductiveWindowTests
):
    pass


class InstalledApprovalProgressTests(test_approval_progress.ApprovalProgressTests):
    pass


class InstalledActualFinalizerProgressTests(
    test_approval_progress.ActualFinalizerProgressTests
):
    pass


class InstalledPartitionedSnapshotCapacity512Tests(unittest.TestCase):
    def test_partitioned_disposition_pricing_and_512_approval_contract(self):
        self.assertEqual(_RECEIPT_PAGE_SIZES, (100, 50, 25, 20, 10, 5, 1))

        with tempfile.TemporaryDirectory() as directory:
            fixture = test_action_run.CommandEntryFixture(directory, limit=512)
            fixture.provider_comments = [
                {
                    "path": "src/app.py",
                    "line": 2,
                    "side": "RIGHT",
                    "body": f"Confirm contract invariant {idx}.",
                    "severity": "high",
                    "blocking": False,
                    "needs_human": True,
                }
                for idx in range(25)
            ]
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            index = parse_finding_index(
                fixture.reviews[-1]["body"],
                repository_id=test_api_work_persistence.IDENTITY.repository_id,
                pull_request=test_api_work_persistence.IDENTITY.pull_request,
                head_sha=test_api_work_persistence.HEAD,
            )
            labels = ["RS-" + entry.instance[:6].upper() for entry in index.entries]
            for start in range(0, 20, 4):
                fixture.SOURCE_ID += 1
                fixture.select_command(
                    "@reviewsensei override "
                    + " ".join(labels[start : start + 4])
                    + " acceptable risk",
                    new_run=True,
                )
                self.assertEqual(fixture.invoke_command(), 0, fixture.error)
            mid = parse_session_comment(
                fixture.comment["body"],
                identity=test_api_work_persistence.IDENTITY,
            )
            self.assertEqual(
                mid.disposition_snapshot["state_manifest"]["encoding"],
                "partitioned-json-v1",
            )
            self.assertTrue(_inline_checkpoint_read_free(mid))
            self.assertTrue(_inline_cas_pricing_eligible(mid))
            for sl in (slice(20, 22), slice(22, 24), slice(24, 25)):
                fixture.SOURCE_ID += 1
                fixture.select_command(
                    "@reviewsensei override "
                    + " ".join(labels[sl])
                    + " acceptable risk",
                    new_run=True,
                )
                self.assertEqual(fixture.invoke_command(), 0, fixture.error)
            final_rec = parse_session_comment(
                fixture.comment["body"],
                identity=test_api_work_persistence.IDENTITY,
            )
            self.assertEqual(
                len(session_dispositions(final_rec, ledger=fixture.selected.ledger)),
                25,
            )
            self.assertEqual(
                sum(1 for r in fixture.reviews if r["state"] == "APPROVED"), 1
            )


def setUpModule():
    global _network
    _network = patch(
        "socket.socket.connect", side_effect=AssertionError("live network forbidden")
    )
    _network.start()


def tearDownModule():
    _network.stop()
