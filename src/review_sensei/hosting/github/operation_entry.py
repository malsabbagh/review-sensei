"""Admission proof and one-use grants for the GitHub operation entry.

The broker verifies a session grant's signature, scope, attestation, audience,
and expiry. This module turns that verified grant into an ``AdmissionProof``
and records the grant digest on the pull request. A second use refuses before
another provider allowance. The worker does not store the digest.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable

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


def operation_handle_witness(handle: object) -> str | None:
    """Return the reservation witness for an operation handle, never a snapshot.

    A mapping, a digest string, or any other snapshot returns ``None``.
    """

    if not isinstance(handle, OperationHandle):
        return None
    return handle.binding
