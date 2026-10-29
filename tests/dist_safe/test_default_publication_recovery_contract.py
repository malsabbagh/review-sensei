"""Default publication/recovery contracts against exact installed distributions."""

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
    raise ImportError("default publication requires the installed GitHub adapter")

# These explicitly packaged test helpers expose no src directory to imports.
sys.path.insert(0, str(_TESTS.parent))
import test_bounded_v1_publication  # noqa: E402
import test_checkpoint_publication_acceptance  # noqa: E402
import test_default_publication_cli_journey  # noqa: E402
import test_narrative_archive  # noqa: E402
import test_publication_cli_recovery  # noqa: E402
import test_publication_recovery  # noqa: E402
import test_trusted_publication_recovery  # noqa: E402


class InstalledCompleteInventoryTests(
    test_bounded_v1_publication.CompleteInventoryPublicationTests
):
    pass


class InstalledNarrativeArchiveTests(test_narrative_archive.NarrativeArchiveTests):
    pass


class InstalledCheckpointAcceptanceTests(
    test_checkpoint_publication_acceptance.CheckpointPublicationAcceptanceTests
):
    pass


class InstalledDefaultCliJourneyTests(
    test_default_publication_cli_journey.DefaultPublicationCliJourneyTests
):
    pass


class InstalledPublicationCliTests(
    test_publication_cli_recovery.PublicationCliRecoveryTests
):
    pass


class InstalledPublicationRecoveryTests(
    test_publication_recovery.PublicationRecoveryTests
):
    pass


class InstalledNativeReplayAuthorityTests(
    test_publication_recovery.NativeReplayAuthorityTests
):
    pass


class InstalledTrustedPublicationRecoveryTests(
    test_trusted_publication_recovery.TrustedPublicationRecoveryTests
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
