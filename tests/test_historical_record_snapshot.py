"""Retained roots are inspected without callbacks or a widened scalar domain."""

import copy
import tempfile
import unittest

from review_sensei_github import historical_record_snapshot as snapshot

import tests.test_api_work_actions as actions
from review_sensei.api_work import MAX_API_REQUEST_LIMIT
from review_sensei.errors import ReviewInputError


class HistoricalRecordSnapshotTests(unittest.TestCase):
    def test_original_record_and_serializer_shadows_are_callback_free(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = actions.NativeActionTests()
            fixture, ledger = helper.prepare(directory)
            _, _, _, record = helper.complete(fixture, ledger)
            original = snapshot._record_snapshot(record)
            self.assertEqual(original["record_sha256"], record.record_sha256)
            calls = []
            for name in ("to_dict", "identity"):
                changed = copy.copy(record)
                object.__setattr__(changed, name, lambda: calls.append(name))
                with (
                    self.subTest(name=name),
                    self.assertRaisesRegex(ReviewInputError, "declared fields changed"),
                ):
                    snapshot._record_snapshot(changed)
            self.assertEqual(calls, [])
            changed = copy.copy(record)
            object.__setattr__(changed, "repository_id", MAX_API_REQUEST_LIMIT + 1)
            with self.assertRaisesRegex(ReviewInputError, "integer is unbounded"):
                snapshot._record_snapshot(changed)
            self.assertEqual(snapshot._record_snapshot(record), original)

    def test_impostor_record_never_runs_any_attribute_callback(self):
        calls = []

        class Poison:
            def __getattribute__(self, name):
                calls.append(name)
                raise AssertionError("record callback invoked")

        with self.assertRaisesRegex(ReviewInputError, "record source changed"):
            snapshot._record_snapshot(Poison())
        self.assertEqual(calls, [])
