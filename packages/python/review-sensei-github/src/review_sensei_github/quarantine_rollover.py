"""Serialized exact-root confirmation for an already authenticated Q successor.

This changes no terminal, cessation, retention or accounting rules. It avoids
repeating the full comment census inside one new run's ordinary startup
rollover. Discovery and later consumers still take fresh complete censuses.
"""

from __future__ import annotations

import weakref
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.session import (
    _history_legacy_write_guard,
    _validate_queue_retention,
    read_session_assessment_queue,
    read_session_baseline,
    read_session_dispositions,
)

from .errors import GitHubPublicationTransientError
from .http import MAX_PAGINATION_PAGES
from .session_ledger import parse_session_comment

if TYPE_CHECKING:
    from .api_run_requests import ApiRunRequestTransport

_ISSUED: weakref.WeakSet[Any] = weakref.WeakSet()


@dataclass(frozen=True, eq=False)
class _StartupSnapshot:
    caller: ApiRunRequestTransport
    ledger: Any
    identity: Any
    slot: Any
    record: Any
    comment_id: int
    owner_id: int
    body: str
    digest: str
    lineage: bytes

    def require(self):
        caller, ledger = self.caller, self.ledger
        if caller is None:
            raise ReviewInputError("quarantine startup caller is absent")
        caller._require_ledger(ledger, self.identity)
        if (
            self not in _ISSUED
            or getattr(ledger, "_startup_caller", None) is not caller
            or caller._slot is not self.slot
            or caller._state is not None
            or not caller._reservation_started
            or caller._active_operation is not None
            or caller._checkpoint is not None
            or caller._root is not None
            or caller.attempt != 1
            or ledger.evidence_producer() != f"github-bot:{self.owner_id}"
            or self.record.record_sha256 != self.digest
            or canonical_bytes(self.record.quarantine_lineage) != self.lineage
        ):
            raise ReviewInputError("quarantine startup snapshot is stale or foreign")
        caller.budget_view.check()

    def validate(self, payload, record, body):
        self.require()
        if (
            not isinstance(payload, dict)
            or type(payload.get("id")) is not int
            or payload["id"] != self.comment_id
            or payload.get("body") != body
            or not isinstance(payload.get("user"), dict)
            or type(payload["user"].get("id")) is not int
            or payload["user"]["id"] != self.owner_id
        ):
            raise ReviewInputError("quarantine startup exact root changed")
        self.ledger._part_owner(payload, self.identity)
        parsed = parse_session_comment(body, identity=self.identity)
        if parsed is None or parsed.to_dict() != record.to_dict():
            raise ReviewInputError("quarantine startup exact record conflicts")
        return parsed

    def get(self, record, body):
        self.require()
        status, payload = self.ledger._request(
            "GET",
            self.ledger.http.repository_path(
                self.identity.repository, f"/issues/comments/{self.comment_id}"
            ),
        )
        if status != 200:
            raise ReviewInputError("quarantine startup exact readback is unavailable")
        return self.validate(payload, record, body)


def retention_preflight(ledger, previous):
    """Exact tail bound only for this issued inline-state startup transaction.

    Archive POST + complete unknown-response census + part readback, then one
    exact old-root GET, current-PR GET, PATCH and at most two exact readbacks.
    The generic path still reserves its three complete-root scans. External
    baseline/decision/queue references keep that conservative generic bound.
    """
    snapshot = getattr(ledger, "_quarantine_rollover_snapshot", None)
    if snapshot is None:
        return None
    if type(snapshot) is not _StartupSnapshot or snapshot not in _ISSUED:
        raise ReviewInputError("quarantine rollover retention proof is foreign")
    snapshot.require()
    if previous.record_sha256 != snapshot.digest:
        raise ReviewInputError("quarantine rollover retention record changed")
    history = previous.convergence_history
    baseline = history.get("baseline") if history is not None else None
    if (
        previous.disposition_snapshot is not None
        or previous.assessment_queue is not None
        or isinstance(baseline, dict)
        and baseline.get("encoding") == "partitioned-json-v1"
    ):
        return None
    return 1 + MAX_PAGINATION_PAGES + 1 + 1 + 1 + 1 + 2


