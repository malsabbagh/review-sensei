"""Local joint retained-capacity test for baseline, queue and orphan parts.

Scope is deliberately narrow: one LocalSessionLedger retention directory per
case, exercised only through the existing local stage_evidence/replace and
cold-reader seams. This does not qualify transport sizes, hosted comment
caps, writer activation, publication, approval or the full issue lifecycle.

This fixture uses separately constructed ledger objects for the synthetic
checkpoint, unactivated stage, queue write and cold read. It checks that their
observed budgets and deadlines remain unchanged afterward. It does not prove
that a live host has authenticated a distinct source or funded a successor,
and it does not resolve the retained publication-pending transaction. The
checkpoint fixture supplies the large baseline alongside a smaller synthetic
review result; this profile does not establish review coverage.
"""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import test_authenticated_partitions as partitions
import test_review_transaction as fixture
from test_evidence_capacity import human_result, varied_baseline, varied_comments

from review_sensei.assessment_queue import AssessmentJournal, AssessmentQueue
from review_sensei.baseline import baseline_finding_from_comment
from review_sensei.bounded_evidence import (
    MAX_PART_READS,
    MAX_STORED_PART_BYTES,
    MAX_STORED_PARTS,
)
from review_sensei.errors import ReviewInputError
from review_sensei.evidence import EvidenceSnapshot
from review_sensei.human_assessment import PendingHumanReview
from review_sensei.session import (
    LocalSessionLedger,
    assessment_queue_manifest_capacity,
    read_session_assessment_queue,
    read_session_baseline,
)


def _budget_state(budget):
    """Calls plus both original deadline representations of one budget."""
    return (
        budget.calls,
        budget.deadline,
        budget.wall_deadline_ms,
        dict(budget.snapshot()),
    )


def _referenced_ids(manifest):
    if not isinstance(manifest, dict):
        return set()
    parts = manifest.get("parts")
    if not isinstance(parts, list):
        return set()
    return {
        part["storage_id"]
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("storage_id"), str)
    }


def _retained_files(root):
    """Exact retained part files and sizes below this ledger's .evidence tree."""
    evidence_root = root / ".evidence"
    if not evidence_root.is_dir():
        return {}
    return {
        path.relative_to(root).as_posix(): path.stat().st_size
        for path in sorted(evidence_root.rglob("*"))
        if path.is_file()
    }


