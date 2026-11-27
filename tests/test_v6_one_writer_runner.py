"""Phase 4 runner, coordinator, takeover, and PR-only recovery tests for v6."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from collections.abc import Callable, Mapping
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import review_sensei_github
from review_sensei_github.cli import main as github_main
from review_sensei_github.errors import (
    GitHubHTTPTransientError,
    GitHubResponseTooLargeError,
)
from review_sensei_github.v6_coordinator_client import (
    ALLOWED_V6_CAPABILITIES,
    ALLOWED_V6_PERMISSION_NAMES,
    V6CoordinatorClient,
    V6CoordinatorError,
    build_v6_slot_key,
    canonicalize_github_host,
)
from review_sensei_github.v6_credential_matrix import (
    WORKFLOW_READ_ONLY_SCOPES,
    GitHubEventKind,
    RepositoryVisibility,
)
from review_sensei_github.v6_fresh_analysis import (
    _format_compare_file_diff,
    run_v6_fresh_pr_analysis,
)
from review_sensei_github.v6_pilot_runner import (
    _MAX_GITHUB_BODY_BYTES,
    UrllibGitHubTransport,
    V6PilotRunConfig,
    V6PilotRunner,
    _build_initial_result_loader,
    default_v6_operation_id,
    run_v6_pilot_cli,
)
from review_sensei_github.v6_recovery_engine import (
    RecoveryCompleted,
    RecoveryRequest,
    StaleWriterOwnershipError,
    V6RecoveryEngine,
)
from review_sensei_github.v6_recovery_ledger import (
    HOLD_SCHEMA,
    MAX_MARKER_ENCODED_BYTES,
    NON_SUBMISSION_SCHEMA,
    OWNER_SCHEMA,
    CompletionReceiptRecord,
    DurableHoldRecord,
    NonSubmissionProofRecord,
    OwnerMarkerRecord,
    UnresolvedResultRecord,
    V6LedgerIntegrityError,
    WriteIntentRecord,
    WriterOwnershipProof,
    canonical_sha256,
    extract_v6_record_markers,
    format_v6_record_marker,
    parse_v6_record_marker,
    reconcile_immutable_records,
    redact_secrets,
    validate_owner_lineage,
    verify_app_authored_record,
)
from review_sensei_github.v6_transport import DualCredentialGitHubClient

OWNER = "octo"
REPO = "repo"
REPO_ID = 101
PR_NUMBER = 12
APP_ID = 424242
INSTALLATION_ID = 7777
BOT_LOGIN = "reviewsensei[bot]"
BOT_USER_ID = 9001
HEAD_A = "a" * 40
HEAD_B = "b" * 40
WORKFLOW_SHA = "c" * 40
MAC_HEX = "d" * 64


def _app_meta(*, app_id: int = APP_ID) -> dict[str, Any]:
    return {
        "performed_via_github_app": {"id": app_id, "slug": "reviewsensei"},
        "user": {"id": BOT_USER_ID, "login": BOT_LOGIN, "type": "Bot"},
    }


def _human_meta() -> dict[str, Any]:
    return {
        "performed_via_github_app": None,
        "user": {"id": 55, "login": "contributor", "type": "User"},
    }


def _effect_tag(operation_id: str, payload_sha256: str) -> str:
    return f"<!-- reviewsensei:v6:effect op={operation_id} sha256={payload_sha256} -->"


def _make_unresolved(
    *,
    operation_id: str = "op-1",
    head_sha: str = HEAD_A,
    findings: tuple[dict[str, Any], ...] = (
        {"path": "src/app.py", "line": 10, "body": "Check bounds"},
    ),
) -> UnresolvedResultRecord:
    return UnresolvedResultRecord(
        record_id=f"unresolved:{operation_id}",
        operation_id=operation_id,
        pr_head_sha=head_sha,
        payload_sha256=canonical_sha256(findings),
        findings_payload=findings,
        required_evidence_digests=("ev-diff-1",),
    )


def _make_owner_record(
    *,
    slot_key: str,
    generation: int = 1,
    lease_epoch: int = 1,
    reservation_id: str | None = None,
    operation_id: str = "op-1",
    run_id: str = "5001",
    run_attempt: int = 1,
    pr_head_sha: str = HEAD_A,
    mac: str = MAC_HEX,
) -> OwnerMarkerRecord:
    return OwnerMarkerRecord(
        record_id=f"owner:{operation_id}:{generation}:{lease_epoch}",
        slot_key=slot_key,
        generation=generation,
        lease_epoch=lease_epoch,
        reservation_id=reservation_id or f"res-{generation}-{lease_epoch}",
        operation_id=operation_id,
        run_id=run_id,
        run_attempt=run_attempt,
        pr_head_sha=pr_head_sha,
        workflow_sha=WORKFLOW_SHA,
        acquired_at="2026-11-27T00:00:00Z",
        expires_at="2026-11-27T01:00:00Z",
        coordinator_attestation_mac=mac,
    )


class _ScriptedTransport:
    def __init__(
        self,
        handler: Callable[[dict[str, Any]], tuple[int, Mapping[str, str], Any]],
    ) -> None:
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


class _FakeUrllibResponse:
    def __init__(
        self,
        status: int,
        body: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.status = status
        self._raw = json.dumps(body).encode("utf-8")
        self.headers = dict(headers or {})

    def read(self, n: int = -1) -> bytes:
        return self._raw if n < 0 else self._raw[:n]

    def __enter__(self) -> _FakeUrllibResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        return None


class _SimulatedCoordinatorStore:
    """In-memory two-gate v6 coordinator state machine enforcing Worker-independent checks."""

    def __init__(
        self,
        *,
        clock: Callable[[], float],
        pr: _SimulatedPRSurface | None = None,
    ) -> None:
        self.clock = clock
        self.pr = pr
        self.slot_key = build_v6_slot_key(
            host="github.com",
            app_id=APP_ID,
            installation_id=INSTALLATION_ID,
            base_repo_id=REPO_ID,
            pr_number=PR_NUMBER,
        )
        self.state = "IDLE"
        self.generation = 0
        self.lease_epoch = 0
        self.reservation_id: str | None = None
        self.owner_run_id: str | None = None
        self.owner_run_attempt: int | None = None
        self.owner_head_sha: str | None = None
        self.owner_comment_id: int | None = None
        self.acquired_at_ms: int | None = None
        self.token_expires_at_ms: int | None = None
        self.token_cleared = False
        self.last_reason_code: str | None = None
        self.active_token: str | None = None
        self.rollback_blocked = False
        self.live_head_sha = HEAD_A
        self.predecessor_terminal = True
        self.drain_revocation_outcome = "204"
        self.worker_pr_writes = 0
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _verify_pr_reconciliation_evidence(
        self,
        *,
        pr: _SimulatedPRSurface,
        outcome: str,
        evidence: Mapping[str, Any],
    ) -> str | None:
        """Worker-side O(1) + bounded verification of PR evidence (zero writes)."""

        old_gen = evidence.get("old_generation")
        if type(old_gen) is not int or old_gen != self.generation or old_gen < 1:
            return "v6_lease_reconciliation_evidence_invalid"

        comments_by_id = {c["id"]: c for c in pr.comments}
        old_owner_cid = evidence.get("old_owner_comment_id")
        if old_owner_cid is not None:
            owner_c = comments_by_id.get(old_owner_cid)
            if owner_c is None or not verify_app_authored_record(
                owner_c, expected_app_id=APP_ID
            ):
                return "v6_lease_slot_continuity_unproved"
            owners = [
                r
                for r in extract_v6_record_markers(owner_c["body"], strict=True)
                if isinstance(r, OwnerMarkerRecord)
                and r.slot_key == self.slot_key
                and r.generation == old_gen
            ]
            if not owners:
                return "v6_lease_slot_continuity_unproved"

        # Collect 100% of App-authored intents for old_generation from PR comments.
        pr_intents_for_gen: dict[int, WriteIntentRecord] = {}
        for c in pr.comments:
            if not verify_app_authored_record(c, expected_app_id=APP_ID):
                continue
            for r in extract_v6_record_markers(c["body"], strict=False):
                if isinstance(r, WriteIntentRecord):
                    belongs = (
                        r.generation == old_gen
                        if r.generation is not None
                        else r.writer_epoch == self.lease_epoch
                    )
                    if belongs:
                        pr_intents_for_gen[c["id"]] = r

        raw_resolutions = evidence.get("intent_resolutions")
        if not isinstance(raw_resolutions, list):
            return "v6_lease_reconciliation_evidence_invalid"
        res_by_intent_cid: dict[int, dict[str, Any]] = {}
        for item in raw_resolutions:
            if not isinstance(item, Mapping):
                return "v6_lease_reconciliation_evidence_invalid"
            icid = item.get("intent_comment_id")
            if type(icid) is not int or icid not in pr_intents_for_gen:
                return "v6_lease_reconciliation_evidence_invalid"
            if icid in res_by_intent_cid:
                return "v6_lease_reconciliation_evidence_invalid"
            res_by_intent_cid[icid] = dict(item)

        if set(res_by_intent_cid.keys()) != set(pr_intents_for_gen.keys()):
            return "v6_lease_reconciliation_evidence_invalid"

        for icid, intent in pr_intents_for_gen.items():
            item = res_by_intent_cid[icid]
            if item.get("operation_id") != intent.operation_id:
                return "v6_lease_reconciliation_evidence_invalid"
            res_kind = item.get("resolution")
            if res_kind == "receipt_present":
                rcid = item.get("receipt_comment_id")
                rc = comments_by_id.get(rcid) if type(rcid) is int else None
                if rc is None or not verify_app_authored_record(
                    rc, expected_app_id=APP_ID
                ):
                    return "v6_lease_reconciliation_evidence_invalid"
                matched = [
                    r
                    for r in extract_v6_record_markers(rc["body"], strict=True)
                    if isinstance(r, CompletionReceiptRecord)
                    and r.operation_id == intent.operation_id
                    and r.payload_sha256 == intent.payload_sha256
                ]
                if not matched:
                    return "v6_lease_reconciliation_evidence_invalid"
            elif res_kind == "adopted_effect":
                eff_kind = item.get("native_effect_kind")
                eff_id = item.get("native_effect_id")
                found_native = False
                if eff_kind == "pull_request_review":
                    found_native = any(
                        r["id"] == eff_id
                        and (
                            r.get("commit_id") is None
                            or r.get("commit_id") == intent.pr_head_sha
                        )
                        and verify_app_authored_record(r, expected_app_id=APP_ID)
                        and _effect_tag(intent.operation_id, intent.payload_sha256)
                        in r["body"]
                        for r in pr.reviews
                    )
                elif eff_kind == "check_run":
                    found_native = any(
                        cr["id"] == eff_id
                        and cr.get("head_sha") == intent.pr_head_sha
                        and _effect_tag(intent.operation_id, intent.payload_sha256)
                        in cr.get("output", {}).get("text", "")
                        for cr in pr.check_runs
                    )
                elif eff_kind == "issue_comment":
                    found_native = any(
                        c["id"] == eff_id
                        and verify_app_authored_record(c, expected_app_id=APP_ID)
                        and _effect_tag(intent.operation_id, intent.payload_sha256)
                        in c["body"]
                        for c in pr.comments
                    )
                elif eff_kind == "pull_request_review_comment":
                    found_native = any(
                        rc["id"] == eff_id
                        and verify_app_authored_record(rc, expected_app_id=APP_ID)
                        and _effect_tag(intent.operation_id, intent.payload_sha256)
                        in rc["body"]
                        for rc in pr.review_comments
                    )
                if not found_native:
                    return "v6_lease_reconciliation_evidence_invalid"
            elif res_kind == "proved_not_submitted":
                pcid = item.get("proof_comment_id")
                pc = comments_by_id.get(pcid) if type(pcid) is int else None
                if pc is None or not verify_app_authored_record(
                    pc, expected_app_id=APP_ID
                ):
                    return "v6_lease_reconciliation_evidence_invalid"
                matched_proofs = [
                    r
                    for r in extract_v6_record_markers(pc["body"], strict=True)
                    if isinstance(r, NonSubmissionProofRecord)
                    and r.slot_key == self.slot_key
                    and r.generation == old_gen
                    and r.operation_id == intent.operation_id
                    and r.payload_sha256 == intent.payload_sha256
                    and r.pr_head_sha == intent.pr_head_sha
                ]
                if not matched_proofs:
                    return "v6_lease_reconciliation_evidence_invalid"
            else:
                return "v6_lease_hold"

        if any(item.get("resolution") == "ambiguous_hold" for item in raw_resolutions):
            return "v6_lease_reconciliation_evidence_invalid"
        if outcome == "clean_receipts":
            if any(
                item.get("resolution") != "receipt_present" for item in raw_resolutions
            ):
                return "v6_lease_reconciliation_evidence_invalid"
        elif outcome == "adopted_missing_receipt":
            if (
                not raw_resolutions
                or any(
                    item.get("resolution")
                    not in {"receipt_present", "adopted_effect", "proved_not_submitted"}
                    for item in raw_resolutions
                )
                or not any(
                    item.get("resolution") == "adopted_effect"
                    for item in raw_resolutions
                )
            ):
                return "v6_lease_reconciliation_evidence_invalid"
        elif outcome == "proved_not_submitted":
            if (
                not raw_resolutions
                or any(
                    item.get("resolution")
                    not in {"receipt_present", "proved_not_submitted"}
                    for item in raw_resolutions
                )
                or not any(
                    item.get("resolution") == "proved_not_submitted"
                    for item in raw_resolutions
                )
            ):
                return "v6_lease_reconciliation_evidence_invalid"
        else:
            return "v6_lease_reconciliation_evidence_invalid"
        return None

    def opener(self, request: Any, timeout: float | None = None) -> _FakeUrllibResponse:
        route = urlsplit(request.full_url).path
        payload = json.loads(request.data.decode("utf-8"))
        self.calls.append((route, payload))

        if route == "/github/v6/lease/status":
            return self._json(
                200,
                {
                    "slot_key": self.slot_key,
                    "state": self.state,
                    "generation": self.generation,
                    "lease_epoch": self.lease_epoch,
                    "reservation_id": self.reservation_id,
                    "owner_run_id": self.owner_run_id,
                    "owner_run_attempt": self.owner_run_attempt,
                    "owner_head_sha": self.owner_head_sha,
                    "owner_workflow_sha": WORKFLOW_SHA if self.generation else None,
                    "owner_job": "v6-pilot" if self.generation else None,
                    "owner_comment_id": self.owner_comment_id,
                    "owner_record_sha256": None,
                    "acquired_at_ms": self.acquired_at_ms,
                    "token_expires_at_ms": self.token_expires_at_ms,
                    "token_cleared": self.token_cleared,
                    "last_reason_code": self.last_reason_code,
                    "updated_at_ms": int(self.clock() * 1000),
                },
            )

        if self.rollback_blocked and route in {
            "/github/v6/lease/acquire",
            "/github/v6/lease/release",
        }:
            return self._err(
                409,
                "v6_lease_rollback_blocked",
                "v6_lease_rollback_blocked",
            )

        if route == "/github/v6/lease/drain":
            if not self.predecessor_terminal:
                return self._err(
                    409,
                    "v6_lease_predecessor_not_terminal",
                    "v6_lease_predecessor_not_terminal",
                )
            if self.drain_revocation_outcome == "204":
                self.state = "RECONCILING"
                self.active_token = None
                self.token_cleared = True
                revocation_status = "confirmed_revoked"
            elif self.drain_revocation_outcome == "401_expired":
                self.state = "RECONCILING"
                self.active_token = None
                self.token_cleared = True
                revocation_status = "expired"
            elif self.drain_revocation_outcome == "uncertain":
                self.state = "RECONCILING"
                self.token_cleared = False
                self.last_reason_code = "v6_lease_revocation_uncertain"
                revocation_status = "revocation_uncertain"
            else:
                self.state = "HOLD"
                self.token_cleared = False
                self.last_reason_code = "v6_lease_escrow_lost"
                revocation_status = "escrow_lost"
            return self._json(
                200,
                {
                    "slot_key": self.slot_key,
                    "state": self.state,
                    "generation": self.generation,
                    "lease_epoch": self.lease_epoch,
                    "token_cleared": self.token_cleared,
                    "revocation_status": revocation_status,
                },
            )

        if route == "/github/v6/lease/complete-reconcile":
            if self.state != "RECONCILING":
                return self._err(409, "v6_lease_hold", "v6_lease_hold")
            if not self.token_cleared:
                return self._err(
                    409,
                    "v6_lease_revocation_uncertain",
                    "v6_lease_revocation_uncertain",
                )
            evidence = payload.get("reconciliation_evidence")
            if not isinstance(evidence, Mapping):
                self.state = "HOLD"
                self.last_reason_code = "v6_lease_reconciliation_evidence_invalid"
                return self._err(
                    400,
                    "v6_lease_reconciliation_evidence_invalid",
                    "v6_lease_reconciliation_evidence_invalid",
                )
            outcome = payload["reconciliation_outcome"]
            if payload.get("anchor_comment_id") is not None:
                self.owner_comment_id = payload["anchor_comment_id"]
            if outcome in {
                "clean_receipts",
                "adopted_missing_receipt",
                "proved_not_submitted",
            }:
                if self.pr is not None:
                    err_diag = self._verify_pr_reconciliation_evidence(
                        pr=self.pr, outcome=outcome, evidence=evidence
                    )
                    if err_diag is not None:
                        self.state = "HOLD"
                        self.last_reason_code = err_diag
                        return self._err(409, err_diag, err_diag)
                self.state = "IDLE"
                self.reservation_id = None
                self.last_reason_code = None
            else:
                self.state = "HOLD"
                self.last_reason_code = "v6_lease_hold"
            return self._json(
                200,
                {
                    "slot_key": self.slot_key,
                    "state": self.state,
                    "generation": self.generation,
                    "lease_epoch": self.lease_epoch,
                },
            )

        if route == "/github/v6/lease/acquire":
            if not self._is_valid_capabilities(payload.get("capabilities")):
                return self._err(
                    400,
                    "broker_capability_invalid",
                    {
                        "code": "broker_capability_invalid",
                        "message": "Invalid capabilities",
                    },
                )
            if self.state != "IDLE":
                return self._err(409, "v6_lease_hold", f"v6_lease_{self.state.lower()}")
            if payload["head_sha"] != self.live_head_sha:
                return self._err(409, "v6_lease_head_changed", "v6_lease_head_changed")
            self.generation += 1
            self.lease_epoch += 1
            self.state = "ACTIVE"
            self.reservation_id = f"res-{self.generation}-{self.lease_epoch}"
            self.owner_head_sha = payload["head_sha"]
            self.owner_run_id = "5002"
            self.owner_run_attempt = 1
            now_ms = int(self.clock() * 1000)
            self.acquired_at_ms = now_ms
            self.token_expires_at_ms = now_ms + 3_600_000
            self.token_cleared = False
            self.active_token = (
                f"ghs_leased_{self.generation}_{self.lease_epoch}_secret"
            )
            return self._json(
                200,
                {
                    "slot_key": self.slot_key,
                    "state": self.state,
                    "generation": self.generation,
                    "lease_epoch": self.lease_epoch,
                    "reservation_id": self.reservation_id,
                    "token": self.active_token,
                    "token_expires_at_ms": self.token_expires_at_ms,
                    "app_id": APP_ID,
                    "installation_id": INSTALLATION_ID,
                    "base_repo_id": REPO_ID,
                    "pull_request": payload["pull_request"],
                    "head_sha": self.live_head_sha,
                    "coordinator_attestation_mac": MAC_HEX,
                },
            )

        if route == "/github/v6/lease/release":
            if self.state != "ACTIVE":
                return self._err(409, "v6_lease_not_active", "v6_lease_hold")
            # Gate 1: /release transitions ACTIVE -> DRAINING -> RECONCILING (NEVER directly to IDLE!).
            if self.drain_revocation_outcome in {"204", "401_expired"}:
                self.state = "RECONCILING"
                self.active_token = None
                self.token_cleared = True
                revocation_status = "confirmed_revoked"
            elif self.drain_revocation_outcome == "uncertain":
                self.state = "RECONCILING"
                self.token_cleared = False
                self.last_reason_code = "v6_lease_revocation_uncertain"
                revocation_status = "revocation_uncertain"
            else:
                self.state = "HOLD"
                self.token_cleared = False
                self.last_reason_code = "v6_lease_escrow_lost"
                revocation_status = "escrow_lost"
            return self._json(
                200,
                {
                    "slot_key": self.slot_key,
                    "state": self.state,
                    "generation": self.generation,
                    "lease_epoch": self.lease_epoch,
                    "token_cleared": self.token_cleared,
                    "revocation_status": revocation_status,
                },
            )

        raise AssertionError(f"unexpected coordinator route: {route}")

    @staticmethod
    def _is_valid_capabilities(capabilities: Any) -> bool:
        if isinstance(capabilities, list):
            return (
                1 <= len(capabilities) <= 10
                and len(set(capabilities)) == len(capabilities)
                and all(
                    type(item) is str and item in ALLOWED_V6_CAPABILITIES
                    for item in capabilities
                )
            )
        if isinstance(capabilities, dict):
            return 1 <= len(capabilities) <= len(ALLOWED_V6_PERMISSION_NAMES) and all(
                type(name) is str
                and name in ALLOWED_V6_PERMISSION_NAMES
                and type(level) is str
                and level in {"read", "write"}
                for name, level in capabilities.items()
            )
        return False

    @staticmethod
    def _json(status: int, body: dict[str, Any]) -> _FakeUrllibResponse:
        return _FakeUrllibResponse(status, body, {"cf-ray": "0123456789abcdef-YYZ"})

    def _err(
        self,
        status: int,
        error: str,
        diagnostic: str | Mapping[str, Any],
    ) -> _FakeUrllibResponse:
        raw = json.dumps(
            {
                "error": error,
                "diagnostic": diagnostic,
                "slot_state": self.state,
            }
        ).encode("utf-8")
        raise HTTPError(
            "https://coordinator.invalid",
            status,
            error,
            {"cf-ray": "0123456789abcdef-YYZ"},  # type: ignore[arg-type]
            io.BytesIO(raw),
        )


class _SimulatedPRSurface:
    """In-memory PR ledger and native GitHub surfaces for Phase 4 runner tests."""

    def __init__(self, *, head_sha: str = HEAD_A) -> None:
        self.head_sha = head_sha
        self.pr_state = "open"
        self.comments: list[dict[str, Any]] = []
        self.reviews: list[dict[str, Any]] = []
        self.review_comments: list[dict[str, Any]] = []
        self.check_runs: list[dict[str, Any]] = []
        self.review_threads: list[dict[str, Any]] = []
        self.run_id = 5002
        self.run_attempt = 1
        self.run_status = "in_progress"
        self._next_id = 100
        self.page_size = 100

    def add_comment(self, body: str, *, is_app: bool = True) -> dict[str, Any]:
        cid = self._next_id
        self._next_id += 1
        meta = _app_meta() if is_app else _human_meta()
        item = {"id": cid, "body": body, **meta}
        self.comments.append(item)
        return item

    def add_review(
        self,
        body: str,
        *,
        commit_id: str,
        is_app: bool = True,
        review_id: int | None = None,
    ) -> dict[str, Any]:
        rid = review_id if review_id is not None else self._next_id
        self._next_id = max(self._next_id, rid + 1)
        meta = _app_meta() if is_app else _human_meta()
        item = {"id": rid, "body": body, "commit_id": commit_id, **meta}
        self.reviews.append(item)
        return item

    def handle_wf(self, call: dict[str, Any]) -> tuple[int, Mapping[str, str], Any]:
        parsed = urlsplit(call["path"])
        qs = parse_qs(parsed.query)
        page = int(qs.get("page", ["1"])[0])
        per_page = min(self.page_size, int(qs.get("per_page", ["100"])[0]))

        def _slice(items: list[dict[str, Any]]) -> tuple[int, dict[str, str], Any]:
            start = (page - 1) * per_page
            chunk = [dict(x) for x in items[start : start + per_page]]
            headers = {"X-RateLimit-Remaining": "999"}
            if page * per_page < len(items):
                headers["Link"] = (
                    f'<https://api.github.com{parsed.path}?page={page + 1}&per_page={per_page}&sort=created&direction=asc>; rel="next"'
                )
            return 200, headers, chunk

        if parsed.path == f"/repos/{OWNER}/{REPO}/issues/{PR_NUMBER}/comments":
            return _slice(self.comments)
        if parsed.path == f"/repos/{OWNER}/{REPO}/pulls/{PR_NUMBER}/reviews":
            return _slice(self.reviews)
        if parsed.path == f"/repos/{OWNER}/{REPO}/pulls/{PR_NUMBER}/comments":
            return _slice(self.review_comments)
        if parsed.path.endswith("/check-runs"):
            ref = parsed.path.split("/")[-2]
            matching = [dict(cr) for cr in self.check_runs if cr.get("head_sha") == ref]
            return 200, {"X-RateLimit-Remaining": "999"}, {"check_runs": matching}
        if call["method"] == "POST" and parsed.path == "/graphql":
            return (
                200,
                {"X-RateLimit-Remaining": "999"},
                {
                    "data": {
                        "connection": {
                            "nodes": [dict(t) for t in self.review_threads],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                },
            )
        raise AssertionError(f"unexpected workflow read: {call}")

    def handle_app(self, call: dict[str, Any]) -> tuple[int, Mapping[str, str], Any]:
        parsed = urlsplit(call["path"])
        method = call["method"]
        body = call.get("body") or {}
        if (
            method == "GET"
            and parsed.path == f"/repos/{OWNER}/{REPO}/pulls/{PR_NUMBER}"
        ):
            return (
                200,
                {"X-RateLimit-Remaining": "4999"},
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
                {"X-RateLimit-Remaining": "4998"},
                {
                    "id": self.run_id,
                    "run_attempt": self.run_attempt,
                    "status": self.run_status,
                },
            )
        if (
            method == "POST"
            and parsed.path == f"/repos/{OWNER}/{REPO}/issues/{PR_NUMBER}/comments"
        ):
            created = self.add_comment(str(body["body"]), is_app=True)
            return 201, {"X-RateLimit-Remaining": "4997"}, dict(created)
        if (
            method == "POST"
            and parsed.path == f"/repos/{OWNER}/{REPO}/pulls/{PR_NUMBER}/reviews"
        ):
            created_rev = self.add_review(
                str(body["body"]), commit_id=str(body.get("commit_id", self.head_sha))
            )
            return 201, {"X-RateLimit-Remaining": "4996"}, dict(created_rev)
        if method == "GET" and parsed.path.startswith(
            f"/repos/{OWNER}/{REPO}/pulls/{PR_NUMBER}/reviews/"
        ):
            rid = int(parsed.path.rsplit("/", 1)[-1])
            for r in self.reviews:
                if r["id"] == rid:
                    return 200, {"X-RateLimit-Remaining": "4995"}, dict(r)
            return 404, {"X-RateLimit-Remaining": "4995"}, {"message": "Not Found"}
        raise AssertionError(f"unexpected App call: {call}")


class V6OneWriterRunnerTests(unittest.TestCase):
    """Comprehensive Phase 4 tests for v6 runner, workflow, and PR-only recovery."""

    def test_marker_escaping_64kib_cap_and_secret_bans(self) -> None:
        slot_key = build_v6_slot_key(
            host="GitHub.COM",
            app_id=APP_ID,
            installation_id=INSTALLATION_ID,
            base_repo_id=REPO_ID,
            pr_number=PR_NUMBER,
        )
        self.assertEqual(
            slot_key,
            f"v6-pr:github.com:{APP_ID}:{INSTALLATION_ID}:{REPO_ID}:{PR_NUMBER}",
        )
        with self.assertRaises(V6CoordinatorError):
            canonicalize_github_host("github.com:443")
        with self.assertRaises(V6CoordinatorError):
            canonicalize_github_host("github.com/api")

        owner_rec = _make_owner_record(slot_key=slot_key, generation=1, lease_epoch=1)
        hold_rec = DurableHoldRecord(
            record_id="hold:op-1:1:1",
            slot_key=slot_key,
            generation=1,
            lease_epoch=1,
            operation_id="op-1",
            pr_head_sha=HEAD_A,
            reason_code="v6_lease_hold",
            missing_evidence=("a-->b<!--c",),
            held_at="2026-11-27T00:00:00Z",
            resolved_by_record_id=None,
        )
        self.assertEqual(owner_rec.schema, OWNER_SCHEMA)
        self.assertEqual(hold_rec.schema, HOLD_SCHEMA)

        for rec in (owner_rec, hold_rec):
            marker = format_v6_record_marker(rec)
            self.assertNotIn("-->", marker[:-4])
            self.assertNotIn("<!--", marker[4:])
            self.assertEqual(parse_v6_record_marker(marker), rec)

        hold_marker = format_v6_record_marker(hold_rec)
        with self.assertRaises(V6LedgerIntegrityError):
            parse_v6_record_marker(hold_marker.replace("\\u003c", "<"))
        with self.assertRaises(V6LedgerIntegrityError):
            parse_v6_record_marker(hold_marker.replace("\\u003e", ">"))

        # 64 KiB cap enforced on both format and parse.
        oversized_findings = tuple(
            {"path": f"src/f{i}.py", "line": i, "body": "x" * 900} for i in range(80)
        )
        oversized = _make_unresolved(operation_id="op-big", findings=oversized_findings)
        with self.assertRaises(V6LedgerIntegrityError):
            format_v6_record_marker(oversized)
        fake_huge = (
            f"<!-- reviewsensei:v6:record sha256={'a' * 64} "
            f'payload={{"a":"{"x" * (MAX_MARKER_ENCODED_BYTES + 10)}"}} -->'
        )
        with self.assertRaises(V6LedgerIntegrityError):
            parse_v6_record_marker(fake_huge)

        # Secret patterns & never_keep_fields rejected in marker formatting/parsing.
        for secret_text in (
            "ghs_1234567890abcdef",
            "ghp_1234567890abcdef",
            "Bearer secret_abc123",
            "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.sig_abcdef",
            "-----BEGIN RSA PRIVATE KEY-----",
        ):
            with self.subTest(secret=secret_text[:12]):
                bad_unresolved = _make_unresolved(
                    findings=({"path": "a.py", "line": 1, "body": secret_text},)
                )
                with self.assertRaises(V6LedgerIntegrityError):
                    format_v6_record_marker(bad_unresolved)
                self.assertIn("[REDACTED]", redact_secrets(secret_text))

        bad_key_findings = ({"path": "a.py", "ollama_api_key": "secret"},)
        with self.assertRaises(V6LedgerIntegrityError):
            format_v6_record_marker(_make_unresolved(findings=bad_key_findings))

    def test_fail_closed_removal_of_proof_writer_epoch_fallback(self) -> None:
        pr = _SimulatedPRSurface()
        unresolved = _make_unresolved()
        pr.add_comment(format_v6_record_marker(unresolved), is_app=True)
        proof = WriterOwnershipProof(
            repository_id=REPO_ID,
            pull_request=PR_NUMBER,
            operation_id="op-1",
            run_id=5002,
            run_attempt=1,
            writer_epoch=1,
            pr_head_sha=HEAD_A,
            verified_by_broker=True,
        )
        req = RecoveryRequest(
            owner=OWNER,
            repo=REPO,
            repository_id=REPO_ID,
            pull_request=PR_NUMBER,
            operation_id="op-1",
            ownership_proof=proof,
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            expected_app_id=APP_ID,
        )

        # Missing writer_epoch in /actions/runs/{id} MUST fail closed (no proof.writer_epoch fallback).
        def app_missing_epoch(
            call: dict[str, Any],
        ) -> tuple[int, Mapping[str, str], Any]:
            if "/actions/runs/" in call["path"]:
                return (
                    200,
                    {"X-RateLimit-Remaining": "4999"},
                    {"id": 5002, "run_attempt": 1, "status": "in_progress"},
                )
            return pr.handle_app(call)

        client1 = DualCredentialGitHubClient(
            workflow_token_transport=_ScriptedTransport(pr.handle_wf),
            broker_app_transport=_ScriptedTransport(app_missing_epoch),
        )
        with self.assertRaises(StaleWriterOwnershipError):
            V6RecoveryEngine(client1).recover_and_publish(req)

        # Missing base.repo.id or state in /pulls/{pr} MUST fail closed (no defaults).
        for mutator in (
            lambda body: body.pop("state"),
            lambda body: body["base"]["repo"].pop("id"),
        ):

            def app_missing_pr_field(
                call: dict[str, Any], mut: Any = mutator
            ) -> tuple[int, Mapping[str, str], Any]:
                st, hdr, body = pr.handle_app(call)
                if call["method"] == "GET" and f"/pulls/{PR_NUMBER}" in call["path"]:
                    mut(body)
                if "/actions/runs/" in call["path"]:
                    body["writer_epoch"] = 1
                return st, hdr, body

            client2 = DualCredentialGitHubClient(
                workflow_token_transport=_ScriptedTransport(pr.handle_wf),
                broker_app_transport=_ScriptedTransport(app_missing_pr_field),
            )
            with self.assertRaises(StaleWriterOwnershipError):
                V6RecoveryEngine(client2).recover_and_publish(req)

    def test_row_3_cross_pr_write_rejected_despite_repo_scoped_token(self) -> None:
        pr = _SimulatedPRSurface()
        pr.add_comment(format_v6_record_marker(_make_unresolved()), is_app=True)
        app_tr = _ScriptedTransport(pr.handle_app)
        client = DualCredentialGitHubClient(
            workflow_token_transport=_ScriptedTransport(pr.handle_wf),
            broker_app_transport=app_tr,
        )
        engine = V6RecoveryEngine(
            client,
            broker_fence_verifier=lambda p: {
                "repository_id": REPO_ID,
                "pull_request": PR_NUMBER,
                "pr_head_sha": HEAD_A,
                "active_writer_epoch": 1,
                "pr_state": "open",
            },
        )
        proof = WriterOwnershipProof(
            repository_id=REPO_ID,
            pull_request=PR_NUMBER,
            operation_id="op-1",
            run_id=5002,
            run_attempt=1,
            writer_epoch=1,
            pr_head_sha=HEAD_A,
            verified_by_broker=True,
        )
        cross_pr_req = RecoveryRequest(
            owner=OWNER,
            repo=REPO,
            repository_id=REPO_ID,
            pull_request=PR_NUMBER,
            operation_id="op-1",
            ownership_proof=proof,
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            expected_app_id=APP_ID,
            effect_path=f"/repos/{OWNER}/{REPO}/pulls/999/reviews",
        )
        with self.assertRaises(StaleWriterOwnershipError) as caught:
            engine.recover_and_publish(cross_pr_req)
        self.assertEqual(caught.exception.diagnostic, "v6_cross_pr_write_refused")
        self.assertEqual([c for c in app_tr.calls if c["method"] != "GET"], [])

    def test_row_5_head_change_during_handoff_reconciles_against_original_head(
        self,
    ) -> None:
        now = 1_764_200_000.0
        pr = _SimulatedPRSurface(head_sha=HEAD_A)
        coord_store = _SimulatedCoordinatorStore(clock=lambda: now, pr=pr)
        slot_key = coord_store.slot_key

        # Predecessor (gen=1, epoch=1) acquired on HEAD_A, wrote owner + unresolved + intent,
        # landed a check-run on HEAD_A, and crashed before writing CompletionReceiptRecord.
        coord_store.state = "ACTIVE"
        coord_store.generation = 1
        coord_store.lease_epoch = 1
        coord_store.reservation_id = "res-1-1"
        coord_store.owner_head_sha = HEAD_A
        owner_1 = _make_owner_record(
            slot_key=slot_key, generation=1, lease_epoch=1, pr_head_sha=HEAD_A
        )
        c_owner = pr.add_comment(format_v6_record_marker(owner_1), is_app=True)
        coord_store.owner_comment_id = c_owner["id"]

        findings = ({"path": "src/a.py", "line": 1, "body": "Check"},)
        payload_sha = canonical_sha256(findings)
        intent_1 = WriteIntentRecord(
            record_id="intent:op-handoff:1",
            operation_id="op-handoff",
            writer_epoch=1,
            pr_head_sha=HEAD_A,
            effect_kind="check_run_write",
            payload_sha256=payload_sha,
            target_digest=canonical_sha256({"head": HEAD_A}),
            generation=1,
            target_location=f"/repos/{OWNER}/{REPO}/check-runs",
        )
        pr.add_comment(format_v6_record_marker(intent_1), is_app=True)
        pr.check_runs.append(
            {
                "id": 8801,
                "head_sha": HEAD_A,
                "external_id": "op-handoff",
                "app": {"id": APP_ID},
                "output": {
                    "summary": "Check complete",
                    "text": _effect_tag("op-handoff", payload_sha),
                },
            }
        )

        # PR head moves to HEAD_B before successor runs!
        pr.head_sha = HEAD_B
        coord_store.live_head_sha = HEAD_B

        coord_client = V6CoordinatorClient(
            base_url="https://coordinator.invalid",
            oidc_token_provider=lambda: "oidc-jwt-token",
            opener=coord_store.opener,
            clock=lambda: now,
        )
        wf_tr = _ScriptedTransport(pr.handle_wf)
        app_tr = _ScriptedTransport(pr.handle_app)
        runner = V6PilotRunner(
            coordinator=coord_client,
            workflow_token_transport=wf_tr,
            app_transport_factory=lambda _g: app_tr,
            clock=lambda: now,
        )

        summary_buf: list[str] = []
        outcome = runner.run(
            V6PilotRunConfig(
                owner=OWNER,
                repo=REPO,
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                head_sha=HEAD_B,
                operation_id="op-handoff",
                run_id=5002,
                workflow_sha=WORKFLOW_SHA,
                expected_app_id=APP_ID,
                expected_bot_login=BOT_LOGIN,
                expected_bot_user_id=BOT_USER_ID,
                step_summary_sink=summary_buf.append,
            )
        )
        self.assertEqual(outcome.status, "completed")
        self.assertTrue(outcome.lease_released)
        self.assertEqual(outcome.model_calls_started, 0)
        assert isinstance(outcome.recovery_outcome, RecoveryCompleted)
        self.assertTrue(outcome.recovery_outcome.reconciled_from_existing_effect)
        self.assertEqual(outcome.recovery_outcome.github_effect_id, 8801)
        # Confirmed: matched check-run on original HEAD_A, appended receipt ONCE, 0 duplicate effects.
        self.assertTrue(
            any(f"/commits/{HEAD_A}/check-runs" in c["path"] for c in wf_tr.calls)
        )
        self.assertEqual(len(pr.reviews), 0)
        receipts = [
            rec
            for c in pr.comments
            for rec in extract_v6_record_markers(c["body"], strict=False)
            if isinstance(rec, CompletionReceiptRecord)
        ]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0].github_effect_id, 8801)

    def test_row_7_preflight_clock_margin_expiry_race_fails_closed(self) -> None:
        now = 1_764_200_000.0
        pr = _SimulatedPRSurface()
        pr.add_comment(format_v6_record_marker(_make_unresolved()), is_app=True)
        app_tr = _ScriptedTransport(pr.handle_app)
        client = DualCredentialGitHubClient(
            workflow_token_transport=_ScriptedTransport(pr.handle_wf),
            broker_app_transport=app_tr,
        )
        engine = V6RecoveryEngine(
            client,
            now_fn=lambda: now,
            broker_fence_verifier=lambda p: {
                "repository_id": REPO_ID,
                "pull_request": PR_NUMBER,
                "pr_head_sha": HEAD_A,
                "active_writer_epoch": 1,
            },
        )
        proof = WriterOwnershipProof(
            repository_id=REPO_ID,
            pull_request=PR_NUMBER,
            operation_id="op-1",
            run_id=5002,
            run_attempt=1,
            writer_epoch=1,
            pr_head_sha=HEAD_A,
            verified_by_broker=True,
        )
        # Token expires within clock_margin_ms (300_000 ms) -> old run refuses preflight before writing!
        req = RecoveryRequest(
            owner=OWNER,
            repo=REPO,
            repository_id=REPO_ID,
            pull_request=PR_NUMBER,
            operation_id="op-1",
            ownership_proof=proof,
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            expected_app_id=APP_ID,
            token_expires_at_ms=int(now * 1000) + 120_000,
            clock_margin_ms=300_000,
        )
        with self.assertRaises(StaleWriterOwnershipError) as caught:
            engine.recover_and_publish(req)
        self.assertEqual(caught.exception.diagnostic, "v6_lease_clock_untrusted")
        self.assertEqual(app_tr.calls, [])

    def test_row_8_lost_receipt_reconciled_and_all_native_effect_matchers(self) -> None:
        # 8A: Lost CompletionReceiptRecord POST response reconciles by stable identity (no duplicate receipt).
        pr = _SimulatedPRSurface()
        unresolved = _make_unresolved(operation_id="op-lost-rcpt")
        pr.add_comment(format_v6_record_marker(unresolved), is_app=True)
        receipt_posts = 0

        def app_drop_receipt_resp(
            call: dict[str, Any],
        ) -> tuple[int, Mapping[str, str], Any]:
            nonlocal receipt_posts
            if (
                call["method"] == "POST"
                and f"/issues/{PR_NUMBER}/comments" in call["path"]
                and "reviewsensei:v6:receipt:v1" in call["body"]["body"]
            ):
                receipt_posts += 1
                pr.add_comment(call["body"]["body"], is_app=True)
                raise TimeoutError("dropped receipt HTTP 201")
            return pr.handle_app(call)

        client = DualCredentialGitHubClient(
            workflow_token_transport=_ScriptedTransport(pr.handle_wf),
            broker_app_transport=_ScriptedTransport(app_drop_receipt_resp),
        )
        engine = V6RecoveryEngine(
            client,
            broker_fence_verifier=lambda p: {
                "repository_id": REPO_ID,
                "pull_request": PR_NUMBER,
                "pr_head_sha": HEAD_A,
                "active_writer_epoch": 1,
            },
        )
        proof = WriterOwnershipProof(
            repository_id=REPO_ID,
            pull_request=PR_NUMBER,
            operation_id="op-lost-rcpt",
            run_id=5002,
            run_attempt=1,
            writer_epoch=1,
            pr_head_sha=HEAD_A,
            verified_by_broker=True,
        )
        outcome = engine.recover_and_publish(
            RecoveryRequest(
                owner=OWNER,
                repo=REPO,
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                operation_id="op-lost-rcpt",
                ownership_proof=proof,
                event=GitHubEventKind.PULL_REQUEST,
                visibility=RepositoryVisibility.PUBLIC,
                granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
                expected_app_id=APP_ID,
            )
        )
        self.assertIsInstance(outcome, RecoveryCompleted)
        self.assertEqual(receipt_posts, 1)

        # 8B: Operation-specific matchers in takeover reconciliation (issue_comment, inline_reply, thread_resolve).
        pr_multi = _SimulatedPRSurface(head_sha=HEAD_A)
        slot_key = build_v6_slot_key(
            host="github.com",
            app_id=APP_ID,
            installation_id=INSTALLATION_ID,
            base_repo_id=REPO_ID,
            pr_number=PR_NUMBER,
        )
        pr_multi.add_comment(
            format_v6_record_marker(_make_owner_record(slot_key=slot_key)), is_app=True
        )
        sha1 = "1" * 64
        sha2 = "2" * 64
        sha3 = "3" * 64
        for idx, (op, kind, psha, tdigest) in enumerate(
            (
                ("op-ic", "issue_comment", sha1, "td-ic"),
                ("op-ir", "inline_reply", sha2, "td-ir"),
                ("op-tr", "review_thread_mutation", sha3, "THREAD-99"),
            ),
            start=1,
        ):
            pr_multi.add_comment(
                format_v6_record_marker(
                    WriteIntentRecord(
                        record_id=f"intent:{op}:1",
                        operation_id=op,
                        writer_epoch=1,
                        pr_head_sha=HEAD_A,
                        effect_kind=kind,
                        payload_sha256=psha,
                        target_digest=tdigest,
                    )
                ),
                is_app=True,
            )
        pr_multi.add_comment(f"Reply\n{_effect_tag('op-ic', sha1)}", is_app=True)
        pr_multi.review_comments.append(
            {
                "id": 701,
                "body": f"Inline\n{_effect_tag('op-ir', sha2)}",
                "commit_id": HEAD_A,
                **_app_meta(),
            }
        )
        pr_multi.review_threads.append(
            {
                "id": "THREAD-99",
                "isResolved": True,
                "comments": {"nodes": []},
            }
        )

        ro_client = DualCredentialGitHubClient(
            workflow_token_transport=_ScriptedTransport(pr_multi.handle_wf),
            broker_app_transport=_ScriptedTransport(
                lambda c: (500, {}, {"message": "must not call app"})
            ),
        )
        recon = V6RecoveryEngine(ro_client).reconcile_takeover_read_only(
            owner=OWNER,
            repo=REPO,
            repository_id=REPO_ID,
            pull_request=PR_NUMBER,
            slot_key=slot_key,
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            expected_app_id=APP_ID,
            expected_bot_login=BOT_LOGIN,
            expected_bot_user_id=BOT_USER_ID,
            graphql_query="query($after:String){...}",
        )
        self.assertEqual(recon.reconciliation_outcome, "adopted_missing_receipt")
        self.assertEqual(len(recon.adopted_effects), 3)
        self.assertEqual(recon.model_calls_started, 0)

    def test_row_8_negative_scan_duplicate_contradictory_and_shifted_pagination(
        self,
    ) -> None:
        slot_key = build_v6_slot_key(
            host="github.com",
            app_id=APP_ID,
            installation_id=INSTALLATION_ID,
            base_repo_id=REPO_ID,
            pr_number=PR_NUMBER,
        )
        # 1) Single empty page / negative scan right after 204 without affirmative non-submission -> HOLD.
        pr_empty = _SimulatedPRSurface()
        pr_empty.add_comment(
            format_v6_record_marker(_make_owner_record(slot_key=slot_key)), is_app=True
        )
        unresolved = _make_unresolved(operation_id="op-neg")
        pr_empty.add_comment(format_v6_record_marker(unresolved), is_app=True)
        pr_empty.add_comment(
            format_v6_record_marker(
                WriteIntentRecord(
                    record_id="intent:op-neg:1",
                    operation_id="op-neg",
                    writer_epoch=1,
                    pr_head_sha=HEAD_A,
                    effect_kind="pr_review_publish",
                    payload_sha256=unresolved.payload_sha256,
                    target_digest="t1",
                )
            ),
            is_app=True,
        )
        summary_lines: list[str] = []
        recon_neg = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=_ScriptedTransport(pr_empty.handle_wf),
                broker_app_transport=_ScriptedTransport(lambda c: (500, {}, {})),
            )
        ).reconcile_takeover_read_only(
            owner=OWNER,
            repo=REPO,
            repository_id=REPO_ID,
            pull_request=PR_NUMBER,
            slot_key=slot_key,
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            expected_app_id=APP_ID,
            expected_bot_login=BOT_LOGIN,
            step_summary_sink=summary_lines.append,
            operation_id="op-neg",
        )
        self.assertEqual(recon_neg.reconciliation_outcome, "ambiguous_hold")
        self.assertEqual(recon_neg.blocker, "unproved_non_submission_after_intent")
        self.assertTrue(summary_lines)
        self.assertEqual(pr_empty.reviews, [])

        # 2) Duplicate matching reviews -> HOLD (no cleanup deletion).
        pr_dup = _SimulatedPRSurface()
        pr_dup.add_comment(
            format_v6_record_marker(_make_owner_record(slot_key=slot_key)), is_app=True
        )
        pr_dup.add_comment(format_v6_record_marker(unresolved), is_app=True)
        pr_dup.add_comment(
            format_v6_record_marker(
                WriteIntentRecord(
                    record_id="intent:op-neg:1",
                    operation_id="op-neg",
                    writer_epoch=1,
                    pr_head_sha=HEAD_A,
                    effect_kind="pr_review_publish",
                    payload_sha256=unresolved.payload_sha256,
                    target_digest="t1",
                )
            ),
            is_app=True,
        )
        tag = _effect_tag("op-neg", unresolved.payload_sha256)
        pr_dup.add_review(f"Review 1\n{tag}", commit_id=HEAD_A, review_id=9101)
        pr_dup.add_review(f"Review 2\n{tag}", commit_id=HEAD_A, review_id=9102)
        recon_dup = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=_ScriptedTransport(pr_dup.handle_wf),
                broker_app_transport=_ScriptedTransport(lambda c: (500, {}, {})),
            )
        ).reconcile_takeover_read_only(
            owner=OWNER,
            repo=REPO,
            repository_id=REPO_ID,
            pull_request=PR_NUMBER,
            slot_key=slot_key,
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            expected_app_id=APP_ID,
            expected_bot_login=BOT_LOGIN,
            operation_id="op-neg",
        )
        self.assertEqual(recon_dup.reconciliation_outcome, "ambiguous_hold")
        self.assertEqual(recon_dup.blocker, "duplicate_effect_match")
        self.assertEqual(len(pr_dup.reviews), 2)

        # 2B) Contradictory match (wrong payload_sha256 for op-neg) -> HOLD (no deletion).
        pr_contra = _SimulatedPRSurface()
        pr_contra.add_comment(
            format_v6_record_marker(_make_owner_record(slot_key=slot_key)), is_app=True
        )
        pr_contra.add_comment(format_v6_record_marker(unresolved), is_app=True)
        pr_contra.add_comment(
            format_v6_record_marker(
                WriteIntentRecord(
                    record_id="intent:op-neg:1",
                    operation_id="op-neg",
                    writer_epoch=1,
                    pr_head_sha=HEAD_A,
                    effect_kind="pr_review_publish",
                    payload_sha256=unresolved.payload_sha256,
                    target_digest="t1",
                )
            ),
            is_app=True,
        )
        pr_contra.add_review(
            f"Contradictory\n{_effect_tag('op-neg', 'f' * 64)}",
            commit_id=HEAD_A,
            review_id=9201,
        )
        recon_contra = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=_ScriptedTransport(pr_contra.handle_wf),
                broker_app_transport=_ScriptedTransport(lambda c: (500, {}, {})),
            )
        ).reconcile_takeover_read_only(
            owner=OWNER,
            repo=REPO,
            repository_id=REPO_ID,
            pull_request=PR_NUMBER,
            slot_key=slot_key,
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            expected_app_id=APP_ID,
            expected_bot_login=BOT_LOGIN,
            operation_id="op-neg",
        )
        self.assertEqual(recon_contra.reconciliation_outcome, "ambiguous_hold")
        self.assertEqual(recon_contra.blocker, "contradictory_effect_match")
        self.assertEqual(len(pr_contra.reviews), 1)

        # 2C) Shifted pagination after earlier comment deletion still recovers landed review.
        pr_shifted = _SimulatedPRSurface()
        pr_shifted.page_size = 1
        early = pr_shifted.add_comment("early-human", is_app=False)
        pr_shifted.add_comment(
            format_v6_record_marker(_make_owner_record(slot_key=slot_key)), is_app=True
        )
        pr_shifted.add_comment(format_v6_record_marker(unresolved), is_app=True)
        pr_shifted.add_comment(
            format_v6_record_marker(
                WriteIntentRecord(
                    record_id="intent:op-neg:1",
                    operation_id="op-neg",
                    writer_epoch=1,
                    pr_head_sha=HEAD_A,
                    effect_kind="pr_review_publish",
                    payload_sha256=unresolved.payload_sha256,
                    target_digest="t1",
                )
            ),
            is_app=True,
        )
        pr_shifted.add_review(f"Landed\n{tag}", commit_id=HEAD_A, review_id=9301)
        pr_shifted.comments = [c for c in pr_shifted.comments if c["id"] != early["id"]]
        recon_shifted = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=_ScriptedTransport(pr_shifted.handle_wf),
                broker_app_transport=_ScriptedTransport(lambda c: (500, {}, {})),
            )
        ).reconcile_takeover_read_only(
            owner=OWNER,
            repo=REPO,
            repository_id=REPO_ID,
            pull_request=PR_NUMBER,
            slot_key=slot_key,
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            expected_app_id=APP_ID,
            expected_bot_login=BOT_LOGIN,
            operation_id="op-neg",
        )
        self.assertEqual(
            recon_shifted.reconciliation_outcome, "adopted_missing_receipt"
        )
        self.assertEqual(len(recon_shifted.adopted_effects), 1)
        self.assertEqual(recon_shifted.adopted_effects[0][1], 9301)

        # 3) Affirmative PR-resident NonSubmissionProofRecord -> proved_not_submitted -> issues ONCE.
        now = 1_764_200_000.0
        pr_empty.add_comment(
            format_v6_record_marker(
                NonSubmissionProofRecord(
                    record_id="non-sub:op-neg:1",
                    slot_key=slot_key,
                    generation=1,
                    operation_id="op-neg",
                    pr_head_sha=HEAD_A,
                    proof_kind="definitive_github_rejection",
                    payload_sha256=unresolved.payload_sha256,
                    recorded_at="2026-11-27T00:05:00Z",
                    http_status=422,
                    diagnostic="github_unprocessable_entity",
                )
            ),
            is_app=True,
        )
        coord_store = _SimulatedCoordinatorStore(clock=lambda: now, pr=pr_empty)
        coord_store.state = "RECONCILING"
        coord_store.generation = 1
        coord_store.lease_epoch = 1
        coord_store.token_cleared = True
        coord_client = V6CoordinatorClient(
            base_url="https://coordinator.invalid",
            oidc_token_provider=lambda: "oidc-jwt-token",
            opener=coord_store.opener,
            clock=lambda: now,
        )
        runner = V6PilotRunner(
            coordinator=coord_client,
            workflow_token_transport=_ScriptedTransport(pr_empty.handle_wf),
            app_transport_factory=lambda _g: _ScriptedTransport(pr_empty.handle_app),
            clock=lambda: now,
        )
        issued_once = runner.run(
            V6PilotRunConfig(
                owner=OWNER,
                repo=REPO,
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                head_sha=HEAD_A,
                operation_id="op-neg",
                run_id=5002,
                workflow_sha=WORKFLOW_SHA,
                expected_app_id=APP_ID,
                expected_bot_login=BOT_LOGIN,
                expected_bot_user_id=BOT_USER_ID,
            )
        )
        self.assertEqual(issued_once.status, "completed")
        self.assertTrue(issued_once.lease_released)
        self.assertEqual(issued_once.coordinator_state, "IDLE")
        self.assertEqual(len(pr_empty.reviews), 1)

    def test_row_9_competing_or_spoofed_owner_markers_fail_closed_with_step_summary(
        self,
    ) -> None:
        slot_key = build_v6_slot_key(
            host="github.com",
            app_id=APP_ID,
            installation_id=INSTALLATION_ID,
            base_repo_id=REPO_ID,
            pr_number=PR_NUMBER,
        )
        o1 = _make_owner_record(slot_key=slot_key, generation=1, lease_epoch=1)
        o1_competing = _make_owner_record(
            slot_key=slot_key,
            generation=1,
            lease_epoch=2,
            reservation_id="res-competing",
        )
        o2 = _make_owner_record(slot_key=slot_key, generation=2, lease_epoch=2)

        # Competing generation=1 rejected.
        with self.assertRaises(V6LedgerIntegrityError):
            validate_owner_lineage(
                [
                    {"body": format_v6_record_marker(o1), **_app_meta()},
                    {"body": format_v6_record_marker(o1_competing), **_app_meta()},
                ],
                slot_key=slot_key,
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                app_login=BOT_LOGIN,
            )

        # Missing predecessor generation=1 rejected.
        with self.assertRaises(V6LedgerIntegrityError):
            validate_owner_lineage(
                [{"body": format_v6_record_marker(o2), **_app_meta()}],
                slot_key=slot_key,
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                app_login=BOT_LOGIN,
            )

        # Non-App / human-authored owner marker rejected.
        with self.assertRaises(V6LedgerIntegrityError):
            validate_owner_lineage(
                [{"body": format_v6_record_marker(o1), **_human_meta()}],
                slot_key=slot_key,
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                app_login=BOT_LOGIN,
            )

        # MAC verifier rejection fails closed.
        with self.assertRaises(V6LedgerIntegrityError):
            validate_owner_lineage(
                [{"body": format_v6_record_marker(o1), **_app_meta()}],
                slot_key=slot_key,
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                app_login=BOT_LOGIN,
                mac_verifier=lambda _rec: False,
            )

        # End-to-end runner holds without mutating PR and writes sanitized step summary.
        now = 1_764_200_000.0
        pr = _SimulatedPRSurface()
        coord_store = _SimulatedCoordinatorStore(clock=lambda: now, pr=pr)
        coord_store.state = "RECONCILING"
        coord_store.generation = 1
        coord_store.token_cleared = True
        pr.add_comment(format_v6_record_marker(o1), is_app=True)
        pr.add_comment(format_v6_record_marker(o1_competing), is_app=True)
        before_comments = [dict(c) for c in pr.comments]

        summary_sink: list[str] = []
        app_calls: list[dict[str, Any]] = []
        runner = V6PilotRunner(
            coordinator=V6CoordinatorClient(
                base_url="https://coordinator.invalid",
                oidc_token_provider=lambda: "oidc-jwt-token",
                opener=coord_store.opener,
                clock=lambda: now,
            ),
            workflow_token_transport=_ScriptedTransport(pr.handle_wf),
            app_transport_factory=lambda _g: _ScriptedTransport(
                lambda c: app_calls.append(c) or pr.handle_app(c)
            ),
            clock=lambda: now,
        )
        outcome = runner.run(
            V6PilotRunConfig(
                owner=OWNER,
                repo=REPO,
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                head_sha=HEAD_A,
                operation_id="op-row9",
                workflow_sha=WORKFLOW_SHA,
                expected_app_id=APP_ID,
                expected_bot_login=BOT_LOGIN,
                step_summary_sink=summary_sink.append,
            )
        )
        self.assertEqual(outcome.status, "hold")
        self.assertEqual(outcome.coordinator_state, "HOLD")
        self.assertEqual(pr.comments, before_comments)
        self.assertEqual(app_calls, [])
        self.assertTrue(summary_sink)
        self.assertIn("v6_competing_owner_marker", summary_sink[0])

    def test_row_10_rollback_during_uncertain_write_preserves_ledger_and_obligations(
        self,
    ) -> None:
        now = 1_764_200_000.0
        pr = _SimulatedPRSurface()
        coord_store = _SimulatedCoordinatorStore(clock=lambda: now, pr=pr)
        unresolved = _make_unresolved(operation_id="op-rb")
        pr.add_comment(format_v6_record_marker(unresolved), is_app=True)

        def app_uncertain_and_rollback(
            call: dict[str, Any],
        ) -> tuple[int, Mapping[str, str], Any]:
            if (
                call["method"] == "POST"
                and f"/pulls/{PR_NUMBER}/reviews" in call["path"]
            ):
                # Flip coordinator rollback block mid-flight and drop write response!
                coord_store.rollback_blocked = True
                raise TimeoutError("dropped review response during rollback")
            return pr.handle_app(call)

        summary_sink: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            outcome_path = Path(tmp) / "outcome.json"
            runner = V6PilotRunner(
                coordinator=V6CoordinatorClient(
                    base_url="https://coordinator.invalid",
                    oidc_token_provider=lambda: "oidc-jwt-token",
                    opener=coord_store.opener,
                    clock=lambda: now,
                ),
                workflow_token_transport=_ScriptedTransport(pr.handle_wf),
                app_transport_factory=lambda _g: _ScriptedTransport(
                    app_uncertain_and_rollback
                ),
                clock=lambda: now,
            )
            outcome = runner.run(
                V6PilotRunConfig(
                    owner=OWNER,
                    repo=REPO,
                    repository_id=REPO_ID,
                    pull_request=PR_NUMBER,
                    head_sha=HEAD_A,
                    operation_id="op-rb",
                    run_id=5002,
                    workflow_sha=WORKFLOW_SHA,
                    expected_app_id=APP_ID,
                    expected_bot_login=BOT_LOGIN,
                    step_summary_sink=summary_sink.append,
                    outcome_path=outcome_path,
                )
            )
            self.assertEqual(outcome.status, "hold")
            self.assertFalse(outcome.lease_released)
            self.assertEqual(outcome.model_calls_started, 0)
            self.assertTrue(outcome_path.is_file())
            written_json = outcome_path.read_text(encoding="utf-8")
            self.assertNotIn("ghs_", written_json)

        # PR ledger preserved: unresolved + owner + intent + durable hold all intact, 0 receipts cleared.
        records = [
            rec
            for c in pr.comments
            for rec in extract_v6_record_markers(c["body"], strict=False)
        ]
        self.assertTrue(any(isinstance(r, UnresolvedResultRecord) for r in records))
        self.assertTrue(any(isinstance(r, WriteIntentRecord) for r in records))
        self.assertTrue(any(isinstance(r, DurableHoldRecord) for r in records))
        summary = reconcile_immutable_records(
            [r for r in records if isinstance(r, WriteIntentRecord)],
            [r for r in records if isinstance(r, CompletionReceiptRecord)],
        )
        self.assertEqual(len(summary.unreconciled_intents), 1)
        self.assertTrue(summary_sink)

    def test_cli_v6_pilot_run_and_review_run_opt_in_preserve_v5_default(self) -> None:
        import time
        from unittest.mock import patch

        now = time.time()
        pr = _SimulatedPRSurface()
        coord_store = _SimulatedCoordinatorStore(clock=lambda: now, pr=pr)
        pr.add_comment(
            format_v6_record_marker(_make_unresolved(operation_id="op-cli")),
            is_app=True,
        )

        with (
            patch.dict(
                "os.environ",
                {
                    "GITHUB_TOKEN": "ghs_wf_readonly_token_xyz",
                    "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "oidc-req-token",
                    "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.invalid/token",
                    "GITHUB_RUN_ID": "5002",
                    "GITHUB_RUN_ATTEMPT": "1",
                },
                clear=False,
            ),
            patch(
                "review_sensei_github.v6_pilot_runner.V6CoordinatorClient",
                return_value=V6CoordinatorClient(
                    base_url="https://coordinator.invalid",
                    oidc_token_provider=lambda: "oidc-jwt-token",
                    opener=coord_store.opener,
                    clock=lambda: now,
                ),
            ),
            patch(
                "review_sensei_github.v6_pilot_runner.UrllibGitHubTransport",
                side_effect=lambda **kw: _ScriptedTransport(
                    pr.handle_wf if kw["token"].startswith("ghs_wf_") else pr.handle_app
                ),
            ),
            redirect_stdout(io.StringIO()) as stdout_buf,
        ):
            code = github_main(
                [
                    "v6-pilot-run",
                    "--repository",
                    f"{OWNER}/{REPO}",
                    "--repository-id",
                    str(REPO_ID),
                    "--pull-request",
                    str(PR_NUMBER),
                    "--head-sha",
                    HEAD_A,
                    "--operation-id",
                    "op-cli",
                    "--workflow-sha",
                    WORKFLOW_SHA,
                ]
            )
        self.assertEqual(code, 0)
        parsed = json.loads(stdout_buf.getvalue())
        self.assertEqual(parsed["protocol"], "v6")
        self.assertEqual(parsed["status"], "completed")
        self.assertNotIn("ghs_", stdout_buf.getvalue())

        # Verify review-run defaults to v5 and routes --recovery-protocol v6 cleanly.
        with (
            patch(
                "review_sensei_github.v6_pilot_runner.run_v6_pilot_cli",
                return_value=0,
            ) as mock_v6_cli,
            tempfile.TemporaryDirectory() as tmp,
        ):
            rc = github_main(
                [
                    "review-run",
                    "--policy-commit",
                    WORKFLOW_SHA,
                    "--run-directory",
                    tmp,
                    "--publication-outcome",
                    str(Path(tmp) / "out.json"),
                    "--recovery-protocol",
                    "v6",
                    "--",
                    "--repository",
                    f"{OWNER}/{REPO}",
                    "--repository-id",
                    str(REPO_ID),
                    "--pull-request",
                    str(PR_NUMBER),
                    "--head-sha",
                    HEAD_A,
                ]
            )
        self.assertEqual(rc, 0)
        mock_v6_cli.assert_called_once()

    def test_key_two_gate_adopted_missing_receipt_and_clean_release(self) -> None:
        """The Key Test: Gen 1 lands review 5001, dies before receipt; replacement adopts + attaches receipt ONCE."""

        now = 1_764_200_000.0
        pr = _SimulatedPRSurface(head_sha=HEAD_A)
        coord_store = _SimulatedCoordinatorStore(clock=lambda: now, pr=pr)
        unresolved = _make_unresolved(operation_id="op-1")
        pr.add_comment(format_v6_record_marker(unresolved), is_app=True)

        coord_client = V6CoordinatorClient(
            base_url="https://coordinator.invalid",
            oidc_token_provider=lambda: "oidc-jwt-token",
            opener=coord_store.opener,
            clock=lambda: now,
        )

        # Gen 1 acquires token, writes OwnerMarkerRecord + WriteIntentRecord (what, where, generation=1),
        # GitHub accepts review 5001, and Gen 1 crashes BEFORE writing CompletionReceiptRecord!
        grant_1 = coord_client.acquire(
            pull_request=PR_NUMBER,
            head_sha=HEAD_A,
            capabilities=("review_publish",),
            operation_id="op-1",
        )
        self.assertEqual(grant_1.generation, 1)
        owner_1 = _make_owner_record(
            slot_key=grant_1.slot_key,
            generation=1,
            lease_epoch=grant_1.lease_epoch,
            reservation_id=grant_1.reservation_id,
            operation_id="op-1",
            pr_head_sha=HEAD_A,
        )
        owner_1_c = pr.add_comment(format_v6_record_marker(owner_1), is_app=True)
        coord_store.owner_comment_id = owner_1_c["id"]

        intent_1 = WriteIntentRecord(
            record_id="intent:op-1:1",
            operation_id="op-1",
            writer_epoch=grant_1.lease_epoch,
            pr_head_sha=HEAD_A,
            effect_kind="pr_review_publish",
            payload_sha256=unresolved.payload_sha256,
            target_digest=canonical_sha256(
                {"repo": REPO_ID, "pr": PR_NUMBER, "head": HEAD_A}
            ),
            generation=1,
            target_location=f"/repos/{OWNER}/{REPO}/pulls/{PR_NUMBER}/reviews",
        )
        intent_1_c = pr.add_comment(format_v6_record_marker(intent_1), is_app=True)
        pr.add_review(
            f"Gen 1 review\n{_effect_tag('op-1', unresolved.payload_sha256)}",
            commit_id=HEAD_A,
            review_id=5001,
        )

        # Worker revokes old token (204) -> slot moves to RECONCILING (token_cleared=True), NEVER IDLE!
        drain_res = coord_client.drain(
            slot_key=grant_1.slot_key,
            expected_generation=1,
            expected_lease_epoch=grant_1.lease_epoch,
            predecessor_anchor_comment_id=owner_1_c["id"],
        )
        self.assertEqual(drain_res.state, "RECONCILING")
        self.assertTrue(drain_res.token_cleared)

        # /acquire MUST fail while slot is RECONCILING!
        with self.assertRaises(V6CoordinatorError):
            coord_client.acquire(
                pull_request=PR_NUMBER,
                head_sha=HEAD_A,
                capabilities=("review_publish",),
                operation_id="op-1",
            )

        # Bare /complete-reconcile without covering intent_1 MUST fail (409 v6_lease_reconciliation_evidence_invalid) -> HOLD.
        bare_store = _SimulatedCoordinatorStore(clock=lambda: now, pr=pr)
        bare_store.state = "RECONCILING"
        bare_store.generation = 1
        bare_store.lease_epoch = 1
        bare_store.token_cleared = True
        bare_client = V6CoordinatorClient(
            base_url="https://coordinator.invalid",
            oidc_token_provider=lambda: "oidc-jwt-token",
            opener=bare_store.opener,
            clock=lambda: now,
        )
        with self.assertRaises(V6CoordinatorError) as bad_ev:
            bare_client.complete_reconcile(
                slot_key=grant_1.slot_key,
                generation=1,
                reconciliation_outcome="clean_receipts",
                reconciliation_evidence={"old_generation": 1, "intent_resolutions": []},
            )
        self.assertEqual(
            bad_ev.exception.diagnostic, "v6_lease_reconciliation_evidence_invalid"
        )
        self.assertEqual(bare_store.state, "HOLD")

        # Fresh replacement runner (zero artifacts, zero local state, read-only GITHUB_TOKEN)
        # finds review 5001, passes Gate 2 -> acquires Gen 2 -> writes Gen 2 owner + receipt 1x -> Gate 1 + Gate 2 -> IDLE!
        wf_tr = _ScriptedTransport(pr.handle_wf)
        app_tr = _ScriptedTransport(pr.handle_app)
        runner = V6PilotRunner(
            coordinator=coord_client,
            workflow_token_transport=wf_tr,
            app_transport_factory=lambda _g: app_tr,
            clock=lambda: now,
        )
        outcome = runner.run(
            V6PilotRunConfig(
                owner=OWNER,
                repo=REPO,
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                head_sha=HEAD_A,
                operation_id="op-1",
                run_id=5002,
                workflow_sha=WORKFLOW_SHA,
                expected_app_id=APP_ID,
                expected_bot_login=BOT_LOGIN,
                expected_bot_user_id=BOT_USER_ID,
            )
        )
        self.assertEqual(outcome.status, "completed")
        self.assertTrue(outcome.lease_released)
        self.assertEqual(outcome.coordinator_state, "IDLE")
        self.assertEqual(outcome.generation, 2)
        self.assertEqual(outcome.model_calls_started, 0)
        self.assertEqual(coord_store.worker_pr_writes, 0)
        assert isinstance(outcome.recovery_outcome, RecoveryCompleted)
        self.assertTrue(outcome.recovery_outcome.reconciled_from_existing_effect)
        self.assertEqual(outcome.recovery_outcome.github_effect_id, 5001)

        # Strictly 1 native review (5001), 0 duplicate reviews!
        self.assertEqual([r["id"] for r in pr.reviews], [5001])
        # Strictly 1 CompletionReceiptRecord for op-1!
        receipts = [
            rec
            for c in pr.comments
            for rec in extract_v6_record_markers(c["body"], strict=False)
            if isinstance(rec, CompletionReceiptRecord) and rec.operation_id == "op-1"
        ]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0].github_effect_id, 5001)

        # Verify Gen 2 coordinator transitions: complete-reconcile (adopted_missing_receipt) -> acquire -> release -> complete-reconcile (clean_receipts)!
        recon_calls = [
            payload
            for route, payload in coord_store.calls
            if route == "/github/v6/lease/complete-reconcile"
        ]
        self.assertEqual(len(recon_calls), 2)
        self.assertEqual(
            recon_calls[0]["reconciliation_outcome"], "adopted_missing_receipt"
        )
        self.assertEqual(recon_calls[0]["reconciliation_evidence"]["old_generation"], 1)
        self.assertEqual(
            recon_calls[0]["reconciliation_evidence"]["old_owner_comment_id"],
            owner_1_c["id"],
        )
        self.assertEqual(
            recon_calls[0]["reconciliation_evidence"]["intent_resolutions"],
            [
                {
                    "operation_id": "op-1",
                    "intent_comment_id": intent_1_c["id"],
                    "resolution": "adopted_effect",
                    "native_effect_kind": "pull_request_review",
                    "native_effect_id": 5001,
                }
            ],
        )
        self.assertEqual(recon_calls[1]["reconciliation_outcome"], "clean_receipts")
        self.assertEqual(recon_calls[1]["reconciliation_evidence"]["old_generation"], 2)
        self.assertEqual(
            recon_calls[1]["reconciliation_evidence"]["intent_resolutions"], []
        )

    def test_two_gate_empty_search_without_non_submission_proof_holds_no_token(
        self,
    ) -> None:
        """Empty search without PR-resident NonSubmissionProofRecord MUST move to HOLD with NO token."""

        now = 1_764_200_000.0
        pr = _SimulatedPRSurface(head_sha=HEAD_A)
        coord_store = _SimulatedCoordinatorStore(clock=lambda: now, pr=pr)
        slot_key = coord_store.slot_key

        coord_store.state = "ACTIVE"
        coord_store.generation = 1
        coord_store.lease_epoch = 1
        coord_store.reservation_id = "res-1-1"
        owner_1_c = pr.add_comment(
            format_v6_record_marker(
                _make_owner_record(slot_key=slot_key, generation=1, lease_epoch=1)
            ),
            is_app=True,
        )
        coord_store.owner_comment_id = owner_1_c["id"]
        unresolved = _make_unresolved(operation_id="op-empty")
        pr.add_comment(format_v6_record_marker(unresolved), is_app=True)
        pr.add_comment(
            format_v6_record_marker(
                WriteIntentRecord(
                    record_id="intent:op-empty:1",
                    operation_id="op-empty",
                    writer_epoch=1,
                    pr_head_sha=HEAD_A,
                    effect_kind="pr_review_publish",
                    payload_sha256=unresolved.payload_sha256,
                    target_digest="td-1",
                    generation=1,
                    target_location=f"/repos/{OWNER}/{REPO}/pulls/{PR_NUMBER}/reviews",
                )
            ),
            is_app=True,
        )

        app_calls: list[dict[str, Any]] = []
        summary_lines: list[str] = []
        runner = V6PilotRunner(
            coordinator=V6CoordinatorClient(
                base_url="https://coordinator.invalid",
                oidc_token_provider=lambda: "oidc-jwt-token",
                opener=coord_store.opener,
                clock=lambda: now,
            ),
            workflow_token_transport=_ScriptedTransport(pr.handle_wf),
            app_transport_factory=lambda _g: _ScriptedTransport(
                lambda c: app_calls.append(c) or pr.handle_app(c)
            ),
            clock=lambda: now,
        )
        outcome = runner.run(
            V6PilotRunConfig(
                owner=OWNER,
                repo=REPO,
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                head_sha=HEAD_A,
                operation_id="op-empty",
                expected_app_id=APP_ID,
                expected_bot_login=BOT_LOGIN,
                affirmative_non_submission=True,
                step_summary_sink=summary_lines.append,
            )
        )
        self.assertEqual(outcome.status, "hold")
        self.assertEqual(outcome.coordinator_state, "HOLD")
        self.assertEqual(coord_store.state, "HOLD")
        self.assertEqual(app_calls, [])
        self.assertFalse(
            any(r == "/github/v6/lease/acquire" for r, _ in coord_store.calls)
        )
        self.assertTrue(summary_lines)
        self.assertEqual(pr.reviews, [])

    def test_clean_exit_writes_receipt_before_release_then_gate2_clean_receipts(
        self,
    ) -> None:
        now = 1_764_200_000.0
        pr = _SimulatedPRSurface(head_sha=HEAD_A)
        coord_store = _SimulatedCoordinatorStore(clock=lambda: now, pr=pr)
        unresolved = _make_unresolved(operation_id="op-clean")
        pr.add_comment(format_v6_record_marker(unresolved), is_app=True)

        events: list[str] = []
        orig_app = pr.handle_app
        orig_opener = coord_store.opener

        def trace_app(call: dict[str, Any]) -> tuple[int, Mapping[str, str], Any]:
            if call["method"] == "POST":
                body_str = str((call.get("body") or {}).get("body", ""))
                if "reviewsensei:v6:owner:v1" in body_str:
                    events.append("post_owner")
                elif "reviewsensei:v6:intent:v1" in body_str:
                    events.append("post_intent")
                elif "reviewsensei:v6:receipt:v1" in body_str:
                    events.append("post_receipt")
                elif "/reviews" in call["path"]:
                    events.append("post_review")
            return orig_app(call)

        def trace_coord(req: Any, timeout: float | None = None) -> _FakeUrllibResponse:
            events.append(f"coord:{urlsplit(req.full_url).path.rsplit('/', 1)[-1]}")
            return orig_opener(req, timeout=timeout)

        runner = V6PilotRunner(
            coordinator=V6CoordinatorClient(
                base_url="https://coordinator.invalid",
                oidc_token_provider=lambda: "oidc-jwt-token",
                opener=trace_coord,
                clock=lambda: now,
            ),
            workflow_token_transport=_ScriptedTransport(pr.handle_wf),
            app_transport_factory=lambda _g: _ScriptedTransport(trace_app),
            clock=lambda: now,
        )
        outcome = runner.run(
            V6PilotRunConfig(
                owner=OWNER,
                repo=REPO,
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                head_sha=HEAD_A,
                operation_id="op-clean",
                run_id=5002,
                workflow_sha=WORKFLOW_SHA,
                expected_app_id=APP_ID,
                expected_bot_login=BOT_LOGIN,
            )
        )
        self.assertEqual(outcome.status, "completed")
        self.assertTrue(outcome.lease_released)
        self.assertEqual(outcome.coordinator_state, "IDLE")

        # Strict ordering: post_owner -> post_intent -> post_review -> post_receipt BEFORE coord:release -> coord:complete-reconcile!
        self.assertLess(events.index("post_owner"), events.index("post_intent"))
        self.assertLess(events.index("post_intent"), events.index("post_review"))
        self.assertLess(events.index("post_review"), events.index("post_receipt"))
        self.assertLess(events.index("post_receipt"), events.index("coord:release"))
        self.assertLess(
            events.index("coord:release"), events.index("coord:complete-reconcile")
        )

        # Intent written MUST have generation + target_location!
        intents = [
            rec
            for c in pr.comments
            for rec in extract_v6_record_markers(c["body"], strict=True)
            if isinstance(rec, WriteIntentRecord)
        ]
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].generation, 1)
        self.assertEqual(
            intents[0].target_location,
            f"/repos/{OWNER}/{REPO}/pulls/{PR_NUMBER}/reviews",
        )

        # Verify Gate 2 clean_receipts evidence referenced the created owner/intent/receipt comments.
        recon_calls = [
            p
            for r, p in coord_store.calls
            if r == "/github/v6/lease/complete-reconcile"
        ]
        self.assertEqual(len(recon_calls), 1)
        self.assertEqual(recon_calls[0]["reconciliation_outcome"], "clean_receipts")
        ev = recon_calls[0]["reconciliation_evidence"]
        self.assertEqual(ev["old_generation"], 1)
        self.assertEqual(len(ev["intent_resolutions"]), 1)
        self.assertEqual(ev["intent_resolutions"][0]["resolution"], "receipt_present")

    def test_two_gate_422_rejection_writes_non_submission_proof_and_successor_publishes_once(
        self,
    ) -> None:
        now = 1_764_200_000.0
        pr = _SimulatedPRSurface(head_sha=HEAD_A)
        coord_store = _SimulatedCoordinatorStore(clock=lambda: now, pr=pr)
        unresolved = _make_unresolved(operation_id="op-422")
        pr.add_comment(format_v6_record_marker(unresolved), is_app=True)

        reject_next_review = True

        def app_422_first_time(
            call: dict[str, Any],
        ) -> tuple[int, Mapping[str, str], Any]:
            nonlocal reject_next_review
            if (
                reject_next_review
                and call["method"] == "POST"
                and f"/pulls/{PR_NUMBER}/reviews" in call["path"]
            ):
                reject_next_review = False
                return (
                    422,
                    {"X-RateLimit-Remaining": "4990"},
                    {"message": "Unprocessable Entity"},
                )
            return pr.handle_app(call)

        coord_client = V6CoordinatorClient(
            base_url="https://coordinator.invalid",
            oidc_token_provider=lambda: "oidc-jwt-token",
            opener=coord_store.opener,
            clock=lambda: now,
        )
        runner = V6PilotRunner(
            coordinator=coord_client,
            workflow_token_transport=_ScriptedTransport(pr.handle_wf),
            app_transport_factory=lambda _g: _ScriptedTransport(app_422_first_time),
            clock=lambda: now,
        )
        first_run = runner.run(
            V6PilotRunConfig(
                owner=OWNER,
                repo=REPO,
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                head_sha=HEAD_A,
                operation_id="op-422",
                run_id=5002,
                workflow_sha=WORKFLOW_SHA,
                expected_app_id=APP_ID,
                expected_bot_login=BOT_LOGIN,
            )
        )
        self.assertEqual(first_run.status, "hold")
        self.assertEqual(first_run.diagnostic, "definitive_github_rejection_422")
        self.assertEqual(coord_store.state, "RECONCILING")
        self.assertTrue(coord_store.token_cleared)
        proofs = [
            rec
            for c in pr.comments
            for rec in extract_v6_record_markers(c["body"], strict=True)
            if isinstance(rec, NonSubmissionProofRecord)
        ]
        self.assertEqual(len(proofs), 1)
        self.assertEqual(proofs[0].schema, NON_SUBMISSION_SCHEMA)
        self.assertEqual(proofs[0].proof_kind, "definitive_github_rejection")
        self.assertEqual(proofs[0].http_status, 422)

        # Successor takes over from RECONCILING -> Gate 2 (proved_not_submitted) -> IDLE -> acquires Gen 2 -> publishes ONCE -> Gate 1 + Gate 2 -> IDLE!
        second_run = runner.run(
            V6PilotRunConfig(
                owner=OWNER,
                repo=REPO,
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                head_sha=HEAD_A,
                operation_id="op-422",
                run_id=5002,
                workflow_sha=WORKFLOW_SHA,
                expected_app_id=APP_ID,
                expected_bot_login=BOT_LOGIN,
            )
        )
        self.assertEqual(second_run.status, "completed")
        self.assertTrue(second_run.lease_released)
        self.assertEqual(second_run.coordinator_state, "IDLE")
        self.assertEqual(second_run.generation, 2)
        self.assertEqual(len(pr.reviews), 1)
        assert second_run.takeover_reconciliation is not None
        self.assertEqual(
            second_run.takeover_reconciliation.reconciliation_outcome,
            "proved_not_submitted",
        )

    def test_two_gate_ledger_and_coordinator_client_validation(self) -> None:
        slot_key = build_v6_slot_key(
            host="github.com",
            app_id=APP_ID,
            installation_id=INSTALLATION_ID,
            base_repo_id=REPO_ID,
            pr_number=PR_NUMBER,
        )
        intent_ext = WriteIntentRecord(
            record_id="intent:ext:1",
            operation_id="op-ext",
            writer_epoch=1,
            pr_head_sha=HEAD_A,
            effect_kind="pr_review_publish",
            payload_sha256="1" * 64,
            target_digest="t1",
            generation=1,
            target_location=f"/repos/{OWNER}/{REPO}/pulls/{PR_NUMBER}/reviews",
        )
        non_sub = NonSubmissionProofRecord(
            record_id="ns:ext:1",
            slot_key=slot_key,
            generation=1,
            operation_id="op-ext",
            pr_head_sha=HEAD_A,
            proof_kind="pre_dispatch_terminal_abort",
            payload_sha256="1" * 64,
            recorded_at="2026-11-27T00:00:00Z",
            http_status=None,
            diagnostic="preflight_aborted-->test",
        )
        for rec in (intent_ext, non_sub):
            marker = format_v6_record_marker(rec)
            self.assertNotIn("-->", marker[:-4])
            self.assertEqual(parse_v6_record_marker(marker), rec)
            self.assertEqual(extract_v6_record_markers(marker, strict=True), (rec,))

        with self.assertRaises(Exception):
            WriteIntentRecord(
                record_id="bad-gen",
                operation_id="op",
                writer_epoch=1,
                pr_head_sha=HEAD_A,
                effect_kind="pr_review_publish",
                payload_sha256="1" * 64,
                target_digest="t1",
                generation=0,
            )
        with self.assertRaises(Exception):
            WriteIntentRecord(
                record_id="bad-loc",
                operation_id="op",
                writer_epoch=1,
                pr_head_sha=HEAD_A,
                effect_kind="pr_review_publish",
                payload_sha256="1" * 64,
                target_digest="t1",
                target_location="/bad\nline",
            )

        now = 1_764_200_000.0
        coord_store = _SimulatedCoordinatorStore(clock=lambda: now)
        coord_client = V6CoordinatorClient(
            base_url="https://coordinator.invalid",
            oidc_token_provider=lambda: "oidc-jwt-token",
            opener=coord_store.opener,
            clock=lambda: now,
        )
        with self.assertRaises(V6CoordinatorError):
            coord_client.complete_reconcile(
                slot_key=slot_key,
                generation=1,
                reconciliation_outcome="clean_receipts",
                reconciliation_evidence={
                    "old_generation": 1,
                    "intent_resolutions": [
                        {
                            "operation_id": "op-1",
                            "intent_comment_id": 101,
                            "resolution": "receipt_present",
                        }
                    ],
                },
            )
        with self.assertRaises(V6CoordinatorError):
            coord_client.complete_reconcile(
                slot_key=slot_key,
                generation=1,
                reconciliation_outcome="adopted_missing_receipt",
                reconciliation_evidence={
                    "old_generation": 1,
                    "intent_resolutions": [
                        {
                            "operation_id": "op-1",
                            "intent_comment_id": 101,
                            "resolution": "adopted_effect",
                        }
                    ],
                },
            )
        with self.assertRaises(V6CoordinatorError) as bad_eff_kind:
            coord_client.complete_reconcile(
                slot_key=slot_key,
                generation=1,
                reconciliation_outcome="adopted_missing_receipt",
                reconciliation_evidence={
                    "old_generation": 1,
                    "intent_resolutions": [
                        {
                            "operation_id": "op-1",
                            "intent_comment_id": 101,
                            "resolution": "adopted_effect",
                            "native_effect_kind": "pr_review_publish",
                            "native_effect_id": 5001,
                        }
                    ],
                },
            )
        self.assertEqual(
            bad_eff_kind.exception.diagnostic,
            "v6_lease_reconciliation_evidence_invalid",
        )

        # Mismatched review commit_id must fail Worker verification -> HOLD.
        pr_mismatch = _SimulatedPRSurface(head_sha=HEAD_A)
        mismatch_payload_sha = canonical_sha256({"body": "mismatch"})
        mismatch_intent = WriteIntentRecord(
            record_id="intent:op-mismatch:1",
            operation_id="op-mismatch",
            writer_epoch=1,
            pr_head_sha=HEAD_A,
            effect_kind="pr_review_publish",
            payload_sha256=mismatch_payload_sha,
            target_digest="t-mismatch",
            generation=1,
            target_location=f"/repos/{OWNER}/{REPO}/pulls/{PR_NUMBER}/reviews",
        )
        mismatch_ic = pr_mismatch.add_comment(
            format_v6_record_marker(mismatch_intent), is_app=True
        )
        pr_mismatch.add_review(
            f"Wrong head review\n{_effect_tag('op-mismatch', mismatch_payload_sha)}",
            commit_id=HEAD_B,
            review_id=5099,
        )
        mismatch_store = _SimulatedCoordinatorStore(clock=lambda: now, pr=pr_mismatch)
        mismatch_store.state = "RECONCILING"
        mismatch_store.generation = 1
        mismatch_store.lease_epoch = 1
        mismatch_store.token_cleared = True
        mismatch_client = V6CoordinatorClient(
            base_url="https://coordinator.invalid",
            oidc_token_provider=lambda: "oidc-jwt-token",
            opener=mismatch_store.opener,
            clock=lambda: now,
        )
        with self.assertRaises(V6CoordinatorError) as bad_head_rev:
            mismatch_client.complete_reconcile(
                slot_key=slot_key,
                generation=1,
                reconciliation_outcome="adopted_missing_receipt",
                reconciliation_evidence={
                    "old_generation": 1,
                    "intent_resolutions": [
                        {
                            "operation_id": "op-mismatch",
                            "intent_comment_id": mismatch_ic["id"],
                            "resolution": "adopted_effect",
                            "native_effect_kind": "pull_request_review",
                            "native_effect_id": 5099,
                        }
                    ],
                },
            )
        self.assertEqual(
            bad_head_rev.exception.diagnostic,
            "v6_lease_reconciliation_evidence_invalid",
        )
        self.assertEqual(mismatch_store.state, "HOLD")
        with self.assertRaises(V6CoordinatorError):
            coord_client.complete_reconcile(
                slot_key=slot_key,
                generation=1,
                reconciliation_outcome="proved_not_submitted",
                reconciliation_evidence={
                    "old_generation": 1,
                    "intent_resolutions": [
                        {
                            "operation_id": "op-1",
                            "intent_comment_id": 101,
                            "resolution": "proved_not_submitted",
                        }
                    ],
                },
            )

    def test_capabilities_wire_parity_and_slot_state_error_parsing(self) -> None:
        """Client and simulated coordinator share v6 capability rules and parse string/dict slot_state."""

        from types import MappingProxyType
        from urllib.request import Request

        self.assertEqual(
            review_sensei_github.ALLOWED_V6_CAPABILITIES, ALLOWED_V6_CAPABILITIES
        )
        self.assertEqual(
            review_sensei_github.ALLOWED_V6_PERMISSION_NAMES,
            ALLOWED_V6_PERMISSION_NAMES,
        )
        self.assertEqual(
            V6PilotRunConfig(
                owner=OWNER,
                repo=REPO,
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                head_sha=HEAD_A,
                operation_id="op-default",
            ).capabilities,
            ("review_publish",),
        )

        now = 1_764_200_000.0
        coord_store = _SimulatedCoordinatorStore(clock=lambda: now)
        coord_client = V6CoordinatorClient(
            base_url="https://coordinator.invalid",
            oidc_token_provider=lambda: "oidc-jwt-token",
            opener=coord_store.opener,
            clock=lambda: now,
        )

        # Client rejects legacy scope strings and malformed mappings before sending HTTP.
        for bad_caps in (
            ("pull_requests:write",),
            ("pull_requests:write", "issues:write"),
            ("review_publish", "review_publish"),
            (),
            {},
            {"pull_requests": "admin"},
            {"unknown_perm": "read"},
            "review_publish",
        ):
            with self.assertRaises(V6CoordinatorError) as bad_ctx:
                coord_client.acquire(
                    pull_request=PR_NUMBER,
                    head_sha=HEAD_A,
                    capabilities=bad_caps,  # type: ignore[arg-type]
                )
            self.assertEqual(bad_ctx.exception.diagnostic, "broker_capability_invalid")
        self.assertEqual(coord_store.calls, [])

        # Simulated coordinator store independently enforces the same capability wire contract.
        raw_req = Request(
            "https://coordinator.invalid/github/v6/lease/acquire",
            data=json.dumps(
                {
                    "pull_request": PR_NUMBER,
                    "head_sha": HEAD_A,
                    "capabilities": ["pull_requests:write"],
                }
            ).encode("utf-8"),
            method="POST",
        )
        with self.assertRaises(HTTPError) as store_bad:
            coord_store.opener(raw_req)
        self.assertEqual(store_bad.exception.code, 400)
        parsed_err = V6CoordinatorClient._parse_http_error(
            store_bad.exception, correlation_id=None
        )
        self.assertEqual(parsed_err.error, "broker_capability_invalid")
        self.assertEqual(parsed_err.diagnostic, "broker_capability_invalid")
        self.assertEqual(parsed_err.slot_state, MappingProxyType({"state": "IDLE"}))

        # Client + store accept both v6 capability list and GitHub permission mapping.
        grant_list = coord_client.acquire(
            pull_request=PR_NUMBER,
            head_sha=HEAD_A,
            capabilities=("review_publish",),
            operation_id="op-list",
        )
        self.assertEqual(grant_list.generation, 1)
        self.assertEqual(coord_store.calls[-1][1]["capabilities"], ["review_publish"])

        map_store = _SimulatedCoordinatorStore(clock=lambda: now)
        map_client = V6CoordinatorClient(
            base_url="https://coordinator.invalid",
            oidc_token_provider=lambda: "oidc-jwt-token",
            opener=map_store.opener,
            clock=lambda: now,
        )
        grant_map = map_client.acquire(
            pull_request=PR_NUMBER,
            head_sha=HEAD_A,
            capabilities={"contents": "read", "pull_requests": "write"},
            operation_id="op-map",
        )
        self.assertEqual(grant_map.generation, 1)
        self.assertEqual(
            map_store.calls[-1][1]["capabilities"],
            {"contents": "read", "pull_requests": "write"},
        )

        # String slot_state ("HOLD") from real Worker diagnosticResponse normalizes cleanly.
        hold_store = _SimulatedCoordinatorStore(clock=lambda: now)
        hold_store.state = "HOLD"
        hold_client = V6CoordinatorClient(
            base_url="https://coordinator.invalid",
            oidc_token_provider=lambda: "oidc-jwt-token",
            opener=hold_store.opener,
            clock=lambda: now,
        )
        with self.assertRaises(V6CoordinatorError) as hold_ctx:
            hold_client.acquire(
                pull_request=PR_NUMBER,
                head_sha=HEAD_A,
                capabilities=("review_publish",),
            )
        self.assertIsNotNone(hold_ctx.exception.slot_state)
        assert hold_ctx.exception.slot_state is not None
        self.assertEqual(hold_ctx.exception.slot_state["state"], "HOLD")
        self.assertEqual(
            hold_ctx.exception.slot_state, MappingProxyType({"state": "HOLD"})
        )

        # Dict slot_state backwards compatibility remains intact.
        dict_err = HTTPError(
            "https://coordinator.invalid",
            409,
            "v6_lease_hold",
            {},  # type: ignore[arg-type]
            io.BytesIO(
                json.dumps(
                    {
                        "error": "v6_lease_hold",
                        "diagnostic": {"code": "v6_lease_hold"},
                        "slot_state": {
                            "slot_key": hold_store.slot_key,
                            "state": "HOLD",
                        },
                    }
                ).encode("utf-8")
            ),
        )
        parsed_dict_err = V6CoordinatorClient._parse_http_error(
            dict_err, correlation_id=None
        )
        assert parsed_dict_err.slot_state is not None
        self.assertEqual(parsed_dict_err.slot_state["state"], "HOLD")
        self.assertEqual(parsed_dict_err.slot_state["slot_key"], hold_store.slot_key)

    def test_fresh_pr_initial_result_loader_persists_result_before_intent_and_reports_one_model_call(
        self,
    ) -> None:
        now = 1_764_200_000.0
        pr = _SimulatedPRSurface(head_sha=HEAD_A)
        coord_store = _SimulatedCoordinatorStore(clock=lambda: now, pr=pr)
        coord_client = V6CoordinatorClient(
            base_url="https://coordinator.invalid",
            oidc_token_provider=lambda: "oidc-jwt-token",
            opener=coord_store.opener,
            clock=lambda: now,
        )
        loader_calls = 0

        def _load_initial() -> tuple[list[dict[str, Any]], list[str]]:
            nonlocal loader_calls
            loader_calls += 1
            return (
                [
                    {
                        "path": "src/app.py",
                        "line": 10,
                        "body": "Initial fresh-PR finding.",
                    }
                ],
                ["diff_sha256:" + ("e" * 64)],
            )

        runner = V6PilotRunner(
            coordinator=coord_client,
            workflow_token_transport=_ScriptedTransport(pr.handle_wf),
            app_transport_factory=lambda _g: _ScriptedTransport(pr.handle_app),
            clock=lambda: now,
        )
        outcome = runner.run(
            V6PilotRunConfig(
                owner=OWNER,
                repo=REPO,
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                head_sha=HEAD_A,
                operation_id="op-fresh-1",
                run_id=5002,
                workflow_sha=WORKFLOW_SHA,
                expected_bot_login=BOT_LOGIN,
                expected_bot_user_id=BOT_USER_ID,
                initial_result_loader=_load_initial,
            )
        )
        self.assertEqual(loader_calls, 1)
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.model_calls_started, 1)
        self.assertTrue(outcome.lease_released)
        self.assertEqual(outcome.coordinator_state, "IDLE")
        assert isinstance(outcome.recovery_outcome, RecoveryCompleted)
        self.assertEqual(outcome.recovery_outcome.model_calls_started, 1)
        self.assertFalse(outcome.recovery_outcome.used_pr_resident_result)
        self.assertEqual(len(pr.reviews), 1)

        schemas_in_order = [
            rec.schema
            for c in pr.comments
            for rec in extract_v6_record_markers(c["body"], strict=True)
        ]
        self.assertEqual(
            schemas_in_order,
            [
                OWNER_SCHEMA,
                "reviewsensei:v6:unresolved:v1",
                "reviewsensei:v6:intent:v1",
                "reviewsensei:v6:receipt:v1",
            ],
        )

        # CLI --help exits cleanly before GITHUB_TOKEN or network checks.
        with redirect_stdout(io.StringIO()) as help_out:
            with self.assertRaises(SystemExit) as help_exc:
                github_main(["v6-pilot-run", "--help"])
            self.assertEqual(help_exc.exception.code, 0)
        self.assertIn("--initial-findings-json", help_out.getvalue())

    def test_rerun_after_intent_refuses_initial_result_loader_with_zero_model_calls(
        self,
    ) -> None:
        now = 1_764_200_000.0
        pr = _SimulatedPRSurface(head_sha=HEAD_A)
        coord_store = _SimulatedCoordinatorStore(clock=lambda: now, pr=pr)
        unresolved = _make_unresolved(operation_id="op-rerun-1")
        pr.add_comment(format_v6_record_marker(unresolved), is_app=True)
        coord_client = V6CoordinatorClient(
            base_url="https://coordinator.invalid",
            oidc_token_provider=lambda: "oidc-jwt-token",
            opener=coord_store.opener,
            clock=lambda: now,
        )
        grant_1 = coord_client.acquire(
            pull_request=PR_NUMBER,
            head_sha=HEAD_A,
            capabilities=("review_publish",),
            operation_id="op-rerun-1",
        )
        owner_1 = _make_owner_record(
            slot_key=grant_1.slot_key,
            generation=1,
            lease_epoch=grant_1.lease_epoch,
            reservation_id=grant_1.reservation_id,
            operation_id="op-rerun-1",
            pr_head_sha=HEAD_A,
        )
        owner_1_c = pr.add_comment(format_v6_record_marker(owner_1), is_app=True)
        coord_store.owner_comment_id = owner_1_c["id"]
        intent_1 = WriteIntentRecord(
            record_id="intent:op-rerun-1:1",
            operation_id="op-rerun-1",
            writer_epoch=grant_1.lease_epoch,
            pr_head_sha=HEAD_A,
            effect_kind="pr_review_publish",
            payload_sha256=unresolved.payload_sha256,
            target_digest=canonical_sha256(
                {"repo": REPO_ID, "pr": PR_NUMBER, "head": HEAD_A}
            ),
            generation=1,
            target_location=f"/repos/{OWNER}/{REPO}/pulls/{PR_NUMBER}/reviews",
        )
        pr.add_comment(format_v6_record_marker(intent_1), is_app=True)
        # Simulate crash before review publish (0 reviews landed) and slot drained to RECONCILING -> HOLD.
        coord_client.drain(
            slot_key=grant_1.slot_key,
            expected_generation=1,
            expected_lease_epoch=grant_1.lease_epoch,
            predecessor_anchor_comment_id=owner_1_c["id"],
        )

        def _forbidden_loader() -> tuple[list[dict[str, Any]], list[str]]:
            raise AssertionError("initial_result_loader must NEVER run on rerun")

        runner = V6PilotRunner(
            coordinator=coord_client,
            workflow_token_transport=_ScriptedTransport(pr.handle_wf),
            app_transport_factory=lambda _g: _ScriptedTransport(pr.handle_app),
            clock=lambda: now,
        )
        hold_outcome = runner.run(
            V6PilotRunConfig(
                owner=OWNER,
                repo=REPO,
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                head_sha=HEAD_A,
                operation_id="op-rerun-1",
                run_id=6002,
                workflow_sha=WORKFLOW_SHA,
                expected_bot_login=BOT_LOGIN,
                expected_bot_user_id=BOT_USER_ID,
                initial_result_loader=_forbidden_loader,
            )
        )
        self.assertEqual(hold_outcome.status, "hold")
        self.assertEqual(hold_outcome.model_calls_started, 0)
        self.assertEqual(len(pr.reviews), 0)

        # Also verify direct V6RecoveryEngine.recover_and_publish refuses initial_result_loader
        # when existing_intent is present even if result:v1 is missing.
        pr_missing_res = _SimulatedPRSurface(head_sha=HEAD_A)
        pr_missing_res.add_comment(format_v6_record_marker(intent_1), is_app=True)
        direct_client = DualCredentialGitHubClient(
            workflow_token_transport=_ScriptedTransport(pr_missing_res.handle_wf),
            broker_app_transport=_ScriptedTransport(pr_missing_res.handle_app),
        )
        direct_engine = V6RecoveryEngine(
            direct_client,
            now_fn=lambda: now,
            broker_fence_verifier=lambda _proof: {
                "repository_id": REPO_ID,
                "pull_request": PR_NUMBER,
                "pr_head_sha": HEAD_A,
                "pr_state": "open",
                "active_writer_epoch": 2,
                "active_run_id": 6003,
                "active_run_attempt": 1,
                "run_status": "in_progress",
                "verified_by_broker": True,
            },
        )
        direct_req = RecoveryRequest(
            owner=OWNER,
            repo=REPO,
            repository_id=REPO_ID,
            pull_request=PR_NUMBER,
            operation_id="op-rerun-1",
            ownership_proof=WriterOwnershipProof(
                operation_id="op-rerun-1",
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                pr_head_sha=HEAD_A,
                writer_epoch=2,
                run_id=6003,
                run_attempt=1,
                verified_by_broker=True,
            ),
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            expected_app_id=APP_ID,
            expected_bot_login=BOT_LOGIN,
            expected_bot_user_id=BOT_USER_ID,
            slot_key=grant_1.slot_key,
            generation=2,
            token_expires_at_ms=int((now + 3600) * 1000),
            initial_result_loader=_forbidden_loader,
        )
        direct_outcome = direct_engine.recover_and_publish(direct_req)
        self.assertEqual(direct_outcome.blocker, "missing_pr_unresolved_result")
        self.assertTrue(direct_outcome.zero_model_calls_started)

        # If target_result_record_id already exists on the PR (even if head mismatch triggers
        # missing_pr_unresolved_result), initial_result_loader MUST NOT be invoked.
        pr_colliding_res = _SimulatedPRSurface(head_sha=HEAD_A)
        colliding_result = UnresolvedResultRecord(
            record_id=f"result:op-collide-1:{HEAD_A[:12]}",
            operation_id="op-collide-1",
            pr_head_sha=HEAD_B,
            payload_sha256=unresolved.payload_sha256,
            findings_payload=unresolved.findings_payload,
            required_evidence_digests=(),
        )
        pr_colliding_res.add_comment(
            format_v6_record_marker(colliding_result), is_app=True
        )
        collide_client = DualCredentialGitHubClient(
            workflow_token_transport=_ScriptedTransport(pr_colliding_res.handle_wf),
            broker_app_transport=_ScriptedTransport(pr_colliding_res.handle_app),
        )
        collide_engine = V6RecoveryEngine(
            collide_client,
            now_fn=lambda: now,
            broker_fence_verifier=lambda _proof: {
                "repository_id": REPO_ID,
                "pull_request": PR_NUMBER,
                "pr_head_sha": HEAD_A,
                "pr_state": "open",
                "active_writer_epoch": 2,
                "active_run_id": 6003,
                "active_run_attempt": 1,
                "run_status": "in_progress",
                "verified_by_broker": True,
            },
        )
        collide_req = RecoveryRequest(
            owner=OWNER,
            repo=REPO,
            repository_id=REPO_ID,
            pull_request=PR_NUMBER,
            operation_id="op-collide-1",
            ownership_proof=WriterOwnershipProof(
                operation_id="op-collide-1",
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                pr_head_sha=HEAD_A,
                writer_epoch=2,
                run_id=6003,
                run_attempt=1,
                verified_by_broker=True,
            ),
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            expected_app_id=APP_ID,
            expected_bot_login=BOT_LOGIN,
            expected_bot_user_id=BOT_USER_ID,
            slot_key=grant_1.slot_key,
            generation=2,
            token_expires_at_ms=int((now + 3600) * 1000),
            initial_result_loader=_forbidden_loader,
        )
        collide_outcome = collide_engine.recover_and_publish(collide_req)
        self.assertEqual(collide_outcome.blocker, "missing_pr_unresolved_result")
        self.assertTrue(collide_outcome.zero_model_calls_started)

    def test_same_run_lost_reply_after_fresh_analysis_reconciles_to_idle(self) -> None:
        now = 1_700_050_000.0
        pr = _SimulatedPRSurface(head_sha=HEAD_A)
        coord = _SimulatedCoordinatorStore(clock=lambda: now, pr=pr)
        client = V6CoordinatorClient(
            base_url="https://coordinator.example",
            oidc_token_provider=lambda: "oidc.jwt.run1",
            opener=coord.opener,
            clock=lambda: now,
        )

        findings = ({"path": "src/app.py", "line": 4, "body": "Bounds check"},)
        loader_calls = 0

        def _initial_loader() -> tuple[tuple[dict[str, Any], ...], tuple[str, ...]]:
            nonlocal loader_calls
            loader_calls += 1
            return findings, ("ev-diff-lost-reply",)

        dropped_once = False

        def _app_with_lost_reply(
            call: dict[str, Any],
        ) -> tuple[int, Mapping[str, str], Any]:
            nonlocal dropped_once
            if (
                call["method"] == "POST"
                and call["path"] == f"/repos/{OWNER}/{REPO}/pulls/{PR_NUMBER}/reviews"
                and not dropped_once
            ):
                dropped_once = True
                status, headers, body = pr.handle_app(call)
                assert status in (200, 201) and body is not None
                raise GitHubHTTPTransientError("simulated dropped HTTP reply")
            return pr.handle_app(call)

        runner = V6PilotRunner(
            coordinator=client,
            workflow_token_transport=_ScriptedTransport(pr.handle_wf),
            app_transport_factory=lambda _grant: _ScriptedTransport(
                _app_with_lost_reply
            ),
            clock=lambda: now,
            sleeper=lambda _sec: None,
            broker_fence_verifier=lambda _proof: {
                "repository_id": REPO_ID,
                "pull_request": PR_NUMBER,
                "pr_head_sha": HEAD_A,
                "pr_state": "open",
                "active_writer_epoch": coord.lease_epoch,
                "active_run_id": 5002,
                "active_run_attempt": 1,
                "run_status": "in_progress",
                "verified_by_broker": True,
            },
        )
        outcome = runner.run(
            V6PilotRunConfig(
                owner=OWNER,
                repo=REPO,
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                head_sha=HEAD_A,
                operation_id="op-lost-reply-same-run",
                run_id=5002,
                run_attempt=1,
                workflow_sha=WORKFLOW_SHA,
                expected_app_id=APP_ID,
                expected_bot_login=BOT_LOGIN,
                expected_bot_user_id=BOT_USER_ID,
                initial_result_loader=_initial_loader,
            )
        )
        self.assertTrue(dropped_once)
        self.assertEqual(loader_calls, 1)
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.coordinator_state, "IDLE")
        self.assertTrue(outcome.lease_released)
        self.assertEqual(outcome.model_calls_started, 1)
        self.assertIsInstance(outcome.recovery_outcome, RecoveryCompleted)
        assert isinstance(outcome.recovery_outcome, RecoveryCompleted)
        self.assertTrue(outcome.recovery_outcome.reconciled_from_existing_effect)
        self.assertTrue(outcome.recovery_outcome.reconciled_after_lost_write)
        self.assertEqual(outcome.to_dict()["reconciled_after_lost_write"], True)
        self.assertEqual(len(pr.reviews), 1)
        receipts = [
            rec
            for comment in pr.comments
            for rec in extract_v6_record_markers(comment["body"], strict=True)
            if isinstance(rec, CompletionReceiptRecord)
            and rec.operation_id == "op-lost-reply-same-run"
        ]
        self.assertEqual(len(receipts), 1)

    def test_mixed_adopted_effect_and_proved_not_submitted_passes_gate2_and_rejects_hold(
        self,
    ) -> None:
        now = 1_700_060_000.0
        pr = _SimulatedPRSurface(head_sha=HEAD_A)
        coord = _SimulatedCoordinatorStore(clock=lambda: now, pr=pr)
        client = V6CoordinatorClient(
            base_url="https://coordinator.example",
            oidc_token_provider=lambda: "oidc.jwt.run2",
            opener=coord.opener,
            clock=lambda: now,
        )

        # Simulate crashed Gen 1 ACTIVE slot with 1 adopted review + 1 proved_not_submitted intent.
        coord.state = "ACTIVE"
        coord.generation = 1
        coord.lease_epoch = 1
        coord.reservation_id = "res-1-1"
        coord.owner_head_sha = HEAD_A
        coord.owner_run_id = "5001"
        coord.owner_run_attempt = 1

        owner1 = _make_owner_record(
            slot_key=coord.slot_key,
            generation=1,
            lease_epoch=1,
            operation_id="op-gen1-a",
            pr_head_sha=HEAD_A,
        )
        owner_comment = pr.add_comment(format_v6_record_marker(owner1), is_app=True)
        coord.owner_comment_id = owner_comment["id"]

        sha_a = canonical_sha256(({"path": "a.py", "line": 1, "body": "A"},))
        sha_b = canonical_sha256(({"path": "b.py", "line": 2, "body": "B"},))
        intent_a = WriteIntentRecord(
            record_id="intent:op-gen1-a:1",
            operation_id="op-gen1-a",
            writer_epoch=1,
            pr_head_sha=HEAD_A,
            effect_kind="pr_review_publish",
            payload_sha256=sha_a,
            target_digest=canonical_sha256({"pr": PR_NUMBER, "op": "a"}),
            generation=1,
            target_location=f"/repos/{OWNER}/{REPO}/pulls/{PR_NUMBER}/reviews",
        )
        intent_a_comment = pr.add_comment(
            format_v6_record_marker(intent_a), is_app=True
        )
        review_a = pr.add_review(
            f"Adopted review\n{_effect_tag('op-gen1-a', sha_a)}",
            commit_id=HEAD_A,
            is_app=True,
        )

        intent_b = WriteIntentRecord(
            record_id="intent:op-gen1-b:1",
            operation_id="op-gen1-b",
            writer_epoch=1,
            pr_head_sha=HEAD_A,
            effect_kind="pr_review_publish",
            payload_sha256=sha_b,
            target_digest=canonical_sha256({"pr": PR_NUMBER, "op": "b"}),
            generation=1,
            target_location=f"/repos/{OWNER}/{REPO}/pulls/{PR_NUMBER}/reviews",
        )
        intent_b_comment = pr.add_comment(
            format_v6_record_marker(intent_b), is_app=True
        )
        proof_b = NonSubmissionProofRecord(
            record_id="nonsub:op-gen1-b:1",
            slot_key=coord.slot_key,
            generation=1,
            operation_id="op-gen1-b",
            pr_head_sha=HEAD_A,
            proof_kind="definitive_github_rejection",
            payload_sha256=sha_b,
            recorded_at="2026-11-27T00:05:00Z",
            http_status=422,
            diagnostic="definitive_github_rejection_422",
        )
        proof_b_comment = pr.add_comment(format_v6_record_marker(proof_b), is_app=True)

        # Adding an unproved intent C -> ambiguous_hold must fail closed to HOLD
        sha_c = canonical_sha256(({"path": "c.py", "line": 3, "body": "C"},))
        intent_c = WriteIntentRecord(
            record_id="intent:op-gen1-c:1",
            operation_id="op-gen1-c",
            writer_epoch=1,
            pr_head_sha=HEAD_A,
            effect_kind="pr_review_publish",
            payload_sha256=sha_c,
            target_digest=canonical_sha256({"pr": PR_NUMBER, "op": "c"}),
            generation=1,
            target_location=f"/repos/{OWNER}/{REPO}/pulls/{PR_NUMBER}/reviews",
        )
        intent_c_comment = pr.add_comment(
            format_v6_record_marker(intent_c), is_app=True
        )

        client.drain(
            slot_key=coord.slot_key,
            expected_generation=1,
            expected_lease_epoch=1,
            predecessor_anchor_comment_id=owner_comment["id"],
        )
        with self.assertRaises(V6CoordinatorError):
            client.complete_reconcile(
                slot_key=coord.slot_key,
                generation=1,
                reconciliation_outcome="adopted_missing_receipt",
                reconciliation_evidence={
                    "old_generation": 1,
                    "old_owner_comment_id": owner_comment["id"],
                    "intent_resolutions": [
                        {
                            "operation_id": "op-gen1-a",
                            "intent_comment_id": intent_a_comment["id"],
                            "resolution": "adopted_effect",
                            "native_effect_kind": "pull_request_review",
                            "native_effect_id": review_a["id"],
                        },
                        {
                            "operation_id": "op-gen1-b",
                            "intent_comment_id": intent_b_comment["id"],
                            "resolution": "proved_not_submitted",
                            "proof_comment_id": proof_b_comment["id"],
                        },
                        {
                            "operation_id": "op-gen1-c",
                            "intent_comment_id": intent_c_comment["id"],
                            "resolution": "ambiguous_hold",
                        },
                    ],
                },
                anchor_comment_id=owner_comment["id"],
                head_sha=HEAD_A,
            )
        self.assertEqual(coord.state, "HOLD")

        # Remove ambiguous intent C, restore RECONCILING, and verify read_only_engine + complete_reconcile reach IDLE
        pr.comments.remove(intent_c_comment)
        coord.state = "RECONCILING"
        coord.last_reason_code = None

        ro_client = DualCredentialGitHubClient(
            workflow_token_transport=_ScriptedTransport(pr.handle_wf),
            broker_app_transport=_ScriptedTransport(pr.handle_app),
        )
        ro_engine = V6RecoveryEngine(ro_client, now_fn=lambda: now)
        recon = ro_engine.reconcile_takeover_read_only(
            owner=OWNER,
            repo=REPO,
            repository_id=REPO_ID,
            pull_request=PR_NUMBER,
            slot_key=coord.slot_key,
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            old_generation=1,
            old_lease_epoch=1,
            expected_app_id=APP_ID,
            expected_bot_login=BOT_LOGIN,
            expected_bot_user_id=BOT_USER_ID,
            operation_id="op-gen1-a",
        )
        self.assertEqual(recon.reconciliation_outcome, "adopted_missing_receipt")
        self.assertEqual(len(recon.adopted_effects), 1)
        self.assertEqual(len(recon.unsubmitted_intents), 1)
        assert recon.reconciliation_evidence is not None
        comp = client.complete_reconcile(
            slot_key=coord.slot_key,
            generation=1,
            reconciliation_outcome=recon.reconciliation_outcome,
            reconciliation_evidence=recon.reconciliation_evidence,
            anchor_comment_id=owner_comment["id"],
            head_sha=HEAD_A,
        )
        self.assertEqual(comp.state, "IDLE")
        self.assertEqual(coord.state, "IDLE")

    def test_default_operation_id_and_cli_override_invariants(self) -> None:
        from unittest import mock

        from review_sensei_github.v6_pilot_runner import V6PilotRunOutcome

        from review_sensei.errors import ReviewInputError

        op1 = default_v6_operation_id(
            repository_id=REPO_ID, pull_request=PR_NUMBER, head_sha=HEAD_A
        )
        op2 = default_v6_operation_id(
            repository_id=REPO_ID, pull_request=PR_NUMBER, head_sha=HEAD_A
        )
        op_b = default_v6_operation_id(
            repository_id=REPO_ID, pull_request=PR_NUMBER, head_sha=HEAD_B
        )
        self.assertEqual(
            op1, f"v6:{REPO_ID}:{PR_NUMBER}:{HEAD_A[:12]}:pr_review_publish"
        )
        self.assertEqual(op1, op2)
        self.assertNotEqual(op1, op_b)

        cfg_no_transport = V6PilotRunConfig(
            owner=OWNER,
            repo=REPO,
            repository_id=REPO_ID,
            pull_request=PR_NUMBER,
            head_sha=HEAD_A,
            operation_id=op1,
            analyze_fresh_pr=True,
        )
        with self.assertRaises(ReviewInputError) as missing_tok:
            _build_initial_result_loader(
                cfg_no_transport, workflow_token_transport=None
            )
        self.assertEqual(missing_tok.exception.diagnostic, "v6_missing_github_token")

        wf_transport = _ScriptedTransport(lambda _call: (404, {}, {}))
        loader = _build_initial_result_loader(
            cfg_no_transport, workflow_token_transport=wf_transport
        )
        self.assertIsNotNone(loader)
        self.assertEqual(wf_transport.calls, [])
        with mock.patch(
            "review_sensei_github.v6_pilot_runner.run_v6_fresh_pr_analysis",
            return_value=(({"path": "a.py", "line": 1, "body": "x"},), ()),
        ) as fake_analyze:
            assert loader is not None
            loader()
            fake_analyze.assert_called_once()

        captured_configs: list[V6PilotRunConfig] = []

        def _fake_run_v6_pilot(
            config: V6PilotRunConfig, **_kwargs: Any
        ) -> V6PilotRunOutcome:
            captured_configs.append(config)
            return V6PilotRunOutcome(status="completed", coordinator_state="IDLE")

        with tempfile.TemporaryDirectory() as tmpdir:
            policy_dir = Path(tmpdir) / "policy"
            policy_dir.mkdir()
            with (
                mock.patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test_token"}),
                mock.patch(
                    "review_sensei_github.v6_pilot_runner.run_v6_pilot",
                    side_effect=_fake_run_v6_pilot,
                ),
                redirect_stdout(io.StringIO()),
            ):
                rc_default = run_v6_pilot_cli(
                    [
                        "--repository",
                        f"{OWNER}/{REPO}",
                        "--repository-id",
                        str(REPO_ID),
                        "--pull-request",
                        str(PR_NUMBER),
                        "--head-sha",
                        HEAD_A,
                        "--run-id",
                        "111",
                        "--run-attempt",
                        "1",
                        "--analyze-fresh-pr",
                        "--policy-root",
                        str(policy_dir),
                        "--provider",
                        "openai-compatible",
                        "--model",
                        "gpt-4o-mini",
                        "--provider-base-url",
                        "https://127.0.0.1:8443/v1",
                        "--allow-custom-endpoint",
                    ]
                )
                rc_override = run_v6_pilot_cli(
                    [
                        "--repository",
                        f"{OWNER}/{REPO}",
                        "--repository-id",
                        str(REPO_ID),
                        "--pull-request",
                        str(PR_NUMBER),
                        "--head-sha",
                        HEAD_A,
                        "--operation-id",
                        "custom-op-id",
                    ]
                )
        self.assertEqual(rc_default, 0)
        self.assertEqual(rc_override, 0)
        self.assertEqual(captured_configs[0].operation_id, op1)
        self.assertTrue(captured_configs[0].analyze_fresh_pr)
        self.assertEqual(captured_configs[0].policy_root, policy_dir)
        self.assertEqual(captured_configs[0].provider, "openai-compatible")
        self.assertEqual(captured_configs[0].model, "gpt-4o-mini")
        self.assertEqual(
            captured_configs[0].provider_base_url, "https://127.0.0.1:8443/v1"
        )
        self.assertTrue(captured_configs[0].allow_custom_endpoint)
        self.assertEqual(captured_configs[1].operation_id, "custom-op-id")

    def test_format_compare_file_diff_preserves_no_newline_verbatim(self) -> None:
        patch_with_no_nl = (
            "@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+new\n"
            "\\ No newline at end of file"
        )
        reconstructed = _format_compare_file_diff(
            {
                "filename": "a.py",
                "status": "modified",
                "patch": patch_with_no_nl,
            }
        )
        self.assertIn("\\ No newline at end of file\n", reconstructed)

    def test_urllib_transport_rejects_oversized_diff_body(self) -> None:
        class _OversizedResponse:
            status = 200
            headers = {"Content-Type": "text/plain; charset=utf-8"}

            def read(self, n: int = -1) -> bytes:
                size = _MAX_GITHUB_BODY_BYTES + 1 if n < 0 else n
                return b"x" * size

            def __enter__(self) -> _OversizedResponse:
                return self

            def __exit__(self, *args: Any) -> None:
                return None

        transport = UrllibGitHubTransport(
            token="ghp_test_token",
            opener=lambda _req, timeout=None: _OversizedResponse(),
        )
        with self.assertRaises(GitHubResponseTooLargeError) as exc:
            transport.request(
                "GET",
                f"/repos/{OWNER}/{REPO}/pulls/{PR_NUMBER}",
                headers={"Accept": "application/vnd.github.v3.diff"},
            )
        self.assertEqual(exc.exception.diagnostic, "v6_response_too_large")

    def test_review_envelope_rendering_and_malformed_validation(self) -> None:
        from review_sensei.errors import ReviewInputError

        now = 1_700_070_000.0
        pr = _SimulatedPRSurface(head_sha=HEAD_A)
        coord = _SimulatedCoordinatorStore(clock=lambda: now, pr=pr)
        client = V6CoordinatorClient(
            base_url="https://coordinator.example",
            oidc_token_provider=lambda: "oidc.jwt.run1",
            opener=coord.opener,
            clock=lambda: now,
        )

        # 1. Valid anchored envelope renders review_body and inline comments.
        valid_envelope = (
            {
                "_v6_review_envelope": "reviewsensei:v6:review_envelope:v1",
                "review_body": "Real formatted summary",
                "event": "COMMENT",
                "comments": [
                    {"path": "src/a.py", "line": 3, "side": "RIGHT", "body": "Fix"}
                ],
                "findings": [],
                "provider": "openai-compatible",
                "model": "gpt-4o-mini",
                "review_status": "complete",
                "content_digest": "e" * 64,
            },
        )
        runner = V6PilotRunner(
            coordinator=client,
            workflow_token_transport=_ScriptedTransport(pr.handle_wf),
            app_transport_factory=lambda _grant: _ScriptedTransport(pr.handle_app),
            clock=lambda: now,
            sleeper=lambda _sec: None,
            broker_fence_verifier=lambda _proof: {
                "repository_id": REPO_ID,
                "pull_request": PR_NUMBER,
                "pr_head_sha": HEAD_A,
                "pr_state": "open",
                "active_writer_epoch": coord.lease_epoch,
                "active_run_id": 5002,
                "active_run_attempt": 1,
                "run_status": "in_progress",
                "verified_by_broker": True,
            },
        )
        outcome = runner.run(
            V6PilotRunConfig(
                owner=OWNER,
                repo=REPO,
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                head_sha=HEAD_A,
                operation_id="op-envelope-valid",
                run_id=5002,
                run_attempt=1,
                workflow_sha=WORKFLOW_SHA,
                expected_app_id=APP_ID,
                expected_bot_login=BOT_LOGIN,
                expected_bot_user_id=BOT_USER_ID,
                initial_result_loader=lambda: (valid_envelope, ()),
            )
        )
        self.assertEqual(outcome.status, "completed")
        self.assertIn("Real formatted summary", pr.reviews[-1]["body"])

        # 2. Anchored envelope with non-Mapping comments fails closed.
        bad_envelope = (
            {
                "_v6_review_envelope": "reviewsensei:v6:review_envelope:v1",
                "review_body": "Body",
                "event": "COMMENT",
                "comments": ["not-a-mapping"],
            },
        )
        bad_outcome = runner.run(
            V6PilotRunConfig(
                owner=OWNER,
                repo=REPO,
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                head_sha=HEAD_A,
                operation_id="op-envelope-bad",
                run_id=5002,
                run_attempt=1,
                workflow_sha=WORKFLOW_SHA,
                expected_app_id=APP_ID,
                expected_bot_login=BOT_LOGIN,
                expected_bot_user_id=BOT_USER_ID,
                initial_result_loader=lambda: (bad_envelope, ()),
            )
        )
        self.assertEqual(bad_outcome.status, "hold")
        self.assertEqual(bad_outcome.diagnostic, "v6_invalid_review_envelope")
        del ReviewInputError  # imported for diagnostic assertion

    def test_simulated_coordinator_rejects_empty_resolutions_and_ambiguous_hold(
        self,
    ) -> None:
        now = 1_700_080_000.0
        pr = _SimulatedPRSurface(head_sha=HEAD_A)
        coord = _SimulatedCoordinatorStore(clock=lambda: now, pr=pr)
        coord.state = "RECONCILING"
        coord.generation = 1
        coord.lease_epoch = 1
        coord.token_cleared = True

        self.assertEqual(
            coord._verify_pr_reconciliation_evidence(
                pr=pr,
                outcome="adopted_missing_receipt",
                evidence={"old_generation": 1, "intent_resolutions": []},
            ),
            "v6_lease_reconciliation_evidence_invalid",
        )
        self.assertEqual(
            coord._verify_pr_reconciliation_evidence(
                pr=pr,
                outcome="proved_not_submitted",
                evidence={"old_generation": 1, "intent_resolutions": []},
            ),
            "v6_lease_reconciliation_evidence_invalid",
        )

    def test_run_v6_fresh_pr_analysis_splits_oversized_compare_tree_blobs_and_subdivides_hunks(
        self,
    ) -> None:
        import base64
        import hashlib
        from unittest import mock

        from review_sensei.models import ReviewComment, ReviewResult

        base_sha = "1" * 40
        merge_base_sha = "2" * 40
        dir_sha_base = "3" * 40
        dir_sha_head = "4" * 40
        blob_a_base = "5" * 40
        blob_a_head = "6" * 40
        blob_bin_head = "7" * 40
        blob_ren = "8" * 40
        big_lines = [f"line_{i} = {i}" for i in range(60)]
        big_lines_head = [f"line_{i} = {i + 1000}" for i in range(60)]

        blobs = {
            blob_a_base: "\n".join(big_lines).encode("utf-8"),
            blob_a_head: "\n".join(big_lines_head).encode("utf-8"),
            blob_bin_head: b"\x00\x01binary-payload\xff",
            blob_ren: b"renamed content\n",
        }

        def _wf_handler(call: dict[str, Any]) -> tuple[int, Mapping[str, str], Any]:
            path = call["path"]
            headers = call.get("headers") or {}
            if path == f"/repos/{OWNER}/{REPO}/pulls/{PR_NUMBER}":
                return (
                    200,
                    {},
                    {
                        "state": "open",
                        "title": "Large tree split PR",
                        "head": {"sha": HEAD_A},
                        "base": {"sha": base_sha},
                    },
                )
            if path == f"/repos/{OWNER}/{REPO}/compare/{base_sha}...{HEAD_A}":
                if "v3.diff" in str(headers.get("Accept", "")):
                    raise GitHubResponseTooLargeError()
                raise GitHubResponseTooLargeError()
            if (
                path
                == f"/repos/{OWNER}/{REPO}/compare/{base_sha}...{HEAD_A}?per_page=1"
            ):
                raise AssertionError("page 1 ?per_page=1 must never be retried")
            if (
                path
                == f"/repos/{OWNER}/{REPO}/compare/{base_sha}...{HEAD_A}?per_page=1&page=2"
            ):
                return 200, {}, {"merge_base_commit": {"sha": merge_base_sha}}
            if path == f"/repos/{OWNER}/{REPO}/git/trees/{merge_base_sha}?recursive=1":
                raise GitHubResponseTooLargeError()
            if path == f"/repos/{OWNER}/{REPO}/git/trees/{head_sha_val}?recursive=1":
                raise GitHubResponseTooLargeError()
            if path == f"/repos/{OWNER}/{REPO}/git/trees/{merge_base_sha}":
                return (
                    200,
                    {},
                    {
                        "tree": [
                            {
                                "path": "src",
                                "type": "tree",
                                "sha": dir_sha_base,
                                "mode": "040000",
                            },
                            {
                                "path": "old_name.txt",
                                "type": "blob",
                                "sha": blob_ren,
                                "mode": "100644",
                            },
                        ]
                    },
                )
            if path == f"/repos/{OWNER}/{REPO}/git/trees/{dir_sha_base}":
                return (
                    200,
                    {},
                    {
                        "tree": [
                            {
                                "path": "big.py",
                                "type": "blob",
                                "sha": blob_a_base,
                                "mode": "100644",
                            },
                        ]
                    },
                )
            if path == f"/repos/{OWNER}/{REPO}/git/trees/{HEAD_A}":
                return (
                    200,
                    {},
                    {
                        "tree": [
                            {
                                "path": "src",
                                "type": "tree",
                                "sha": dir_sha_head,
                                "mode": "040000",
                            },
                            {
                                "path": "new_name.txt",
                                "type": "blob",
                                "sha": blob_ren,
                                "mode": "100644",
                            },
                            {
                                "path": "assets.bin",
                                "type": "blob",
                                "sha": blob_bin_head,
                                "mode": "100644",
                            },
                        ]
                    },
                )
            if path == f"/repos/{OWNER}/{REPO}/git/trees/{dir_sha_head}":
                return (
                    200,
                    {},
                    {
                        "tree": [
                            {
                                "path": "big.py",
                                "type": "blob",
                                "sha": blob_a_head,
                                "mode": "100644",
                            },
                        ]
                    },
                )
            if path.startswith(f"/repos/{OWNER}/{REPO}/git/blobs/"):
                sha = path.rsplit("/", 1)[-1]
                raw = blobs[sha]
                return (
                    200,
                    {},
                    {
                        "sha": sha,
                        "size": len(raw),
                        "encoding": "base64",
                        "content": base64.b64encode(raw).decode("ascii"),
                    },
                )
            raise AssertionError(f"unexpected call: {call}")

        head_sha_val = HEAD_A
        review_calls: list[str] = []

        class _FakeService:
            def __init__(self, *_a: Any, **_kw: Any) -> None:
                pass

            def review(self, req: Any) -> ReviewResult:
                review_calls.append(req.diff)
                return ReviewResult(
                    summary=f"Batch {len(review_calls)}",
                    comments=(
                        ReviewComment(
                            path="src/big.py",
                            line=2,
                            side="RIGHT",
                            body="Shared finding",
                        ),
                        ReviewComment(
                            path="src/big.py",
                            line=2 + len(review_calls),
                            side="RIGHT",
                            body=f"Batch {len(review_calls)} finding",
                        ),
                    ),
                    provider="openai-compatible",
                    model="gpt-4o-mini",
                    review_status="complete",
                )

        with (
            mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-key"}),
            mock.patch(
                "review_sensei_github.v6_fresh_analysis.ReviewService",
                _FakeService,
            ),
        ):
            payloads, digests = run_v6_fresh_pr_analysis(
                owner=OWNER,
                repo=REPO,
                pull_request=PR_NUMBER,
                head_sha=HEAD_A,
                workflow_token_transport=_ScriptedTransport(_wf_handler),
                granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
                provider="openai-compatible",
                model="gpt-4o-mini",
                provider_base_url="https://127.0.0.1:8443/v1",
                allow_custom_endpoint=True,
                batch_max_bytes=700,
            )

        self.assertGreater(len(review_calls), 1)
        self.assertEqual(len(payloads), 1)
        envelope = payloads[0]
        self.assertEqual(
            envelope["_v6_review_envelope"], "reviewsensei:v6:review_envelope:v1"
        )
        inv_by_path = {row["path"]: row for row in envelope["file_inventory"]}
        self.assertEqual(inv_by_path["src/big.py"]["disposition"], "text")
        self.assertEqual(inv_by_path["assets.bin"]["disposition"], "binary")
        self.assertEqual(inv_by_path["new_name.txt"]["disposition"], "rename_only")
        # Shared finding across batches must be deduplicated cleanly.
        bodies = [c["body"] for c in envelope["comments"]]
        self.assertEqual(bodies.count("Shared finding"), 1)
        self.assertTrue(digests[0].startswith("diff:sha256:"))
        del hashlib

    def test_partitioned_result_recovery_orphan_hold_and_413_size_refused(self) -> None:
        from review_sensei_github.v6_recovery_ledger import (
            NonSubmissionProofRecord,
            ResultManifestRecord,
            ResultPartRecord,
        )

        now = 1_700_090_000.0
        large_body = "Summary detail " + ("x" * 70_000)
        oversized_envelope = (
            {
                "_v6_review_envelope": "reviewsensei:v6:review_envelope:v1",
                "review_body": large_body,
                "event": "COMMENT",
                "comments": [
                    {"path": "src/a.py", "line": 3, "side": "RIGHT", "body": "Fix"}
                ],
                "findings": [],
                "provider": "openai-compatible",
                "model": "gpt-4o-mini",
                "review_status": "complete",
                "content_digest": "f" * 64,
            },
        )

        # 1. Oversized (>64 KiB) result splits into result-part:v1 + result-manifest:v1
        pr = _SimulatedPRSurface(head_sha=HEAD_A)
        coord = _SimulatedCoordinatorStore(clock=lambda: now, pr=pr)
        client = V6CoordinatorClient(
            base_url="https://coordinator.example",
            oidc_token_provider=lambda: "oidc.jwt.run1",
            opener=coord.opener,
            clock=lambda: now,
        )
        runner = V6PilotRunner(
            coordinator=client,
            workflow_token_transport=_ScriptedTransport(pr.handle_wf),
            app_transport_factory=lambda _grant: _ScriptedTransport(pr.handle_app),
            clock=lambda: now,
            sleeper=lambda _sec: None,
            broker_fence_verifier=lambda _proof: {
                "repository_id": REPO_ID,
                "pull_request": PR_NUMBER,
                "pr_head_sha": HEAD_A,
                "pr_state": "open",
                "active_writer_epoch": coord.lease_epoch,
                "active_run_id": 5010,
                "active_run_attempt": 1,
                "run_status": "in_progress",
                "verified_by_broker": True,
            },
        )
        outcome = runner.run(
            V6PilotRunConfig(
                owner=OWNER,
                repo=REPO,
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                head_sha=HEAD_A,
                operation_id="op-partition-1",
                run_id=5010,
                run_attempt=1,
                workflow_sha=WORKFLOW_SHA,
                expected_app_id=APP_ID,
                expected_bot_login=BOT_LOGIN,
                expected_bot_user_id=BOT_USER_ID,
                initial_result_loader=lambda: (oversized_envelope, ()),
            )
        )
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(len(pr.reviews), 1)
        self.assertIn(large_body[:1000], pr.reviews[0]["body"])
        self.assertEqual(
            len(pr.reviews[0]["body"]),
            len(large_body)
            + len("\n\n<!-- reviewsensei:v6:effect op=op-partition-1 sha256=")
            + 64
            + len(" -->"),
        )
        all_recs = [
            rec
            for c in pr.comments
            for rec in extract_v6_record_markers(c["body"], strict=True)
        ]
        parts = [r for r in all_recs if isinstance(r, ResultPartRecord)]
        manifests = [r for r in all_recs if isinstance(r, ResultManifestRecord)]
        self.assertGreaterEqual(len(parts), 1)
        self.assertEqual(len(manifests), 1)

        # 2. Cold recovery from partitioned result-part:v1 + result-manifest:v1 (0 model calls)
        pr_cold = _SimulatedPRSurface(head_sha=HEAD_A)
        for c in pr.comments:
            rec_list = extract_v6_record_markers(c["body"], strict=True)
            if any(
                isinstance(r, (ResultPartRecord, ResultManifestRecord))
                for r in rec_list
            ):
                pr_cold.comments.append(dict(c))
        coord_cold = _SimulatedCoordinatorStore(clock=lambda: now, pr=pr_cold)
        client_cold = V6CoordinatorClient(
            base_url="https://coordinator.example",
            oidc_token_provider=lambda: "oidc.jwt.run2",
            opener=coord_cold.opener,
            clock=lambda: now,
        )
        loader_calls = 0

        def _should_not_run_loader() -> tuple[Any, Any]:
            nonlocal loader_calls
            loader_calls += 1
            return oversized_envelope, ()

        runner_cold = V6PilotRunner(
            coordinator=client_cold,
            workflow_token_transport=_ScriptedTransport(pr_cold.handle_wf),
            app_transport_factory=lambda _grant: _ScriptedTransport(pr_cold.handle_app),
            clock=lambda: now,
            sleeper=lambda _sec: None,
            broker_fence_verifier=lambda _proof: {
                "repository_id": REPO_ID,
                "pull_request": PR_NUMBER,
                "pr_head_sha": HEAD_A,
                "pr_state": "open",
                "active_writer_epoch": coord_cold.lease_epoch,
                "active_run_id": 5011,
                "active_run_attempt": 1,
                "run_status": "in_progress",
                "verified_by_broker": True,
            },
        )
        cold_outcome = runner_cold.run(
            V6PilotRunConfig(
                owner=OWNER,
                repo=REPO,
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                head_sha=HEAD_A,
                operation_id="op-partition-1",
                run_id=5011,
                run_attempt=1,
                workflow_sha=WORKFLOW_SHA,
                expected_app_id=APP_ID,
                expected_bot_login=BOT_LOGIN,
                expected_bot_user_id=BOT_USER_ID,
                initial_result_loader=_should_not_run_loader,
            )
        )
        self.assertEqual(cold_outcome.status, "completed")
        self.assertEqual(loader_calls, 0)
        self.assertEqual(len(pr_cold.reviews), 1)
        self.assertEqual(len(pr_cold.reviews[0]["body"]), len(pr.reviews[0]["body"]))

        # 3. Orphan result-part:v1 without manifest -> HOLD incomplete_partitioned_result_missing_manifest (0 model calls)
        pr_orphan = _SimulatedPRSurface(head_sha=HEAD_A)
        for c in pr.comments:
            rec_list = extract_v6_record_markers(c["body"], strict=True)
            if any(isinstance(r, ResultPartRecord) for r in rec_list):
                pr_orphan.add_comment(c["body"], is_app=True)
        coord_orphan = _SimulatedCoordinatorStore(clock=lambda: now, pr=pr_orphan)
        client_orphan = V6CoordinatorClient(
            base_url="https://coordinator.example",
            oidc_token_provider=lambda: "oidc.jwt.run3",
            opener=coord_orphan.opener,
            clock=lambda: now,
        )
        runner_orphan = V6PilotRunner(
            coordinator=client_orphan,
            workflow_token_transport=_ScriptedTransport(pr_orphan.handle_wf),
            app_transport_factory=lambda _grant: _ScriptedTransport(
                pr_orphan.handle_app
            ),
            clock=lambda: now,
            sleeper=lambda _sec: None,
            broker_fence_verifier=lambda _proof: {
                "repository_id": REPO_ID,
                "pull_request": PR_NUMBER,
                "pr_head_sha": HEAD_A,
                "pr_state": "open",
                "active_writer_epoch": coord_orphan.lease_epoch,
                "active_run_id": 5012,
                "active_run_attempt": 1,
                "run_status": "in_progress",
                "verified_by_broker": True,
            },
        )
        orphan_outcome = runner_orphan.run(
            V6PilotRunConfig(
                owner=OWNER,
                repo=REPO,
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                head_sha=HEAD_A,
                operation_id="op-partition-1",
                run_id=5012,
                run_attempt=1,
                workflow_sha=WORKFLOW_SHA,
                expected_app_id=APP_ID,
                expected_bot_login=BOT_LOGIN,
                expected_bot_user_id=BOT_USER_ID,
                initial_result_loader=_should_not_run_loader,
            )
        )
        self.assertEqual(orphan_outcome.status, "hold")
        self.assertEqual(
            orphan_outcome.diagnostic, "incomplete_partitioned_result_missing_manifest"
        )
        self.assertEqual(loader_calls, 0)

        # 4. HTTP 413 on POST /pulls/{pr}/reviews persists NonSubmissionProofRecord(github_native_review_size_refused)
        pr_413 = _SimulatedPRSurface(head_sha=HEAD_A)
        coord_413 = _SimulatedCoordinatorStore(clock=lambda: now, pr=pr_413)
        client_413 = V6CoordinatorClient(
            base_url="https://coordinator.example",
            oidc_token_provider=lambda: "oidc.jwt.run4",
            opener=coord_413.opener,
            clock=lambda: now,
        )

        def _app_with_413(call: dict[str, Any]) -> tuple[int, Mapping[str, str], Any]:
            if call["method"] == "POST" and call["path"].endswith("/reviews"):
                return 413, {}, {"message": "Review payload too large"}
            return pr_413.handle_app(call)

        runner_413 = V6PilotRunner(
            coordinator=client_413,
            workflow_token_transport=_ScriptedTransport(pr_413.handle_wf),
            app_transport_factory=lambda _grant: _ScriptedTransport(_app_with_413),
            clock=lambda: now,
            sleeper=lambda _sec: None,
            broker_fence_verifier=lambda _proof: {
                "repository_id": REPO_ID,
                "pull_request": PR_NUMBER,
                "pr_head_sha": HEAD_A,
                "pr_state": "open",
                "active_writer_epoch": coord_413.lease_epoch,
                "active_run_id": 5013,
                "active_run_attempt": 1,
                "run_status": "in_progress",
                "verified_by_broker": True,
            },
        )
        out_413 = runner_413.run(
            V6PilotRunConfig(
                owner=OWNER,
                repo=REPO,
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                head_sha=HEAD_A,
                operation_id="op-413-refuse",
                run_id=5013,
                run_attempt=1,
                workflow_sha=WORKFLOW_SHA,
                expected_app_id=APP_ID,
                expected_bot_login=BOT_LOGIN,
                expected_bot_user_id=BOT_USER_ID,
                initial_result_loader=lambda: (oversized_envelope, ()),
            )
        )
        self.assertEqual(out_413.status, "hold")
        self.assertEqual(out_413.diagnostic, "github_native_review_size_refused")
        proofs_413 = [
            rec
            for c in pr_413.comments
            for rec in extract_v6_record_markers(c["body"], strict=True)
            if isinstance(rec, NonSubmissionProofRecord)
        ]
        self.assertEqual(len(proofs_413), 1)
        self.assertEqual(proofs_413[0].diagnostic, "github_native_review_size_refused")

    def test_strict_comment_only_publisher_and_effect_payload_digest_bound(
        self,
    ) -> None:
        from review_sensei_github.v6_recovery_ledger import canonical_sha256

        now = 1_700_095_000.0
        pr = _SimulatedPRSurface(head_sha=HEAD_A)
        coord = _SimulatedCoordinatorStore(clock=lambda: now, pr=pr)
        client = V6CoordinatorClient(
            base_url="https://coordinator.example",
            oidc_token_provider=lambda: "oidc.jwt.run1",
            opener=coord.opener,
            clock=lambda: now,
        )
        runner = V6PilotRunner(
            coordinator=client,
            workflow_token_transport=_ScriptedTransport(pr.handle_wf),
            app_transport_factory=lambda _grant: _ScriptedTransport(pr.handle_app),
            clock=lambda: now,
            sleeper=lambda _sec: None,
            broker_fence_verifier=lambda _proof: {
                "repository_id": REPO_ID,
                "pull_request": PR_NUMBER,
                "pr_head_sha": HEAD_A,
                "pr_state": "open",
                "active_writer_epoch": coord.lease_epoch,
                "active_run_id": 5020,
                "active_run_attempt": 1,
                "run_status": "in_progress",
                "verified_by_broker": True,
            },
        )
        approve_envelope = (
            {
                "_v6_review_envelope": "reviewsensei:v6:review_envelope:v1",
                "review_body": "Should reject non-comment",
                "event": "APPROVE",
                "comments": [],
            },
        )
        out = runner.run(
            V6PilotRunConfig(
                owner=OWNER,
                repo=REPO,
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                head_sha=HEAD_A,
                operation_id="op-no-approve",
                run_id=5020,
                run_attempt=1,
                workflow_sha=WORKFLOW_SHA,
                expected_app_id=APP_ID,
                expected_bot_login=BOT_LOGIN,
                expected_bot_user_id=BOT_USER_ID,
                initial_result_loader=lambda: (approve_envelope, ()),
            )
        )
        self.assertEqual(out.status, "hold")
        self.assertEqual(out.diagnostic, "v6_unsupported_review_event")
        intents_bad = [
            rec
            for c in pr.comments
            for rec in extract_v6_record_markers(c["body"], strict=True)
            if isinstance(rec, WriteIntentRecord)
        ]
        self.assertEqual(intents_bad, [])

        # Valid COMMENT envelope binds target_digest to exact canonical_sha256(effect_payload)
        valid_envelope = (
            {
                "_v6_review_envelope": "reviewsensei:v6:review_envelope:v1",
                "review_body": "Bound effect digest",
                "event": "COMMENT",
                "comments": [],
            },
        )
        pr_ok = _SimulatedPRSurface(head_sha=HEAD_A)
        coord_ok = _SimulatedCoordinatorStore(clock=lambda: now, pr=pr_ok)
        client_ok = V6CoordinatorClient(
            base_url="https://coordinator.example",
            oidc_token_provider=lambda: "oidc.jwt.run2",
            opener=coord_ok.opener,
            clock=lambda: now,
        )
        posted_bodies: list[dict[str, Any]] = []

        def _capture_app(call: dict[str, Any]) -> tuple[int, Mapping[str, str], Any]:
            if call["method"] == "POST" and call["path"].endswith("/reviews"):
                posted_bodies.append(dict(call["body"]))
            return pr_ok.handle_app(call)

        runner_ok = V6PilotRunner(
            coordinator=client_ok,
            workflow_token_transport=_ScriptedTransport(pr_ok.handle_wf),
            app_transport_factory=lambda _grant: _ScriptedTransport(_capture_app),
            clock=lambda: now,
            sleeper=lambda _sec: None,
            broker_fence_verifier=lambda _proof: {
                "repository_id": REPO_ID,
                "pull_request": PR_NUMBER,
                "pr_head_sha": HEAD_A,
                "pr_state": "open",
                "active_writer_epoch": coord_ok.lease_epoch,
                "active_run_id": 5021,
                "active_run_attempt": 1,
                "run_status": "in_progress",
                "verified_by_broker": True,
            },
        )
        ok_outcome = runner_ok.run(
            V6PilotRunConfig(
                owner=OWNER,
                repo=REPO,
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                head_sha=HEAD_A,
                operation_id="op-bound-effect",
                run_id=5021,
                run_attempt=1,
                workflow_sha=WORKFLOW_SHA,
                expected_app_id=APP_ID,
                expected_bot_login=BOT_LOGIN,
                expected_bot_user_id=BOT_USER_ID,
                initial_result_loader=lambda: (valid_envelope, ()),
            )
        )
        self.assertEqual(ok_outcome.status, "completed")
        intents_ok = [
            rec
            for c in pr_ok.comments
            for rec in extract_v6_record_markers(c["body"], strict=True)
            if isinstance(rec, WriteIntentRecord)
        ]
        self.assertEqual(len(intents_ok), 1)
        self.assertEqual(
            intents_ok[0].target_digest, canonical_sha256(posted_bodies[0])
        )

    def test_pre_publish_head_verifier_blocks_before_post_and_persists_non_submission(
        self,
    ) -> None:
        from review_sensei_github.v6_recovery_ledger import NonSubmissionProofRecord

        from review_sensei.errors import ReviewInputError

        now = 1_700_096_000.0
        pr = _SimulatedPRSurface(head_sha=HEAD_A)
        coord = _SimulatedCoordinatorStore(clock=lambda: now, pr=pr)
        client = V6CoordinatorClient(
            base_url="https://coordinator.example",
            oidc_token_provider=lambda: "oidc.jwt.run1",
            opener=coord.opener,
            clock=lambda: now,
        )
        checks = 0

        def _move_after_intent() -> None:
            nonlocal checks
            checks += 1
            if checks >= 2:
                raise ReviewInputError(
                    "PR head moved mid-run", diagnostic="v6_pr_head_mismatch"
                )

        runner = V6PilotRunner(
            coordinator=client,
            workflow_token_transport=_ScriptedTransport(pr.handle_wf),
            app_transport_factory=lambda _grant: _ScriptedTransport(pr.handle_app),
            clock=lambda: now,
            sleeper=lambda _sec: None,
            broker_fence_verifier=lambda _proof: {
                "repository_id": REPO_ID,
                "pull_request": PR_NUMBER,
                "pr_head_sha": HEAD_A,
                "pr_state": "open",
                "active_writer_epoch": coord.lease_epoch,
                "active_run_id": 5025,
                "active_run_attempt": 1,
                "run_status": "in_progress",
                "verified_by_broker": True,
            },
        )
        out = runner.run(
            V6PilotRunConfig(
                owner=OWNER,
                repo=REPO,
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                head_sha=HEAD_A,
                operation_id="op-head-move",
                run_id=5025,
                run_attempt=1,
                workflow_sha=WORKFLOW_SHA,
                expected_app_id=APP_ID,
                expected_bot_login=BOT_LOGIN,
                expected_bot_user_id=BOT_USER_ID,
                initial_result_loader=lambda: (
                    _make_unresolved(operation_id="op-head-move").findings_payload,
                    (),
                ),
                pre_publish_head_verifier=_move_after_intent,
            )
        )
        self.assertEqual(out.status, "hold")
        self.assertEqual(out.diagnostic, "v6_pr_head_mismatch")
        self.assertEqual(pr.reviews, [])
        proofs = [
            rec
            for c in pr.comments
            for rec in extract_v6_record_markers(c["body"], strict=True)
            if isinstance(rec, NonSubmissionProofRecord)
        ]
        self.assertEqual(len(proofs), 1)
        self.assertEqual(proofs[0].proof_kind, "pre_dispatch_terminal_abort")

    def test_trusted_policy_enforcement_skips_before_coordinator(self) -> None:
        from unittest import mock

        pr = _SimulatedPRSurface(head_sha=HEAD_A)
        coord_calls: list[str] = []

        class _FailIfCalledCoord:
            def status(self, **_kw: Any) -> Any:
                coord_calls.append("status")
                raise AssertionError("coordinator must not be called when skipped")

        runner = V6PilotRunner(
            coordinator=_FailIfCalledCoord(),  # type: ignore[arg-type]
            workflow_token_transport=_ScriptedTransport(pr.handle_wf),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            pdir = Path(tmpdir)
            (pdir / ".reviewsensei.yml").write_text(
                "schema: 1\ngithub:\n  writes: false\n  automatic_reviews: true\n",
                encoding="utf-8",
            )
            out_writes_off = runner.run(
                V6PilotRunConfig(
                    owner=OWNER,
                    repo=REPO,
                    repository_id=REPO_ID,
                    pull_request=PR_NUMBER,
                    head_sha=HEAD_A,
                    operation_id="op-skip-w",
                    policy_root=pdir,
                )
            )
            self.assertEqual(out_writes_off.status, "skipped")
            self.assertEqual(out_writes_off.diagnostic, "v6_policy_writes_disabled")
            self.assertEqual(coord_calls, [])

            (pdir / ".reviewsensei.yml").write_text(
                "schema: 1\ngithub:\n  writes: true\n  automatic_reviews: false\n",
                encoding="utf-8",
            )
            out_auto_off = runner.run(
                V6PilotRunConfig(
                    owner=OWNER,
                    repo=REPO,
                    repository_id=REPO_ID,
                    pull_request=PR_NUMBER,
                    head_sha=HEAD_A,
                    operation_id="op-skip-a",
                    event=GitHubEventKind.PULL_REQUEST,
                    policy_root=pdir,
                )
            )
            self.assertEqual(out_auto_off.status, "skipped")
            self.assertEqual(
                out_auto_off.diagnostic, "v6_policy_automatic_reviews_disabled"
            )
            self.assertEqual(coord_calls, [])

            # CLI returns exit code 0 when outcome is skipped.
            with (
                mock.patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test_token"}),
                mock.patch(
                    "review_sensei_github.v6_pilot_runner.run_v6_pilot",
                    return_value=out_writes_off,
                ),
                redirect_stdout(io.StringIO()),
            ):
                rc = run_v6_pilot_cli(
                    [
                        "--repository",
                        f"{OWNER}/{REPO}",
                        "--repository-id",
                        str(REPO_ID),
                        "--pull-request",
                        str(PR_NUMBER),
                        "--head-sha",
                        HEAD_A,
                        "--policy-root",
                        str(pdir),
                    ]
                )
            self.assertEqual(rc, 0)

    def test_adaptive_rest_pagination_step_down_deduplicates_across_restarts(
        self,
    ) -> None:
        from review_sensei_github.v6_pagination import (
            NoProgressGuard,
            StabilityVerifiedPaginator,
        )

        items = [{"id": idx + 1, "body": f"c-{idx + 1}"} for idx in range(15)]
        requested_urls: list[str] = []

        def _wf_paged(call: dict[str, Any]) -> tuple[int, Mapping[str, str], Any]:
            url = call["path"]
            requested_urls.append(url)
            parsed = urlsplit(url)
            qs = parse_qs(parsed.query)
            per_page = int(qs.get("per_page", ["100"])[0])
            page = int(qs.get("page", ["1"])[0])
            if per_page == 100:
                raise GitHubResponseTooLargeError()
            if per_page == 12 and page == 2:
                raise GitHubResponseTooLargeError()
            start = (page - 1) * per_page
            chunk = items[start : start + per_page]
            headers: dict[str, str] = {}
            if start + per_page < len(items):
                headers["Link"] = (
                    f'<https://api.github.com{parsed.path}?per_page={per_page}&page={page + 1}&sort=created&direction=asc>; rel="next"'
                )
            return 200, headers, chunk

        client = DualCredentialGitHubClient(
            workflow_token_transport=_ScriptedTransport(_wf_paged),
            broker_app_transport=_ScriptedTransport(lambda _c: (403, {}, {})),
        )
        paginator = StabilityVerifiedPaginator(client)
        res = paginator.scan_rest(
            endpoint_id="pr_issue_comments",
            path=f"/repos/{OWNER}/{REPO}/issues/{PR_NUMBER}/comments?per_page=100",
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            progress_guard=NoProgressGuard(operation_id="op-adapt"),
        )
        self.assertEqual([r.record_id for r in res.records], [i + 1 for i in range(15)])
        self.assertTrue(any("per_page=12" in u for u in requested_urls))
        self.assertTrue(any("per_page=5" in u for u in requested_urls))

    def test_merge_base_resolution_page2_git_commit_lca_and_fail_closed(self) -> None:
        import base64
        from unittest import mock

        from review_sensei.errors import ReviewInputError
        from review_sensei.models import ReviewResult

        base_sha = "1" * 40
        head_sha = "a" * 40
        mid_head = "9" * 40
        lca_sha = "2" * 40
        older_sha = "3" * 40
        blob_base = "4" * 40
        blob_head = "5" * 40

        seen_compare_paths: list[str] = []

        class _FakeService:
            def __init__(self, *_a: Any, **_kw: Any) -> None:
                pass

            def review(self, _req: Any) -> ReviewResult:
                return ReviewResult(
                    summary="Clean",
                    comments=(),
                    provider="openai-compatible",
                    model="gpt-4o-mini",
                    review_status="complete",
                )

        def _base_tree_and_blobs(
            path: str,
        ) -> tuple[int, Mapping[str, str], Any] | None:
            if path == f"/repos/{OWNER}/{REPO}/pulls/{PR_NUMBER}":
                return (
                    200,
                    {},
                    {
                        "state": "open",
                        "title": "Merge-base resolution",
                        "head": {"sha": head_sha},
                        "base": {"sha": base_sha},
                    },
                )
            if path == f"/repos/{OWNER}/{REPO}/git/trees/{lca_sha}?recursive=1":
                return (
                    200,
                    {},
                    {
                        "truncated": False,
                        "tree": [
                            {
                                "path": "f.py",
                                "type": "blob",
                                "sha": blob_base,
                                "mode": "100644",
                            }
                        ],
                    },
                )
            if path == f"/repos/{OWNER}/{REPO}/git/trees/{head_sha}?recursive=1":
                return (
                    200,
                    {},
                    {
                        "truncated": False,
                        "tree": [
                            {
                                "path": "f.py",
                                "type": "blob",
                                "sha": blob_head,
                                "mode": "100644",
                            }
                        ],
                    },
                )
            if path == f"/repos/{OWNER}/{REPO}/git/blobs/{blob_base}":
                return (
                    200,
                    {},
                    {
                        "sha": blob_base,
                        "encoding": "base64",
                        "content": base64.b64encode(b"x = 1\n").decode("ascii"),
                    },
                )
            if path == f"/repos/{OWNER}/{REPO}/git/blobs/{blob_head}":
                return (
                    200,
                    {},
                    {
                        "sha": blob_head,
                        "encoding": "base64",
                        "content": base64.b64encode(b"x = 2\n").decode("ascii"),
                    },
                )
            return None

        # 1. Compare page=1 >512 KiB -> resolves via page=2 (never refetching page=1 ?per_page=1)
        def _page2_handler(call: dict[str, Any]) -> tuple[int, Mapping[str, str], Any]:
            path = call["path"]
            common = _base_tree_and_blobs(path)
            if common is not None:
                return common
            if "/compare/" in path:
                seen_compare_paths.append(path)
                if path == f"/repos/{OWNER}/{REPO}/compare/{base_sha}...{head_sha}":
                    raise GitHubResponseTooLargeError()
                if path == (
                    f"/repos/{OWNER}/{REPO}/compare/{base_sha}...{head_sha}?per_page=1&page=2"
                ):
                    return 200, {}, {"merge_base_commit": {"sha": lca_sha}}
            raise AssertionError(f"unexpected call: {call}")

        with (
            mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-key"}),
            mock.patch(
                "review_sensei_github.v6_fresh_analysis.ReviewService", _FakeService
            ),
        ):
            payloads, _ = run_v6_fresh_pr_analysis(
                owner=OWNER,
                repo=REPO,
                pull_request=PR_NUMBER,
                head_sha=head_sha,
                workflow_token_transport=_ScriptedTransport(_page2_handler),
                granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
                provider="openai-compatible",
                model="gpt-4o-mini",
                provider_base_url="https://127.0.0.1:8443/v1",
                allow_custom_endpoint=True,
            )
        self.assertEqual(len(payloads), 1)
        self.assertEqual(
            seen_compare_paths,
            [
                f"/repos/{OWNER}/{REPO}/compare/{base_sha}...{head_sha}",
                f"/repos/{OWNER}/{REPO}/compare/{base_sha}...{head_sha}?per_page=1&page=2",
            ],
        )

        # 2. Compare page=1 and page=2 both fail -> resolves exact maximal LCA via git_commit
        commit_parents = {
            base_sha: [lca_sha],
            head_sha: [mid_head],
            mid_head: [lca_sha],
            lca_sha: [older_sha],
            older_sha: [],
        }

        def _lca_handler(call: dict[str, Any]) -> tuple[int, Mapping[str, str], Any]:
            path = call["path"]
            common = _base_tree_and_blobs(path)
            if common is not None:
                return common
            if "/compare/" in path:
                return 500, {}, {"message": "Compare failed"}
            if path.startswith(f"/repos/{OWNER}/{REPO}/git/commits/"):
                sha = path.rsplit("/", 1)[-1]
                return (
                    200,
                    {},
                    {
                        "sha": sha,
                        "parents": [{"sha": p} for p in commit_parents[sha]],
                    },
                )
            raise AssertionError(f"unexpected call: {call}")

        with (
            mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-key"}),
            mock.patch(
                "review_sensei_github.v6_fresh_analysis.ReviewService", _FakeService
            ),
        ):
            payloads_lca, _ = run_v6_fresh_pr_analysis(
                owner=OWNER,
                repo=REPO,
                pull_request=PR_NUMBER,
                head_sha=head_sha,
                workflow_token_transport=_ScriptedTransport(_lca_handler),
                granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
                provider="openai-compatible",
                model="gpt-4o-mini",
                provider_base_url="https://127.0.0.1:8443/v1",
                allow_custom_endpoint=True,
            )
        self.assertEqual(len(payloads_lca), 1)

        # 3. Disjoint commit graphs -> fails closed with v6_merge_base_unavailable
        def _disjoint_handler(
            call: dict[str, Any],
        ) -> tuple[int, Mapping[str, str], Any]:
            path = call["path"]
            common = _base_tree_and_blobs(path)
            if common is not None:
                return common
            if "/compare/" in path:
                raise GitHubResponseTooLargeError()
            if path.startswith(f"/repos/{OWNER}/{REPO}/git/commits/"):
                sha = path.rsplit("/", 1)[-1]
                return 200, {}, {"sha": sha, "parents": []}
            raise AssertionError(f"unexpected call: {call}")

        with (
            mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-key"}),
            self.assertRaises(ReviewInputError) as mb_exc,
        ):
            run_v6_fresh_pr_analysis(
                owner=OWNER,
                repo=REPO,
                pull_request=PR_NUMBER,
                head_sha=head_sha,
                workflow_token_transport=_ScriptedTransport(_disjoint_handler),
                granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
                provider="openai-compatible",
                model="gpt-4o-mini",
                provider_base_url="https://127.0.0.1:8443/v1",
                allow_custom_endpoint=True,
            )
        self.assertEqual(mb_exc.exception.diagnostic, "v6_merge_base_unavailable")


if __name__ == "__main__":
    unittest.main()
