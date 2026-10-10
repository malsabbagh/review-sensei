"""Real kills around C receipts; synthetic host, no production grant claim."""

from __future__ import annotations

import copy
import hashlib
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from review_sensei.assessment_queue import (
    AssessmentCheckpoint,
    AssessmentJournal,
    AssessmentQueue,
)
from review_sensei.budgets import ReviewWorkBudgets
from review_sensei.errors import ReviewInputError
from review_sensei.outcomes import ResourceBudget, ResourceBudgetTracker
from review_sensei.reassessment_work import reassess
from tests.epic238_f_process_support import barrier, kill_at, load, save
from tests.test_assessment_queue import fixture
from tests.test_review_work import HUMAN, AssessingProvider

SOURCE = "f" * 64
OPERATION = "c" * 64


class PersistentProvider(AssessingProvider):
    # unittest discovery and `python -m` use different import aliases. The
    # fixture represents the same configured provider in either process.
    __module__ = "tests.test_epic238_frozen_queue_faults"

    def __init__(self, root: Path):
        super().__init__()
        self.root = root

    def complete(self, request):
        path = self.root / "provider.json"
        calls = load(path) if path.exists() else []
        calls.append({"prompt_bytes": len(request.prompt.encode("utf-8"))})
        save(path, calls)
        response = super().complete(request)
        save(self.root / "response.json", {"bytes": len(response.text.encode("utf-8"))})
        return response


class DurableJournal:
    """Atomic local fixture for public journal semantics, not a global lock."""

    def __init__(
        self,
        root: Path,
        queue,
        *,
        fault="",
        source=SOURCE,
        operation=OPERATION,
        now=None,
    ):
        self.root, self.fault = root, fault
        self.source, self.operation = source, operation
        self.now = now
        if not (root / "root.json").exists():
            save(
                root / "root.json",
                {"generation": 0, "journal": AssessmentJournal(queue).to_document()},
            )
        self.generation = load(root / "root.json")["generation"]

    def journal(self):
        return AssessmentJournal.from_document(load(self.root / "root.json")["journal"])

    def queue(self):
        return AssessmentQueue.from_document(self.journal().to_document()["queue"])

    def checkpoint(self):
        def read():
            state = load(self.root / "root.json")
            self.generation = state["generation"]
            journal = AssessmentJournal.from_document(state["journal"])
            return journal.receipt(
                source_digest=self.source, operation_id=self.operation
            )

        def mutate(document, context):
            barrier(self.root, self.fault, "before-" + context.reason)
            state = load(self.root / "root.json")
            if context.root_generation != state["generation"]:
                raise ReviewInputError("synthetic stale root")
            previous = AssessmentJournal.from_document(state["journal"])
            journal = previous.record(
                queue=AssessmentQueue.from_document(previous.to_document()["queue"]),
                source_digest=self.source,
                receipt=document,
            )
            self.generation = state["generation"] + 1
            save(
                self.root / "root.json",
                {"generation": self.generation, "journal": journal.to_document()},
            )
            barrier(self.root, self.fault, "after-" + context.reason)

        def forbidden_write(_):
            raise AssertionError("on_mutation must carry generation metadata")

        return AssessmentCheckpoint(
            operation_id=self.operation,
            read=read,
            write=forbidden_write,
            on_mutation=mutate,
            root_generation=lambda: self.generation,
            **({"now": self.now} if self.now is not None else {}),
        )


def run(
    root: Path,
    *,
    fault="",
    source_body=HUMAN,
    authority=SOURCE,
    targets=None,
    count=8,
    source=SOURCE,
    operation=OPERATION,
    max_calls=1,
    now=None,
):
    pending, bundle, queue = fixture(count)
    store = DurableJournal(
        root, queue, fault=fault, source=source, operation=operation, now=now
    )
    latest = store.queue()
    pending = replace(pending, resolved=latest.resolved_ids)
    if targets is None:
        targets = (queue.pending_order[-1],)
    provider = PersistentProvider(root)
    work = reassess(
        provider=provider,
        pending=pending,
        bundle=bundle,
        queue=latest,
        targets=targets,
        source_body=source_body,
        authority_digest=authority,
        work_budgets=ReviewWorkBudgets(mode="unified"),
        tracker=ResourceBudgetTracker(
            ResourceBudget.create(max_provider_calls=max_calls)
        ),
        checkpoint=store.checkpoint(),
    )
    return work, store


def receipt(root: Path):
    return AssessmentJournal.from_document(load(root / "root.json")["journal"]).receipt(
        source_digest=SOURCE, operation_id=OPERATION
    )


