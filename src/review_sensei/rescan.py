"""Bounded, replay-safe admission of explicitly authorized incomplete rescans."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import cast

from .errors import ReviewInputError
from .models import ReviewTransaction
from .session import SessionRecord

MAX_RESCAN_RECEIPTS = 6
KINDS = frozenset({"issue_comment", "workflow_dispatch"})
# Recovery is only an unfinished publication of an already charged result.
# analysis_failed and publication_suppressed are substantive terminal outcomes
# and stay on the normal admission path.
PUBLICATION_RECOVERY_PHASES = frozenset({"publication_pending", "publication_failed"})
SUBSTANTIVE_TERMINAL_PHASES = frozenset(
    {"analysis_failed", "publication_succeeded", "publication_suppressed"}
)


@dataclass(frozen=True)
class AuthorizedRescan:
    """Trusted host proof of a human request and a published incomplete result.

    Hosts must authenticate the event and the previous publication before
    constructing this value. It is never parsed from model output or CLI JSON.
    """

    kind: str
    request_id: int
    source: ReviewTransaction
    review_id: int
    ordinary_reservation: str | None = None

    def __post_init__(self) -> None:
        if (
            self.kind not in KINDS
            or type(self.request_id) is not int
            or not 0 < self.request_id < 2**63
            or type(self.review_id) is not int
            or not 0 < self.review_id < 2**63
            or self.source.phase != "publication_succeeded"
        ):
            raise ReviewInputError("explicit rescan authority is invalid")


def validate_rescan_state(state: object) -> dict[str, object]:
    from .schemas import validate_public_document

    if (
        isinstance(state, dict)
        and isinstance(state.get("receipts"), list)
        and len(state["receipts"]) > MAX_RESCAN_RECEIPTS
    ):
        raise ReviewInputError("rescan receipt bound was exceeded")
    validate_public_document(state, "rescan-state")
    assert isinstance(state, dict)
    receipts = state["receipts"]
    if not isinstance(receipts, list) or len(receipts) > MAX_RESCAN_RECEIPTS:
        raise ReviewInputError("rescan receipt bound was exceeded")
    for receipt in receipts:
        source = ReviewTransaction.from_dict(receipt["source"])
        if source.phase != "publication_succeeded":
            raise ReviewInputError("rescan source must be published")
        if receipt["request_id"] > state[receipt["kind"]]:
            raise ReviewInputError("rescan receipt exceeds replay watermark")
    return json.loads(json.dumps(state))


def rescan_source(
    record: SessionRecord, head_sha: str | None = None
) -> ReviewTransaction | None:
    """Retain the published source after a failed rescan releases its hold.

    Receipt order is consumption order, not a head timeline. When the live
    transaction is gone, select the latest receipt for ``head_sha`` instead of
    assuming ``receipts[-1]`` is that head.
    """

    if record.transaction is not None:
        return record.transaction
    if record.rescan_state is None:
        return None
    receipts = record.rescan_state["receipts"]
    if not isinstance(receipts, list) or not receipts:
        return None
    selected: dict[str, object] | None = None
    for item in receipts:
        if not isinstance(item, dict):
            continue
        source = item.get("source")
        if not isinstance(source, dict):
            continue
        if head_sha is not None and source.get("head_sha") != head_sha:
            continue
        selected = source
    if selected is None:
        return None
    return ReviewTransaction.from_dict(selected)


def replayed_rescan(record: SessionRecord, request: AuthorizedRescan) -> bool:
    return record.rescan_state is not None and request.request_id <= int(
        cast(int, record.rescan_state[request.kind])
    )


def rescan_reservation(
    record: SessionRecord, request: object, head_sha: str | None
) -> str:
    if not isinstance(request, AuthorizedRescan):
        raise ReviewInputError("explicit rescan authority is invalid")
    source = rescan_source(record, head_sha)
    if not replayed_rescan(record, request) and (
        source is None
        or source.transaction_id != request.source.transaction_id
        or source.head_sha != head_sha
        or source.repository != record.repository
        or source.pull_request != record.pull_request
    ):
        raise ReviewInputError("explicit rescan source changed")
    return hashlib.sha256(
        f"rescan:{record.repository}:{record.pull_request}:{head_sha}:{request.kind}:{request.request_id}".encode()
    ).hexdigest()


def ordinary_rescan_duplicate(
    record: SessionRecord, reservation: str, head_sha: str | None
) -> bool:
    if record.rescan_state is None:
        return False
    receipts = record.rescan_state["receipts"]
    assert isinstance(receipts, list)
    return any(
        item["source"]["head_sha"] == head_sha
        and reservation in {item["ordinary_reservation"], item["reservation"]}
        for item in receipts
    )


def consume_rescan_request(
    record: SessionRecord, request: object, reservation: str
) -> SessionRecord:
    if not isinstance(request, AuthorizedRescan) or replayed_rescan(record, request):
        raise ReviewInputError("explicit rescan request was already consumed")
    rescan_reservation(record, request, request.source.head_sha)
    prior = record.rescan_state
    if prior is None:
        carried: list[object] = []
        state: dict[str, object] = {"issue_comment": 0, "workflow_dispatch": 0}
    else:
        state = {
            "issue_comment": prior["issue_comment"],
            "workflow_dispatch": prior["workflow_dispatch"],
        }
        receipts = prior["receipts"]
        assert isinstance(receipts, list)
        # Copy the list and each receipt so later mutation cannot alias the
        # source record. The new state always includes this receipt; an empty
        # list is never built or validated.
        carried = copy.deepcopy(list(receipts))
    state[request.kind] = request.request_id
    state["receipts"] = [
        *carried[-(MAX_RESCAN_RECEIPTS - 1) :],
        {
            "kind": request.kind,
            "request_id": request.request_id,
            "review_id": request.review_id,
            "source": request.source.to_dict(),
            "ordinary_reservation": request.ordinary_reservation
            or request.source.reservation_id,
            "reservation": reservation,
        },
    ]
    if not state["receipts"]:
        raise ReviewInputError("explicit rescan receipt was not recorded")
    return record.evolve(rescan_state=state)


def is_rescan_transaction(record: SessionRecord) -> bool:
    if record.transaction is None or record.rescan_state is None:
        return False
    receipts = record.rescan_state["receipts"]
    assert isinstance(receipts, list)
    return any(
        item["reservation"] == record.transaction.reservation_id for item in receipts
    )


def rescan_publication_recovery(record: SessionRecord, head_sha: str | None) -> bool:
    transaction = record.transaction
    if transaction is None or transaction.phase in SUBSTANTIVE_TERMINAL_PHASES:
        return False
    if transaction.phase not in PUBLICATION_RECOVERY_PHASES:
        return False
    return bool(is_rescan_transaction(record) and transaction.head_sha == head_sha)
