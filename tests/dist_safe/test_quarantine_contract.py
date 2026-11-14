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
