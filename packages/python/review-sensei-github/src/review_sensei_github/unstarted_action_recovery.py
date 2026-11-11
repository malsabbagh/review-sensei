"""Narrow authenticated recovery for unstarted actions after source mutation (G32).

When an admitted command or mention-reply action leaves ``action.phase == "pending"``
with ``execution_started == False`` and zero uncertain dispatches because its live
GitHub source comment was edited or deleted before ``begin_action_effect``, default
execution and rollover remain held fail-closed.

This module provides a strictly opt-in, process-authenticated cancellation/supersession
witness (``UnstartedCancellationWitness``) that never reuses ``controlled_restart``
whole-history abandonment and never activates ``controlled_restarts.json``.
Every transition requires:
1. Stranded ``action.phase == "pending"`` with ``execution_started is False``,
   null result/outcome, zero uncertain requests, and no active review reservation.
2. Exact binding to the durable action intent, session identity, and root digest.
3. A fresh metered GitHub source read proving ``source_edited`` (changed ``updated_at``
   or ``body_sha256``) or ``source_deleted`` (HTTP 404). Unchanged sources refuse so
   ordinary same-source succession executes normally.
4. Positive writer exclusion/cessation proof via ``ApiRunActionsEvidence`` or an
   active ``ContainedForwardingCoordinator`` epoch capability.
"""

from __future__ import annotations

import hashlib
import re
import weakref
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final, cast

from review_sensei.api_work import (
    UNSTARTED_CANCELLATION_REASONS,
    ApiExecutionOwner,
    ApiRunAccounting,
)
from review_sensei.bounded_evidence import (
    MAX_PARTITION_DECODED_BYTES,
    canonical_bytes,
    encode_evidence,
    inline_manifest,
    read_inline_evidence,
)
from review_sensei.errors import ReviewInputError
from review_sensei.recovery_authority import AuthorityRefusal
from review_sensei.session import (
    SessionIdentity,
    SessionRecord,
    _parse_aware_datetime,
)
from review_sensei.writer_containment import (
    ContainedForwardingCoordinator,
    IssuedWriterToken,
    extract_unsettled_dispatches,
)

from .api_run_history import (
    action_result_binding,
    decode_action_result_carrier,
)
from .api_run_owner import ApiRunActionsEvidence
from .api_run_requests import json_copy

if TYPE_CHECKING:
    from .api_run_requests import ApiRunRequestTransport
    from .session_ledger import GitHubIssueCommentSessionLedger

_ISO_STAMP_RE: Final[re.Pattern[str]] = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$"
)

SAME_RUN_AUTHORITY_MODES: Final[frozenset[str]] = frozenset(
    {
        "same_owner_active",
        "same_run_ceased_predecessor",
        "canonical_epoch_fenced",
    }
)
CROSS_RUN_AUTHORITY_MODES: Final[frozenset[str]] = frozenset(
    {
        "cross_run_ceased_predecessor",
        "canonical_epoch_fenced",
    }
)

_ISSUED_WITNESSES: weakref.WeakSet[UnstartedCancellationWitness] = weakref.WeakSet()


@dataclass(frozen=True, eq=False)
class UnstartedCancellationWitness:
    """Process-issued proof authorizing one narrow unstarted-action cancellation."""

    identity: SessionIdentity
    target_run_id: int
    target_root_sha256: str
    intent_sha256: str
    intent: Mapping[str, Any]
    reason: str
    authority_mode: str
    authority_proof_sha256: str
    live_source_observation: Mapping[str, Any]
    producer: str


def _issue_witness(
    witness: UnstartedCancellationWitness,
) -> UnstartedCancellationWitness:
    _ISSUED_WITNESSES.add(witness)
    return witness


def require_witness_issued(witness: object) -> UnstartedCancellationWitness:
    """Verify that ``witness`` is a live, process-issued cancellation proof."""

    if (
        type(witness) is not UnstartedCancellationWitness
        or witness not in _ISSUED_WITNESSES
    ):
        raise ReviewInputError(
            "unstarted action cancellation requires a process-issued witness"
        )
    return witness