class FrozenQueueFaultTests(unittest.TestCase):
    def test_real_parent_kill_original_reservation_and_target_replay(self):
        for boundary in (
            "after-admission",
            "before-dispatch",
            "after-dispatch",
            "before-accounting",
            "after-accounting",
            "before-accepted",
            "after-accepted",
        ):
            with (
                self.subTest(boundary=boundary),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                kill_at("tests.test_epic238_frozen_queue_faults", root, boundary)
                before = copy.deepcopy(receipt(root))
                calls_before = (
                    len(load(root / "provider.json"))
                    if (root / "provider.json").exists()
                    else 0
                )
                if boundary in ("after-dispatch", "before-accounting"):
                    self.assertEqual(before["response_bytes_reserved"], 16 * 1024)
                    self.assertEqual(before["counters"]["response_bytes"], 0)
                if boundary in (
                    "after-accounting",
                    "before-accepted",
                    "after-accepted",
                ):
                    self.assertEqual(before["response_bytes_reserved"], 0)
                    self.assertEqual(
                        before["counters"]["response_bytes"],
                        load(root / "response.json")["bytes"],
                    )
                work, store = run(root)
                after = receipt(root)
                for field in (
                    "operation_id",
                    "execution_identity",
                    "resource_budget",
                    "request_digests",
                    "created_at",
                    "deadline_at",
                    "expires_at",
                    "admitted_queue",
                ):
                    self.assertEqual(after[field], before[field], field)
                total_calls = (
                    len(load(root / "provider.json"))
                    if (root / "provider.json").exists()
                    else 0
                )
                if boundary in ("after-admission", "before-dispatch"):
                    self.assertEqual(total_calls, 1)
                    self.assertEqual(len(work.reply.decisions), 1)
                elif boundary == "after-accepted":
                    self.assertEqual(calls_before, 1)
                    self.assertEqual(total_calls, 1)
                    self.assertEqual(len(work.reply.decisions), 1)
                    self.assertEqual(len(store.queue().resolved_ids), 1)
                else:
                    self.assertEqual(total_calls, calls_before)
                    self.assertFalse(work.reply.decisions)
                    self.assertFalse(store.queue().resolved_ids)
                self.assertLessEqual(after["counters"]["provider_calls"], 1)
                self.assertGreaterEqual(
                    after["counters"]["prompt_bytes"],
                    before["counters"]["prompt_bytes"],
                )
                # A fresh invocation cannot reset the same original allowance.
                run(root)
                self.assertEqual(
                    len(load(root / "provider.json"))
                    if (root / "provider.json").exists()
                    else 0,
                    total_calls,
                )

    def test_same_trigger_changed_body_target_authority_or_operation_refuses(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run(root)
            _, _, queue = fixture(8)
            for changes in (
                {"source_body": HUMAN + " Changed instruction."},
                {"targets": (queue.pending_order[0],)},
                {"authority": "e" * 64},
                {"operation": "d" * 64},
            ):
                with self.subTest(changes=changes), self.assertRaises(ReviewInputError):
                    run(root, **changes)
            self.assertEqual(len(load(root / "provider.json")), 1)
            self.assertEqual(len(load(root / "root.json")["journal"]["operations"]), 1)

    def test_original_receipt_cannot_be_refunded_or_dropped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, store = run(root)
            original = receipt(root)
            for field, value in (
                ("counters", {**original["counters"], "provider_calls": 0}),
                ("completed", []),
                ("attempted_ids", []),
            ):
                corrupted = copy.deepcopy(original)
                corrupted[field] = value
                with self.subTest(field=field), self.assertRaises(ReviewInputError):
                    store.journal().record(
                        queue=store.queue(), source_digest=SOURCE, receipt=corrupted
                    )
            self.assertEqual(receipt(root), original)

    def test_expired_source_tombstone_remains_and_cannot_mint_new_allowance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run(root)
            original = receipt(root)
            future = datetime.fromisoformat(original["expires_at"]) + timedelta(
                seconds=1
            )
            with self.assertRaises(ReviewInputError):
                run(root, now=lambda: future)
            with self.assertRaises(ReviewInputError):
                run(root, operation="e" * 64, now=lambda: future)
            self.assertEqual(receipt(root), original)
            self.assertEqual(len(load(root / "provider.json")), 1)
            self.assertEqual(len(load(root / "root.json")["journal"]["operations"]), 1)

    def test_complete_100_250_primitive_queue_retains_every_original_source_receipt(
        self,
    ):
        # Each next step represents a distinct explicit authenticated event in
        # this fixture. Replaying any event gets no new allowance. This is not
        # a hosted default product flow or a new queue for the same baseline.
        for count in (100, 250):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                remaining = count
                index = 0
                originals = []
                while remaining:
                    source = hashlib.sha256(
                        f"trusted-event:{index}".encode()
                    ).hexdigest()
                    operation = hashlib.sha256(
                        f"operation:{index}".encode()
                    ).hexdigest()
                    _, _, initial = fixture(count)
                    store = DurableJournal(
                        root, initial, source=source, operation=operation
                    )
                    selected = store.queue().pending_order[:32]
                    work, store = run(
                        root,
                        count=count,
                        targets=selected,
                        source=source,
                        operation=operation,
                        max_calls=8,
                    )
                    remaining = max(0, remaining - 32)
                    self.assertEqual(len(store.queue().pending_order), remaining)
                    self.assertEqual(len(work.reply.decisions), len(selected))
                    original = store.journal().receipt(
                        source_digest=source, operation_id=operation
                    )
                    originals.append((source, operation, original))
                    calls = len(load(root / "provider.json"))
                    run(
                        root,
                        count=count,
                        targets=selected,
                        source=source,
                        operation=operation,
                        max_calls=8,
                    )
                    self.assertEqual(len(load(root / "provider.json")), calls)
                    for old_source, old_operation, old_receipt in originals:
                        # saved_at may update on replay; admission/charges and
                        # accepted payloads remain exactly those first stored.
                        current = store.journal().receipt(
                            source_digest=old_source, operation_id=old_operation
                        )
                        for field in (
                            "resource_budget",
                            "created_at",
                            "deadline_at",
                            "expires_at",
                            "counters",
                            "completed",
                            "admitted_queue",
                        ):
                            self.assertEqual(current[field], old_receipt[field])
                    index += 1
                self.assertEqual(len(store.queue().resolved_ids), count)
                self.assertEqual(len(store.queue().inventory_ids), count)
                self.assertEqual(
                    len(store.journal().to_document()["operations"]), index
                )


if __name__ == "__main__":
    from tests.test_epic238_frozen_queue_faults import run as canonical_run

    canonical_run(Path(sys.argv[1]), fault=sys.argv[2])
