"""Original guarded512 intermediate command, including physical response loss."""

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from review_sensei_github import quarantine_historical_execution as closed
from review_sensei_github.api_run_history import read_action_result
from review_sensei_github.quarantine_capacity import installed_capacity
from review_sensei_github.trust_effects import verification_profile
from test_quarantine_journey import registered_resource
from test_quarantine_published_tail_journey import (
    BROKER_QUALIFICATIONS,
    PublishedTailJourneyFixture,
)

from review_sensei.errors import ReviewInputError


class GuardedHistoricalFixture(PublishedTailJourneyFixture):
    def __init__(self, directory, *, faults=()):
        super().__init__(directory)
        self.faults = set(faults)
        self.closed_started = False
        self.lost_roots = set()
        self.root_writes = {}
        self.lost_receipt = False
        self.lost_review = False
        self.native_puts = 0
        self.native_readbacks = 0
        self.native_readback_frames = []

    def external(self, request, timeout):
        method, path = request.get_method(), urlsplit(request.full_url).path
        body = json.loads(request.data) if request.data else {}
        receipt = (
            self.closed_started
            and method == "POST"
            and "reviewsensei:command-receipt:v1" in str(body.get("body", ""))
        )
        review = self.closed_started and method == "PUT" and "/reviews/" in path
        if review:
            self.native_puts += 1
        if (
            self.lost_review
            and method == "GET"
            and "/reviews/" in path
            and not path.endswith("/comments")
        ):
            self.native_readbacks += 1
            frame = sys._getframe()
            names = []
            while frame is not None:
                names.append(frame.f_code.co_name)
                frame = frame.f_back
            self.native_readback_frames.append(names)
        snapshot = getattr(
            getattr(self, "accounted", None), "_quarantine_checkpoint_snapshot", None
        )
        root = (
            snapshot.root
            if self.closed_started
            and snapshot is not None
            and path == f"/repos/owner/repo/issues/comments/{snapshot.comment_id}"
            else None
        )
        if root is not None and method == "PATCH":
            self.root_writes[root] = self.root_writes.get(root, 0) + 1
        if review and "review-absent" in self.faults and not self.lost_review:
            self._meter(request)
            self.lost_review = True
            raise TimeoutError("synthetic uncommitted guarded PUT response loss")
        if receipt and "receipt-absent" in self.faults and not self.lost_receipt:
            self._meter(request)
            self.lost_receipt = True
            raise TimeoutError("synthetic uncommitted guarded receipt response loss")
        response = super().external(request, timeout)
        if (
            root is not None
            and method == "GET"
            and snapshot.operation.write_attempted
            and "roots" in self.faults
            and root not in self.lost_roots
        ):
            self.lost_roots.add(root)
            raise TimeoutError("synthetic guarded checkpoint final GET response loss")
        if review and "review-stored" in self.faults and not self.lost_review:
            self.lost_review = True
            raise TimeoutError("synthetic stored guarded PUT response loss")
        if receipt and "receipt-stored" in self.faults and not self.lost_receipt:
            self.lost_receipt = True
            raise TimeoutError("synthetic stored guarded receipt response loss")
        return response


