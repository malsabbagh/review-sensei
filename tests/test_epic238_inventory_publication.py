"""Synthetic issue241 patterns; no missing SiteVault provider metadata is inferred."""

import re
import unittest
from dataclasses import replace
from unittest.mock import patch

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.context import finding_lifecycle_for_comment
from review_sensei.coverage import CoverageManifest, FileCoverage
from review_sensei.errors import ReviewInputError
from review_sensei.hosting.github.publication import approval_eligibility_from_body
from review_sensei.hosting.github.publication_parts import (
    MAX_PROSE_PART_BYTES,
    FindingProseReadback,
    prepare_finding_prose_parts,
    verify_finding_prose_readbacks,
)
from review_sensei.human_assessment import (
    MAX_HUMAN_REVIEW_BYTES,
    HumanAssessmentDecision,
    HumanInventoryResolution,
    PendingHumanReview,
    finding_instance_fingerprint,
)
from review_sensei.models import ReviewComment
from review_sensei.presentation import assign_finding_identifiers, escape_markdown_label
from tests.test_evidence_capacity import (
    human_result,
    publish,
    varied_comments,
)
from tests.test_human_assessment import BASE, HEAD, State


def observed_location_pattern():
    # Paths/bodies are invented normalized input, not a reconstructed live run.
    return tuple(
        ReviewComment(
            path=path,
            line=line,
            side="FILE" if line is None else "RIGHT",
            body=f"Independent explanation {index}: preserve the full exact transition.",
            blocking=False,
            severity="high",
            needs_human=True,
        )
        for index, (path, line) in enumerate(
            (
                ("src/editor.tsx", 163),
                ("src/editor.tsx", 323),
                ("src/editor.tsx", 407),
                ("src/editor.tsx", 293),
                ("src/editor.tsx", None),
                ("tests/session.spec.ts", 2320),
                ("src/toolbar.tsx", 44),
                ("src/toolbar.tsx", 172),
            )
        )
    )


class FindingInstanceTests(unittest.TestCase):
    def test_eight_explanations_have_stable_unique_instances_and_separate_concerns(
        self,
    ):
        comments = observed_location_pattern()
        concerns = [
            finding_lifecycle_for_comment(item).fingerprint for item in comments
        ]
        self.assertEqual(len(set(concerns)), 3)
        instances = [finding_instance_fingerprint(item) for item in comments]
        self.assertEqual(len(set(instances)), 8)
        whole = PendingHumanReview.from_result(human_result(comments), BASE)
        single = PendingHumanReview.from_result(human_result(comments[:1]), BASE)
        self.assertIn(single.findings[0], whole.findings)
        self.assertEqual(
            whole,
            PendingHumanReview.from_result(
                human_result(tuple(reversed(comments))), BASE
            ),
        )
        self.assertEqual({item.fingerprint for item in whole.findings}, set(instances))

    def test_optional_metadata_and_runtime_admission_do_not_rename_an_instance(self):
        comment = observed_location_pattern()[0]
        annotated = replace(
            comment,
            category="correctness",
            severity="high",
            fix_effort="small",
            symbol="another_annotation",
            evidence_id="a" * 64,
            effective_blocking=False,
        )
        self.assertEqual(
            finding_instance_fingerprint(comment),
            finding_instance_fingerprint(annotated),
        )
        duplicates = human_result((comment, annotated))
        self.assertEqual(duplicates.comments, (comment,))
        self.assertEqual(
            PendingHumanReview.from_result(duplicates, BASE),
            PendingHumanReview.from_result(human_result((comment,)), BASE),
        )
        for changed in (
            replace(comment, line=164),
            replace(comment, side="LEFT"),
            replace(comment, body=comment.body + " Additional evidence."),
            replace(comment, path="src/another.tsx"),
        ):
            self.assertNotEqual(
                finding_instance_fingerprint(comment),
                finding_instance_fingerprint(changed),
            )

    def test_full_id_decision_clears_one_shared_concern_obligation(self):
        inventory = PendingHumanReview.from_result(
            human_result(observed_location_pattern()), BASE
        )
        selected = inventory.findings[0].fingerprint
        decision = HumanAssessmentDecision(
            selected,
            "dismissed",
            "A validated decision.",
            "Exact source.",
            "Exact diff.",
        )
        updated = inventory.apply((decision,))
        self.assertEqual(updated.resolved, (selected,))
        self.assertEqual(len(updated.pending), 7)
        with self.assertRaises(ReviewInputError):
            HumanAssessmentDecision(
                selected[:6], "dismissed", "rationale", "source", "diff"
            )

    def test_prefix_collisions_extend_and_irreducible_projection_refuses(self):
        a, b = "123456" + "0" * 58, "123456" + "1" * 58
        self.assertEqual(
            assign_finding_identifiers((a, b)), assign_finding_identifiers((b, a))
        )
        self.assertEqual(len(set(assign_finding_identifiers((a, b)).values())), 2)
        with self.assertRaises(ReviewInputError):
            assign_finding_identifiers(("a" * 64, "a" * 16 + "b" * 48))


