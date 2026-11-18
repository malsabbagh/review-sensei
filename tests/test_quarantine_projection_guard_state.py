"""Typed native projection custody survives canonical capacity sealing."""

import copy
import json
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from review_sensei_github import quarantine_capacity as capacity
from review_sensei_github.native_reader_projection import (
    HistoricalRoot,
    ReaderProjection,
)
from test_quarantine_activation import IDENTITY, registered_resource
from test_quarantine_capacity import no_reserve
from test_quarantine_published_tail_journey import (
    BROKER_QUALIFICATIONS,
    NativePublishedTailJourneyFixture,
)

from review_sensei.errors import ReviewInputError


class ProjectionGuardStateTests(unittest.TestCase):
    def guard(self):
        projection = ReaderProjection(
            123,
            7,
            999,
            "a" * 64,
            "b" * 64,
            ("c" * 40,),
            ("d" * 40,),
            1,
            (HistoricalRoot(10, "PRRT_original", 20, "c" * 40),),
        )
        guard = capacity.NativeCapacityGuard(
            identity=IDENTITY,
            reserve=no_reserve(),
            native_profile={"projection": projection, "targets": {10: "PRRT_original"}},
        )
        return guard, projection

    def test_complete_fields_and_identity_are_committed(self):
        guard, projection = self.guard()
        original = capacity._guard_state(guard)
        encoded = json.loads(original)["native_profile"]["projection"]
        self.assertEqual(set(encoded), set(vars(projection)))
        self.assertEqual(encoded["roots"], [vars(projection.roots[0])])
        for field, value in vars(projection).items():
            if field != "roots":
                self.assertEqual(
                    encoded[field], list(value) if type(value) is tuple else value
                )
        for owner, field, replacement in (
            (projection, "producer_id", 1000),
            (projection, "source_contract_sha256", "f" * 64),
            (projection.roots[0], "thread_id", "PRRT_changed"),
        ):
            old = getattr(owner, field)
            try:
                object.__setattr__(owner, field, replacement)
                self.assertNotEqual(capacity._guard_state(guard), original)
            finally:
                object.__setattr__(owner, field, old)
        guard.native_profile["projection"] = copy.copy(projection)
        self.assertNotEqual(capacity._guard_state(guard), original)

    def test_foreign_projection_and_nested_root_refuse(self):
        guard, projection = self.guard()
        guard.native_profile["projection"] = SimpleNamespace(**vars(projection))
        with self.assertRaisesRegex(ReviewInputError, "projection type changed"):
            capacity._guard_state(guard)
        guard.native_profile["projection"] = projection
        object.__setattr__(
            projection, "roots", (SimpleNamespace(**vars(projection.roots[0])),)
        )
        with self.assertRaisesRegex(ReviewInputError, "projection type changed"):
            capacity._guard_state(guard)

    def test_complete_metadata_mutation_and_foreign_type(self):
        from review_sensei_github.native_review_census import RootMetadata

        guard, _ = self.guard()
        row = RootMetadata(
            10, 20, 999, False, "RIGHT", "LINE", "c" * 40, "2026-11-13T00:00:00Z"
        )
        guard.native_profile["root_metadata"] = {10: row}
        original = capacity._guard_state(guard)
        self.assertEqual(
            json.loads(original)["native_profile"]["root_metadata"]["10"], vars(row)
        )
        object.__setattr__(row, "resolved", True)
        self.assertNotEqual(capacity._guard_state(guard), original)
        object.__setattr__(row, "resolved", False)
        guard.native_profile["root_metadata"] = {10: copy.copy(row)}
        self.assertNotEqual(capacity._guard_state(guard), original)
        guard.native_profile["root_metadata"] = {10: SimpleNamespace(**vars(row))}
        with self.assertRaisesRegex(ReviewInputError, "metadata type changed"):
            capacity._guard_state(guard)

    def test_actual_native_bootstrap_installs_complete_projection(self):
        captured = []

        def observe(frame, event, arg):
            if frame.f_code is capacity.install_capacity.__code__:
                if event == "return" and arg is not None:
                    captured.append(capacity._guard_state(arg))
                return observe
            return None

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            fixture = NativePublishedTailJourneyFixture(directory)
            _, registration = fixture.original_origins()
            previous = sys.gettrace()
            try:
                sys.settrace(observe)
                with registered_resource([registration]):
                    fixture.invoke()
            finally:
                sys.settrace(previous)
            self.assertTrue(captured, fixture.error)
            projection = json.loads(captured[0])["native_profile"]["projection"]
            self.assertTrue(projection["roots"])
            self.assertEqual(projection["repository_id"], 123)
            self.assertNotIn("evidence is not bounded canonical JSON", fixture.error)
            # Subsequent capacity/finalization is outside this bootstrap regression.