def _sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _validate_unstarted_record(
    *,
    identity: SessionIdentity,
    record: SessionRecord,
    expected_intent: Mapping[str, Any] | None,
) -> tuple[ApiRunAccounting, dict[str, Any], str]:
    if not isinstance(record, SessionRecord) or record.identity() != identity:
        raise ReviewInputError("unstarted action record scope conflicts")
    if record.api_run_accounting is None:
        raise ReviewInputError("unstarted action record lacks caller accounting")
    if record.reservation_id is not None or record.reserved_slot is not None:
        raise ReviewInputError("unstarted action conflicts with a review reservation")
    if record.transaction is not None and record.transaction.phase not in {
        "publication_succeeded",
        "publication_suppressed",
    }:
        raise ReviewInputError("unstarted action conflicts with an unfinished review")
    state = ApiRunAccounting.from_document(record.api_run_accounting)
    state.require_writer_version()
    if state.analysis_abort is not None or state.terminal_transaction is not None:
        raise ReviewInputError("unstarted action accounting state conflicts")
    if state.uncertain > 0:
        raise ReviewInputError(
            "action has uncertain caller requests and cannot be canceled as unstarted"
        )
    action = state.action
    if action is None:
        raise ReviewInputError("caller action intent is unavailable")
    if action["phase"] != "pending":
        raise ReviewInputError("caller action is already terminal")
    if action["execution_started"]:
        raise ReviewInputError("started action cannot be canceled as unstarted")
    if action["result"] is not None or action["outcome"] is not None:
        raise ReviewInputError("pending action cannot carry a completion result")
    intent = cast(dict[str, Any], json_copy(action["intent"]))
    if expected_intent is not None and json_copy(expected_intent) != intent:
        raise ReviewInputError("caller action intent does not match stranded record")
    intent_sha256 = _sha256_json(intent)
    return state, intent, intent_sha256


