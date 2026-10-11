"""Admission proof and one-use grant recording for the GitHub operation entry."""

from __future__ import annotations

import hashlib
import unittest

from review_sensei.hosting.github.operation_comments import ResultPartStore
from review_sensei.hosting.github.operation_entry import (
    accepted_payload_matches,
    analysis_payload,
    consume_recorded_grant,
    fingerprints_for_head,
    github_admission_proof,
    github_event_id,
    operation_handle_witness,
    run_admitted_trigger,
    run_hosted_provider,
)
from review_sensei.hosting.github.operation_host import (
    OPERATION_COMMENT_MARKER,
    AdmissionProof,
    ExecutionResult,
    GitHubCommentOperationStore,
    OperationHandle,
    OperationHost,
    OperationRefusal,
    OperationRequest,
    ProviderOutput,
    TransitionPlan,
)


def _hex(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _request(event: str) -> OperationRequest:
    return OperationRequest(
        event_id=event,
        operation_id=_hex("operation"),
        inventory_digest=_hex("inventory"),
        source_digest=_hex("source"),
        root_sha=_hex("root"),
        root_generation=0,
        producer_contract="existing-inventory-reassessment",
    )


def _plan(event: str) -> TransitionPlan:
    return TransitionPlan(
        attempt_id=_hex("attempt"),
        sequence=0,
        ordinary_dispatches=1,
        fence_dispatches=0,
        prior_root_sha=_hex("root"),
        prior_root_generation=0,
        target_root_sha=_hex("next"),
        target_root_generation=1,
        request_digest=event,
        dispatch_digest=_hex("dispatch"),
    )


class MemoryComments:
    def __init__(self) -> None:
        self.comments: dict[int, object] = {}
        self.next_id = 1
        self.requests = 0

    def list_comments(self):
        self.requests += 1
        return tuple(self.comments.values())

    def create(self, body: str):
        from review_sensei.hosting.github.operation_host import OperationComment

        self.requests += 2
        comment = OperationComment(self.next_id, body, 42, "Bot")
        self.comments[self.next_id] = comment
        self.next_id += 1
        return comment

    def update(self, comment_id: int, body: str):
        from review_sensei.hosting.github.operation_host import OperationComment

        self.requests += 2
        comment = OperationComment(comment_id, body, 42, "Bot")
        self.comments[comment_id] = comment
        return comment


def _proof(**overrides: object) -> AdmissionProof | OperationRefusal:
    values = {
        "repository_id": 7,
        "pull_request": 11,
        "attestation_digest": _hex("attestation"),
        "run_id": "100",
        "run_attempt": "1",
        "app_id": 99,
        "reservation_id": "reservation-1",
        "server_authenticated": True,
    }
    values.update(overrides)
    return github_admission_proof(**values)


class AdmissionTests(unittest.TestCase):
    def test_missing_and_forged_proofs_do_not_write(self):
        comments = MemoryComments()
        store = GitHubCommentOperationStore(comments, app_user_id=42)
        host = OperationHost(store, now=lambda: 1_000)
        event = github_event_id(delivery_id="delivery-1")
        before = comments.requests
        for proof in (
            _proof(server_authenticated=False),
            _proof(attestation_digest="not-a-digest"),
            _proof(reservation_id=""),
        ):
            result = run_admitted_trigger(
                host,
                trigger="full-review",
                proof=proof,
                request=_request(event),
                plan=_plan(event),
                provider_call=lambda: ProviderOutput(b"x", _hex("d")),
                validator=lambda _output, _measured: True,
                grant="grant-1",
                server_started_ms=1_000,
            )
            self.assertIsInstance(result, OperationRefusal)
        self.assertEqual(comments.requests, before)
        self.assertEqual(len(comments.comments), 0)

    def test_a_grant_cannot_fund_a_second_allowance(self):
        comments = MemoryComments()
        calls = {"n": 0}

        def provider() -> ProviderOutput:
            calls["n"] += 1
            return ProviderOutput(output=b"known", decisions_digest=_hex("d"))

        store = GitHubCommentOperationStore(comments, app_user_id=42)
        host = OperationHost(store, now=lambda: 1_000)
        proof = _proof()
        assert isinstance(proof, AdmissionProof)
        first = github_event_id(delivery_id="delivery-1")
        accepted = run_admitted_trigger(
            host,
            trigger="full-review",
            proof=proof,
            request=_request(first),
            plan=_plan(first),
            provider_call=provider,
            validator=lambda output, measured: measured == len(output.output),
            grant="grant-1",
            server_started_ms=1_000,
        )
        self.assertIsInstance(accepted, ExecutionResult)
        with store.transaction() as txn:
            self.assertFalse(store.note_consumed_grant("grant-1"))
            txn.rollback()
        self.assertEqual(calls["n"], 1)
        digest = hashlib.sha256(b"grant-1").hexdigest()
        joined = "\n".join(comment.body for comment in comments.comments.values())
        self.assertIn(digest, joined)

    def test_comment_and_delivery_events_differ(self):
        self.assertNotEqual(
            github_event_id(delivery_id="1"),
            github_event_id(comment_id=1, updated_at="1"),
        )
        with self.assertRaises(ValueError):
            github_event_id()

    def test_a_snapshot_is_not_an_operation_witness(self):
        self.assertIsNone(operation_handle_witness({"binding": "x"}))
        self.assertIsNone(operation_handle_witness("a" * 64))
        handle = OperationHandle(
            scope_digest="a" * 64,
            event_id="b" * 64,
            operation_id="c" * 64,
            binding="d" * 64,
            created_at_ms=1,
            control_deadline_ms=2,
            calls=1,
            sequence=0,
            root_sha="e" * 64,
            root_generation=0,
        )
        self.assertEqual(operation_handle_witness(handle), "d" * 64)

    def test_recorded_grant_refuses_a_repeat_outside_a_trigger(self):
        comments = MemoryComments()
        store = GitHubCommentOperationStore(comments, app_user_id=42)
        self.assertTrue(consume_recorded_grant(store, "grant-9"))
        self.assertFalse(consume_recorded_grant(store, "grant-9"))

    def test_hosted_provider_calls_once_and_replays_the_bytes(self):
        comments = MemoryComments()
        store = GitHubCommentOperationStore(comments, app_user_id=42)
        parts = ResultPartStore(comments, app_user_id=42)
        calls = {"n": 0}

        class Result:
            def to_dict(self) -> dict[str, str]:
                return {"review_status": "complete"}

        class Run:
            def __init__(self) -> None:
                self.result = Result()

        def execute() -> Run:
            calls["n"] += 1
            return Run()

        values = dict(
            trigger="full-review",
            repository_id=7,
            pull_request=11,
            attestation_digest=_hex("attestation"),
            run_id="100",
            run_attempt="1",
            app_id=99,
            reservation_id="reservation-1",
            delivery_id="delivery-1",
            grant="grant-1",
            server_started_ms=1_000,
            execute=execute,
            encode=analysis_payload,
            parts=parts,
            now=lambda: 1_000,
        )
        first = run_hosted_provider(store, **values)
        second = run_hosted_provider(store, **values)
        self.assertEqual(calls["n"], 1)
        self.assertIsNotNone(first.fresh)
        self.assertIsNone(second.fresh)
        self.assertIsNotNone(second.replay_payload)
        assert second.replay_payload is not None
        self.assertIn(b"complete", second.replay_payload)

    def test_fingerprints_come_from_the_current_head_only(self):
        head = "a" * 40
        other = "b" * 40
        current = "c" * 64
        stale = "d" * 64
        body = (
            f"<!-- reviewsensei:finding:v2 repo=7 pr=11 head={head} "
            f"base={'e' * 40} fingerprint={current} state=new blocking=true -->\n"
            f"<!-- reviewsensei:finding:v2 repo=7 pr=11 head={other} "
            f"base={'e' * 40} fingerprint={stale} state=new blocking=true -->"
        )
        self.assertEqual(
            fingerprints_for_head(
                body, repository_id=7, pull_request=11, head_sha=head
            ),
            (current,),
        )

    def test_publish_requires_the_accepted_digest_when_a_record_exists(self):
        digest = "a" * 64
        self.assertIsNone(accepted_payload_matches(("hello",), digest))
        self.assertFalse(
            accepted_payload_matches((f"{OPERATION_COMMENT_MARKER}\n{{}}\n",), digest)
        )
        self.assertTrue(
            accepted_payload_matches(
                (f"{OPERATION_COMMENT_MARKER}\n{digest}\n",), digest
            )
        )
