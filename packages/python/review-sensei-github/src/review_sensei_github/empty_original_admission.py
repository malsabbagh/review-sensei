"""Same-invocation evidence that an empty original needs no comment flags.

Only the original restore producer can record namespace validation. An ordinary
empty original additionally requires its accepted payload and exact selected root.
This receipt cannot make a historical namespace current, release obligations, or
replace any later quarantine, native, or checkpoint fence.
"""

from __future__ import annotations

import inspect
import json
import sys
from contextvars import ContextVar
from datetime import datetime
from functools import wraps
from typing import Any
from weakref import ref

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import require_instance_source, require_module_seal


def _build():
    active: ContextVar[Any] = ContextVar("empty_original_admission", default=None)
    installed = set()
    cold_reads: dict[int, Any] = {}
    recovering: ContextVar[Any] = ContextVar("empty_original_recovery", default=None)
    reading: ContextVar[Any] = ContextVar("empty_original_reader", default=None)

    def defining_source(frame, name):
        module = sys.modules.get(name)
        spec = getattr(module, "__spec__", None)
        get_code = getattr(getattr(spec, "loader", None), "get_code", None)
        if (
            frame is None
            or module is None
            or frame.f_globals is not vars(module)
            or frame.f_locals is not frame.f_globals
            or not getattr(spec, "_initializing", False)
            or not callable(get_code)
            or frame.f_code != get_code(name)
            or name in installed
        ):
            raise ReviewInputError("empty original source requires defining installer")
        installed.add(name)

    def install_reader(cls):
        frame = inspect.currentframe()
        defining_source(frame.f_back if frame is not None else None, cls.__module__)
        from . import accepted_result, publication_recovery, session_ledger

        require_module_seal(session_ledger)
        hosted_ledger = session_ledger.GitHubIssueCommentSessionLedger

        module = sys.modules[cls.__module__]
        decode, source = cls._decode, cls._source
        loaders = tuple(
            (
                item,
                item.__spec__,
                getattr(item.__spec__, "loader", None),
                getattr(
                    getattr(getattr(item.__spec__, "loader", None), "get_code", None),
                    "__func__",
                    None,
                ),
            )
            for item in (module, accepted_result, publication_recovery)
        )
        loader_details = tuple(
            (
                getattr(get_code, "__code__", None),
                repr(getattr(get_code, "__defaults__", None)),
                repr(getattr(get_code, "__kwdefaults__", None)),
                getattr(loader, "name", None),
                getattr(loader, "path", None),
            )
            for _item, _spec, loader, get_code in loaders
        )
        carriers_reader = cls.read_available_with_carriers
        source_reader = accepted_result.read_accepted_result

        def intact():
            for (item, spec, loader, get_code), details in zip(
                loaders, loader_details, strict=True
            ):
                if (
                    (
                        getattr(get_code, "__code__", None),
                        repr(getattr(get_code, "__defaults__", None)),
                        repr(getattr(get_code, "__kwdefaults__", None)),
                        getattr(loader, "name", None),
                        getattr(loader, "path", None),
                    )
                    != details
                    or item.__spec__ is not spec
                    or getattr(spec, "loader", None) is not loader
                    or getattr(getattr(loader, "get_code", None), "__func__", None)
                    is not get_code
                ):
                    raise ReviewInputError("empty original reader loader changed")
            require_module_seal(module)
            require_module_seal(accepted_result)
            require_module_seal(publication_recovery)
            if module.read_accepted_result is not source_reader:
                raise ReviewInputError("empty original accepted reader changed")

        @wraps(source)
        def read(owner: Any, reference, store=None):
            payload = source(owner, reference, store)
            current = reading.get()
            frame = inspect.currentframe()
            parent = frame.f_back if frame is not None else None
            # The original decoder's loader lambda is the only source call
            # which can contribute to this invocation's authenticated result.
            if current is not None and current[0] is owner and parent is not None:
                parent = parent.f_back
                if parent is not None:
                    parent = parent.f_back
                if (
                    parent is not None
                    and parent.f_code is decode.__code__
                    and id(parent.f_locals.get("self")) == id(owner)
                ):
                    try:
                        intact()
                    except ReviewInputError:
                        return payload
                    current[1] = (
                        canonical_bytes(reference),
                        payload,
                        owner._raw,
                        store,
                    )
            return payload

        @wraps(decode)
        def decoded(owner: Any, payload, store=None):
            current = [owner, None]
            token = reading.set(current)
            try:
                result = decode(owner, payload, store)
            finally:
                reading.reset(token)
            raw = current[1] is None and not owner.last_read_was_projection
            if current[1] is None and not raw:
                return result
            frame = inspect.currentframe()
            candidate = frame.f_back if frame is not None else None
            if (
                candidate is None
                or candidate.f_code is not carriers_reader.__code__
                or candidate.f_globals is not vars(module)
                or id(candidate.f_locals.get("self")) != id(owner)
                or candidate.f_locals.get("snapshot") is not store
            ):
                return result
            carriers = candidate.f_locals.get("available")
            if (
                not isinstance(carriers, tuple)
                or len(carriers) != 3
                or carriers[1] is not payload
            ):
                return result
            recovery = inspect.unwrap(
                publication_recovery.recover_retained_review_result
            )
            for _ in range(8):
                if candidate is None or candidate.f_code is recovery.__code__:
                    break
                candidate = candidate.f_back
            if (
                candidate is None
                or candidate.f_code is not recovery.__code__
                or candidate.f_globals is not vars(publication_recovery)
                or id(candidate.f_locals.get("store")) != id(owner)
            ):
                return result
            loaded: Any = candidate.f_locals.get("loaded")
            ledger: Any = candidate.f_locals.get("ledger")
            if (
                loaded is None
                or loaded.record is None
                or loaded.record.transaction is not owner.transaction
            ):
                return result
            # This observer grants only original hosted caller/root custody.
            # A successful local/custom decode must not depend on unrelated
            # hosted metadata, even when it exposes lookalike instance fields.
            if type(ledger) is not hosted_ledger:
                return result
            try:
                intact()
                from review_sensei import models

                require_module_seal(session_ledger)
                require_module_seal(models)
                if raw:
                    original_result = models.ReviewResult.from_dict(json.loads(payload))
                    if (
                        canonical_bytes(original_result.to_dict()) != payload
                        or original_result.content_digest()
                        != owner.transaction.result_sha256
                        or original_result.transaction is None
                        or not original_result.transaction.logical_identity_matches(
                            owner.transaction
                        )
                    ):
                        return result
                    # No compact fallback: this is the complete original raw
                    # representation, read under the original selected event.
                    current[1] = (None, payload, owner._raw, store)
                require_instance_source(
                    ledger, methods=("load", "read_evidence", "evidence_producer")
                )
                transport = publication_recovery._publication_transport(owner.http)
                if (
                    publication_recovery._publication_transport(ledger.http)
                    is not transport
                ):
                    return result
                source_caller = getattr(ledger, "caller", None)
                if (
                    source_caller is None
                    or source_caller._checkpoint is not None
                    or source_caller._root != loaded.record.record_sha256
                ):
                    return result
                source_caller._require_ledger(ledger, loaded.record.identity())
                recovery_entry = recovering.get()
                if recovery_entry is None:
                    return result
                recovery_entry["receipt"] = (
                    ledger,
                    source_caller,
                    str(source_caller.run.run_id),
                    str(source_caller.attempt),
                    owner,
                    owner._raw,
                    owner.transaction,
                    canonical_bytes(owner.transaction.to_dict()),
                    current[1],
                    result,
                    loaded.record,
                    canonical_bytes(loaded.record.to_dict()),
                    canonical_bytes(loaded.record.finding_lifecycle),
                    tuple(carriers[2]),
                    owner.http,
                    owner.identity,
                    owner.app_id,
                    intact,
                )
            except (ReviewInputError, ValueError):
                pass
            return result

        cls._source, cls._decode = read, decoded

    def install_recovery(original):
        frame = inspect.currentframe()
        defining_source(
            frame.f_back if frame is not None else None, original.__module__
        )

        @wraps(original)
        def recover(**kwargs):
            # A new attempt, including one that raises, invalidates the prior
            # return for this exact ledger. Failed recovery cannot keep a grant.
            for key, pending in tuple(cold_reads.items()):
                if pending[2][0] is kwargs.get("ledger"):
                    cold_reads.pop(key, None)
            entry = {"receipt": None}
            token = recovering.set(entry)
            try:
                result = original(**kwargs)
                if entry["receipt"] is not None:
                    key = id(result)
                    cold_reads[key] = (
                        ref(result, lambda _ref: cold_reads.pop(key, None)),
                        canonical_bytes(result.to_dict()),
                        entry["receipt"],
                    )
                return result
            finally:
                entry.clear()
                recovering.reset(token)

        return recover

    def install_application(cls):
        frame = inspect.currentframe()
        defining_source(frame.f_back if frame is not None else None, cls.__module__)
        original = cls.publish_review

        @wraps(original)
        def invoke(owner, **kwargs):
            # Nested or custom invocations never inherit another receipt.
            result = kwargs.get("result")
            recovered = cold_reads.pop(id(result), None)
            # A failed/copy invocation must not leave its original sibling
            # return available for a later publication on the same ledger.
            for key, pending in tuple(cold_reads.items()):
                if pending[2][0] is getattr(owner, "session_ledger", None):
                    cold_reads.pop(key, None)
            entry = {
                "owner": owner,
                "receipt": None,
                "selected": None,
                "cold": recovered[2]
                if recovered is not None
                and recovered[0]() is result
                and canonical_bytes(result.to_dict()) == recovered[1]
                else None,
            }
            token = active.set(entry)
            try:
                return original(owner, **kwargs)
            finally:
                entry.clear()
                active.reset(token)

        cls.publish_review = invoke

    def install_restore(original):
        frame = inspect.currentframe()
        defining_source(
            frame.f_back if frame is not None else None, original.__module__
        )

        @wraps(original)
        def restore(reviewer, ledger, identity, **kwargs):
            record, document = original(reviewer, ledger, identity, **kwargs)
            entry = active.get()
            if entry is None or record is None or record.transaction is None:
                return record, document
            restored_empty = False
            if document is not None:
                restored_empty = ordinary_empty_document(
                    document, record, identity, ledger
                )
                if not restored_empty:
                    return record, document
            historical = (
                record.finding_lifecycle is not None
                and record.quarantine_lineage is not None
            )
            original_empty = (
                record.finding_lifecycle is None and record.quarantine_lineage is None
            )
            if not historical and not original_empty and not restored_empty:
                return record, document
            # The actual restore has either validated inherited historical
            # authority or authenticated that this root has no lifecycle or
            # quarantine namespace. Only a separately selected empty original
            # may omit per-comment flags; native/history gates still run.
            # Mixed or caller-supplied namespaces cannot issue this receipt.
            caller = getattr(ledger, "caller", None)
            if caller is None or caller._checkpoint is not None:
                return record, document
            try:
                require_module_seal(sys.modules[original.__module__])
                require_instance_source(
                    ledger, methods=("load", "read_evidence", "evidence_producer")
                )
                caller._require_ledger(ledger, identity)
            except ReviewInputError:
                return record, document
            if caller._root != record.record_sha256:
                return record, document
            entry["receipt"] = (
                ledger,
                caller,
                record,
                canonical_bytes(record.to_dict()),
                canonical_bytes(record.finding_lifecycle),
                record.transaction.result_sha256,
                (document, canonical_bytes(document)) if restored_empty else None,
            )
            return record, document

        return restore

    def ordinary_empty_document(document, record, identity, ledger):
        from review_sensei.active_finding_window import ActiveWindowRoundState
        from review_sensei.finding_drift import DriftEvaluationResult
        from review_sensei.finding_lifecycle import decode_lifecycle_state

        if (
            record.quarantine_lineage is not None
            or record.finding_lifecycle is None
            or document["unknown_trust_effect"]
            or document["native_receipts"]
        ):
            return False
        from review_sensei import (
            active_finding_window,
            finding_drift,
            finding_lifecycle,
        )

        for source in (active_finding_window, finding_drift, finding_lifecycle):
            require_module_seal(source)
        state = decode_lifecycle_state(document["lifecycle"]).state
        inventory: Any
        if type(state) is ActiveWindowRoundState:
            inventory = state.authoritative_decision_inventory
        elif type(state) is DriftEvaluationResult:
            inventory = state.inventory
        else:
            return False
        if inventory is None or inventory.obligations:
            return False
        from . import checks

        require_module_seal(checks)
        check = checks.ReviewCheckPublisher(http=ledger.http)
        expected = check._mutation_body(
            repository=identity.repository,
            head_sha=record.transaction.head_sha,
            create=True,
            **check._state_fields(),
        )
        if expected["external_id"] != checks.REVIEW_CHECK_EXTERNAL_ID:
            return False
        effects = document["effects"]
        if not effects:
            # The authenticated ordinary journal stopped before any native
            # business intent. This grants no settlement: the consumer still
            # needs the exact complete original recovery and selected root.
            return True
        # Only the original initial check. A rejected effect, review intent,
        # update, or unknown is not evidence of an empty original admission.
        if len(effects) != 1:
            return False
        effect = effects[0]
        response = effect["response"]
        return (
            effect["method"] == "POST"
            and effect["path"] == f"/repos/{identity.repository}/check-runs"
            and effect["status"] == "confirmed"
            and effect["request"] == expected
            and response is not None
            and response[0] in {200, 201}
            and type(response[1]) is dict
            and set(response[1]) == {"id", "producer_id", "app_id", "mutation"}
            and response[1]["producer_id"] is None
            and response[1]["mutation"] is None
            and type(response[1].get("id")) is int
            and response[1]["id"] > 0
            and response[1].get("app_id") == ledger.authenticated_app_id
            and type(ledger.authenticated_app_id) is int
        )

    def load_transaction(ledger, identity, result, **kwargs):
        from review_sensei import session

        # Execute the actual validating producer; no supplied callback or return
        # value can register a selection. Outside the invocation this remains
        # the same core operation but cannot retain an omission receipt.
        try:
            require_module_seal(session)
            qualified = True
        except ReviewInputError:
            qualified = False
        record = session.load_review_transaction_for_publication(
            ledger, identity, result, **kwargs
        )
        entry = active.get()
        if entry is not None:
            entry["selected"] = None
            if not qualified:
                return record
            caller = getattr(ledger, "caller", None)
            try:
                require_instance_source(ledger, methods=("load", "replace"))
                if caller is not None:
                    caller._require_ledger(ledger, identity)
            except ReviewInputError:
                return record
            if (
                caller is not None
                and caller._checkpoint is None
                and caller._effect_record is record
                and caller._root == record.record_sha256
            ):
                entry["selected"] = (
                    ledger,
                    caller,
                    record,
                    canonical_bytes(record.to_dict()),
                    result.content_digest(),
                )
        return record

    def retained_after_owned_intent(ledger, caller, old_record, raw, record):
        if canonical_bytes(old_record.to_dict()) != raw:
            return False
        if canonical_bytes(record.to_dict()) == raw:
            return True
        from review_sensei import session

        from .quarantine_checkpoint import confirmed_checkpoint_capacity

        try:
            require_module_seal(session)
            receipt = confirmed_checkpoint_capacity(caller, ledger, record.identity())
            if (
                receipt.record is not record
                or old_record.transaction is None
                or old_record.transaction.phase != "publication_pending"
                or record.transaction is None
                or record.transaction.phase != "publication_failed"
            ):
                return False
            # Reproduce the original business transition and the source caller's
            # accounting attachment. evolve independently checks accounting's
            # monotone successor; full canonical comparison permits no other
            # changed field, reference, lineage, decision, or effect.
            now = datetime.fromisoformat(record.updated_at.replace("Z", "+00:00"))
            expected = session._publication_state_mutation(
                old_record.transaction, published=False, now=now
            )(old_record).evolve(api_run_accounting=record.api_run_accounting, now=now)
            return canonical_bytes(expected.to_dict()) == canonical_bytes(
                record.to_dict()
            )
        except (ReviewInputError, ValueError):
            return False

    def plain_after_selected_intent(old_record, raw, record):
        """Validate only the original pending-to-failed business transition.

        The consumer separately requires the actual load producer's exact
        current record to be the caller's owned root. No cached root or generic
        receipt can substitute for that independently selected current state.
        """
        from review_sensei import session

        from . import accepted_result

        if (
            old_record.finding_lifecycle is not None
            or old_record.quarantine_lineage is not None
            or old_record.transaction is None
            or old_record.transaction.phase != "publication_pending"
            or record.transaction is None
            or record.transaction.phase != "publication_failed"
            or canonical_bytes(old_record.to_dict()) != raw
        ):
            return False
        try:
            require_module_seal(session)
            require_module_seal(accepted_result)
            now = datetime.fromisoformat(record.updated_at.replace("Z", "+00:00"))
            expected = session._publication_state_mutation(
                old_record.transaction, published=False, now=now
            )(old_record).evolve(api_run_accounting=record.api_run_accounting, now=now)
            return canonical_bytes(expected.to_dict()) == canonical_bytes(
                record.to_dict()
            )
        except (ReviewInputError, ValueError):
            return False

    def consume(owner, ledger, record, result):
        entry = active.get()
        if entry is None or entry.get("owner") is not owner:
            return False
        saved = entry.pop("receipt", None)
        entry["receipt"] = None
        if saved is None or result.comments or record is None:
            return False
        if saved[2].finding_lifecycle is None or saved[6] is not None:
            # Validate the defining parser and digest producers before the
            # first authority comparison, not only immediately before parsing.
            from review_sensei import models

            try:
                require_module_seal(models)
                require_instance_source(result, methods=("content_digest", "to_dict"))
            except ReviewInputError:
                return False
        selection = entry.get("selected")
        if (
            selection is None
            or selection[0] is not ledger
            or selection[2] is not record
        ):
            return False
        if (
            selection[3] != canonical_bytes(record.to_dict())
            or selection[4] != result.content_digest()
        ):
            return False
        old_ledger, saved_caller, old_record, raw, reference, digest, ordinary = saved
        caller: Any = saved_caller
        ledger_caller: Any = getattr(ledger, "caller", None)
        if caller is None or ledger_caller is None:
            return False
        if (
            ledger is not old_ledger
            or selection[1] is not caller
            or ledger_caller is not caller
            or caller._checkpoint is not None
            or caller._root != record.record_sha256
            or caller._effect_record is not record
            or not (
                retained_after_owned_intent(ledger, caller, old_record, raw, record)
                or plain_after_selected_intent(old_record, raw, record)
            )
            or canonical_bytes(record.finding_lifecycle) != reference
            or record.transaction is None
            or result.transaction is None
            or result.transaction.to_dict() != record.transaction.to_dict()
            or result.content_digest() != digest
            or result.content_digest() != record.transaction.result_sha256
        ):
            return False
        if ordinary is not None:
            from review_sensei.models import ReviewResult

            try:
                source = entry.pop("cold", None)
                if source is None:
                    return False
                (
                    source_ledger,
                    source_caller,
                    source_run,
                    source_attempt,
                    reader,
                    raw_store,
                    transaction,
                    transaction_bytes,
                    authenticated,
                    decoded,
                    source_record,
                    source_record_bytes,
                    source_lifecycle,
                    carriers,
                    source_http,
                    source_identity,
                    source_app_id,
                    intact,
                ) = source
                intact()
                if (
                    source_ledger is not ledger
                    or source_caller is not caller
                    or source_run != str(caller.run.run_id)
                    or source_attempt != str(caller.attempt)
                    or not retained_after_owned_intent(
                        ledger, caller, source_record, source_record_bytes, record
                    )
                    or canonical_bytes(ordinary[0]) != ordinary[1]
                    or source_lifecycle != reference
                    or not carriers
                    or reader.http is not source_http
                    or reader.identity is not source_identity
                    or reader.app_id != source_app_id
                    or source_app_id != ledger.authenticated_app_id
                    or reader._raw is not raw_store
                    or reader.transaction is not transaction
                    or canonical_bytes(transaction.to_dict()) != transaction_bytes
                ):
                    return False
                accepted = ReviewResult.from_dict(json.loads(authenticated[1]))
                selected = ReviewResult.from_dict(json.loads(decoded))
                if (
                    accepted.comments
                    or selected.comments
                    or accepted.content_digest() != result.content_digest()
                    or selected.content_digest() != result.content_digest()
                    or not transaction.logical_identity_matches(record.transaction)
                ):
                    return False
                from .accepted_result import _reference

                if authenticated[0] is not None:
                    _reference(
                        json.loads(authenticated[0]),
                        record.transaction,
                        app_id=ledger.authenticated_app_id,
                    )
            except (ReviewInputError, ValueError):
                return False
        if old_record.finding_lifecycle is None:
            # This optimization is only the first original invocation, never a
            # cold retry with missing admission artifacts. Independently bind
            # the exact empty accepted payload, not an empty projected result.
            from review_sensei.models import ReviewResult

            from . import accepted_result

            try:
                require_module_seal(accepted_result)
                source = accepted_result.selected_accepted_result(
                    ledger.http,
                    record.transaction,
                    app_id=ledger.authenticated_app_id,
                )
                accepted = ReviewResult.from_dict(json.loads(source.payload))
            except (ReviewInputError, ValueError):
                return False
            reference = source.reference
            if (
                old_record.transaction is None
                or old_record.transaction.phase != "publication_pending"
                or not old_record.transaction.logical_identity_matches(
                    record.transaction
                )
                or accepted.comments
                or accepted.content_digest() != result.content_digest()
                or reference["run_id"] != str(caller.run.run_id)
                or reference["run_attempt"] != str(caller.attempt)
            ):
                return False
        from .checkpoint_result_store import uses_grouped_publication

        if uses_grouped_publication(ledger.http, result):
            return False
        try:
            require_instance_source(owner, methods=("publish_review",))
            require_instance_source(
                ledger, methods=("load", "read_evidence", "evidence_producer")
            )
            caller._require_ledger(ledger, record.identity())
        except ReviewInputError:
            return False
        return True

    return (
        install_application,
        install_restore,
        load_transaction,
        consume,
        install_reader,
        install_recovery,
    )


(
    install_application,
    install_restore,
    load_transaction,
    consume,
    install_reader,
    install_recovery,
) = _build()
del _build


from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
