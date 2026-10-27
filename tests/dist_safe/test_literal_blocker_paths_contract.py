"""Literal-path admission contract against installed packages."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packaging_guard import assert_distribution_import  # noqa: E402

assert_distribution_import()

from literal_blocker_paths_contract import LiteralBlockerPathsContract  # noqa: E402


class InstalledLiteralBlockerPathsTests(LiteralBlockerPathsContract, unittest.TestCase):
    pass
