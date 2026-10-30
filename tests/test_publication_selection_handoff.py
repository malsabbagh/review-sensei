"""Inactive selection intent survives accepted-operation retention exactly."""

import copy
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.errors import ReviewInputError
from review_sensei.models import (
    FindingLifecycleRecord,
    ReviewComment,
    ReviewResult,
    ReviewTransaction,
)
from review_sensei.publication_selection import (
    PublicationCandidate,
    PublicationSelectionHandoff,
    PublishedSlot,
    freeze_publication_selection,
    validate_publication_selection_source,
)
from review_sensei.review_verdict import bind_review_verdict
from review_sensei.schemas import validate_public_document


class PublicationSelectionHandoffTests(unittest.TestCase):
    def setUp(self):
        self.policy = ReviewConvergencePolicy(quality="high-signal")
        self.rows = (
            PublicationCandidate(
                ReviewComment(
                    "a.py", 1, "Required fix", blocking=False, effective_blocking=True
                ),
                True,
                "critical",
                "causal-source",
            ),
            PublicationCandidate(
                ReviewComment("b.py", 2, "Existing advisory", effective_blocking=False),
                False,
                None,
                "advisory",
                "comment:existing",
            ),
            PublicationCandidate(
                ReviewComment(
                    "c.py",
                    3,
                    "Human decision",
                    effective_blocking=False,
                    needs_human=True,
                ),
                True,
                "high",
                "human-judgment",
                "comment:released",
            ),
        )
        self.slots = (
            PublishedSlot("comment:existing", "active", "a" * 64),
            PublishedSlot("comment:released", "released", "b" * 64),
        )
        self.source = self.source_for(self.rows)

    def source_for(self, rows):
        return bind_review_verdict(
            ReviewResult(
                "Complete.",
                tuple(row.comment for row in rows),
                "fixture",
                review_status="complete",
            ),
            policy=self.policy,
        )

    def freeze(self, **kwargs):
        return freeze_publication_selection(
            kwargs.pop("source", self.source),
            candidates=kwargs.pop("candidates", self.rows),
            slots=kwargs.pop("slots", self.slots),
            max_active_findings=kwargs.pop("max_active_findings", 2),
            policy=kwargs.pop("policy", self.policy),
            **kwargs,
        )

    def test_roundtrip_binds_selection_and_keeps_unselected_verdict_without_queue(self):
        selected = self.freeze()
        handoff = selected.publication_selection
        self.assertEqual(handoff.source_result_sha256, self.source.content_digest())
        self.assertEqual(handoff.policy_digest, self.policy.digest())
        self.assertEqual(
            tuple(item.action for item in handoff.actions), ("create", "reuse")
        )
        self.assertTrue(selected.review_verdict.unselected_human_decision)
        self.assertFalse(selected.review_verdict.unselected_blocking_defect)
        document = selected.to_dict()
        validate_public_document(document, "review-result")
        validate_public_document(handoff.to_document(), "publication-selection")
        self.assertNotIn("Human decision", json.dumps(document))
        self.assertNotIn("deferred", json.dumps(document))
        cold = ReviewResult.from_dict(document)
        self.assertEqual(cold, selected)
        self.assertEqual(cold.content_digest(), selected.content_digest())
        cold.publication_selection.require_occupancy(self.slots)
        validate_publication_selection_source(
            self.source,
            cold.publication_selection,
            candidates=self.rows,
            slots=self.slots,
            policy=self.policy,
        )

    def test_existing_targets_require_reuse_or_reopening_with_exact_proof(self):
        slots = (
            PublishedSlot("active", "active", "1" * 64),
            PublishedSlot("contingent", "contingent", "2" * 64),
            PublishedSlot("released", "released", "3" * 64),
        )
        rows = tuple(
            PublicationCandidate(
                ReviewComment(f"{state}.py", 1, state, effective_blocking=False),
                False,
                None,
                "advisory",
                state,
            )
            for state in ("active", "contingent", "released")
        )
        result = self.freeze(
            source=self.source_for(rows),
            candidates=rows,
            slots=slots,
            max_active_findings=3,
        )
        entries = {
            item.existing_target: item for item in result.publication_selection.actions
        }
        self.assertEqual(
            (entries["active"].action, entries["active"].consumes_slot),
            ("reuse", False),
        )
        self.assertEqual(
            (entries["contingent"].action, entries["contingent"].consumes_slot),
            ("reopen", False),
        )
        self.assertEqual(
            (entries["released"].action, entries["released"].consumes_slot),
            ("reopen", True),
        )
        self.assertEqual(entries["released"].target_proof_sha256, "3" * 64)
        invalid = result.publication_selection.to_document()
        invalid["actions"][0]["action"] = "create"
        with self.assertRaises(ReviewInputError):
            PublicationSelectionHandoff.from_document(invalid)
        with self.assertRaises(ReviewInputError):
            result.publication_selection.require_occupancy(
                (replace(slots[0], proof_sha256="4" * 64), *slots[1:])
            )
        with self.assertRaises(ReviewInputError):
            self.freeze(
                source=self.source_for(rows),
                candidates=rows,
                slots=(replace(slots[0], state="uncertain"), *slots[1:]),
                max_active_findings=3,
            )

    def test_closed_decode_and_source_policy_binding(self):
        handoff = self.freeze().publication_selection
        mutations = (
            lambda d: d.update(extra=True),
            lambda d: d.update(schema_version="future"),
            lambda d: d.update(max_active_findings=True),
            lambda d: d["actions"][0].update(extra=True),
            lambda d: d["actions"][0].update(consumes_slot=1),
            lambda d: d["actions"].append(copy.deepcopy(d["actions"][0])),
        )
        for change in mutations:
            document = handoff.to_document()
            change(document)
            with self.subTest(document=document), self.assertRaises(ReviewInputError):
                PublicationSelectionHandoff.from_document(document)
        changed = replace(handoff, source_result_sha256="d" * 64)
        with self.assertRaises(ReviewInputError):
            validate_publication_selection_source(
                self.source,
                changed,
                candidates=self.rows,
                slots=self.slots,
                policy=self.policy,
            )
        with self.assertRaises(ReviewInputError):
            self.freeze(policy=ReviewConvergencePolicy())
        with self.assertRaises(ReviewInputError):
            self.freeze(candidates=self.rows[:-1])
        with self.assertRaises(ReviewInputError):
            self.freeze(source=replace(self.source, review_verdict=None))

    def test_bound_result_cannot_mutate_selection_or_remove_prior_obligations(self):
        from review_sensei.context import finding_lifecycle_for_comment

        selected = self.freeze()
        with self.assertRaises(ReviewInputError):
            replace(selected, summary="Different accepted result")
        changed = selected.to_dict()
        changed["summary"] = "Different accepted result"
        with self.assertRaises(ReviewInputError):
            ReviewResult.from_dict(changed)
        with self.assertRaises(ReviewInputError):
            self.freeze(source=selected)
        fingerprint = finding_lifecycle_for_comment(self.rows[2].comment).fingerprint
        prior = replace(
            self.source,
            finding_lifecycles=(FindingLifecycleRecord(fingerprint, "still-present"),),
        )
        with self.assertRaisesRegex(ReviewInputError, "retained obligation"):
            self.freeze(source=prior)

    def test_sealed_transaction_binds_all_handoff_bytes_and_default_is_unchanged(self):
        self.assertNotIn("publication_selection", self.source.to_dict())
        before = self.source.content_digest()
        self.assertEqual(
            ReviewResult.from_dict(self.source.to_dict()).content_digest(), before
        )
        selected = self.freeze()
        transaction = ReviewTransaction.create(
            repository="owner/repo",
            pull_request=1,
            base_sha="a" * 40,
            head_sha="b" * 40,
            policy_digest=self.policy.digest(),
            configuration_digest="c" * 64,
            evidence_digest="d" * 64,
            reservation_id="e" * 64,
            generation=1,
        ).with_result(selected.content_digest())
        sealed = replace(selected, transaction=transaction)
        self.assertEqual(ReviewResult.from_dict(sealed.to_dict()), sealed)
        for field in ("source_result_sha256", "occupancy_sha256", "remove"):
            document = sealed.to_dict()
            if field == "remove":
                document.pop("publication_selection")
            else:
                self.assertIn(field, selected.publication_selection.to_document())
                self.assertNotEqual(document["publication_selection"][field], "f" * 64)
                document["publication_selection"][field] = "f" * 64
            with self.subTest(field=field), self.assertRaises(ReviewInputError):
                ReviewResult.from_dict(document)
        with self.assertRaises(ReviewInputError):
            self.freeze(
                source=replace(
                    self.source,
                    transaction=replace(
                        transaction, result_sha256=self.source.content_digest()
                    ),
                )
            )

    def test_accepted_operation_replay_does_not_reselect_or_replace_targets(self):
        from review_sensei_github.operation_comments import ResultPartStore
        from review_sensei_github.operation_entry import (
            analysis_payload,
            run_hosted_provider,
        )
        from review_sensei_github.operation_host import GitHubCommentOperationStore
        from test_operation_entry import MemoryComments, _hex

        comments = MemoryComments()
        store = GitHubCommentOperationStore(comments, app_user_id=42)
        parts = ResultPartStore(comments, app_user_id=42)
        calls = []

        def execute():
            calls.append(True)
            return SimpleNamespace(result=self.freeze())

        kwargs = dict(
            trigger="full-review",
            repository_id=7,
            pull_request=11,
            attestation_digest=_hex("attestation"),
            run_id="100",
            run_attempt="1",
            app_id=99,
            reservation_id="reservation-1",
            delivery_id="delivery-1",
            grant="grant-1",
            server_started_ms=1000,
            execute=execute,
            encode=analysis_payload,
            parts=parts,
            now=lambda: 1000,
        )
        first = run_hosted_provider(store, **kwargs)
        with patch(
            "review_sensei.publication_selection.select_publication",
            side_effect=AssertionError("reselection"),
        ):
            second = run_hosted_provider(store, **kwargs)
            cold = ReviewResult.from_dict(json.loads(second.replay_payload))
        self.assertEqual(len(calls), 1)
        self.assertEqual(cold.content_digest(), first.fresh.result.content_digest())
        self.assertEqual(
            cold.publication_selection.actions[1].existing_target, "comment:existing"
        )
        self.assertEqual(cold.publication_selection.actions[1].action, "reuse")
