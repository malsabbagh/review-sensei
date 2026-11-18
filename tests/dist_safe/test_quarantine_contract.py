"""Quarantine contracts exercise only the installed core and hosted adapter."""

import importlib
import sys
import unittest
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
    raise ImportError("quarantine qualification requires the installed hosted adapter")


# Reviewed deterministic process-local construction and module-local guard.
INSTALLED_SUITE_HOOK_CONTRACT = ("reproducible-module-local-v1", ())
_guarded_setups = {}


# Independent-method review is deliberately narrower than the local-hook
# contract: these exact seven methods have fresh case-local live fixtures. The
# setup may only be the socket guard below with no composed original fixture.
# Canonical LF source/dependency hashes are independent of checkout CRLF policy.
# Source/dependency drift revokes splitting, not coverage: the runner executes
# the original discovered root instead. Re-review before refreshing hashes.
INSTALLED_METHOD_SHARD_CONTRACT = (
    "independent-socket-methods-v1",
    (
        (
            "test_quarantine_checkpoint",
            (
                "test_quarantine_checkpoint.QuarantineCarrierPlanningTests.test_funded_staging_creates_owned_carriers_instead_of_reusing_copies",
                "test_quarantine_checkpoint.QuarantineCarrierPlanningTests.test_human_wrong_bot_and_foreign_copies_cannot_underfund_public_staging",
                "test_quarantine_checkpoint.QuarantineCheckpointTests.test_both_exact_fences_reject_changed_foreign_or_wrong_owner_roots_before_patch",
                "test_quarantine_checkpoint.QuarantineCheckpointTests.test_stored_or_unstored_lost_ack_uses_one_patch_and_only_original_exact_readback",
                "test_quarantine_checkpoint.QuarantineCheckpointTests.test_uncertain_individual_event_never_binds_or_clears_a_hold",
                "test_quarantine_checkpoint.QuarantineDecisionScopeFenceTests.test_head_or_base_changed_during_exact_decision_preparation_never_patches_the_decision",
                "test_quarantine_checkpoint.QuarantineRolloverTests.test_startup_rollover_fences_and_lost_ack_keep_one_write_and_no_old_completion",
            ),
            (
                (
                    "test_quarantine_checkpoint.py",
                    "7bb95ea52f82b843049aea99dc319599a5782974bbde90f7f55ec4b28dc379a9",
                ),
                (
                    "test_quarantine_journey.py",
                    "e551fc7bb729e85c3cea78517d45e628f0a6c6b73b75ad990ce71e95a9537279",
                ),
                (
                    "test_quarantine_creation.py",
                    "0bc8e2c9538ea07ee931765e7f89ad81b3eb3e2d0f6792a9a9aa5b3269f605b8",
                ),
                (
                    "test_quarantine_origin.py",
                    "6e574cec03be53ed4bcbda12a5dbbe5450a4eb492fe17db174dbc3e8c734ee90",
                ),
                (
                    "test_quarantine_evidence.py",
                    "5d5244749479a6cfc98e2680c91f16dde9a38534e4415a41bd36eb7964ed1cf5",
                ),
                (
                    "test_quarantine_activation.py",
                    "981e16a9e7929a5504862bf4d2a11f0e4d30c1f8e570838b595d9b1069e42960",
                ),
                (
                    "test_action_run.py",
                    "d704283ef492fe3daefaa3eb4797fdfa95dfbfba03c1d1d57c850307177109c7",
                ),
                (
                    "test_review_run.py",
                    "1bf42f11032e9b5fb15c375dee615cffbd360eaa5ec414e25419bc9b3ad0599d",
                ),
                (
                    "test_api_work_lifecycle.py",
                    "cbae76b34264b7c224e6c437e7b6f2a5017bab21d30f7f7d573228b6b267b269",
                ),
                (
                    "test_api_work_public_review.py",
                    "bffde26f4ac3aa61920f5f5068c10ad3f9430aa1af0a603e691a76b10fa520cf",
                ),
                (
                    "test_api_work_persistence.py",
                    "0bfd6c612de9916abdd45464592fecbbc4a072588bf1176b7143aab84024ed7c",
                ),
                (
                    "test_review_transaction.py",
                    "b964f84dcaa688775c1e4befb3ea56c4b9a04d0e3394e52420853a3dedf3ecf5",
                ),
                (
                    "quarantine_fixture_identity.py",
                    "b67d99ddb7b026b393c57b406f8690ee3b63a0411c5b4b5e3372dfeffda204a4",
                ),
                (
                    "original_finish_interrupt.py",
                    "9dc08c0eaa96ff76246bfb4904970fd9819c64c9dc2f731250b1628ccc401f1c",
                ),
                (
                    "original_source_observation.py",
                    "340c52e403310393bc9794c7812b7255aff9b0f7f24722d50a0161dbb2bdd6f8",
                ),
                (
                    "fake_github_http.py",
                    "c78d99bea62cfdee2b7a66ed20afb29af38c36a18f58149cd66700157f8cdfbc",
                ),
                (
                    "session_fixture_clock.py",
                    "3078625136fac0bc1c543e4b26954d8753ae2119b455df9397a67aa8ae8d0235",
                ),
                (
                    "test_quarantine_isolation.py",
                    "1816757203ed7a0aea53448e6fac98edc1fb78ad4ee899fc647e5ff0f53b592f",
                ),
                (
                    "test_review_run_rescan.py",
                    "b351c460c5a90d7777403e28ca7237d80cf0b9a975936ab8a7b432efd30a0ca4",
                ),
            ),
        ),
    ),
)


def _guard_module(module):
    current = vars(module).get("setUpModule")
    if module in _guarded_setups:
        if current is not _guarded_setups[module]:
            raise ImportError(
                "quarantine module setup changed after guard installation"
            )
        return

    def set_up_module():
        # unittest dispatches fixtures by the leaf class's module, not this
        # collector. Register restoration first so original setup, teardown
        # and all subsequently registered cleanups remain inside the guard.
        unittest.enterModuleContext(
            patch(
                "socket.socket.connect",
                side_effect=AssertionError("live network forbidden"),
            )
        )
        if current is not None:
            current()

    module.setUpModule = set_up_module
    _guarded_setups[module] = set_up_module


def load_tests(loader, tests, pattern):
    # Publication proofs require the optional pinned Sigstore/PyPI verifiers.
    # CI runs these modules after installing those tools; keep this consumer
    # lane limited to the core and hosted wheels' declared dependencies.
    publication_proofs = {
        "test_quarantine_v3_check_capacity",
        "test_quarantine_v3_journey",
        "test_quarantine_v3_publication",
    }
    for path in sorted(_TESTS.glob("test_quarantine*.py")):
        if path.stem in publication_proofs:
            continue
        module = importlib.import_module(path.stem)
        _guard_module(module)
        tests.addTests(loader.loadTestsFromModule(module))
    return tests
