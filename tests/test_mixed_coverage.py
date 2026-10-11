"""Closed mixed-coverage predicate. No GitHub calls and no finalizer writes."""

import unittest
from dataclasses import replace

from review_sensei.coverage import CoverageManifest, FileCoverage, HunkCoverage
from review_sensei.errors import ReviewInputError
from review_sensei.hosting.github.human_file_review import HumanFileReviewPolicy
from review_sensei.human_file_review import UnsupportedFile
from review_sensei.mixed_coverage import BinaryConfirmation, evaluate_mixed_coverage
from review_sensei.models import ReviewComment, ReviewResult

EVENT = "event-1"


def _file(path, change="modified", old="a" * 40, new="b" * 40, old_path=None):
    old_blob = None if change == "added" else old
    new_blob = None if change == "removed" else new
    old_mode = None if old_blob is None else "100644"
    new_mode = None if new_blob is None else "100644"
    return UnsupportedFile(
        path,
        change,
        old_blob,
        new_blob,
        old_path,
        old_mode,
        new_mode,
    )


def _result(files, *, comments=(), persistence_status=None, flag=None):
    coverage = CoverageManifest(
        files=files,
        hunks=(HunkCoverage(1, "src/a.py", "reviewed"),),
        enumerated_paths=tuple(item.path for item in files),
    )
    if flag is None:
        flag = all(
            item.outcome == "reviewed"
            or (item.outcome == "unsupported" and item.reason == "binary")
            for item in files
        ) and any(item.reason == "binary" for item in files)
    return ReviewResult(
        "Completed text review; binary files remain unsupported by AI.",
        comments,
        "fixture",
        review_status="partial",
        coverage=coverage,
        persistence_status=persistence_status,
        coverage_only_partial=flag,
    )