class CompleteInventoryTests(unittest.TestCase):
    def test_varied_100_250_distinct_paths_and_partial_all_resolution_growth(self):
        for count in (100, 250):
            for detail in (1, 3):
                with self.subTest(count=count, detail=detail):
                    comments = varied_comments(count, detail=detail, unique_paths=count)
                    comments = tuple(
                        replace(item, body=item.body + f"\n精确🙂 NFC é {index} [\\]")
                        for index, item in enumerate(comments)
                    )
                    inventory = PendingHumanReview.from_result(
                        human_result(comments), BASE
                    )
                    restored = PendingHumanReview.from_inventory_document(
                        inventory.inventory_document()
                    )
                    self.assertEqual(restored, inventory)
                    self.assertEqual(
                        len({item.path for item in restored.findings}), count
                    )
                    with self.assertRaises(ReviewInputError):
                        restored.to_dict()  # No claim that the old host marker supports this.
                    for resolved in (
                        (),
                        tuple(item.fingerprint for item in inventory.findings[::2]),
                        tuple(item.fingerprint for item in inventory.findings),
                    ):
                        updated = replace(inventory, resolved=resolved)
                        receipt = HumanInventoryResolution.from_inventory(updated)
                        self.assertLessEqual(
                            len(canonical_bytes(receipt.to_dict())),
                            MAX_HUMAN_REVIEW_BYTES,
                        )
                        read_receipt = HumanInventoryResolution.from_dict(
                            receipt.to_dict()
                        )
                        self.assertEqual(read_receipt.restore(restored), updated)
                        self.assertEqual(
                            updated.inventory_digest, inventory.inventory_digest
                        )

    def test_receipt_cannot_transfer_to_new_authority_or_unknown_id(self):
        inventory = PendingHumanReview.from_result(
            human_result(observed_location_pattern()), BASE
        )
        receipt = HumanInventoryResolution(
            inventory.inventory_digest, (inventory.findings[0].fingerprint,)
        )
        new = replace(inventory, base_sha="c" * 40)
        with self.assertRaises(ReviewInputError):
            receipt.restore(new)
        resolved = receipt.restore(inventory)
        with self.assertRaises(ReviewInputError):
            HumanInventoryResolution(inventory.inventory_digest).restore(resolved)
        with self.assertRaises(ReviewInputError):
            HumanInventoryResolution(inventory.inventory_digest, ("f" * 64,)).restore(
                inventory
            )
        with self.assertRaises(ReviewInputError):
            PendingHumanReview.from_inventory_document(
                {**inventory.inventory_document(), "resolved": list(receipt.resolved)}
            )

    def test_metadata_classification_and_required_paths_change_inventory_authority(
        self,
    ):
        comment = observed_location_pattern()[0]
        original = PendingHumanReview.from_result(human_result((comment,)), BASE)
        receipt = HumanInventoryResolution(
            original.inventory_digest, (original.findings[0].fingerprint,)
        )
        for annotated in (
            replace(comment, category="correctness"),
            replace(comment, blocking=True),
        ):
            changed = PendingHumanReview.from_result(human_result((annotated,)), BASE)
            self.assertEqual(
                changed.findings[0].fingerprint, original.findings[0].fingerprint
            )
            self.assertNotEqual(changed.inventory_digest, original.inventory_digest)
            with self.assertRaises(ReviewInputError):
                receipt.restore(changed)
        extended = replace(
            original,
            findings=(
                replace(
                    original.findings[0],
                    required_paths=(comment.path, "src/related.py"),
                ),
            ),
        )
        with self.assertRaises(ReviewInputError):
            receipt.restore(extended)

    def test_twenty_resolved_then_new_shared_concern_preserves_original_ids(self):
        from review_sensei.hosting.github.approval import (
            approval_eligibility_from_result,
        )

        comments = varied_comments(20)
        previous = approval_eligibility_from_result(
            human_result(comments),
            head_sha=HEAD,
            base_sha=BASE,
            enabled=True,
            app_authored=False,
        )
        previous = replace(
            previous,
            human_review=replace(
                previous.human_review,
                resolved=tuple(
                    item.fingerprint for item in previous.human_review.findings
                ),
            ),
            facts=replace(previous.facts, has_human_adjudication_findings=False),
        )
        new_comment = replace(
            comments[0],
            line=501,
            body="A new independent explanation of the same lifecycle concern.",
        )
        expanded = approval_eligibility_from_result(
            human_result((new_comment,)),
            head_sha=HEAD,
            base_sha=BASE,
            enabled=True,
            app_authored=False,
            retained_eligibility=previous,
        )
        self.assertEqual(expanded.human_review.resolved, previous.human_review.resolved)
        self.assertEqual(len(expanded.human_review.findings), 21)
        self.assertEqual(
            tuple(item.fingerprint for item in expanded.human_review.pending),
            (finding_instance_fingerprint(new_comment),),
        )

    def test_legacy_id_is_unchanged_and_literal_paths_are_retained(self):
        legacy = {
            "base_sha": BASE,
            "findings": [
                {
                    "fingerprint": "e" * 64,
                    "path": "src/[abc]*?.py",
                    "body": "Original evidence.",
                }
            ],
            "resolved": ["e" * 64],
        }
        restored = PendingHumanReview.from_dict(legacy)
        self.assertEqual(restored.to_dict(), legacy)
        self.assertEqual(restored.pending, ())


