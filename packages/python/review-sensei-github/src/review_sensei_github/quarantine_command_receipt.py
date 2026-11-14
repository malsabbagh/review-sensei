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
    protocol: str = "rest"
    scope: tuple = ()
    first: int = 100


def project_receipt_page(
    rows, *, mutable_bounds=(), future_bounds=(), allow_graphql=False, identity=None
):
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
        if allow_graphql is not True:
            raise ReviewInputError("receipt complete single page exceeds wire bound")
        scope = _receipt_scope(identity)
        _graphql_request(scope, count + 1)
        maximum = 4096 + sum(known.values()) + sum(future_bounds) + count * 1024
        if maximum > MAX_GITHUB_RESPONSE_BYTES:
            raise ReviewInputError("receipt complete GraphQL page exceeds wire bound")
        return ReceiptPageProjection(
            tuple(known.items()), future_bounds, maximum, "graphql", scope, count + 1
        )
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
    protocol: str = "rest"
    scope: tuple = ()
    first: int = 100


def _receipt_scope(identity):
    from review_sensei import session

    require_module_seal(session)
    if type(identity) is not session.SessionIdentity:
        raise ReviewInputError("receipt GraphQL identity is unavailable")
    values = vars(identity)
    if (
        type(values) is not dict
        or any(type(key) is not str for key in values)
        or set(values) != {"repository", "pull_request", "repository_id"}
    ):
        raise ReviewInputError("receipt GraphQL identity fields are invalid")
    repository, number, identifier = (
        values.get("repository"),
        values.get("pull_request"),
        values.get("repository_id"),
    )
    if (
        type(repository) is not str
        or repository.count("/") != 1
        or type(number) is not int
        or number <= 0
        or type(identifier) is not int
        or identifier <= 0
    ):
        raise ReviewInputError("receipt GraphQL scope is invalid")
    return repository, number, identifier


def _protocol_fields(value):
    protocol, scope, first, maximum = (
        value.protocol,
        value.scope,
        value.first,
        value.maximum_bytes,
    )
    if (
        type(protocol) is not str
        or type(scope) is not tuple
        or type(first) is not int
        or type(maximum) is not int
        or not 0 < maximum <= MAX_GITHUB_RESPONSE_BYTES
    ):
        raise ReviewInputError("receipt protocol fields are invalid")
    if protocol == "rest":
        if len(scope) != 0 or first != 100:
            raise ReviewInputError("receipt REST protocol changed")
    elif protocol == "graphql":
        if (
            len(scope) != 3
            or type(scope[0]) is not str
            or type(scope[1]) is not int
            or type(scope[2]) is not int
            or len(scope[0]) > 513
            or scope[0].count("/") != 1
            or scope[1] <= 0
            or scope[2] <= 0
            or not 2 <= first <= 100
        ):
            raise ReviewInputError("receipt GraphQL protocol changed")
    else:
        raise ReviewInputError("receipt protocol is unsupported")
    return protocol, scope, first, maximum


def _projection_binding(value):
    require_module_seal(sys.modules[__name__])
    if type(value) is not ReceiptPageProjection:
        raise ReviewInputError("receipt projection is unavailable")
    fields = vars(value)
    if (
        type(fields) is not dict
        or any(type(key) is not str for key in fields)
        or set(fields)
        != {"known", "future", "maximum_bytes", "protocol", "scope", "first"}
    ):
        raise ReviewInputError("receipt projection fields are invalid")
    _protocol_fields(value)
    if (
        type(value.known) is not tuple
        or type(value.future) is not tuple
        or any(
            type(pair) is not tuple
            or len(pair) != 2
            or any(type(part) is not int or part <= 0 for part in pair)
            for pair in value.known
        )
        or any(type(part) is not int or part <= 0 for part in value.future)
    ):
        raise ReviewInputError("receipt projection bounds changed")
    count = len(value.known) + len(value.future)
    body = sum(bound for _, bound in value.known) + sum(value.future)
    expected = (
        2 + body + count * (METADATA_BYTES + 1)
        if value.protocol == "rest"
        else 4096 + body + count * 1024
    )
    if (
        count >= 100
        or value.maximum_bytes != expected
        or (value.protocol == "graphql" and value.first != count + 1)
    ):
        raise ReviewInputError("receipt projection protocol bound changed")
    return (
        value.known,
        value.future,
        value.maximum_bytes,
        value.protocol,
        value.scope,
        value.first,
    )


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
    _projection_binding(projection)
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
        projection.protocol,
        projection.scope,
        projection.first,
    )


