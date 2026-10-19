"""Wall-time units for original hosted admission; no public dispatch claim."""

from __future__ import annotations

import time
import unittest
from dataclasses import replace

from review_sensei_github.operation_host import (
    CONTROL_DEADLINE_MS,
    GitHubCommentOperationStore,
    InMemoryOperationStore,
    OperationHandle,
    OperationHost,
    OperationRefusal,
    RestoredOperation,
    TransitionPermit,
)

from tests.test_operation_entry import MemoryComments
from tests.test_operation_host import _plan, _proof, _request


class OperationWallClockTests(unittest.TestCase):
    def test_default_wall_clock_admits_real_unix_milliseconds(self):
        started = int(time.time() * 1000)
        host = OperationHost()
        handle = host.begin_operation(_proof(), _request(), started, 7)
        self.assertIsInstance(handle, OperationHandle)
        self.assertEqual(handle.created_at_ms, started)
        self.assertEqual(handle.control_deadline_ms, started + CONTROL_DEADLINE_MS)
        self.assertEqual(handle.calls, 7)

    def test_remote_owned_record_restores_original_unix_origin_and_charges(self):
        now = [1_792_368_000_000]
        comments = MemoryComments()
        store = GitHubCommentOperationStore(comments, app_user_id=42)
        host = OperationHost(store, now=lambda: now[0])
        handle = host.begin_operation(_proof(), _request(), now[0], 9)
        self.assertIsInstance(handle, OperationHandle)
        permit = host.consume_transition(
            handle, _plan(ordinary=7), consume_grant=lambda: True
        )
        self.assertIsInstance(permit, TransitionPermit)
        now[0] += 5_000
        restored = OperationHost(
            GitHubCommentOperationStore(comments, app_user_id=42), now=lambda: now[0]
        ).restore_operation(_proof(), _request())
        self.assertIsInstance(restored, RestoredOperation)
        self.assertEqual(restored.handle.binding, handle.binding)
        self.assertEqual(restored.handle.created_at_ms, handle.created_at_ms)
        self.assertEqual(restored.deadline_ms, handle.control_deadline_ms)
        self.assertEqual(restored.calls, 16)

    def test_timestamp_domain_is_finite_and_does_not_expand_generation_bounds(self):
        maximum = 9_007_199_254_740_991
        for invalid in (True, -1, 1.0, maximum - CONTROL_DEADLINE_MS + 1, maximum + 1):
            with self.subTest(invalid=invalid):
                host = OperationHost(InMemoryOperationStore(), now=lambda: invalid)
                self.assertEqual(
                    host.begin_operation(_proof(), _request(), 0, 1),
                    OperationRefusal("invalid"),
                )
        host = OperationHost(now=lambda: maximum - CONTROL_DEADLINE_MS)
        handle = host.begin_operation(
            _proof(), _request(), maximum - CONTROL_DEADLINE_MS, 1
        )
        self.assertIsInstance(handle, OperationHandle)
        self.assertEqual(handle.control_deadline_ms, maximum)
        with self.assertRaises(ValueError):
            replace(_request(), root_generation=2_147_483_648)

    def test_real_epoch_does_not_relax_expiry_or_clock_rollback(self):
        now = [1_792_368_000_000]
        host = OperationHost(now=lambda: now[0])
        handle = host.begin_operation(_proof(), _request(), now[0], 1)
        self.assertIsInstance(handle, OperationHandle)
        now[0] += CONTROL_DEADLINE_MS
        self.assertEqual(
            host.consume_transition(handle, _plan(), consume_grant=lambda: True),
            OperationRefusal("expired"),
        )
        now[0] -= 1
        self.assertEqual(
            host.consume_transition(handle, _plan(), consume_grant=lambda: True),
            OperationRefusal("clock_rollback"),
        )
