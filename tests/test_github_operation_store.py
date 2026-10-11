"""The shared trigger entry reloads its record from a GitHub issue comment."""

from __future__ import annotations

import hashlib
import unittest

from review_sensei.hosting.github.operation_host import (
    REVIEW_TRIGGERS,
    AdmissionProof,
    ExecutionResult,
    GitHubCommentOperationStore,
    OperationComment,
    OperationHost,
    OperationRequest,
    OperationWriteUnconfirmed,
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
    def __init__(self, *, app_user_id: int = 42) -> None:
        self.comments: dict[int, OperationComment] = {}
        self.next_id = 1
        self.requests = 0
        self.methods: list[str] = []
        self.app_user_id = app_user_id
        self.lose_post = False
        self.mismatch = False

    def list_comments(self) -> tuple[OperationComment, ...]:
        self.requests += 1
        self.methods.append("GET")
        return tuple(self.comments.values())

    def create(self, body: str) -> OperationComment:
        self.requests += 1
        self.methods.append("POST")
        comment_id = self.next_id
        self.next_id += 1
        comment = OperationComment(comment_id, body, self.app_user_id, "Bot")
        self.comments[comment_id] = comment
        if self.lose_post:
            raise OperationWriteUnconfirmed("lost")
        self.requests += 1
        self.methods.append("GET")
        if self.mismatch:
            return OperationComment(
                comment_id,
                '<!-- review-sensei-operation -->\n```json\n{"no":1}\n```\n',
                self.app_user_id,
                "Bot",
            )
        return comment

    def update(self, comment_id: int, body: str) -> OperationComment:
        self.requests += 1
        self.methods.append("PATCH")
        comment = OperationComment(comment_id, body, self.app_user_id, "Bot")
        self.comments[comment_id] = comment
        self.requests += 1
        self.methods.append("GET")
        return comment


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
                GitHubCommentOperationStore(comments, app_user_id=42),
                now=lambda: 1_000,
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
        bodies = [comment.body for comment in comments.comments.values()]
        self.assertEqual(len(bodies), 5)
        joined = "\n".join(bodies)
        self.assertNotIn("known-output", joined)
        for trigger in REVIEW_TRIGGERS:
            self.assertIn(trigger, joined)

        replay_host = OperationHost(
            GitHubCommentOperationStore(comments, app_user_id=42),
            now=lambda: 1_000,
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
        self.assertEqual(len(comments.comments), 5)

    def test_a_refused_trigger_does_not_write_a_comment(self):
        comments = MemoryComments()
        host = OperationHost(
            GitHubCommentOperationStore(comments, app_user_id=42), now=lambda: 1_000
        )
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
        self.assertEqual(comments.methods, ["GET"])

    def test_one_trigger_writes_the_event_and_the_index(self):
        comments = MemoryComments()
        host = OperationHost(
            GitHubCommentOperationStore(comments, app_user_id=42), now=lambda: 1_000
        )
        result = run_review_trigger(
            host,
            trigger="full-review",
            proof=_proof("b"),
            request=_request("b"),
            plan=_plan("b"),
            provider_call=_output,
            validator=_accept,
            consume_grant=lambda: True,
            server_started_ms=1_000,
        )
        self.assertIsInstance(result, ExecutionResult)
        # list, create event, read it back, create index, read it back,
        # then patch+readback for consume, charge, and the accepted packet.
        self.assertEqual(
            comments.methods,
            [
                "GET",
                "POST",
                "GET",
                "POST",
                "GET",
                "PATCH",
                "GET",
                "PATCH",
                "GET",
                "PATCH",
                "GET",
            ],
        )
        self.assertEqual(comments.requests, 11)

    def test_a_foreign_marker_is_ignored(self):
        comments = MemoryComments()
        comments.comments[1] = OperationComment(
            1,
            "<!-- review-sensei-operation -->\n```json\n{}\n```\n",
            99,
            "User",
        )
        comments.next_id = 2
        with self.assertLogs(
            "review_sensei.hosting.github.operation", level="WARNING"
        ) as logs:
            GitHubCommentOperationStore(comments, app_user_id=42)
        self.assertIn("ignored", logs.output[0])

    def test_two_app_indexes_are_ambiguous(self):
        from review_sensei.hosting.github.operation_host import encode_index_comment

        comments = MemoryComments()
        body = encode_index_comment(
            {"events": {}, "transitions": {}, "grants": {}, "scopes": {}}
        )
        comments.comments[1] = OperationComment(1, body, 42, "Bot")
        comments.comments[2] = OperationComment(2, body, 42, "Bot")
        with self.assertRaisesRegex(ValueError, "ambiguous operation index"):
            GitHubCommentOperationStore(comments, app_user_id=42)

    def test_a_lost_create_is_not_retried_and_reloads(self):
        comments = MemoryComments()
        comments.lose_post = True
        host = OperationHost(
            GitHubCommentOperationStore(comments, app_user_id=42), now=lambda: 1_000
        )
        with self.assertRaises(OperationWriteUnconfirmed):
            run_review_trigger(
                host,
                trigger="reply",
                proof=_proof("b"),
                request=_request("b"),
                plan=_plan("b"),
                provider_call=_output,
                validator=_accept,
                consume_grant=lambda: True,
                server_started_ms=1_000,
            )
        self.assertEqual(comments.methods.count("POST"), 1)
        comments.lose_post = False
        restored = OperationHost(
            GitHubCommentOperationStore(comments, app_user_id=42), now=lambda: 1_000
        )
        result = run_review_trigger(
            restored,
            trigger="reply",
            proof=_proof("b"),
            request=_request("b"),
            plan=_plan("b"),
            provider_call=_output,
            validator=_accept,
            consume_grant=lambda: True,
            server_started_ms=1_000,
        )
        self.assertIsInstance(result, ExecutionResult)
        event_posts = [
            comment.body
            for comment in comments.comments.values()
            if '"kind":"event"' in comment.body
        ]
        self.assertEqual(len(event_posts), 1)

    def test_readback_mismatch_refuses(self):
        comments = MemoryComments()
        comments.mismatch = True
        host = OperationHost(
            GitHubCommentOperationStore(comments, app_user_id=42), now=lambda: 1_000
        )
        with self.assertRaisesRegex(ValueError, "readback failed"):
            run_review_trigger(
                host,
                trigger="verify",
                proof=_proof("b"),
                request=_request("b"),
                plan=_plan("b"),
                provider_call=_output,
                validator=_accept,
                consume_grant=lambda: True,
                server_started_ms=1_000,
            )

    def test_a_prompt_field_is_not_written(self):
        comments = MemoryComments()
        store = GitHubCommentOperationStore(comments, app_user_id=42)
        host = OperationHost(store, now=lambda: 1_000)
        run_review_trigger(
            host,
            trigger="full-review",
            proof=_proof("b"),
            request=_request("b"),
            plan=_plan("b"),
            provider_call=_output,
            validator=_accept,
            consume_grant=lambda: True,
            server_started_ms=1_000,
        )
        proof = _proof("b")
        key = (proof.scope_digest, _request("b").event_id)
        with store.transaction() as txn:
            event = store._live()["events"][key]
            assert isinstance(event, dict)
            event["prompt"] = "SECRET PROMPT"
            with self.assertRaisesRegex(ValueError, "not allowed"):
                txn.commit()
        joined = "\n".join(comment.body for comment in comments.comments.values())
        self.assertNotIn("SECRET PROMPT", joined)
        self.assertNotIn("known-output", joined)
