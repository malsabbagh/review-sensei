"""Fresh closed-invocation census, never a replacement lifetime allocation.

The initial consumer retains its existing C4 quote and reservations. This
reader has no independent execution issuer and cannot admit a final decision.
"""

import hashlib
import json
import sys
from typing import Any, cast
from weakref import WeakKeyDictionary

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import require_module_seal

from .http import MAX_GITHUB_RESPONSE_BYTES


def _wire(value):
    return len(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode())


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _owner(row):
    user = row.get("user")
    return (
        tuple(user.get(key) for key in ("id", "type", "login"))
        if isinstance(user, dict)
        else None
    )


def _registry():
    issued: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()
    reads: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()

    class Read:
        pass

    def issue(lease, entry):
        from . import quarantine_historical_execution as historical
        from .session_ledger import parse_session_comment

        require_module_seal(historical)
        frame = sys._getframe(1)
        values = cast(Any, frame.f_locals)
        if (
            frame.f_code is not historical.activate.__code__
            or frame.f_globals is not vars(historical)
            or values.get("lease") is not lease
            or values.get("saved") is not entry
        ):
            raise ReviewInputError("historical census lacks original lease issuance")
        recipe = json.loads(entry["recipe"])
        # Qualify a physical reader first without lowering the existing quote.
        if recipe["contract"] != "closed-inline-c4-reserved-verification-v1":
            return
        if entry["page"].maximum_bytes + 4096 > MAX_GITHUB_RESPONSE_BYTES:
            return
        rows = json.loads(entry["captured"]["rows"])
        root = entry["caller"]._effect_record
        selected = [
            row
            for row in rows
            if (
                record := parse_session_comment(row["body"], identity=entry["identity"])
            )
            is not None
            and record.record_sha256 == root.record_sha256
        ]
        if len(selected) != 1 or any(
            b.pending for b in entry["caller"].accounting.blocks
        ):
            raise ReviewInputError("historical census initial root is unsupported")
        known = {row["id"]: (_digest(row["body"]), _owner(row)) for row in rows}
        if len(known) != len(rows) or len(known) + 2 >= 100:
            raise ReviewInputError(
                "historical census initial membership is unsupported"
            )
        issued[entry["caller"]] = dict(
            lease=lease,
            entry=entry,
            known=known,
            root_id=selected[0]["id"],
            bounds=dict(entry["page"].known),
            event=recipe["event_exact_body"],
            maximum=entry["page"].maximum_bytes + 4096,
        )

    def choose(ledger, identity):
        from . import quarantine_census
        from . import quarantine_historical_execution as historical

        caller = getattr(ledger, "caller", None)
        if caller is None:
            return None
        saved = issued.get(caller)
        if saved is None or caller._terminal_reason is not None:
            return None
        require_module_seal(sys.modules[__name__])
        require_module_seal(quarantine_census)
        frame = sys._getframe(1)
        values = cast(Any, frame.f_locals)
        if (
            frame.f_code is not quarantine_census.fetch_comments.__code__
            or frame.f_globals is not vars(quarantine_census)
            or values.get("ledger") is not ledger
            or values.get("identity") is not identity
        ):
            raise ReviewInputError("historical census reader is not original")
        entry = saved["entry"]
        # Receipt/terminal discovery retains the existing complete reader.
        if entry["phase"] >= 7:
            return None
        require_module_seal(historical)
        historical.require_closed(saved["lease"], entry["run"])
        if entry["ledger"] is not ledger or entry["identity"] != identity:
            raise ReviewInputError("historical census scope changed")
        token = Read()
        reads[token] = (
            saved,
            caller._root,
            caller._effect_record,
            len(caller.observations),
            caller._active_operation,
        )
        return token

    def finish(token, ledger, identity, rows, response):
        from . import quarantine_census
        from . import quarantine_historical_execution as historical
        from .session_ledger import parse_session_comment

        require_module_seal(sys.modules[__name__])
        require_module_seal(quarantine_census)
        frame = sys._getframe(1)
        values = cast(Any, frame.f_locals)
        saved = reads.pop(token, None) if type(token) is Read else None
        if (
            saved is None
            or frame.f_code is not quarantine_census.fetch_comments.__code__
            or frame.f_globals is not vars(quarantine_census)
            or values.get("historical_reader") is not token
            or values.get("collected") is not rows
            or values.get("response") is not response
            or values.get("_attempt") != 0
            or values.get("first") != 100
            or values.get("after") is not None
        ):
            raise ReviewInputError("historical census completion is copied or foreign")
        owner, root, record, start, operation = saved
        entry = owner["entry"]
        caller = entry["caller"]
        require_module_seal(historical)
        historical.require_closed(owner["lease"], entry["run"])
        observed = caller.observations[start:]
        if (
            ledger is not entry["ledger"]
            or identity != entry["identity"]
            or caller._root != root
            or caller._effect_record is not record
            or caller._active_operation is not operation
            or len(observed) != 1
            or observed[0].get("mutation") is not False
            or observed[0].get("outcome") != "response"
            or _wire(response) > owner["maximum"]
        ):
            raise ReviewInputError("historical census charged response changed")
        nodes = response["data"]["repository"]["pullRequest"]["comments"]["nodes"]
        if any(_wire(node) - _wire({"body": node["body"]}) > 4096 for node in nodes):
            raise ReviewInputError("historical census metadata exceeds its bound")
        known, found, event_rows = owner["known"], set(), []
        for row in rows:
            identifier = row["id"]
            if identifier in found:
                raise ReviewInputError("historical census duplicates membership")
            found.add(identifier)
            if identifier not in known:
                if row["body"] != owner["event"]:
                    raise ReviewInputError("historical census has unpriced membership")
                ledger._part_owner(row, identity)
                event_rows.append(row)
                continue
            digest, author = known[identifier]
            if (
                _owner(row) != author
                or _wire({"body": row["body"]}) > owner["bounds"][identifier]
            ):
                raise ReviewInputError(
                    "historical census member owner or width changed"
                )
            if identifier == owner["root_id"]:
                parsed = parse_session_comment(row["body"], identity=identity)
                if parsed is None or canonical_bytes(
                    parsed.to_dict()
                ) != canonical_bytes(record.to_dict()):
                    raise ReviewInputError("historical census owned root changed")
                ledger._part_owner(row, identity)
            elif _digest(row["body"]) != digest:
                raise ReviewInputError("historical census retained body changed")
        if set(known) - found or len(event_rows) != int(entry["phase"] >= 2):
            raise ReviewInputError("historical census complete membership changed")
        return rows

    return issue, choose, finish


issue, choose, finish = _registry()
del _registry

from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