def read_receipt_page(
    http, *, path, token, membership, reconcile=False, ledger=None, identity=None
):
    """Perform exactly one fresh charged read, or refuse without a retry."""
    if type(membership) is not ReceiptMembership or type(reconcile) is not bool:
        raise ReviewInputError("receipt census input is invalid")
    binding = _membership_binding(membership)
    if not isinstance(path, str) or "?" in path or not path.endswith("/comments"):
        raise ReviewInputError("receipt census path is invalid")
    if membership.protocol == "graphql":
        rows = _read_graphql_receipt(http, path, token, membership, ledger, identity)
    elif (
        membership.protocol == "rest"
        and membership.scope == ()
        and membership.first == 100
    ):
        status, rows = http.request("GET", path + "?per_page=100&page=1", token=token)
        require_module_seal(sys.modules[__name__])
        if _membership_binding(membership) != binding:
            raise ReviewInputError("receipt membership changed during read")
        if status != 200 or not isinstance(rows, list) or len(rows) >= 100:
            raise ReviewInputError("receipt census lacks one complete page")
        if _wire(rows) > min(membership.maximum_bytes, MAX_GITHUB_RESPONSE_BYTES):
            raise ReviewInputError("receipt census exceeded complete wire bound")
    else:
        raise ReviewInputError("receipt census protocol changed")
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


def _graphql_request(scope, first):
    from . import quarantine_census

    require_module_seal(quarantine_census)
    owner, name = scope[0].split("/", 1)
    variables = {
        "owner": owner,
        "name": name,
        "number": scope[1],
        "first": first,
        "after": None,
    }
    body = {"query": quarantine_census.QUERY, "variables": variables}
    if not quarantine_census._variables(variables) or _wire(body) > 8192:
        raise ReviewInputError("receipt GraphQL query exceeds its source bounds")
    return body


def _read_graphql_receipt(http, path, token, membership, ledger, identity):
    from . import packed_comment_census, quarantine_census

    require_module_seal(packed_comment_census)
    require_module_seal(quarantine_census)
    if (
        ledger is None
        or _receipt_scope(identity) != membership.scope
        or type(membership.first) is not int
        or not 2 <= membership.first <= 100
        or path
        != http.repository_path(
            identity.repository, f"/issues/{identity.pull_request}/comments"
        )
    ):
        raise ReviewInputError("receipt GraphQL source scope changed")
    body = _graphql_request(membership.scope, membership.first)
    binding = _membership_binding(membership)
    status, response = http.request("POST", "/graphql", token=token, body=body)
    require_module_seal(sys.modules[__name__])
    require_module_seal(packed_comment_census)
    require_module_seal(quarantine_census)
    packed_comment_census._single_plain(response)
    if (
        _membership_binding(membership) != binding
        or _receipt_scope(identity) != membership.scope
    ):
        raise ReviewInputError("receipt GraphQL identity changed during read")
    try:
        if (
            status != 200
            or type(response) is not dict
            or response.get("errors")
            or set(response) - {"data", "errors", "extensions"}
            or _wire(response)
            > min(MAX_GITHUB_RESPONSE_BYTES, membership.maximum_bytes)
        ):
            raise ReviewInputError("receipt GraphQL complete response is unavailable")
        repository = response["data"]["repository"]
        quarantine_census._repository(repository, identity)
        pr = repository["pullRequest"]
        connection = pr["comments"]
        nodes, page = connection["nodes"], connection["pageInfo"]
        if (
            type(pr["number"]) is not int
            or pr["number"] != identity.pull_request
            or type(nodes) is not list
            or len(nodes) >= membership.first
            or type(connection["totalCount"]) is not int
            or connection["totalCount"] != len(nodes)
            or page["hasNextPage"] is not False
            or (nodes and not quarantine_census._cursor(page["endCursor"]))
            or _wire(response) - sum(_wire(node) for node in nodes) > 4096
        ):
            raise ReviewInputError("receipt GraphQL membership is incomplete")
        rows = []
        for node in nodes:
            if _wire({k: v for k, v in node.items() if k != "body"}) > 1024:
                raise ReviewInputError("receipt GraphQL metadata exceeds wire bound")
            rows.append(quarantine_census._comment(node, ledger, identity))
        return rows
    except ReviewInputError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise ReviewInputError("receipt GraphQL response is malformed") from error


