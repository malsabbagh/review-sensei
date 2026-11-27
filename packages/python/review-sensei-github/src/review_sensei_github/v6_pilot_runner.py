"""ReviewSensei v6 opt-in pilot runner wiring coordinator, dual-credential client, and recovery engine."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from review_sensei.errors import ReviewInputError, ReviewSenseiError

from .errors import GitHubHTTPTransientError
from .v6_coordinator_client import (
    DEFAULT_COORDINATOR_BASE_URL,
    V6CoordinatorClient,
    V6CoordinatorError,
    V6LeaseGrant,
)
from .v6_credential_matrix import (
    WORKFLOW_READ_ONLY_SCOPES,
    CredentialKind,
    GitHubEventKind,
    RepositoryVisibility,
)
from .v6_pagination import NoProgressGuard
from .v6_recovery_engine import (
    DEFAULT_CLOCK_MARGIN_MS,
    RecoveryCompleted,
    RecoveryHold,
    RecoveryOutcome,
    RecoveryPaused,
    RecoveryRequest,
    StaleWriterOwnershipError,
    TakeoverReconciliationResult,
    V6RecoveryEngine,
    render_v6_hold_summary,
    write_v6_step_summary,
)
from .v6_recovery_ledger import (
    CompletionReceiptRecord,
    OwnerMarkerRecord,
    WriterOwnershipProof,
    redact_secrets,
)
from .v6_transport import DualCredentialGitHubClient, DualCredentialTransport

_SHA1_40_RE = re.compile(r"^[0-9a-f]{40}$")
_REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_MAX_GITHUB_BODY_BYTES = 512 * 1024


def _iso_utc_from_ms(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _app_id_from_slot_key(slot_key: str) -> int | None:
    """Extract positive GitHub App ID from canonical ``v6-pr:<host>:<app_id>:...`` key."""

    parts = slot_key.split(":")
    if len(parts) == 6 and parts[0] == "v6-pr" and parts[2].isdigit():
        parsed = int(parts[2])
        return parsed if parsed > 0 else None
    return None


def _build_initial_result_loader(
    config: V6PilotRunConfig,
) -> Callable[[], tuple[Sequence[Mapping[str, Any]], Sequence[str]]] | None:
    """Return a lazy initial findings loader without touching disk unless invoked."""

    if config.initial_result_loader is not None:
        return config.initial_result_loader
    if config.initial_findings_path is None:
        return None
    findings_path = config.initial_findings_path

    def _load() -> tuple[Sequence[Mapping[str, Any]], Sequence[str]]:
        raw = json.loads(findings_path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            findings_raw: Any = raw
            digests_raw: Any = ()
        elif isinstance(raw, Mapping):
            findings_raw = raw.get("findings", ())
            digests_raw = raw.get("required_evidence_digests", ())
        else:
            raise ReviewInputError(
                "initial findings JSON must be an object or list",
                diagnostic="v6_invalid_initial_findings",
            )
        if (
            not isinstance(findings_raw, Sequence)
            or isinstance(findings_raw, (str, bytes, bytearray))
            or not all(isinstance(entry, Mapping) for entry in findings_raw)
        ):
            raise ReviewInputError(
                "initial findings entries must be JSON objects",
                diagnostic="v6_invalid_initial_findings",
            )
        if (
            not isinstance(digests_raw, Sequence)
            or isinstance(digests_raw, (str, bytes, bytearray))
            or not all(isinstance(item, str) and item.strip() for item in digests_raw)
        ):
            raise ReviewInputError(
                "required_evidence_digests must be non-empty strings",
                diagnostic="v6_invalid_initial_findings",
            )
        return tuple(dict(entry) for entry in findings_raw), tuple(digests_raw)

    return _load


class _FailClosedReadOnlyAppTransport:
    """Stub broker transport used during read-only pre-lease reconciliation."""

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[int, Mapping[str, str], Any]:
        raise StaleWriterOwnershipError(
            f"refusing App call {method} {path} without an active coordinator write lease",
            diagnostic="v6_no_active_write_lease",
        )


class UrllibGitHubTransport:
    """Bounded urllib-backed ``DualCredentialTransport`` redacting bearer credentials."""

    def __init__(
        self,
        *,
        token: str,
        api_url: str = "https://api.github.com",
        opener: Callable[..., Any] = urlopen,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not isinstance(token, str) or not token.strip():
            raise ReviewInputError(
                "GitHub transport token must be non-empty",
                diagnostic="v6_missing_github_token",
            )
        self._token = token.strip()
        self.api_url = api_url.rstrip("/")
        self.opener = opener
        self.timeout_seconds = float(timeout_seconds)

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[int, Mapping[str, str], Any]:
        if not isinstance(path, str) or not path.startswith("/") or "\n" in path:
            raise ReviewInputError(
                f"invalid GitHub API path {path!r}", diagnostic="v6_invalid_path"
            )
        req_headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": "ReviewSensei-GitHub-App/1.0 (+https://reviewsensei.dev)",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if headers:
            for k, v in headers.items():
                if k.lower() != "authorization":
                    req_headers[k] = v
        payload_bytes: bytes | None = None
        if body is not None:
            payload_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
            req_headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.api_url}{path}",
            data=payload_bytes,
            headers=req_headers,
            method=method,
        )
        effective_timeout = (
            self.timeout_seconds
            if timeout_seconds is None
            else min(self.timeout_seconds, float(timeout_seconds))
        )
        try:
            with self.opener(request, timeout=effective_timeout) as response:
                status = int(getattr(response, "status", 200))
                resp_headers = (
                    dict(response.headers.items())
                    if getattr(response, "headers", None) is not None
                    else {}
                )
                raw = response.read(_MAX_GITHUB_BODY_BYTES + 1)
                parsed = (
                    json.loads(bytes(raw).decode("utf-8", errors="strict"))
                    if raw
                    else None
                )
                return status, resp_headers, parsed
        except HTTPError as exc:
            err_headers = dict(exc.headers.items()) if exc.headers is not None else {}
            parsed_err: Any = None
            try:
                raw_err = exc.read(_MAX_GITHUB_BODY_BYTES + 1)
                if raw_err:
                    parsed_err = json.loads(
                        bytes(raw_err).decode("utf-8", errors="replace")
                    )
            except Exception:
                parsed_err = None
            finally:
                try:
                    exc.close()
                except Exception:
                    pass
            return exc.code, err_headers, parsed_err
        except (URLError, TimeoutError, OSError) as exc:
            raise GitHubHTTPTransientError(
                redact_secrets(f"GitHub HTTP request failed ({type(exc).__name__})")
            ) from exc


@dataclass(frozen=True)
class V6PilotRunConfig:
    """Configuration for one v6 pilot runner execution."""

    owner: str
    repo: str
    repository_id: int
    pull_request: int
    head_sha: str
    operation_id: str
    event: GitHubEventKind = GitHubEventKind.PULL_REQUEST
    visibility: RepositoryVisibility = RepositoryVisibility.PUBLIC
    granted_scopes: Collection[str] = WORKFLOW_READ_ONLY_SCOPES
    capabilities: Sequence[str] | Mapping[str, str] = ("review_publish",)
    run_id: int = 1
    run_attempt: int = 1
    workflow_sha: str = "0" * 40
    host: str = "github.com"
    slot_key: str | None = None
    expected_app_id: int | None = None
    expected_bot_login: str | None = None
    expected_bot_user_id: int | None = None
    effect_kind: str = "pr_review_publish"
    effect_endpoint_id: str = "pr_review_publish"
    effect_path: str | None = None
    observed_evidence_digests: tuple[str, ...] | None = None
    affirmative_non_submission: bool = False
    graphql_query: str | None = None
    mac_verifier: Callable[[OwnerMarkerRecord], bool] | None = None
    step_summary_sink: Callable[[str], None] | Path | None = None
    outcome_path: Path | None = None
    clock_margin_ms: int = DEFAULT_CLOCK_MARGIN_MS
    initial_findings_path: Path | None = None
    initial_result_loader: (
        Callable[[], tuple[Sequence[Mapping[str, Any]], Sequence[str]]] | None
    ) = None


@dataclass(frozen=True)
class V6PilotRunOutcome:
    """Structured outcome returned by :class:`V6PilotRunner`."""

    status: str
    slot_key: str | None = None
    coordinator_state: str | None = None
    generation: int | None = None
    lease_epoch: int | None = None
    recovery_outcome: RecoveryOutcome | None = None
    takeover_reconciliation: TakeoverReconciliationResult | None = None
    diagnostic: str | None = None
    lease_released: bool = False
    model_calls_started: int = 0
    _details: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """Serialize a secret-redacted outcome dictionary."""

        payload: dict[str, Any] = {
            "protocol": "v6",
            "status": self.status,
            "slot_key": self.slot_key,
            "coordinator_state": self.coordinator_state,
            "generation": self.generation,
            "lease_epoch": self.lease_epoch,
            "diagnostic": (
                redact_secrets(self.diagnostic)
                if isinstance(self.diagnostic, str)
                else None
            ),
            "lease_released": self.lease_released,
            "model_calls_started": self.model_calls_started,
        }
        if isinstance(self.recovery_outcome, RecoveryCompleted):
            payload["github_effect_id"] = self.recovery_outcome.github_effect_id
            payload["reconciled_from_existing_effect"] = (
                self.recovery_outcome.reconciled_from_existing_effect
            )
            payload["receipt_record_id"] = self.recovery_outcome.receipt.record_id
        elif isinstance(self.recovery_outcome, RecoveryHold):
            payload["blocker"] = redact_secrets(self.recovery_outcome.blocker)
            payload["missing_evidence"] = [
                redact_secrets(item) for item in self.recovery_outcome.missing_evidence
            ]
        elif isinstance(self.recovery_outcome, RecoveryPaused):
            payload["resume_after_epoch"] = self.recovery_outcome.resume_after_epoch
            payload["rerun_command"] = self.recovery_outcome.rerun_command
        return payload


class V6PilotRunner:
    """Coordinates v6 lease acquisition, takeover reconciliation, and PR recovery."""

    def __init__(
        self,
        *,
        coordinator: V6CoordinatorClient,
        workflow_token_transport: DualCredentialTransport,
        app_transport_factory: Callable[[V6LeaseGrant], DualCredentialTransport]
        | None = None,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
        broker_fence_verifier: Callable[[WriterOwnershipProof], Mapping[str, Any]]
        | None = None,
        is_ghec: bool = False,
        github_api_url: str = "https://api.github.com",
    ) -> None:
        self.coordinator = coordinator
        self.workflow_token_transport = workflow_token_transport
        self.app_transport_factory = app_transport_factory or (
            lambda grant: UrllibGitHubTransport(
                token=grant.token, api_url=github_api_url
            )
        )
        self.clock = clock
        self.sleeper = sleeper
        self.broker_fence_verifier = broker_fence_verifier
        self.is_ghec = is_ghec

    def _emit_hold_summary(
        self,
        config: V6PilotRunConfig,
        *,
        slot_key: str | None,
        blocker: str,
        missing_evidence: tuple[str, ...] = (),
    ) -> None:
        write_v6_step_summary(
            render_v6_hold_summary(
                operation_id=config.operation_id,
                blocker=blocker,
                missing_evidence=missing_evidence,
                slot_key=slot_key,
                repository=f"{config.owner}/{config.repo}",
                pull_request=config.pull_request,
                pr_head_sha=config.head_sha,
            ),
            sink=config.step_summary_sink,
        )

    def _write_outcome(
        self, config: V6PilotRunConfig, outcome: V6PilotRunOutcome
    ) -> V6PilotRunOutcome:
        if config.outcome_path is not None:
            config.outcome_path.parent.mkdir(parents=True, exist_ok=True)
            config.outcome_path.write_text(
                json.dumps(outcome.to_dict(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        return outcome

    def _default_fence_verifier(
        self,
        *,
        config: V6PilotRunConfig,
        grant: V6LeaseGrant,
        client: DualCredentialGitHubClient,
    ) -> Callable[[WriterOwnershipProof], Mapping[str, Any]]:
        def _verify(proof: WriterOwnershipProof) -> Mapping[str, Any]:
            pr_resp = client.execute(
                "broker_pr_binding_fence",
                method="GET",
                path=f"/repos/{config.owner}/{config.repo}/pulls/{config.pull_request}",
                event=config.event,
                visibility=config.visibility,
                granted_scopes=config.granted_scopes,
                credential_kind=CredentialKind.APP_BROKER,
                now_epoch=self.clock(),
            )
            run_resp = client.execute(
                "broker_active_writer_fence",
                method="GET",
                path=f"/repos/{config.owner}/{config.repo}/actions/runs/{proof.run_id}",
                event=config.event,
                visibility=config.visibility,
                granted_scopes=config.granted_scopes,
                credential_kind=CredentialKind.APP_BROKER,
                now_epoch=self.clock(),
            )
            slot_status = self.coordinator.status(
                pull_request=config.pull_request,
                slot_key=grant.slot_key,
            )
            pr_body = pr_resp.body if isinstance(pr_resp.body, Mapping) else {}
            run_body = run_resp.body if isinstance(run_resp.body, Mapping) else {}
            raw_head = pr_body.get("head")
            head_info: Mapping[str, Any] = (
                raw_head if isinstance(raw_head, Mapping) else {}
            )
            raw_base = pr_body.get("base")
            base_info: Mapping[str, Any] = (
                raw_base if isinstance(raw_base, Mapping) else {}
            )
            raw_base_repo = base_info.get("repo")
            base_repo: Mapping[str, Any] = (
                raw_base_repo if isinstance(raw_base_repo, Mapping) else {}
            )
            if (
                slot_status.state != "ACTIVE"
                or slot_status.reservation_id != grant.reservation_id
                or slot_status.generation != grant.generation
            ):
                raise StaleWriterOwnershipError(
                    f"coordinator slot {grant.slot_key} no longer ACTIVE for this reservation",
                    diagnostic=slot_status.last_reason_code or "v6_lease_hold",
                )
            return {
                "repository_id": base_repo.get("id"),
                "pull_request": config.pull_request,
                "pr_head_sha": head_info.get("sha"),
                "pr_state": pr_body.get("state"),
                "active_writer_epoch": slot_status.lease_epoch,
                "active_run_id": run_body.get("id"),
                "active_run_attempt": run_body.get("run_attempt"),
                "run_status": run_body.get("status"),
                "verified_by_broker": True,
            }

        return _verify

    def run(self, config: V6PilotRunConfig) -> V6PilotRunOutcome:
        """Execute one full v6 coordinator + PR-governed recovery lifecycle."""

        read_only_client = DualCredentialGitHubClient(
            workflow_token_transport=self.workflow_token_transport,
            broker_app_transport=_FailClosedReadOnlyAppTransport(),
            is_ghec=self.is_ghec,
        )
        read_only_engine = V6RecoveryEngine(
            read_only_client,
            sleeper=self.sleeper,
            now_fn=self.clock,
        )

        try:
            slot_status = self.coordinator.status(
                pull_request=config.pull_request,
                slot_key=config.slot_key,
            )
        except V6CoordinatorError as exc:
            diag = exc.diagnostic or "v6_coordinator_status_failed"
            self._emit_hold_summary(
                config,
                slot_key=config.slot_key,
                blocker=diag,
                missing_evidence=(str(exc),),
            )
            return self._write_outcome(
                config,
                V6PilotRunOutcome(
                    status="hold",
                    slot_key=config.slot_key,
                    diagnostic=diag,
                ),
            )

        slot_key = slot_status.slot_key
        effective_expected_app_id = (
            config.expected_app_id
            if config.expected_app_id is not None
            else _app_id_from_slot_key(slot_key)
        )
        current_state = slot_status.state
        current_generation = slot_status.generation
        current_epoch = slot_status.lease_epoch
        token_cleared = slot_status.token_cleared
        anchor_comment_id = slot_status.owner_comment_id
        takeover_recon: TakeoverReconciliationResult | None = None

        if current_state == "HOLD":
            diag = slot_status.last_reason_code or "v6_lease_hold"
            self._emit_hold_summary(config, slot_key=slot_key, blocker=diag)
            return self._write_outcome(
                config,
                V6PilotRunOutcome(
                    status="hold",
                    slot_key=slot_key,
                    coordinator_state=current_state,
                    generation=current_generation,
                    lease_epoch=current_epoch,
                    diagnostic=diag,
                ),
            )

        if current_state in {"RESERVED", "MINTING"}:
            diag = slot_status.last_reason_code or "v6_lease_reserved"
            self._emit_hold_summary(config, slot_key=slot_key, blocker=diag)
            return self._write_outcome(
                config,
                V6PilotRunOutcome(
                    status="hold",
                    slot_key=slot_key,
                    coordinator_state=current_state,
                    generation=current_generation,
                    lease_epoch=current_epoch,
                    diagnostic=diag,
                ),
            )

        if current_state in {"ACTIVE", "DRAINING"} or (
            current_state == "RECONCILING" and not token_cleared
        ):
            try:
                drain_res = self.coordinator.drain(
                    slot_key=slot_key,
                    expected_generation=current_generation,
                    expected_lease_epoch=current_epoch,
                    predecessor_anchor_comment_id=anchor_comment_id,
                )
            except V6CoordinatorError as exc:
                diag = exc.diagnostic or "v6_lease_draining"
                self._emit_hold_summary(
                    config,
                    slot_key=slot_key,
                    blocker=diag,
                    missing_evidence=(str(exc),),
                )
                return self._write_outcome(
                    config,
                    V6PilotRunOutcome(
                        status="hold",
                        slot_key=slot_key,
                        coordinator_state=current_state,
                        generation=current_generation,
                        lease_epoch=current_epoch,
                        diagnostic=diag,
                    ),
                )
            current_state = drain_res.state
            current_generation = drain_res.generation
            current_epoch = drain_res.lease_epoch
            token_cleared = drain_res.token_cleared
            if current_state == "HOLD" or not token_cleared:
                diag = (
                    "v6_lease_hold"
                    if current_state == "HOLD"
                    else "v6_lease_revocation_uncertain"
                )
                self._emit_hold_summary(
                    config,
                    slot_key=slot_key,
                    blocker=diag,
                    missing_evidence=(
                        f"revocation_status:{drain_res.revocation_status}",
                    ),
                )
                return self._write_outcome(
                    config,
                    V6PilotRunOutcome(
                        status="hold",
                        slot_key=slot_key,
                        coordinator_state=current_state,
                        generation=current_generation,
                        lease_epoch=current_epoch,
                        diagnostic=diag,
                    ),
                )

        if current_state == "RECONCILING":
            takeover_recon = read_only_engine.reconcile_takeover_read_only(
                owner=config.owner,
                repo=config.repo,
                repository_id=config.repository_id,
                pull_request=config.pull_request,
                slot_key=slot_key,
                event=config.event,
                visibility=config.visibility,
                granted_scopes=config.granted_scopes,
                old_generation=current_generation,
                old_lease_epoch=current_epoch,
                expected_app_id=effective_expected_app_id,
                expected_bot_login=config.expected_bot_login,
                expected_bot_user_id=config.expected_bot_user_id,
                mac_verifier=config.mac_verifier,
                affirmative_non_submission_ops=(
                    (config.operation_id,) if config.affirmative_non_submission else ()
                ),
                graphql_query=config.graphql_query,
                step_summary_sink=config.step_summary_sink,
                operation_id=config.operation_id,
            )
            if takeover_recon.old_owner_comment_id is not None:
                anchor_comment_id = takeover_recon.old_owner_comment_id
            elif takeover_recon.anchor_comment_id is not None:
                anchor_comment_id = takeover_recon.anchor_comment_id

            recon_evidence: Mapping[str, Any] = (
                takeover_recon.reconciliation_evidence
                if takeover_recon.reconciliation_evidence is not None
                else {
                    "old_generation": max(1, current_generation),
                    "intent_resolutions": [],
                    **(
                        {"old_owner_comment_id": anchor_comment_id}
                        if anchor_comment_id is not None
                        else {}
                    ),
                }
            )
            try:
                comp = self.coordinator.complete_reconcile(
                    slot_key=slot_key,
                    generation=current_generation,
                    reconciliation_outcome=takeover_recon.reconciliation_outcome,
                    reconciliation_evidence=recon_evidence,
                    anchor_comment_id=anchor_comment_id,
                    head_sha=config.head_sha,
                )
            except V6CoordinatorError as exc:
                diag = exc.diagnostic or "v6_lease_reconciling"
                self._emit_hold_summary(
                    config,
                    slot_key=slot_key,
                    blocker=diag,
                    missing_evidence=(str(exc),),
                )
                return self._write_outcome(
                    config,
                    V6PilotRunOutcome(
                        status="hold",
                        slot_key=slot_key,
                        coordinator_state=current_state,
                        generation=current_generation,
                        lease_epoch=current_epoch,
                        takeover_reconciliation=takeover_recon,
                        diagnostic=diag,
                    ),
                )
            current_state = comp.state
            current_generation = comp.generation
            current_epoch = comp.lease_epoch
            if current_state != "IDLE" or takeover_recon.reconciliation_outcome in {
                "ambiguous_hold",
                "incomplete",
            }:
                diag = takeover_recon.blocker or "v6_lease_hold"
                return self._write_outcome(
                    config,
                    V6PilotRunOutcome(
                        status="hold",
                        slot_key=slot_key,
                        coordinator_state=current_state,
                        generation=current_generation,
                        lease_epoch=current_epoch,
                        takeover_reconciliation=takeover_recon,
                        diagnostic=diag,
                    ),
                )

        if current_state != "IDLE":
            diag = f"v6_unexpected_coordinator_state:{current_state}"
            self._emit_hold_summary(config, slot_key=slot_key, blocker=diag)
            return self._write_outcome(
                config,
                V6PilotRunOutcome(
                    status="hold",
                    slot_key=slot_key,
                    coordinator_state=current_state,
                    generation=current_generation,
                    lease_epoch=current_epoch,
                    takeover_reconciliation=takeover_recon,
                    diagnostic=diag,
                ),
            )

        try:
            grant = self.coordinator.acquire(
                pull_request=config.pull_request,
                head_sha=config.head_sha,
                capabilities=config.capabilities,
                operation_id=config.operation_id,
                predecessor_anchor_comment_id=anchor_comment_id,
            )
        except V6CoordinatorError as exc:
            diag = exc.diagnostic or "v6_lease_acquire_refused"
            self._emit_hold_summary(
                config, slot_key=slot_key, blocker=diag, missing_evidence=(str(exc),)
            )
            return self._write_outcome(
                config,
                V6PilotRunOutcome(
                    status="hold",
                    slot_key=slot_key,
                    coordinator_state=current_state,
                    generation=current_generation,
                    lease_epoch=current_epoch,
                    takeover_reconciliation=takeover_recon,
                    diagnostic=diag,
                ),
            )

        lease_released = False
        final_state = grant.state
        outcome_status = "hold"
        diag_code: str | None = None
        recovery_outcome: RecoveryOutcome | None = None
        current_gen_owner_cid: int | None = None
        current_gen_resolutions: list[dict[str, Any]] = []
        receipt_verified_before_release = False
        try:
            app_transport = self.app_transport_factory(grant)
            dual_client = DualCredentialGitHubClient(
                workflow_token_transport=self.workflow_token_transport,
                broker_app_transport=app_transport,
                is_ghec=self.is_ghec,
            )
            fence_verifier = (
                self.broker_fence_verifier
                if self.broker_fence_verifier is not None
                else self._default_fence_verifier(
                    config=config, grant=grant, client=dual_client
                )
            )
            engine = V6RecoveryEngine(
                dual_client,
                sleeper=self.sleeper,
                now_fn=self.clock,
                broker_fence_verifier=fence_verifier,
            )
            now_ms = int(self.clock() * 1000)
            owner_marker = OwnerMarkerRecord(
                record_id=(
                    f"owner:{config.operation_id}:{grant.generation}:{grant.lease_epoch}"
                ),
                slot_key=grant.slot_key,
                generation=grant.generation,
                lease_epoch=grant.lease_epoch,
                reservation_id=grant.reservation_id,
                operation_id=config.operation_id,
                run_id=str(config.run_id),
                run_attempt=config.run_attempt,
                pr_head_sha=grant.head_sha,
                workflow_sha=config.workflow_sha,
                acquired_at=_iso_utc_from_ms(now_ms),
                expires_at=_iso_utc_from_ms(grant.token_expires_at_ms),
                coordinator_attestation_mac=grant.coordinator_attestation_mac,
            )
            proof = grant.to_ownership_proof(
                operation_id=config.operation_id,
                run_id=config.run_id,
                run_attempt=config.run_attempt,
            )
            request = RecoveryRequest(
                owner=config.owner,
                repo=config.repo,
                repository_id=config.repository_id,
                pull_request=config.pull_request,
                operation_id=config.operation_id,
                ownership_proof=proof,
                event=config.event,
                visibility=config.visibility,
                granted_scopes=config.granted_scopes,
                effect_kind=config.effect_kind,
                effect_endpoint_id=config.effect_endpoint_id,
                effect_path=config.effect_path,
                expected_app_id=effective_expected_app_id or grant.app_id,
                expected_bot_login=config.expected_bot_login,
                expected_bot_user_id=config.expected_bot_user_id,
                observed_evidence_digests=config.observed_evidence_digests,
                graphql_query=config.graphql_query,
                slot_key=grant.slot_key,
                generation=grant.generation,
                token_expires_at_ms=grant.token_expires_at_ms,
                clock_margin_ms=config.clock_margin_ms,
                owner_marker=owner_marker,
                mac_verifier=config.mac_verifier,
                affirmative_non_submission=config.affirmative_non_submission,
                has_active_write_lease=True,
                emit_durable_hold_on_pr=True,
                step_summary_sink=config.step_summary_sink,
                initial_result_loader=_build_initial_result_loader(config),
            )

            if takeover_recon is not None and takeover_recon.adopted_effects:
                guard = NoProgressGuard(operation_id=config.operation_id)
                current_gen_owner_cid = engine._persist_owner_marker_if_needed(
                    request=request,
                    guard=guard,
                    prior_hint=None,
                    existing_owners=takeover_recon.owner_lineage,
                )
                adopted_receipt: CompletionReceiptRecord | None = None
                adopted_effect_id: str | int | None = None
                adopted_receipt_cid: int | None = None
                for adopted_intent, effect_id in takeover_recon.adopted_effects:
                    op_proof = grant.to_ownership_proof(
                        operation_id=adopted_intent.operation_id,
                        run_id=config.run_id,
                        run_attempt=config.run_attempt,
                    )
                    op_request = replace(
                        request,
                        operation_id=adopted_intent.operation_id,
                        ownership_proof=op_proof,
                        effect_kind=adopted_intent.effect_kind,
                    )
                    rec, _, rcpt_cid = engine._persist_completion_receipt(
                        request=op_request,
                        guard=guard,
                        prior_hint=None,
                        github_effect_id=effect_id,
                        payload_sha256=adopted_intent.payload_sha256,
                    )
                    if adopted_intent.operation_id == config.operation_id:
                        adopted_receipt = rec
                        adopted_effect_id = effect_id
                        adopted_receipt_cid = rcpt_cid
                if adopted_receipt is not None and adopted_effect_id is not None:
                    recovery_outcome = RecoveryCompleted(
                        operation_id=config.operation_id,
                        pr_head_sha=grant.head_sha,
                        receipt=adopted_receipt,
                        github_effect_id=adopted_effect_id,
                        reconciled_from_existing_effect=True,
                        used_pr_resident_result=False,
                        readback_gets_emitted=0,
                        model_calls_started=0,
                        owner_comment_id=current_gen_owner_cid,
                        receipt_comment_id=adopted_receipt_cid,
                    )
                    receipt_verified_before_release = adopted_receipt_cid is not None

            if recovery_outcome is None:
                recovery_outcome = engine.recover_and_publish(request)
                if isinstance(recovery_outcome, RecoveryCompleted):
                    if recovery_outcome.owner_comment_id is not None:
                        current_gen_owner_cid = recovery_outcome.owner_comment_id
                    if (
                        recovery_outcome.intent_record is not None
                        and recovery_outcome.intent_record.generation
                        == grant.generation
                    ):
                        if (
                            recovery_outcome.intent_comment_id is not None
                            and recovery_outcome.receipt_comment_id is not None
                        ):
                            current_gen_resolutions.append(
                                {
                                    "operation_id": recovery_outcome.operation_id,
                                    "intent_comment_id": recovery_outcome.intent_comment_id,
                                    "resolution": "receipt_present",
                                    "receipt_comment_id": recovery_outcome.receipt_comment_id,
                                }
                            )
                            receipt_verified_before_release = True
                        else:
                            receipt_verified_before_release = False
                    else:
                        receipt_verified_before_release = (
                            recovery_outcome.receipt_comment_id is not None
                        )

            if isinstance(recovery_outcome, RecoveryCompleted):
                outcome_status = "completed"
                diag_code = None
            elif isinstance(recovery_outcome, RecoveryPaused):
                outcome_status = "paused"
                diag_code = recovery_outcome.reason
            else:
                outcome_status = "hold"
                diag_code = recovery_outcome.blocker
        except (ReviewSenseiError, V6CoordinatorError) as exc:
            diag_code = getattr(exc, "diagnostic", None) or "v6_runner_failed_closed"
            self._emit_hold_summary(
                config,
                slot_key=grant.slot_key,
                blocker=str(diag_code),
                missing_evidence=(redact_secrets(str(exc)),),
            )
            recovery_outcome = RecoveryHold(
                operation_id=config.operation_id,
                blocker=str(diag_code),
                missing_evidence=(redact_secrets(str(exc)),),
                zero_model_calls_started=True,
            )
            outcome_status = "hold"
            receipt_verified_before_release = False
        finally:
            try:
                rel = self.coordinator.release(
                    slot_key=grant.slot_key,
                    generation=grant.generation,
                    lease_epoch=grant.lease_epoch,
                    reservation_id=grant.reservation_id,
                )
                final_state = rel.state
                if (
                    outcome_status == "completed"
                    and receipt_verified_before_release
                    and rel.state == "RECONCILING"
                    and rel.token_cleared
                ):
                    clean_evidence: dict[str, Any] = {
                        "old_generation": grant.generation,
                        "intent_resolutions": current_gen_resolutions,
                    }
                    if current_gen_owner_cid is not None:
                        clean_evidence["old_owner_comment_id"] = current_gen_owner_cid
                    comp2 = self.coordinator.complete_reconcile(
                        slot_key=grant.slot_key,
                        generation=grant.generation,
                        reconciliation_outcome="clean_receipts",
                        reconciliation_evidence=clean_evidence,
                        anchor_comment_id=current_gen_owner_cid,
                        head_sha=grant.head_sha,
                    )
                    final_state = comp2.state
                    lease_released = comp2.state == "IDLE"
                else:
                    lease_released = False
                    if outcome_status == "completed":
                        outcome_status = "hold"
                        diag_code = diag_code or "v6_lease_revocation_uncertain"
            except (V6CoordinatorError, ReviewSenseiError, OSError, ValueError) as exc:
                lease_released = False
                if diag_code is None:
                    diag_code = getattr(exc, "diagnostic", None)

        model_calls_started = (
            recovery_outcome.model_calls_started
            if isinstance(recovery_outcome, (RecoveryCompleted, RecoveryPaused))
            else 0
        )
        return self._write_outcome(
            config,
            V6PilotRunOutcome(
                status=outcome_status,
                slot_key=grant.slot_key,
                coordinator_state=final_state,
                generation=grant.generation,
                lease_epoch=grant.lease_epoch,
                recovery_outcome=recovery_outcome,
                takeover_reconciliation=takeover_recon,
                diagnostic=diag_code,
                lease_released=lease_released,
                model_calls_started=model_calls_started,
            ),
        )


def run_v6_pilot(
    config: V6PilotRunConfig,
    *,
    coordinator: V6CoordinatorClient,
    workflow_token_transport: DualCredentialTransport,
    app_transport_factory: Callable[[V6LeaseGrant], DualCredentialTransport]
    | None = None,
    clock: Callable[[], float] = time.time,
    sleeper: Callable[[float], None] = time.sleep,
    broker_fence_verifier: Callable[[WriterOwnershipProof], Mapping[str, Any]]
    | None = None,
    is_ghec: bool = False,
    github_api_url: str = "https://api.github.com",
) -> V6PilotRunOutcome:
    """Convenience entry point running :class:`V6PilotRunner`."""

    runner = V6PilotRunner(
        coordinator=coordinator,
        workflow_token_transport=workflow_token_transport,
        app_transport_factory=app_transport_factory,
        clock=clock,
        sleeper=sleeper,
        broker_fence_verifier=broker_fence_verifier,
        is_ghec=is_ghec,
        github_api_url=github_api_url,
    )
    return runner.run(config)


def run_v6_pilot_cli(argv: Sequence[str]) -> int:
    """CLI handler for ``review-sensei-github v6-pilot-run``."""

    parser = argparse.ArgumentParser(prog="review-sensei-github v6-pilot-run")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--repository-id", type=int, required=True)
    parser.add_argument("--pull-request", type=int, required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--operation-id", default=None)
    parser.add_argument(
        "--event",
        choices=("pull_request", "issue_comment", "workflow_dispatch"),
        default="pull_request",
    )
    parser.add_argument(
        "--visibility",
        choices=("public", "private", "fork_pull_request"),
        default="public",
    )
    parser.add_argument("--coordinator-url", default=DEFAULT_COORDINATOR_BASE_URL)
    parser.add_argument(
        "--github-api-url",
        default=os.environ.get("GITHUB_API_URL") or "https://api.github.com",
    )
    parser.add_argument("--run-id", type=int, default=None)
    parser.add_argument("--run-attempt", type=int, default=None)
    parser.add_argument("--workflow-sha", default=None)
    parser.add_argument("--effect-kind", default="pr_review_publish")
    parser.add_argument(
        "--outcome", "--publication-outcome", dest="outcome", type=Path, default=None
    )
    parser.add_argument("--step-summary-path", type=Path, default=None)
    parser.add_argument("--initial-findings-json", type=Path, default=None)
    parser.add_argument("--expected-bot-login", default=None)
    parser.add_argument("--expected-app-id", type=int, default=None)
    parser.add_argument(
        "--affirmative-non-submission", action="store_true", default=False
    )
    options = parser.parse_args(list(argv))

    if _REPO_SLUG_RE.fullmatch(options.repository) is None:
        raise ReviewInputError("repository slug is invalid")
    if options.repository_id <= 0 or options.pull_request <= 0:
        raise ReviewInputError("repository_id and pull_request must be positive")
    if options.expected_app_id is not None and options.expected_app_id <= 0:
        raise ReviewInputError("expected_app_id must be positive")
    if _SHA1_40_RE.fullmatch(options.head_sha) is None:
        raise ReviewInputError("head_sha must be a 40-hex SHA-1")

    owner, repo = options.repository.split("/", 1)
    run_id = (
        options.run_id
        if options.run_id is not None
        else int(os.environ.get("GITHUB_RUN_ID", "1"))
    )
    run_attempt = (
        options.run_attempt
        if options.run_attempt is not None
        else int(os.environ.get("GITHUB_RUN_ATTEMPT", "1"))
    )
    workflow_sha = (
        options.workflow_sha
        or os.environ.get("REVIEW_SENSEI_WORKFLOW_SHA")
        or os.environ.get("GITHUB_SHA")
        or ("0" * 40)
    )
    if _SHA1_40_RE.fullmatch(workflow_sha) is None:
        raise ReviewInputError("workflow_sha must be a 40-hex SHA-1")

    gh_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not gh_token:
        raise ReviewInputError(
            "GITHUB_TOKEN is required for read-only PR bulk scans",
            diagnostic="v6_missing_github_token",
        )
    operation_id = (
        options.operation_id
        or f"v6:{options.repository_id}:{options.pull_request}:{options.head_sha[:12]}:{run_id}:{run_attempt}"
    )

    coordinator = V6CoordinatorClient(base_url=options.coordinator_url)
    wf_transport = UrllibGitHubTransport(
        token=gh_token,
        api_url=options.github_api_url,
    )
    config = V6PilotRunConfig(
        owner=owner,
        repo=repo,
        repository_id=options.repository_id,
        pull_request=options.pull_request,
        head_sha=options.head_sha,
        operation_id=operation_id,
        event=GitHubEventKind(options.event),
        visibility=RepositoryVisibility(options.visibility),
        run_id=run_id,
        run_attempt=run_attempt,
        workflow_sha=workflow_sha,
        expected_app_id=options.expected_app_id,
        expected_bot_login=options.expected_bot_login,
        effect_kind=options.effect_kind,
        affirmative_non_submission=options.affirmative_non_submission,
        step_summary_sink=options.step_summary_path,
        outcome_path=options.outcome,
        initial_findings_path=options.initial_findings_json,
    )
    outcome = run_v6_pilot(
        config,
        coordinator=coordinator,
        workflow_token_transport=wf_transport,
        github_api_url=options.github_api_url,
    )
    sys.stdout.write(json.dumps(outcome.to_dict(), indent=2, sort_keys=True) + "\n")
    return 0 if outcome.status in {"completed", "paused"} else 1
