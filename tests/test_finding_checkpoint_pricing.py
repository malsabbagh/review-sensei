"""Source-owned checkpoint prices are sizing witnesses, never funding grants."""

import copy
import tempfile
import unittest
from unittest.mock import patch

from review_sensei.errors import ReviewInputError
from review_sensei.finding_lifecycle_store import (
    _consume_checkpoint_pricing,
    _refine_checkpoint_pricing,
    stage_finding_lifecycle,
)
from tests import test_finding_lifecycle_parts as parts_fixtures


class CheckpointPricingTests(unittest.TestCase):
    def plan(self, ledger, record, parts=(1, 1)):
        return ledger.preflight_finding_lifecycle_checkpoint_pricing(
            record,
            added_parts=sum(parts),
            added_bytes=33000,
            checkpoint_parts=parts,
        )

    def test_exact_plain_multi_checkpoint_price_covers_complete_physical_calls(self):
        with tempfile.TemporaryDirectory() as root:
            fixture, record, kwargs = (
                parts_fixtures.CurrentRunFindingPartsTests().fixture(root)
            )
            proof = self.plan(fixture.ledger, record)
            self.assertIsNotNone(proof)
            _, calls = _consume_checkpoint_pricing(
                proof, fixture.ledger, record, (1, 1)
            )
            self.assertEqual(calls, (7, 7))
            for price in calls:
                before = len(fixture.caller.observations)
                record = stage_finding_lifecycle(fixture.ledger, record, **kwargs)
                self.assertLessEqual(len(fixture.caller.observations) - before, price)
                self.assertGreater(len(fixture.caller.observations) - before, 0)
            fixture.assert_physical_accounting(self)

    def test_copied_unissued_reused_and_changed_tuple_cannot_refine(self):
        for fault in ("dict", "copy", "reuse", "tuple"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as root:
                fixture, record, _ = (
                    parts_fixtures.CurrentRunFindingPartsTests().fixture(root)
                )
                proof = self.plan(fixture.ledger, record)
                if fault == "dict":
                    proof = {"calls": (7, 7)}
                elif fault == "copy":
                    proof = copy.copy(proof)
                elif fault == "reuse":
                    _consume_checkpoint_pricing(proof, fixture.ledger, record, (1, 1))
                before = list(fixture.service.trace)
                with self.assertRaises(ReviewInputError):
                    _consume_checkpoint_pricing(
                        proof,
                        fixture.ledger,
                        record,
                        (2,) if fault == "tuple" else (1, 1),
                    )
                self.assertEqual(fixture.service.trace, before)

    def test_mutated_root_clone_or_replaced_writer_cannot_refine(self):
        for fault in ("nested", "clone", "writer"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as root:
                fixture, record, _ = (
                    parts_fixtures.CurrentRunFindingPartsTests().fixture(root)
                )
                proof = self.plan(fixture.ledger, record)
                before = list(fixture.service.trace)
                if fault == "nested":
                    record.api_run_accounting["configuration"]["limit"] += 1
                elif fault == "clone":
                    record = copy.deepcopy(record)
                else:
                    fixture.ledger.replace = lambda *a, **kw: None
                with self.assertRaises(ReviewInputError):
                    _consume_checkpoint_pricing(proof, fixture.ledger, record, (1, 1))
                self.assertEqual(fixture.service.trace, before)

    def test_unregistered_pricer_and_inline_checkpoint_keep_conservative_cost(self):
        from types import SimpleNamespace
        from unittest.mock import Mock

        fake = Mock(return_value={"conditional_calls": 0})
        checkpoints = [{"parts": 1, "conditional_calls": 25}]
        self.assertIsNone(
            _refine_checkpoint_pricing(
                SimpleNamespace(preflight_finding_lifecycle_checkpoint_pricing=fake),
                None,
                checkpoints,
                added_parts=1,
                added_bytes=33000,
            )
        )
        fake.assert_not_called()
        self.assertEqual(checkpoints[0]["conditional_calls"], 25)
        with tempfile.TemporaryDirectory() as root:
            fixture, record, _ = parts_fixtures.CurrentRunFindingPartsTests().fixture(
                root
            )
            checkpoints = [
                {"parts": 0, "conditional_calls": 17},
                {"parts": 1, "conditional_calls": 25},
            ]
            self.assertIsNotNone(
                _refine_checkpoint_pricing(
                    fixture.ledger,
                    record,
                    checkpoints,
                    added_parts=1,
                    added_bytes=33000,
                )
            )
            self.assertEqual([c["conditional_calls"] for c in checkpoints], [17, 7])

    def test_one_short_original_budget_refuses_without_dispatch(self):
        with tempfile.TemporaryDirectory() as root:
            fixture, record, _ = parts_fixtures.CurrentRunFindingPartsTests().fixture(
                root
            )
            before = list(fixture.service.trace)
            with patch.object(
                fixture.ledger.evidence_budget, "ordinary_remaining", return_value=13
            ):
                with self.assertRaises(ReviewInputError):
                    self.plan(fixture.ledger, record)
            self.assertEqual(fixture.service.trace, before)

    def test_zero_part_inline_uses_same_guarded_writer_and_original_physical_budget(
        self,
    ):
        from review_sensei.finding_lifecycle_store import preflight_finding_lifecycle

        with tempfile.TemporaryDirectory() as root:
            fixture, record, kwargs = (
                parts_fixtures.CurrentRunFindingPartsTests().fixture(root)
            )
            kwargs["document"] = {"small": "exact current checkpoint"}
            before = len(fixture.caller.observations)
            plan = preflight_finding_lifecycle(fixture.ledger, record, **kwargs)
            self.assertEqual(plan["checkpoints"][0]["parts"], 0)
            self.assertEqual(plan["checkpoints"][0]["conditional_calls"], 5)
            updated = stage_finding_lifecycle(fixture.ledger, record, **kwargs)
            self.assertEqual(
                updated.finding_lifecycle["manifest"]["encoding"], "inline-json-v1"
            )
            self.assertLessEqual(len(fixture.caller.observations) - before, 5)
            fixture.assert_physical_accounting(self)
            self.assertIsNone(
                getattr(fixture.ledger, "_finding_lifecycle_page_size", None)
            )

    def test_zero_part_inline_frontier_growth_refuses_without_pagination_or_write(self):
        from fake_github_http import json_response

        from review_sensei.finding_lifecycle_store import preflight_finding_lifecycle

        with tempfile.TemporaryDirectory() as root:
            fixture, record, kwargs = (
                parts_fixtures.CurrentRunFindingPartsTests().fixture(root)
            )
            kwargs["document"] = {"small": "exact current checkpoint"}
            preflight_finding_lifecycle(fixture.ledger, record, **kwargs)
            original = fixture.caller._opener
            attempts = []

            def grown(request, timeout):
                attempts.append((request.get_method(), request.full_url))
                if (
                    request.get_method() == "GET"
                    and "/issues/7/comments?" in request.full_url
                ):
                    return json_response(
                        [{"id": i, "body": "discussion"} for i in range(100)]
                    )
                return original(request, timeout)

            fixture.caller._opener = grown
            with self.assertRaisesRegex(
                ReviewInputError, "single-page frontier changed"
            ):
                stage_finding_lifecycle(fixture.ledger, record, **kwargs)
            self.assertEqual([method for method, _ in attempts], ["GET"])
            self.assertIn("per_page=100&page=1", attempts[0][1])
            self.assertEqual(
                fixture.caller._effect_record.finding_lifecycle,
                record.finding_lifecycle,
            )
