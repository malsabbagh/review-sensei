"""Unconnected wire candidate for a single prospective G29 closure PATCH.

Preparation is a pure accounting projection, not permission to dispatch. A
future producer must bind the actual transport slot to this exact body after
the native root/target fences, and prove that closure is the final mutation.
The echo proves only the encoded effect, never the original HTTP response or
that other operations did not start. Nothing here enables ordinary rollover.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, cast

from review_sensei.api_work import (
    ACCOUNTING_VERSION,
    MAX_API_REQUEST_LIMIT,
    ApiExecutionOwner,
    ApiRunAccounting,
    CallerRequestBlock,
    PendingApiRequest,
)
from review_sensei.bounded_evidence import (
    MAX_PART_BYTES,
    canonical_bytes,
    partition_evidence,
)
from review_sensei.errors import EvidenceBudgetExhausted, ReviewInputError
from review_sensei.models import ReviewTransaction
from review_sensei.session import SessionIdentity, SessionRecord

from .api_run_history import retained_binding
from .api_run_plan import WRITE_CONFIRMATION_TAIL
from .session_ledger import (
    _immutable_part_body,
    parse_session_comment,
    render_session_comment,
)

_DOMAIN = "abort-close-v1"
_TEMPLATE = _DOMAIN + "-{:x}-{}"
_MARKER = re.compile(r"abort-close-v1-([1-9a-f][0-9a-f]{0,13})-([0-9a-f]{64})")
_PHYSICAL = re.compile(r"c-0-([1-9a-f][0-9a-f]{0,13})")
_OWNER = ApiExecutionOwner(1, "hosted", 0)
_BLOCK = "hosted-terminal"
_EMPTY_DIGEST = "0" * 64


def _positive(value: object, limit: int) -> bool:
    return type(value) is int and 0 < value <= limit


@dataclass(frozen=True)
class AbortCloseTarget:
    """Values for an already selected plain GitHub root; no authenticated handle."""

    identity: SessionIdentity
    api_url: str
    comment_id: int
    app_login: str
    producer_id: int

    def __post_init__(self) -> None:
        if (
            type(self.identity) is not SessionIdentity
            or self.identity.repository_id is None
            or not _positive(self.comment_id, 9_223_372_036_854_775_807)
            or not _positive(self.producer_id, 9_223_372_036_854_775_807)
            or not isinstance(self.app_login, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,98}\[bot\]", self.app_login)
            is None
            or not isinstance(self.api_url, str)
            or re.fullmatch(
                r"https://[A-Za-z0-9.-]{1,253}(?::[0-9]{1,5})?(?:/api/v3)?",
                self.api_url,
            )
            is None
        ):
            raise ReviewInputError("prospective abort close native target is invalid")

    @property
    def patch_path(self) -> str:
        return f"/repos/{self.identity.repository}/issues/comments/{self.comment_id}"

    @property
    def issue_url(self) -> str:
        return f"{self.api_url}/repos/{self.identity.repository}/issues/{self.identity.pull_request}"


@dataclass(frozen=True)
class PreparedAbortClose:
    record: SessionRecord
    pending: PendingApiRequest
    body: str
    archive_body_bytes: int


@dataclass(frozen=True)
class AbortCloseEcho:
    root_sha256: str
    run_id: int
    request_id: str
    original_deadline_unix_ms: int
    authority: Literal[False] = field(init=False, default=False)
    confirms_original_response: Literal[False] = field(init=False, default=False)


def _plain_release(record: SessionRecord, target: AbortCloseTarget) -> None:
    if (
        record.identity() != target.identity
        or record.transaction is not None
        or record.reservation_id is not None
        or record.reserved_slot is not None
        or record.quarantine_lineage is not None
        or record.trust_enrollment is not None
        or record.historical_assessments is not None
    ):
        raise ReviewInputError("prospective abort close requires a plain released root")


def _terminal_block(
    state: ApiRunAccounting, target: AbortCloseTarget
) -> CallerRequestBlock:
    if (
        state.version != ACCOUNTING_VERSION
        or state.owner != _OWNER
        or state.identity.repository_id != target.identity.repository_id
        or state.action is not None
        or state.terminal_transaction is not None
    ):
        raise ReviewInputError(
            "prospective abort close requires the original hosted owner"
        )
    block = next((item for item in state.blocks if item.block_id == _BLOCK), None)
    if block is None or block.issued_by != state.owner:
        raise ReviewInputError("prospective abort close has no owned terminal block")
    return block


def _closure_transaction(
    state: ApiRunAccounting, target: AbortCloseTarget
) -> ReviewTransaction:
    receipt = state.analysis_abort
    if receipt is None or receipt["source_root"] == receipt["cleanup_root"]:
        raise ReviewInputError("prospective abort close has no distinct abort roots")
    tx = ReviewTransaction.from_dict(cast(dict[str, object], receipt["transaction"]))
    if (tx.repository, tx.pull_request) != (
        target.identity.repository,
        target.identity.pull_request,
    ):
        raise ReviewInputError("prospective abort close transaction scope changed")
    return tx


def _validate_sequence(state: ApiRunAccounting, sequence: int) -> None:
    if not _positive(sequence, MAX_API_REQUEST_LIMIT) or any(
        (match := _PHYSICAL.fullmatch(request.request_id)) is None
        or int(match[1], 16) >= sequence
        for request in state.completed
    ):
        raise ReviewInputError("prospective abort close request sequence is not fresh")


def _body(record: SessionRecord, target: AbortCloseTarget) -> str:
    assert target.identity.repository_id is not None  # enforced by AbortCloseTarget
    return render_session_comment(
        repository_id=target.identity.repository_id,
        pull_request=target.identity.pull_request,
        record=record,
    )


def _require_exact_native_row(
    target: AbortCloseTarget, record: SessionRecord, row: object
) -> None:
    user = row.get("user") if type(row) is dict else None
    if (
        type(row) is not dict
        or type(row.get("id")) is not int
        or row["id"] != target.comment_id
        or row.get("issue_url") != target.issue_url
        or type(user) is not dict
        or type(user.get("id")) is not int
        or user["id"] != target.producer_id
        or user.get("type") != "Bot"
        or not isinstance(user.get("login"), str)
        or user["login"].casefold() != target.app_login.casefold()
        or row.get("body") != _body(record, target)
    ):
        raise ReviewInputError("prospective abort close native echo is not exact")
    observed = parse_session_comment(row["body"], identity=target.identity)
    if observed is None or observed.to_dict() != record.to_dict():
        raise ReviewInputError("prospective abort close native root is unconfirmed")


def _commitment(
    record: SessionRecord,
    target: AbortCloseTarget,
    pending: PendingApiRequest,
    sequence: int,
) -> str:
    # Change only the exact pending row's digest suffix in a detached canonical
    # graph. The separately verified record digest is the other recursive field.
    document = json.loads(canonical_bytes(record.to_dict()))
    document.pop("record_sha256")
    accounting = cast(dict[str, Any], document["api_run_accounting"])
    block = next(item for item in accounting["blocks"] if item["block_id"] == _BLOCK)
    row = next(
        item for item in block["pending"] if item["request_id"] == pending.request_id
    )
    row["request_id"] = _TEMPLATE.format(sequence, _EMPTY_DIGEST)
    return hashlib.sha256(
        canonical_bytes(
            {
                "domain": _DOMAIN,
                "method": "PATCH",
                "target": target.api_url + target.patch_path,
                "app_login": target.app_login.casefold(),
                "producer_id": target.producer_id,
                "record": document,
            }
        )
    ).hexdigest()


def prepare_abort_close(
    target: AbortCloseTarget,
    *,
    cleanup: SessionRecord,
    cleanup_row: object,
    accounting: ApiRunAccounting,
    sequence: int,
    deadline: int,
    now: datetime,
) -> PreparedAbortClose:
    """Price and render the close candidate for an already-known last write.

    The caller supplies the independently confirmed, unadorned cleanup row
    and current accounting with the source-owned
    abort receipt and the exact physical slot/deadline from its transport.
    A projected debit is not durable before the native write actually lands.
    """
    _plain_release(cleanup, target)
    _require_exact_native_row(target, cleanup, cleanup_row)
    old = ApiRunAccounting.from_document(cleanup.api_run_accounting)
    old_block = _terminal_block(old, target)
    block = _terminal_block(accounting, target)
    tx = _closure_transaction(accounting, target)
    receipt = accounting.analysis_abort
    assert receipt is not None
    if (
        receipt["cleanup_root"] != cleanup.record_sha256
        or cleanup.generation != tx.generation + 1
        or old.analysis_abort is not None
        or old.uncertain
        or accounting.uncertain
    ):
        raise ReviewInputError(
            "prospective abort close has an unconfirmed cleanup or another charge"
        )
    old.require_successor(accounting)
    # Every paid call between the retained cleanup bytes and preparation has
    # been observed on this same terminal block. Unallocated funds and other
    # blocks are not part of it; even a newer otherwise valid state cannot
    # replenish this operation or invent an extra reservation.
    if (
        tuple(item.block_id for item in old.blocks)
        != tuple(item.block_id for item in accounting.blocks)
        or tuple(item for item in old.blocks if item.block_id != _BLOCK)
        != tuple(item for item in accounting.blocks if item.block_id != _BLOCK)
        or old.prior_nonstartup_liability != accounting.prior_nonstartup_liability
        or old_block.undispatched - block.undispatched
        != accounting.confirmed - old.confirmed
    ):
        raise ReviewInputError(
            "prospective abort close changed its paid terminal allowance"
        )
    if block.undispatched < WRITE_CONFIRMATION_TAIL:
        raise EvidenceBudgetExhausted(
            "prospective abort close confirmation tail is unfunded"
        )
    _validate_sequence(accounting, sequence)

    def with_pending(pending: PendingApiRequest) -> SessionRecord:
        return cleanup.evolve(
            now=now,
            generation=cleanup.generation + 1,
            api_run_accounting=accounting.dispatch(_BLOCK, pending).to_document(),
        )

    placeholder = PendingApiRequest(_TEMPLATE.format(sequence, _EMPTY_DIGEST), deadline)
    preview = with_pending(placeholder)
    pending = PendingApiRequest(
        _TEMPLATE.format(sequence, _commitment(preview, target, placeholder, sequence)),
        deadline,
    )
    record = with_pending(pending)
    body = _body(record, target)
    # Prepare the same existing single-part predecessor carrier; no live storage
    # is reserved here. The eventual owner still needs to check actual occupancy.
    parts = partition_evidence(
        {"schema_version": "caller-run-root-v1", "record": record.to_dict()},
        binding=retained_binding(record, f"github-bot:{target.producer_id}"),
        item_count=1,
    )
    if (
        len(parts) != 1
        or len(archive := _immutable_part_body(parts[0]).encode()) > MAX_PART_BYTES
    ):
        raise ReviewInputError(
            "prospective abort close exceeds the existing one-part carrier"
        )
    return PreparedAbortClose(record, pending, body, len(archive))


def observe_exact_abort_close(
    target: AbortCloseTarget, *, selected: SessionRecord, row: object
) -> AbortCloseEcho:
    """Match exactly one encoded effect in the fresh row supplied by a native reader.

    This pure function cannot establish read freshness or writer cessation.
    The root and native metadata must come from the caller's trusted selection
    and paid read, not from a historical packet or a user-supplied body.
    """
    _plain_release(selected, target)
    state = ApiRunAccounting.from_document(selected.api_run_accounting)
    block = _terminal_block(state, target)
    tx = _closure_transaction(state, target)
    if (
        selected.generation != tx.generation + 2
        or state.uncertain != 1
        or len(block.pending) != 1
        or block.undispatched < WRITE_CONFIRMATION_TAIL - 1
    ):
        raise ReviewInputError(
            "prospective abort close has no unique paid root witness"
        )
    pending = block.pending[0]
    match = _MARKER.fullmatch(pending.request_id)
    if match is None:
        raise ReviewInputError("opaque request is not a prospective abort close")
    sequence = int(match[1], 16)
    _validate_sequence(state, sequence)
    if _commitment(selected, target, pending, sequence) != match[2]:
        raise ReviewInputError("prospective abort close binding changed")
    _require_exact_native_row(target, selected, row)
    return AbortCloseEcho(
        selected.record_sha256,
        state.identity.run_id,
        pending.request_id,
        pending.deadline_unix_ms,
    )
