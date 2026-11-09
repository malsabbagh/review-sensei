"""Installed core decision adapters, aggregation and CLI configuration contracts."""

import sys
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))

from packaging_guard import assert_distribution_import  # noqa: E402

assert_distribution_import()

from test_review_decision_adapters import ReviewDecisionAdapterTests  # noqa: E402, F401
from test_review_decision_binding import ReviewDecisionBindingTests  # noqa: E402, F401
from test_review_decision_configuration import (  # noqa: E402, F401
    ReviewDecisionCliTests,
    ReviewDecisionConfigurationTests,
)
from test_review_decision_engine import ReviewDecisionEngineTests  # noqa: E402, F401
