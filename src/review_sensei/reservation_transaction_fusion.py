"""Atomic original reservation and analysis attachment on a run-owned ledger.

This changes no admission decision or write authority. Only the defining core
producer and native reservation mutator can share a short-lived attachment.
Custom ledgers retain the original separate reservation and attachment calls.
"""

from __future__ import annotations

import inspect
import sys
from contextvars import ContextVar
from types import CodeType
from typing import TYPE_CHECKING, Any

from .bounded_evidence import canonical_bytes
from .errors import ReviewInputError
from .source_registry import require_instance_source, require_module_seal

if TYPE_CHECKING:
    from .convergence import ReviewConvergencePolicy
    from .session import SessionIdentity, SessionRecord


def _build():
    active: ContextVar[Any] = ContextVar("reservation_transaction_fusion", default=None)
    sources: dict[str, Any] = {}

    def install(namespace):
        frame = inspect.currentframe()
        frame = frame.f_back if frame else None
        name = namespace.get("__name__")
        module = sys.modules.get(name)
        spec = getattr(module, "__spec__", None)
        loader = getattr(spec, "loader", None)
        get_code = getattr(loader, "get_code", None)
        if (
            name not in {"review_sensei.session", "review_sensei_github.session_ledger"}
            or name in sources
            or module is None
            or namespace is not vars(module)
            or frame is None
            or frame.f_globals is not namespace
            or frame.f_locals is not namespace
            or not getattr(spec, "_initializing", False)
            or not callable(get_code)
            or frame.f_code != get_code(name)
        ):
            raise ReviewInputError("reservation fusion requires defining source")
        if name == "review_sensei.session":
            sources[name] = (
                namespace["prepare_review_transaction"],
                namespace["prepare_session_round"],
            )
        else:
            cls = namespace["GitHubIssueCommentSessionLedger"]
            reserve = cls.reserve
            codes = tuple(
                code
                for code in reserve.__code__.co_consts
                if isinstance(code, CodeType) and code.co_name == "mutate"
            )
            if len(codes) != 1:
                raise ReviewInputError("reservation fusion native mutation changed")
            sources[name] = (cls, reserve, cls._own_reservation, codes[0])

    def reserve_transaction(
        ledger: Any,
        identity: SessionIdentity,
        policy: ReviewConvergencePolicy,
        *,
        binding: dict[str, str],
        **kwargs: Any,
    ):
        core = sources.get("review_sensei.session")
        frame = inspect.currentframe()
        frame = frame.f_back if frame else None
        if (
            core is None
            or frame is None
            or frame.f_code is not core[0].__code__
            or frame.f_globals is not core[0].__globals__
        ):
            raise ReviewInputError("reservation fusion requires original preparation")
        supplied = frame.f_locals
        if (
            supplied.get("ledger") is not ledger
            or supplied.get("identity") is not identity
            or supplied.get("policy") is not policy
            or set(binding)
            != {
                "base_sha",
                "head_sha",
                "policy_digest",
                "configuration_digest",
                "evidence_digest",
            }
            or any(
                binding[key] != supplied.get(key)
                for key in (
                    "base_sha",
                    "head_sha",
                    "configuration_digest",
                    "evidence_digest",
                )
            )
            or kwargs
            != {
                "reservation_id": supplied["reservation_id"],
                "now": supplied["now"],
                "head_sha": supplied["head_sha"],
                "rescan_request": supplied["rescan_request"],
                **supplied["state_flags"],
            }
        ):
            raise ReviewInputError("reservation fusion producer binding changed")
        native = sources.get("review_sensei_github.session_ledger")
        caller = getattr(ledger, "caller", None)
        qualified = (
            native is not None
            and isinstance(ledger, native[0])
            and getattr(ledger.reserve, "__func__", None) is native[1]
            and getattr(ledger._own_reservation, "__func__", None) is native[2]
            and caller is not None
        )
        if qualified:
            assert caller is not None
            try:
                require_module_seal(sys.modules["review_sensei.session"])
                require_module_seal(sys.modules["review_sensei.models"])
                require_instance_source(policy, methods=("digest",))
                if binding["policy_digest"] != policy.digest():
                    raise ReviewInputError("reservation fusion policy changed")
                require_instance_source(
                    ledger, methods=("reserve", "replace", "_own_reservation")
                )
                require_instance_source(caller, methods=("_require_ledger",))
                caller._require_ledger(ledger, identity)
            except ReviewInputError:
                qualified = False
        if not qualified:
            return core[1](ledger, identity, policy, **kwargs), False
        assert caller is not None
        if active.get() is not None:
            raise ReviewInputError("reservation fusion cannot nest")
        entry: dict[str, Any] = {
            "ledger": ledger,
            "identity": identity,
            "binding": dict(binding),
            "now": kwargs.get("now"),
            "attached": None,
        }
        token = active.set(entry)
        try:
            prepared = core[1](ledger, identity, policy, **kwargs)
            require_module_seal(sys.modules["review_sensei.session"])
            require_module_seal(sys.modules["review_sensei.models"])
            require_module_seal(sys.modules["review_sensei_github.session_ledger"])
            attached: Any = entry["attached"]
            if attached is None:
                if prepared.reservation_id is not None:
                    raise ReviewInputError("reservation fusion lost its mutation")
                return prepared, False
            record = prepared.record
            if (
                prepared.reservation_id != attached.reservation_id
                or record.transaction != attached
                or record is not caller._effect_record
                or record.record_sha256 != caller._root
                or caller._checkpoint is not None
                or caller._checkpoint_operation is not None
            ):
                raise ReviewInputError("reservation fusion remains unconfirmed")
            return prepared, True
        finally:
            entry.clear()
            active.reset(token)

    def attach_reserved(
        ledger: Any,
        identity: SessionIdentity,
        before: SessionRecord,
        reserved: SessionRecord,
    ):
        entry = active.get()
        if entry is None:
            return reserved
        native = sources.get("review_sensei_github.session_ledger")
        frame = inspect.currentframe()
        frame = frame.f_back if frame else None
        if (
            native is None
            or frame is None
            or frame.f_code is not native[3]
            or frame.f_globals is not native[1].__globals__
            or frame.f_locals.get("self") is not ledger
            or frame.f_locals.get("record") is not before
            or frame.f_locals.get("reserved") is not reserved
            or frame.f_locals.get("identity") is not identity
            or entry["ledger"] is not ledger
            or entry["identity"] != identity
            or entry["attached"] is not None
        ):
            raise ReviewInputError("reservation fusion native source changed")
        reserve_frame = frame.f_back
        while (
            reserve_frame is not None and reserve_frame.f_code is not native[1].__code__
        ):
            reserve_frame = reserve_frame.f_back
        producer = sources["review_sensei.session"][1]
        if (
            reserve_frame is None
            or reserve_frame.f_locals.get("self") is not ledger
            or reserve_frame.f_back is None
            or reserve_frame.f_back.f_code is not producer.__code__
            or reserve_frame.f_back.f_globals is not producer.__globals__
            or reserve_frame.f_back.f_locals.get("ledger") is not ledger
        ):
            raise ReviewInputError("reservation fusion caller changed")
        require_module_seal(sys.modules["review_sensei.session"])
        require_module_seal(sys.modules["review_sensei.models"])
        require_module_seal(sys.modules["review_sensei_github.session_ledger"])
        require_instance_source(
            ledger, methods=("reserve", "replace", "_own_reservation")
        )
        from .models import ReviewTransaction

        # mutate_reserved and _own_reservation have already run, including all
        # rescan, generation, competing-owner and abandoned-analysis rules.
        # Refuse an idempotent/no-reservation result rather than attach to it.
        if (
            reserved.identity() != identity
            or reserved.reservation_id is None
            or before.reservation_id is not None
            or reserved.transaction is not None
            or reserved.generation != before.generation + 1
        ):
            raise ReviewInputError("reservation fusion reservation conflicts")
        transaction = ReviewTransaction.create(
            repository=identity.repository,
            pull_request=identity.pull_request,
            reservation_id=reserved.reservation_id,
            generation=reserved.generation,
            **entry["binding"],
        )
        updated = reserved.evolve(transaction=transaction, now=entry["now"])
        # No new state besides the original exact transaction may enter this CAS.
        projected = updated.to_dict()
        expected = reserved.to_dict()
        projected.pop("transaction", None)
        expected.pop("transaction", None)
        projected["updated_at"] = expected["updated_at"]
        projected.pop("record_sha256", None)
        expected.pop("record_sha256", None)
        if canonical_bytes(projected) != canonical_bytes(expected):
            raise ReviewInputError("reservation fusion changed unrelated state")
        entry["attached"] = transaction
        return updated

    return install, reserve_transaction, attach_reserved


install, reserve_transaction, attach_reserved = _build()
del _build

from .source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
