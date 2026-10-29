"""Exact lineage selection, never newest/greatest-root election."""

from review_sensei.errors import ReviewInputError

from .quarantine_activation import CreationSlot, registrations_for
from .quarantine_origin import QuarantineOriginReader


def select_quarantine(ledger, identity, items, found):
    successors = [
        (identifier, record)
        for identifier, record in found
        if record.quarantine_lineage is not None
    ]
    if not successors:
        registrations = registrations_for(identity)
        if registrations or getattr(ledger, "_quarantine_required", False):
            # Ordinary readers may never return to a registered predecessor.
            # Only this process's positive first-execution slot may inspect it
            # before creation, and never after a successor has been observed.
            slot = getattr(ledger, "_quarantine_creation_slot", None)
            caller = getattr(ledger, "_startup_caller", None)
            if (
                getattr(ledger, "_quarantine_required", False)
                or type(slot) is not CreationSlot
                or caller is None
            ):
                raise ReviewInputError(
                    "registered quarantine successor is missing; predecessor fallback and initialization are forbidden"
                )
            slot.require_process(
                http=ledger.http, identity=identity, run_id=caller.run.run_id
            )
            if (
                len(found) != 1
                or found[0][0] != slot.registration["predecessor_comment_id"]
                or found[0][1].record_sha256
                != slot.registration["predecessor_record_sha256"]
            ):
                raise ReviewInputError(
                    "explicit quarantine predecessor inspection conflicts"
                )
        return None
    reader = getattr(ledger, "_quarantine_origin_reader", None)
    if type(reader) is not QuarantineOriginReader:
        raise ReviewInputError("quarantine lineage requires authenticated provenance")
    if len(successors) != 1 or len(found) != 2:
        raise ReviewInputError(
            "quarantine successor or predecessor is duplicated or missing"
        )
    selected_id, selected = successors[0]
    predecessor_id = selected.quarantine_lineage["predecessor_comment_id"]
    if predecessor_id == selected_id or {identifier for identifier, _ in found} != {
        predecessor_id,
        selected_id,
    }:
        raise ReviewInputError("quarantine lineage root relationship conflicts")
    selected_item = next(
        item
        for item in items
        if isinstance(item, dict) and item.get("id") == selected_id
    )
    author = selected_item.get("user")
    if (
        not isinstance(author, dict)
        or type(author.get("id")) is not int
        or author["id"] < 1
    ):
        raise ReviewInputError("quarantine root numeric Bot owner is unavailable")
    ledger._evidence_author_id = author["id"]
    for identifier, _ in found:
        ledger._part_owner(
            next(
                item
                for item in items
                if isinstance(item, dict) and item.get("id") == identifier
            ),
            identity,
        )
    # Every required body must come from this fresh complete listing. Deleted
    # carriers must not survive an incremental-since cache or a previous read.
    prior_reuse = ledger._reuse_listed_parts
    ledger._reuse_listed_parts = True
    ledger._quarantine_complete_read = True
    ledger._verified_parts = {}
    ledger._part_digests = {}
    try:
        ledger._quarantine_authenticated = reader.read(ledger, identity, selected)
    finally:
        ledger._reuse_listed_parts = prior_reuse
        ledger._quarantine_complete_read = False
    ledger._quarantine_required = True
    from .quarantine_capacity import install_retained_capacity

    install_retained_capacity(
        ledger,
        identity,
        ledger._quarantine_authenticated,
        token=reader.metadata_token,
    )
    caller = getattr(ledger, "caller", None) or getattr(ledger, "_startup_caller", None)
    if caller is not None:
        caller._require_ledger(ledger, identity)
        caller._quarantine_census_api_url = ledger.http.api_url
    return selected_id, selected
