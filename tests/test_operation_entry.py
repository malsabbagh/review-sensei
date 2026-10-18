"""Admission proof and one-use grant recording for the GitHub operation entry."""

from __future__ import annotations

import hashlib
import unittest
from dataclasses import replace

from review_sensei_github.operation_comments import ResultPartStore
from review_sensei_github.operation_entry import (
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
from review_sensei_github.operation_host import (
    OPERATION_COMMENT_MARKER,
    RESULT_COMMENT_MARKER,
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
        from review_sensei_github.operation_host import OperationComment

        self.requests += 2
        comment = OperationComment(self.next_id, body, 42, "Bot")
        self.comments[self.next_id] = comment
        self.next_id += 1
        return comment

    def update(self, comment_id: int, body: str):
        from review_sensei_github.operation_host import OperationComment

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
        self.assertLessEqual(comments.requests, 64)
        self.assertIsNotNone(first.fresh)
        self.assertIsNone(second.fresh)
        self.assertIsNotNone(second.replay_payload)
        assert second.replay_payload is not None
        self.assertIn(b"complete", second.replay_payload)

    def test_each_trigger_uses_the_same_entry_and_replays(self):
        for trigger in ("full-review", "reply", "reassessment", "verify"):
            comments = MemoryComments()
            store = GitHubCommentOperationStore(comments, app_user_id=42)
            parts = ResultPartStore(comments, app_user_id=42)
            calls = {"n": 0}

            class Result:
                def to_dict(self) -> dict[str, str]:
                    return {"trigger": trigger}

            class Run:
                def __init__(self) -> None:
                    self.result = Result()

            def execute() -> Run:
                calls["n"] += 1
                return Run()

            values = dict(
                trigger=trigger,
                repository_id=7,
                pull_request=11,
                attestation_digest=_hex("attestation"),
                run_id="100",
                run_attempt="1",
                app_id=99,
                reservation_id=f"reservation-{trigger}",
                delivery_id=f"delivery-{trigger}",
                grant=f"grant-{trigger}",
                server_started_ms=1_000,
                execute=execute,
                encode=analysis_payload,
                parts=parts,
                now=lambda: 1_000,
            )
            run_hosted_provider(store, **values)
            replay = run_hosted_provider(store, **values)
            self.assertEqual(calls["n"], 1, trigger)
            self.assertLessEqual(comments.requests, 64, trigger)
            self.assertIsNone(replay.fresh)
            assert replay.replay_payload is not None
            self.assertIn(trigger.encode(), replay.replay_payload)

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


class HostedComputationContinuationTests(unittest.TestCase):
    def setUp(self):
        self.comments = MemoryComments()
        self.calls = []

    def run_event(
        self, event, *, execute=None, parts=True, payload=b"validated-result"
    ):
        store = GitHubCommentOperationStore(self.comments, app_user_id=42)
        result = run_hosted_provider(
            store,
            trigger="reply",
            repository_id=7,
            pull_request=11,
            attestation_digest=_hex("attestation"),
            run_id=f"100-{event}",
            run_attempt="1",
            app_id=99,
            reservation_id=f"reservation-{event}",
            delivery_id=event,
            grant=f"grant-{event}",
            server_started_ms=1000,
            execute=execute or (lambda: self.calls.append(event) or payload),
            encode=lambda value: value,
            parts=ResultPartStore(self.comments, app_user_id=42) if parts else None,
            now=lambda: 1000,
        )
        return result, store

    def test_distinct_revision_continues_and_old_event_replay_keeps_current_cursor(
        self,
    ):
        first, _ = self.run_event("revision-1")
        self.assertEqual(first.fresh, b"validated-result")
        second, store = self.run_event("revision-2")
        self.assertNotIsInstance(second, OperationRefusal)
        self.assertEqual(self.calls, ["revision-1", "revision-2"])
        self.assertEqual(
            [scope["root_generation"] for scope in store._live()["scopes"].values()],
            [2],
        )
        before = store._live()
        replay, store = self.run_event("revision-1")
        self.assertEqual(replay.replay_payload, b"validated-result")
        self.assertEqual(self.calls, ["revision-1", "revision-2"])
        self.assertEqual(store._live(), before)
        self.assertTrue(
            all(not event["ack_attempted"] for event in before["events"].values())
        )
        self.assertTrue(
            all(
                transition["state"] == "confirmed"
                for transition in before["transitions"].values()
            )
        )

    def test_unknown_provider_outcome_retains_writer_exclusion(self):
        def unknown():
            self.calls.append("unknown")
            raise RuntimeError("synthetic provider interruption")

        first, _ = self.run_event("revision-1", execute=unknown)
        self.assertEqual(first.reason, "provider_unknown")
        second, store = self.run_event("revision-2")
        self.assertEqual(second.reason, "unknown_reservation")
        self.assertEqual(self.calls, ["unknown"])
        self.assertTrue(
            any(
                item["state"] == "unknown"
                for item in store._live()["transitions"].values()
            )
        )

    def test_accepted_packet_without_retained_result_does_not_release(self):
        # Oversized-for-inline output cannot be treated as durable solely
        # because the current process still has its bytes in memory.
        first, _ = self.run_event("revision-1", parts=False, payload=b"x" * 90_000)
        self.assertEqual(first.fresh, b"x" * 90_000)
        second, _ = self.run_event("revision-2")
        self.assertEqual(second.reason, "unknown_reservation")
        replay, _ = self.run_event("revision-1", parts=False)
        self.assertEqual(replay.reason, "result_missing")
        self.assertEqual(self.calls, ["revision-1"])

    def test_owned_parts_complete_large_result_and_next_revision(self):
        payload = b"x" * 90_000
        first, _ = self.run_event("revision-1", payload=payload)
        self.assertEqual(first.fresh, payload)
        second, _ = self.run_event("revision-2")
        self.assertNotIsInstance(second, OperationRefusal)
        replay, _ = self.run_event("revision-1")
        self.assertEqual(replay.replay_payload, payload)
        self.assertEqual(self.calls, ["revision-1", "revision-2"])

    def test_crash_before_or_after_completion_write_recovers_without_inference(self):
        class InterruptedComments(MemoryComments):
            mode = None

            def update(self, comment_id, body):
                mode = self.mode if '"state":"confirmed"' in body else None
                if mode:
                    self.mode = None
                if mode == "before":
                    raise KeyboardInterrupt()
                result = super().update(comment_id, body)
                if mode == "after":
                    raise KeyboardInterrupt()
                return result

        for mode in ("before", "after"):
            with self.subTest(mode=mode):
                self.comments = InterruptedComments()
                self.calls = []
                self.comments.mode = mode
                with self.assertRaises(KeyboardInterrupt):
                    self.run_event("revision-1")
                if mode == "before":
                    # Refusal must not bind revision-2 permanently to the
                    # still-held prior cursor.
                    refused, store = self.run_event("revision-2")
                    self.assertEqual(refused.reason, "unknown_reservation")
                    self.assertEqual(len(store._live()["events"]), 1)
                replay, _ = self.run_event("revision-1")
                self.assertEqual(replay.replay_payload, b"validated-result")
                self.assertEqual(self.calls, ["revision-1"])
                next_result, store = self.run_event("revision-2")
                self.assertNotIsInstance(next_result, OperationRefusal)
                self.assertEqual(self.calls, ["revision-1", "revision-2"])
                self.assertEqual(
                    [
                        scope["root_generation"]
                        for scope in store._live()["scopes"].values()
                    ],
                    [2],
                )

    def test_completion_never_clears_an_unknown_transition(self):
        # Simulate an interrupted/unknown external-effect observation after
        # acceptance but before adapter completion. Packet existence alone
        # cannot relax generic unknown-outcome safety.
        class InterruptBeforeCompletion(MemoryComments):
            def update(self, comment_id, body):
                if '"state":"confirmed"' in body:
                    raise KeyboardInterrupt()
                return super().update(comment_id, body)

        self.comments = InterruptBeforeCompletion()
        with self.assertRaises(KeyboardInterrupt):
            self.run_event("revision-1")
        store = GitHubCommentOperationStore(self.comments, app_user_id=42)
        with store.transaction() as txn:
            for transition in store._live()["transitions"].values():
                transition["state"] = "unknown"
            txn.commit()
        refused, _ = self.run_event("revision-1")
        self.assertEqual(refused.reason, "unknown_reservation")
        next_result, _ = self.run_event("revision-2")
        self.assertEqual(next_result.reason, "unknown_reservation")
        self.assertEqual(self.calls, ["revision-1"])

    def test_unowned_result_parts_cannot_complete_the_computation(self):
        class UnownedParts(MemoryComments):
            def list_comments(self):
                return tuple(
                    replace(comment, user_id=999)
                    if RESULT_COMMENT_MARKER in comment.body
                    else comment
                    for comment in super().list_comments()
                )

        self.comments = UnownedParts()
        with self.assertRaisesRegex(ValueError, "result part is missing"):
            self.run_event("revision-1", payload=b"x" * 90_000)
        next_result, store = self.run_event("revision-2")
        self.assertEqual(next_result.reason, "unknown_reservation")
        self.assertEqual(self.calls, ["revision-1"])
        self.assertEqual(len(store._live()["events"]), 1)
        self.assertEqual(
            [item["state"] for item in store._live()["transitions"].values()],
            ["inflight"],
        )
