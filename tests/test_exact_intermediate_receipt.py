"""Pure receipt sizing, without a hosted issuer or publication grant."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from review_sensei_github import quarantine_action, quarantine_command_finalization

from review_sensei import presentation
from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError


class ExactIntermediateReceiptTests(unittest.TestCase):
    def inputs(self):
        return dict(
            action="dismiss",
            scope="a" * 64,
            instance="b" * 12,
            operator_paused=False,
            actor="maintainer",
            source_comment_id=7905,
            command_digest="c" * 64,
        )

    def test_matches_original_producers_for_both_actions(self):
        for action in ("dismiss", "accept-risk"):
            for paused in (False, True):
                data = self.inputs()
                data.update(action=action, operator_paused=paused)
                result = quarantine_action.historical_command_result(
                    SimpleNamespace(action=action, scope=data["scope"]),
                    SimpleNamespace(instance=data["instance"]),
                    SimpleNamespace(operator_paused=paused),
                    existing=None,
                    actor={"login": data["actor"]},
                )
                expected = presentation.render_command_receipt(
                    actor=data["actor"],
                    source_comment_id=data["source_comment_id"],
                    command_digest=data["command_digest"],
                    receipts=result.receipts,
                    ledger_written=True,
                )
                text, wire = quarantine_command_finalization.exact_intermediate_receipt(
                    **data
                )
                self.assertEqual(text, expected)
                self.assertEqual(wire, len(canonical_bytes({"body": expected})))
                self.assertLess(wire, 65536)

    def test_hostile_values_never_execute_callbacks(self):
        calls = []

        class Poison:
            def __eq__(self, other):
                calls.append("eq")
                raise AssertionError("callback")

            def __str__(self):
                calls.append("str")
                raise AssertionError("callback")

        for key in self.inputs():
            data = self.inputs()
            data[key] = Poison()
            with self.assertRaisesRegex(ReviewInputError, "primitives are invalid"):
                quarantine_command_finalization.exact_intermediate_receipt(**data)
        self.assertEqual(calls, [])

    def test_original_maximum_and_domain_remain_holds(self):
        for field, value in (
            ("actor", "x" * 65536),
            ("action", "retry"),
            ("source_comment_id", True),
            ("source_comment_id", 0),
        ):
            data = self.inputs()
            data[field] = value
            with self.assertRaises(ReviewInputError):
                quarantine_command_finalization.exact_intermediate_receipt(**data)

    def test_replaced_producer_is_rejected_before_callback(self):
        calls = []

        def replaced(*args, **kwargs):
            calls.append(True)
            raise AssertionError("callback")

        for module, name in (
            (quarantine_action, "historical_command_result"),
            (presentation, "render_command_receipt"),
        ):
            with patch.object(module, name, replaced):
                with self.assertRaises(ReviewInputError):
                    quarantine_command_finalization.exact_intermediate_receipt(
                        **self.inputs()
                    )
        self.assertEqual(calls, [])
