"""Source-issued identity of an isolated native target, never origin evidence."""

import inspect
import sys
import weakref
from typing import Any, cast

from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import require_module_seal


def _raw_fields(value):
    fields = object.__getattribute__(value, "__dict__")
    if type(fields) is not dict or any(type(key) is not str for key in fields):
        raise ReviewInputError("isolated predecessor model fields changed")
    return fields


def _identity_snapshot(identity):
    from review_sensei import session

    require_module_seal(session)
    if type(identity) is not session.SessionIdentity:
        raise ReviewInputError("isolated predecessor identity type changed")
    fields = _raw_fields(identity)
    if (
        set(fields) != {"repository", "pull_request", "repository_id"}
        or type(fields.get("repository")) is not str
        or type(fields.get("pull_request")) is not int
        or (
            fields.get("repository_id") is not None
            and type(fields["repository_id"]) is not int
        )
    ):
        raise ReviewInputError("isolated predecessor identity fields changed")
    return fields["repository"], fields["pull_request"], fields["repository_id"]


def _plain_graph(value, ancestors=()):
    kind = type(value)
    if value is None or kind is str or kind is int or kind is bool:
        return
    if id(value) in ancestors:
        raise ReviewInputError("isolated predecessor model graph is cyclic")
    if kind is dict:
        if any(type(key) is not str for key in value):
            raise ReviewInputError("isolated predecessor model graph keys changed")
        children = value.values()
    elif kind is list or kind is tuple:
        children = value
    else:
        raise ReviewInputError("isolated predecessor model graph changed")
    for child in children:
        _plain_graph(child, (*ancestors, id(value)))


def validate_models(authenticated, identity, *records):
    """Reject supplied callbacks before any model property/equality/serializer."""
    from review_sensei import session
    from review_sensei.source_registry import require_instance_source

    from . import quarantine_origin

    require_module_seal(session)
    require_module_seal(quarantine_origin)
    identity_snapshot = _identity_snapshot(identity)
    if type(authenticated) is not quarantine_origin.AuthenticatedQuarantine:
        raise ReviewInputError("isolated predecessor authenticated type changed")
    fields = _raw_fields(authenticated)
    names = (
        "lineage_bytes",
        "archive_bytes",
        "inventories_bytes",
        "state_bytes",
        "source_documents_bytes",
    )
    if type(fields.get("record_sha256")) is not str or any(
        type(fields.get(name)) is not bytes for name in names
    ):
        raise ReviewInputError("isolated predecessor authenticated fields changed")
    _identity_snapshot(fields.get("original_identity"))
    for record in records:
        if type(record) is not session.SessionRecord:
            raise ReviewInputError("isolated predecessor record type changed")
        raw = _raw_fields(record)
        if type(raw.get("record_sha256")) is not str:
            raise ReviewInputError("isolated predecessor record digest changed")
        _plain_graph(raw.get("quarantine_lineage"))
        require_instance_source(record, methods=("to_dict",))
    return identity_snapshot, (
        fields["record_sha256"],
        *(fields[name] for name in names),
    )


