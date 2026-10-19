"""Admission proof and one-use grants for the GitHub operation entry.

The broker verifies a session grant's signature, scope, attestation, audience,
and expiry. This module turns that verified grant into an ``AdmissionProof``
and records the grant digest on the pull request. A second use refuses before
another provider allowance. The worker does not store the digest.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass

from review_sensei.errors import ReviewInputError
from review_sensei.operation_witness import operation_handle_witness  # noqa: F401

from .operation_comments import ResultPartStore
from .operation_host import (
    REVIEW_TRIGGERS,
    AcceptedPacket,
    AdmissionProof,
    ExecutionResult,
    InMemoryOperationStore,
    OperationHandle,
    OperationHost,
    OperationRefusal,
    OperationRequest,
    ProviderOutput,
    RestoredOperation,
    TransitionPlan,
    _binding,
    _event_key_text,
    run_review_trigger,
)

_HEX64 = re.compile(r"^[a-f0-9]{64}$")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _positive(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def github_event_id(
    *,
    delivery_id: str | None = None,
    comment_id: int | None = None,
    updated_at: str | None = None,
) -> str:
    """Stable event identity. A pull request uses its delivery id.

    A mention or command uses the source comment id plus ``updated_at``.
    """

    if isinstance(delivery_id, str) and delivery_id.strip():
        return _sha256(f"pull_request\n{delivery_id.strip()}")
    if _positive(comment_id) and isinstance(updated_at, str) and updated_at.strip():
        return _sha256(f"comment\n{comment_id}\n{updated_at.strip()}")
    raise ValueError("operation event id is missing")


def github_admission_proof(
    *,
    repository_id: object,
    pull_request: object,
    attestation_digest: object,
    run_id: object,
    run_attempt: object,
    app_id: object,
    reservation_id: object,
    server_authenticated: object,
) -> AdmissionProof | OperationRefusal:
    """Build the server admission proof from a verified broker grant.

    ``server_authenticated`` is true only after the broker has checked the
    grant signature, scope, attestation, audience, and expiry. A missing or
    forged field refuses and does not write an operation comment.
    """

    if server_authenticated is not True:
        return OperationRefusal("unauthenticated")
    if (
        not _positive(repository_id)
        or not _positive(pull_request)
        or not _positive(app_id)
    ):
        return OperationRefusal("invalid")
    if (
        not isinstance(attestation_digest, str)
        or _HEX64.fullmatch(attestation_digest) is None
    ):
        return OperationRefusal("invalid")
    if (
        not isinstance(run_id, str)
        or not run_id.strip()
        or not isinstance(run_attempt, str)
        or not run_attempt.strip()
        or not isinstance(reservation_id, str)
        or not reservation_id.strip()
    ):
        return OperationRefusal("invalid")
    execution = _sha256(f"{run_id.strip()}\n{run_attempt.strip()}")[:32]
    return AdmissionProof(
        scope_digest=_sha256(f"{repository_id}:{pull_request}"),
        authority_digest=attestation_digest,
        execution_identity=execution,
        owner_digest=_sha256(str(app_id)),
        reservation_digest=_sha256(reservation_id.strip()),
        server_authenticated=True,
    )


def consume_recorded_grant(store: InMemoryOperationStore, grant: str) -> bool:
    """Commit one grant digest, or refuse when that digest is already recorded."""

    with store.transaction() as txn:
        if store.note_consumed_grant(grant) is not True:
            txn.rollback()
            return False
        txn.commit()
        return True


def run_admitted_trigger(
    host: OperationHost,
    *,
    trigger: str,
    proof: AdmissionProof | OperationRefusal,
    request: OperationRequest,
    plan: TransitionPlan,
    provider_call: Callable[[], ProviderOutput],
    validator: Callable[[ProviderOutput, int], bool],
    grant: str,
    server_started_ms: int,
    bootstrap_dispatches: int = 1,
) -> ExecutionResult | OperationRefusal:
    """Run one trigger only after the admission proof and grant are both valid."""

    if not isinstance(proof, AdmissionProof):
        return proof
    if trigger not in REVIEW_TRIGGERS:
        return OperationRefusal("trigger")

    def consume_grant() -> bool:
        return host.store.note_consumed_grant(grant)

    return run_review_trigger(
        host,
        trigger=trigger,
        proof=proof,
        request=request,
        plan=plan,
        provider_call=provider_call,
        validator=validator,
        consume_grant=consume_grant,
        server_started_ms=server_started_ms,
        bootstrap_dispatches=bootstrap_dispatches,
    )


@dataclass(frozen=True, slots=True)
class HostedProviderResult:
    """One hosted provider attempt, or the bytes already accepted for it."""

    fresh: object | None
    replay_payload: bytes | None
    payload_digest: str | None
    operation: RestoredOperation | None = None


@dataclass(frozen=True, slots=True)
class HostedComputationIdentity:
    """Complete caller-owned computation identity, separate from store cursor.

    These digests bind retained work; they do not authenticate a source or grant.
    The caller must obtain its admission proof from actual verified authority.
    """

    operation_id: str
    inventory_digest: str
    source_digest: str

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or _HEX64.fullmatch(value) is None
            for value in (self.operation_id, self.inventory_digest, self.source_digest)
        ):
            raise ReviewInputError("hosted computation identity is invalid")


@dataclass(frozen=True, slots=True)
class HostedOperationExecution:
    """Actual restored handle supplied inside the charged computation callback.

    The handle retains its original admission binding/deadline. Declared host
    dispatch liability is not a measurement of all physical GitHub/broker calls.
    """

    handle: OperationHandle
    request: OperationRequest


def _computation_cursor(
    store: InMemoryOperationStore, scope: str, event: str
) -> tuple[str, int, str] | OperationRefusal:
    """Use the current cursor for a new event and original cursor for replay."""

    live = store._live()
    record = live["events"].get((scope, event))
    if isinstance(record, dict):
        transitions = [
            item
            for item in live["transitions"].values()
            if isinstance(item, dict)
            and item.get("scope_digest") == scope
            and item.get("event_id") == event
            and item.get("sequence") == 0
        ]
        if len(transitions) == 1:
            prior = transitions[0]
            return (
                str(prior["prior_root_sha"]),
                int(prior["prior_root_generation"]),
                str(prior["target_root_sha"]),
            )
        if transitions or record.get("sequence") != 0:
            return OperationRefusal("root_conflict")
        root, generation = record["root_sha"], record["root_generation"]
    else:
        # Do not bind a new event to a cursor whose computation is still held.
        # A refused contender must not become a stale admitted guard that can
        # later spend against the old cursor after its owner completes.
        if any(
            isinstance(item, dict)
            and item.get("scope_digest") == scope
            and item.get("state") in {"inflight", "unknown", "pending"}
            for item in live["transitions"].values()
        ):
            return OperationRefusal("unknown_reservation")
        current = live["scopes"].get(scope)
        if isinstance(current, dict):
            root, generation = current["root_sha"], current["root_generation"]
        else:
            root, generation = _sha256("operation-root"), 0
    if (
        not isinstance(root, str)
        or not isinstance(generation, int)
        or isinstance(generation, bool)
        or not 0 <= generation < 2_147_483_647
    ):
        return OperationRefusal("root_conflict")
    target = _sha256(f"computation\n{scope}\n{event}\n{root}\n{generation + 1}")
    return root, generation, target


def _complete_retained_computation(
    store: InMemoryOperationStore,
    proof: AdmissionProof,
    request: OperationRequest,
    packet: AcceptedPacket,
    payload: bytes,
) -> OperationRefusal | None:
    """Release only this adapter's computation writer, never publication.

    The accepted packet and full owned result are the computation's durable
    target. There is no remote product root or source acknowledgement here.
    Generic OperationHost remote-effect reconciliation is deliberately unchanged.

    This adapter's sole producer, run_hosted_provider.provider_call, assigns
    SHA-256 of the complete encoded result to ProviderOutput.decisions_digest.
    OperationHost independently hashes those same bytes for payload_digest.
    A generic OperationHost producer may use a distinct decision digest, but
    its packet is not this adapter's computation-completion contract.
    This adapter also sets TransitionPlan.request_digest to github_event_id;
    both the accepted packet and transition must retain that exact event ID.
    """

    digest = hashlib.sha256(payload).hexdigest()
    if (
        packet.payload_digest != digest
        or packet.decisions_digest != digest
        or packet.known_output_bytes != len(payload)
        or packet.request_digest != request.event_id
    ):
        return OperationRefusal("result_missing")
    with store.transaction() as txn:
        live = store._live()
        event = live["events"].get((proof.scope_digest, request.event_id))
        if (
            not isinstance(event, dict)
            or event.get("binding") != _binding(proof, request)
            or event.get("operation_id") != request.operation_id
            or event.get("accepted") != packet
            or event.get("provider_charged") is not True
            or event.get("provider_unknown") is not False
            or event.get("inference_blocked") is not False
            or event.get("quarantine") is not False
            or event.get("ack_attempted") is not False
        ):
            txn.rollback()
            return OperationRefusal("disagreement")
        transitions = [
            item
            for item in live["transitions"].values()
            if isinstance(item, dict)
            and item.get("scope_digest") == proof.scope_digest
            and item.get("event_id") == request.event_id
        ]
        if len(transitions) != 1:
            txn.rollback()
            return OperationRefusal("unknown_reservation")
        transition = transitions[0]
        if (
            transition.get("state") not in {"inflight", "confirmed"}
            or transition.get("request_digest") != request.event_id
            or transition.get("prior_root_sha") != request.root_sha
            or transition.get("prior_root_generation") != request.root_generation
            or transition.get("target_root_generation") != request.root_generation + 1
        ):
            txn.rollback()
            return OperationRefusal("unknown_reservation")
        target = {
            "root_sha": transition["target_root_sha"],
            "root_generation": transition["target_root_generation"],
        }
        if transition["state"] == "confirmed":
            # A later event may already have advanced the scope again. Replaying
            # this event must not roll that cursor or its accounting backward.
            matches = all(event.get(key) == value for key, value in target.items())
            txn.rollback()
            return None if matches else OperationRefusal("root_conflict")
        prior = {
            "root_sha": request.root_sha,
            "root_generation": request.root_generation,
        }
        if live["scopes"].get(proof.scope_digest) != prior or any(
            event.get(key) != value for key, value in prior.items()
        ):
            txn.rollback()
            return OperationRefusal("root_conflict")
        if any(
            isinstance(item, dict)
            and item is not transition
            and item.get("scope_digest") == proof.scope_digest
            and item.get("state") in {"inflight", "unknown", "pending"}
            for item in live["transitions"].values()
        ):
            txn.rollback()
            return OperationRefusal("unknown_reservation")
        transition["state"] = "confirmed"
        event.update(target)
        live["scopes"][proof.scope_digest] = target
        txn.commit()
    return None


def run_hosted_provider(
    store: InMemoryOperationStore,
    *,
    trigger: str,
    repository_id: int,
    pull_request: int,
    attestation_digest: str,
    run_id: str,
    run_attempt: str,
    app_id: int,
    reservation_id: str,
    delivery_id: str,
    grant: str,
    server_started_ms: int,
    execute: Callable[[], object],
    encode: Callable[[object], bytes],
    parts: ResultPartStore | None = None,
    now: Callable[[], int] | None = None,
    bootstrap_dispatches: int = 1,
    identity: HostedComputationIdentity | None = None,
    execute_with_operation: Callable[[HostedOperationExecution], object] | None = None,
) -> HostedProviderResult | OperationRefusal:
    """Admit one trigger, call the provider once, and keep the validated bytes.

    A later call with the same delivery id reads the accepted packet and does
    not call ``execute``. A missing proof returns a refusal before ``execute``.
    Fresh work requires owned ResultPartStore capacity; without it, only an
    already accepted embedded result can replay. A result_missing refusal before
    admission can be retried with that storage configured and the same identity.
    The optional handle-aware callback receives only a real restored admission;
    it cannot receive an unaccepted fresh result on a failed computation.
    """

    if (
        identity is not None and not isinstance(identity, HostedComputationIdentity)
    ) or (
        execute_with_operation is not None
        and (identity is None or not callable(execute_with_operation))
    ):
        return OperationRefusal("invalid")

    proof = github_admission_proof(
        repository_id=repository_id,
        pull_request=pull_request,
        attestation_digest=attestation_digest,
        run_id=run_id,
        run_attempt=run_attempt,
        app_id=app_id,
        reservation_id=reservation_id,
        server_authenticated=True,
    )
    if not isinstance(proof, AdmissionProof):
        return proof
    try:
        event = github_event_id(delivery_id=delivery_id)
    except ValueError:
        return OperationRefusal("invalid")
    if parts is None:
        # A fresh encoded result's size is unknown before inference. Even a
        # store that supports inline embedding cannot guarantee it will fit.
        # Refuse before original admission/debit rather than leaving a held
        # computation whose only complete output was lost with this process.
        record = store._live()["events"].get((proof.scope_digest, event))
        packet = record.get("accepted") if isinstance(record, dict) else None
        embedded = getattr(store, "embedded_result", None)
        retained = embedded((proof.scope_digest, event)) if callable(embedded) else None
        if not (
            isinstance(packet, AcceptedPacket)
            and isinstance(retained, bytes)
            and len(retained) == packet.known_output_bytes
            and hashlib.sha256(retained).hexdigest() == packet.payload_digest
        ):
            return OperationRefusal("result_missing")
    cursor = _computation_cursor(store, proof.scope_digest, event)
    if isinstance(cursor, OperationRefusal):
        return cursor
    root, generation, target = cursor
    request = OperationRequest(
        event_id=event,
        operation_id=identity.operation_id
        if identity is not None
        else _sha256(f"operation\n{reservation_id}"),
        inventory_digest=identity.inventory_digest
        if identity is not None
        else _sha256(f"inventory\n{repository_id}:{pull_request}"),
        source_digest=identity.source_digest
        if identity is not None
        else _sha256(f"source\n{delivery_id}"),
        root_sha=root,
        root_generation=generation,
        producer_contract=trigger,
    )
    plan = TransitionPlan(
        attempt_id=_sha256(f"attempt\n{run_id}\n{run_attempt}"),
        sequence=0,
        ordinary_dispatches=1,
        fence_dispatches=0,
        prior_root_sha=request.root_sha,
        prior_root_generation=generation,
        target_root_sha=target,
        target_root_generation=generation + 1,
        request_digest=event,
        dispatch_digest=_sha256(f"dispatch\n{trigger}"),
    )
    captured: dict[str, object] = {}

    def provider_call() -> ProviderOutput:
        if execute_with_operation is None:
            value = execute()
        else:
            restored = host.restore_operation(proof, request)
            if not isinstance(restored, RestoredOperation):
                raise ReviewInputError(
                    "original hosted operation handle is unavailable"
                )
            value = execute_with_operation(
                HostedOperationExecution(restored.handle, request)
            )
        captured["value"] = value
        payload = encode(value)
        if not isinstance(payload, bytes) or not payload:
            raise ValueError("provider returned no result")
        captured["payload"] = payload
        return ProviderOutput(payload, hashlib.sha256(payload).hexdigest())

    host = OperationHost(store, now=now)
    admitted = run_admitted_trigger(
        host,
        trigger=trigger,
        proof=proof,
        request=request,
        plan=plan,
        provider_call=provider_call,
        validator=lambda output, measured: measured == len(output.output),
        grant=grant,
        server_started_ms=server_started_ms,
        bootstrap_dispatches=bootstrap_dispatches,
    )
    if isinstance(admitted, OperationRefusal):
        return admitted
    if admitted.status != "accepted" or admitted.packet is None:
        if "value" in captured and execute_with_operation is None:
            return HostedProviderResult(captured["value"], None, None)
        return OperationRefusal(admitted.reason or "provider_unknown")
    payload_digest = admitted.packet.payload_digest
    event_key = _event_key_text((proof.scope_digest, event))
    embedded = getattr(store, "embedded_result", None)
    stored = embedded((proof.scope_digest, event)) if callable(embedded) else None
    if (
        isinstance(stored, bytes)
        and hashlib.sha256(stored).hexdigest() == payload_digest
    ):
        retained = stored
    elif "value" in captured:
        payload = captured.get("payload")
        if not isinstance(payload, bytes) or not payload:
            return OperationRefusal("result_missing")
        if parts is None:
            # The preflight admits only existing owned embedded replay without
            # parts. A fresh or unexpectedly missing target cannot complete.
            return OperationRefusal("result_missing")
        parts.write(event_key=event_key, result=payload)
        retained = parts.read(event_key=event_key, payload_digest=payload_digest)
    elif parts is not None:
        retained = parts.read(event_key=event_key, payload_digest=payload_digest)
    else:
        return OperationRefusal("result_missing")
    refused = _complete_retained_computation(
        store, proof, request, admitted.packet, retained
    )
    if refused is not None:
        return refused
    restored = host.restore_operation(proof, request)
    if not isinstance(restored, RestoredOperation):
        return OperationRefusal("result_missing")
    return HostedProviderResult(
        captured.get("value"),
        None if "value" in captured else retained,
        payload_digest,
        restored,
    )


def _issue_comments(
    http: object, token: str, repository: str, pull_request: int
) -> list[object] | None:
    from .http import GitHubHttp

    if not isinstance(http, GitHubHttp):
        return None
    path = http.repository_path(repository, f"/issues/{pull_request}/comments")
    collected: list[object] = []
    for page in range(1, 11):
        status, payload = http.request(
            "GET", f"{path}?per_page=100&page={page}", token=token
        )
        if status != 200 or not isinstance(payload, list) or len(payload) > 100:
            return None
        collected.extend(payload)
        if len(payload) < 100:
            http.primed_issue_comments[path] = collected
            return collected
    return None


def _bot_user_id(comments: list[object]) -> int | None:
    """The App user id is the author of a session or operation comment."""

    from .operation_host import OPERATION_COMMENT_MARKER, RESULT_COMMENT_MARKER

    markers = (
        "<!-- reviewsensei:session:",
        OPERATION_COMMENT_MARKER,
        RESULT_COMMENT_MARKER,
    )
    for item in comments:
        if not isinstance(item, dict):
            continue
        user = item.get("user")
        body = item.get("body")
        if not isinstance(user, dict) or not isinstance(body, str):
            continue
        user_id = user.get("id")
        if (
            user.get("type") != "Bot"
            or isinstance(user_id, bool)
            or not isinstance(user_id, int)
            or user_id <= 0
        ):
            continue
        if any(marker in body for marker in markers):
            return user_id
    return None


def run_actions_analysis(
    *,
    http: object,
    token: str,
    repository: str,
    repository_id: int,
    pull_request: int,
    run_id: str,
    run_attempt: str,
    app_id: int | None,
    reservation_id: str,
    attestation_digest: str,
    execute: Callable[[], object],
    encode: Callable[[object], bytes],
    trigger: str = "full-review",
    delivery_id: str | None = None,
    grant: str | None = None,
) -> HostedProviderResult | OperationRefusal:
    """Open the pull-request operation record and run one Actions provider call."""

    if not isinstance(app_id, int) or isinstance(app_id, bool) or app_id <= 0:
        return OperationRefusal("invalid")
    from .http import GitHubHttp
    from .operation_comments import GitHubIssueCommentOperationPort
    from .operation_host import GitHubCommentOperationStore

    if not isinstance(http, GitHubHttp):
        return OperationRefusal("unavailable")
    listed = _issue_comments(http, token, repository, pull_request)
    if listed is None:
        return OperationRefusal("unavailable")
    user_id = _bot_user_id(listed)
    if user_id is None:
        status, body = http.request("GET", "/user", token=token)
        if status != 200 or not isinstance(body, dict):
            return OperationRefusal("unavailable")
        found = body.get("id")
        if (
            body.get("type") != "Bot"
            or isinstance(found, bool)
            or not isinstance(found, int)
            or found <= 0
        ):
            return OperationRefusal("unavailable")
        user_id = found
    port = GitHubIssueCommentOperationPort(
        http,
        token=token,
        repository=repository,
        pull_request=pull_request,
        app_user_id=user_id,
    )
    port.prime(listed)
    store = GitHubCommentOperationStore(port, app_user_id=user_id)
    started = 1_000
    return run_hosted_provider(
        store,
        trigger=trigger,
        repository_id=repository_id,
        pull_request=pull_request,
        attestation_digest=attestation_digest,
        run_id=run_id,
        run_attempt=run_attempt,
        app_id=app_id,
        reservation_id=reservation_id,
        delivery_id=delivery_id or f"{run_id}:{run_attempt}",
        grant=grant or f"{trigger}:{run_id}:{run_attempt}",
        server_started_ms=started,
        execute=execute,
        encode=encode,
        parts=ResultPartStore(port, app_user_id=user_id),
        now=lambda: started,
    )


_V2_FINGERPRINT = re.compile(
    r"<!-- reviewsensei:finding:v2 repo=(?P<repository_id>[1-9][0-9]*) "
    r"pr=(?P<pull_request>[1-9][0-9]*) head=(?P<head_sha>[a-f0-9]{40}) "
    r"base=(?P<base_sha>[a-f0-9]{40}) fingerprint=(?P<fingerprint>[a-f0-9]{64}) "
    r"state=(?P<state>new|still-present|fixed|outdated|uncertain) "
    r"blocking=(?P<blocking>true|false) -->"
)


def finding_inventory_for_head(
    body: str, *, repository_id: int, pull_request: int, head_sha: str
):
    """Finding index for this head, or None when the review predates the index."""

    from .publication import parse_finding_index

    return parse_finding_index(
        body,
        repository_id=repository_id,
        pull_request=pull_request,
        head_sha=head_sha,
    )


def fingerprints_for_head(
    body: str, *, repository_id: int, pull_request: int, head_sha: str
) -> tuple[str, ...]:
    """Fingerprints from v2 finding markers that belong to this head."""

    found: list[str] = []
    for match in _V2_FINGERPRINT.finditer(body):
        if (
            int(match.group("repository_id")) != repository_id
            or int(match.group("pull_request")) != pull_request
            or match.group("head_sha") != head_sha
        ):
            continue
        fingerprint = match.group("fingerprint")
        if fingerprint not in found:
            found.append(fingerprint)
    return tuple(found)


def accepted_payload_matches(
    bodies: tuple[str, ...], payload_digest: str
) -> bool | None:
    """Whether an operation record already accepted these result bytes.

    ``None`` means no operation index is present, so the legacy publish path
    still applies. A present index must name this digest.
    """

    from .operation_host import OPERATION_COMMENT_MARKER

    if not any(OPERATION_COMMENT_MARKER in body for body in bodies):
        return None
    return any(payload_digest in body for body in bodies)


def analysis_payload(run: object) -> bytes:
    """Canonical validated result bytes. A prompt or transcript is not included."""

    result = getattr(run, "result", None)
    to_dict = getattr(result, "to_dict", None)
    if not callable(to_dict):
        return b""
    body = to_dict()
    if not isinstance(body, dict):
        return b""
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def call_operation_trigger(
    store: InMemoryOperationStore,
    *,
    trigger: str,
    repository_id: int,
    pull_request: int,
    attestation_digest: str,
    run_id: str,
    run_attempt: str,
    app_id: int,
    reservation_id: str,
    delivery_id: str,
    grant: str,
    execute: Callable[[], object],
    encode: Callable[[object], bytes],
    decode: Callable[[bytes], object],
    parts: ResultPartStore | None = None,
    now_ms: int | None = None,
    identity: HostedComputationIdentity | None = None,
    execute_with_operation: Callable[[HostedOperationExecution], object] | None = None,
    clock: Callable[[], int] | None = None,
    server_started_ms: int | None = None,
    bootstrap_dispatches: int | None = None,
) -> object:
    """Run one trigger, or decode the bytes already accepted for it."""

    # Preserve the existing structural wrapper by default. A handle-aware
    # caller must supply its original clock origin and measured bootstrap;
    # neither a new origin nor the fixture clock can qualify that lifecycle.
    if execute_with_operation is not None and (
        not callable(clock)
        or server_started_ms is None
        or identity is None
        or bootstrap_dispatches is None
    ):
        raise ReviewInputError("operation entry requires original clock and identity")
    if (clock is None) != (server_started_ms is None) or (
        clock is not None and now_ms is not None
    ):
        raise ReviewInputError("operation entry clock configuration is invalid")
    started = (
        server_started_ms
        if server_started_ms is not None
        else 1_000
        if now_ms is None
        else now_ms
    )
    admitted = run_hosted_provider(
        store,
        trigger=trigger,
        repository_id=repository_id,
        pull_request=pull_request,
        attestation_digest=attestation_digest,
        run_id=run_id,
        run_attempt=run_attempt,
        app_id=app_id,
        reservation_id=reservation_id,
        delivery_id=delivery_id,
        grant=grant,
        server_started_ms=started,
        execute=execute,
        encode=encode,
        parts=parts,
        now=clock if clock is not None else lambda: started,
        bootstrap_dispatches=1
        if bootstrap_dispatches is None
        else bootstrap_dispatches,
        identity=identity,
        execute_with_operation=execute_with_operation,
    )
    if isinstance(admitted, OperationRefusal):
        raise ReviewInputError(f"operation entry refused: {admitted.reason}")
    if not isinstance(admitted, HostedProviderResult):
        raise ReviewInputError("operation entry refused: invalid")
    if admitted.fresh is not None:
        return admitted.fresh
    if admitted.replay_payload is None:
        raise ReviewInputError("operation entry refused: result_missing")
    return decode(admitted.replay_payload)
