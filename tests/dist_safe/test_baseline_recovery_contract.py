"""The same baseline recovery acceptance contract against installed CLI code."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packaging_guard import assert_distribution_import  # noqa: E402

assert_distribution_import()

from baseline_cli_contract import BaselineCLIContract  # noqa: E402


class InstalledBaselineRecoveryContractTests(BaselineCLIContract, unittest.TestCase):
    installed = True
