"""One-use complete PR response between two exact original publication sites.

Only an empty grouped replay lookup with no intervening physical request may
share the response. Both ordinary parsers run. This never grants publication.
"""

import dataclasses
import hashlib
import inspect
import json
import sys
from typing import Any
from weakref import WeakKeyDictionary

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import require_instance_source, require_module_seal


class _Observation:
    pass


def _registry():
    source: Any = None
    entries: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()
    owners: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()

    def install(cls):
        nonlocal source
        current = inspect.currentframe()
        frame = current.f_back if current else None
        module = sys.modules.get("review_sensei_github.publication")
        spec = getattr(module, "__spec__", None)
        loader = getattr(getattr(spec, "loader", None), "get_code", None)
        if (
            source is not None
            or module is None
            or frame is None
            or frame.f_globals is not vars(module)
            or frame.f_locals is not frame.f_globals
            or not getattr(spec, "_initializing", False)
            or not callable(loader)
            or loader(module.__name__) != frame.f_code
            or cls is not module.ReviewPublisher
        ):
            raise ReviewInputError(
                "adjacent preflight requires defining publisher source"
            )
        source = (
            cls,
            {
                name: inspect.unwrap(getattr(cls, name)).__code__
                for name in (
                    "publish",
                    "_publish_impl",
                    "_preflight_pr",
                    "_published_state_index",
                )
            },
        )

    def frame_for(owner: Any, name):
        current = inspect.currentframe()
        parent = current.f_back if current else None
        frame = parent.f_back if parent else None
        module = sys.modules.get("review_sensei_github.publication")
        if (
            source is None
            or owner is None
            or type(owner) is not source[0]
            or frame is None
            or module is None
            or frame.f_code is not source[1][name]
            or frame.f_globals is not vars(module)
            or frame.f_locals.get("self") is not owner
        ):
            raise ReviewInputError("adjacent preflight original call frame changed")
        require_module_seal(module)
        require_instance_source(
            owner, methods=("_publish_impl", "_preflight_pr", "_published_state_index")
        )
        require_instance_source(
            getattr(owner, "finalizer"),
            methods=(
                "quarantine_reader",
                "_evaluate_live_pr_trust_boundary",
                "_eligibility_from_reviews",
                "finding_index_from_reviews",
            ),
        )
        return frame

    def arguments(values):
        keys = (
            "repository",
            "repository_id",
            "pull_request",
            "head_sha",
            "base_branch",
            "base_sha",
            "app_slug",
        )
        return canonical_bytes({key: values[key] for key in keys}), hashlib.sha256(
            values["token"].encode()
        ).digest()

    def transport_stamp(http: Any, caller: Any, capacity, expected_journal):
        from . import command_finalization_plan, publication_recovery
        from .api_run_budget import CallerRunBudget
        from .finding_lifecycle import _JournalTransport
        from .http import GitHubHttp
        from .publication_recovery import (
            PublicationBudgetHttp,
            _PaginationObservationHttp,
        )

        require_module_seal(command_finalization_plan)
        require_module_seal(publication_recovery)
        seen = set()
        chain: list[tuple[Any, ...]] = []
        for _ in range(16):
            if id(http) in seen:
                break
            seen.add(id(http))
            require_instance_source(http, methods=("request",))
            if type(http) is _JournalTransport:
                delegate = command_finalization_plan.registered_journal_delegate(http)
                # repository_path is delegated, not a method on this adapter.
                # An instance shadow must not replace the actual path producer.
                if (
                    "repository_path" in http.__dict__
                    or http.repository_path != delegate.repository_path
                ):
                    raise ReviewInputError("adjacent preflight path delegation changed")
                journal = http.journal
                if journal is not expected_journal:
                    raise ReviewInputError("adjacent preflight journal owner changed")
                require_instance_source(
                    journal,
                    methods=(
                        "request",
                        "_is_effect",
                        "_observe_review_read",
                        "_observe_check_read",
                    ),
                )
                chain.append(
                    (
                        id(http),
                        id(delegate),
                        id(journal),
                        http.api_url,
                        http.timeout,
                        journal.identity,
                        journal.head_sha,
                        journal._armed,
                    )
                )
                http = delegate
            elif type(http) in (PublicationBudgetHttp, _PaginationObservationHttp):
                require_instance_source(http, methods=("repository_path",))
                budget = (
                    http.publication_budget
                    if type(http) is PublicationBudgetHttp
                    else http.observation_budget
                )
                if (
                    type(budget) is not CallerRunBudget
                    or budget is not caller.budget_view
                    or budget.caller is not caller
                ):
                    raise ReviewInputError("adjacent preflight budget caller changed")
                require_instance_source(
                    budget, methods=("check", "consume", "_remaining_seconds")
                )
                extra: tuple[Any, ...]
                if type(http) is PublicationBudgetHttp:
                    # This hot path has no optional operation observer. A custom
                    # callback cannot be skipped or treated as the original parser.
                    if (
                        http.operation_observer is not None
                        or type(http._already_charged) is not bool
                    ):
                        raise ReviewInputError(
                            "adjacent preflight observer is unsupported"
                        )
                    extra = (http.operation_observer, http._already_charged)
                else:
                    # PR reads have no page scope; the oversized-page cache is
                    # neither read nor mutated by this request's successful path.
                    extra = (id(http._oversized_page_ceilings),)
                chain.append(
                    (
                        id(http),
                        id(http.transport),
                        id(budget),
                        id(budget.caller),
                        http.api_url,
                        http.timeout,
                        budget._ordinary_hold,
                        id(budget._tail_ticket),
                        extra,
                    )
                )
                http = http.transport
            elif type(http) is GitHubHttp:
                require_instance_source(
                    http, methods=("repository_path", "_parse_body")
                )
                if (
                    getattr(http.opener, "__self__", None) is not caller
                    or getattr(http.opener, "__func__", None)
                    is not getattr(type(caller), "open", None)
                    or http._controlled_restart_guard is not None
                    or http._quarantine_capacity_guard is not capacity
                ):
                    raise ReviewInputError("adjacent preflight physical caller changed")
                chain.append(
                    (
                        id(http),
                        id(getattr(http.opener, "__func__", None)),
                        http.api_url,
                        http.timeout,
                        id(capacity),
                        http._controlled_restart_guard,
                    )
                )
                return tuple(chain)
            else:
                break
        raise ReviewInputError("adjacent preflight transport is not the original stack")

    def stamp(owner, host):
        from . import (
            api_run_requests,
            caller_operation,
            quarantine_capacity,
            quarantine_native_scope,
            trust_effects,
        )
        from .api_run_requests import ApiRunRequestTransport, ApiRunSessionLedger
        from .application import GitHubApplication
        from .native_group_publication import NativeGroupPublication
        from .review_run import ReviewRunLedger

        require_module_seal(api_run_requests)
        require_module_seal(caller_operation)
        require_module_seal(quarantine_native_scope)
        require_module_seal(quarantine_capacity)
        if (
            trust_effects.verification_profile
            is not api_run_requests._original_trust_verification_profile
        ):
            raise ReviewInputError("adjacent preflight trust producer changed")
        if type(host) is not NativeGroupPublication or type(host.ledger) not in (
            ApiRunSessionLedger,
            ReviewRunLedger,
        ):
            raise ReviewInputError("adjacent preflight needs original grouped host")
        require_instance_source(
            host, methods=("state", "_current_frontier", "adopt_read_frontier")
        )
        factory = owner.finalizer.quarantine_reader_factory
        application = getattr(factory, "__self__", None)
        if (
            type(application) is not GitHubApplication
            or getattr(factory, "__func__", None)
            is not GitHubApplication._quarantine_hold_reader
        ):
            raise ReviewInputError("adjacent preflight reader factory changed")
        require_instance_source(application, methods=("_quarantine_hold_reader",))
        caller = host.ledger.caller
        if (
            type(caller) is not ApiRunRequestTransport
            or caller._checkpoint is not None
            or caller._effect_record is None
            or caller._effect_record.record_sha256 != caller._root
        ):
            raise ReviewInputError("adjacent preflight current owner changed")
        require_instance_source(caller, methods=("open", "_require_ledger"))
        caller._require_ledger(host.ledger, host.identity)
        require_instance_source(host.ledger, methods=("load", "replace"))
        if caller._effect_record.publication_staging is not None:
            raise ReviewInputError(
                "adjacent preflight requires an empty grouped frontier"
            )
        require_instance_source(caller._effect_record, methods=("to_dict",))
        native_scope = quarantine_native_scope.native_scope(
            owner.http,
            repository=host.identity.repository,
            head_sha=host.plan.head_sha,
        )
        namespace = None
        if native_scope is not None:
            if native_scope.caller is not caller:
                raise ReviewInputError("adjacent preflight native scope changed")
            require_instance_source(native_scope, methods=("filter_reviews",))
            namespace = (
                id(native_scope),
                native_scope.identity,
                native_scope.base,
                native_scope.head,
                native_scope.scope,
                native_scope.producer,
                native_scope.prior,
            )
        journal = owner._finding_lifecycle_journal
        require_instance_source(journal, methods=("request", "preflight_native_batch"))
        if (
            journal.reviewer is not owner
            or journal.identity != host.identity
            or journal.ledger.caller is not caller
        ):
            raise ReviewInputError("adjacent preflight publication journal changed")
        caller._require_ledger(journal.ledger, journal.identity)
        capacity = quarantine_capacity.installed_capacity(
            journal.ledger, journal.identity
        )
        require_instance_source(
            capacity, methods=("before_request", "after_response", "_before_request")
        )
        if capacity._pending_create is not None:
            raise ReviewInputError("adjacent preflight has an unresolved creation")
        profile = trust_effects.verification_profile(caller)
        return (
            caller,
            caller._sequence,
            caller._block,
            caller._root,
            id(caller._effect_record),
            canonical_bytes(caller._effect_record.to_dict()),
            canonical_bytes(caller.accounting.to_document()),
            transport_stamp(owner.http, caller, capacity, journal),
            transport_stamp(host.ledger.http, caller, capacity, journal),
            id(host.ledger),
            id(host.plan),
            id(host.transaction),
            factory,
            namespace,
            id(caller.plan),
            id(caller._opener),
            quarantine_capacity._guard_state(capacity),
            canonical_bytes(
                [
                    (list(k), dataclasses.asdict(v))
                    for k, v in sorted(owner.finalizer._trust_snapshots.items())
                ]
            ),
            (
                id(owner.finalizer._observed_trust_context),
                None
                if owner.finalizer._observed_trust_context is None
                else canonical_bytes(
                    dataclasses.asdict(owner.finalizer._observed_trust_context)
                ),
            ),
            profile,
        )

    def begin(owner, host):
        if host is None:
            return None
        try:
            frame = frame_for(owner, "_publish_impl")
        except ReviewInputError:
            return None
        ancestor = frame.f_back
        while ancestor is not None and ancestor.f_code is not source[1]["publish"]:
            ancestor = ancestor.f_back
        if ancestor is None or ancestor.f_locals.get("self") is not owner:
            return None
        old = owners.pop(owner, None)
        if old is not None:
            entries.pop(old, None)
        if host is None or not frame.f_locals.get("grouped_publication"):
            return None
        try:
            stamp(owner, host)
        except (ReviewInputError, TypeError, ValueError):
            return None
        observation = _Observation()
        entries[observation] = {
            "owner": owner,
            "host": host,
            "frame": frame,
            "arguments": arguments(frame.f_locals),
            "phase": "first",
            "saved": None,
        }
        owners[owner] = observation
        return observation

    def observe(observation, owner, status, payload):
        if observation is None:
            return
        frame = frame_for(owner, "_preflight_pr")
        entry = entries.get(observation) if type(observation) is _Observation else None
        if (
            entry is None
            or entry["owner"] is not owner
            or entry["phase"] != "first"
            or frame.f_back is not entry["frame"]
            or arguments(frame.f_locals) != entry["arguments"]
        ):
            return
        if status != 200 or not isinstance(payload, dict):
            entries.pop(observation, None)
            return
        try:
            raw = canonical_bytes(payload)
            if len(raw) > 512 * 1024:
                return
            entry["saved"] = (raw, stamp(owner, entry["host"]))
            entry["phase"] = "observed"
        except (ReviewInputError, TypeError, ValueError):
            entries.pop(observation, None)

    def index(owner, host, identity_states, result_states, eligibility):
        if source is None or type(owner) is not source[0]:
            return
        observation = owners.get(owner)
        if observation is None:
            return
        frame = frame_for(owner, "_published_state_index")
        entry = entries.get(observation) if observation is not None else None
        if entry is None:
            return
        if (
            entry["phase"] != "observed"
            or entry["host"] is not host
            or frame.f_back is not entry["frame"]
            or identity_states
            or result_states
            or eligibility is not None
            or frame.f_locals.get("frontier") is not None
            or frame.f_locals.get("payload") != []
            or entry["saved"] is None
        ):
            entries.pop(observation, None)
            return
        try:
            if stamp(owner, host) != entry["saved"][1]:
                entries.pop(observation, None)
                return
        except (ReviewInputError, TypeError, ValueError):
            entries.pop(observation, None)
            return
        entry["phase"] = "indexed"

    def consume(observation, owner):
        if observation is None:
            return None
        frame = frame_for(owner, "_preflight_pr")
        entry = entries.get(observation) if type(observation) is _Observation else None
        if entry is None or entry["phase"] == "first":
            return None
        entries.pop(observation, None)
        if (
            entry["owner"] is not owner
            or entry["phase"] != "indexed"
            or entry["saved"] is None
            or frame.f_back is not entry["frame"]
            or arguments(frame.f_locals) != entry["arguments"]
        ):
            return None
        try:
            if stamp(owner, entry["host"]) != entry["saved"][1]:
                return None
        except (ReviewInputError, TypeError, ValueError):
            return None
        return 200, json.loads(entry["saved"][0])

    def discard(owner):
        if source is None or type(owner) is not source[0]:
            return
        observation = owners.pop(owner, None)
        if observation is not None:
            entries.pop(observation, None)

    return install, begin, observe, index, consume, discard


install_source, begin, observe, index, consume, discard = _registry()
del _registry
from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
