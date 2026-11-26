"""Unit tests for ReviewSensei v6 GitHub-governed recovery (Phases 2 & 3)."""

from __future__ import annotations

import json
import unittest
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from typing import Any

from review_sensei_github.errors import (
    GitHubHTTPPermissionError,
)
from review_sensei_github.v6_credential_matrix import (
    WORKFLOW_READ_ONLY_SCOPES,
    GitHubEventKind,
    RepositoryVisibility,
)
from review_sensei_github.v6_pagination import (
    NoProgressGuard,
    PaginatedRecord,
    RecoveryStalledError,
    StabilityVerifiedPaginator,
    compute_records_prefix_digest,
)
from review_sensei_github.v6_recovery_engine import (
    MutationSanityBound,
    MutationSanityBoundError,
    RecoveryCompleted,
    RecoveryHold,
    RecoveryPaused,
    RecoveryRequest,
    StaleWriterOwnershipError,
    V6RecoveryEngine,
    verify_writer_ownership,
)
from review_sensei_github.v6_recovery_ledger import (
    INTENT_SCHEMA,
    PROGRESS_HINT_SCHEMA,
    RECEIPT_SCHEMA,
    UNRESOLVED_RESULT_SCHEMA,
    AdvisoryProgressHint,
    CompletionReceiptRecord,
    UnresolvedResultRecord,
    V6LedgerIntegrityError,
    WriteIntentRecord,
    WriterOwnershipProof,
    canonical_sha256,
    extract_v6_record_markers,
    format_v6_record_marker,
    parse_v6_record_marker,
    reconcile_immutable_records,
    verify_app_authored_record,
)
from review_sensei_github.v6_transport import (
    MAINTAINER_RERUN_COMMAND,
    DualCredentialGitHubClient,
)

from review_sensei.errors import EvidenceBudgetExhausted, ReviewInputError
from review_sensei.finding_lifecycle_store import _preflight_with_breakdown

HEAD_SHA = "a" * 40
OTHER_SHA = "b" * 40
APP_ID = 424242
BOT_LOGIN = "reviewsensei[bot]"


def _app_meta(*, app_id: int = APP_ID) -> dict[str, Any]:
    return {
        "performed_via_github_app": {"id": app_id, "slug": "reviewsensei"},
        "user": {"id": 9001, "login": BOT_LOGIN, "type": "Bot"},
    }


def _human_meta() -> dict[str, Any]:
    return {
        "performed_via_github_app": None,
        "user": {"id": 77, "login": "contributor", "type": "User"},
    }


def _make_proof(
    *,
    epoch: int = 1,
    head_sha: str = HEAD_SHA,
    verified_by_broker: bool = True,
    operation_id: str = "op-1",
) -> WriterOwnershipProof:
    return WriterOwnershipProof(
        repository_id=101,
        pull_request=12,
        operation_id=operation_id,
        run_id=5001,
        run_attempt=1,
        writer_epoch=epoch,
        pr_head_sha=head_sha,
        verified_by_broker=verified_by_broker,
    )


def _authoritative_fence(
    _proof: WriterOwnershipProof,
    *,
    active_writer_epoch: int = 1,
) -> dict[str, Any]:
    return {
        "repository_id": 101,
        "pull_request": 12,
        "pr_head_sha": HEAD_SHA,
        "active_writer_epoch": active_writer_epoch,
        "pr_state": "open",
        "active_run_id": 5001,
        "active_run_attempt": 1,
        "run_status": "in_progress",
        "verified_by_broker": True,
    }


def _make_unresolved_record(
    *,
    operation_id: str = "op-1",
    head_sha: str = HEAD_SHA,
    findings: tuple[dict[str, Any], ...] = (
        {"path": "src/app.py", "line": 10, "body": "Check bounds"},
    ),
    required_evidence_digests: tuple[str, ...] = ("ev-digest-1",),
    artifact_cache_sha256: str | None = None,
) -> UnresolvedResultRecord:
    payload_sha = canonical_sha256(findings)
    return UnresolvedResultRecord(
        record_id=f"unresolved:{operation_id}",
        operation_id=operation_id,
        pr_head_sha=head_sha,
        payload_sha256=payload_sha,
        findings_payload=findings,
        required_evidence_digests=required_evidence_digests,
        artifact_cache_sha256=artifact_cache_sha256,
    )


class ScriptedTransport:
    """Deterministic stub transport supporting dynamic handlers and fault injection."""

    def __init__(self, handler: Any) -> None:
        self._handler = handler
        self.calls: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[int, Mapping[str, str], Any]:
        call = {
            "method": method,
            "path": path,
            "headers": dict(headers or {}),
            "body": body,
            "timeout_seconds": timeout_seconds,
        }
        self.calls.append(call)
        return self._handler(call)


