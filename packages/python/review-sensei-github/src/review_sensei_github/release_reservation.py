"""Owner-only release of this run's session reservation.

Used by the always-run workflow cleanup step after a review step that was
cancelled or killed: the Python process may never have run its own abort,
so the reservation it held would otherwise block the next review until
session expiry. Releasing publishes nothing, so the owning run may release
its own hold even after the PR head has moved. Ownership is proven twice:
the broker derives the owner run from verified OIDC, and the durable
ledger record must name that same run as its reservation owner.
"""

from __future__ import annotations

import re

from review_sensei.errors import ReviewInputError
from review_sensei.session import SessionIdentity

from .broker_client import BrokerClient
from .http import GitHubHttp
from .session_ledger import GitHubIssueCommentSessionLedger

_HEAD_SHA = re.compile(r"[a-f0-9]{40}")
_RUN_ID = re.compile(r"[1-9][0-9]{0,18}")
_REPOSITORY = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")


def release_own_reservation(
    *,
    broker: BrokerClient,
    http: GitHubHttp,
    repository: str,
    repository_id: int,
    pull_request: int,
    head_sha: str,
    run_id: str,
    oidc_token: str | None = None,
) -> str:
    """Release this run's reservation if it still owns one.

    Returns a bounded status: ``released``, ``no-reservation``,
    ``not-owner``, or ``publication-saved``. Only ``released`` writes.
    """

    if (
        not isinstance(repository, str)
        or _REPOSITORY.fullmatch(repository) is None
        or isinstance(repository_id, bool)
        or not isinstance(repository_id, int)
        or repository_id <= 0
        or isinstance(pull_request, bool)
        or not isinstance(pull_request, int)
        or pull_request <= 0
        or not isinstance(head_sha, str)
        or _HEAD_SHA.fullmatch(head_sha) is None
        or not isinstance(run_id, str)
        or _RUN_ID.fullmatch(run_id) is None
    ):
        raise ReviewInputError("reservation release scope is invalid")
    session = broker.open_session_release(
        oidc_token if oidc_token is not None else broker.request_oidc_token(),
        repository_id=repository_id,
        pull_request=pull_request,
        head_sha=head_sha,
    )
    owner = session.reservation_owner
    if owner is None or str(owner.get("run_id")) != run_id:
        raise ReviewInputError("broker reservation owner does not match this run")
    ledger = GitHubIssueCommentSessionLedger(
        http,
        token=session.token,
        reservation_owner=owner,
    )
    identity = SessionIdentity(
        repository=repository,
        pull_request=pull_request,
        repository_id=repository_id,
    )
    loaded = ledger.load(identity)
    if loaded.status == "missing":
        return "no-reservation"
    if loaded.status != "ok" or loaded.record is None:
        # A load failure (expired, integrity, conflict) is not an absence:
        # surface it so the cleanup step warns instead of reporting a
        # successful no-op while the reservation stays held.
        raise ReviewInputError(
            f"reservation release could not load the session: {loaded.status}"
        )
    record = loaded.record
    if record.reservation_id is None:
        return "no-reservation"
    if (
        record.reservation_owner is None
        or str(record.reservation_owner.get("run_id")) != run_id
    ):
        return "not-owner"
    if record.transaction is not None and record.transaction.phase != "analysis":
        # A transaction that reached publication is saved publication
        # work. It is never reclaimed here; publication/recovery paths
        # own it. An analysis-phase checkpoint is not publication work:
        # abort discards it with the reservation, as ADR 0063 recovery
        # does.
        return "publication-saved"
    ledger.abort(
        identity,
        reservation_id=record.reservation_id,
        expected_generation=record.generation,
    )
    return "released"
