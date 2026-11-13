"""Actual ordinary readback proofs cannot be copied, rebound, or replayed."""

import copy
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from review_sensei_github import ordinary_native_recovery as recovery
from review_sensei_github.session_ledger import parse_session_comment

from review_sensei.api_work import ApiRunAccounting
from review_sensei.errors import ReviewInputError
from tests import test_recovery_context_g17 as fixtures
from tests.test_api_work_persistence import IDENTITY


class OrdinaryNativeRecoveryCustodyTests(unittest.TestCase):
    def fixture(self, directory, *, altered_native=False):
        fixture = (
            fixtures.OrdinaryEntryHostedAndCliRecoveryG17Tests._make_hosted_fixture(
                directory
            )
        )
        original = fixture.external
        lost = []

        def stored_loss(request, timeout):
            response = original(request, timeout)
            if (
                not lost
                and request.get_method() == "POST"
                and urlsplit(request.full_url).path.endswith("/check-runs")
            ):
                lost.append(True)
                if altered_native:
                    fixture.check_runs[0]["head_sha"] = "c" * 40
                raise TimeoutError("synthetic stored check response loss")
            return response

        fixture.external = stored_loss
        return fixture

    def assert_terminal_held(self, fixture):
        self.assertEqual(fixture.reviews, [])
        record = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
        self.assertIsNone(
            ApiRunAccounting.from_document(
                record.api_run_accounting
            ).terminal_transaction
        )

    def test_actual_check_proof_is_not_copyable_forgeable_or_replayable(self):
        original = recovery.consume_check
        observed = []

        def consume(proof, checks, **kwargs):
            for foreign in (copy.copy(proof), recovery.ReconciledCheck("unissued")):
                with self.assertRaises(ReviewInputError):
                    original(foreign, checks, **kwargs)
            identifier = original(proof, checks, **kwargs)
            with self.assertRaises(ReviewInputError):
                original(proof, checks, **kwargs)
            observed.append(identifier)
            return identifier

        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            with patch.object(recovery, "consume_check", consume):
                self.assertEqual(fixture.invoke(), 0, fixture.error)
            self.assertEqual(observed, [fixture.check_runs[0]["id"]])
            unknown = [
                item
                for item in fixture.accounted.observations
                if item.get("mutation") and item["outcome"] == "unknown"
            ]
            self.assertEqual(len(unknown), 1)
            self.assertIn(
                unknown[0]["request_id"], fixture.accounted._reconciled_mutations
            )
            self.assertGreater(fixture.accounted.accounting.uncertain, 0)

    def test_foreign_request_source_change_and_stale_root_cannot_consume(self):
        original = recovery.consume_check
        for fault in ("request", "source", "root"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                fixture = self.fixture(directory)

                def consume(proof, checks, **kwargs):
                    capture = recovery._CHECK_RESULTS[proof][0]
                    if fault == "request":
                        return original(
                            proof,
                            checks,
                            **{**kwargs, "path": kwargs["path"] + "/foreign"},
                        )
                    if fault == "source":
                        checks._create = lambda **_: None
                        try:
                            return original(proof, checks, **kwargs)
                        finally:
                            del checks._create
                    root = capture.caller._root
                    capture.caller._root = "f" * 64
                    try:
                        return original(proof, checks, **kwargs)
                    finally:
                        capture.caller._root = root

                with patch.object(recovery, "consume_check", consume):
                    self.assertEqual(fixture.invoke(), 1, fixture.error)
                self.assert_terminal_held(fixture)

    def test_changed_readback_credential_holds_without_effect_settlement(self):
        original = recovery.recover
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            with patch.object(
                recovery,
                "recover",
                lambda capture, **_: original(capture, token="foreign-synthetic-token"),
            ):
                self.assertEqual(fixture.invoke(), 1, fixture.error)
            self.assert_terminal_held(fixture)
            unknown = [
                item
                for item in fixture.accounted.observations
                if item.get("mutation") and item["outcome"] == "unknown"
            ]
            self.assertTrue(unknown)
            self.assertTrue(
                all(
                    item["request_id"] not in fixture.accounted._reconciled_mutations
                    for item in unknown
                )
            )

    def test_changed_native_head_keeps_original_unknown_liability(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory, altered_native=True)
            self.assertEqual(fixture.invoke(), 1, fixture.error)
            self.assert_terminal_held(fixture)
            unknown = [
                item
                for item in fixture.accounted.observations
                if item.get("mutation") and item["outcome"] == "unknown"
            ]
            self.assertTrue(unknown)
            self.assertTrue(
                all(
                    item["request_id"] not in fixture.accounted._reconciled_mutations
                    for item in unknown
                )
            )

    def test_mutated_physical_request_identity_cannot_settle_another_charge(self):
        original = recovery.recover
        for field, value in (
            ("request_id", "foreign-unresolved"),
            ("deadline_unix_ms", 1),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                fixture = self.fixture(directory)
                observed = []

                def mutate(capture, **kwargs):
                    observed.append(capture.mutation["request_id"])
                    capture.mutation[field] = value
                    return original(capture, **kwargs)

                with patch.object(recovery, "recover", mutate):
                    self.assertEqual(fixture.invoke(), 1, fixture.error)
                self.assert_terminal_held(fixture)
                self.assertTrue(observed)
                self.assertTrue(
                    all(
                        identifier not in fixture.accounted._reconciled_mutations
                        for identifier in observed
                    )
                )
                self.assertNotIn(
                    "foreign-unresolved", fixture.accounted._reconciled_mutations
                )
