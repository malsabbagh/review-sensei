"""Complete default human inventory against exact installed distributions."""

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
    raise ImportError("default human inventory requires the installed GitHub adapter")

sys.path.insert(0, str(_TESTS.parent))
import test_default_human_consumers  # noqa: E402
import test_default_human_inventory  # noqa: E402
import test_default_human_inventory_cli  # noqa: E402
import test_default_human_inventory_publisher  # noqa: E402
import test_default_human_inventory_store  # noqa: E402
import test_default_human_reader_binding  # noqa: E402
import test_default_human_rescan  # noqa: E402
import test_feedback_fence_budget  # noqa: E402
import test_local_partition_read_cache  # noqa: E402
import test_publication_pagination_budget  # noqa: E402
import test_publication_recovery_phase  # noqa: E402
import test_retained_full_rescan  # noqa: E402
import test_schema5_public_contract  # noqa: E402


class InstalledDefaultHumanInventoryTests(
    test_default_human_inventory.DefaultHumanInventoryTests
):
    pass


class InstalledDefaultHumanCliTests(
    test_default_human_inventory_cli.DefaultHumanInventoryCliTests
):
    pass


class InstalledDefaultHumanPublisherTests(
    test_default_human_inventory_publisher.DefaultHumanInventoryPublisherTests
):
    pass


class InstalledDefaultHumanStorageTests(
    test_default_human_inventory_store.DefaultHumanInventoryStoreTests
):
    pass


class InstalledDefaultHumanReaderBindingTests(
    test_default_human_reader_binding.DefaultHumanReaderBindingTests
):
    pass


class InstalledDefaultHumanRescanTests(
    test_default_human_rescan.DefaultHumanRescanTests
):
    pass


class InstalledRetainedFullRescanTests(
    test_retained_full_rescan.RetainedFullRescanTests
):
    pass


class InstalledSchema5PublicContractTests(
    test_schema5_public_contract.Schema5PublicContractTests
):
    pass


class InstalledReplyBrokerBudgetTests(
    test_feedback_fence_budget.ReplyBrokerBudgetTests
):
    pass


class InstalledLocalPartitionReadCacheTests(
    test_local_partition_read_cache.LocalPartitionReadCacheTests
):
    pass


class InstalledPublicationPaginationBudgetTests(
    test_publication_pagination_budget.PublicationPaginationBudgetTests
):
    pass


class InstalledPublicationRecoveryReadPhaseTests(
    test_publication_recovery_phase.PublicationRecoveryReadPhaseTests
):
    pass


def load_tests(loader, suite, pattern):
    # Include all public codec/finalizer/consumer classes without duplicating
    # imported fixture classes or exposing a checkout src directory.
    suite.addTests(loader.loadTestsFromModule(test_default_human_consumers))
    return suite


def setUpModule():
    global _network
    _network = patch(
        "socket.socket.connect", side_effect=AssertionError("live network forbidden")
    )
    _network.start()


def tearDownModule():
    _network.stop()