class JointRetainedCapacityTests(unittest.TestCase):
    def test_baseline_orphan_and_queue_coexist_and_restore_cold(self):
        for count in (100, 250):
            with (
                self.subTest(count=count),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                comments = varied_comments(count, detail=1, unique_paths=count)
                self.assertEqual(len(comments), count)
                self.assertEqual(
                    len({comment.path for comment in comments}), len(comments)
                )

                def make_baseline(_count, comments=comments):
                    # Same construction as varied_baseline, but with every
                    # path distinct for this case instead of the default pool.
                    self.assertEqual(_count, count)
                    return replace(
                        varied_baseline(1),
                        findings=tuple(
                            sorted(
                                (
                                    baseline_finding_from_comment(comment)
                                    for comment in comments
                                ),
                                key=lambda finding: finding.fingerprint,
                            )
                        ),
                        reviewed_paths=tuple(
                            sorted({comment.path for comment in comments})
                        ),
                    )

                # (a) Real baseline checkpoint through the transaction fixture,
                # including fixture._result() inside partitions.checkpoint.
                baseline_ledger = LocalSessionLedger(root, enable_partition_writes=True)
                baseline_initial = _budget_state(baseline_ledger.evidence_budget)
                with patch.object(
                    partitions, "varied_baseline", side_effect=make_baseline
                ):
                    baseline, _checkpoint_result = partitions.checkpoint(
                        baseline_ledger, count
                    )
                baseline_loaded = baseline_ledger.load(
                    fixture.IDENTITY, now=fixture.NOW
                )
                self.assertEqual(baseline_loaded.status, "ok")
                baseline_record = baseline_loaded.record
                self.assertIsNotNone(baseline_record.transaction)
                self.assertEqual(
                    baseline_record.transaction.phase, "publication_pending"
                )
                baseline_after = _budget_state(baseline_ledger.evidence_budget)
                self.assertEqual(len(baseline.findings), len(comments))
                self.assertEqual(
                    {finding.fingerprint for finding in baseline.findings},
                    {
                        baseline_finding_from_comment(comment).fingerprint
                        for comment in comments
                    },
                )
                self.assertEqual(
                    set(baseline.reviewed_paths),
                    {comment.path for comment in comments},
                )

                # Real queue bound to the same repository, PR, base and head.
                pending = PendingHumanReview.from_result(
                    human_result(comments), fixture.BASE_SHA
                )
                snapshot = EvidenceSnapshot(
                    fixture.IDENTITY.repository,
                    fixture.IDENTITY.pull_request,
                    fixture.BASE_SHA,
                    fixture.HEAD_SHA,
                )
                queue = AssessmentQueue.create(pending, snapshot)
                expected_pending_ids = {
                    finding.fingerprint for finding in pending.findings
                }
                self.assertEqual(len(pending.findings), len(comments))
                self.assertEqual(set(queue.inventory_ids), expected_pending_ids)
                self.assertEqual(
                    set(queue.pending_order),
                    expected_pending_ids - set(pending.resolved),
                )
                self.assertEqual(queue.resolved_ids, pending.resolved)
                queue_document = {
                    "schema_version": "1.0",
                    "kind": "assessment-queue-state",
                    "journal": AssessmentJournal(queue).to_document(),
                }

                # (b) Synthetic staging without root activation. Its part is
                # unreferenced; this does not model an uncertain native write.
                orphan_ledger = LocalSessionLedger(root, enable_partition_writes=True)
                orphan_initial = _budget_state(orphan_ledger.evidence_budget)
                before_orphan = orphan_ledger.load(fixture.IDENTITY, now=fixture.NOW)
                self.assertEqual(before_orphan.status, "ok")
                orphan_manifest = orphan_ledger.stage_evidence(
                    fixture.IDENTITY,
                    binding=partitions.binding(
                        producer=orphan_ledger.evidence_producer(),
                        generation=before_orphan.record.generation,
                        purpose="evidence",
                    ),
                    document={
                        "schema_version": "1.0",
                        "kind": "synthetic-abandoned-stage",
                        "case_finding_count": len(comments),
                        "text": "prior immutable staging retained without activation",
                    },
                    item_count=1,
                    max_manifest_bytes=8192,
                )
                orphan_ids = _referenced_ids(orphan_manifest)
                self.assertTrue(orphan_ids)
                after_orphan = orphan_ledger.load(fixture.IDENTITY, now=fixture.NOW)
                self.assertEqual(after_orphan.status, "ok")
                self.assertEqual(after_orphan.record, before_orphan.record)
                orphan_after = _budget_state(orphan_ledger.evidence_budget)

                # (c) Synthetic queue write on the same local root, through
                # stage_evidence inside the conditional replace seam. This is
                # not proof of hosted admission or successor authority.
                queue_ledger = LocalSessionLedger(root, enable_partition_writes=True)
                queue_initial = _budget_state(queue_ledger.evidence_budget)
                mutation_generations = []

                def mutate(
                    record,
                    queue_ledger=queue_ledger,
                    queue_document=queue_document,
                    queue=queue,
                ):
                    mutation_generations.append(record.generation)
                    context = partitions.binding(
                        producer=queue_ledger.evidence_producer(),
                        generation=record.generation,
                        purpose="queue",
                    )
                    manifest = queue_ledger.stage_evidence(
                        fixture.IDENTITY,
                        binding=context,
                        document=queue_document,
                        item_count=len(queue.inventory_ids),
                        max_manifest_bytes=assessment_queue_manifest_capacity(record),
                    )
                    return record.evolve(
                        assessment_queue={
                            "schema_version": "1.0",
                            "inventory_digest": queue.inventory_digest,
                            "inventory_generation": manifest["binding"]["generation"],
                            "state_manifest": manifest,
                            "active_operation": None,
                        },
                        generation=record.generation + 1,
                        now=fixture.NOW,
                    )

                queue_record = queue_ledger.replace(
                    fixture.IDENTITY, mutate, now=fixture.NOW
                )
                self.assertEqual(
                    mutation_generations, [before_orphan.record.generation]
                )
                self.assertEqual(
                    queue_record.generation, before_orphan.record.generation + 1
                )
                retained_after_queue = _retained_files(root)
                self.assertTrue(
                    orphan_ids.issubset(
                        {Path(name).name for name in retained_after_queue}
                    )
                )
                self.assertEqual(
                    queue_record.assessment_queue["inventory_digest"],
                    queue.inventory_digest,
                )
                # Staging a queue does not settle the earlier publication.
                self.assertEqual(queue_record.transaction, baseline_record.transaction)
                self.assertEqual(
                    queue_record.reservation_id, baseline_record.reservation_id
                )
                self.assertEqual(
                    queue_record.api_run_accounting, baseline_record.api_run_accounting
                )
                queue_after = _budget_state(queue_ledger.evidence_budget)

                # Cold reader: writers off, its own original budget.
                reader = LocalSessionLedger(root)
                reader_initial = _budget_state(reader.evidence_budget)
                loaded = reader.load(fixture.IDENTITY, now=fixture.NOW)
                self.assertEqual(loaded.status, "ok")
                loaded_record = loaded.record
                self.assertIsNotNone(loaded_record)
                restored_baseline = read_session_baseline(reader, loaded_record)
                restored_document = read_session_assessment_queue(reader, loaded_record)
                reader_after = _budget_state(reader.evidence_budget)

                # Exact baseline and queue, including instance identities,
                # pending order/obligations and the bound snapshot.
                self.assertEqual(restored_baseline, baseline)
                self.assertEqual(restored_document, queue_document)
                restored_journal = AssessmentJournal.from_document(
                    restored_document["journal"]
                )
                restored_queue = AssessmentQueue.from_document(
                    restored_journal.to_document()["queue"]
                )
                self.assertEqual(restored_queue, queue)
                self.assertEqual(restored_queue.snapshot, snapshot)
                self.assertEqual(
                    restored_queue.snapshot.repository, fixture.IDENTITY.repository
                )
                self.assertEqual(
                    restored_queue.snapshot.pull_request,
                    fixture.IDENTITY.pull_request,
                )
                self.assertEqual(restored_queue.snapshot.base_sha, fixture.BASE_SHA)
                self.assertEqual(restored_queue.snapshot.head_sha, fixture.HEAD_SHA)
                self.assertEqual(
                    set(restored_queue.inventory_ids), expected_pending_ids
                )
                self.assertEqual(restored_queue.pending_order, queue.pending_order)
                self.assertEqual(restored_queue.resolved_ids, queue.resolved_ids)
                self.assertEqual(
                    loaded_record.assessment_queue["inventory_digest"],
                    queue.inventory_digest,
                )

                # Actual retained files/bytes in this same directory.
                retained = _retained_files(root)
                retained_names = {Path(name).name for name in retained}
                retained_bytes = sum(retained.values())
                self.assertTrue(retained)
                self.assertTrue(all(size > 0 for size in retained.values()))
                self.assertGreater(retained_bytes, 0)
                self.assertLessEqual(len(retained), MAX_STORED_PARTS)
                self.assertLessEqual(retained_bytes, MAX_STORED_PART_BYTES)

                baseline_manifest = (
                    loaded_record.convergence_history.get("baseline")
                    if isinstance(loaded_record.convergence_history, dict)
                    else None
                )
                queue_manifest = (
                    loaded_record.assessment_queue.get("state_manifest")
                    if isinstance(loaded_record.assessment_queue, dict)
                    else None
                )
                baseline_ids = _referenced_ids(baseline_manifest)
                queue_ids = _referenced_ids(queue_manifest)
                self.assertTrue(baseline_ids)
                self.assertTrue(queue_ids)
                self.assertTrue(baseline_ids.issubset(retained_names))
                self.assertTrue(queue_ids.issubset(retained_names))
                self.assertTrue(orphan_ids.issubset(retained_names))
                self.assertFalse(orphan_ids.intersection(baseline_ids))
                self.assertFalse(orphan_ids.intersection(queue_ids))

                # Each fixture ledger stayed within its control budget and
                # kept its observed deadlines and calls during later work.
                # This does not authenticate a live successor allocation.
                for operation, initial, after, budget in (
                    (
                        "baseline",
                        baseline_initial,
                        baseline_after,
                        baseline_ledger.evidence_budget,
                    ),
                    (
                        "orphan",
                        orphan_initial,
                        orphan_after,
                        orphan_ledger.evidence_budget,
                    ),
                    (
                        "queue",
                        queue_initial,
                        queue_after,
                        queue_ledger.evidence_budget,
                    ),
                    (
                        "cold-reader",
                        reader_initial,
                        reader_after,
                        reader.evidence_budget,
                    ),
                ):
                    with self.subTest(operation=operation):
                        self.assertGreater(after[0], initial[0])
                        self.assertLessEqual(after[0], MAX_PART_READS)
                        self.assertEqual(after[1], initial[1])
                        self.assertEqual(after[2], initial[2])
                        self.assertEqual(
                            after[3]["deadline_unix_ms"],
                            initial[3]["deadline_unix_ms"],
                        )
                        self.assertEqual(_budget_state(budget), after)

                # A writers-off staging attempt is refused before any write
                # and loses nothing already retained.
                with self.assertRaisesRegex(ReviewInputError, "enablement"):
                    reader.stage_evidence(
                        fixture.IDENTITY,
                        binding=partitions.binding(
                            producer=reader.evidence_producer(),
                            generation=loaded_record.generation,
                            purpose="queue",
                        ),
                        document={
                            "schema_version": "1.0",
                            "kind": "must-not-be-staged",
                        },
                        item_count=1,
                        max_manifest_bytes=8192,
                    )
                self.assertEqual(_retained_files(root), retained)
                self.assertEqual(_budget_state(reader.evidence_budget), reader_after)

                final_reader = LocalSessionLedger(root)
                final_loaded = final_reader.load(fixture.IDENTITY, now=fixture.NOW)
                self.assertEqual(final_loaded.status, "ok")
                self.assertEqual(final_loaded.record, loaded_record)
                self.assertEqual(
                    _budget_state(baseline_ledger.evidence_budget), baseline_after
                )
                self.assertEqual(
                    _budget_state(orphan_ledger.evidence_budget), orphan_after
                )
                self.assertEqual(
                    _budget_state(queue_ledger.evidence_budget), queue_after
                )
                self.assertEqual(_budget_state(reader.evidence_budget), reader_after)


if __name__ == "__main__":
    unittest.main()
