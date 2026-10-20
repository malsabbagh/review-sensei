"""Independent hosted-history acceptance against exact installed distributions."""

# ruff: noqa: E402
import sys
from pathlib import Path

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
    raise ImportError("hosted-history acceptance requires the installed GitHub adapter")
sys.path.insert(0, str(_TESTS.parent))
import test_hosted_history_independent_acceptance as _contract


class InstalledIndependentAcceptanceTests(_contract.IndependentAcceptanceTests):
    pass
