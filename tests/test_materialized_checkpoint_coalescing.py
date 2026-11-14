"""Fresh-census root adjacency and unchanged original readback liabilities."""

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from urllib.parse import urlsplit

from fake_github_http import json_response
from review_sensei_github import materialized_publication_phase as phase
from review_sensei_github.session_ledger import (
    parse_session_comment,
    render_session_comment,
)

from review_sensei.errors import ReviewInputError
from tests.test_quarantine_activation import registered_resource
from tests.test_quarantine_journey import JourneyFixture


class CoalescingFixture(JourneyFixture):
    def __init__(self, directory, fault=None):
        super().__init__(directory)
        self.fault = fault
        self.lost = set()
        self.writes = {}
        self.copies_rejected = set()
        self.reads_before_write = {}

    def external(self, request, timeout):
        snapshot = getattr(
            getattr(self, "accounted", None), "_quarantine_checkpoint_snapshot", None
        )
        observed = (
            None if snapshot is None else phase.phase_observation(snapshot.ledger)
        )
        if snapshot is None or observed is None or not observed["closed"]:
            return super().external(request, timeout)
        target = f"/repos/owner/repo/issues/comments/{snapshot.comment_id}"
        if urlsplit(request.full_url).path != target:
            return super().external(request, timeout)
        key = snapshot.root
        if key not in self.copies_rejected:
            try:
                replace(snapshot).require()
            except ReviewInputError:
                self.copies_rejected.add(key)
            else:
                raise AssertionError("copied snapshot acquired original authority")
        method = request.get_method()
        if method == "PATCH":
            self.writes[key] = self.writes.get(key, 0) + 1
        if method == "GET" and not snapshot.operation.write_attempted:
            self.reads_before_write[key] = self.reads_before_write.get(key, 0) + 1
        response = super().external(request, timeout)
        if (
            method == "GET"
            and snapshot.operation.write_attempted
            and self.fault == "one-loss-per-root"
            and key not in self.lost
        ):
            self.lost.add(key)
            raise TimeoutError("synthetic original final readback response loss")
        if (
            method == "GET"
            and not snapshot.operation.write_attempted
            and self.fault in {"changed", "owner", "foreign"}
        ):
            payload = json.loads(response.read(524288))
            if self.fault == "owner":
                payload["user"]["id"] = 998
            elif self.fault == "foreign":
                payload["id"] += 1
            else:
                record = parse_session_comment(
                    payload["body"], identity=snapshot.identity
                )
                payload["body"] = render_session_comment(
                    repository_id=123,
                    pull_request=7,
                    record=record.evolve(generation=record.generation + 1),
                )
            return json_response(payload)
        return response