def _build():
    issued: weakref.WeakKeyDictionary[Any, Any] = weakref.WeakKeyDictionary()

    def capture(ledger, identity, selected, found):
        from . import quarantine_origin, quarantine_selection

        require_module_seal(quarantine_selection)
        require_module_seal(quarantine_origin)
        frame = sys._getframe(1)
        if (
            frame.f_code is not quarantine_selection.select_quarantine.__code__
            or frame.f_globals is not vars(quarantine_selection)
            or any(
                frame.f_locals.get(k) is not v
                for k, v in (
                    ("ledger", ledger),
                    ("identity", identity),
                    ("selected", selected),
                    ("found", found),
                )
            )
        ):
            raise ReviewInputError("isolated predecessor requires original selection")
        issued.pop(ledger, None)
        from . import session_ledger

        require_module_seal(session_ledger)
        parent = frame.f_back
        if (
            parent is None
            or parent.f_code
            is not inspect.unwrap(
                session_ledger.GitHubIssueCommentSessionLedger._discover
            ).__code__
            or parent.f_globals is not vars(session_ledger)
            or parent.f_locals.get("self") is not ledger
            or parent.f_locals.get("identity") is not identity
            or parent.f_locals.get("found") is not found
            or parent.f_locals.get("items") is not frame.f_locals["items"]
        ):
            return  # Direct selection/creation cannot issue read custody.
        from review_sensei.source_registry import require_instance_source

        require_instance_source(frame.f_locals["reader"], methods=("read",))
        ledger = cast(Any, ledger)
        authenticated = ledger._quarantine_authenticated
        caller = getattr(ledger, "caller", None) or getattr(
            ledger, "_startup_caller", None
        )
        identity_snapshot, authenticated_snapshot = validate_models(
            authenticated, identity, selected
        )
        archive = authenticated.archive
        isolation = archive["target_isolation"]
        lineage = authenticated.lineage
        # Other contracts retain exact frozen predecessor matching. This receipt
        # was authenticated by the original reader's executor/isolation census.
        if isolation.get("schema_version") != "quarantine-published-tail-isolation-v1":
            return
        if caller is None:
            return  # Model readers cannot issue hosted native-target custody.
        quarantine_origin.require_authenticated_read_owner(
            authenticated, ledger, caller, identity
        )
        if _raw_fields(authenticated)["original_identity"] is not identity:
            raise ReviewInputError("isolated predecessor original identity changed")
        if type(found) is not list:
            raise ReviewInputError("isolated predecessor root selection changed")
        for row in found:
            if type(row) is not tuple or len(row) != 2 or type(row[0]) is not int:
                raise ReviewInputError("isolated predecessor root selection changed")
            validate_models(authenticated, identity, row[1])
        predecessor = quarantine_origin.read_predecessor(archive)
        validate_models(authenticated, identity, predecessor)
        old_id = lineage["predecessor_comment_id"]
        selected_id = frame.f_locals["selected_id"]
        if (
            isolation.get("disposition") != "unresolved-isolated"
            or isolation.get("predecessor_comment_id") != old_id
            or isolation.get("predecessor_record_sha256") != predecessor.record_sha256
            or lineage["predecessor_record_sha256"] != predecessor.record_sha256
            or authenticated.record_sha256 != selected.record_sha256
            or len(found) != 2
            or {key for key, _ in found} != {old_id, selected_id}
            or old_id == selected_id
        ):
            raise ReviewInputError("isolated predecessor selection conflicts")
        old = next(value for key, value in found if key == old_id)
        validate_models(authenticated, identity, old)
        issued[ledger] = (
            weakref.ref(caller),
            identity_snapshot,
            weakref.ref(authenticated),
            authenticated_snapshot,
            old_id,
            selected_id,
            old.record_sha256,
            weakref.ref(identity),
        )

    def selected_digest(
        ledger: Any, identity: Any, authenticated: Any, predecessor: Any
    ):
        from . import quarantine_census, quarantine_origin

        require_module_seal(quarantine_census)
        require_module_seal(quarantine_origin)
        frame = sys._getframe(1)
        if (
            frame.f_code
            is not inspect.unwrap(quarantine_census.lifecycle_capacity_plan).__code__
            or frame.f_globals is not vars(quarantine_census)
            or frame.f_locals.get("ledger") is not ledger
            or frame.f_locals.get("authenticated") is not authenticated
            or frame.f_locals.get("predecessor") is not predecessor
        ):
            raise ReviewInputError("isolated predecessor requires original capacity")
        identity_snapshot, authenticated_snapshot = validate_models(
            authenticated, identity, predecessor
        )
        ledger = cast(Any, ledger)
        predecessor = cast(Any, predecessor)
        entry = issued.get(ledger)
        if entry is None:
            return predecessor.record_sha256, None
        caller = ledger.caller
        original_identity = entry[7]()
        if (
            original_identity is None
            or _raw_fields(authenticated)["original_identity"] is not original_identity
            or _identity_snapshot(original_identity) != entry[1]
        ):
            raise ReviewInputError("isolated predecessor original identity changed")
        quarantine_origin.require_authenticated_read_owner(
            authenticated, ledger, caller, identity
        )
        if (
            entry[0]() is not caller
            or entry[1] != identity_snapshot
            or entry[2]() is not authenticated
            or entry[3] != authenticated_snapshot
            or entry[4] != authenticated.lineage["predecessor_comment_id"]
            or predecessor.record_sha256
            != authenticated.lineage["predecessor_record_sha256"]
        ):
            raise ReviewInputError("isolated predecessor selection custody changed")
        # The caller separately enforces exact two-root membership, Bot owner,
        # selected current root/confirmed checkpoint and maximum root wire size.
        # This digest is only the observed old native target, not a new archive.
        return entry[6], entry[5]

    return capture, selected_digest


capture, selected_digest = _build()
del _build

from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
