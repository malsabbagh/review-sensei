"""Same-invocation evidence that an empty original needs no comment flags.

Only the original restore producer can record namespace validation. An ordinary
empty original additionally requires its accepted payload and exact selected root.
This receipt cannot make a historical namespace current, release obligations, or
replace any later quarantine, native, or checkpoint fence.
"""

from __future__ import annotations

import inspect
import sys
from contextvars import ContextVar
from datetime import datetime
from functools import wraps
from typing import Any

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import require_instance_source, require_module_seal


def _build():
    active: ContextVar[Any] = ContextVar("empty_original_admission", default=None)
    installed = set()

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

    def install_application(cls):
        frame = inspect.currentframe()
        defining_source(frame.f_back if frame is not None else None, cls.__module__)
        original = cls.publish_review

        @wraps(original)
        def invoke(owner, **kwargs):
            # Nested or custom invocations never inherit another receipt.
            entry = {"owner": owner, "receipt": None, "selected": None}
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
            if (
                entry is None
                or record is None
                or document is not None
                or record.transaction is None
            ):
                return record, document
            historical = (
                record.finding_lifecycle is not None
                and record.quarantine_lineage is not None
            )
            original_empty = (
                record.finding_lifecycle is None and record.quarantine_lineage is None
            )
            if not historical and not original_empty:
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
            )
            return record, document

        return restore

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
        old_ledger, saved_caller, old_record, raw, reference, digest = saved
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
        if old_record.finding_lifecycle is None:
            # This optimization is only the first original invocation, never a
            # cold retry with missing admission artifacts. Independently bind
            # the exact empty accepted payload, not an empty projected result.
            import json

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

    return install_application, install_restore, load_transaction, consume


install_application, install_restore, load_transaction, consume = _build()
del _build
