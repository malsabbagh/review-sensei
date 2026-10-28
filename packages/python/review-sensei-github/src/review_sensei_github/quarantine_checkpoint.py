"""Operation-bound quarantine checkpoint confirmation under the existing cap.

One complete census selects/authenticates the ledger. Exact-root GETs confirm
that target inside the serialized checkpoint; they never discover a lineage.
Approval and later operations still require fresh complete discovery. This is
not CAS or continuous protection against privileged external tampering.
"""

from __future__ import annotations

import hashlib
import weakref
from dataclasses import dataclass
from typing import Any

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import EvidenceBudgetExhausted, ReviewInputError
from review_sensei.session import (
    _history_legacy_write_guard,
    _validate_queue_retention,
    read_session_assessment_queue,
    read_session_baseline,
    read_session_dispositions,
)

from .errors import GitHubPublicationError, GitHubPublicationTransientError
from .session_ledger import parse_session_comment

_ISSUED: weakref.WeakSet[Any] = weakref.WeakSet()


@dataclass(frozen=True, eq=False)
class _CheckpointSnapshot:
    caller: Any
    ledger: Any
    identity: Any
    operation: Any
    block: Any
    root: str
    comment_id: int
    owner_id: int
    record: Any
    body: str
    lineage: bytes
    listed_ids: frozenset
    staged: tuple = ()
    candidate: Any = None
    candidate_body: str | None = None

    def require(self):
        caller, ledger = self.caller, self.ledger
        caller._require_ledger(ledger, self.identity)
        if (
            self not in _ISSUED
            or caller._active_operation is not self.operation
            or caller._checkpoint_operation is not self.operation
            or caller._block != self.block
            or caller._root != self.root
            or ledger.caller is not caller
            or getattr(ledger, "_quarantine_checkpoint_snapshot", None) is not self
            or getattr(caller, "_quarantine_checkpoint_snapshot", None) is not self
            or ledger.evidence_producer() != f"github-bot:{self.owner_id}"
            or self.record.record_sha256 != self.root
            or canonical_bytes(self.record.quarantine_lineage) != self.lineage
        ):
            raise ReviewInputError("quarantine checkpoint snapshot is stale or foreign")
        caller.budget_view.check()

    def require_part(self, identifier):
        self.require()
        if int(identifier) not in self.listed_ids and identifier not in {
            key for key, _ in self.staged
        }:
            raise ReviewInputError(
                "quarantine checkpoint carrier has no census or exact staging echo"
            )

    def note_staged(self, payload):
        self.require()
        child = self.caller._staged_part_operation
        if child is None or not child.confirmed or not child.write_attempted:
            raise ReviewInputError("quarantine checkpoint staging is not confirmed")
        self.ledger._part_owner(payload, self.identity)
        identifier = payload["id"]
        if (
            type(identifier) is not int
            or identifier < 1
            or identifier in self.listed_ids
            or str(identifier) in {key for key, _ in self.staged}
        ):
            raise ReviewInputError("quarantine checkpoint staging identity is invalid")
        digest = hashlib.sha256(payload["body"].encode()).hexdigest()
        object.__setattr__(self, "staged", (*self.staged, (str(identifier), digest)))
        # The exact owned echo is the only additional carrier admitted after
        # the complete census. Its body can now be read without another GET.
        self.ledger._merge_comments([payload])
        self.ledger._part_from_item(payload, self.identity, str(identifier))

    def preflight_staging(self, document, binding, item_count, max_manifest_bytes):
        from review_sensei.bounded_evidence import inline_manifest, partition_evidence

        from .quarantine_creation import _body

        self.require()
        inline = inline_manifest(document, binding=binding, item_count=item_count)
        if inline is not None and len(canonical_bytes(inline)) <= max_manifest_bytes:
            return
        present = {
            item.get("body")
            for item in self.ledger._owned_carrier_items(
                self.ledger._cached_comments(), self.identity
            )
        }
        missing = sum(
            _body(part) not in present
            for part in partition_evidence(
                document, binding=binding, item_count=item_count
            )
        )
        # Each new carrier needs its normal live-head GET and one exact-echo
        # POST. Keep the final root fence, live-head read and protected tail.
        if self.operation.remaining < 2 * missing + 5:
            raise EvidenceBudgetExhausted(
                "quarantine complete staging and root confirmation are unfunded"
            )

    def validate(self, payload, *, record, body):
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
            raise ReviewInputError("quarantine exact checkpoint root changed")
        self.ledger._part_owner(payload, self.identity)
        parsed = parse_session_comment(payload["body"], identity=self.identity)
        if (
            parsed is None
            or parsed.record_sha256 != record.record_sha256
            or parsed.generation != record.generation
            or canonical_bytes(parsed.quarantine_lineage)
            != canonical_bytes(record.quarantine_lineage)
            or parsed.to_dict() != record.to_dict()
        ):
            raise ReviewInputError("quarantine exact checkpoint record conflicts")
        return parsed

    def get(self, *, candidate=False):
        self.require()
        record = self.candidate if candidate else self.record
        body = self.candidate_body if candidate else self.body
        if record is None or body is None:
            raise ReviewInputError("quarantine checkpoint candidate is absent")
        status, payload = self.ledger._request(
            "GET",
            self.ledger.http.repository_path(
                self.identity.repository, f"/issues/comments/{self.comment_id}"
            ),
        )
        if status != 200:
            raise ReviewInputError(
                "quarantine exact checkpoint readback is unavailable"
            )
        return self.validate(payload, record=record, body=body)


