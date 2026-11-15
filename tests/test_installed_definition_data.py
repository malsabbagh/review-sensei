"""Executable fingerprints for synthetic constant slices, without product fixtures."""

import runpy
import unittest
from pathlib import Path

RUNNER = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts/run_installed_test_suites.py")
)


class InstalledDefinitionDataTests(unittest.TestCase):
    def test_slice_components_are_distinct_and_exact(self):
        encode = RUNNER["definition_data"]
        self.assertEqual(
            encode(slice(None, 6, -1)),
            ["slice", ["NoneType", None], ["int", 6], ["int", -1]],
        )
        values = (slice(None, 6), slice(0, 6), slice(None, 7), slice(None, 6, 1))
        self.assertEqual(len({repr(encode(value)) for value in values}), len(values))

    def test_compiled_slice_changes_executable_fingerprint(self):
        snapshot = RUNNER["code_snapshot"]

        def first(values):
            return values[1:6:2]

        def same(values):
            return values[1:6:2]

        def changed(values):
            return values[1:7:2]

        self.assertEqual(snapshot(first.__code__), snapshot(same.__code__))
        self.assertNotEqual(snapshot(first.__code__), snapshot(changed.__code__))

    def test_unsupported_slice_component_does_not_invoke_callbacks(self):
        class Hostile:
            def __repr__(self):
                raise AssertionError("unexpected repr")

            def __index__(self):
                raise AssertionError("unexpected index")

            def __eq__(self, other):
                raise AssertionError("unexpected equality")

            def __hash__(self):
                raise AssertionError("unexpected hash")

        with self.assertRaisesRegex(ValueError, "unsupported"):
            RUNNER["definition_data"](slice(Hostile()))

    def test_cyclic_slice_default_is_rejected(self):
        values = []
        value = slice(values)
        values.append(value)
        with self.assertRaisesRegex(ValueError, "cyclic"):
            RUNNER["definition_data"](value)
