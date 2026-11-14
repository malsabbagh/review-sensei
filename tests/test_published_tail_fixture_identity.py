"""Import aliases share synthetic run allocation without clearing authority."""

import importlib
import unittest


class PublishedTailFixtureIdentityTests(unittest.TestCase):
    def test_bare_and_package_imports_share_one_execution_sequence(self):
        bare = importlib.import_module("test_quarantine_published_tail_journey")
        package = importlib.import_module("tests.test_quarantine_published_tail_journey")
        self.assertIs(bare._RUNS, package._RUNS)
        first = next(bare._RUNS)
        second = next(package._RUNS)
        self.assertEqual(second, first + 1)
