"""The shared trigger entry reloads its record from a GitHub issue comment."""

from __future__ import annotations

import hashlib
import unittest

from review_sensei.hosting.github.operation_host import (
    REVIEW_TRIGGERS,
    AdmissionProof,
    ExecutionResult,
    GitHubCommentOperationStore,
    OperationHost,
    OperationRequest,
    ProviderOutput,
    TransitionPlan,
    run_review_trigger,
)


def _hex(text: str) -> str:
    return text * 64


def _proof(scope: str = "a") -> AdmissionProof:
    return AdmissionProof(
        scope_digest=_hex(scope),
        authority_digest=_hex("e"),
        execution_identity="f" * 32,
        owner_digest=_hex("1"),
        reservation_digest=_hex("2"),
        server_authenticated=True,
    )


def _request(event: str) -> OperationRequest:
    return OperationRequest(
        event_id=_hex(event),
        operation_id=_hex("c"),
        inventory_digest=_hex("0"),
        source_digest=_hex("d"),
        root_sha=_hex("3"),
        root_generation=0,
        producer_contract="existing-inventory-reassessment",
    )


def _plan(event: str) -> TransitionPlan:
    return TransitionPlan(
        attempt_id=_hex("4"),
        sequence=0,
        ordinary_dispatches=1,
        fence_dispatches=0,
        prior_root_sha=_hex("3"),
        prior_root_generation=0,
        target_root_sha=_hex("5"),
        target_root_generation=1,
        request_digest=_hex(event),
        dispatch_digest=_hex("6"),
    )


def _output() -> ProviderOutput:
    return ProviderOutput(output=b"known-output", decisions_digest=_hex("d"))


def _accept(output: ProviderOutput, measured: int) -> bool:
    return measured == len(output.output)


class MemoryComments:
    def __init__(self) -> None:
        self.comments: dict[int, str] = {}
        self.next_id = 1

    def find(self, marker: str) -> tuple[int, str] | None:
        for comment_id, body in self.comments.items():
            if marker in body:
                return comment_id, body
        return None

    def create(self, body: str) -> tuple[int, str]:
        comment_id = self.next_id
        self.next_id += 1
        self.comments[comment_id] = body
        return comment_id, body

    def update(self, comment_id: int, body: str) -> str:
        self.comments[comment_id] = body
        return body


class GitHubOperationStoreTests(unittest.TestCase):
    def test_every_trigger_reloads_from_one_comment_without_a_second_call(self):
        comments = MemoryComments()
        calls = {"n": 0}

        def provider() -> ProviderOutput:
            calls["n"] += 1
            return _output()

        for trigger, event in zip(
            ("full-review", "reply", "reassessment", "verify"),
            ("b", "c", "d", "e"),
            strict=True,
        ):
            host = OperationHost(
                GitHubCommentOperationStore(comments), now=lambda: 1_000
            )
            result = run_review_trigger(
                host,
                trigger=trigger,
                proof=_proof(event),
                request=_request(event),
                plan=_plan(event),
                provider_call=provider,
                validator=_accept,
                consume_grant=lambda: True,
                server_started_ms=1_000,
            )
            self.assertIsInstance(result, ExecutionResult)
            assert isinstance(result, ExecutionResult)
            self.assertEqual(result.status, "accepted")
            self.assertEqual(result.packet is not None, True)
            assert result.packet is not None
            self.assertEqual(
                result.packet.payload_digest,
                hashlib.sha256(b"known-output").hexdigest(),
            )

        self.assertEqual(calls["n"], 4)
        self.assertEqual(len(comments.comments), 1)
        body = next(iter(comments.comments.values()))
        self.assertNotIn("known-output", body)
        for trigger in REVIEW_TRIGGERS:
            self.assertIn(trigger, body)

        replay_host = OperationHost(
            GitHubCommentOperationStore(comments), now=lambda: 1_000
        )
        replay = run_review_trigger(
            replay_host,
            trigger="reply",
            proof=_proof("c"),
            request=_request("c"),
            plan=_plan("c"),
            provider_call=provider,
            validator=_accept,
            consume_grant=lambda: True,
            server_started_ms=1_000,
        )
        self.assertIsInstance(replay, ExecutionResult)
        assert isinstance(replay, ExecutionResult)
        self.assertEqual(replay.status, "accepted")
        self.assertEqual(calls["n"], 4)
        self.assertEqual(len(comments.comments), 1)

    def test_a_refused_trigger_does_not_write_a_comment(self):
        comments = MemoryComments()
        host = OperationHost(GitHubCommentOperationStore(comments), now=lambda: 1_000)
        refused = run_review_trigger(
            host,
            trigger="not-a-trigger",
            proof=_proof(),
            request=_request("b"),
            plan=_plan("b"),
            provider_call=_output,
            validator=_accept,
            consume_grant=lambda: True,
            server_started_ms=1_000,
        )
        self.assertEqual(getattr(refused, "reason", None), "trigger")
        self.assertEqual(comments.comments, {})
