"""Local immutable evidence reuse retains fresh root, ownership and file fences."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.errors import ReviewInputError
from review_sensei.session import (
    LocalSessionLedger,
    complete_review_publication,
    read_session_baseline,
)
from tests import test_review_transaction as fixture
from tests.test_authenticated_partitions import checkpoint


class LocalPartitionReadCacheTests(unittest.TestCase):
    def test_repeated_public_authority_reads_leave_original_budget_for_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline, result = checkpoint(
                LocalSessionLedger(root, enable_partition_writes=True), 250
            )
            reader = LocalSessionLedger(root)
            actual_open = Path.open
            part_opens = []
            root_opens = []

            def observed_open(path, *args, **kwargs):
                (part_opens if ".evidence" in path.parts else root_opens).append(path)
                return actual_open(path, *args, **kwargs)

            def journey():
                for _ in range(6):
                    loaded = reader.load(fixture.IDENTITY, now=fixture.NOW)
                    if loaded.status != "ok":
                        raise ReviewInputError(
                            "original finite read budget withheld authority"
                        )
                    self.assertEqual(
                        read_session_baseline(reader, loaded.record), baseline
                    )
                return complete_review_publication(
                    reader,
                    fixture.IDENTITY,
                    result.transaction,
                    published=True,
                    now=fixture.NOW,
                )

            with patch.object(Path, "open", observed_open):
                if sys.platform == "win32":
                    # No unsafe metadata-only cache proof on Windows. The
                    # unqualified large repeated-read profile remains bounded
                    # and withheld instead of refilling the original allowance.
                    with self.assertRaises(ReviewInputError):
                        journey()
                    self.assertEqual(reader.evidence_budget.calls, 60)
                    return
                completed = journey()
                latest = reader.load(fixture.IDENTITY, now=fixture.NOW)
            self.assertEqual(completed.transaction.phase, "publication_succeeded")
            self.assertEqual(latest.record.transaction.phase, "publication_succeeded")
            self.assertEqual(latest.record.generation, completed.generation)
            manifest = latest.record.convergence_history["baseline"]
            self.assertEqual(len(part_opens), len(manifest["parts"]))
            self.assertGreaterEqual(len(root_opens), 8)
            self.assertLessEqual(reader.evidence_budget.calls, 64)

    def test_cached_part_tamper_deletion_replacement_and_symlinks_fail_closed(self):
        for fault in (
            "same-size-tamper",
            "delete",
            "replaced",
            "file-symlink",
            "directory-symlink",
        ):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                baseline, _ = checkpoint(
                    LocalSessionLedger(root, enable_partition_writes=True), 100
                )
                reader = LocalSessionLedger(root)
                record = reader.load(fixture.IDENTITY, now=fixture.NOW).record
                self.assertEqual(read_session_baseline(reader, record), baseline)
                part = next(root.glob(".evidence/*/*/*"))
                raw = part.read_bytes()
                before = part.stat()
                if fault == "same-size-tamper":
                    part.write_bytes(raw[:-1] + b"!")
                    # Restoring mtime cannot hide the fresh ctime observation.
                    os.utime(part, ns=(before.st_atime_ns, before.st_mtime_ns))
                elif fault == "delete":
                    part.unlink()
                elif fault == "replaced":
                    other = part.with_name(".replacement")
                    other.write_bytes(raw[:-1] + b"!")
                    os.replace(other, part)
                elif fault == "file-symlink":
                    target = root / "foreign-part"
                    target.write_bytes(raw)
                    part.unlink()
                    part.symlink_to(target)
                else:
                    target = root / "foreign-directory"
                    part.parent.rename(target)
                    part.parent.symlink_to(target, target_is_directory=True)
                self.assertEqual(
                    reader.load(fixture.IDENTITY, now=fixture.NOW).status,
                    "integrity-failed",
                )

    def test_fresh_ledger_never_inherits_another_instances_part_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint(LocalSessionLedger(root, enable_partition_writes=True), 100)
            first = LocalSessionLedger(root)
            self.assertEqual(first.load(fixture.IDENTITY, now=fixture.NOW).status, "ok")
            part = next(root.glob(".evidence/*/*/*"))
            part.write_bytes(b"{}")
            restarted = LocalSessionLedger(root)
            self.assertEqual(
                restarted.load(fixture.IDENTITY, now=fixture.NOW).status,
                "integrity-failed",
            )

    def test_expired_original_budget_does_not_dispatch_cached_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint(LocalSessionLedger(root, enable_partition_writes=True), 100)
            now = [0.0]
            budget = EvidenceReadBudget(clock=lambda: now[0])
            reader = LocalSessionLedger(root, evidence_budget=budget)
            record = reader.load(fixture.IDENTITY, now=fixture.NOW).record
            calls = budget.calls
            now[0] = 61.0
            with patch.object(
                Path, "stat", side_effect=AssertionError("expired metadata dispatch")
            ):
                with self.assertRaises(ReviewInputError):
                    read_session_baseline(reader, record)
            self.assertEqual(budget.calls, calls)

    def test_windows_fallback_rehashes_cached_bytes_and_retains_finite_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint(LocalSessionLedger(root, enable_partition_writes=True), 100)
            reader = LocalSessionLedger(root)
            original_open = Path.open
            parts = []

            def observed_open(path, *args, **kwargs):
                if ".evidence" in path.parts:
                    parts.append(path)
                return original_open(path, *args, **kwargs)

            with (
                patch("review_sensei.session.sys.platform", "win32"),
                patch.object(Path, "open", observed_open),
            ):
                self.assertEqual(
                    reader.load(fixture.IDENTITY, now=fixture.NOW).status, "ok"
                )
                first = len(parts)
                calls = reader.evidence_budget.calls
                self.assertEqual(
                    reader.load(fixture.IDENTITY, now=fixture.NOW).status, "ok"
                )
            self.assertGreater(first, 0)
            self.assertEqual(len(parts), 2 * first)
            self.assertEqual(reader.evidence_budget.calls - calls, 1 + 3 * first)

    def test_other_ledger_transaction_update_is_observed_with_cached_parts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, result = checkpoint(
                LocalSessionLedger(root, enable_partition_writes=True), 100
            )
            reader = LocalSessionLedger(root)
            original = reader.load(fixture.IDENTITY, now=fixture.NOW).record
            changed = complete_review_publication(
                LocalSessionLedger(root),
                fixture.IDENTITY,
                result.transaction,
                published=False,
                now=fixture.NOW,
            )
            current = reader.load(fixture.IDENTITY, now=fixture.NOW).record
            self.assertGreater(current.generation, original.generation)
            self.assertEqual(current.generation, changed.generation)
            self.assertEqual(current.transaction.phase, "publication_failed")
            self.assertEqual(current.failed_attempts, original.failed_attempts)
