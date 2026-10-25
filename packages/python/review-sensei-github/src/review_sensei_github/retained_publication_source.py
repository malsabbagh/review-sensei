"""Read-only inventory authority selected by an explicit rescan resolver.

This reference never restores a transaction, changes a receipt or authorizes a
publication. Every use rechecks the same independently loaded ledger root and
its latest applicable published-source receipt.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, cast

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewTransaction
from review_sensei.rescan import rescan_source
from review_sensei.session import SessionLoadResult


def _latest_receipt(loaded: SessionLoadResult, head: str) -> dict[str, Any] | None:
    record = loaded.record
    receipts = cast(
        list[dict[str, Any]],
        record.rescan_state["receipts"] if record and record.rescan_state else [],
    )
    return next(
        (item for item in reversed(receipts) if item["source"]["head_sha"] == head),
        None,
    )


@dataclass(frozen=True)
class RetainedPublishedInventorySource:
    transaction: ReviewTransaction
    root_sha256: str
    receipt_sha256: str
    review_id: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.transaction, ReviewTransaction)
            or self.transaction.phase != "publication_succeeded"
            or self.transaction.result_sha256 is None
            or type(self.review_id) is not int
            or self.review_id <= 0
            or any(
                not isinstance(value, str)
                or re.fullmatch(r"[a-f0-9]{64}", value) is None
                for value in (self.root_sha256, self.receipt_sha256)
            )
        ):
            raise ReviewInputError("retained published inventory source is invalid")

    @classmethod
    def from_checkpoint(
        cls, loaded: SessionLoadResult, source: ReviewTransaction
    ) -> RetainedPublishedInventorySource:
        receipt = _latest_receipt(loaded, source.head_sha)
        if loaded.record is None or receipt is None:
            raise ReviewInputError(
                "retained published inventory receipt is unavailable"
            )
        reference = cls(
            source,
            loaded.record.record_sha256,
            hashlib.sha256(canonical_bytes(receipt)).hexdigest(),
            receipt["review_id"],
        )
        reference.select(loaded)
        return reference

    def select(self, loaded: SessionLoadResult) -> ReviewTransaction:
        record = loaded.record
        source = rescan_source(record, self.transaction.head_sha) if record else None
        latest = _latest_receipt(loaded, self.transaction.head_sha)
        if (
            loaded.status not in {"ok", "migrated"}
            or record is None
            or record.record_sha256 != self.root_sha256
            or record.transaction is not None
            or record.reservation_id is not None
            or record.reserved_slot is not None
            or source is None
            or source.to_dict() != self.transaction.to_dict()
            or (source.repository, source.pull_request)
            != (record.repository, record.pull_request)
            or latest is None
            or latest["source"] != self.transaction.to_dict()
            or latest["review_id"] != self.review_id
            or hashlib.sha256(canonical_bytes(latest)).hexdigest()
            != self.receipt_sha256
        ):
            raise ReviewInputError(
                "retained published inventory receipt authority conflicts"
            )
        return source
