"""A separate value construction retains counters and cannot complete the old run."""

import unittest
from copy import deepcopy
from dataclasses import replace

from review_sensei_github.api_run_host import review_startup_plan
from test_quarantine_isolation import predecessor
from test_quarantine_lineage import lineage_value

from review_sensei.api_work import (
    ApiExecutionOwner,
    ApiRunIdentity,
    FrozenApiConfiguration,
    initial_accounting,
)
from review_sensei.bounded_evidence import inline_manifest
from review_sensei.errors import ReviewInputError
from review_sensei.quarantine_lineage import lineage_scope


def successor_values(old):
    value = lineage_value()
    value["predecessor_record_sha256"] = old.record_sha256
    value["scope_sha256"] = lineage_scope(value)
    value["assessment_state_manifest"] = inline_manifest(
        {
            "schema_version": "quarantine-assessment-state-v1",
            "scope_sha256": value["scope_sha256"],
            "settlements": [],
        },
        binding={
            **value["assessment_state_manifest"]["binding"],
            "policy_digest": value["scope_sha256"],
        },
        item_count=0,
    )
    state = initial_accounting(
        ApiRunIdentity(123, 900),
        FrozenApiConfiguration("c" * 40, "d" * 64, 512),
        ApiExecutionOwner(1, "hosted", 0),
        review_startup_plan("hosted"),
        prior_nonstartup_liability_upper_bound=0,
    )
    return value, state


class QuarantineRecordTests(unittest.TestCase):
    def test_published_tail_value_preserves_the_missing_terminal_marker(self):
        old, _ = predecessor()
        old = old.evolve(
            transaction=replace(old.transaction, phase="publication_succeeded")
        )
        lineage, state = successor_values(old)
        with self.assertRaises(ReviewInputError):
            old.quarantine_api_accounting(state.to_document(), lineage)
        lineage["schema_version"] = "quarantine-published-tail-v1"
        lineage["scope_sha256"] = lineage_scope(lineage)
        lineage["assessment_state_manifest"] = inline_manifest(
            {
                "schema_version": "quarantine-assessment-state-v1",
                "scope_sha256": lineage["scope_sha256"],
                "settlements": [],
            },
            binding={
                **lineage["assessment_state_manifest"]["binding"],
                "policy_digest": lineage["scope_sha256"],
            },
            item_count=0,
        )
        before = deepcopy(old.to_dict())
        successor = old.quarantine_api_accounting(state.to_document(), lineage)
        self.assertEqual(old.to_dict(), before)
        self.assertIsNone(old.api_run_accounting["terminal_transaction"])
        self.assertEqual(old.transaction.phase, "publication_succeeded")
        self.assertEqual(
            successor.completed_initial_reviews, old.completed_initial_reviews
        )
        self.assertEqual(successor.api_run_accounting, state.to_document())
        with self.assertRaises(ReviewInputError):
            old.rollover_api_accounting(state.to_document())

    def test_new_value_preserves_limits_history_and_old_accounting_without_completion(
        self,
    ):
        old, _ = predecessor()
        old = old.evolve(
            failed_attempts=3, failed_attempts_head_sha=old.transaction.head_sha
        )
        before = deepcopy(old.to_dict())
        lineage, state = successor_values(old)
        successor = old.quarantine_api_accounting(state.to_document(), lineage)
        self.assertEqual(old.to_dict(), before)
        self.assertIsNone(old.api_run_accounting["terminal_transaction"])
        self.assertIsNone(successor.transaction)
        self.assertEqual(successor.generation, old.generation + 1)
        for field in (
            "completed_initial_reviews",
            "completed_verification_rounds",
            "failed_attempts",
            "failed_attempts_head_sha",
            "last_committed_reservation_id",
            "created_at",
            "expires_at",
            "convergence_history",
            "dispositions",
            "disposition_snapshot",
            "assessment_queue",
            "continuation_grants",
        ):
            self.assertEqual(getattr(successor, field), getattr(old, field), field)
        self.assertEqual(
            successor.quarantine_lineage["predecessor_record_sha256"], old.record_sha256
        )
        self.assertEqual(successor.api_run_accounting, state.to_document())
        self.assertEqual(type(old).from_dict(successor.to_dict()), successor)
        with self.assertRaises(ReviewInputError):
            old.rollover_api_accounting(state.to_document())

    def test_same_run_foreign_snapshot_or_existing_lineage_cannot_construct_successor(
        self,
    ):
        old, _ = predecessor()
        lineage, state = successor_values(old)
        for document in (old.api_run_accounting,):
            with self.assertRaises(ReviewInputError):
                old.quarantine_api_accounting(document, lineage)
        with self.assertRaises(ReviewInputError):
            old.quarantine_api_accounting(state.to_document(), lineage_value())
        successor = old.quarantine_api_accounting(state.to_document(), lineage)
        with self.assertRaises(ReviewInputError):
            successor.quarantine_api_accounting(state.to_document(), lineage)