def replace_quarantine_rollover(
    caller: ApiRunRequestTransport, ledger, identity, previous, prepare
):
    """Only the existing new-run rollover issuer may enter this startup path."""
    from .quarantine_action import require_native_decision
    from .review_run import StartupRunLedger

    if (
        caller is None
        or not isinstance(ledger, StartupRunLedger)
        or getattr(ledger, "_startup_caller", None) is not caller
        or caller._state is not None
        or not caller._reservation_started
        or caller._root is not None
        or not ledger._quarantine_required
    ):
        raise ReviewInputError(
            "quarantine rollover requires its actual new-run startup"
        )
    ledger._verify_mutation_grant(identity)
    comment_id, record = ledger._discover(identity)
    authenticated = getattr(ledger, "_quarantine_authenticated", None)
    if (
        type(comment_id) is not int
        or record is None
        or record.expired()
        or record.quarantine_lineage is None
        or record.record_sha256 != previous.record_sha256
        or authenticated is None
        or authenticated.record_sha256 != record.record_sha256
    ):
        raise ReviewInputError("quarantine rollover complete census conflicts")
    item = ledger._comments_by_id[comment_id]
    ledger._part_owner(item, identity)
    snapshot = _StartupSnapshot(
        caller,
        ledger,
        identity,
        caller._slot,
        record,
        comment_id,
        item["user"]["id"],
        item["body"],
        record.record_sha256,
        canonical_bytes(record.quarantine_lineage),
    )
    reader = ledger._quarantine_origin_reader
    if reader is None:
        raise ReviewInputError("quarantine startup origin reader is absent")
    _ISSUED.add(snapshot)
    if getattr(ledger, "_quarantine_rollover_snapshot", None) is not None:
        raise ReviewInputError("quarantine rollover retention proof already exists")
    ledger._quarantine_rollover_snapshot = snapshot
    try:
        snapshot.get(record, snapshot.body)
        reader.current_scope.require()
        _history_legacy_write_guard(ledger, record)
        ledger._mutation_active = True
        try:
            # The original issuer still checks exact previous root, ceased
            # terminal owner, immutable archive readback and normal allocation.
            # Its retention preflight is derived from this issued exact-root
            # transaction; ordinary three-census rollover remains unchanged.
            updated = prepare(record)
            require_native_decision(ledger, record, updated)
            _validate_queue_retention(record, updated)
            read_session_baseline(ledger, updated)
            read_session_dispositions(ledger, updated)
            read_session_assessment_queue(ledger, updated)
            _history_legacy_write_guard(ledger, updated)
        finally:
            ledger._mutation_active = False
        snapshot.get(record, snapshot.body)
        reader.current_scope.require()
        body = ledger._render_comment(
            repository_id=identity.repository_id,
            pull_request=identity.pull_request,
            record=updated,
        )
        caller.budget_view.preflight(3)
        try:
            status, payload = ledger._request(
                "PATCH",
                ledger.http.repository_path(
                    identity.repository, f"/issues/comments/{comment_id}"
                ),
                body={"body": body},
            )
        except GitHubPublicationTransientError:
            status, payload = 0, None
        if status == 200:
            snapshot.validate(payload, updated, body)
        elif status not in {0, 202, 409, 422, 429} and status < 500:
            raise ReviewInputError("quarantine startup root update refused")
        # One write and at most two exact GET attempts consume the original
        # startup tail. Absence, a changed body or exhausted readback never
        # authorizes another PATCH or generic speculative reconciliation.
        try:
            return snapshot.get(updated, body)
        except GitHubPublicationTransientError:
            try:
                return snapshot.get(updated, body)
            except GitHubPublicationTransientError as error:
                raise ReviewInputError(
                    "quarantine startup root remains unconfirmed; no retry"
                ) from error
    finally:
        ledger._quarantine_rollover_snapshot = None
        _ISSUED.discard(snapshot)