class MixedCoverageTests(unittest.TestCase):
    def inventory(self):
        return (
            _file("photo.png", old="c" * 40, new="d" * 40),
            _file("movie.mp4", change="added", new="e" * 40),
        )

    def result(self, **kwargs):
        files = (
            FileCoverage("src/a.py", "reviewed"),
            FileCoverage("photo.png", "unsupported", "binary"),
            FileCoverage("movie.mp4", "unsupported", "binary"),
        )
        return _result(files, **kwargs)

    def confirmations(self, inventory, **overrides):
        values = []
        for item in inventory:
            fields = dict(
                file=item,
                event_id=EVENT,
                valid=True,
                permission_current=True,
                current=True,
            )
            fields.update(overrides)
            values.append(BinaryConfirmation(**fields))
        return tuple(values)

    def evaluate(self, result, inventory, **kwargs):
        fields = dict(
            text_completed=True,
            allow_confirmations=True,
            event_id=EVENT,
        )
        fields.update(kwargs)
        return evaluate_mixed_coverage(
            result,
            inventory,
            self.confirmations(inventory, event_id=fields["event_id"]),
            **fields,
        )

    def test_two_binaries_and_completed_text_permit_partial_ai_decision(self):
        result = self.result()
        before = result.to_dict()
        decision = self.evaluate(result, self.inventory())
        self.assertTrue(decision.approved)
        self.assertTrue(decision.coverage_satisfied)
        self.assertTrue(decision.coverage_only_partial)
        self.assertEqual(decision.ai_review_status, "partial")
        self.assertEqual(result.review_status, "partial")
        self.assertEqual(result.to_dict(), before)
        self.assertFalse(decision.publishes_approve)
        self.assertFalse(decision.baseline_completed)
        self.assertFalse(decision.cache_completed)
        self.assertFalse(decision.binary_blocker_remains)
        self.assertEqual(decision.blockers, ())

    def test_text_finding_or_context_loss_withholds_approval(self):
        inventory = self.inventory()
        finding = replace(
            self.result(),
            comments=(
                ReviewComment("src/a.py", 1, "Fix the text defect.", blocking=True),
            ),
        )
        blocked = self.evaluate(finding, inventory)
        self.assertFalse(blocked.approved)
        self.assertEqual(blocked.ai_review_status, "partial")
        self.assertIn("unresolved-finding", blocked.blockers)
        self.assertEqual(finding.review_status, "partial")

        lost = self.evaluate(
            self.result(), inventory, mandatory_document_context_lost=True
        )
        self.assertFalse(lost.approved)
        self.assertFalse(lost.coverage_only_partial)
        self.assertIn("document-context-lost", lost.blockers)
        self.assertTrue(lost.binary_blocker_remains)

    def test_rename_and_delete_confirmations_can_satisfy_binary_coverage(self):
        renamed = _file(
            "new.png", change="renamed", old="a" * 40, new="b" * 40, old_path="old.png"
        )
        deleted = _file("gone.png", change="removed", old="c" * 40)
        inventory = (renamed, deleted)
        files = (
            FileCoverage("src/a.py", "reviewed"),
            FileCoverage("old.png", "unsupported", "binary"),
            FileCoverage("new.png", "unsupported", "binary"),
            FileCoverage("gone.png", "unsupported", "binary"),
        )
        result = _result(files)
        decision = self.evaluate(result, inventory)
        self.assertEqual(renamed.change, "renamed")
        self.assertEqual(deleted.change, "removed")
        self.assertIsNone(deleted.new_blob)
        self.assertTrue(decision.approved)
        self.assertEqual(decision.ai_review_status, "partial")
        self.assertFalse(decision.publishes_approve)

    def test_same_event_replay_does_not_mint_a_new_receipt_identity(self):
        result = self.result()
        inventory = self.inventory()
        first = self.evaluate(result, inventory)
        second = self.evaluate(result, inventory, prior_receipt_id=first.receipt_id)
        replayed = self.evaluate(result, inventory)
        self.assertTrue(first.minted_receipt)
        self.assertFalse(second.minted_receipt)
        self.assertEqual(second.receipt_id, first.receipt_id)
        self.assertEqual(replayed.receipt_id, first.receipt_id)
        other = self.evaluate(result, inventory, event_id="event-2")
        self.assertNotEqual(other.receipt_id, first.receipt_id)
        with self.assertRaises(ReviewInputError):
            self.evaluate(result, inventory, prior_receipt_id=other.receipt_id)

    def test_policy_off_leaves_the_binary_blocker_in_place(self):
        policy = HumanFileReviewPolicy()
        self.assertFalse(policy.allow_confirmations)
        decision = self.evaluate(
            self.result(),
            self.inventory(),
            allow_confirmations=policy.allow_confirmations,
        )
        self.assertFalse(decision.approved)
        self.assertFalse(decision.coverage_satisfied)
        self.assertTrue(decision.coverage_only_partial)
        self.assertTrue(decision.binary_blocker_remains)
        self.assertIn("binary-coverage", decision.blockers)
        self.assertIn("human-file-policy-disabled", decision.blockers)
        self.assertIsNone(decision.receipt_id)
        self.assertEqual(decision.ai_review_status, "partial")

    def test_other_coverage_and_approval_facts_refuse_or_withhold(self):
        inventory = self.inventory()
        result = self.result()
        cases = (
            ({"source_context_incomplete": True}, "source-context-incomplete", False),
            ({"unknown_file": True}, "unknown-file", False),
            ({"failed_stage": True}, "failed-stage", False),
            (
                {"provider_output_incomplete": True},
                "incomplete-provider-output",
                False,
            ),
            ({"qualification": "missing"}, "qualification-missing", True),
            ({"unresolved_threads": True}, "unresolved-thread", True),
            ({"unresolved_threads": None}, "review-threads-incomplete", True),
        )
        for kwargs, blocker, flag in cases:
            with self.subTest(blocker=blocker):
                decision = self.evaluate(result, inventory, **kwargs)
                self.assertFalse(decision.approved)
                self.assertIn(blocker, decision.blockers)
                self.assertEqual(decision.coverage_only_partial, flag)
                self.assertEqual(decision.ai_review_status, "partial")

        generated = _result(
            (
                FileCoverage("src/a.py", "reviewed"),
                FileCoverage("photo.png", "unsupported", "binary"),
                FileCoverage("vendor.lock", "unsupported", "lockfile-format"),
            ),
            flag=False,
        )
        decision = self.evaluate(generated, (_file("photo.png"),))
        self.assertFalse(decision.approved)
        self.assertFalse(decision.coverage_only_partial)
        self.assertIn("non-binary-unsupported", decision.blockers)

        exhausted = _result(
            (
                FileCoverage("src/a.py", "reviewed"),
                FileCoverage("photo.png", "unsupported", "binary"),
                FileCoverage("movie.mp4", "unsupported", "binary"),
            ),
            persistence_status="capacity-exceeded",
            flag=False,
        )
        decision = self.evaluate(exhausted, inventory)
        self.assertFalse(decision.approved)
        self.assertFalse(decision.coverage_only_partial)
        self.assertIn("persistence-capacity", decision.blockers)

        revoked = self.confirmations(inventory, permission_current=False)
        decision = evaluate_mixed_coverage(
            result,
            inventory,
            revoked,
            text_completed=True,
            allow_confirmations=True,
            event_id=EVENT,
        )
        self.assertFalse(decision.approved)
        self.assertIn("permission-revoked", decision.blockers)
        self.assertTrue(decision.binary_blocker_remains)

        stale = self.confirmations(inventory, current=False)
        decision = evaluate_mixed_coverage(
            result,
            inventory,
            stale,
            text_completed=True,
            allow_confirmations=True,
            event_id=EVENT,
        )
        self.assertFalse(decision.approved)
        self.assertIn("stale-receipt", decision.blockers)

        with self.assertRaisesRegex(ReviewInputError, "contradictory reason"):
            self.evaluate(result, inventory, reason_metadata={"photo.png": "generated"})
        with self.assertRaisesRegex(ReviewInputError, "schema"):
            self.evaluate(result, inventory, schema_version="mixed-coverage-v0")
