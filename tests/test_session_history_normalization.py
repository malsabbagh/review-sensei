"""Session history restoration keeps validation, integrity and fresh values."""

import unittest
from collections import UserDict
from copy import deepcopy

from test_session_ledger import FIXED_NOW, IDENTITY, SessionRecordTests

from review_sensei.errors import ReviewInputError
from review_sensei.session import SessionRecord


class SessionHistoryNormalizationTests(unittest.TestCase):
    def record(self):
        return SessionRecord.create(
            IDENTITY,
            now=FIXED_NOW,
            convergence_history=SessionRecordTests()._history(),
        )

    def test_restoration_keeps_fresh_history_and_original_integrity(self):
        original = self.record()
        for container in (dict, UserDict):
            document = container(deepcopy(original.to_dict()))
            restored = SessionRecord.from_dict(document)
            self.assertEqual(restored.to_dict(), original.to_dict())
            document["convergence_history"]["baseline"]["findings"].clear()
            self.assertEqual(restored.to_dict(), original.to_dict())

    def test_invalid_baseline_history_and_tampering_still_refuse(self):
        original = self.record()
        for mutation in (
            lambda h: h["baseline"]["findings"][0].update(extra=True),
            lambda h: h["baseline"]["findings"][0].update(blocking="yes"),
            lambda h: h["progress"][0].update(generation=True),
            lambda h: h["provenance"].update(ledger_digest="invalid"),
            lambda h: h.update(state="invalidated"),
        ):
            for container in (dict, UserDict):
                document = container(deepcopy(original.to_dict()))
                mutation(document["convergence_history"])
                with self.assertRaises(ReviewInputError):
                    SessionRecord.from_dict(document)

    def test_subclass_prevalidation_precedes_constructor_callbacks(self):
        entered = []

        class ObservingRecord(SessionRecord):
            def __post_init__(self, digest_shape):
                entered.append(True)
                super().__post_init__(digest_shape)

        original = self.record()
        invalid = deepcopy(original.to_dict())
        invalid["convergence_history"]["progress"][0]["generation"] = True
        with self.assertRaises(ReviewInputError):
            ObservingRecord.from_dict(invalid)
        self.assertEqual(entered, [])
        self.assertEqual(
            ObservingRecord.from_dict(original.to_dict()).to_dict(), original.to_dict()
        )
        self.assertEqual(entered, [True])
