"""Explicit historical inventory reads for one admitted rescan publication.

The live ledger remains live. Only a retained-source selector reads the exact
archived predecessor; it grants no staging, transaction or publication authority.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from review_sensei.api_work import ApiRunAccounting
from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewTransaction
from review_sensei.rescan import AuthorizedRescan, rescan_source
from review_sensei.session import SessionIdentity

from .api_run_history import read_previous_run
from .default_human_inventory import HumanInventoryReference
from .default_human_reader import DefaultHumanInventoryReader
from .retained_publication_source import RetainedPublishedInventorySource


def _receipt(record, request):
    rows = record.rescan_state["receipts"] if record.rescan_state else []
    matches = [
        r
        for r in rows
        if r["kind"] == request.kind and r["request_id"] == request.request_id
    ]
    if len(matches) != 1:
        raise ReviewInputError("prior publication has no exact consumed rescan receipt")
    receipt = matches[0]
    if (
        receipt["source"] != request.source.to_dict()
        or receipt["review_id"] != request.review_id
        or receipt.get("mode", "full") != request.mode
        or record.transaction is None
        or receipt["reservation"] != record.transaction.reservation_id
    ):
        raise ReviewInputError("prior publication consumed rescan receipt conflicts")
    return receipt


def admitted_rescan(record) -> AuthorizedRescan | None:
    """Recover only the consumed request bound to this unfinished transaction."""
    if record.transaction is None or record.rescan_state is None:
        return None
    rows = [
        r
        for r in record.rescan_state["receipts"]
        if r["reservation"] == record.transaction.reservation_id
    ]
    if not rows:
        return None
    if len(rows) != 1:
        raise ReviewInputError("successor rescan receipt is ambiguous")
    r = rows[0]
    return AuthorizedRescan(
        r["kind"],
        r["request_id"],
        ReviewTransaction.from_dict(r["source"]),
        r["review_id"],
        ordinary_reservation=r["ordinary_reservation"],
        mode=r.get("mode", "full"),
    )


@dataclass(frozen=True)
class ArchivedRescanSource(RetainedPublishedInventorySource):
    ledger: Any = field(repr=False, compare=False)
    identity: SessionIdentity
    run: Any
    owner: Any
    active_transaction: ReviewTransaction
    archive: Any = field(repr=False)
    request: AuthorizedRescan
    producer: str

    @classmethod
    def create(cls, ledger, request):
        loaded = ledger.load(
            SessionIdentity(
                request.source.repository,
                request.source.pull_request,
                ledger.caller.run.repository_id,
            )
        )
        record = loaded.record
        if loaded.status != "ok" or record is None or record.transaction is None:
            raise ReviewInputError("prior publication live transaction is unavailable")
        state = ApiRunAccounting.from_document(record.api_run_accounting)
        caller = ledger.caller
        if (
            record.record_sha256 != caller._root
            or caller._checkpoint is not None
            or state.identity != caller.accounting.identity
            or state.owner != caller.accounting.owner
            or record.reservation_id is not None
            or record.transaction.phase
            not in {
                "publication_pending",
                "publication_failed",
                "publication_succeeded",
            }
            or (
                record.transaction.repository,
                record.transaction.pull_request,
                record.transaction.base_sha,
                record.transaction.head_sha,
            )
            != (
                request.source.repository,
                request.source.pull_request,
                request.source.base_sha,
                request.source.head_sha,
            )
        ):
            raise ReviewInputError(
                "prior publication live business authority conflicts"
            )
        receipt = _receipt(record, request)
        archive = state.previous_record
        if archive is None:
            raise ReviewInputError(
                "prior publication requires an authenticated archived root"
            )
        identity = record.identity()
        previous = read_previous_run(ledger, identity, archive)
        source = rescan_source(previous, request.source.head_sha)
        if source is None or source.to_dict() != request.source.to_dict():
            raise ReviewInputError("prior publication archive source conflicts")
        result = cls(
            source,
            previous.record_sha256,
            hashlib.sha256(canonical_bytes(receipt)).hexdigest(),
            request.review_id,
            ledger,
            identity,
            state.identity,
            state.owner,
            record.transaction,
            copy.deepcopy(archive),
            request,
            ledger.evidence_producer(),
        )
        result.select(ledger.load(identity))
        return result

    def select(self, loaded):
        record = loaded.record
        caller = self.ledger.caller
        if loaded.status != "ok" or record is None:
            raise ReviewInputError("prior publication live owner is unavailable")
        state = ApiRunAccounting.from_document(record.api_run_accounting)
        tx = record.transaction
        if (
            record.identity() != self.identity
            or record.record_sha256 != caller._root
            or caller._checkpoint is not None
            or state.identity != self.run
            or state.owner != self.owner
            or caller.accounting.identity != self.run
            or caller.accounting.owner != self.owner
            or state.previous_record != self.archive
            or self.ledger.evidence_producer() != self.producer
            or record.reservation_id is not None
            or tx is None
            or tx.phase
            not in {
                "publication_pending",
                "publication_failed",
                "publication_succeeded",
            }
            or not tx.logical_identity_matches(self.active_transaction)
            or tx.result_sha256 != self.active_transaction.result_sha256
            or hashlib.sha256(
                canonical_bytes(_receipt(record, self.request))
            ).hexdigest()
            != self.receipt_sha256
        ):
            raise ReviewInputError("prior publication live business authority changed")
        # The exact immutable bytes were authenticated at construction. Mutable
        # owner/root and source-review prose are freshly checked on every use.
        return self.transaction


class PriorRescanInventoryReader(DefaultHumanInventoryReader):
    def __init__(self, *, reference, source, **kwargs):
        super().__init__(_retained_source=source, **kwargs)
        self.original_reference = reference
        self.source = source

    def __call__(self, reference):
        if reference != self.original_reference:
            raise ReviewInputError("prior publication inventory reference changed")
        return super().__call__(reference)

    def _original_review(self, review, reviews):
        if not isinstance(review, dict) or review.get("id") != self.source.review_id:
            raise ReviewInputError("prior publication exact review conflicts")
        return super()._original_review(review, reviews)


def prior_reader_factory(ledger, http, request, *, original_reference=None):
    """Select no fallback: only the already selected exact source review."""
    source = None
    reader = None

    def selected(review):
        nonlocal source, reader
        if not isinstance(review, dict) or review.get("id") != request.review_id:
            return None
        from .publication import (
            _APPROVAL_ELIGIBILITY_RE,
            APPROVAL_ELIGIBILITY_MARKER_PREFIX,
        )

        body = review.get("body")
        if (
            not isinstance(body, str)
            or body.count(APPROVAL_ELIGIBILITY_MARKER_PREFIX) != 1
        ):
            raise ReviewInputError("prior publication eligibility is malformed")
        match = _APPROVAL_ELIGIBILITY_RE.search(body)
        if match is None:
            raise ReviewInputError("prior publication eligibility is malformed")
        try:
            raw = match.group("payload").rstrip("=")
            doc = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
            value = doc.get("human_inventory_reference")
            if value is None:
                return None
            reference = HumanInventoryReference.from_dict(value)
        except (ValueError, TypeError, AttributeError) as error:
            raise ReviewInputError("prior publication reference is invalid") from error
        if original_reference is not None and reference != original_reference:
            raise ReviewInputError("prior publication original reference conflicts")
        if reader is None:
            source = ArchivedRescanSource.create(ledger, request)
            reader = PriorRescanInventoryReader(
                reference=reference,
                source=source,
                http=http,
                ledger=ledger,
                token=ledger.token,
                repository=source.identity.repository,
                pull_request=source.identity.pull_request,
                head_sha=source.transaction.head_sha,
                app_slug=ledger.app_slug,
                budget=ledger.caller.budget_view,
            )
        elif reference != reader.original_reference:
            raise ReviewInputError("prior publication reference changed")
        return reader

    return selected