class MaterializedCheckpointCoalescingTests(unittest.TestCase):
    def test_every_lost_final_read_keeps_one_patch_and_original_confirmation_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = CoalescingFixture(directory, "one-loss-per-root")
            _, registration = fixture.original_origins(
                failed_count=1, published_count=1
            )
            with registered_resource([registration]):
                self.assertEqual(fixture.invoke(), 0, fixture.error)
            self.assertGreaterEqual(len(fixture.lost), 4)
            self.assertEqual(set(fixture.writes), fixture.lost)
            self.assertTrue(all(count == 1 for count in fixture.writes.values()))
            self.assertEqual(set(fixture.writes), fixture.copies_rejected)
            self.assertTrue(
                any(count == 1 for count in fixture.reads_before_write.values())
            )
            self.assertEqual(fixture.accounted.accounting.configuration.limit, 512)

    def test_intervening_read_invalidates_census_receipt_before_first_write(self):
        injected = []

        def trace(frame, event, _arg):
            own = frame.f_code.co_name == "walked" and frame.f_globals is vars(phase)
            if own and event == "return" and not injected:
                entry = frame.f_locals.get("entry")
                if entry is not None and entry["census_receipt"] is not None:
                    ledger = entry["ledger"]
                    ledger.http.request(
                        "GET", "/repos/owner/repo/pulls/7", token=ledger.token
                    )
                    injected.append(True)
            return trace if own else None

        with tempfile.TemporaryDirectory() as directory:
            fixture = CoalescingFixture(directory)
            _, registration = fixture.original_origins(
                failed_count=1, published_count=1
            )
            previous = sys.gettrace()
            try:
                sys.settrace(trace)
                with registered_resource([registration]):
                    self.assertEqual(fixture.invoke(), 1)
            finally:
                sys.settrace(previous)
            self.assertEqual(injected, [True])
            self.assertIn("census root receipt is stale or foreign", fixture.error)
            self.assertEqual(fixture.writes, {})

    def test_read_loss_receipts_reject_changed_owner_block_and_deadline(self):
        for field in ("owner", "block", "deadline"):
            changed = []

            def trace(frame, event, _arg):
                own = (
                    frame.f_code.co_name == "only_captured_read_losses"
                    and frame.f_globals is vars(phase)
                )
                if own and event == "call" and not changed:
                    entry = frame.f_locals["entry"]
                    if entry["read_losses"]:
                        loss = entry["read_losses"][0]
                        if field == "deadline":
                            loss["observation"]["deadline_unix_ms"] += 1
                        else:
                            loss[field] = "foreign"
                        changed.append(True)
                return trace if own else None

            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                fixture = CoalescingFixture(directory, "one-loss-per-root")
                _, registration = fixture.original_origins(
                    failed_count=1, published_count=1
                )
                previous = sys.gettrace()
                try:
                    sys.settrace(trace)
                    with registered_resource([registration]):
                        self.assertEqual(fixture.invoke(), 1)
                finally:
                    sys.settrace(previous)
                self.assertEqual(changed, [True])
                self.assertIn(
                    "materialized readonly loss receipt changed", fixture.error
                )
                self.assertEqual(len(fixture.writes), 1)
                self.assertTrue(all(count == 1 for count in fixture.writes.values()))

    def test_reconciled_loss_root_cannot_change_before_next_checkpoint(self):
        changed = []

        def trace(frame, event, _arg):
            own = (
                frame.f_code.co_name == "only_captured_read_losses"
                and frame.f_globals is vars(phase)
            )
            if own and event == "call" and not changed:
                entry = frame.f_locals["entry"]
                if entry["read_losses"] and entry["read_losses"][0]["reconciled"]:
                    entry["read_losses"][0]["root"] = "0" * 64
                    changed.append(True)
            return trace if own else None

        with tempfile.TemporaryDirectory() as directory:
            fixture = CoalescingFixture(directory, "one-loss-per-root")
            _, registration = fixture.original_origins(
                failed_count=1, published_count=1
            )
            previous = sys.gettrace()
            try:
                sys.settrace(trace)
                with registered_resource([registration]):
                    self.assertEqual(fixture.invoke(), 1)
            finally:
                sys.settrace(previous)
            self.assertEqual(changed, [True])
            self.assertIn("materialized readonly loss receipt changed", fixture.error)
            self.assertEqual(len(fixture.writes), 1)

    def test_captured_get_code_is_checked_before_readback_invocation(self):
        from review_sensei_github.quarantine_checkpoint import _CheckpointSnapshot

        original = _CheckpointSnapshot.get.__wrapped__
        code = original.__code__
        changed = []

        def replacement(snapshot, *, candidate=False):
            snapshot.ledger._forbidden_get_replacement = True
            return snapshot.candidate

        def trace(frame, event, _arg):
            own = frame.f_code.co_name == "get" and frame.f_globals is vars(phase)
            if (
                own
                and event == "call"
                and frame.f_locals.get("candidate")
                and not changed
            ):
                snapshot = frame.f_locals["snapshot"]
                observed = phase.phase_observation(snapshot.ledger)
                if observed is None or not observed["closed"]:
                    return trace
                original.__code__ = replacement.__code__
                changed.append(snapshot.ledger)
            return trace if own else None

        with tempfile.TemporaryDirectory() as directory:
            fixture = CoalescingFixture(directory)
            _, registration = fixture.original_origins(
                failed_count=1, published_count=1
            )
            previous = sys.gettrace()
            try:
                sys.settrace(trace)
                with registered_resource([registration]):
                    self.assertEqual(fixture.invoke(), 1)
            finally:
                sys.settrace(previous)
                original.__code__ = code
            self.assertEqual(len(changed), 1)
            self.assertFalse(hasattr(changed[0], "_forbidden_get_replacement"))
            self.assertIn(
                "materialized publication original writer changed", fixture.error
            )

    def test_post_mutate_fence_keeps_changed_root_and_owner_holds(self):
        for fault in ("changed", "owner", "foreign"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                fixture = CoalescingFixture(directory, fault)
                _, registration = fixture.original_origins(
                    failed_count=1, published_count=1
                )
                with registered_resource([registration]):
                    self.assertEqual(fixture.invoke(), 1)
                self.assertIn("quarantine exact checkpoint", fixture.error)
                self.assertEqual(fixture.writes, {})
