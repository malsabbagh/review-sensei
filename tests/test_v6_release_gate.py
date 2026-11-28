"""Phase 4 v6 Release-Gate Qualification Suite.

Proves all 8 mandatory ReviewSensei v6 release gates end-to-end against
simulated GitHub PR surfaces without external databases or artifact reliance:

1. Cold recovery past 75-request and 512-request boundaries with 0 App bulk reads.
2. Comment deletion before a saved anchor triggers rescan + deterministic dedup.
3. Persistent no-progress loop guard halts early across restarts with quota left.
4. Throttling on both credentials, GraphQL HTTP 200 RATE_LIMITED, and 403 isolation.
5. Stale/unverified writer rejection preserving immutable intents and receipts.
6. Lost write response after GitHub commit reconciles with 0 duplicate reviews/model calls.
7. Artifact-free restarts and missing-PR-evidence fail-closed RecoveryHold.
8. Cloudflare wrangler.jsonc keeps PUBLIC_WORKFLOW_TAG pinned to ``v5``.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from review_sensei_github.errors import GitHubHTTPPermissionError
from review_sensei_github.v6_credential_matrix import (
    WORKFLOW_READ_ONLY_SCOPES,
    CredentialKind,
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
    RecoveryCompleted,
    RecoveryHold,
    RecoveryPaused,
    RecoveryRequest,
    StaleWriterOwnershipError,
    V6RecoveryEngine,
)
from review_sensei_github.v6_recovery_ledger import (
    AdvisoryProgressHint,
    CompletionReceiptRecord,
    UnresolvedResultRecord,
    WriteIntentRecord,
    WriterOwnershipProof,
    canonical_sha256,
    extract_v6_record_markers,
    format_v6_record_marker,
    reconcile_immutable_records,
)
from review_sensei_github.v6_transport import (
    MAINTAINER_RERUN_COMMAND,
    DualCredentialGitHubClient,
    classify_github_response,
)

ROOT = Path(__file__).resolve().parents[1]
OWNER = "octo"
REPO = "repo"
REPO_ID = 101
PR_NUMBER = 12
HEAD_SHA = "a" * 40
APP_ID = 424242
BOT_LOGIN = "reviewsensei[bot]"
BOT_USER_ID = 9001

TransportInterceptor = Callable[
    [dict[str, Any], Callable[[], tuple[int, Mapping[str, str], Any]]],
    tuple[int, Mapping[str, str], Any],
]


def _app_meta(*, app_id: int = APP_ID) -> dict[str, Any]:
    return {
        "performed_via_github_app": {"id": app_id, "slug": "reviewsensei"},
        "user": {"id": BOT_USER_ID, "login": BOT_LOGIN, "type": "Bot"},
    }


def _human_meta(*, user_id: int = 77, login: str = "contributor") -> dict[str, Any]:
    return {
        "performed_via_github_app": None,
        "user": {"id": user_id, "login": login, "type": "User"},
    }


def _make_proof(
    *,
    operation_id: str = "op-1",
    epoch: int = 1,
    run_id: int = 5001,
    run_attempt: int = 1,
    head_sha: str = HEAD_SHA,
    verified_by_broker: bool = True,
) -> WriterOwnershipProof:
    return WriterOwnershipProof(
        repository_id=REPO_ID,
        pull_request=PR_NUMBER,
        operation_id=operation_id,
        run_id=run_id,
        run_attempt=run_attempt,
        writer_epoch=epoch,
        pr_head_sha=head_sha,
        verified_by_broker=verified_by_broker,
    )


def _make_unresolved_record(
    *,
    operation_id: str = "op-1",
    head_sha: str = HEAD_SHA,
    findings: tuple[dict[str, Any], ...] = (
        {"path": "src/app.py", "line": 10, "body": "Check bounds"},
    ),
    required_evidence_digests: tuple[str, ...] = ("ev-diff-1",),
    artifact_cache_sha256: str | None = None,
) -> UnresolvedResultRecord:
    return UnresolvedResultRecord(
        record_id=f"unresolved:{operation_id}",
        operation_id=operation_id,
        pr_head_sha=head_sha,
        payload_sha256=canonical_sha256(findings),
        findings_payload=findings,
        required_evidence_digests=required_evidence_digests,
        artifact_cache_sha256=artifact_cache_sha256,
    )


def _make_request(
    *,
    operation_id: str = "op-1",
    proof: WriterOwnershipProof | None = None,
    event: GitHubEventKind = GitHubEventKind.PULL_REQUEST,
    visibility: RepositoryVisibility = RepositoryVisibility.PUBLIC,
    advisory_comment_id: int | None = None,
    observed_evidence_digests: tuple[str, ...] | None = ("ev-diff-1",),
    artifact_cache: Mapping[str, Any] | None = None,
    graphql_query: str | None = None,
    graphql_stability_probe: Mapping[str, Any] | None = None,
) -> RecoveryRequest:
    effective_proof = _make_proof(operation_id=operation_id) if proof is None else proof
    return RecoveryRequest(
        owner=OWNER,
        repo=REPO,
        repository_id=REPO_ID,
        pull_request=PR_NUMBER,
        operation_id=operation_id,
        ownership_proof=effective_proof,
        event=event,
        visibility=visibility,
        granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
        expected_app_id=APP_ID,
        expected_bot_login=BOT_LOGIN,
        expected_bot_user_id=BOT_USER_ID,
        advisory_comment_id=advisory_comment_id,
        observed_evidence_digests=observed_evidence_digests,
        artifact_cache=artifact_cache,
        graphql_query=graphql_query,
        graphql_stability_probe=graphql_stability_probe,
    )


def _is_bulk_read_call(call: Mapping[str, Any]) -> bool:
    method = str(call.get("method", "")).upper()
    path = urlsplit(str(call.get("path", ""))).path
    if method == "POST" and path == "/graphql":
        return True
    if method != "GET":
        return False
    return (
        path.endswith(f"/pulls/{PR_NUMBER}/reviews")
        or path.endswith(f"/issues/{PR_NUMBER}/comments")
        or path.endswith(f"/pulls/{PR_NUMBER}/comments")
        or path.endswith(f"/pulls/{PR_NUMBER}/files")
        or path.endswith(f"/pulls/{PR_NUMBER}/commits")
        or "/check-runs" in path
        or "/check-suites" in path
    )


class _SimulatedPRTransport:
    """Deterministic DualCredentialTransport wrapper with optional fault injection."""

    def __init__(
        self,
        default_handler: Callable[[dict[str, Any]], tuple[int, Mapping[str, str], Any]],
        *,
        interceptor: TransportInterceptor | None = None,
    ) -> None:
        self._default_handler = default_handler
        self._interceptor = interceptor
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
        call: dict[str, Any] = {
            "method": method,
            "path": path,
            "headers": dict(headers or {}),
            "body": body,
            "timeout_seconds": timeout_seconds,
        }
        self.calls.append(call)
        if self._interceptor is not None:
            return self._interceptor(call, lambda: self._default_handler(call))
        return self._default_handler(call)


class SimulatedGitHubPR:
    """In-memory GitHub pull-request surface enforcing PR-as-sole-ledger semantics."""

    def __init__(
        self,
        *,
        page_size: int = 100,
        workflow_limit: int = 1000,
        app_limit: int = 5000,
        active_writer_epoch: int = 1,
        active_run_id: int = 5001,
        active_run_attempt: int = 1,
    ) -> None:
        self.page_size = page_size
        self.workflow_remaining = workflow_limit
        self.app_remaining = app_limit
        self.pr_state = "open"
        self.head_sha = HEAD_SHA
        self.active_writer_epoch = active_writer_epoch
        self.active_run_id = active_run_id
        self.active_run_attempt = active_run_attempt
        self.run_status = "in_progress"
        self.comments: list[dict[str, Any]] = []
        self.reviews: list[dict[str, Any]] = []
        self._next_comment_id = 100
        self._next_review_id = 9001

    def add_comment(
        self,
        body: str,
        *,
        is_app: bool = False,
        comment_id: int | None = None,
    ) -> dict[str, Any]:
        assigned_id = self._next_comment_id if comment_id is None else int(comment_id)
        self._next_comment_id = max(self._next_comment_id, assigned_id + 1)
        meta = _app_meta() if is_app else _human_meta()
        comment: dict[str, Any] = {"id": assigned_id, "body": body, **meta}
        self.comments.append(comment)
        return comment

    def delete_comment(self, comment_id: int) -> None:
        self.comments = [c for c in self.comments if c["id"] != comment_id]

    def get_comment(self, comment_id: int) -> dict[str, Any]:
        for comment in self.comments:
            if comment["id"] == comment_id:
                return dict(comment)
        raise KeyError(f"comment {comment_id} not found")

    def collect_ledger_records(self) -> list[Any]:
        records: list[Any] = []
        for comment in self.comments:
            body = comment.get("body")
            if isinstance(body, str) and "<!-- reviewsensei:v6:" in body:
                records.extend(extract_v6_record_markers(body, strict=False))
        return records

    def _wf_headers(
        self,
        *,
        base_path: str,
        page: int,
        per_page: int,
        total_items: int,
    ) -> dict[str, str]:
        self.workflow_remaining = max(0, self.workflow_remaining - 1)
        headers: dict[str, str] = {
            "X-RateLimit-Remaining": str(self.workflow_remaining),
            "X-RateLimit-Reset": "1764200000",
        }
        if page * per_page < total_items:
            headers["Link"] = (
                f"<https://api.github.com{base_path}"
                f"?page={page + 1}&per_page={per_page}&sort=created&direction=asc>; "
                'rel="next"'
            )
        return headers

    def _app_headers(self) -> dict[str, str]:
        self.app_remaining = max(0, self.app_remaining - 1)
        return {
            "X-RateLimit-Remaining": str(self.app_remaining),
            "X-RateLimit-Reset": "1764200000",
        }

    def handle_workflow_request(
        self, call: dict[str, Any]
    ) -> tuple[int, Mapping[str, str], Any]:
        method = call["method"]
        parsed = urlsplit(call["path"])
        qs = parse_qs(parsed.query)
        page = int(qs["page"][0]) if "page" in qs else 1
        per_page = (
            min(self.page_size, int(qs["per_page"][0]))
            if "per_page" in qs
            else self.page_size
        )

        if (
            method == "GET"
            and parsed.path == f"/repos/{OWNER}/{REPO}/issues/{PR_NUMBER}/comments"
        ):
            start = (page - 1) * per_page
            slice_items = [
                dict(item) for item in self.comments[start : start + per_page]
            ]
            headers = self._wf_headers(
                base_path=parsed.path,
                page=page,
                per_page=per_page,
                total_items=len(self.comments),
            )
            return 200, headers, slice_items

        if (
            method == "GET"
            and parsed.path == f"/repos/{OWNER}/{REPO}/pulls/{PR_NUMBER}/reviews"
        ):
            start = (page - 1) * per_page
            slice_items = [
                dict(item) for item in self.reviews[start : start + per_page]
            ]
            headers = self._wf_headers(
                base_path=parsed.path,
                page=page,
                per_page=per_page,
                total_items=len(self.reviews),
            )
            return 200, headers, slice_items

        if method == "POST" and parsed.path == "/graphql":
            self.workflow_remaining = max(0, self.workflow_remaining - 1)
            variables = (call.get("body") or {}).get("variables") or {}
            after = variables.get("after")
            start = 0
            if isinstance(after, str) and after.startswith("cursor:"):
                start = int(after.split(":", 1)[1])
            batch = self.comments[start : start + self.page_size]
            end_idx = start + len(batch)
            has_next = end_idx < len(self.comments)
            end_cursor = f"cursor:{end_idx}" if has_next else None
            nodes = [
                {
                    **item,
                    "id": f"node-{item['id']}",
                    "databaseId": item["id"],
                }
                for item in batch
            ]
            return (
                200,
                {"X-RateLimit-Remaining": str(self.workflow_remaining)},
                {
                    "data": {
                        "connection": {
                            "nodes": nodes,
                            "pageInfo": {
                                "hasNextPage": has_next,
                                "endCursor": end_cursor,
                            },
                        }
                    }
                },
            )

        raise AssertionError(f"unexpected workflow_token call: {call!r}")

    def handle_broker_request(
        self, call: dict[str, Any]
    ) -> tuple[int, Mapping[str, str], Any]:
        method = call["method"]
        parsed = urlsplit(call["path"])
        body = call.get("body") or {}

        if (
            method == "GET"
            and parsed.path == f"/repos/{OWNER}/{REPO}/pulls/{PR_NUMBER}"
        ):
            return (
                200,
                self._app_headers(),
                {
                    "state": self.pr_state,
                    "head": {"sha": self.head_sha},
                    "base": {"repo": {"id": REPO_ID}},
                },
            )

        if method == "GET" and parsed.path.startswith(
            f"/repos/{OWNER}/{REPO}/actions/runs/"
        ):
            return (
                200,
                self._app_headers(),
                {
                    "id": self.active_run_id,
                    "run_attempt": self.active_run_attempt,
                    "status": self.run_status,
                    "writer_epoch": self.active_writer_epoch,
                },
            )

        if (
            method == "POST"
            and parsed.path == f"/repos/{OWNER}/{REPO}/issues/{PR_NUMBER}/comments"
        ):
            created = self.add_comment(str(body["body"]), is_app=True)
            return 201, self._app_headers(), dict(created)

        if method == "PATCH" and parsed.path.startswith(
            f"/repos/{OWNER}/{REPO}/issues/{PR_NUMBER}/comments/"
        ):
            target_id = int(parsed.path.rsplit("/", 1)[-1])
            for comment in self.comments:
                if comment["id"] == target_id:
                    comment["body"] = str(body["body"])
                    return 200, self._app_headers(), dict(comment)
            return 404, self._app_headers(), {"message": "Not Found"}

        if (
            method == "POST"
            and parsed.path == f"/repos/{OWNER}/{REPO}/pulls/{PR_NUMBER}/reviews"
        ):
            review_id = self._next_review_id
            self._next_review_id += 1
            review: dict[str, Any] = {
                "id": review_id,
                "body": str(body["body"]),
                "commit_id": body.get("commit_id"),
                "event": body.get("event"),
                **_app_meta(),
            }
            self.reviews.append(review)
            return 201, self._app_headers(), dict(review)

        if method == "GET" and parsed.path.startswith(
            f"/repos/{OWNER}/{REPO}/pulls/{PR_NUMBER}/reviews/"
        ):
            target_id = int(parsed.path.rsplit("/", 1)[-1])
            for review in self.reviews:
                if review["id"] == target_id:
                    return 200, self._app_headers(), dict(review)
            return 404, self._app_headers(), {"message": "Not Found"}

        raise AssertionError(f"unexpected broker_app call: {call!r}")


def _build_client(
    pr: SimulatedGitHubPR,
    *,
    workflow_interceptor: TransportInterceptor | None = None,
    broker_interceptor: TransportInterceptor | None = None,
    is_ghec: bool = False,
) -> tuple[DualCredentialGitHubClient, _SimulatedPRTransport, _SimulatedPRTransport]:
    wf_transport = _SimulatedPRTransport(
        pr.handle_workflow_request, interceptor=workflow_interceptor
    )
    app_transport = _SimulatedPRTransport(
        pr.handle_broker_request, interceptor=broker_interceptor
    )
    client = DualCredentialGitHubClient(
        workflow_token_transport=wf_transport,
        broker_app_transport=app_transport,
        is_ghec=is_ghec,
    )
    return client, wf_transport, app_transport


class V6ReleaseGateTests(unittest.TestCase):
    """Qualification tests proving all 8 mandatory ReviewSensei v6 release gates."""

    def test_gate_1_cold_recovery_past_75_and_512_requests_without_app_bulk_reads(
        self,
    ) -> None:
        for comment_pages, is_ghec in ((120, False), (530, True)):
            with self.subTest(comment_pages=comment_pages, is_ghec=is_ghec):
                pr = SimulatedGitHubPR(
                    page_size=1,
                    workflow_limit=15000 if is_ghec else 1000,
                )
                for idx in range(comment_pages - 1):
                    pr.add_comment(f"history comment {idx}", is_app=False)
                unresolved = _make_unresolved_record(operation_id=f"op-{comment_pages}")
                pr.add_comment(format_v6_record_marker(unresolved), is_app=True)

                client, wf_transport, app_transport = _build_client(pr, is_ghec=is_ghec)
                engine = V6RecoveryEngine(client)
                outcome = engine.recover_and_publish(
                    _make_request(operation_id=f"op-{comment_pages}")
                )

                self.assertIsInstance(outcome, RecoveryCompleted)
                assert isinstance(outcome, RecoveryCompleted)
                self.assertEqual(outcome.model_calls_started, 0)
                self.assertTrue(outcome.used_pr_resident_result)
                self.assertEqual(len(pr.reviews), 1)
                self.assertEqual(
                    client.workflow_quota.snapshot().observed_requests,
                    comment_pages + 1,
                )
                self.assertEqual(
                    sum(1 for c in wf_transport.calls if _is_bulk_read_call(c)),
                    comment_pages + 1,
                )
                self.assertEqual(
                    [c for c in app_transport.calls if _is_bulk_read_call(c)],
                    [],
                )

    def test_gate_2_comment_deletion_before_anchor_rescans_and_deduplicates(
        self,
    ) -> None:
        # Part A: GraphQL cursor stability invalidation + overlap deduplication.
        pr_gql = SimulatedGitHubPR(page_size=2)
        c1 = pr_gql.add_comment("comment-1-before-anchor", is_app=False)
        c2 = pr_gql.add_comment("comment-2-anchor", is_app=False)
        c3 = pr_gql.add_comment("comment-3", is_app=False)

        prefix_records = [
            PaginatedRecord.from_payload(
                f"node-{c1['id']}",
                {**c1, "id": f"node-{c1['id']}", "databaseId": c1["id"]},
            ),
            PaginatedRecord.from_payload(
                f"node-{c2['id']}",
                {**c2, "id": f"node-{c2['id']}", "databaseId": c2["id"]},
            ),
        ]
        saved_hint = AdvisoryProgressHint(
            operation_id="op-gate2-gql",
            recovery_phase="graphql_scan",
            cursor_hint="cursor:2",
            verified_prefix_count=2,
            verified_prefix_digest=compute_records_prefix_digest(prefix_records),
            last_verified_id=f"node-{c2['id']}",
            stall_count=0,
            last_progress_digest=compute_records_prefix_digest(prefix_records),
            transient_error_count=0,
        )
        # Persist advisory boundary on the PR, then delete c1 before the anchor.
        pr_gql.add_comment(format_v6_record_marker(saved_hint), is_app=True)
        pr_gql.delete_comment(c1["id"])
        c4 = pr_gql.add_comment("comment-4-after-delete", is_app=False)

        # Simulate shifted/overlapping boundary pages on rescan ([c2, c3] then [c3, c4]).
        seen_after: list[str | None] = []

        def gql_overlap_interceptor(
            call: dict[str, Any], default_fn: Callable[[], Any]
        ) -> tuple[int, Mapping[str, str], Any]:
            after = (call.get("body") or {}).get("variables", {}).get("after")
            seen_after.append(after)
            if after is None:
                nodes = [
                    {**c2, "id": f"node-{c2['id']}", "databaseId": c2["id"]},
                    {**c3, "id": f"node-{c3['id']}", "databaseId": c3["id"]},
                ]
                return (
                    200,
                    {"X-RateLimit-Remaining": "998"},
                    {
                        "data": {
                            "connection": {
                                "nodes": nodes,
                                "pageInfo": {
                                    "hasNextPage": True,
                                    "endCursor": "cursor:c3",
                                },
                            }
                        }
                    },
                )
            nodes = [
                {**c3, "id": f"node-{c3['id']}", "databaseId": c3["id"]},
                {**c4, "id": f"node-{c4['id']}", "databaseId": c4["id"]},
            ]
            return (
                200,
                {"X-RateLimit-Remaining": "997"},
                {
                    "data": {
                        "connection": {
                            "nodes": nodes,
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                },
            )

        gql_client, _, gql_app = _build_client(
            pr_gql, workflow_interceptor=gql_overlap_interceptor
        )
        restored_hint = next(
            r
            for r in pr_gql.collect_ledger_records()
            if isinstance(r, AdvisoryProgressHint)
        )
        shifted_prefix_digest = compute_records_prefix_digest(
            [
                PaginatedRecord.from_payload(
                    f"node-{c2['id']}",
                    {**c2, "id": f"node-{c2['id']}", "databaseId": c2["id"]},
                )
            ]
        )
        gql_res = StabilityVerifiedPaginator(gql_client).scan_graphql(
            query="query($after:String){...}",
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            progress_guard=NoProgressGuard(operation_id="op-gate2-gql"),
            advisory_hint=restored_hint,
            known_records=prefix_records,
            current_total_count=3,
            current_prefix_digest=shifted_prefix_digest,
            anchor_resolved_id=f"node-{c2['id']}",
        )
        self.assertFalse(gql_res.resumed_from_cursor)
        self.assertTrue(gql_res.rescanned)
        self.assertEqual(seen_after, [None, "cursor:c3"])
        self.assertEqual(
            [r.record_id for r in gql_res.records],
            [f"node-{c2['id']}", f"node-{c3['id']}", f"node-{c4['id']}"],
        )
        self.assertEqual(len(gql_app.calls), 0)

        # Part B: Full V6RecoveryEngine Run 1 pause -> earlier comment deleted ->
        # Run 2 artifact-free rescan + REST overlap dedup -> RecoveryCompleted.
        pr = SimulatedGitHubPR(page_size=2)
        early = pr.add_comment("earlier-before-anchor", is_app=False, comment_id=101)
        anchor = pr.add_comment("saved-anchor", is_app=False, comment_id=102)
        mid = pr.add_comment("mid-comment", is_app=False, comment_id=103)
        unresolved = _make_unresolved_record(operation_id="op-gate2")
        pr.add_comment(format_v6_record_marker(unresolved), is_app=True, comment_id=104)
        advisory = pr.add_comment("advisory placeholder", is_app=True, comment_id=105)

        def run1_throttle_page2(
            call: dict[str, Any], default_fn: Callable[[], Any]
        ) -> tuple[int, Mapping[str, str], Any]:
            if "/issues/12/comments" in call["path"] and "page=2" in call["path"]:
                return (
                    429,
                    {"Retry-After": "900", "X-RateLimit-Remaining": "0"},
                    {"message": "secondary rate limit"},
                )
            return default_fn()

        client1, _, _ = _build_client(pr, workflow_interceptor=run1_throttle_page2)
        req = _make_request(operation_id="op-gate2", advisory_comment_id=advisory["id"])
        paused = V6RecoveryEngine(client1, now_fn=lambda: 1000.0).recover_and_publish(
            req
        )
        self.assertIsInstance(paused, RecoveryPaused)
        assert isinstance(paused, RecoveryPaused)
        self.assertEqual(paused.checkpoint_state.verified_prefix_count, 2)
        self.assertEqual(paused.checkpoint_state.last_verified_id, anchor["id"])

        # Delete comment before saved anchor; duplicate mid-comment across pages on Run 2.
        pr.delete_comment(early["id"])

        def run2_shifted_overlap(
            call: dict[str, Any], default_fn: Callable[[], Any]
        ) -> tuple[int, Mapping[str, str], Any]:
            status, headers, body = default_fn()
            if (
                "/issues/12/comments" in call["path"]
                and "page=2" in call["path"]
                and isinstance(body, list)
            ):
                # Inject duplicate of anchor/mid from shifted page 1 to prove dedup.
                return status, headers, [dict(mid), *body]
            return status, headers, body

        client2, wf2, app2 = _build_client(
            pr, workflow_interceptor=run2_shifted_overlap
        )
        completed = V6RecoveryEngine(client2).recover_and_publish(req)
        self.assertIsInstance(completed, RecoveryCompleted)
        assert isinstance(completed, RecoveryCompleted)
        self.assertEqual(completed.model_calls_started, 0)
        self.assertEqual(len(pr.reviews), 1)
        self.assertTrue(
            any("page=1" in c["path"] or "page=" not in c["path"] for c in wf2.calls)
        )
        self.assertEqual([c for c in app2.calls if _is_bulk_read_call(c)], [])

    def test_gate_3_persistent_no_progress_loop_guard_across_restarts(self) -> None:
        pr = SimulatedGitHubPR(page_size=1, workflow_limit=1000)
        c1 = pr.add_comment("only-comment", is_app=False, comment_id=101)
        advisory = pr.add_comment("advisory", is_app=True, comment_id=102)

        run1_calls = 0

        def run1_stalled_graphql(
            call: dict[str, Any], default_fn: Callable[[], Any]
        ) -> tuple[int, Mapping[str, str], Any]:
            nonlocal run1_calls
            if call["path"] == "/graphql":
                run1_calls += 1
                if run1_calls <= 3:
                    # Page 1 yields c1; pages 2 & 3 repeat c1 (0 new records) with
                    # brand-new opaque cursor strings.
                    return (
                        200,
                        {"X-RateLimit-Remaining": str(1000 - run1_calls)},
                        {
                            "data": {
                                "connection": {
                                    "nodes": [
                                        {
                                            **c1,
                                            "id": f"node-{c1['id']}",
                                            "databaseId": c1["id"],
                                        }
                                    ],
                                    "pageInfo": {
                                        "hasNextPage": True,
                                        "endCursor": f"opaque-cursor-run1-{run1_calls}",
                                    },
                                }
                            }
                        },
                    )
                return (
                    429,
                    {"Retry-After": "900", "X-RateLimit-Remaining": "995"},
                    {"message": "secondary rate limit"},
                )
            return default_fn()

        client1, _, _ = _build_client(pr, workflow_interceptor=run1_stalled_graphql)
        req = _make_request(
            operation_id="op-gate3",
            advisory_comment_id=advisory["id"],
            graphql_query="query($after:String){...}",
        )
        paused = V6RecoveryEngine(client1, now_fn=lambda: 1000.0).recover_and_publish(
            req,
            progress_guard=NoProgressGuard(operation_id="op-gate3", max_stall_steps=3),
        )
        self.assertIsInstance(paused, RecoveryPaused)

        persisted_hints = [
            rec
            for rec in pr.collect_ledger_records()
            if isinstance(rec, AdvisoryProgressHint) and rec.operation_id == "op-gate3"
        ]
        self.assertEqual(len(persisted_hints), 1)
        self.assertEqual(persisted_hints[0].stall_count, 2)

        # Restart Run 2 with zero local state: restore guard from PR comment.
        def run2_stalled_graphql(
            call: dict[str, Any], default_fn: Callable[[], Any]
        ) -> tuple[int, Mapping[str, str], Any]:
            if call["path"] == "/graphql":
                return (
                    200,
                    {"X-RateLimit-Remaining": "999"},
                    {
                        "data": {
                            "connection": {
                                "nodes": [],
                                "pageInfo": {
                                    "hasNextPage": True,
                                    "endCursor": "opaque-cursor-run2-brand-new",
                                },
                            }
                        }
                    },
                )
            return default_fn()

        client2, _, _ = _build_client(pr, workflow_interceptor=run2_stalled_graphql)
        restored_guard = NoProgressGuard.from_advisory_hint(
            persisted_hints[-1], max_stall_steps=3
        )
        with self.assertRaises(RecoveryStalledError) as caught:
            V6RecoveryEngine(client2).recover_and_publish(
                req, progress_guard=restored_guard
            )
        self.assertEqual(caught.exception.stall_count, 3)
        self.assertEqual(client2.workflow_graphql_quota.snapshot().remaining, 999)
        self.assertIsNone(client2.workflow_quota.snapshot().remaining)

        # Even without passing a restored guard, persisted stall_count >= max halts early.
        client3, _, _ = _build_client(pr)
        req_rest = _make_request(
            operation_id="op-gate3", advisory_comment_id=advisory["id"]
        )
        with self.assertRaises(RecoveryStalledError) as caught2:
            V6RecoveryEngine(client3).recover_and_publish(
                req_rest,
                progress_guard=NoProgressGuard(
                    operation_id="op-gate3", max_stall_steps=2
                ),
            )
        self.assertGreaterEqual(caught2.exception.stall_count, 2)
        self.assertGreater(client3.workflow_quota.snapshot().remaining or 0, 990)

    def test_gate_4_throttling_both_credentials_graphql_200_and_permission_403(
        self,
    ) -> None:
        # 4A: GITHUB_TOKEN REST 403 and 429 long wait -> RecoveryPaused -> maintainer rerun.
        for status, headers, body in (
            (
                403,
                {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "2000"},
                {"message": "API rate limit exceeded"},
            ),
            (
                429,
                {"Retry-After": "900", "X-RateLimit-Remaining": "0"},
                {"message": "secondary rate limit"},
            ),
        ):
            with self.subTest(rest_throttle_status=status):
                pr = SimulatedGitHubPR()
                unresolved = _make_unresolved_record(operation_id=f"op-4a-{status}")
                pr.add_comment(format_v6_record_marker(unresolved), is_app=True)
                advisory = pr.add_comment("advisory", is_app=True)

                def wf_throttle(
                    call: dict[str, Any],
                    default_fn: Callable[[], Any],
                    *,
                    st: int = status,
                    hd: Mapping[str, str] = headers,
                    bd: Mapping[str, Any] = body,
                ) -> tuple[int, Mapping[str, str], Any]:
                    return st, hd, bd

                client1, _, app1 = _build_client(pr, workflow_interceptor=wf_throttle)
                req = _make_request(
                    operation_id=f"op-4a-{status}",
                    advisory_comment_id=advisory["id"],
                )
                paused = V6RecoveryEngine(
                    client1, now_fn=lambda: 1000.0
                ).recover_and_publish(req)
                self.assertIsInstance(paused, RecoveryPaused)
                assert isinstance(paused, RecoveryPaused)
                self.assertEqual(paused.rerun_command, MAINTAINER_RERUN_COMMAND)
                self.assertFalse(paused.reused_prior_durable_state)
                self.assertEqual(paused.model_calls_started, 0)
                self.assertGreater(paused.resume_after_epoch, 1000.0)
                self.assertEqual(
                    [c["method"] for c in app1.calls], ["GET", "GET", "PATCH"]
                )

                # Maintainer rerun resumes cleanly from PR alone.
                client2, _, _ = _build_client(pr)
                req_rerun = _make_request(
                    operation_id=f"op-4a-{status}",
                    event=GitHubEventKind.ISSUE_COMMENT,
                    advisory_comment_id=advisory["id"],
                )
                completed = V6RecoveryEngine(client2).recover_and_publish(req_rerun)
                self.assertIsInstance(completed, RecoveryCompleted)
                assert isinstance(completed, RecoveryCompleted)
                self.assertEqual(completed.model_calls_started, 0)
                self.assertEqual(len(pr.reviews), 1)

        # 4B: GITHUB_TOKEN GraphQL HTTP 200 RATE_LIMITED classification & pause/resume.
        gql_rl_body: dict[str, Any] = {
            "data": {
                "rateLimit": {
                    "limit": 5000,
                    "remaining": 0,
                    "resetAt": "1970-01-01T00:33:20Z",  # epoch 2000
                }
            },
            "errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}],
        }
        with self.assertRaises(Exception) as caught_gql:
            classify_github_response(
                200,
                {},
                gql_rl_body,
                credential_kind=CredentialKind.WORKFLOW_GITHUB_TOKEN,
                endpoint_id="graphql_review_threads",
                is_graphql=True,
            )
        self.assertEqual(
            getattr(caught_gql.exception, "rate_limit_kind", None), "graphql_200"
        )
        self.assertNotIsInstance(caught_gql.exception, GitHubHTTPPermissionError)

        pr_gql = SimulatedGitHubPR()
        unresolved_gql = _make_unresolved_record(operation_id="op-4b-gql")
        pr_gql.add_comment(format_v6_record_marker(unresolved_gql), is_app=True)
        advisory_gql = pr_gql.add_comment("advisory", is_app=True)

        client_gql1, _, _ = _build_client(
            pr_gql,
            workflow_interceptor=lambda call, df: (200, {}, gql_rl_body),
        )
        req_gql = _make_request(
            operation_id="op-4b-gql",
            advisory_comment_id=advisory_gql["id"],
            graphql_query="query($after:String){...}",
        )
        paused_gql = V6RecoveryEngine(
            client_gql1, now_fn=lambda: 1000.0
        ).recover_and_publish(req_gql)
        self.assertIsInstance(paused_gql, RecoveryPaused)
        assert isinstance(paused_gql, RecoveryPaused)
        self.assertEqual(paused_gql.resume_after_epoch, 2000.0)
        self.assertEqual(paused_gql.rerun_command, MAINTAINER_RERUN_COMMAND)
        self.assertEqual(paused_gql.model_calls_started, 0)

        client_gql2, _, _ = _build_client(pr_gql)
        completed_gql = V6RecoveryEngine(client_gql2).recover_and_publish(
            _make_request(
                operation_id="op-4b-gql",
                event=GitHubEventKind.ISSUE_COMMENT,
                advisory_comment_id=advisory_gql["id"],
                graphql_query="query($after:String){...}",
            )
        )
        self.assertIsInstance(completed_gql, RecoveryCompleted)
        self.assertEqual(len(pr_gql.reviews), 1)

        # 4C: APP_BROKER throttle reuses prior durable PR state safely & resumes cleanly.
        pr_app = SimulatedGitHubPR()
        unresolved_app = _make_unresolved_record(operation_id="op-4c-app")
        pr_app.add_comment(format_v6_record_marker(unresolved_app), is_app=True)
        prior_hint = AdvisoryProgressHint(
            operation_id="op-4c-app",
            recovery_phase="scan_reviews",
            cursor_hint=None,
            verified_prefix_count=2,
            verified_prefix_digest="f" * 64,
            last_verified_id=101,
            stall_count=0,
            last_progress_digest="f" * 64,
            transient_error_count=0,
        )
        prior_body = f"prior durable checkpoint\n{format_v6_record_marker(prior_hint)}"
        advisory_app = pr_app.add_comment(prior_body, is_app=True)

        client_app1, _, _ = _build_client(
            pr_app,
            broker_interceptor=lambda call, df: (
                429,
                {"Retry-After": "900"},
                {"message": "secondary rate limit"},
            ),
        )
        req_app = _make_request(
            operation_id="op-4c-app",
            advisory_comment_id=advisory_app["id"],
        )
        paused_app = V6RecoveryEngine(
            client_app1, now_fn=lambda: 1000.0
        ).recover_and_publish(req_app)
        self.assertIsInstance(paused_app, RecoveryPaused)
        assert isinstance(paused_app, RecoveryPaused)
        self.assertTrue(paused_app.reused_prior_durable_state)
        self.assertEqual(paused_app.checkpoint_state, prior_hint)
        self.assertEqual(paused_app.rerun_command, MAINTAINER_RERUN_COMMAND)
        self.assertEqual(pr_app.get_comment(advisory_app["id"])["body"], prior_body)

        client_app2, _, _ = _build_client(pr_app)
        completed_app = V6RecoveryEngine(client_app2).recover_and_publish(
            _make_request(
                operation_id="op-4c-app",
                event=GitHubEventKind.WORKFLOW_DISPATCH,
                advisory_comment_id=advisory_app["id"],
            )
        )
        self.assertIsInstance(completed_app, RecoveryCompleted)
        self.assertEqual(len(pr_app.reviews), 1)

        # 4D: Permission 403 fails closed immediately with 0 retries, 0 sleeps, 0 App calls.
        pr_perm = SimulatedGitHubPR()
        slept: list[float] = []
        client_perm, wf_perm, app_perm = _build_client(
            pr_perm,
            workflow_interceptor=lambda call, df: (
                403,
                {"X-RateLimit-Remaining": "999"},
                {"message": "Resource not accessible by integration"},
            ),
        )
        with self.assertRaises(GitHubHTTPPermissionError):
            V6RecoveryEngine(client_perm, sleeper=slept.append).recover_and_publish(
                _make_request(
                    operation_id="op-4d-perm",
                    visibility=RepositoryVisibility.FORK_PULL_REQUEST,
                )
            )
        self.assertEqual(slept, [])
        self.assertEqual(len(wf_perm.calls), 1)
        self.assertEqual(len(app_perm.calls), 0)

    def test_gate_5_stale_writer_rejected_and_immutable_records_preserved(
        self,
    ) -> None:
        pr = SimulatedGitHubPR(active_writer_epoch=2, active_run_id=6002)
        unresolved_active = _make_unresolved_record(operation_id="op-active")
        pr.add_comment(format_v6_record_marker(unresolved_active), is_app=True)

        # Active writer epoch=2 executes and records immutable intent + receipt.
        active_client, _, _ = _build_client(pr)
        active_proof = _make_proof(operation_id="op-active", epoch=2, run_id=6002)
        active_outcome = V6RecoveryEngine(active_client).recover_and_publish(
            _make_request(operation_id="op-active", proof=active_proof)
        )
        self.assertIsInstance(active_outcome, RecoveryCompleted)

        # Also stage an unresolved second operation with an active epoch-2 intent.
        unresolved_pending = _make_unresolved_record(operation_id="op-pending")
        pr.add_comment(format_v6_record_marker(unresolved_pending), is_app=True)
        pending_intent = WriteIntentRecord(
            record_id="intent:op-pending:2",
            operation_id="op-pending",
            writer_epoch=2,
            pr_head_sha=HEAD_SHA,
            effect_kind="pr_review_publish",
            payload_sha256=unresolved_pending.payload_sha256,
            target_digest="target-pending",
        )
        pr.add_comment(format_v6_record_marker(pending_intent), is_app=True)
        snapshot_before = [dict(c) for c in pr.comments]

        # Stale epoch=1 runner and unverified runner both attempt delayed writes.
        for label, stale_proof in (
            (
                "superseded_epoch",
                _make_proof(operation_id="op-pending", epoch=1, run_id=6001),
            ),
            (
                "unverified_broker",
                _make_proof(
                    operation_id="op-pending",
                    epoch=2,
                    run_id=6002,
                    verified_by_broker=False,
                ),
            ),
        ):
            with self.subTest(stale_case=label):
                stale_client, _, stale_app = _build_client(pr)
                with self.assertRaises(StaleWriterOwnershipError):
                    V6RecoveryEngine(stale_client).recover_and_publish(
                        _make_request(operation_id="op-pending", proof=stale_proof)
                    )
                self.assertEqual(
                    [c for c in stale_app.calls if c["method"] != "GET"],
                    [],
                )

        # All immutable intents and receipts remain completely intact on the PR.
        self.assertEqual(pr.comments, snapshot_before)
        ledger = pr.collect_ledger_records()
        intents = [r for r in ledger if isinstance(r, WriteIntentRecord)]
        receipts = [r for r in ledger if isinstance(r, CompletionReceiptRecord)]
        summary = reconcile_immutable_records(intents, receipts)
        self.assertEqual(summary.completed_operations, ("op-active",))
        self.assertEqual(summary.unreconciled_intents, (pending_intent,))
        self.assertEqual(summary.conflicting_records, ())

    def test_gate_6_lost_write_response_reconciles_with_zero_duplicates_and_model_calls(
        self,
    ) -> None:
        # Case 6A: Run 1 loses review POST response after GitHub commits 201 and exits;
        # Run 2 reconciles from PR alone -> 0 duplicate reviews & 0 model calls.
        pr = SimulatedGitHubPR()
        unresolved = _make_unresolved_record(operation_id="op-gate6")
        pr.add_comment(format_v6_record_marker(unresolved), is_app=True)

        def drop_write_and_verify_in_run1(
            call: dict[str, Any], default_fn: Callable[[], Any]
        ) -> tuple[int, Mapping[str, str], Any]:
            if call["method"] == "POST" and "/pulls/12/reviews" in call["path"]:
                # GitHub applies the 201 mutation, then drops the HTTP response.
                default_fn()
                raise TimeoutError("dropped HTTP response after GitHub stored review")
            if call["method"] == "GET" and "/pulls/12/reviews/" in call["path"]:
                raise TimeoutError("transient readback drop forcing restart recovery")
            return default_fn()

        client1, _, app1 = _build_client(
            pr, broker_interceptor=drop_write_and_verify_in_run1
        )
        req = _make_request(operation_id="op-gate6")
        hold = V6RecoveryEngine(client1).recover_and_publish(req)
        self.assertIsInstance(hold, RecoveryHold)
        assert isinstance(hold, RecoveryHold)
        self.assertEqual(hold.blocker, "lost_write_outcome_unverified")
        self.assertTrue(hold.zero_model_calls_started)
        self.assertEqual(len(pr.reviews), 1)
        review_posts_run1 = [
            c
            for c in app1.calls
            if c["method"] == "POST" and "/pulls/12/reviews" in c["path"]
        ]
        self.assertEqual(len(review_posts_run1), 1)

        # Wipe all local state; Run 2 recovers purely from PR ledger + landed review.
        client2, _, app2 = _build_client(pr)
        completed = V6RecoveryEngine(client2).recover_and_publish(req)
        self.assertIsInstance(completed, RecoveryCompleted)
        assert isinstance(completed, RecoveryCompleted)
        self.assertTrue(completed.reconciled_from_existing_effect)
        self.assertEqual(completed.github_effect_id, pr.reviews[0]["id"])
        self.assertEqual(completed.model_calls_started, 0)
        self.assertEqual(len(pr.reviews), 1)
        review_posts_run2 = [
            c
            for c in app2.calls
            if c["method"] == "POST" and "/pulls/12/reviews" in c["path"]
        ]
        self.assertEqual(review_posts_run2, [])

        ledger = pr.collect_ledger_records()
        intents = [r for r in ledger if isinstance(r, WriteIntentRecord)]
        receipts = [r for r in ledger if isinstance(r, CompletionReceiptRecord)]
        summary = reconcile_immutable_records(intents, receipts)
        self.assertEqual(summary.completed_operations, ("op-gate6",))
        self.assertEqual(summary.unreconciled_intents, ())

        # Case 6B: Inline reconciliation immediately after dropped 201 response.
        pr_inline = SimulatedGitHubPR()
        pr_inline.add_comment(
            format_v6_record_marker(_make_unresolved_record(operation_id="op-gate6b")),
            is_app=True,
        )

        def drop_write_only(
            call: dict[str, Any], default_fn: Callable[[], Any]
        ) -> tuple[int, Mapping[str, str], Any]:
            if call["method"] == "POST" and "/pulls/12/reviews" in call["path"]:
                default_fn()
                raise TimeoutError("dropped HTTP 201 response")
            return default_fn()

        client_inline, _, app_inline = _build_client(
            pr_inline, broker_interceptor=drop_write_only
        )
        inline_outcome = V6RecoveryEngine(client_inline).recover_and_publish(
            _make_request(operation_id="op-gate6b")
        )
        self.assertIsInstance(inline_outcome, RecoveryCompleted)
        assert isinstance(inline_outcome, RecoveryCompleted)
        self.assertTrue(inline_outcome.reconciled_from_existing_effect)
        self.assertEqual(inline_outcome.readback_gets_emitted, 1)
        self.assertEqual(inline_outcome.model_calls_started, 0)
        self.assertEqual(len(pr_inline.reviews), 1)
        self.assertEqual(
            len(
                [
                    c
                    for c in app_inline.calls
                    if c["method"] == "POST" and "/pulls/12/reviews" in c["path"]
                ]
            ),
            1,
        )

    def test_gate_7_artifact_free_restarts_and_missing_pr_evidence_hold(self) -> None:
        # 7A: Optional artifact cache exists during Run 1, is wiped before Run 2,
        # and Run 2 completes purely from PR-resident state.
        pr = SimulatedGitHubPR()
        findings = ({"path": "src/app.py", "line": 42, "body": "Fix null guard"},)
        cache_dir = Path(tempfile.mkdtemp(prefix="v6-gate7-cache-"))
        self.addCleanup(shutil.rmtree, cache_dir, True)
        cache_file = cache_dir / "unresolved.json"
        cache_bytes = json.dumps({"findings": findings}).encode("utf-8")
        cache_file.write_bytes(cache_bytes)
        cache_sha = canonical_sha256({"findings": findings})

        unresolved = _make_unresolved_record(
            operation_id="op-gate7",
            findings=findings,
            required_evidence_digests=("ev-diff-1", "ev-mirror-1"),
            artifact_cache_sha256=cache_sha,
        )
        pr.add_comment(format_v6_record_marker(unresolved), is_app=True)
        advisory = pr.add_comment("advisory", is_app=True)

        client1, _, _ = _build_client(
            pr,
            workflow_interceptor=lambda call, df: (
                429,
                {"Retry-After": "900"},
                {"message": "secondary rate limit"},
            ),
        )
        paused = V6RecoveryEngine(client1, now_fn=lambda: 1000.0).recover_and_publish(
            _make_request(
                operation_id="op-gate7",
                advisory_comment_id=advisory["id"],
                observed_evidence_digests=("ev-diff-1", "ev-mirror-1"),
                artifact_cache={"findings": findings},
            )
        )
        self.assertIsInstance(paused, RecoveryPaused)

        # Delete optional artifact cache completely before Run 2.
        shutil.rmtree(cache_dir)
        self.assertFalse(cache_dir.exists())

        client2, _, _ = _build_client(pr)
        completed = V6RecoveryEngine(client2).recover_and_publish(
            _make_request(
                operation_id="op-gate7",
                event=GitHubEventKind.ISSUE_COMMENT,
                advisory_comment_id=advisory["id"],
                observed_evidence_digests=("ev-diff-1", "ev-mirror-1"),
                artifact_cache=None,
            )
        )
        self.assertIsInstance(completed, RecoveryCompleted)
        assert isinstance(completed, RecoveryCompleted)
        self.assertTrue(completed.used_pr_resident_result)
        self.assertEqual(completed.model_calls_started, 0)

        # 7B: Deleting required evidence digest returns RecoveryHold with 0 model calls.
        pr_missing = SimulatedGitHubPR()
        pr_unresolved_comment = pr_missing.add_comment(
            format_v6_record_marker(unresolved), is_app=True
        )
        client_md, _, app_md = _build_client(pr_missing)
        hold_digest = V6RecoveryEngine(client_md).recover_and_publish(
            _make_request(
                operation_id="op-gate7",
                observed_evidence_digests=("ev-diff-1",),  # missing ev-mirror-1
            )
        )
        self.assertIsInstance(hold_digest, RecoveryHold)
        assert isinstance(hold_digest, RecoveryHold)
        self.assertEqual(hold_digest.blocker, "missing_required_pr_evidence")
        self.assertEqual(hold_digest.missing_evidence, ("ev-mirror-1",))
        self.assertTrue(hold_digest.zero_model_calls_started)
        self.assertEqual(app_md.calls, [])

        # 7C: Deleting PR-resident UnresolvedResultRecord returns RecoveryHold even if
        # an optional artifact_cache is provided.
        pr_missing.delete_comment(pr_unresolved_comment["id"])
        client_no_res, _, app_no_res = _build_client(pr_missing)
        hold_res = V6RecoveryEngine(client_no_res).recover_and_publish(
            _make_request(
                operation_id="op-gate7",
                observed_evidence_digests=("ev-diff-1", "ev-mirror-1"),
                artifact_cache={"findings": findings},
            )
        )
        self.assertIsInstance(hold_res, RecoveryHold)
        assert isinstance(hold_res, RecoveryHold)
        self.assertEqual(hold_res.blocker, "missing_pr_unresolved_result")
        self.assertEqual(
            hold_res.missing_evidence,
            (f"unresolved_result:op-gate7:{HEAD_SHA}",),
        )
        self.assertTrue(hold_res.zero_model_calls_started)
        self.assertEqual(app_no_res.calls, [])

    def test_gate_8_v5_remains_pinned_in_wrangler_config(self) -> None:
        wrangler_path = ROOT / "deploy" / "cloudflare" / "wrangler.jsonc"
        self.assertTrue(
            wrangler_path.is_file(), f"missing wrangler config at {wrangler_path}"
        )
        wrangler = json.loads(wrangler_path.read_text(encoding="utf-8"))
        self.assertEqual(wrangler["vars"]["PUBLIC_WORKFLOW_TAG"], "v5")

    def test_quota_bucket_isolation_for_both_credentials_and_orders(self) -> None:
        from review_sensei_github.errors import GitHubHTTPRateLimitError

        # Order 1 (GraphQL low -> REST call) + Workflow interleaving.
        pr1 = SimulatedGitHubPR()
        app_calls_1 = 0

        def broker_order_1(
            call: dict[str, Any],
            default_fn: Callable[[], tuple[int, Mapping[str, str], Any]],
        ) -> tuple[int, Mapping[str, str], Any]:
            nonlocal app_calls_1
            app_calls_1 += 1
            if call["path"] == "/graphql":
                return (
                    200,
                    {
                        "X-RateLimit-Resource": "graphql",
                        "X-RateLimit-Remaining": "5",
                        "X-RateLimit-Reset": "1764200100",
                    },
                    {"data": {"resolveReviewThread": {"thread": {"id": "T1"}}}},
                )
            return (
                200 if call["method"] == "GET" else 201,
                {
                    "X-RateLimit-Resource": "core",
                    "X-RateLimit-Remaining": "4500",
                    "X-RateLimit-Reset": "1764200000",
                },
                {"id": app_calls_1},
            )

        wf_calls_1 = 0

        def wf_interleaved(
            call: dict[str, Any],
            default_fn: Callable[[], tuple[int, Mapping[str, str], Any]],
        ) -> tuple[int, Mapping[str, str], Any]:
            nonlocal wf_calls_1
            wf_calls_1 += 1
            if call["path"] == "/graphql":
                return (
                    200,
                    {"X-RateLimit-Resource": "graphql", "X-RateLimit-Remaining": "10"},
                    {
                        "data": {
                            "rateLimit": {"limit": 1000, "remaining": 10, "cost": 1}
                        }
                    },
                )
            return (
                200,
                {"X-RateLimit-Resource": "core", "X-RateLimit-Remaining": "950"},
                [],
            )

        client1, _, app_tr1 = _build_client(
            pr1, workflow_interceptor=wf_interleaved, broker_interceptor=broker_order_1
        )
        client1.execute(
            "broker_app_identity",
            method="GET",
            path="/app",
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
        )
        client1.execute(
            "review_thread_mutation",
            method="POST",
            path="/graphql",
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            body={"query": "mutation {}"},
            is_graphql=True,
        )
        rest_ok = client1.execute(
            "pr_review_publish",
            method="POST",
            path=f"/repos/{OWNER}/{REPO}/pulls/{PR_NUMBER}/reviews",
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            body={"event": "COMMENT", "body": "ok"},
        )
        self.assertEqual(rest_ok.status, 201)
        self.assertEqual(client1.app_quota.remaining, 4500)
        self.assertEqual(client1.app_graphql_quota.remaining, 5)
        with self.assertRaises(GitHubHTTPRateLimitError) as gql_blocked:
            client1.execute(
                "review_thread_mutation",
                method="POST",
                path="/graphql",
                event=GitHubEventKind.PULL_REQUEST,
                visibility=RepositoryVisibility.PUBLIC,
                granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
                body={"query": "mutation {}"},
                is_graphql=True,
            )
        self.assertEqual(gql_blocked.exception.resource, "graphql")
        self.assertEqual(len(app_tr1.calls), 3)

        client1.execute(
            "graphql_review_threads",
            method="POST",
            path="/graphql",
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            body={"query": "query {}"},
            is_graphql=True,
        )
        client1.execute(
            "pr_issue_comments",
            method="GET",
            path=f"/repos/{OWNER}/{REPO}/issues/{PR_NUMBER}/comments",
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
        )
        self.assertEqual(client1.workflow_graphql_quota.remaining, 10)
        self.assertEqual(client1.workflow_quota.remaining, 950)
        self.assertEqual(
            client1.total_requests_for(CredentialKind.WORKFLOW_GITHUB_TOKEN), 2
        )
        self.assertEqual(client1.total_requests_for(CredentialKind.APP_BROKER), 3)

        # Order 2 (REST low -> GraphQL call).
        pr2 = SimulatedGitHubPR()

        def broker_order_2(
            call: dict[str, Any],
            default_fn: Callable[[], tuple[int, Mapping[str, str], Any]],
        ) -> tuple[int, Mapping[str, str], Any]:
            if call["path"] == "/graphql":
                return (
                    200,
                    {
                        "X-RateLimit-Resource": "graphql",
                        "X-RateLimit-Remaining": "4500",
                        "X-RateLimit-Reset": "1764200100",
                    },
                    {"data": {"resolveReviewThread": {"thread": {"id": "T2"}}}},
                )
            return (
                200 if call["method"] == "GET" else 201,
                {
                    "X-RateLimit-Resource": "core",
                    "X-RateLimit-Remaining": "5",
                    "X-RateLimit-Reset": "1764200000",
                },
                {"id": 77},
            )

        client2, _, app_tr2 = _build_client(pr2, broker_interceptor=broker_order_2)
        client2.execute(
            "review_thread_mutation",
            method="POST",
            path="/graphql",
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            body={"query": "mutation {}"},
            is_graphql=True,
        )
        client2.execute(
            "broker_app_identity",
            method="GET",
            path="/app",
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
        )
        gql_ok = client2.execute(
            "review_thread_mutation",
            method="POST",
            path="/graphql",
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            body={"query": "mutation {}"},
            is_graphql=True,
        )
        self.assertEqual(gql_ok.status, 200)
        self.assertEqual(client2.app_graphql_quota.remaining, 4500)
        self.assertEqual(client2.app_quota.remaining, 5)
        with self.assertRaises(GitHubHTTPRateLimitError) as core_blocked:
            client2.execute(
                "pr_review_publish",
                method="POST",
                path=f"/repos/{OWNER}/{REPO}/pulls/{PR_NUMBER}/reviews",
                event=GitHubEventKind.PULL_REQUEST,
                visibility=RepositoryVisibility.PUBLIC,
                granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
                body={"event": "COMMENT", "body": "blocked"},
            )
        self.assertEqual(core_blocked.exception.resource, "core")
        self.assertEqual(len(app_tr2.calls), 3)
        checkpoint_ok = client2.execute(
            "completion_receipt_comment",
            method="POST",
            path=f"/repos/{OWNER}/{REPO}/issues/{PR_NUMBER}/comments",
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            body={"body": "checkpoint"},
            allow_checkpoint_reserve_override=True,
        )
        self.assertEqual(checkpoint_ok.status, 201)
        self.assertEqual(client2.total_requests_for(CredentialKind.APP_BROKER), 4)


if __name__ == "__main__":
    unittest.main()
