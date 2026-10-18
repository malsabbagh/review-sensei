"""Run the shared actual-adapter journeys against the installed distributions."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packaging_guard import assert_distribution_import  # noqa: E402

assert_distribution_import()

import review_sensei_github  # noqa: E402
from packaging_guard import is_disarmed  # noqa: E402

if not is_disarmed():
    assert (
        Path(review_sensei_github.__file__)
        .resolve()
        .is_relative_to(Path(sys.prefix).resolve())
    )

import provider_common_flow_contract as contract  # noqa: E402


def load_tests(loader, tests, pattern):
    return contract.load_common_flow_tests(loader)