def _membership_binding(value):
    # Every comparison consumer receives only this finite primitive snapshot.
    # Seal before resolving the class export or traversing mutable instances.
    require_module_seal(sys.modules[__name__])
    if type(value) is not ReceiptMembership:
        raise ReviewInputError("receipt phase membership is unavailable")
    fields = vars(value)
    required = {
        "bodies",
        "owners",
        "receipt_text",
        "receipt_update_id",
        "app_id",
        "maximum_bytes",
        "protocol",
        "scope",
        "first",
    }
    if (
        type(fields) is not dict
        or any(type(key) is not str for key in fields)
        or set(fields) != required
    ):
        raise ReviewInputError("receipt membership fields are invalid")
    _protocol_fields(value)
    bodies, owners = fields["bodies"], fields["owners"]
    if (
        type(bodies) is not tuple
        or type(owners) is not tuple
        or len(bodies) >= 100
        or len(owners) != len(bodies)
    ):
        raise ReviewInputError("receipt membership inventory is invalid")
    identifiers = set()
    for pair in bodies:
        if (
            type(pair) is not tuple
            or len(pair) != 2
            or type(pair[0]) is not int
            or pair[0] <= 0
            or type(pair[1]) is not str
            or len(pair[1]) != 64
            or any(char not in "0123456789abcdef" for char in pair[1])
        ):
            raise ReviewInputError("receipt membership body commitment is invalid")
        if pair[0] in identifiers:
            raise ReviewInputError("receipt membership body identity is duplicated")
        identifiers.add(pair[0])
    owner_ids = set()
    for pair in owners:
        if (
            type(pair) is not tuple
            or len(pair) != 2
            or type(pair[0]) is not int
            or pair[0] <= 0
            or type(pair[1]) is not tuple
            or len(pair[1]) != 3
        ):
            raise ReviewInputError("receipt membership owner commitment is invalid")
        identifier, kind, login = pair[1]
        if identifier is not None and (type(identifier) is not int or identifier <= 0):
            raise ReviewInputError("receipt membership owner ID is invalid")
        if any(
            part is not None and (type(part) is not str or len(part) > METADATA_BYTES)
            for part in (kind, login)
        ):
            raise ReviewInputError("receipt membership owner text is invalid")
        if pair[0] in owner_ids:
            raise ReviewInputError("receipt membership owner identity is duplicated")
        owner_ids.add(pair[0])
    text, update, app = (
        fields["receipt_text"],
        fields["receipt_update_id"],
        fields["app_id"],
    )
    if (
        owner_ids != identifiers
        or type(text) is not str
        or len(text) > MAX_GITHUB_RESPONSE_BYTES
        or _wire({"body": text}) > MAX_GITHUB_RESPONSE_BYTES
        or type(app) is not int
        or app <= 0
        or (
            update is not None
            and (type(update) is not int or update <= 0 or update not in identifiers)
        )
    ):
        raise ReviewInputError("receipt membership writer fields are invalid")
    return (
        bodies,
        owners,
        text,
        update,
        app,
        fields["maximum_bytes"],
        fields["protocol"],
        fields["scope"],
        fields["first"],
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
                ledger=run.ledger,
                identity=run.identity,
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