def _observe_mutated_source(
    *,
    caller: ApiRunRequestTransport,
    ledger: GitHubIssueCommentSessionLedger,
    identity: SessionIdentity,
    source: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    suffix = (
        "/issues/comments/" if source["kind"] == "issue_comment" else "/pulls/comments/"
    ) + str(source["id"])
    path = ledger.http.repository_path(identity.repository, suffix)
    with caller.read_operation("unstarted-action-source-fence"):
        status, payload = ledger.http.request("GET", path, token=ledger.token)
    if status == 404:
        return (
            "source_deleted",
            {
                "status": 404,
                "comment_id": int(source["id"]),
                "observed_updated_at": None,
                "observed_body_sha256": None,
            },
        )
    if status != 200:
        raise ReviewInputError(
            f"live action source observation failed with HTTP {status}"
        )
    if (
        not isinstance(payload, dict)
        or type(payload.get("id")) is not int
        or payload["id"] != source["id"]
        or not isinstance(payload.get("updated_at"), str)
        or _ISO_STAMP_RE.fullmatch(payload["updated_at"]) is None
        or not isinstance(payload.get("body"), str)
    ):
        raise ReviewInputError("live action source payload is malformed")
    observed_updated_at = payload["updated_at"]
    observed_body_sha256 = hashlib.sha256(payload["body"].encode("utf-8")).hexdigest()
    # Every admitted source revision is bound by the full tuple
    # `(kind, id, updated_at, body_sha256)`. Under G27, editing a comment and
    # later restoring its original text (`edit_restored_body`, where `updated_at`
    # advanced while `body_sha256` matches) is intentionally a distinct comment
    # revision rather than a replay of the original revision, so any divergence
    # in `(updated_at, body_sha256)` authorizes narrow G32 unstarted supersession.
    if (
        observed_updated_at == source["updated_at"]
        and observed_body_sha256 == source["body_sha256"]
    ):
        raise ReviewInputError(
            "live action source is unchanged; same-source succession must execute normally"
        )
    return (
        "source_edited",
        {
            "status": 200,
            "comment_id": int(source["id"]),
            "observed_updated_at": observed_updated_at,
            "observed_body_sha256": observed_body_sha256,
        },
    )


def _authenticate_authority_proof(
    *,
    caller: ApiRunRequestTransport,
    identity: SessionIdentity,
    state: ApiRunAccounting,
    intent: Mapping[str, Any],
    mode: str,
    actions_evidence: ApiRunActionsEvidence | None,
    canonical_coordinator: ContainedForwardingCoordinator | None,
    canonical_token: IssuedWriterToken | None,
) -> tuple[str, str]:
    if mode not in {"same_run", "cross_run"}:
        raise ReviewInputError("unstarted cancellation mode is unsupported")
    has_actions = actions_evidence is not None
    has_canonical = canonical_coordinator is not None or canonical_token is not None
    if not has_actions and not has_canonical:
        raise ReviewInputError(
            "unstarted action cancellation requires owner cessation or canonical fence proof"
        )
    original_owner = ApiExecutionOwner(**intent["owner"])
    proof_payload: dict[str, object] = {
        "mode": mode,
        "target_run_id": state.identity.run_id,
        "caller_run_id": caller.run.run_id,
        "caller_attempt": caller.attempt,
        "caller_job": caller.job,
    }
    selected_mode: str | None = None

    if has_actions:
        if not isinstance(actions_evidence, ApiRunActionsEvidence):
            raise ReviewInputError("actions cessation evidence is invalid")
        if (
            actions_evidence.command_run_id != str(caller.run.run_id)
            or actions_evidence.job != caller.job
        ):
            raise ReviewInputError("actions evidence owner scope conflicts")
        with caller.read_operation("unstarted-action-owner-fence"):
            if mode == "same_run":
                actions_evidence.current_owner(
                    identity,
                    attempt=state.owner.run_attempt,
                    generation=state.owner.generation,
                )
                if state.owner.run_attempt > original_owner.run_attempt:
                    ceased = actions_evidence.ceased_owner(identity, original_owner)
                    selected_mode = "same_run_ceased_predecessor"
                    proof_payload["ceased_owner"] = list(ceased)
                else:
                    if state.owner != original_owner:
                        raise ReviewInputError(
                            "unstarted action owner conflicts with original intent"
                        )
                    selected_mode = "same_owner_active"
            else:
                actions_evidence.current_owner(
                    identity,
                    attempt=caller.attempt,
                    generation=0,
                )
                ceased_run = actions_evidence.ceased_run_owner(
                    identity, state.identity.run_id, state.owner
                )
                selected_mode = "cross_run_ceased_predecessor"
                proof_payload["ceased_run_owner"] = list(ceased_run)

    if has_canonical:
        if not isinstance(
            canonical_coordinator, ContainedForwardingCoordinator
        ) or not isinstance(canonical_token, IssuedWriterToken):
            raise ReviewInputError("canonical writer fence proof is incomplete")
        try:
            access = canonical_coordinator.authenticate(canonical_token)
            active_epoch = canonical_coordinator.active_epoch(
                base_store_id=canonical_token.base_store_id,
                repository_id=identity.repository_id or 0,
                pull_request=identity.pull_request,
            )
            snapshot = canonical_coordinator.candidate.snapshot(canonical_token)
            unsettled = extract_unsettled_dispatches(snapshot)
        except AuthorityRefusal as exc:
            raise ReviewInputError(
                f"canonical writer fence refused unstarted cancellation: {exc}"
            ) from exc
        if (
            access.repository != identity.repository
            or access.repository_id != identity.repository_id
            or access.pull_request != identity.pull_request
            or not access.native_exclusive
            or active_epoch != canonical_token.epoch
        ):
            raise ReviewInputError("canonical writer fence scope or epoch conflicts")
        if unsettled:
            raise ReviewInputError(
                "canonical writer epoch has unsettled dispatches; cannot cancel as unstarted"
            )
        if (mode == "cross_run" or state.owner != original_owner) and (
            canonical_token.epoch < 2
        ):
            raise ReviewInputError(
                "cross-owner unstarted cancellation requires a fenced successor epoch"
            )
        selected_mode = "canonical_epoch_fenced"
        proof_payload["canonical_epoch"] = canonical_token.epoch
        proof_payload["canonical_grant_id"] = canonical_token.grant_id

    if selected_mode is None:
        raise ReviewInputError(
            "unstarted action cancellation requires owner cessation or canonical fence proof"
        )
    return selected_mode, _sha256_json(proof_payload)


def authenticate_unstarted_cancellation(
    *,
    caller: ApiRunRequestTransport,
    ledger: GitHubIssueCommentSessionLedger,
    identity: SessionIdentity,
    record: SessionRecord,
    actions_evidence: ApiRunActionsEvidence | None = None,
    canonical_coordinator: ContainedForwardingCoordinator | None = None,
    canonical_token: IssuedWriterToken | None = None,
    expected_intent: Mapping[str, Any] | None = None,
    mode: str = "same_run",
) -> UnstartedCancellationWitness:
    """Authenticate live source mutation and writer exclusion for an unstarted action."""

    transport_state = caller.observe_action_transport_state(ledger, identity)
    if transport_state.has_unresolved_or_started_work:
        raise ReviewInputError(
            "caller transport has started or unresolved work; cannot cancel as unstarted"
        )
    state, intent, intent_sha256 = _validate_unstarted_record(
        identity=identity,
        record=record,
        expected_intent=expected_intent,
    )
    if mode == "same_run":
        if (
            not transport_state.is_activated
            or transport_state.root_sha256 != record.record_sha256
            or state.identity != caller.run
            or state.owner != transport_state.owner
        ):
            raise ReviewInputError(
                "same-run unstarted cancellation requires the active owning transport"
            )
        if transport_state.has_live_started_accounting:
            raise ReviewInputError("started action cannot be canceled as unstarted")
    elif mode == "cross_run":
        if (
            state.identity == caller.run
            or state.identity.repository_id != caller.run.repository_id
            or caller.attempt != 1
        ):
            raise ReviewInputError(
                "cross-run unstarted cancellation requires a new attempt-1 caller run"
            )
    else:
        raise ReviewInputError("unstarted cancellation mode is unsupported")

    authority_mode, authority_proof_sha256 = _authenticate_authority_proof(
        caller=caller,
        identity=identity,
        state=state,
        intent=intent,
        mode=mode,
        actions_evidence=actions_evidence,
        canonical_coordinator=canonical_coordinator,
        canonical_token=canonical_token,
    )
    reason, live_observation = _observe_mutated_source(
        caller=caller,
        ledger=ledger,
        identity=identity,
        source=intent["source"],
    )
    producer = ledger.evidence_producer()
    return _issue_witness(
        UnstartedCancellationWitness(
            identity=identity,
            target_run_id=state.identity.run_id,
            target_root_sha256=record.record_sha256,
            intent_sha256=intent_sha256,
            intent=json_copy(intent),
            reason=reason,
            authority_mode=authority_mode,
            authority_proof_sha256=authority_proof_sha256,
            live_source_observation=json_copy(live_observation),
            producer=producer,
        )
    )


def settle_unstarted_accounting(
    *,
    identity: SessionIdentity,
    state: ApiRunAccounting,
    producer: str,
    witness: UnstartedCancellationWitness,
) -> ApiRunAccounting:
    """Build the terminal ``canceled_unstarted`` accounting value with prior charges intact."""

    verified = require_witness_issued(witness)
    state.require_writer_version()
    action = state.action
    if (
        verified.identity != identity
        or verified.target_run_id != state.identity.run_id
        or verified.producer != producer
        or verified.reason not in UNSTARTED_CANCELLATION_REASONS
        or state.uncertain > 0
        or action is None
        or action["phase"] != "pending"
        or action["execution_started"]
        or action["intent"] != verified.intent
        or _sha256_json(action["intent"]) != verified.intent_sha256
    ):
        raise ReviewInputError("unstarted cancellation witness binding conflicts")

    outcome = {"kind": "canceled_unstarted", "reason": verified.reason}
    result_document = {
        "status": "canceled_unstarted",
        "reason": verified.reason,
        "authority_mode": verified.authority_mode,
        "authority_proof_sha256": verified.authority_proof_sha256,
        "target_run_id": verified.target_run_id,
        "superseded_from_root_sha256": verified.target_root_sha256,
        "live_source_observation": json_copy(verified.live_source_observation),
    }
    envelope = {
        "schema_version": "caller-action-result-v1",
        "intent_sha256": verified.intent_sha256,
        "outcome": outcome,
        "result": result_document,
    }
    carrier = {
        "schema_version": "caller-action-result-v1",
        "payload": encode_evidence(
            envelope, max_decoded_bytes=MAX_PARTITION_DECODED_BYTES
        ),
    }
    manifest = inline_manifest(
        carrier,
        binding=action_result_binding(identity, state, producer),
        item_count=1,
    )
    if manifest is None:
        raise ReviewInputError(
            "canceled unstarted action result exceeds its inline retention bound"
        )
    if (
        decode_action_result_carrier(
            read_inline_evidence(
                manifest,
                expected_binding=cast(Mapping[str, object], manifest["binding"]),
            )
        )
        != envelope
    ):
        raise ReviewInputError("canceled unstarted action result codec differs")
    settled = replace(
        state,
        action={
            **action,
            "phase": "terminal",
            "execution_started": False,
            "result": manifest,
            "outcome": outcome,
        },
    )
    state.require_successor(settled)
    return settled


def settle_unstarted_record(
    *,
    identity: SessionIdentity,
    record: SessionRecord,
    producer: str,
    witness: UnstartedCancellationWitness,
) -> SessionRecord:
    """Deterministically materialize the settled archival record for cross-run rollover."""

    verified = require_witness_issued(witness)
    if (
        verified.authority_mode not in CROSS_RUN_AUTHORITY_MODES
        or record.identity() != identity
        or record.record_sha256 != verified.target_root_sha256
        or record.reservation_id is not None
        or record.reserved_slot is not None
    ):
        raise ReviewInputError(
            "cross-run unstarted cancellation record binding conflicts"
        )
    state = ApiRunAccounting.from_document(record.api_run_accounting)
    settled_state = settle_unstarted_accounting(
        identity=identity,
        state=state,
        producer=producer,
        witness=verified,
    )
    preserved_now = _parse_aware_datetime(record.updated_at, label="updated_at")
    return record.evolve(
        generation=record.generation + 1,
        now=preserved_now,
        api_run_accounting=settled_state.to_document(),
    )