def expire(snapshot: _CheckpointSnapshot | None):
    if snapshot is None:
        return
    caller, ledger = snapshot.caller, snapshot.ledger
    _ISSUED.discard(snapshot)
    if getattr(caller, "_quarantine_checkpoint_snapshot", None) is snapshot:
        caller._quarantine_checkpoint_snapshot = None
    if getattr(ledger, "_quarantine_checkpoint_snapshot", None) is snapshot:
        ledger._quarantine_checkpoint_snapshot = None


def replace_quarantine_checkpoint(caller, ledger, identity, mutate, *, now=None):
    from .api_run_requests import ApiRunSessionLedger
    from .quarantine_action import require_native_decision

    if not isinstance(ledger, ApiRunSessionLedger) or ledger.caller is not caller:
        raise ReviewInputError(
            "quarantine checkpoint requires the current native caller"
        )
    if getattr(caller, "_quarantine_checkpoint_snapshot", None) is not None:
        raise ReviewInputError("quarantine checkpoint snapshot cannot be replenished")
    caller._require_ledger(ledger, identity)
    ledger._verify_mutation_grant(identity)
    comment_id, record = ledger._discover(identity, now=now)
    authenticated = getattr(ledger, "_quarantine_authenticated", None)
    operation = caller._active_operation
    if (
        type(comment_id) is not int
        or record is None
        or record.expired(now=now)
        or record.quarantine_lineage is None
        or record.record_sha256 != caller._root
        or authenticated is None
        or authenticated.record_sha256 != record.record_sha256
        or operation is None
        or operation is not caller._checkpoint_operation
        or operation.write_attempted
    ):
        raise ReviewInputError("quarantine checkpoint complete census is unavailable")
    # Two root fences, the normal live-head read and protected write/GET tail.
    if operation.remaining < 6:
        raise EvidenceBudgetExhausted(
            "quarantine complete checkpoint cannot fund its fences and confirmation"
        )
    item = ledger._comments_by_id[comment_id]
    ledger._part_owner(item, identity)
    snapshot = _CheckpointSnapshot(
        caller,
        ledger,
        identity,
        operation,
        caller._block,
        caller._root,
        comment_id,
        item["user"]["id"],
        record,
        item["body"],
        canonical_bytes(record.quarantine_lineage),
        frozenset(ledger._comments_by_id),
    )
    _ISSUED.add(snapshot)
    caller._quarantine_checkpoint_snapshot = snapshot
    ledger._quarantine_checkpoint_snapshot = snapshot
    previous_reuse, previous_complete = (
        ledger._reuse_listed_parts,
        ledger._quarantine_complete_read,
    )
    ledger._reuse_listed_parts = ledger._quarantine_complete_read = True
    try:
        snapshot.get()
        _history_legacy_write_guard(ledger, record)
        ledger._mutation_active = True
        try:
            updated = mutate(record)
            require_native_decision(ledger, record, updated)
            _validate_queue_retention(record, updated)
            read_session_baseline(ledger, updated)
            read_session_dispositions(ledger, updated)
            read_session_assessment_queue(ledger, updated)
            _history_legacy_write_guard(ledger, updated)
        finally:
            ledger._mutation_active = False
        object.__setattr__(snapshot, "candidate", updated)
        body = ledger._render_comment(
            repository_id=identity.repository_id,
            pull_request=identity.pull_request,
            record=updated,
        )
        object.__setattr__(snapshot, "candidate_body", body)
        snapshot.get()
        own_release = ledger._is_own_release(record, updated)
        if not own_release or canonical_bytes(
            record.quarantine_lineage
        ) != canonical_bytes(updated.quarantine_lineage):
            reader = ledger._quarantine_origin_reader
            if reader is None:
                raise ReviewInputError("quarantine checkpoint current scope is absent")
            reader.current_scope.require()
        if operation.remaining < operation.write_tail:
            raise EvidenceBudgetExhausted(
                "quarantine checkpoint write confirmation is unfunded"
            )
        status, payload = ledger._request(
            "PATCH",
            ledger.http.repository_path(
                identity.repository, f"/issues/comments/{comment_id}"
            ),
            body={"body": body},
        )
        if status != 200:
            if status in {409, 422, 429} or status >= 500:
                raise GitHubPublicationTransientError(
                    "quarantine checkpoint update was ambiguous"
                )
            raise GitHubPublicationError("quarantine checkpoint update failed")
        snapshot.validate(payload, record=updated, body=body)
        return snapshot.get(candidate=True)
    except BaseException:
        if not operation.write_attempted:
            expire(snapshot)
        raise
    finally:
        ledger._reuse_listed_parts, ledger._quarantine_complete_read = (
            previous_reuse,
            previous_complete,
        )


def reconcile_quarantine_checkpoint(caller, ledger, identity):
    snapshot = getattr(caller, "_quarantine_checkpoint_snapshot", None)
    if (
        type(snapshot) is not _CheckpointSnapshot
        or snapshot.ledger is not ledger
        or snapshot.identity != identity
        or not snapshot.operation.write_attempted
        or caller._checkpoint is None
        or snapshot.candidate is None
        or snapshot.candidate.record_sha256 != caller._checkpoint.record_sha256
    ):
        raise ReviewInputError(
            "quarantine checkpoint reconciliation has no issued snapshot"
        )
    return snapshot.get(candidate=True)
