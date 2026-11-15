"""Pure data and denied-source controls; no genuine preparation is issued here."""

import unittest
from unittest.mock import patch

from review_sensei_github import quarantine_command_finalization as source
from review_sensei_github.quarantine_origin import AuthenticatedQuarantine

from review_sensei.errors import ReviewInputError


class ProspectiveSizingDataTests(unittest.TestCase):
    def test_hold_snapshot_includes_all_five_byte_components(self):
        hold = AuthenticatedQuarantine(
            "a" * 64, b"lineage", b"archive", b"inventories", b"state", b"source"
        )
        frozen = source._hold_bytes(hold)
        self.assertEqual(
            frozen, (b"lineage", b"archive", b"inventories", b"state", b"source")
        )
        for name in (
            "lineage_bytes",
            "archive_bytes",
            "inventories_bytes",
            "state_bytes",
            "source_documents_bytes",
        ):
            previous = object.__getattribute__(hold, name)
            object.__setattr__(hold, name, b"changed")
            self.assertNotEqual(source._hold_bytes(hold), frozen)
            object.__setattr__(hold, name, previous)
        self.assertEqual(source._hold_bytes(hold), frozen)

    def test_hold_impostor_and_hostile_field_never_execute(self):
        calls = []

        class Bomb:
            def __getattribute__(self, name):
                calls.append(name)
                raise AssertionError(name)

        with self.assertRaises(ReviewInputError):
            source._hold_bytes(Bomb())
        hold = AuthenticatedQuarantine("a" * 64, b"a", b"b", b"c", b"d")
        object.__setattr__(hold, "state_bytes", Bomb())
        with self.assertRaises(ReviewInputError):
            source._hold_bytes(hold)
        self.assertEqual(calls, [])

    def test_public_sizing_helpers_refuse_without_lexical_owner(self):
        token = source.QuarantineFinalizerPreparation()
        with self.assertRaises(ReviewInputError):
            source.attach_sizing_hold(token, object(), object(), head_sha="a" * 40)
        with self.assertRaises(ReviewInputError):
            source.prospective_pr_branch(
                token,
                object(),
                repository="o/r",
                repository_id=1,
                pull_request=1,
                head_sha="a" * 40,
                base_sha="b" * 40,
            )
        with self.assertRaises(ReviewInputError):
            with source.prospective_sizing_scope(
                token, object(), object(), object(), object(), object()
            ):
                self.fail("unissued scope entered")
        self.assertIsNone(source._SIZING_SCOPE.get())

    def test_copied_context_data_is_not_a_preparation(self):
        token = source.QuarantineFinalizerPreparation()
        reset = source._SIZING_SCOPE.set(
            (token, object(), object(), object(), object(), object(), "a" * 64)
        )
        try:
            with self.assertRaises(ReviewInputError):
                source._require_prospective(token, object())
        finally:
            source._SIZING_SCOPE.reset(reset)
        with self.assertRaises(ReviewInputError):
            source._require_prospective(token, object())

    def test_replaced_helper_is_rejected_before_callback(self):
        calls = []

        def poison(*args, **kwargs):
            calls.append(True)

        with patch.object(source, "_require_prospective", poison):
            with self.assertRaises(ReviewInputError):
                source.attach_sizing_hold(
                    object(), object(), object(), head_sha="a" * 40
                )
        self.assertEqual(calls, [])

    def test_branch_is_not_live_pr_or_observation(self):
        from review_sensei_github.publication import _FinalizationPreflight

        value = source.ProspectiveSizingBranch(1, "b" * 40)
        self.assertNotIsInstance(value, _FinalizationPreflight)
        self.assertFalse(hasattr(value, "payload"))
        self.assertFalse(hasattr(value, "app_authored"))
        with self.assertRaises(ReviewInputError):
            source.require_observation(value)