class ProsePublicationTests(unittest.TestCase):
    def test_varied_parts_retain_each_full_explanation_and_unique_target(self):
        for count in (100, 250):
            with self.subTest(count=count):
                comments = varied_comments(count, detail=3, unique_paths=count)
                parts = prepare_finding_prose_parts(comments, head_sha=HEAD)
                self.assertEqual(
                    parts,
                    prepare_finding_prose_parts(
                        tuple(reversed(comments)), head_sha=HEAD
                    ),
                )
                self.assertGreater(len(parts), 1)
                targets = [instance for part in parts for instance in part.instances]
                self.assertEqual(len(set(targets)), count)
                all_prose = "\n\n".join(part.body for part in parts)
                for comment in comments:
                    self.assertTrue(
                        comment.body in all_prose, "complete prose was lost"
                    )
                    self.assertTrue(
                        escape_markdown_label(comment.path) in all_prose,
                        "complete path was lost",
                    )
                for part in parts:
                    self.assertLessEqual(part.byte_length, MAX_PROSE_PART_BYTES)
                    self.assertLessEqual(len(part.paths), 64)
                    self.assertNotIn("reviewsensei:eligibility:", part.body)
                    self.assertNotIn("reviewsensei:review:", part.body)
                readbacks = tuple(
                    FindingProseReadback(100 + index, 7, HEAD, part.body)
                    for index, part in enumerate(parts)
                )
                receipts = verify_finding_prose_readbacks(
                    parts, readbacks, producer_id=7, head_sha=HEAD
                )
                self.assertEqual(
                    [receipt["sha256"] for receipt in receipts],
                    [part.sha256 for part in parts],
                )

    def test_exact_64_65_paths_and_framed_bytes(self):
        self.assertEqual(
            len(
                prepare_finding_prose_parts(
                    varied_comments(64, unique_paths=64), head_sha=HEAD
                )
            ),
            1,
        )
        self.assertEqual(
            len(
                prepare_finding_prose_parts(
                    varied_comments(65, unique_paths=65), head_sha=HEAD
                )
            ),
            2,
        )
        comment = observed_location_pattern()[0]
        part = prepare_finding_prose_parts((comment,), head_sha=HEAD)[0]
        # The conservative maximum-count header reserve is two bytes wider.
        for delta, accepted in ((0, True), (-1, False)):
            with patch(
                "review_sensei.hosting.github.publication_parts.MAX_PROSE_PART_BYTES",
                part.byte_length + 2 + delta,
            ):
                if accepted:
                    self.assertEqual(
                        len(prepare_finding_prose_parts((comment,), head_sha=HEAD)), 1
                    )
                else:
                    with self.assertRaises(ReviewInputError):
                        prepare_finding_prose_parts((comment,), head_sha=HEAD)

    def test_missing_duplicate_reordered_changed_wrong_owner_and_head_refuse(self):
        parts = prepare_finding_prose_parts(
            varied_comments(65, unique_paths=65), head_sha=HEAD
        )
        reads = tuple(
            FindingProseReadback(index + 1, 7, HEAD, part.body)
            for index, part in enumerate(parts)
        )
        invalid = (
            reads[:1],
            tuple(reversed(reads)),
            (reads[0], replace(reads[1], storage_id=1)),
            (replace(reads[0], body=reads[0].body + "!"), reads[1]),
            (replace(reads[0], producer_id=8), reads[1]),
            (replace(reads[0], head_sha="c" * 40), reads[1]),
        )
        for observed in invalid:
            with self.subTest(observed=observed):
                with self.assertRaises(ReviewInputError):
                    verify_finding_prose_readbacks(
                        parts, observed, producer_id=7, head_sha=HEAD
                    )

    def test_optional_narrative_cannot_replace_host_facts_or_partial_withholding(self):
        comments = observed_location_pattern()
        state = State()
        # Exercise actual full-review POST and finalizer, not only the renderer.
        published = human_result(comments)
        published = replace(
            published,
            summary="Several defects must be fixed before merge. Look only at inline comments.",
            review_status="partial",
            coverage=CoverageManifest(
                files=tuple(
                    FileCoverage(path=path, outcome="unsupported")
                    for path in sorted({item.path for item in comments})
                ),
                enumeration_complete=True,
                enumerated_paths=tuple(sorted({item.path for item in comments})),
            ),
        )
        with patch("tests.test_evidence_capacity.human_result", return_value=published):
            outcome = publish(state, comments)
        self.assertEqual(outcome.status, "published")
        self.assertNotIn("APPROVE", state.events())
        body = state.reviews[-1]["body"]
        authoritative = body.split("### Provider narrative", 1)[0]
        self.assertIn("No required fixes · 8 optional improvements", authoritative)
        self.assertNotIn("must be fixed before merge", authoritative)
        self.assertIn("Review incomplete", authoritative)
        self.assertIn("explained in this review body", authoritative)
        authority = approval_eligibility_from_body(body)
        self.assertEqual(authority.facts.coverage_blocker, "partial")
        self.assertEqual(len(authority.human_review.findings), 8)
        identifiers = assign_finding_identifiers(
            finding_instance_fingerprint(item) for item in comments
        )
        for comment in comments:
            identity = identifiers[finding_instance_fingerprint(comment)]
            self.assertIn(f"`{identity}`", body)
            self.assertIn(comment.body, body)
        self.assertEqual(len(set(re.findall(r"`(RS-[A-F0-9]+)`", body))), 8)


if __name__ == "__main__":
    unittest.main()