class HistoricalExecutionTests(unittest.TestCase):
    def exercise(self, *, faults=(), hold=None):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            fixture = GuardedHistoricalFixture(directory, faults=faults)
            old, registration = fixture.original_origins(
                failed_count=1, published_count=1
            )
            old_document = old.to_dict()
            with registered_resource([registration]):
                self.assertEqual(fixture.invoke(), 0, fixture.error)
                origin, inventory = next(
                    (key, value)
                    for key, value in fixture.selected.ledger._quarantine_authenticated.inventories.items()
                    if value["assessments"]
                )
                fixture.quarantine_predecessor_comment_id = None
                fixture.select_command(
                    "@reviewsensei dismiss "
                    + inventory["assessments"][0]["instance"]
                    + ' --reason "quarantine:'
                    + origin
                    + ' Exact source disproves this historical concern."',
                    new_run=True,
                )
                issued = []
                providers = fixture.provider_calls
                reviews = len(fixture.reviews)

                def observe(frame, event, result):
                    if (
                        event != "return"
                        or frame.f_code is not closed.activate.__code__
                        or result is None
                    ):
                        return
                    run = frame.f_locals["run"]
                    fixture.closed_started = True
                    self.assertIsNone(verification_profile(run.caller))
                    state = run.caller.accounting
                    blocks = {b.block_id: b for b in state.blocks}
                    self.assertEqual(
                        (
                            state.configuration.limit,
                            state.startup.liability,
                            len(state.completed),
                            blocks[run.caller._block].undispatched,
                            blocks[run.caller._terminal_block].undispatched,
                            state.remaining,
                        ),
                        (512, 132, 35, 105, 50, 190),
                    )
                    guard = installed_capacity(run.ledger, run.identity)
                    self.assertFalse(guard.complete_tail)
                    self.assertEqual(len(guard.comment_plan.unknown), 16)
                    with self.assertRaisesRegex(ReviewInputError, "copied or foreign"):
                        closed.require_closed(copy.copy(result), run)
                    with self.assertRaisesRegex(ReviewInputError, "ledger changed"):
                        closed.available(run.caller, copy.copy(run.ledger))
                    original_root = run.caller._root
                    try:
                        run.caller._root = "0" * 64
                        with self.assertRaises(ReviewInputError):
                            closed.require_closed(result, run)
                    finally:
                        run.caller._root = original_root
                    closed.require_closed(result, run)
                    issued.append(
                        (result, run, run.caller._block, run.caller._terminal_block)
                    )

                previous = sys.getprofile()
                try:
                    sys.setprofile(observe)
                    result = fixture.invoke_command()
                finally:
                    sys.setprofile(previous)
                self.assertEqual(result, 1 if hold else 0, fixture.error)
                self.assertEqual(len(issued), 1)
                _, run, ordinary, terminal = issued[0]
                state = run.caller.accounting
                blocks = {b.block_id: b for b in state.blocks}
                self.assertGreaterEqual(blocks[ordinary].undispatched, 0)
                self.assertGreaterEqual(blocks[terminal].undispatched, 0)
                self.assertEqual(state.configuration.limit, 512)
                self.assertEqual(state.remaining, 190)
                self.assertEqual(fixture.provider_calls, providers)
                self.assertEqual(len(fixture.reviews), reviews)
                self.assertFalse(
                    any(row.get("state") == "APPROVED" for row in fixture.reviews)
                )
                self.assertEqual(old.to_dict(), old_document)
                self.assertEqual(fixture.native_puts, 1)
                self.assertEqual(
                    state.startup.liability
                    + len(state.completed)
                    + sum(b.undispatched + len(b.pending) for b in state.blocks)
                    + state.prior_nonstartup_liability
                    + state.remaining,
                    512,
                )
                self.assertEqual(len(fixture.root_writes), 4 if hold else 5)
                self.assertTrue(
                    all(count == 1 for count in fixture.root_writes.values())
                )
                current = fixture.current_record()
                if hold:
                    from review_sensei_github.finding_lifecycle import _unpack_journal

                    from review_sensei.bounded_evidence import read_inline_evidence

                    self.assertEqual(
                        current.api_run_accounting["action"]["phase"], "pending"
                    )
                    self.assertTrue(
                        current.api_run_accounting["action"]["execution_started"]
                    )
                    self.assertIsNone(current.api_run_accounting["action"]["result"])
                    manifest = current.finding_lifecycle["manifest"]
                    self.assertEqual(manifest["encoding"], "inline-json-v1")
                    packed = read_inline_evidence(
                        manifest, expected_binding=manifest["binding"]
                    )["lifecycle"]
                    document = _unpack_journal(packed)
                    updates = [e for e in document["effects"] if e["method"] == "PUT"]
                    self.assertEqual(len(updates), 1)
                    self.assertEqual(
                        updates[0]["status"],
                        "pending" if hold == "review-absent" else "confirmed",
                    )
                    self.assertEqual(blocks[terminal].undispatched, 50)
                    self.assertEqual(state.uncertain, 5)
                    self.assertEqual(len(fixture.lost_roots), 4)
                    self.assertFalse(
                        any(
                            "reviewsensei:command-receipt:v1" in row.get("body", "")
                            for row in fixture.extra_comments.values()
                        )
                    )
                    if hold.startswith("review-"):
                        self.assertTrue(fixture.lost_review)
                        self.assertFalse(fixture.lost_receipt)
                        # No ordinary-publication witness exists on this historical
                        # route, so native_attempt yields None. Its one independent
                        # readback is journal.finish, not ordinary recover().
                        self.assertEqual(fixture.native_readbacks, 1)
                        self.assertIn("finish", fixture.native_readback_frames[0])
                        self.assertIn("_readback", fixture.native_readback_frames[0])
                        self.assertNotIn("recover", fixture.native_readback_frames[0])
                        self.assertIn(
                            "command effect or required finalization remains unconfirmed",
                            fixture.error,
                        )
                    else:
                        self.assertTrue(fixture.lost_receipt)
                        self.assertIn(
                            "current command receipt is unconfirmed", fixture.error
                        )
                    return
                self.assertEqual(
                    current.api_run_accounting["action"]["phase"], "terminal"
                )
                retained = read_action_result(run.ledger, run.identity, current)
                self.assertEqual(
                    retained["outcome"], {"kind": "applied", "reason": None}
                )
                self.assertTrue(retained["result"]["applied"])
                self.assertFalse(retained["result"]["evaluation_failed"])
                self.assertEqual(
                    [r["outcome"] for r in retained["result"]["receipts"]], ["recorded"]
                )
                marker = f"<!-- reviewsensei:command-receipt:v1 source={fixture.source['id']} digest={hashlib.sha256(fixture.command_body.encode()).hexdigest()} -->"
                receipts = [
                    row
                    for row in fixture.extra_comments.values()
                    if row.get("body", "").rstrip().endswith(marker)
                ]
                self.assertEqual(len(receipts), 1)
                self.assertEqual(
                    receipts[0]["user"]["id"], run.ledger._evidence_author_id
                )
                self.assertEqual(state.uncertain, 6 if faults else 0)
                if faults:
                    self.assertEqual(len(fixture.lost_roots), 5)
                    self.assertTrue(fixture.lost_receipt)
                self.assertFalse(
                    installed_capacity(run.ledger, run.identity).complete_tail
                )

    def test_original512_intermediate_command_has_durable_result_and_receipt(self):
        self.exercise()

    def test_all_root_readback_losses_and_stored_receipt_loss_preserve_six_unknowns(
        self,
    ):
        self.exercise(faults=("roots", "receipt-stored"))

    def test_stored_native_response_loss_preserves_exact_nonterminal_hold(self):
        self.exercise(faults=("roots", "review-stored"), hold="review-stored")

    def test_absent_native_response_loss_preserves_pending_effect_without_replay(self):
        self.exercise(faults=("roots", "review-absent"), hold="review-absent")

    def test_absent_receipt_response_preserves_completed_native_result_without_closure(
        self,
    ):
        self.exercise(faults=("roots", "receipt-absent"), hold="receipt-absent")
