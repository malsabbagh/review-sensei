"""Exact installed engine configuration and credential isolation contract."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packaging_guard import assert_distribution_import  # noqa: E402

assert_distribution_import()

from openrouter_cli_contract import OpenRouterCLIContract  # noqa: E402


class InstalledOpenRouterCLIContractTests(OpenRouterCLIContract, unittest.TestCase):
    pass
