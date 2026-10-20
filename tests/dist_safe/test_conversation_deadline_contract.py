"""Run ordinary-discussion deadline acceptance against installed packages."""

import sys
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))

from packaging_guard import assert_distribution_import  # noqa: E402

assert_distribution_import()

from conversation_deadline_contract import ConversationDeadlineContractTests  # noqa: E402, F401
