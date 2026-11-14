"""Closed intermediate review reader, with no independent effect authority.

The lifetime guard and the original 105-call quote are unchanged. No approval
allocation is supported here; a new review, even a matching one, is a hold.
"""

import json
import sys
from dataclasses import dataclass
from typing import Any, cast
from weakref import WeakKeyDictionary

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import require_module_seal


def _wire(value):
    return len(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode())


@dataclass(frozen=True, eq=False)
class ReviewWalk:
    width: int
    requests: int

    def validate(self, rows):
        finish(self, rows)


def _registry():
    issued: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()
    walks: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()
    pending: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()

    def issue(lease, entry: Any):
        from . import quarantine_historical_execution as historical

        require_module_seal(sys.modules[__name__])
        require_module_seal(historical)
        frame = sys._getframe(1)
        values = cast(Any, frame.f_locals)
        if (
            frame.f_code is not historical.activate.__code__
            or frame.f_globals is not vars(historical)
            or values.get("lease") is not lease
            or values.get("saved") is not entry
        ):
            raise ReviewInputError("historical review reader lacks original issuer")
        recipe = json.loads(entry["recipe"])
        if recipe["contract"] != "closed-inline-c4-reserved-verification-v1":
            return
        caller = entry["caller"]
        if caller in issued or any(block.pending for block in caller.accounting.blocks):
            raise ReviewInputError("historical review reader has simultaneous scope")
        historical.require_closed(lease, entry["run"])
        rows = json.loads(entry["target"].native_frontier_bytes)
        before = json.loads(entry["target"].native_review_bytes)
        update = recipe["review"]
        if (
            not isinstance(rows, list)
            or not rows
            or len(rows) >= 100
            or sum(row == before for row in rows) != 1
            or update[:2]
            != [
                "PUT",
                entry["run"].http.repository_path(
                    entry["identity"].repository,
                    f"/pulls/{entry['identity'].pull_request}/reviews/{before['id']}",
                ),
            ]
            or set(update[2]) != {"body"}
        ):
            raise ReviewInputError("historical review reader frontier is unsupported")
        known = {row["id"]: canonical_bytes(row) for row in rows}
        if len(known) != len(rows) or any(
            type(key) is not int or not 0 < key <= 2**63 - 1 for key in known
        ):
            raise ReviewInputError(
                "historical review reader identities are unsupported"
            )
        certificate = entry["guard"].certificates["reviews"]
        largest = certificate["largest_upper_bound"]
        maximum = 2 + len(rows) * largest + len(rows) - 1
        after = {**before, "body": update[2]["body"]}
        if maximum > 512 * 1024 or any(_wire(row) > largest for row in (*rows, after)):
            # A larger source shape retains the original complete lifetime walk.
            return
        issued[caller] = dict(
            lease=lease,
            entry=entry,
            known=known,
            selected=before["id"],
            after=canonical_bytes(after),
            largest=largest,
            maximum=maximum,
            width=len(rows) + 1,
        )

    def resolve_journal(http):
        from . import (
            command_finalization_plan,
            finding_lifecycle,
            publication_recovery,
            quarantine_scan_capacity,
        )
        from . import quarantine_historical_execution as historical

        require_module_seal(publication_recovery)
        original_http = http
        http = publication_recovery._publication_transport(http)
        if type(http) is not finding_lifecycle._JournalTransport:
            return None
        journal = vars(http).get("journal")
        if type(journal) is not finding_lifecycle.FindingLifecycleJournal:
            return None
        saved = issued.get(journal.ledger.caller)
        if saved is None:
            return None
        for module in (
            sys.modules[__name__],
            command_finalization_plan,
            finding_lifecycle,
            quarantine_scan_capacity,
            historical,
        ):
            require_module_seal(module)
        frame = sys._getframe(1)
        if (
            frame.f_code is not quarantine_scan_capacity.capacity_review_walk.__code__
            or frame.f_globals is not vars(quarantine_scan_capacity)
            or frame.f_locals.get("base") is not original_http
        ):
            raise ReviewInputError("historical journal reader selection is foreign")
        entry = saved["entry"]
        historical.require_closed(saved["lease"], entry["run"])
        if entry["journal"] is not journal:
            raise ReviewInputError("historical journal reader owner changed")
        delegate = command_finalization_plan.registered_journal_delegate(http)
        transport = publication_recovery._publication_transport(delegate)
        if transport is not publication_recovery._publication_transport(
            entry["run"].http
        ):
            raise ReviewInputError("historical journal reader delegate changed")
        return entry["ledger"], entry["guard"], transport

    def choose(ledger, guard):
        from . import quarantine_historical_execution as historical
        from . import quarantine_scan_capacity

        caller = ledger.caller
        saved = issued.get(caller)
        if saved is None or caller._terminal_reason is not None:
            return None
        require_module_seal(sys.modules[__name__])
        require_module_seal(quarantine_scan_capacity)
        frame = sys._getframe(1)
        if (
            frame.f_code is not quarantine_scan_capacity.capacity_review_walk.__code__
            or frame.f_globals is not vars(quarantine_scan_capacity)
            or frame.f_locals.get("ledger") is not ledger
            or frame.f_locals.get("guard") is not guard
        ):
            raise ReviewInputError("historical review selection source changed")
        entry = saved["entry"]
        if entry["phase"] >= 7:
            return None
        require_module_seal(historical)
        historical.require_closed(saved["lease"], entry["run"])
        if (
            entry["ledger"] is not ledger
            or entry["guard"] is not guard
            or caller in pending
        ):
            raise ReviewInputError(
                "historical review reader has simultaneous or foreign scope"
            )
        token = ReviewWalk(saved["width"], 1)
        walks[token] = (
            saved,
            caller._root,
            caller._effect_record,
            len(caller.observations),
            caller._active_operation,
        )
        pending[caller] = token
        return token

    def finish(token, rows):
        from . import http
        from . import quarantine_historical_execution as historical

        require_module_seal(sys.modules[__name__])
        require_module_seal(http)
        saved_read = walks.pop(token, None) if type(token) is ReviewWalk else None
        frame = sys._getframe(2)
        values = cast(Any, frame.f_locals)
        if (
            saved_read is None
            or frame.f_code is not http.GitHubHttp._paginate.__code__
            or frame.f_globals is not vars(http)
            or values.get("review_walk") is not token
        ):
            raise ReviewInputError("historical review completion is copied or foreign")
        saved, root, record, start, operation = saved_read
        entry, caller = saved["entry"], saved["entry"]["caller"]
        if pending.pop(caller, None) is not token:
            raise ReviewInputError("historical review completion scope changed")
        require_module_seal(historical)
        historical.require_closed(saved["lease"], entry["run"])
        observed = caller.observations[start:]
        if (
            token.width != saved["width"]
            or token.requests != 1
            or values.get("page_size") != token.width
            or values.get("requests") != 1
            or values.get("collected") != []
            or values.get("body") != rows
            or caller._root != root
            or caller._effect_record is not record
            or caller._active_operation is not operation
            or len(observed) != 1
            or observed[0].get("mutation") is not False
            or observed[0].get("outcome") != "response"
            or _wire(rows) > saved["maximum"]
        ):
            raise ReviewInputError("historical review charged complete page changed")
        if any(type(row) is not dict or type(row.get("id")) is not int for row in rows):
            raise ReviewInputError("historical review membership changed")
        if len(rows) != len(saved["known"]) or {row.get("id") for row in rows} != set(
            saved["known"]
        ):
            raise ReviewInputError("historical review membership changed")
        for row in rows:
            raw, identifier = canonical_bytes(row), row["id"]
            if _wire(row) > saved["largest"] or not (
                raw == saved["known"][identifier]
                or identifier == saved["selected"]
                and raw == saved["after"]
            ):
                raise ReviewInputError("historical review body or owner changed")

    return issue, resolve_journal, choose, finish


issue, resolve_journal, choose, finish = _registry()
del _registry

from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
