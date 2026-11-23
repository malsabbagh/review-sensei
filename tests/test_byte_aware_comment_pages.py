"""Self-contained regression tests for generic byte-aware comment page planning."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from http.client import HTTPMessage
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from review_sensei_github import action_receipt
from review_sensei_github.action_receipt import (
    COMMAND_RECEIPT_WIRE_BOUND,
    plan_plain_receipt_pages,
    publish_command_receipt,
)
from review_sensei_github.api_run_host import review_startup_plan
from review_sensei_github.api_run_plan import (
    COMMAND_RECEIPT_CONFIRMATION_REQUESTS,
    READ_OPERATION_REQUESTS,
)
from review_sensei_github.api_run_requests import ApiRunRequestTransport
from review_sensei_github.command_finalization_plan import issue_plan
from review_sensei_github.errors import GitHubHTTPResponseTooLargeError
from review_sensei_github.finding_lifecycle import (
    VERSION,
    FindingLifecycleJournal,
)
from review_sensei_github.http import (
    MAX_GITHUB_RESPONSE_BYTES,
    MAX_PAGINATION_PAGES,
    GitHubHttp,
)
from review_sensei_github.publication import ReviewApprovalFinalizer
from review_sensei_github.review_run import ActionRunLedger
from review_sensei_github.session_ledger import (
    GitHubIssueCommentSessionLedger,
    parse_session_comment,
    render_session_comment,
)

from review_sensei.active_finding_window import evaluate_active_window_round
from review_sensei.api_work import (
    ApiExecutionOwner,
    ApiRunIdentity,
    FrozenApiConfiguration,
)
from review_sensei.bounded_evidence import (
    EvidenceReadBudget,
    canonical_bytes,
    inline_manifest,
    validate_binding,
)
from review_sensei.coverage import CoverageManifest, FileCoverage
from review_sensei.errors import ReviewInputError
from review_sensei.finding_lifecycle import encode_lifecycle_state
from review_sensei.finding_lifecycle_store import estimate_lifecycle_branch_parts
from review_sensei.models import ReviewComment, ReviewResult
from review_sensei.presentation import render_command_receipt
from review_sensei.session import SessionIdentity, SessionRecord

REPO = "owner/repo"
REPO_ID = 123
PULL_REQUEST = 7
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
POLICY_DIGEST = "c" * 64
CONFIG_DIGEST = "d" * 64
OP_ID = "1" * 64
APP_SLUG = "reviewsensei[bot]"
APP_ID = 999
NOW = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)
SOURCE_STAMP = "2026-11-22T12:00:00Z"


class _FakeResponse:
    def __init__(self, payload: object, status: int = 200) -> None:
        self.status = status
        raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode(
            "utf-8"
        )
        self._buf = io.BytesIO(raw)
        self.headers = HTTPMessage()
        self.headers["Content-Type"] = "application/json"

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> bool:
        return False


class _FakeIssueCommentServer:
    """In-memory GitHub REST endpoint backed by real GitHubHttp 512 KiB wire limits."""

    def __init__(
        self,
        *,
        command_id: int = 900,
        command_body: str = "@reviewsensei override RS-123456 acceptable risk",
    ) -> None:
        self.comments: list[dict[str, object]] = []
        self.next_id = 1000
        self.requests: list[tuple[str, str, dict[str, list[str]]]] = []
        self.command_id = command_id
        self.command_body = command_body
        self.fail_post_after_store = False

    def opener(self, request, timeout: float = 30) -> _FakeResponse:
        parsed = urlsplit(request.full_url)
        path = parsed.path
        query = parse_qs(parsed.query)
        method = request.get_method()
        self.requests.append((method, path, query))

        if path == f"/repos/{REPO}/pulls/{PULL_REQUEST}" and method == "GET":
            return _FakeResponse(
                {
                    "number": PULL_REQUEST,
                    "head": {"sha": HEAD_SHA},
                    "base": {"sha": BASE_SHA, "ref": "main"},
                }
            )
        if path == f"/repos/{REPO}/issues/{PULL_REQUEST}/comments" and method == "GET":
            items = self.comments
            since = query.get("since", [None])[0]
            if isinstance(since, str):
                items = [
                    item
                    for item in items
                    if not isinstance(item.get("updated_at"), str)
                    or str(item["updated_at"]) >= since
                ]
            per_page = int(query.get("per_page", ["30"])[0])
            page = int(query.get("page", ["1"])[0])
            start = (page - 1) * per_page
            return _FakeResponse(copy.deepcopy(items[start : start + per_page]))
        if path == f"/repos/{REPO}/issues/{PULL_REQUEST}/comments" and method == "POST":
            payload = json.loads(request.data)
            self.next_id += 1
            created = {
                "id": self.next_id,
                "body": payload["body"],
                "updated_at": SOURCE_STAMP,
                "user": {"id": APP_ID, "login": APP_SLUG, "type": "Bot"},
                "issue_url": f"https://api.github.test/repos/{REPO}/issues/{PULL_REQUEST}",
            }
            self.comments.append(created)
            if self.fail_post_after_store:
                self.fail_post_after_store = False
                raise TimeoutError("synthetic lost receipt response")
            return _FakeResponse(copy.deepcopy(created), status=201)
        if path.startswith(f"/repos/{REPO}/issues/comments/") and method == "GET":
            cid = int(path.rsplit("/", 1)[-1])
            for item in self.comments:
                if item.get("id") == cid:
                    return _FakeResponse(copy.deepcopy(item))
            return _FakeResponse({"message": "Not Found"}, status=404)
        if path.startswith(f"/repos/{REPO}/issues/comments/") and method == "PATCH":
            cid = int(path.rsplit("/", 1)[-1])
            payload = json.loads(request.data)
            for item in self.comments:
                if item.get("id") == cid:
                    item["body"] = payload["body"]
                    item["updated_at"] = SOURCE_STAMP
                    return _FakeResponse(copy.deepcopy(item))
            return _FakeResponse({"message": "Not Found"}, status=404)
        if path == f"/users/{APP_SLUG}" and method == "GET":
            return _FakeResponse({"id": APP_ID, "login": APP_SLUG, "type": "Bot"})
        raise AssertionError(f"Unexpected HTTP request: {method} {request.full_url}")


def _identity() -> SessionIdentity:
    return SessionIdentity(
        repository=REPO,
        pull_request=PULL_REQUEST,
        repository_id=REPO_ID,
    )


def _bot_comment(
    cid: int, body: str, *, updated_at: str = SOURCE_STAMP
) -> dict[str, object]:
    return {
        "id": cid,
        "body": body,
        "updated_at": updated_at,
        "user": {"id": APP_ID, "login": APP_SLUG, "type": "Bot"},
        "issue_url": f"https://api.github.test/repos/{REPO}/issues/{PULL_REQUEST}",
    }


def _human_comment(
    cid: int, size_bytes: int, *, updated_at: str = SOURCE_STAMP
) -> dict[str, object]:
    return {
        "id": cid,
        "body": "h" * size_bytes,
        "updated_at": updated_at,
        "user": {"id": 42, "login": "alice", "type": "User"},
        "issue_url": f"https://api.github.test/repos/{REPO}/issues/{PULL_REQUEST}",
    }


def _make_record_with_lifecycle(
    identity: SessionIdentity, *, generation: int = 1
) -> tuple[SessionRecord, dict[str, object]]:
    finding = ReviewComment(
        path="src/one.py",
        line=9,
        body="Admitted finding.",
        blocking=False,
        severity="high",
        category="correctness",
        fix_effort="small",
        symbol="dispatch",
        defect_kind="unchecked-result",
        evidence_id="evidence-1",
        side="LEFT",
        effective_blocking=True,
        needs_human=True,
    )
    review_result = ReviewResult(
        summary="Review.",
        comments=(finding,),
        provider="fixture",
        review_status="complete",
        coverage=CoverageManifest(
            files=(FileCoverage("src/one.py", "reviewed"),),
            enumerated_paths=("src/one.py",),
            enumeration_complete=True,
        ),
    )
    active_state = evaluate_active_window_round(
        review_result,
        head_sha=HEAD_SHA,
        base_sha=BASE_SHA,
        max_active_findings=5,
    )
    journal_doc: dict[str, object] = {
        "schema_version": VERSION,
        "lifecycle": encode_lifecycle_state(
            active_state,
            input_identity=(HEAD_SHA, BASE_SHA, "e" * 64),
        ),
        "effects": [],
        "unknown_trust_effect": False,
        "native_receipts": [],
    }
    envelope = {
        "schema_version": "finding-lifecycle-evidence-v2",
        "lifecycle": journal_doc,
    }
    binding = validate_binding(
        {
            "repository": REPO,
            "repository_id": REPO_ID,
            "pull_request": PULL_REQUEST,
            "base_sha": BASE_SHA,
            "head_sha": HEAD_SHA,
            "policy_digest": POLICY_DIGEST,
            "configuration_digest": CONFIG_DIGEST,
            "generation": generation,
            "producer": f"github-bot:{APP_ID}",
            "purpose": "finding-lifecycle",
            "schema_version": "1.0",
        }
    )
    manifest = inline_manifest(envelope, binding=binding, item_count=1)
    assert manifest is not None
    record = SessionRecord.create(
        identity,
        now=NOW,
    ).evolve(
        generation=generation,
        now=NOW,
        reservation_id=None,
        last_committed_reservation_id=OP_ID,
        finding_lifecycle={
            "schema_version": "finding-lifecycle-reference-v1",
            "operation_id": OP_ID,
            "previous_root_sha256": "0" * 64,
            "previous_snapshot_sha256": None,
            "manifest": manifest,
        },
    )
    return record, journal_doc, active_state


class ByteAwareCommentPageTests(unittest.TestCase):
    def _build_action_harness(
        self,
        tmp_dir: str,
        server: _FakeIssueCommentServer,
        *,
        existing_comments: list[dict[str, object]],
        command_body: str = "@reviewsensei override RS-123456 acceptable risk",
    ):
        identity = _identity()
        caller = ApiRunRequestTransport(
            directory=Path(tmp_dir),
            run=ApiRunIdentity(REPO_ID, 456),
            plan=review_startup_plan("command"),
            attempt=1,
            job="command",
            opener=server.opener,
        )
        http = GitHubHttp(api_url="https://api.github.test", opener=caller.open)
        native = GitHubIssueCommentSessionLedger(
            http,
            token="synthetic-token",
            app_slug=APP_SLUG,
            enable_partition_writes=True,
            enable_finding_lifecycle_writes=True,
        )
        native.authenticated_app_id = APP_ID
        native._evidence_author_id = APP_ID

        caller.initialize_and_activate(
            native,
            identity,
            FrozenApiConfiguration(BASE_SHA, CONFIG_DIGEST, 512),
            ApiExecutionOwner(1, "command", 0),
            now=NOW,
        )
        ledger = ActionRunLedger(native, caller, identity)
        # Rebind the server comment list to simulate an already-existing large PR
        # whose comments cross 512 KiB only after command writes append parts/receipt.
        current_record = parse_session_comment(
            str(server.comments[0]["body"]), identity=identity
        )
        assert current_record is not None
        _, journal_doc, active_state = _make_record_with_lifecycle(
            identity, generation=current_record.generation + 1
        )
        binding = validate_binding(
            {
                "repository": REPO,
                "repository_id": REPO_ID,
                "pull_request": PULL_REQUEST,
                "base_sha": BASE_SHA,
                "head_sha": HEAD_SHA,
                "policy_digest": POLICY_DIGEST,
                "configuration_digest": CONFIG_DIGEST,
                "generation": current_record.generation + 1,
                "producer": f"github-bot:{APP_ID}",
                "purpose": "finding-lifecycle",
                "schema_version": "1.0",
            }
        )
        envelope = {
            "schema_version": "finding-lifecycle-evidence-v2",
            "lifecycle": journal_doc,
        }
        manifest = inline_manifest(envelope, binding=binding, item_count=1)
        assert manifest is not None
        seeded_record = current_record.evolve(
            generation=current_record.generation + 1,
            now=NOW,
            finding_lifecycle={
                "schema_version": "finding-lifecycle-reference-v1",
                "operation_id": OP_ID,
                "previous_root_sha256": current_record.record_sha256,
                "previous_snapshot_sha256": None,
                "manifest": manifest,
            },
        )
        root_comment_id = int(server.comments[0]["id"])
        root_comment = _bot_comment(
            root_comment_id,
            render_session_comment(
                repository_id=REPO_ID, pull_request=PULL_REQUEST, record=seeded_record
            ),
        )
        all_comments = [root_comment, *existing_comments]
        server.comments = copy.deepcopy(all_comments)
        server.next_id = max(int(c["id"]) for c in all_comments)
        ledger._replace_inventory(copy.deepcopy(all_comments))
        ledger._inventory_complete = True
        ledger._list_refresh = False
        caller._root = seeded_record.record_sha256

        source = {
            "kind": "issue_comment",
            "id": 900,
            "updated_at": SOURCE_STAMP,
            "body_sha256": hashlib.sha256(command_body.encode()).hexdigest(),
        }
        # Only present in server if caller fences source comment id 900
        if all(c["id"] != 900 for c in server.comments):
            server.comments.append(
                {
                    "id": 900,
                    "body": command_body,
                    "updated_at": SOURCE_STAMP,
                    "user": {"id": 42, "login": "alice", "type": "User"},
                    "author_association": "OWNER",
                }
            )
            ledger._replace_inventory(copy.deepcopy(server.comments))

        op_key = hashlib.sha256(b"op-key").hexdigest()
        caller.admit_action(
            ledger,
            identity,
            purpose="command",
            source=source,
            head_sha=HEAD_SHA,
            operation_key=op_key,
        )
        admitted_record = parse_session_comment(
            str(server.comments[0]["body"]), identity=identity
        )
        assert admitted_record is not None
        preview_record = admitted_record.evolve(
            generation=admitted_record.generation + 1,
            now=NOW,
        )
        # Keep caller._root synced to admitted_record and use preview_record in plan.
        reviewer_inst = SimpleNamespace(
            finalizer=ReviewApprovalFinalizer(
                http=http, evidence_budget=caller.budget_view
            ),
            _drift_states={},
            _active_window_states={(REPO_ID, PULL_REQUEST): active_state},
            _drift_inputs={},
            _active_window_inputs={
                (REPO_ID, PULL_REQUEST): (HEAD_SHA, BASE_SHA, "e" * 64)
            },
            _drift_concern_history={},
            _unknown_effect_holds=set(),
            native_finding_receipts=lambda **kwargs: (),
        )
        journal = FindingLifecycleJournal(
            reviewer_inst,
            ledger,
            identity,
            operation_id=OP_ID,
            base_sha=BASE_SHA,
            head_sha=HEAD_SHA,
            policy_digest=POLICY_DIGEST,
            configuration_digest=CONFIG_DIGEST,
            app_slug=APP_SLUG,
            record=admitted_record,
            document=journal_doc,
            review_token="synthetic-token",
            native_budget=caller.budget_view,
        )
        finalizer = ReviewApprovalFinalizer(
            http=http, evidence_budget=caller.budget_view
        )
        finalizer._finding_lifecycle_journal = journal  # type: ignore[attr-defined]
        plan = issue_plan(
            finalizer,
            payload={
                "repository": REPO,
                "repository_id": REPO_ID,
                "pull_request": PULL_REQUEST,
                "head_sha": HEAD_SHA,
                "base_sha": BASE_SHA,
                "app_slug": APP_SLUG,
                "review_update": None,
                "review_before": None,
                "cleanup_requests": [],
                "cleanup_native_calls": 0,
                "check_request": None,
                "conditional_approval": None,
                "frontier": [],
                "execution_read_tail": {"preparation": 0},
                "execution_capability_tail": {"review_publish": 0},
                "read_contract": "closed-plain-single-complete-page-v1",
            },
            states=(active_state,),
            check_token="check-token",
            cleanup_instances=frozenset(),
        )
        action = caller.accounting.action
        assert action is not None
        details = {
            "original_root": admitted_record.record_sha256,
            "preview_root": preview_record.record_sha256,
            "intent_sha256": hashlib.sha256(
                canonical_bytes(dict(action["intent"]))
            ).hexdigest(),
            "staged_parts": (),
            "dispositions": b"[]",
        }
        authority = SimpleNamespace(
            document={
                "source": source,
                "actor": {"login": "alice", "type": "User", "association": "OWNER"},
            },
            fence=lambda: None,
        )
        broker = SimpleNamespace(exchange=lambda _oidc, capability: f"tok-{capability}")
        run = SimpleNamespace(
            broker=broker,
            oidc="synthetic-oidc",
            identity=identity,
            http=http,
            ledger=ledger,
            caller=caller,
        )
        result = SimpleNamespace(
            receipts=(
                SimpleNamespace(
                    reference="RS-123456",
                    outcome="recorded",
                    detail="acceptable risk",
                ),
            ),
            applied=True,
        )
        return (
            identity,
            caller,
            ledger,
            journal,
            plan,
            preview_record,
            details,
            run,
            authority,
            command_body,
            result,
        )

    def _preflight_and_publish(
        self,
        journal,
        plan,
        preview_record,
        details,
        run,
        authority,
        command_body,
        result,
        *,
        post_write_parts: int = 0,
        post_write_part_bytes: int = 28_000,
        server: _FakeIssueCommentServer | None = None,
    ) -> tuple[dict[str, object], list[tuple[int, dict[str, object]]]]:
        quote = journal.preflight_command_finalization(
            plan,
            preview_record=preview_record,
            capacity_details=details,
            http=run.http,
            budget=run.caller.budget_view,
        )
        if post_write_parts and server is not None:
            for idx in range(post_write_parts):
                server.comments.append(
                    _bot_comment(server.next_id + 1 + idx, "p" * post_write_part_bytes)
                )
            server.next_id += post_write_parts

        captured_tails: list[tuple[int, dict[str, object]]] = []
        original_op = run.caller._operation

        @contextmanager
        def observe_op(key: str, capacity: int, **kwargs: object):
            if key.startswith("command-receipt:"):
                captured_tails.append((capacity, dict(kwargs)))
            with original_op(key, capacity, **kwargs) as op:
                yield op

        with patch.object(run.caller, "_operation", observe_op):
            publish_command_receipt(run, authority, command_body, result)
        return quote, captured_tails

    def test_1_mid_override_crossing_512kib_succeeds_without_per_page_100(self) -> None:
        # Start with 14 human comments of 32_000 B + root + source comment (~458 KiB total < 512 KiB).
        # Simulated staged parts + post-write lifecycle parts + receipt push GET ?per_page=100 past 512 KiB.
        comments = [_human_comment(10 + i, 32_000) for i in range(14)]
        server = _FakeIssueCommentServer()
        with tempfile.TemporaryDirectory() as tmp:
            (
                identity,
                caller,
                ledger,
                journal,
                plan,
                preview_record,
                details,
                run,
                authority,
                command_body,
                result,
            ) = self._build_action_harness(tmp, server, existing_comments=comments)

            # Verify pre-write per_page=100 fits under 512 KiB.
            status, initial_page = run.http.request(
                "GET",
                f"/repos/{REPO}/issues/{PULL_REQUEST}/comments?per_page=100&page=1",
                token="tok",
            )
            self.assertEqual(status, 200)
            self.assertLess(len(initial_page), 20)

            # Verify estimate_lifecycle_branch_parts distinguishes inline vs multi-part partitioned docs.
            base_doc = journal._document()
            inline_parts = estimate_lifecycle_branch_parts(
                ledger,
                journal._record,
                alternatives=(((base_doc,), frozenset({0})),),
                operation_id=OP_ID,
                base_sha=BASE_SHA,
                head_sha=HEAD_SHA,
                policy_digest=POLICY_DIGEST,
                configuration_digest=CONFIG_DIGEST,
                item_count=1,
                projected_record=preview_record,
            )
            self.assertEqual(inline_parts, 0)
            large_doc = copy.deepcopy(base_doc)
            large_doc["large_payload"] = "".join(
                hashlib.sha256(str(idx).encode()).hexdigest() for idx in range(3_000)
            )
            partitioned_parts = estimate_lifecycle_branch_parts(
                ledger,
                journal._record,
                alternatives=(((large_doc,), frozenset({0})),),
                operation_id=OP_ID,
                base_sha=BASE_SHA,
                head_sha=HEAD_SHA,
                policy_digest=POLICY_DIGEST,
                configuration_digest=CONFIG_DIGEST,
                item_count=1,
                projected_record=preview_record,
            )
            self.assertGreaterEqual(partitioned_parts, 2)

            # Plan with 2 future lifecycle parts crosses 512 KiB -> multi-page plan.
            direct_plan = plan_plain_receipt_pages(
                ledger, identity, staged_parts_count=0, lifecycle_parts_count=2
            )
            self.assertIsNotNone(direct_plan)
            assert direct_plan is not None
            _, page_requests, page_bounds, _ = direct_plan
            self.assertGreaterEqual(len(page_requests), 2)
            self.assertTrue(all(size <= 20 for size, _ in page_requests))
            self.assertTrue(
                all(bound < MAX_GITHUB_RESPONSE_BYTES for bound in page_bounds)
            )

            # Also verify _bind_plain_single_page_receipt refuses outside preflight.
            with self.assertRaisesRegex(ReviewInputError, "requires command preflight"):
                action_receipt._bind_plain_single_page_receipt(
                    ledger, caller, identity, operation_id=OP_ID
                )

            # Seal a multi-page command receipt via canonical staged_parts (crossing > 512 KiB).
            details["staged_parts"] = (
                canonical_bytes({"part": 1}),
                canonical_bytes({"part": 2}),
            )
            req_start = len(server.requests)
            quote, captured_tails = self._preflight_and_publish(
                journal,
                plan,
                preview_record,
                details,
                run,
                authority,
                command_body,
                result,
                post_write_parts=2,
                post_write_part_bytes=36_000,
                server=server,
            )
            receipt_calls = quote["reserved_cost_breakdown"]["receipt_calls"]  # type: ignore[index]
            self.assertGreater(receipt_calls, 8)
            receipt_reqs = server.requests[req_start:]
            comment_gets = [
                q
                for m, p, q in receipt_reqs
                if m == "GET" and p.endswith(f"/issues/{PULL_REQUEST}/comments")
            ]
            self.assertTrue(comment_gets)
            self.assertFalse(
                any(q.get("per_page") == ["100"] for q in comment_gets),
                comment_gets,
            )
            # Confirm live server now actually exceeds 512 KiB on per_page=100!
            with self.assertRaises(GitHubHTTPResponseTooLargeError):
                run.http.request(
                    "GET",
                    f"/repos/{REPO}/issues/{PULL_REQUEST}/comments?per_page=100&page=1",
                    token="tok",
                )
            self.assertEqual(len(captured_tails), 1)
            write_tail = captured_tails[0][1]["write_tail"]
            self.assertEqual(receipt_calls, 6 + 2 * (write_tail - 3))  # type: ignore[operator]

    def test_2_small_pr_retains_single_page_fast_path_and_8_call_budget(self) -> None:
        comments = [_human_comment(10 + i, 500) for i in range(2)]
        server = _FakeIssueCommentServer()
        with tempfile.TemporaryDirectory() as tmp:
            (
                _,
                _,
                _,
                journal,
                plan,
                preview_record,
                details,
                run,
                authority,
                command_body,
                result,
            ) = self._build_action_harness(tmp, server, existing_comments=comments)
            req_start = len(server.requests)
            quote, captured_tails = self._preflight_and_publish(
                journal,
                plan,
                preview_record,
                details,
                run,
                authority,
                command_body,
                result,
            )
            self.assertEqual(quote["reserved_cost_breakdown"]["receipt_calls"], 8)  # type: ignore[index]
            self.assertEqual(captured_tails[0][1]["write_tail"], 4)
            comment_gets = [
                q
                for m, p, q in server.requests[req_start:]
                if m == "GET" and p.endswith(f"/issues/{PULL_REQUEST}/comments")
            ]
            self.assertTrue(all(q.get("per_page") == ["100"] for q in comment_gets))

    def test_3_multipage_lost_post_reconciles_via_live_readback(self) -> None:
        comments = [_human_comment(10 + i, 30_000) for i in range(16)]
        server = _FakeIssueCommentServer()
        with tempfile.TemporaryDirectory() as tmp:
            (
                _,
                caller,
                _,
                journal,
                plan,
                preview_record,
                details,
                run,
                authority,
                command_body,
                result,
            ) = self._build_action_harness(tmp, server, existing_comments=comments)
            server.fail_post_after_store = True
            before_reconciled = len(caller._reconciled_mutations)
            quote, _ = self._preflight_and_publish(
                journal,
                plan,
                preview_record,
                details,
                run,
                authority,
                command_body,
                result,
            )
            self.assertGreater(quote["reserved_cost_breakdown"]["receipt_calls"], 8)  # type: ignore[index]
            self.assertEqual(len(caller._reconciled_mutations), before_reconciled + 1)

    def test_4_multipage_fail_closed_on_prefix_drift_growth_or_nonshort_end(
        self,
    ) -> None:
        base_comments = [_human_comment(10 + i, 30_000) for i in range(16)]
        for fault in ("prefix-drift", "growth-over-max", "nonshort-terminal"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as tmp:
                server = _FakeIssueCommentServer()
                (
                    _,
                    _,
                    ledger,
                    journal,
                    plan,
                    preview_record,
                    details,
                    run,
                    authority,
                    command_body,
                    result,
                ) = self._build_action_harness(
                    tmp, server, existing_comments=copy.deepcopy(base_comments)
                )
                quote = journal.preflight_command_finalization(
                    plan,
                    preview_record=preview_record,
                    capacity_details=details,
                    http=run.http,
                    budget=run.caller.budget_view,
                )
                self.assertGreater(quote["reserved_cost_breakdown"]["receipt_calls"], 8)  # type: ignore[index]
                if fault == "prefix-drift":
                    # Mutate an ID in the cached prefix on the server
                    for item in server.comments:
                        if item["id"] == 10:
                            item["id"] = 99999
                            break
                elif fault == "growth-over-max":
                    server.comments.extend(
                        [_human_comment(5000 + k, 100) for k in range(5)]
                    )
                else:
                    # Pad server comments to fill every page so terminal page is not short
                    while len(server.comments) < 40:
                        server.comments.append(
                            _human_comment(6000 + len(server.comments), 100)
                        )

                with self.assertRaisesRegex(
                    ReviewInputError, "command receipt paged frontier changed"
                ):
                    publish_command_receipt(run, authority, command_body, result)

    def test_5_existing_receipt_patch_padding_and_unplanned_fallback(self) -> None:
        # 16 human comments (~480 KiB) + an existing small receipt comment (< 512 KiB total).
        # Padding the existing receipt comment by COMMAND_RECEIPT_WIRE_BOUND forces multi-page.
        comments = [_human_comment(10 + i, 29_500) for i in range(14)]
        digest = hashlib.sha256(
            b"@reviewsensei override RS-123456 acceptable risk"
        ).hexdigest()
        existing_receipt = _bot_comment(
            500,
            render_command_receipt(
                actor="alice",
                source_comment_id=900,
                command_digest=digest,
                receipts=(),
                ledger_written=False,
            ),
        )
        server = _FakeIssueCommentServer()
        with tempfile.TemporaryDirectory() as tmp:
            (
                identity,
                _,
                ledger,
                journal,
                plan,
                preview_record,
                details,
                run,
                authority,
                command_body,
                result,
            ) = self._build_action_harness(
                tmp, server, existing_comments=[*comments, existing_receipt]
            )
            planned = plan_plain_receipt_pages(
                ledger, identity, staged_parts_count=0, lifecycle_parts_count=0
            )
            self.assertIsNotNone(planned)
            assert planned is not None
            _, page_requests, _, _ = planned
            self.assertGreaterEqual(len(page_requests), 2)

            # Now test unplanned fallback when _inventory_complete is False:
            # preflight reserves 4 + READ_OPERATION_REQUESTS + COMMAND_RECEIPT_CONFIRMATION_REQUESTS = 26,
            # and publish_command_receipt falls back to run.http.paginate with write_tail = 13.
            ledger._inventory_complete = False
            self.assertIsNone(
                plan_plain_receipt_pages(
                    ledger, identity, staged_parts_count=0, lifecycle_parts_count=0
                )
            )
            # Need a fresh plan whose payload/states aren't mutated; reuse journal/plan.
            quote, captured_tails = self._preflight_and_publish(
                journal,
                plan,
                preview_record,
                details,
                run,
                authority,
                command_body,
                result,
            )
            self.assertEqual(
                quote["reserved_cost_breakdown"]["receipt_calls"],  # type: ignore[index]
                4 + READ_OPERATION_REQUESTS + COMMAND_RECEIPT_CONFIRMATION_REQUESTS,
            )
            self.assertEqual(
                4 + READ_OPERATION_REQUESTS + COMMAND_RECEIPT_CONFIRMATION_REQUESTS, 26
            )
            self.assertEqual(
                captured_tails[0][1]["write_tail"],
                1 + COMMAND_RECEIPT_CONFIRMATION_REQUESTS,
            )

    def test_6_preflight_finding_lifecycle_inline_refuses_above_512kib(self) -> None:
        identity = _identity()
        server = _FakeIssueCommentServer()
        http = GitHubHttp(api_url="https://api.github.test", opener=server.opener)
        ledger = GitHubIssueCommentSessionLedger(
            http,
            token="synthetic-token",
            app_slug=APP_SLUG,
            evidence_budget=EvidenceReadBudget(),
        )
        ledger.authenticated_app_id = APP_ID
        ledger._evidence_author_id = APP_ID
        record = SessionRecord.create(identity, now=NOW)
        root_comment = _bot_comment(
            1,
            render_session_comment(
                repository_id=REPO_ID, pull_request=PULL_REQUEST, record=record
            ),
        )
        # Small inventory passes (< 512 KiB).
        ledger._replace_inventory([root_comment, _human_comment(2, 100)])
        ledger._inventory_complete = True
        small_plan = ledger.preflight_finding_lifecycle_inline(record)
        self.assertIsNotNone(small_plan)
        assert small_plan is not None
        self.assertEqual(small_plan["conditional_calls"], 4)

        # 18 comments (~505 KiB) with < 100 items, but root replacement pushes >= 512 KiB -> None.
        heavy = [root_comment, *[_human_comment(10 + i, 29_500) for i in range(17)]]
        self.assertLess(len(heavy), 100)
        self.assertGreater(
            len(canonical_bytes(heavy)) + 64 * 1024, MAX_GITHUB_RESPONSE_BYTES
        )
        ledger._replace_inventory(heavy)
        self.assertIsNone(ledger.preflight_finding_lifecycle_inline(record))

    def test_7_tail_fence_shrinks_page_size_before_sealing_and_scans_safely(
        self,
    ) -> None:
        identity = _identity()
        server = _FakeIssueCommentServer()
        http = GitHubHttp(api_url="https://api.github.test", opener=server.opener)
        budget = EvidenceReadBudget()
        ledger = GitHubIssueCommentSessionLedger(
            http,
            token="synthetic-token",
            app_slug=APP_SLUG,
            enable_partition_writes=True,
            enable_history_graph=True,
            evidence_budget=budget,
        )
        ledger.authenticated_app_id = APP_ID
        ledger._evidence_author_id = APP_ID
        initial = SessionRecord.create(identity, now=NOW)
        # Pre-write comments total ~508 KiB (< 512 KiB), so discovery leaves _fence_page_size = 100.
        root_comment = _bot_comment(
            1,
            render_session_comment(
                repository_id=REPO_ID, pull_request=PULL_REQUEST, record=initial
            ),
            updated_at="2026-11-22T12:00:00Z",
        )
        recent_comments = [
            _human_comment(10 + i, 34_480, updated_at="2026-11-22T12:00:05Z")
            for i in range(15)
        ]
        server.comments = [root_comment, *recent_comments]
        loaded = ledger.load(identity, now=NOW)
        self.assertEqual(loaded.status, "ok")
        self.assertEqual(ledger._fence_page_size, 100)

        # Draft grows root enough that post-write ?since= listing on per_page=100 > 512 KiB.
        grown_doc = {
            "schema_version": "finding-lifecycle-evidence-v2",
            "lifecycle": {
                "blob": "".join(
                    hashlib.sha256(str(i).encode()).hexdigest() for i in range(45)
                )
            },
        }
        grown_binding = validate_binding(
            {
                "repository": REPO,
                "repository_id": REPO_ID,
                "pull_request": PULL_REQUEST,
                "base_sha": BASE_SHA,
                "head_sha": HEAD_SHA,
                "policy_digest": POLICY_DIGEST,
                "configuration_digest": CONFIG_DIGEST,
                "generation": initial.generation + 1,
                "producer": f"github-bot:{APP_ID}",
                "purpose": "finding-lifecycle",
                "schema_version": "1.0",
            }
        )
        grown_manifest = inline_manifest(grown_doc, binding=grown_binding, item_count=1)
        assert grown_manifest is not None
        draft = initial.evolve(
            generation=initial.generation + 1,
            now=NOW,
            finding_lifecycle={
                "schema_version": "finding-lifecycle-reference-v1",
                "operation_id": OP_ID,
                "previous_root_sha256": initial.record_sha256,
                "previous_snapshot_sha256": None,
                "manifest": grown_manifest,
            },
        )
        ledger._fence_anchor = "2026-11-22T11:59:58Z"
        safe_size = ledger._safe_fence_page_size(identity, comment_id=1, draft=draft)
        self.assertLessEqual(safe_size, 20)
        ledger._fence_page_size = min(ledger._fence_page_size, safe_size)

        # Update server root comment to grown draft and verify _fence_scan succeeds without 512 KiB error.
        server.comments[0]["body"] = render_session_comment(
            repository_id=REPO_ID, pull_request=PULL_REQUEST, record=draft
        )
        ledger._tail_phase = "readback"
        ledger._tail_scan_pages = 6
        ledger._activation_identity = identity
        ledger._activation_ticket = SimpleNamespace(consume=lambda *_a, **_kw: 10.0)  # type: ignore[assignment]
        before_reqs = len(server.requests)
        scanned = ledger._fence_scan(identity)
        self.assertEqual(len(scanned), len(server.comments))
        scan_queries = [
            q
            for m, p, q in server.requests[before_reqs:]
            if m == "GET" and p.endswith(f"/issues/{PULL_REQUEST}/comments")
        ]
        self.assertTrue(scan_queries)
        self.assertFalse(any(q.get("per_page") == ["100"] for q in scan_queries))

    def test_8_nonnegotiable_limits_preserved(self) -> None:
        self.assertEqual(MAX_GITHUB_RESPONSE_BYTES, 512 * 1024)
        self.assertEqual(MAX_PAGINATION_PAGES, 10)
        self.assertEqual(review_startup_plan("hosted").liability, 130)
        self.assertEqual(review_startup_plan("command").liability, 132)
        self.assertEqual(COMMAND_RECEIPT_WIRE_BOUND, 65536 + 4096)


if __name__ == "__main__":
    unittest.main()
