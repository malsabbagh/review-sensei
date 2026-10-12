"""Original-attempt witness shared by the engine and the hosted GitHub runtime.

A mapping, a digest string, or any other snapshot is not a witness. Only an
``OperationHandle`` carries a reservation binding the engine will honor.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OperationHandle:
    """Identity-bound handle. Counters on a stale handle are not authority."""

    scope_digest: str
    event_id: str
    operation_id: str
    binding: str
    created_at_ms: int
    control_deadline_ms: int
    calls: int
    sequence: int
    root_sha: str
    root_generation: int


def operation_handle_witness(handle: object) -> str | None:
    """Return the reservation witness for an operation handle, never a snapshot.

    A mapping, a digest string, or any other snapshot returns ``None``.
    """

    if not isinstance(handle, OperationHandle):
        return None
    return handle.binding
