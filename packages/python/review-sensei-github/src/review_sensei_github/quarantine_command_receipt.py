"""Negative-only, complete receipt-page checks; never command admission.

A full-route issuer must authenticate and bind these capacity observations before
business effects. This module neither changes lifetime allocation plans nor
turns a body/count observation into permission to publish a receipt.
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import sys
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import require_module_seal

from .http import MAX_GITHUB_RESPONSE_BYTES

METADATA_BYTES = 4096


def _wire(value):
    return len(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode())


def _body_digest(body):
    return hashlib.sha256(body.encode()).hexdigest()


@dataclass(frozen=True)
class ReceiptPageProjection:
    """Sizing data only. Explicit future allocations are never omitted."""

    known: tuple[tuple[int, int], ...]
    future: tuple[int, ...]
    maximum_bytes: int


def project_receipt_page(rows, *, mutable_bounds=(), future_bounds=()):
    """Bound every complete body plus REST metadata and list framing.

    A caller must supply each reachable own event/archive/receipt allocation.
    This projection does not release or reclassify any lifetime unknown slot.
    It describes a fixed-membership invocation whose growth must be rejected.
    """
    mutable = dict(mutable_bounds)
    if len(mutable) != len(mutable_bounds) or any(
        type(key) is not int or key <= 0 for key in mutable
    ):
        raise ReviewInputError("receipt mutable capacity duplicates identity")
    known = {}
    for row in rows:
        if (
            type(row) is not dict
            or type(row.get("id")) is not int
            or row["id"] <= 0
            or row["id"] in known
            or not isinstance(row.get("body"), str)
        ):
            raise ReviewInputError("receipt capacity inventory is invalid")
        known[row["id"]] = _wire({"body": row["body"]})
    if set(mutable) - set(known):
        raise ReviewInputError("receipt mutable capacity identity is absent")
    for identifier, bound in mutable.items():
        if type(bound) is not int or bound < known[identifier]:
            raise ReviewInputError("receipt mutable body bound is invalid")
        known[identifier] = bound
    if type(future_bounds) is not tuple or any(
        type(bound) is not int or bound <= 0 for bound in future_bounds
    ):
        raise ReviewInputError("receipt future allocation bound is invalid")
    count = len(known) + len(future_bounds)
    # A full page is not evidence of completeness, even at the capacity ceiling.
    if count >= 100:
        raise ReviewInputError("receipt complete single page exceeds item bound")
    maximum = (
        2 + sum(known.values()) + sum(future_bounds) + count * (METADATA_BYTES + 1)
    )
    if maximum > MAX_GITHUB_RESPONSE_BYTES:
        raise ReviewInputError("receipt complete single page exceeds wire bound")
    return ReceiptPageProjection(tuple(known.items()), future_bounds, maximum)


@dataclass(frozen=True)
class ReceiptMembership:
    """Exact final-phase commitments, not an executable grant or cached data."""

    bodies: tuple[tuple[int, str], ...]
    owners: tuple[tuple[int, tuple[object, object, object]], ...]
    receipt_text: str
    receipt_update_id: int | None
    app_id: int
    maximum_bytes: int


def _owner(row):
    user = row.get("user")
    return (
        tuple(user.get(key) for key in ("id", "type", "login"))
        if isinstance(user, dict)
        else (None, None, None)
    )


def freeze_receipt_membership(rows, *, projection, receipt_text, update_id, app_id):
    """Check final known rows against an already bounded complete inventory.

    The source issuer, not this pure checker, must prove new own event identity
    and final checkpoint custody. Newly materialized known rows consume the
    smallest matching future bound, never an old lifetime uncertainty slot.
    """
    if type(projection) is not ReceiptPageProjection:
        raise ReviewInputError("receipt page projection is invalid")
    if not isinstance(receipt_text, str) or type(app_id) is not int or app_id <= 0:
        raise ReviewInputError("receipt writer identity is invalid")
    known, future, seen = dict(projection.known), list(projection.future), {}
    owners = {}
    for row in rows:
        if (
            type(row) is not dict
            or type(row.get("id")) is not int
            or row["id"] <= 0
            or row["id"] in seen
            or not isinstance(row.get("body"), str)
        ):
            raise ReviewInputError("receipt final inventory is invalid")
        identifier, size = row["id"], _wire({"body": row["body"]})
        bound = known.get(identifier)
        if bound is None:
            fitting = [item for item in future if item >= size]
            if not fitting:
                raise ReviewInputError("receipt final inventory has unbounded growth")
            future.remove(min(fitting))
        elif size > bound:
            raise ReviewInputError("receipt final body exceeds its pre-effect bound")
        seen[identifier] = _body_digest(row["body"])
        owners[identifier] = _owner(row)
    if set(known) - set(seen):
        raise ReviewInputError("receipt final inventory member disappeared")
    receipt_size = _wire({"body": receipt_text})
    if update_id is None:
        if not any(bound >= receipt_size for bound in future):
            raise ReviewInputError("receipt creation allocation is absent")
    elif (
        type(update_id) is not int
        or update_id not in seen
        or receipt_size > known.get(update_id, 0)
    ):
        raise ReviewInputError("receipt update allocation is absent")
    return ReceiptMembership(
        tuple(seen.items()),
        tuple(owners.items()),
        receipt_text,
        update_id,
        app_id,
        projection.maximum_bytes,
    )


def read_receipt_page(http, *, path, token, membership, reconcile=False):
    """Perform exactly one fresh charged read, or refuse without a retry."""
    if type(membership) is not ReceiptMembership or type(reconcile) is not bool:
        raise ReviewInputError("receipt census input is invalid")
    if not isinstance(path, str) or "?" in path or not path.endswith("/comments"):
        raise ReviewInputError("receipt census path is invalid")
    status, rows = http.request("GET", path + "?per_page=100&page=1", token=token)
    if status != 200 or not isinstance(rows, list) or len(rows) >= 100:
        raise ReviewInputError("receipt census lacks one complete page")
    if _wire(rows) > min(membership.maximum_bytes, MAX_GITHUB_RESPONSE_BYTES):
        raise ReviewInputError("receipt census exceeded complete wire bound")
    expected, observed = dict(membership.bodies), set()
    owners = dict(membership.owners)
    created = 0
    for row in rows:
        if (
            type(row) is not dict
            or type(row.get("id")) is not int
            or row["id"] <= 0
            or row["id"] in observed
            or not isinstance(row.get("body"), str)
        ):
            raise ReviewInputError("receipt census identity is invalid")
        identifier, body = row["id"], row["body"]
        observed.add(identifier)
        if identifier in expected and _owner(row) != owners.get(identifier):
            raise ReviewInputError("receipt census known owner changed")
        if _wire(row) - _wire({"body": body}) > METADATA_BYTES:
            raise ReviewInputError("receipt census metadata exceeds its bound")
        changed = expected.get(identifier) != _body_digest(body)
        if not changed:
            continue
        user = row.get("user")
        own_receipt = (
            reconcile
            and body == membership.receipt_text
            and isinstance(user, dict)
            and user.get("id") == membership.app_id
            and user.get("type") == "Bot"
        )
        if identifier in expected:
            if not own_receipt or identifier != membership.receipt_update_id:
                raise ReviewInputError("receipt census known body changed")
        else:
            created += 1
            if (
                not own_receipt
                or membership.receipt_update_id is not None
                or created > 1
            ):
                raise ReviewInputError("receipt census membership grew")
    if set(expected) - observed:
        raise ReviewInputError("receipt census member disappeared")
    return rows


def _membership_binding(value):
    if type(value) is not ReceiptMembership:
        raise ReviewInputError("receipt phase membership is unavailable")
    return (
        value.bodies,
        value.owners,
        value.receipt_text,
        value.receipt_update_id,
        value.app_id,
        value.maximum_bytes,
    )


def _writer_frame(*, run, authority):
    from . import action_receipt

    require_module_seal(action_receipt)
    code = action_receipt.publish_command_receipt.__code__
    frame = inspect.currentframe()
    while frame is not None:
        if frame.f_code is code:
            if (
                frame.f_globals is not vars(action_receipt)
                or frame.f_locals.get("run") is not run
                or frame.f_locals.get("authority") is not authority
            ):
                break
            return frame
        frame = frame.f_back
    raise ReviewInputError("receipt phase requires its original writer frame")


def _make_windows():
    windows: weakref.WeakKeyDictionary[Any, Any] = weakref.WeakKeyDictionary()

    def require(window):
        require_module_seal(sys.modules[__name__])
        entry = windows.get(window) if type(window) is _ReceiptWindow else None
        if entry is None:
            raise ReviewInputError("receipt read window is unissued or consumed")
        _writer_frame(run=entry["run"], authority=entry["authority"])
        require_module_seal(entry["issuer"])
        actual = entry["issuer"].validate_receipt_phase(
            entry["lease"], entry["run"], entry["authority"]
        )
        if _membership_binding(actual) != entry["binding"]:
            raise ReviewInputError("receipt read window membership changed")
        return entry

    @dataclass(frozen=True, eq=False)
    class _ReceiptWindow:
        def read(self, *, path, token):
            entry = require(self)
            run = entry["run"]
            expected = run.http.repository_path(
                run.identity.repository, f"/issues/{run.identity.pull_request}/comments"
            )
            if path != expected or entry["reads"] not in (0, 1):
                raise ReviewInputError("receipt read window scope or count changed")
            reconcile = entry["reads"] == 1
            if reconcile:
                mutation = entry["mutation"]
                if mutation is None:
                    raise ReviewInputError("receipt reconciliation has no own dispatch")
                method, target, start = mutation
                from .trust_effects import require_native_attempt, verification_profile

                profile = verification_profile(run.caller)
                attempts = run.caller.observations[start:]
                if len(attempts) != 1 + int(profile is not None):
                    raise ReviewInputError(
                        "receipt reconciliation dispatch count changed"
                    )
                if profile is not None:
                    require_native_attempt(
                        run.caller,
                        observations_start=start,
                        method=method,
                        url=run.http.api_url + target,
                        body=json.dumps(
                            {"body": entry["membership"].receipt_text},
                            separators=(",", ":"),
                        ).encode(),
                    )
                elif (
                    len([item for item in attempts if item.get("mutation") is True])
                    != 1
                ):
                    raise ReviewInputError(
                        "receipt reconciliation own mutation is absent"
                    )
            elif entry["mutation"] is not None:
                raise ReviewInputError("receipt first census followed a mutation")
            entry["reads"] += 1
            return read_receipt_page(
                run.http,
                path=path,
                token=token,
                membership=entry["membership"],
                reconcile=reconcile,
            )

        def confirmation_requests(self):
            entry = require(self)
            if entry["reads"] != 1 or entry["mutation"] is not None:
                raise ReviewInputError("receipt confirmation bound is out of phase")
            # One complete read and the unchanged two-read authority fence.
            return 3

        def before_write(self, *, method, target, observation_index):
            entry = require(self)
            run, membership = entry["run"], entry["membership"]
            expected = run.http.repository_path(
                run.identity.repository,
                f"/issues/{run.identity.pull_request}/comments"
                if membership.receipt_update_id is None
                else f"/issues/comments/{membership.receipt_update_id}",
            )
            if (
                entry["reads"] != 1
                or entry["mutation"] is not None
                or method
                != ("POST" if membership.receipt_update_id is None else "PATCH")
                or target != expected
                or observation_index != len(run.caller.observations)
            ):
                raise ReviewInputError(
                    "receipt writer differs from its source-bound phase"
                )
            entry["mutation"] = (method, target, observation_index)

    @contextmanager
    def receipt_read_window(run, authority, *, text, marker):
        _writer_frame(run=run, authority=authority)
        name = "review_sensei_github.quarantine_command_execution"
        try:
            issuer = importlib.import_module(name)
        except ModuleNotFoundError as error:
            if error.name != name:
                raise
            # No complete-route issuer exists in older/ordinary compositions.
            # This fallback preserves the old conservative reader and quote.
            yield None
            return
        require_module_seal(issuer)
        lease = issuer.require_receipt_phase(run, authority, text=text, marker=marker)
        if lease is None:
            yield None
            return
        window = _ReceiptWindow()
        try:
            membership = issuer.validate_receipt_phase(lease, run, authority)
            binding = _membership_binding(membership)
            if membership.receipt_text != text:
                raise ReviewInputError(
                    "receipt phase text differs from its source producer"
                )
            windows[window] = dict(
                issuer=issuer,
                lease=lease,
                run=run,
                authority=authority,
                membership=membership,
                binding=binding,
                reads=0,
                mutation=None,
            )
            yield window
        finally:
            windows.pop(window, None)
            issuer.close_receipt_phase(lease, run, authority)

    return _ReceiptWindow, receipt_read_window


_ReceiptWindow, receipt_read_window = _make_windows()
del _make_windows

from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
