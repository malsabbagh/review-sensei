"""Detached result sizing uses raw codec bounds, never observed compression."""

import unittest
from dataclasses import asdict, replace
from types import SimpleNamespace

from review_sensei_github import approval_progress
from review_sensei_github import command_result_capacity as bound
from review_sensei_github.application import GitHubApplication
from review_sensei_github.operation_diagnostics import (
    OperationDiagnostics,
    OperationFailure,
    PhaseFailures,
)

from review_sensei.action_result_codec import encode_action_result_carrier
from review_sensei.bounded_evidence import canonical_bytes, inline_manifest
from review_sensei.disposition import MaintainerCommandResult
from review_sensei.errors import ReviewInputError

BINDING = {
    "repository": "owner/repo",
    "repository_id": 123,
    "pull_request": 7,
    "base_sha": "b" * 40,
    "head_sha": "a" * 40,
    "policy_digest": "c" * 64,
    "configuration_digest": "d" * 64,
    "generation": 1,
    "producer": "github-bot:123",
    "purpose": "evidence",
    "schema_version": "1.0",
}


class CommandResultWireBoundTests(unittest.TestCase):
    def test_raw_upper_is_only_an_inert_outer_manifest(self):
        from review_sensei.action_result_codec import decode_action_result_carrier
        from review_sensei.bounded_evidence import read_inline_evidence

        for raw in (1, 1024, 4096):
            with self.subTest(raw=raw):
                manifest = bound.raw_result_manifest_upper(
                    envelope_bytes=raw, binding=BINDING, reference_history=True
                )
                self.assertEqual(
                    len(canonical_bytes(manifest)),
                    bound.raw_result_manifest_bound(
                        envelope_bytes=raw, binding=BINDING, reference_history=True
                    ),
                )
                # The genuine root/accounting validator may size its outer
                # inline shape. No reader can mistake the synthetic payload for
                # a decodable, authenticated terminal result.
                carrier = read_inline_evidence(manifest, expected_binding=BINDING)
                with self.assertRaises(ReviewInputError):
                    decode_action_result_carrier(carrier, history={})

    def test_original_formatter_and_codec_fit_raw_bound(self):
        base = MaintainerCommandResult("dismiss", True, False, "decision recorded")
        outcome = {"kind": "applied", "reason": None}
        size = bound.command_result_capacity_bound(
            base,
            binding=BINDING,
            intent_sha256="c" * 64,
            outcome=outcome,
            call_limit=1152,
            source_review_id=42,
            approval_review_id=2**63 - 1,
            reference_history=True,
        )
        strings = GitHubApplication._refinalize_command.__code__.co_consts
        suffixes = [
            value
            for value in strings
            if isinstance(value, str) and value.startswith(";")
        ]
        phase = PhaseFailures(
            OperationFailure(
                "approval.persisted-readback",
                "changed-authority",
                599,
                ("GRAPHQL_VALIDATION_FAILED", "UNAUTHORIZED", "RATE_LIMITED"),
                1152,
            ),
            1000,
        )
        diagnostics = OperationDiagnostics(
            phase,
            phase,
            phase,
            phase,
            1152,
            False,
            1000,
            "inspect_operation_failure",
            1152,
            1152,
        )
        for suffix in suffixes:
            for state in (
                "approved",
                "already_approved",
                "approval_withheld",
                "unknown",
            ):
                progress = approval_progress.build_approval_progress(
                    head_sha="a" * 40,
                    result_digest="d" * 64,
                    source_review_id=2**63 - 1,
                    honored_findings=42,
                    pending_human_assessments=14,
                    body_blocker_remaining=False,
                    review_status="summary-only",
                    coverage_blocker="incomplete",
                    recorded_qualification="not-required",
                    decided_thread_reconciliation="unconfirmed",
                    approval_status=state,
                    approval_review_id=2**63 - 1,
                    check_attempted=False,
                    check_confirmed=False,
                    check_run_id=2**63 - 1,
                    requested_check_conclusion="action_required",
                )
                result = approval_progress.render_command_progress(
                    replace(
                        base,
                        summary=base.summary
                        + "; decision recorded; thread resolution unconfirmed, comment `@reviewsensei verify` to retry"
                        + suffix,
                    ),
                    SimpleNamespace(
                        approval_progress=progress, operation_diagnostics=diagnostics
                    ),
                )
                document = {
                    "schema_version": "caller-action-result-v1",
                    "intent_sha256": "c" * 64,
                    "outcome": outcome,
                    "result": {
                        **asdict(result),
                        "accepted_semantic_history": {"fixture": True},
                    },
                }
                carrier = encode_action_result_carrier(document, reference_history=True)
                manifest = inline_manifest(carrier, binding=BINDING, item_count=1)
                self.assertIsNotNone(manifest)
                self.assertLessEqual(len(canonical_bytes(manifest)), size)
        self.assertLess(size, 8192)

    def test_required_native_identity_domain_is_not_inferred_from_source_seals(self):
        for invalid in (True, 0, -1, 2**63, "42"):
            for field in ("source_review_id", "approval_review_id"):
                with self.subTest(field=field, invalid=invalid):
                    domain = {"source_review_id": 42, "approval_review_id": None}
                    domain[field] = invalid
                    with self.assertRaisesRegex(ReviewInputError, "identity domain"):
                        bound.formatter_growth_bound(call_limit=512, **domain)
        self.assertEqual(
            bound.formatter_growth_bound(
                call_limit=512, source_review_id=2**63 - 1, approval_review_id=2**63 - 1
            ),
            2777,
        )

    def test_unpriced_v1_history_is_not_qualified(self):
        with self.assertRaises(ReviewInputError):
            bound.command_result_capacity_bound(
                MaintainerCommandResult("dismiss", True, False),
                binding=BINDING,
                intent_sha256="c" * 64,
                outcome={"kind": "applied", "reason": None},
                call_limit=512,
                source_review_id=42,
                approval_review_id=None,
                reference_history=False,
            )

    def test_nullable_diagnostics_are_priced_without_numeric_width_slack(self):
        # Null is four bytes, wider than a three-digit HTTP status or 512.
        for limit in (1, 512, 1152):
            self.assertEqual(
                bound.formatter_growth_bound(
                    source_review_id=42, approval_review_id=None, call_limit=limit
                ),
                2762,
            )
        self.assertGreater(
            bound.formatter_growth_bound(
                source_review_id=42, approval_review_id=None, call_limit=10000
            ),
            2762,
        )

    def test_raw_wire_overflow_and_unbounded_call_limit_refuse(self):
        with self.assertRaisesRegex(ReviewInputError, "inline bound"):
            bound.raw_result_manifest_bound(
                envelope_bytes=7000, binding=BINDING, reference_history=True
            )
        for limit in (True, -1, 2**63):
            with self.assertRaises(ReviewInputError):
                bound.formatter_growth_bound(
                    source_review_id=42, approval_review_id=None, call_limit=limit
                )

    def test_formatter_source_mutation_cannot_supply_bound(self):
        original = approval_progress.render_command_progress.__code__
        try:
            approval_progress.render_command_progress.__code__ = (
                lambda result, publication: result
            ).__code__
            with self.assertRaisesRegex(ReviewInputError, "source.*changed"):
                bound.formatter_growth_bound(
                    source_review_id=42, approval_review_id=None, call_limit=1152
                )
        finally:
            approval_progress.render_command_progress.__code__ = original
        self.assertEqual(
            bound.formatter_growth_bound(
                source_review_id=42, approval_review_id=None, call_limit=1152
            ),
            2762,
        )