class RecoveryLedgerTests(unittest.TestCase):
    """Verify immutable ledger schemas, markers, App provenance, and reconciliation."""

    def test_writer_ownership_proof_strict_validation(self) -> None:
        proof = _make_proof()
        with self.assertRaises(FrozenInstanceError):
            proof.writer_epoch = 2  # type: ignore[misc]

        with self.assertRaises(ReviewInputError):
            WriterOwnershipProof(
                repository_id=True,  # type: ignore[arg-type]
                pull_request=12,
                operation_id="op-1",
                run_id=5001,
                run_attempt=1,
                writer_epoch=1,
                pr_head_sha=HEAD_SHA,
                verified_by_broker=True,
            )
        with self.assertRaises(ReviewInputError):
            WriterOwnershipProof(
                repository_id=101,
                pull_request=12,
                operation_id="op-1",
                run_id=5001,
                run_attempt=1,
                writer_epoch=1,
                pr_head_sha="not-a-40-hex-sha",
                verified_by_broker=True,
            )
        with self.assertRaises(ReviewInputError):
            WriterOwnershipProof(
                repository_id=101,
                pull_request=12,
                operation_id=" ",
                run_id=5001,
                run_attempt=1,
                writer_epoch=1,
                pr_head_sha=HEAD_SHA,
                verified_by_broker=True,
            )

    def test_marker_codec_round_trip_and_fail_closed_tamper_checks(self) -> None:
        unresolved = _make_unresolved_record(artifact_cache_sha256="c" * 64)
        intent = WriteIntentRecord(
            record_id="intent:op-1:1",
            operation_id="op-1",
            writer_epoch=1,
            pr_head_sha=HEAD_SHA,
            effect_kind="pr_review_publish",
            payload_sha256=unresolved.payload_sha256,
            target_digest="target-1",
        )
        receipt = CompletionReceiptRecord(
            record_id="receipt:op-1:1",
            operation_id="op-1",
            writer_epoch=1,
            github_effect_id=777,
            payload_sha256=unresolved.payload_sha256,
            completed_at="2026-11-26T16:00:00Z",
        )
        hint = AdvisoryProgressHint(
            operation_id="op-1",
            recovery_phase="scan_comments",
            cursor_hint="cursor-1",
            verified_prefix_count=3,
            verified_prefix_digest="d" * 64,
            last_verified_id=1003,
            stall_count=1,
            last_progress_digest="e" * 64,
            transient_error_count=0,
        )

        self.assertEqual(intent.schema, INTENT_SCHEMA)
        self.assertEqual(receipt.schema, RECEIPT_SCHEMA)
        self.assertEqual(unresolved.schema, UNRESOLVED_RESULT_SCHEMA)
        self.assertEqual(hint.schema, PROGRESS_HINT_SCHEMA)

        for record in (intent, receipt, unresolved, hint):
            marker = format_v6_record_marker(record)
            parsed = parse_v6_record_marker(marker)
            self.assertEqual(parsed, record)

        # Tampered digest fails closed.
        good_marker = format_v6_record_marker(intent)
        tampered = good_marker.replace('"writer_epoch":1', '"writer_epoch":9')
        with self.assertRaises(ReviewInputError):
            parse_v6_record_marker(tampered)

        # Unknown schema fails closed.
        unknown_payload = {
            "schema": "reviewsensei:v6:unknown:v9",
            "operation_id": "op-1",
        }
        unknown_marker = (
            f"<!-- reviewsensei:v6:record sha256={canonical_sha256(unknown_payload)} "
            f"payload={json.dumps(unknown_payload)} -->"
        )
        with self.assertRaises(ReviewInputError):
            parse_v6_record_marker(unknown_marker)

        # AdvisoryProgressHint must NEVER hold sole copy of intents/receipts/results.
        forbidden_payload = {
            "schema": PROGRESS_HINT_SCHEMA,
            "operation_id": "op-1",
            "recovery_phase": "scan",
            "cursor_hint": None,
            "verified_prefix_count": 0,
            "verified_prefix_digest": "",
            "last_verified_id": None,
            "stall_count": 0,
            "last_progress_digest": "",
            "transient_error_count": 0,
            "pending_write_intent": {"id": "sneaky"},
        }
        forbidden_marker = (
            f"<!-- reviewsensei:v6:record sha256={canonical_sha256(forbidden_payload)} "
            f"payload={json.dumps(forbidden_payload)} -->"
        )
        with self.assertRaises(ReviewInputError):
            parse_v6_record_marker(forbidden_marker)

        # Extract strict vs non-strict and App provenance verification.
        body = f"Header\n{good_marker}\n"
        self.assertEqual(extract_v6_record_markers(body, strict=True), (intent,))
        self.assertTrue(verify_app_authored_record(_app_meta(), expected_app_id=APP_ID))
        self.assertFalse(
            verify_app_authored_record(_human_meta(), expected_app_id=APP_ID)
        )

    def test_reconcile_immutable_records_union_and_stale_writer_non_overwrite(
        self,
    ) -> None:
        payload_sha = "1" * 64
        other_sha = "2" * 64
        intent_active = WriteIntentRecord(
            record_id="intent:op-1:1",
            operation_id="op-1",
            writer_epoch=1,
            pr_head_sha=HEAD_SHA,
            effect_kind="pr_review_publish",
            payload_sha256=payload_sha,
            target_digest="t1",
        )
        intent_pending = WriteIntentRecord(
            record_id="intent:op-2:1",
            operation_id="op-2",
            writer_epoch=1,
            pr_head_sha=HEAD_SHA,
            effect_kind="pr_review_publish",
            payload_sha256=other_sha,
            target_digest="t2",
        )
        receipt_1 = CompletionReceiptRecord(
            record_id="receipt:op-1:1",
            operation_id="op-1",
            writer_epoch=1,
            github_effect_id=555,
            payload_sha256=payload_sha,
            completed_at="2026-11-26T16:00:00Z",
        )
        # Stale runner attempts to reuse record_id with a different payload.
        stale_conflicting = WriteIntentRecord(
            record_id="intent:op-2:1",
            operation_id="op-3",
            writer_epoch=0,
            pr_head_sha=HEAD_SHA,
            effect_kind="pr_review_publish",
            payload_sha256="3" * 64,
            target_digest="t3",
        )

        clean_summary = reconcile_immutable_records(
            [intent_active, intent_active, intent_pending],
            [receipt_1, receipt_1],
        )
        self.assertEqual(clean_summary.completed_operations, ("op-1",))
        self.assertEqual(clean_summary.unreconciled_intents, (intent_pending,))
        self.assertEqual(clean_summary.matched_receipts, (receipt_1,))
        self.assertEqual(clean_summary.conflicting_records, ())

        conflict_summary = reconcile_immutable_records(
            [intent_active, intent_pending, stale_conflicting],
            [receipt_1],
        )
        self.assertEqual(conflict_summary.completed_operations, ("op-1",))
        self.assertIn(intent_pending, conflict_summary.conflicting_records)
        self.assertIn(stale_conflicting, conflict_summary.conflicting_records)


