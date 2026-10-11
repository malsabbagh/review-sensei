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
import time
from collections.abc import Callable
from dataclasses import dataclass

from .operation_comments import ResultPartStore
from .operation_host import (
    REVIEW_TRIGGERS,
    AdmissionProof,
    ExecutionResult,
    InMemoryOperationStore,
    OperationHandle,
    OperationHost,
    OperationRefusal,
    OperationRequest,
    ProviderOutput,
    TransitionPlan,
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
) -> HostedProviderResult | OperationRefusal:
    """Admit one trigger, call the provider once, and keep the validated bytes.

    A later call with the same delivery id reads the accepted packet and does
    not call ``execute``. A missing proof returns a refusal before ``execute``.
    """

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
    request = OperationRequest(
        event_id=event,
        operation_id=_sha256(f"operation\n{reservation_id}"),
        inventory_digest=_sha256(f"inventory\n{repository_id}:{pull_request}"),
        source_digest=_sha256(f"source\n{delivery_id}"),
        root_sha=_sha256("operation-root"),
        root_generation=0,
        producer_contract=trigger,
    )
    plan = TransitionPlan(
        attempt_id=_sha256(f"attempt\n{run_id}\n{run_attempt}"),
        sequence=0,
        ordinary_dispatches=1,
        fence_dispatches=0,
        prior_root_sha=request.root_sha,
        prior_root_generation=0,
        target_root_sha=_sha256("operation-root-next"),
        target_root_generation=1,
        request_digest=event,
        dispatch_digest=_sha256(f"dispatch\n{trigger}"),
    )
    captured: dict[str, object] = {}

    def provider_call() -> ProviderOutput:
        value = execute()
        captured["value"] = value
        payload = encode(value)
        if not isinstance(payload, bytes) or not payload:
            raise ValueError("provider returned no result")
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
    )
    if isinstance(admitted, OperationRefusal):
        return admitted
    if admitted.status != "accepted" or admitted.packet is None:
        if "value" in captured:
            return HostedProviderResult(captured["value"], None, None)
        return OperationRefusal(admitted.reason or "provider_unknown")
    payload_digest = admitted.packet.payload_digest
    event_key = _event_key_text((proof.scope_digest, event))
    if "value" in captured:
        payload = encode(captured["value"])
        if parts is not None and isinstance(payload, bytes) and payload:
            parts.write(event_key=event_key, result=payload)
        return HostedProviderResult(captured["value"], None, payload_digest)
    if parts is None:
        return OperationRefusal("result_missing")
    return HostedProviderResult(
        None,
        parts.read(event_key=event_key, payload_digest=payload_digest),
        payload_digest,
    )


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
) -> HostedProviderResult | OperationRefusal:
    """Open the pull-request operation record and run one Actions provider call."""

    if not isinstance(app_id, int) or isinstance(app_id, bool) or app_id <= 0:
        return OperationRefusal("invalid")
    from .http import GitHubHttp
    from .operation_comments import GitHubIssueCommentOperationPort
    from .operation_host import GitHubCommentOperationStore

    if not isinstance(http, GitHubHttp):
        return OperationRefusal("unavailable")
    status, body = http.request("GET", "/user", token=token)
    if status != 200 or not isinstance(body, dict):
        return OperationRefusal("unavailable")
    user_id = body.get("id")
    if (
        body.get("type") != "Bot"
        or isinstance(user_id, bool)
        or not isinstance(user_id, int)
        or user_id <= 0
    ):
        return OperationRefusal("unavailable")
    port = GitHubIssueCommentOperationPort(
        http,
        token=token,
        repository=repository,
        pull_request=pull_request,
        app_user_id=user_id,
    )
    store = GitHubCommentOperationStore(port, app_user_id=user_id)
    started = time.time_ns() // 1_000_000
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
        delivery_id=f"{run_id}:{run_attempt}",
        grant=f"{trigger}:{run_id}:{run_attempt}",
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


def operation_handle_witness(handle: object) -> str | None:
    """Return the reservation witness for an operation handle, never a snapshot.

    A mapping, a digest string, or any other snapshot returns ``None``.
    """

    if not isinstance(handle, OperationHandle):
        return None
    return handle.binding
