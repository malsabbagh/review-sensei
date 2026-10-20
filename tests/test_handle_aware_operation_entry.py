"""Actual owned operation admission/restore, not CLI or source-grant qualification."""

from __future__ import annotations

import hashlib
import unittest
from dataclasses import replace

from review_sensei_github.operation_comments import ResultPartStore
from review_sensei_github.operation_entry import (
    HostedComputationIdentity,
    HostedOperationExecution,
    HostedProviderResult,
    call_operation_trigger,
    run_hosted_provider,
)
from review_sensei_github.operation_host import (
    GitHubCommentOperationStore,
    OperationHandle,
    OperationRefusal,
)

from review_sensei.errors import ReviewInputError
from review_sensei.operation_witness import operation_handle_witness
from tests.test_operation_entry import MemoryComments


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


class HandleAwareOperationEntryTests(unittest.TestCase):
    def test_entry_requires_explicit_original_clock_identity_and_bootstrap_for_handle_mode(
        self,
    ):
        values = self.values()
        values["execute_with_operation"] = lambda _: b"owned"
        values["decode"] = lambda payload: payload
        values["clock"] = lambda: values["server_started_ms"]
        for missing in (
            "clock",
            "server_started_ms",
            "identity",
            "bootstrap_dispatches",
        ):
            with self.subTest(missing=missing):
                comments = MemoryComments()
                arguments = dict(values)
                arguments.pop(missing)
                with self.assertRaises(ReviewInputError):
                    call_operation_trigger(
                        GitHubCommentOperationStore(comments, app_user_id=42),
                        parts=ResultPartStore(comments, app_user_id=42),
                        **arguments,
                    )
                self.assertEqual(comments.comments, {})
        comments = MemoryComments()
        self.assertEqual(
            call_operation_trigger(
                GitHubCommentOperationStore(comments, app_user_id=42),
                parts=ResultPartStore(comments, app_user_id=42),
                **values,
            ),
            b"owned",
        )

    def values(self):
        return dict(
            trigger="reassessment",
            repository_id=7,
            pull_request=11,
            attestation_digest=digest("actual-authority"),
            run_id="100",
            run_attempt="1",
            app_id=99,
            reservation_id="source-derived-reservation",
            delivery_id="feedback-event",
            grant="original-grant",
            server_started_ms=1_792_368_000_000,
            bootstrap_dispatches=9,
            identity=HostedComputationIdentity(
                digest("complete-operation"),
                digest("immutable-inventory"),
                digest("complete-selected-sources"),
            ),
            execute=lambda: self.fail(
                "handle-aware mode must not use unbound execution"
            ),
            encode=lambda value: value,
        )

    def test_actual_owned_admission_handle_and_complete_binding_replay_without_execution(
        self,
    ):
        comments = MemoryComments()
        now = [1_792_368_000_000]
        contexts = []

        def execute(context):
            self.assertIsInstance(context, HostedOperationExecution)
            self.assertIsInstance(context.handle, OperationHandle)
            self.assertTrue(
                comments.comments, "original charged admission must already be durable"
            )
            self.assertEqual(context.handle.operation_id, digest("complete-operation"))
            self.assertEqual(
                context.request.inventory_digest, digest("immutable-inventory")
            )
            self.assertEqual(
                context.request.source_digest, digest("complete-selected-sources")
            )
            self.assertEqual(context.handle.calls, 10)
            self.assertEqual(context.handle.created_at_ms, now[0])
            self.assertEqual(context.handle.control_deadline_ms, now[0] + 60_000)
            self.assertNotEqual(
                operation_handle_witness(context.handle), "source-derived-reservation"
            )
            self.assertIsNone(operation_handle_witness(context.handle.__getstate__()))
            contexts.append(context)
            return b"fully validated owned decisions"

        values = self.values()
        result = run_hosted_provider(
            GitHubCommentOperationStore(comments, app_user_id=42),
            parts=ResultPartStore(comments, app_user_id=42),
            now=lambda: now[0],
            execute_with_operation=execute,
            **values,
        )
        self.assertIsInstance(result, HostedProviderResult)
        self.assertEqual(len(contexts), 1)
        self.assertEqual(result.operation.handle.binding, contexts[0].handle.binding)
        self.assertEqual(result.operation.calls, 10)
        self.assertEqual(
            result.operation.accepted_packet.payload_digest,
            hashlib.sha256(result.fresh).hexdigest(),
        )
        now[0] += 5_000
        replay = run_hosted_provider(
            GitHubCommentOperationStore(comments, app_user_id=42),
            parts=ResultPartStore(comments, app_user_id=42),
            now=lambda: now[0],
            execute_with_operation=execute,
            **values,
        )
        self.assertIsInstance(replay, HostedProviderResult)
        self.assertEqual(replay.replay_payload, result.fresh)
        self.assertEqual(len(contexts), 1)
        self.assertEqual(replay.operation.handle.binding, contexts[0].handle.binding)
        self.assertEqual(
            replay.operation.deadline_ms, contexts[0].handle.control_deadline_ms
        )
        self.assertEqual(replay.operation.calls, 10)

    def test_changed_operation_inventory_or_sources_cannot_reuse_accepted_handle(self):
        for field in ("operation_id", "inventory_digest", "source_digest"):
            with self.subTest(field=field):
                comments = MemoryComments()
                values = self.values()
                values["execute_with_operation"] = lambda _: b"owned"
                first = run_hosted_provider(
                    GitHubCommentOperationStore(comments, app_user_id=42),
                    parts=ResultPartStore(comments, app_user_id=42),
                    now=lambda: values["server_started_ms"],
                    **values,
                )
                self.assertIsInstance(first, HostedProviderResult)
                before = dict(comments.comments)
                values["identity"] = replace(
                    values["identity"], **{field: digest("changed")}
                )
                values["execute_with_operation"] = lambda _: self.fail(
                    "changed authority must not execute"
                )
                refused = run_hosted_provider(
                    GitHubCommentOperationStore(comments, app_user_id=42),
                    parts=ResultPartStore(comments, app_user_id=42),
                    now=lambda: values["server_started_ms"],
                    **values,
                )
                self.assertEqual(refused, OperationRefusal("binding_conflict"))
                self.assertEqual(comments.comments, before)

    def test_unknown_after_callback_never_returns_unaccepted_work_or_reexecutes(self):
        comments = MemoryComments()
        values = self.values()
        calls = []
        values["execute_with_operation"] = lambda context: (
            calls.append(context) or b"not-encoded"
        )
        values["encode"] = lambda _: (_ for _ in ()).throw(
            ValueError("bounded encoder rejected result")
        )
        for _ in range(2):
            refused = run_hosted_provider(
                GitHubCommentOperationStore(comments, app_user_id=42),
                parts=ResultPartStore(comments, app_user_id=42),
                now=lambda: values["server_started_ms"],
                **values,
            )
            self.assertEqual(refused, OperationRefusal("provider_unknown"))
        self.assertEqual(len(calls), 1)