class PaginationAndNoProgressGuardTests(unittest.TestCase):
    """Verify stability-verified pagination, deletion rescanning, and loop guards."""

    def test_graphql_stability_verified_resumes_and_skips_redundant_pages(self) -> None:
        prefix_records = [
            PaginatedRecord.from_payload("c1", {"id": "c1", "body": "one"}),
            PaginatedRecord.from_payload("c2", {"id": "c2", "body": "two"}),
        ]
        prefix_digest = compute_records_prefix_digest(prefix_records)
        hint = AdvisoryProgressHint(
            operation_id="op-gql",
            recovery_phase="graphql_scan",
            cursor_hint="cursor-p1",
            verified_prefix_count=2,
            verified_prefix_digest=prefix_digest,
            last_verified_id="c2",
            stall_count=0,
            last_progress_digest=prefix_digest,
            transient_error_count=0,
        )

        seen_after: list[str | None] = []

        def wf_handler(call: dict[str, Any]) -> tuple[int, dict[str, str], Any]:
            after = call["body"]["variables"]["after"]
            seen_after.append(after)
            return (
                200,
                {"X-RateLimit-Remaining": "999"},
                {
                    "data": {
                        "connection": {
                            "nodes": [{"id": "c3", "body": "three"}],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                },
            )

        wf_transport = ScriptedTransport(wf_handler)
        app_transport = ScriptedTransport(
            lambda _: (500, {}, {"message": "unexpected app call"})
        )
        client = DualCredentialGitHubClient(
            workflow_token_transport=wf_transport,
            broker_app_transport=app_transport,
        )
        paginator = StabilityVerifiedPaginator(client)
        guard = NoProgressGuard.from_advisory_hint(hint)

        result = paginator.scan_graphql(
            query="query($after:String){...}",
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            progress_guard=guard,
            advisory_hint=hint,
            known_records=prefix_records,
            current_total_count=3,
            current_prefix_digest=prefix_digest,
            anchor_resolved_id="c2",
            anchor_content_sha256=prefix_records[1].content_sha256,
            expected_anchor_sha256=prefix_records[1].content_sha256,
        )

        self.assertTrue(result.resumed_from_cursor)
        self.assertFalse(result.rescanned)
        self.assertEqual(seen_after, ["cursor-p1"])
        self.assertEqual([r.record_id for r in result.records], ["c1", "c2", "c3"])
        self.assertEqual(len(app_transport.calls), 0)

    def test_earlier_deletion_invalidates_stability_and_rescans_with_dedup(
        self,
    ) -> None:
        # Prior run saw c1, c2, c3. Between runs, c1 was deleted and c4 was added.
        old_prefix = [
            PaginatedRecord.from_payload("c1", {"id": "c1", "body": "one"}),
            PaginatedRecord.from_payload("c2", {"id": "c2", "body": "two"}),
        ]
        old_digest = compute_records_prefix_digest(old_prefix)
        hint = AdvisoryProgressHint(
            operation_id="op-del",
            recovery_phase="graphql_scan",
            cursor_hint="cursor-p1",
            verified_prefix_count=2,
            verified_prefix_digest=old_digest,
            last_verified_id="c2",
            stall_count=0,
            last_progress_digest=old_digest,
            transient_error_count=0,
        )

        new_prefix_digest = compute_records_prefix_digest([old_prefix[1]])
        seen_after: list[str | None] = []

        def wf_handler(call: dict[str, Any]) -> tuple[int, dict[str, str], Any]:
            after = call["body"]["variables"]["after"]
            seen_after.append(after)
            if after is None:
                return (
                    200,
                    {"X-RateLimit-Remaining": "998"},
                    {
                        "data": {
                            "connection": {
                                "nodes": [
                                    {"id": "c2", "body": "two"},
                                    {"id": "c3", "body": "three"},
                                ],
                                "pageInfo": {"hasNextPage": True, "endCursor": "c3"},
                            }
                        }
                    },
                )
            return (
                200,
                {"X-RateLimit-Remaining": "997"},
                {
                    "data": {
                        "connection": {
                            "nodes": [
                                {"id": "c3", "body": "three"},
                                {"id": "c4", "body": "four"},
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                },
            )

        client = DualCredentialGitHubClient(
            workflow_token_transport=ScriptedTransport(wf_handler),
            broker_app_transport=ScriptedTransport(
                lambda _: (500, {}, {"message": "unexpected app call"})
            ),
        )
        paginator = StabilityVerifiedPaginator(client)
        guard = NoProgressGuard(operation_id="op-del")

        result = paginator.scan_graphql(
            query="query($after:String){...}",
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            progress_guard=guard,
            advisory_hint=hint,
            known_records=old_prefix,
            current_total_count=3,
            current_prefix_digest=new_prefix_digest,
            anchor_resolved_id="c2",
        )

        self.assertFalse(result.resumed_from_cursor)
        self.assertTrue(result.rescanned)
        self.assertEqual(seen_after, [None, "c3"])
        self.assertEqual([r.record_id for r in result.records], ["c2", "c3", "c4"])

        # REST offset pagination also refuses blind resume and rescans cleanly.
        self.assertFalse(
            paginator.verify_resume_stability(
                mode="rest",
                cursor_hint="/repos/octo/repo/issues/12/comments?page=2",
                verified_prefix_count=2,
                verified_prefix_digest=old_digest,
            )
        )

    def test_since_parameter_rejected_and_over_512_pages_supported(self) -> None:
        from urllib.parse import parse_qs, urlsplit

        def wf_handler(call: dict[str, Any]) -> tuple[int, dict[str, str], Any]:
            qs = parse_qs(urlsplit(call["path"]).query)
            page = int(qs["page"][0]) if "page" in qs else 1
            headers = {"X-RateLimit-Remaining": str(15000 - page)}
            if page < 525:
                headers["Link"] = (
                    f'<https://api.github.com/repos/octo/repo/issues/12/comments?page={page + 1}&sort=created&direction=asc>; rel="next"'
                )
            return 200, headers, [{"id": page, "body": f"msg-{page}"}]

        wf_transport = ScriptedTransport(wf_handler)
        app_transport = ScriptedTransport(
            lambda _: (500, {}, {"message": "unexpected app call"})
        )
        client = DualCredentialGitHubClient(
            workflow_token_transport=wf_transport,
            broker_app_transport=app_transport,
            is_ghec=True,
        )
        paginator = StabilityVerifiedPaginator(client)
        guard = NoProgressGuard(operation_id="op-525")

        with self.assertRaises(ReviewInputError) as caught:
            paginator.scan_rest(
                endpoint_id="pr_issue_comments",
                path="/repos/octo/repo/issues/12/comments?since=2026-01-01T00:00:00Z",
                event=GitHubEventKind.PULL_REQUEST,
                visibility=RepositoryVisibility.PUBLIC,
                granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
                progress_guard=guard,
            )
        self.assertEqual(caught.exception.diagnostic, "v6_since_parameter_forbidden")

        res = paginator.scan_rest(
            endpoint_id="pr_issue_comments",
            path="/repos/octo/repo/issues/12/comments?per_page=100",
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            progress_guard=guard,
        )
        self.assertEqual(res.pages_fetched, 525)
        self.assertEqual(len(res.records), 525)
        self.assertEqual(len(app_transport.calls), 0)

    def test_cross_restart_no_progress_guard_triggers_even_with_new_opaque_cursor(
        self,
    ) -> None:
        # Run 1: page 1 yields empty records with cursor-1 -> stall_count=1.
        guard1 = NoProgressGuard(
            operation_id="op-loop", recovery_phase="graphql_scan", max_stall_steps=2
        )
        guard1.record_step(
            new_records_count=0,
            reconciled_effect=False,
            new_phase="graphql_scan",
            state_digest="digest-1",
        )
        self.assertEqual(guard1.stall_count, 1)
        persisted_marker = format_v6_record_marker(
            guard1.to_advisory_hint(cursor_hint="opaque-cursor-a")
        )

        # Restart 2: restore AdvisoryProgressHint from PR, server gives brand-new
        # opaque cursor 'opaque-cursor-b' with 0 new records -> stalls immediately!
        restored_hint = parse_v6_record_marker(persisted_marker)
        assert isinstance(restored_hint, AdvisoryProgressHint)
        guard2 = NoProgressGuard.from_advisory_hint(restored_hint, max_stall_steps=2)

        def wf_handler(call: dict[str, Any]) -> tuple[int, dict[str, str], Any]:
            return (
                200,
                {"X-RateLimit-Remaining": "999"},
                {
                    "data": {
                        "connection": {
                            "nodes": [],
                            "pageInfo": {
                                "hasNextPage": True,
                                "endCursor": "opaque-cursor-b",
                            },
                        }
                    }
                },
            )

        client = DualCredentialGitHubClient(
            workflow_token_transport=ScriptedTransport(wf_handler),
            broker_app_transport=ScriptedTransport(
                lambda _: (500, {}, {"message": "unused"})
            ),
        )
        paginator = StabilityVerifiedPaginator(client)
        with self.assertRaises(RecoveryStalledError):
            paginator.scan_graphql(
                query="query($after:String){...}",
                event=GitHubEventKind.PULL_REQUEST,
                visibility=RepositoryVisibility.PUBLIC,
                granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
                progress_guard=guard2,
                advisory_hint=restored_hint,
                current_total_count=0,
                current_prefix_digest="x" * 64,
            )


class RecoveryEngineTests(unittest.TestCase):
    """End-to-end tests for fencing, readback, lost writes, cache loss, and bounds."""

    def test_stale_writer_rejected_at_write_boundary_before_any_mutation(self) -> None:
        unresolved = _make_unresolved_record()
        unresolved_comment = {
            "id": 100,
            "body": format_v6_record_marker(unresolved),
            **_app_meta(),
        }

        def wf_handler(call: dict[str, Any]) -> tuple[int, dict[str, str], Any]:
            if "/issues/12/comments" in call["path"]:
                return 200, {"X-RateLimit-Remaining": "999"}, [unresolved_comment]
            if "/pulls/12/reviews" in call["path"]:
                return 200, {"X-RateLimit-Remaining": "998"}, []
            raise AssertionError(f"unexpected workflow call {call}")

        app_calls: list[dict[str, Any]] = []

        def app_handler(call: dict[str, Any]) -> tuple[int, dict[str, str], Any]:
            app_calls.append(call)
            if call["method"] == "GET" and call["path"] == "/repos/octo/repo/pulls/12":
                return (
                    200,
                    {"X-RateLimit-Remaining": "4999"},
                    {
                        "state": "open",
                        "head": {"sha": HEAD_SHA},
                        "base": {"repo": {"id": 101}},
                    },
                )
            if (
                call["method"] == "GET"
                and call["path"] == "/repos/octo/repo/actions/runs/5001"
            ):
                # Broker reports epoch 2 is now active -> epoch 1 writer is stale!
                return (
                    200,
                    {"X-RateLimit-Remaining": "4998"},
                    {
                        "id": 5001,
                        "run_attempt": 1,
                        "status": "in_progress",
                        "writer_epoch": 2,
                        "verified_by_broker": True,
                    },
                )
            raise AssertionError(f"stale writer must never reach mutation: {call}")

        client = DualCredentialGitHubClient(
            workflow_token_transport=ScriptedTransport(wf_handler),
            broker_app_transport=ScriptedTransport(app_handler),
        )
        engine = V6RecoveryEngine(client)
        request = RecoveryRequest(
            owner="octo",
            repo="repo",
            repository_id=101,
            pull_request=12,
            operation_id="op-1",
            ownership_proof=_make_proof(epoch=1),
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            expected_app_id=APP_ID,
        )

        with self.assertRaises(StaleWriterOwnershipError):
            engine.recover_and_publish(request)
        self.assertTrue(all(c["method"] == "GET" for c in app_calls))

    def test_write_response_as_readback_emits_zero_extra_gets(self) -> None:
        unresolved = _make_unresolved_record()
        unresolved_comment = {
            "id": 100,
            "body": format_v6_record_marker(unresolved),
            **_app_meta(),
        }

        def wf_handler(call: dict[str, Any]) -> tuple[int, dict[str, str], Any]:
            if "/issues/12/comments" in call["path"]:
                return 200, {"X-RateLimit-Remaining": "999"}, [unresolved_comment]
            if "/pulls/12/reviews" in call["path"]:
                return 200, {"X-RateLimit-Remaining": "998"}, []
            raise AssertionError(f"unexpected workflow call {call}")

        app_transport = ScriptedTransport(
            lambda call: (
                (
                    201,
                    {"X-RateLimit-Remaining": "4999"},
                    {"id": 201, "body": call["body"]["body"]},
                )
                if call["method"] == "POST" and "/issues/12/comments" in call["path"]
                else (
                    200,
                    {"X-RateLimit-Remaining": "4998"},
                    {"id": 9001, "body": call["body"]["body"]},
                )
            )
        )
        client = DualCredentialGitHubClient(
            workflow_token_transport=ScriptedTransport(wf_handler),
            broker_app_transport=app_transport,
        )
        engine = V6RecoveryEngine(
            client,
            broker_fence_verifier=_authoritative_fence,
        )
        request = RecoveryRequest(
            owner="octo",
            repo="repo",
            repository_id=101,
            pull_request=12,
            operation_id="op-1",
            ownership_proof=_make_proof(epoch=1),
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            expected_app_id=APP_ID,
            observed_evidence_digests=("ev-digest-1",),
            artifact_cache=None,  # Artifact cache expired/deleted -> recovers from PR!
        )

        outcome = engine.recover_and_publish(request)
        self.assertIsInstance(outcome, RecoveryCompleted)
        assert isinstance(outcome, RecoveryCompleted)
        self.assertEqual(outcome.readback_gets_emitted, 0)
        self.assertEqual(outcome.model_calls_started, 0)
        self.assertTrue(outcome.used_pr_resident_result)
        self.assertEqual(outcome.github_effect_id, 9001)
        # Only 3 POSTs (intent, review, receipt); 0 readback GETs!
        self.assertEqual(
            [c["method"] for c in app_transport.calls], ["POST", "POST", "POST"]
        )

    def test_lost_timeout_write_reconciles_to_receipt_with_zero_duplicate_writes(
        self,
    ) -> None:
        unresolved = _make_unresolved_record()
        intent = WriteIntentRecord(
            record_id="intent:op-1:1",
            operation_id="op-1",
            writer_epoch=1,
            pr_head_sha=HEAD_SHA,
            effect_kind="pr_review_publish",
            payload_sha256=unresolved.payload_sha256,
            target_digest="t1",
        )
        landed_review_body = (
            "ReviewSensei recovery publication (1 finding(s)).\n"
            f"<!-- reviewsensei:v6:effect op=op-1 sha256={unresolved.payload_sha256} -->"
        )
        landed_review = {
            "id": 9002,
            "body": landed_review_body,
            **_app_meta(),
        }

        # Case 1: Inline TimeoutError during pr_review_publish reconciles via
        # pr_reviews scan + broker_effect_verification without a duplicate POST.
        review_scans = 0

        def wf_handler_inline(call: dict[str, Any]) -> tuple[int, dict[str, str], Any]:
            nonlocal review_scans
            if "/issues/12/comments" in call["path"]:
                return (
                    200,
                    {"X-RateLimit-Remaining": "999"},
                    [
                        {
                            "id": 100,
                            "body": format_v6_record_marker(unresolved),
                            **_app_meta(),
                        }
                    ],
                )
            if "/pulls/12/reviews" in call["path"]:
                review_scans += 1
                if review_scans == 1:
                    return 200, {"X-RateLimit-Remaining": "998"}, []
                return 200, {"X-RateLimit-Remaining": "997"}, [landed_review]
            raise AssertionError(call)

        def app_handler_inline(call: dict[str, Any]) -> tuple[int, dict[str, str], Any]:
            if call["method"] == "POST" and "/pulls/12/reviews" in call["path"]:
                raise TimeoutError(
                    "simulated dropped HTTP response after GitHub commit"
                )
            if call["method"] == "GET" and "/pulls/12/reviews/9002" in call["path"]:
                return 200, {"X-RateLimit-Remaining": "4998"}, landed_review
            if call["method"] == "POST" and "/issues/12/comments" in call["path"]:
                return (
                    201,
                    {"X-RateLimit-Remaining": "4997"},
                    {"id": 301, "body": call["body"]["body"]},
                )
            raise AssertionError(call)

        app_transport_inline = ScriptedTransport(app_handler_inline)
        engine_inline = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=ScriptedTransport(wf_handler_inline),
                broker_app_transport=app_transport_inline,
            ),
            broker_fence_verifier=_authoritative_fence,
        )
        req = RecoveryRequest(
            owner="octo",
            repo="repo",
            repository_id=101,
            pull_request=12,
            operation_id="op-1",
            ownership_proof=_make_proof(epoch=1),
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            expected_app_id=APP_ID,
        )
        outcome_inline = engine_inline.recover_and_publish(req)
        self.assertIsInstance(outcome_inline, RecoveryCompleted)
        assert isinstance(outcome_inline, RecoveryCompleted)
        self.assertTrue(outcome_inline.reconciled_from_existing_effect)
        self.assertEqual(outcome_inline.github_effect_id, 9002)
        self.assertEqual(outcome_inline.model_calls_started, 0)
        self.assertEqual(outcome_inline.readback_gets_emitted, 1)
        review_posts = [
            c
            for c in app_transport_inline.calls
            if c["method"] == "POST" and "/pulls/12/reviews" in c["path"]
        ]
        self.assertEqual(len(review_posts), 1)

        # Case 2: Restart recovery when an unreconciled WriteIntentRecord exists
        # and the review already landed -> 0 review POSTs, 0 model calls!
        def wf_handler_restart(call: dict[str, Any]) -> tuple[int, dict[str, str], Any]:
            if "/issues/12/comments" in call["path"]:
                return (
                    200,
                    {"X-RateLimit-Remaining": "999"},
                    [
                        {
                            "id": 100,
                            "body": format_v6_record_marker(unresolved),
                            **_app_meta(),
                        },
                        {
                            "id": 101,
                            "body": format_v6_record_marker(intent),
                            **_app_meta(),
                        },
                    ],
                )
            if "/pulls/12/reviews" in call["path"]:
                return 200, {"X-RateLimit-Remaining": "998"}, [landed_review]
            raise AssertionError(call)

        def app_handler_restart(
            call: dict[str, Any],
        ) -> tuple[int, dict[str, str], Any]:
            if call["method"] == "GET" and "/pulls/12/reviews/9002" in call["path"]:
                return 200, {"X-RateLimit-Remaining": "4999"}, landed_review
            if call["method"] == "POST" and "/issues/12/comments" in call["path"]:
                return (
                    201,
                    {"X-RateLimit-Remaining": "4998"},
                    {"id": 302, "body": call["body"]["body"]},
                )
            raise AssertionError(f"unexpected call on restart: {call}")

        app_transport_restart = ScriptedTransport(app_handler_restart)
        engine_restart = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=ScriptedTransport(wf_handler_restart),
                broker_app_transport=app_transport_restart,
            ),
            broker_fence_verifier=_authoritative_fence,
        )
        outcome_restart = engine_restart.recover_and_publish(req)
        self.assertIsInstance(outcome_restart, RecoveryCompleted)
        assert isinstance(outcome_restart, RecoveryCompleted)
        self.assertTrue(outcome_restart.reconciled_from_existing_effect)
        self.assertEqual(outcome_restart.github_effect_id, 9002)
        self.assertEqual(
            [c["method"] for c in app_transport_restart.calls], ["GET", "POST"]
        )

    def test_missing_pr_evidence_returns_hold_with_zero_model_calls(self) -> None:
        # Even if optional artifact_cache is provided, missing PR-resident result
        # or missing required_evidence_digest MUST hold with zero model calls.
        unresolved = _make_unresolved_record(required_evidence_digests=("ev-a", "ev-b"))

        def wf_empty(call: dict[str, Any]) -> tuple[int, dict[str, str], Any]:
            return 200, {"X-RateLimit-Remaining": "999"}, []

        engine = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=ScriptedTransport(wf_empty),
                broker_app_transport=ScriptedTransport(
                    lambda c: (500, {}, {"message": "must not write"})
                ),
            )
        )
        req = RecoveryRequest(
            owner="octo",
            repo="repo",
            repository_id=101,
            pull_request=12,
            operation_id="op-1",
            ownership_proof=_make_proof(),
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            artifact_cache={"findings": [{"path": "cached.py"}]},
        )
        hold1 = engine.recover_and_publish(req)
        self.assertIsInstance(hold1, RecoveryHold)
        assert isinstance(hold1, RecoveryHold)
        self.assertEqual(hold1.blocker, "missing_pr_unresolved_result")
        self.assertTrue(hold1.zero_model_calls_started)

        def wf_missing_ev(call: dict[str, Any]) -> tuple[int, dict[str, str], Any]:
            if "/issues/12/comments" in call["path"]:
                return (
                    200,
                    {"X-RateLimit-Remaining": "999"},
                    [
                        {
                            "id": 100,
                            "body": format_v6_record_marker(unresolved),
                            **_app_meta(),
                        }
                    ],
                )
            return 200, {"X-RateLimit-Remaining": "998"}, []

        engine2 = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=ScriptedTransport(wf_missing_ev),
                broker_app_transport=ScriptedTransport(
                    lambda c: (500, {}, {"message": "must not write"})
                ),
            )
        )
        req2 = RecoveryRequest(
            owner="octo",
            repo="repo",
            repository_id=101,
            pull_request=12,
            operation_id="op-1",
            ownership_proof=_make_proof(),
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            expected_app_id=APP_ID,
            observed_evidence_digests=("ev-a",),  # ev-b is missing!
        )
        hold2 = engine2.recover_and_publish(req2)
        self.assertIsInstance(hold2, RecoveryHold)
        assert isinstance(hold2, RecoveryHold)
        self.assertEqual(hold2.blocker, "missing_required_pr_evidence")
        self.assertEqual(hold2.missing_evidence, ("ev-b",))
        self.assertTrue(hold2.zero_model_calls_started)

    def test_mutation_sanity_bound_refuses_runaway_writes(self) -> None:
        unresolved = _make_unresolved_record()

        def wf_handler(call: dict[str, Any]) -> tuple[int, dict[str, str], Any]:
            if "/issues/12/comments" in call["path"]:
                return (
                    200,
                    {"X-RateLimit-Remaining": "999"},
                    [
                        {
                            "id": 100,
                            "body": format_v6_record_marker(unresolved),
                            **_app_meta(),
                        }
                    ],
                )
            return 200, {"X-RateLimit-Remaining": "998"}, []

        app_transport = ScriptedTransport(
            lambda call: (
                201,
                {"X-RateLimit-Remaining": "4999"},
                {"id": 1, "body": call["body"]["body"]},
            )
        )
        engine = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=ScriptedTransport(wf_handler),
                broker_app_transport=app_transport,
            ),
            mutation_bound=MutationSanityBound(max_writes=2),
            broker_fence_verifier=_authoritative_fence,
        )
        req = RecoveryRequest(
            owner="octo",
            repo="repo",
            repository_id=101,
            pull_request=12,
            operation_id="op-1",
            ownership_proof=_make_proof(),
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            expected_app_id=APP_ID,
        )
        # Needs 3 writes (intent, review, receipt) -> blocked before 3rd write!
        with self.assertRaises(MutationSanityBoundError):
            engine.recover_and_publish(req)
        self.assertEqual(len(app_transport.calls), 2)

    def test_throttle_short_sleep_long_pause_and_permission_fail_closed(self) -> None:
        unresolved = _make_unresolved_record()
        comment_calls = 0

        def wf_short_wait(call: dict[str, Any]) -> tuple[int, dict[str, str], Any]:
            nonlocal comment_calls
            if "/issues/12/comments" in call["path"]:
                comment_calls += 1
                if comment_calls == 1:
                    return (
                        429,
                        {"Retry-After": "12", "X-RateLimit-Remaining": "0"},
                        {"message": "secondary rate limit"},
                    )
                return (
                    200,
                    {"X-RateLimit-Remaining": "999"},
                    [
                        {
                            "id": 100,
                            "body": format_v6_record_marker(unresolved),
                            **_app_meta(),
                        }
                    ],
                )
            return 200, {"X-RateLimit-Remaining": "998"}, []

        slept: list[float] = []
        engine_sleep = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=ScriptedTransport(wf_short_wait),
                broker_app_transport=ScriptedTransport(
                    lambda c: (
                        201,
                        {"X-RateLimit-Remaining": "4999"},
                        {"id": 5, "body": c["body"]["body"]},
                    )
                ),
            ),
            sleeper=slept.append,
            max_in_job_sleep_seconds=60.0,
            remaining_job_seconds=600.0,
            broker_fence_verifier=_authoritative_fence,
        )
        req = RecoveryRequest(
            owner="octo",
            repo="repo",
            repository_id=101,
            pull_request=12,
            operation_id="op-1",
            ownership_proof=_make_proof(),
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            expected_app_id=APP_ID,
            advisory_comment_id=99,
        )
        completed = engine_sleep.recover_and_publish(req)
        self.assertIsInstance(completed, RecoveryCompleted)
        self.assertEqual(slept, [12.0])

        # Long wait where saving advisory hint is also rate-limited -> reuses prior state.
        def wf_long_wait(call: dict[str, Any]) -> tuple[int, dict[str, str], Any]:
            return (
                403,
                {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "2000"},
                {"message": "API rate limit exceeded"},
            )

        def app_throttled(call: dict[str, Any]) -> tuple[int, dict[str, str], Any]:
            return (
                429,
                {"Retry-After": "900"},
                {"message": "secondary rate limit"},
            )

        engine_pause = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=ScriptedTransport(wf_long_wait),
                broker_app_transport=ScriptedTransport(app_throttled),
            ),
            now_fn=lambda: 1000.0,
            max_in_job_sleep_seconds=300.0,
            remaining_job_seconds=1800.0,
        )
        paused = engine_pause.recover_and_publish(req)
        self.assertIsInstance(paused, RecoveryPaused)
        assert isinstance(paused, RecoveryPaused)
        self.assertEqual(paused.rerun_command, MAINTAINER_RERUN_COMMAND)
        self.assertEqual(paused.resume_after_epoch, 2000.0)
        self.assertTrue(paused.reused_prior_durable_state)

        # Permission 403 fails closed immediately without retry or App call.
        app_perm_transport = ScriptedTransport(
            lambda c: (500, {}, {"message": "must never fallback"})
        )
        wf_perm_transport = ScriptedTransport(
            lambda c: (
                403,
                {"X-RateLimit-Remaining": "999"},
                {"message": "Resource not accessible by integration"},
            )
        )
        engine_perm = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=wf_perm_transport,
                broker_app_transport=app_perm_transport,
            )
        )
        with self.assertRaises(GitHubHTTPPermissionError):
            engine_perm.recover_and_publish(req)
        self.assertEqual(len(wf_perm_transport.calls), 1)
        self.assertEqual(len(app_perm_transport.calls), 0)

    def test_gap10_native_tail_calls_diagnostic_includes_all_fields(self) -> None:
        class StubBudget:
            def ordinary_remaining(self) -> int:
                return 10

            def preflight(self, needed: int) -> None:
                raise EvidenceBudgetExhausted(
                    f"need {needed} calls but only 10 remaining"
                )

        tail_breakdown = {
            "native_tail_calls": {
                "reads": {"reviews": 2, "threads": 1},
                "capability_mints": 1,
                "review_update": 3,
                "cleanup_threads": 2,
                "check_run": 2,
                "approval": 0,
                "other_writes": 4,
                "restore_and_snapshot_reads": 5,
                "body_receipt_calls": 1,
                "total": 20,
            },
            "receipt_calls": 6,
        }

        with self.assertRaises(EvidenceBudgetExhausted) as caught:
            _preflight_with_breakdown(
                StubBudget(),
                ledger=object(),
                checkpoints=[{"conditional_calls": 7}, {"conditional_calls": 4}],
                has_projected_root=True,
                calls=11,
                reserved_tail_calls=26,
                tail_breakdown=tail_breakdown,
                caller_allocation={
                    "limit": 512,
                    "startup": 130,
                    "first_work": 141,
                    "terminal": 50,
                    "successor": 191,
                },
            )

        msg = str(caught.exception)
        self.assertIn("preflight caller-budget funding refusal", msg)
        self.assertIn(
            "native_tail_calls=20("
            "reads=3,capability_mints=1,review_update=3,cleanup_threads=2,"
            "check_run=2,approval=0,other_writes=4,"
            "restore_and_snapshot_reads=5,body_receipt_calls=1)",
            msg,
        )

    def test_repeated_rest_pagination_url_halts_immediately_without_loop(self) -> None:
        calls_count = 0

        def wf_loop(call: dict[str, Any]) -> tuple[int, dict[str, str], Any]:
            nonlocal calls_count
            calls_count += 1
            return (
                200,
                {
                    "X-RateLimit-Remaining": "999",
                    "Link": '<https://api.github.com/repos/octo/repo/issues/12/comments?per_page=100&sort=created&direction=asc>; rel="next"',
                },
                [{"id": 1, "body": "only-comment"}],
            )

        paginator = StabilityVerifiedPaginator(
            DualCredentialGitHubClient(
                workflow_token_transport=ScriptedTransport(wf_loop),
                broker_app_transport=ScriptedTransport(
                    lambda c: (500, {}, {"message": "unused"})
                ),
            )
        )
        guard = NoProgressGuard(operation_id="op-repeat-url", max_stall_steps=5)
        with self.assertRaises(RecoveryStalledError):
            paginator.scan_rest(
                endpoint_id="pr_issue_comments",
                path="/repos/octo/repo/issues/12/comments?per_page=100",
                event=GitHubEventKind.PULL_REQUEST,
                visibility=RepositoryVisibility.PUBLIC,
                granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
                progress_guard=guard,
            )
        self.assertEqual(calls_count, 1)

    def test_fail_closed_bot_provenance_and_strict_marker_field_types(self) -> None:
        # Generic Bot user without performed_via_github_app or expected bot identity is rejected.
        generic_bot_meta = {
            "performed_via_github_app": None,
            "user": {"id": 9999, "login": "some-other[bot]", "type": "Bot"},
        }
        self.assertFalse(verify_app_authored_record(generic_bot_meta))
        self.assertTrue(
            verify_app_authored_record(
                generic_bot_meta, expected_bot_login="some-other[bot]"
            )
        )
        self.assertFalse(
            verify_app_authored_record(generic_bot_meta, expected_bot_login=BOT_LOGIN)
        )

        # Malformed field types in markers raise V6LedgerIntegrityError fail-closed.
        bad_payloads: list[dict[str, Any]] = [
            {
                "schema": INTENT_SCHEMA,
                "record_id": "intent:1",
                "operation_id": "op-1",
                "writer_epoch": "1",
                "pr_head_sha": HEAD_SHA,
                "effect_kind": "pr_review_publish",
                "payload_sha256": "a" * 64,
                "target_digest": "t1",
            },
            {
                "schema": PROGRESS_HINT_SCHEMA,
                "operation_id": "op-1",
                "recovery_phase": "scan",
                "cursor_hint": None,
                "verified_prefix_count": True,
                "verified_prefix_digest": "",
                "last_verified_id": None,
                "stall_count": 0,
                "last_progress_digest": "",
                "transient_error_count": 0,
            },
            {
                "schema": PROGRESS_HINT_SCHEMA,
                "operation_id": "op-1",
                "recovery_phase": "scan",
                "cursor_hint": "c1",
                "verified_prefix_count": 1,
                "verified_prefix_digest": "not-a-sha256-hex-digest",
                "last_verified_id": 1,
                "stall_count": 0,
                "last_progress_digest": "",
                "transient_error_count": 0,
            },
            {
                "schema": UNRESOLVED_RESULT_SCHEMA,
                "record_id": "unresolved:op-1",
                "operation_id": "op-1",
                "pr_head_sha": HEAD_SHA,
                "payload_sha256": canonical_sha256([]),
                "findings_payload": [],
                "required_evidence_digests": "abc",
                "artifact_cache_sha256": None,
            },
        ]
        for bad in bad_payloads:
            bad_marker = (
                f"<!-- reviewsensei:v6:record sha256={canonical_sha256(bad)} "
                f"payload={json.dumps(bad)} -->"
            )
            with self.assertRaises(V6LedgerIntegrityError):
                parse_v6_record_marker(bad_marker)

        with self.assertRaises(V6LedgerIntegrityError):
            canonical_sha256({"bad": float("nan")})

    def test_intent_digest_mismatch_and_ambiguous_broker_200_both_hold(self) -> None:
        unresolved = _make_unresolved_record()
        mismatched_intent = WriteIntentRecord(
            record_id="intent:op-1:1",
            operation_id="op-1",
            writer_epoch=1,
            pr_head_sha=HEAD_SHA,
            effect_kind="pr_review_publish",
            payload_sha256="f" * 64,
            target_digest="t1",
        )

        def wf_mismatch(call: dict[str, Any]) -> tuple[int, dict[str, str], Any]:
            if "/issues/12/comments" in call["path"]:
                return (
                    200,
                    {"X-RateLimit-Remaining": "999"},
                    [
                        {
                            "id": 100,
                            "body": format_v6_record_marker(unresolved),
                            **_app_meta(),
                        },
                        {
                            "id": 101,
                            "body": format_v6_record_marker(mismatched_intent),
                            **_app_meta(),
                        },
                    ],
                )
            return 200, {"X-RateLimit-Remaining": "998"}, []

        app_never = ScriptedTransport(
            lambda c: (500, {}, {"message": "must not mutate on mismatch"})
        )
        req = RecoveryRequest(
            owner="octo",
            repo="repo",
            repository_id=101,
            pull_request=12,
            operation_id="op-1",
            ownership_proof=_make_proof(epoch=1),
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            expected_app_id=APP_ID,
        )
        hold_mismatch = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=ScriptedTransport(wf_mismatch),
                broker_app_transport=app_never,
            )
        ).recover_and_publish(req)
        self.assertIsInstance(hold_mismatch, RecoveryHold)
        assert isinstance(hold_mismatch, RecoveryHold)
        self.assertEqual(hold_mismatch.blocker, "pr_result_digest_mismatch_with_intent")
        self.assertTrue(hold_mismatch.zero_model_calls_started)
        self.assertEqual(len(app_never.calls), 0)

        # RS-2D226A: broker_effect_verification returning 200 with mismatched payload
        # must return RecoveryHold(ambiguous_write_outcome) rather than duplicate write.
        matching_intent = WriteIntentRecord(
            record_id="intent:op-1:1",
            operation_id="op-1",
            writer_epoch=1,
            pr_head_sha=HEAD_SHA,
            effect_kind="pr_review_publish",
            payload_sha256=unresolved.payload_sha256,
            target_digest="t1",
        )
        candidate_review = {
            "id": 9003,
            "body": (
                "ReviewSensei recovery publication (1 finding(s)).\n"
                f"<!-- reviewsensei:v6:effect op=op-1 sha256={unresolved.payload_sha256} -->"
            ),
            **_app_meta(),
        }

        def wf_candidate(call: dict[str, Any]) -> tuple[int, dict[str, str], Any]:
            if "/issues/12/comments" in call["path"]:
                return (
                    200,
                    {"X-RateLimit-Remaining": "999"},
                    [
                        {
                            "id": 100,
                            "body": format_v6_record_marker(unresolved),
                            **_app_meta(),
                        },
                        {
                            "id": 101,
                            "body": format_v6_record_marker(matching_intent),
                            **_app_meta(),
                        },
                    ],
                )
            return 200, {"X-RateLimit-Remaining": "998"}, [candidate_review]

        def app_ambiguous(call: dict[str, Any]) -> tuple[int, dict[str, str], Any]:
            if call["method"] == "GET" and "/pulls/12/reviews/9003" in call["path"]:
                return (
                    200,
                    {"X-RateLimit-Remaining": "4999"},
                    {"id": 9003, "body": "different unverified body", **_app_meta()},
                )
            raise AssertionError(f"must not post duplicate review: {call}")

        app_ambig_transport = ScriptedTransport(app_ambiguous)
        hold_ambig = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=ScriptedTransport(wf_candidate),
                broker_app_transport=app_ambig_transport,
            )
        ).recover_and_publish(req)
        self.assertIsInstance(hold_ambig, RecoveryHold)
        assert isinstance(hold_ambig, RecoveryHold)
        self.assertEqual(hold_ambig.blocker, "ambiguous_write_outcome")
        self.assertTrue(hold_ambig.zero_model_calls_started)
        self.assertEqual([c["method"] for c in app_ambig_transport.calls], ["GET"])

    def test_verify_writer_ownership_fails_closed_on_missing_or_unverified_fields(
        self,
    ) -> None:
        proof = _make_proof(epoch=1)
        # Valid authoritative call succeeds.
        verify_writer_ownership(
            proof,
            live_repository_id=101,
            live_pull_request=12,
            live_pr_head_sha=HEAD_SHA,
            active_writer_epoch=1,
            pr_state="open",
            active_run_id=5001,
            active_run_attempt=1,
            run_status="in_progress",
            verified_by_broker=True,
        )

        invalid_kwargs: list[tuple[str, dict[str, Any]]] = [
            ("missing_active_run_id", {"active_run_id": None}),
            ("missing_active_run_attempt", {"active_run_attempt": None}),
            ("missing_run_status", {"run_status": None}),
            ("completed_run_status", {"run_status": "completed"}),
            ("queued_run_status", {"run_status": "queued"}),
            ("missing_verified_by_broker", {"verified_by_broker": None}),
            ("false_verified_by_broker", {"verified_by_broker": False}),
            ("empty_pr_state", {"pr_state": ""}),
            ("closed_pr_state", {"pr_state": "closed"}),
        ]
        for label, overrides in invalid_kwargs:
            with self.subTest(case=label):
                kwargs: dict[str, Any] = {
                    "live_repository_id": 101,
                    "live_pull_request": 12,
                    "live_pr_head_sha": HEAD_SHA,
                    "active_writer_epoch": 1,
                    "pr_state": "open",
                    "active_run_id": 5001,
                    "active_run_attempt": 1,
                    "run_status": "in_progress",
                    "verified_by_broker": True,
                }
                kwargs.update(overrides)
                with self.assertRaises(StaleWriterOwnershipError):
                    verify_writer_ownership(proof, **kwargs)

    def test_write_boundary_fails_closed_when_authoritative_fields_missing_or_non_200(
        self,
    ) -> None:
        unresolved = _make_unresolved_record()
        unresolved_comment = {
            "id": 100,
            "body": format_v6_record_marker(unresolved),
            **_app_meta(),
        }

        def wf_handler(call: dict[str, Any]) -> tuple[int, dict[str, str], Any]:
            if "/issues/12/comments" in call["path"]:
                return 200, {"X-RateLimit-Remaining": "999"}, [unresolved_comment]
            if "/pulls/12/reviews" in call["path"]:
                return 200, {"X-RateLimit-Remaining": "998"}, []
            raise AssertionError(f"unexpected workflow call {call}")

        valid_pr: dict[str, Any] = {
            "state": "open",
            "head": {"sha": HEAD_SHA},
            "base": {"repo": {"id": 101}},
        }
        valid_run: dict[str, Any] = {
            "id": 5001,
            "run_attempt": 1,
            "status": "in_progress",
            "writer_epoch": 1,
            "verified_by_broker": True,
        }

        cases: list[tuple[str, tuple[int, Any], tuple[int, Any]]] = [
            (
                "missing_writer_epoch",
                (200, valid_pr),
                (200, {k: v for k, v in valid_run.items() if k != "writer_epoch"}),
            ),
            (
                "missing_active_run_id",
                (200, valid_pr),
                (200, {k: v for k, v in valid_run.items() if k != "id"}),
            ),
            (
                "missing_run_attempt",
                (200, valid_pr),
                (200, {k: v for k, v in valid_run.items() if k != "run_attempt"}),
            ),
            (
                "missing_run_status",
                (200, valid_pr),
                (200, {k: v for k, v in valid_run.items() if k != "status"}),
            ),
            (
                "non_in_progress_run_status_completed",
                (200, valid_pr),
                (200, {**valid_run, "status": "completed"}),
            ),
            (
                "non_in_progress_run_status_queued",
                (200, valid_pr),
                (200, {**valid_run, "status": "queued"}),
            ),
            (
                "verified_by_broker_false",
                (200, valid_pr),
                (200, {**valid_run, "verified_by_broker": False}),
            ),
            (
                "invalid_authoritative_writer_epoch",
                (200, valid_pr),
                (200, {**valid_run, "authoritative_writer_epoch": None}),
            ),
            (
                "missing_base_repo_id",
                (200, {**valid_pr, "base": {"repo": {}}}),
                (200, valid_run),
            ),
            (
                "missing_pr_state",
                (200, {k: v for k, v in valid_pr.items() if k != "state"}),
                (200, valid_run),
            ),
            (
                "invalid_head_sha",
                (200, {**valid_pr, "head": {"sha": "not-a-sha"}}),
                (200, valid_run),
            ),
            ("non_200_pr_lookup", (404, {"message": "Not Found"}), (200, valid_run)),
            ("non_200_run_lookup", (200, valid_pr), (500, {"message": "Error"})),
            ("non_mapping_pr_body", (200, ["bad"]), (200, valid_run)),
            ("non_mapping_run_body", (200, valid_pr), (200, "bad")),
        ]

        req = RecoveryRequest(
            owner="octo",
            repo="repo",
            repository_id=101,
            pull_request=12,
            operation_id="op-1",
            ownership_proof=_make_proof(epoch=1),
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            expected_app_id=APP_ID,
        )

        for label, pr_outcome, run_outcome in cases:
            with self.subTest(case=label):
                app_calls: list[dict[str, Any]] = []

                def app_handler(
                    call: dict[str, Any],
                    *,
                    pr_res: tuple[int, Any] = pr_outcome,
                    run_res: tuple[int, Any] = run_outcome,
                    recorded: list[dict[str, Any]] = app_calls,
                ) -> tuple[int, dict[str, str], Any]:
                    recorded.append(call)
                    if (
                        call["method"] == "GET"
                        and call["path"] == "/repos/octo/repo/pulls/12"
                    ):
                        return pr_res[0], {"X-RateLimit-Remaining": "4999"}, pr_res[1]
                    if (
                        call["method"] == "GET"
                        and call["path"] == "/repos/octo/repo/actions/runs/5001"
                    ):
                        return run_res[0], {"X-RateLimit-Remaining": "4998"}, run_res[1]
                    raise AssertionError(
                        f"fail-closed fence must emit 0 writes: {call}"
                    )

                engine = V6RecoveryEngine(
                    DualCredentialGitHubClient(
                        workflow_token_transport=ScriptedTransport(wf_handler),
                        broker_app_transport=ScriptedTransport(app_handler),
                    )
                )
                with self.assertRaises(StaleWriterOwnershipError):
                    engine.recover_and_publish(req)
                self.assertTrue(all(c["method"] == "GET" for c in app_calls))

        # Also prove broker_fence_verifier fails closed if any authoritative field is omitted.
        base_fence = _authoritative_fence(_make_proof(epoch=1))
        for missing_key in (
            "repository_id",
            "pull_request",
            "pr_state",
            "active_run_id",
            "active_run_attempt",
            "run_status",
            "verified_by_broker",
        ):
            with self.subTest(fence_missing=missing_key):
                app_never = ScriptedTransport(
                    lambda call: (500, {}, {"message": f"must not mutate: {call}"})
                )
                incomplete_fence = {
                    k: v for k, v in base_fence.items() if k != missing_key
                }

                def _return_incomplete(
                    _proof: WriterOwnershipProof,
                    *,
                    payload: Mapping[str, Any] = incomplete_fence,
                ) -> Mapping[str, Any]:
                    return payload

                engine_fence = V6RecoveryEngine(
                    DualCredentialGitHubClient(
                        workflow_token_transport=ScriptedTransport(wf_handler),
                        broker_app_transport=app_never,
                    ),
                    broker_fence_verifier=_return_incomplete,
                )
                with self.assertRaises(StaleWriterOwnershipError):
                    engine_fence.recover_and_publish(req)
                self.assertEqual(app_never.calls, [])
