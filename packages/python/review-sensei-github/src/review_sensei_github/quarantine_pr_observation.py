"""One-use fresh PR data shared across two pure source validators.

This is not a read cache or write authority. An intervening charged request,
root transition, source override or compacted path restores the ordinary read.
"""

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Any
from weakref import WeakKeyDictionary

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError

_ACTIVE: ContextVar[Any] = ContextVar("quarantine_pr_observation", default=None)
_INSTALLED: set[str] = set()
_OBSERVATIONS: WeakKeyDictionary = WeakKeyDictionary()
_HOLD_SOURCE = None


@dataclass(eq=False)
class _Observation:
    pass


def install_reader(cls):
    if "reader" in _INSTALLED:
        raise ReviewInputError("quarantine PR reader already installed")
    _INSTALLED.add("reader")
    methods = {
        name: getattr(cls, name)
        for name in ("require", "validate_pull", "_require_caller")
    }
    original_require = cls.require

    def observe(scope, pull):
        active = _ACTIVE.get()
        entry = _OBSERVATIONS.get(active) if active is not None else None
        if (
            entry is None
            or entry.get("blocked")
            or entry["saved"] is not None
            or type(scope) is not cls
        ):
            return
        owner = entry["owner"]
        if any(
            getattr(getattr(scope, name, None), "__func__", None) is not method
            for name, method in methods.items()
        ):
            return
        ledger = scope.ledger
        caller = getattr(ledger, "caller", None)
        if caller is None:
            from .review_run import StartupRunLedger

            if type(ledger) is StartupRunLedger:
                caller = ledger._startup_caller
        from .api_run_requests import ApiRunRequestTransport

        if (
            type(caller) is not ApiRunRequestTransport
            or caller._checkpoint is not None
            or caller._effect_record is None
            or caller._effect_record.record_sha256 != caller._root
        ):
            return
        from .command_finalization_plan import _underlying_http

        try:
            transport = _underlying_http(ledger.http, budget=ledger.evidence_budget)
            if transport is not _underlying_http(
                owner.http, budget=owner.evidence_budget
            ):
                return
        except ReviewInputError:
            return
        if not caller.observations or _HOLD_SOURCE is None:
            return
        hold_cls, hold_methods = _HOLD_SOURCE
        if any(
            getattr(hold_cls, name) is not method
            for name, method in hold_methods.items()
        ):
            return
        entry["saved"] = dict(
            scope=scope,
            scope_binding=(
                scope.identity.repository,
                scope.identity.pull_request,
                scope.identity.repository_id,
                scope.base_sha,
                scope.head_sha,
            ),
            ledger=ledger,
            caller=caller,
            transport=transport,
            budget=ledger.evidence_budget,
            record=caller._effect_record,
            root=caller._root,
            record_bytes=canonical_bytes(caller._effect_record.to_dict()),
            observations=canonical_bytes(caller.observations),
            payload=canonical_bytes(pull),
            metadata_digest=hashlib.sha256(scope.metadata_token.encode()).digest(),
            run=caller.run,
            methods=methods,
            cls=cls,
        )

    @wraps(original_require)
    def require(scope):
        # The issuer is lexical to this wrapper. No caller-supplied payload or
        # callback can reach it without executing the real source read first.
        pull = original_require(scope)
        if pull is not None:
            observe(scope, pull)
        return pull

    methods["require"] = require
    return require


def install_finalizer(cls):
    if "finalizer" in _INSTALLED:
        raise ReviewInputError("quarantine PR finalizer already installed")
    _INSTALLED.add("finalizer")
    methods = {
        name: getattr(cls, name)
        for name, value in vars(cls).items()
        if callable(value) and not name.startswith("__")
    }
    from .operation_diagnostics import OperationObserver

    observer_at = OperationObserver.at

    def pure_observer(owner):
        observer = owner._operation_observer
        return observer is None or (
            type(observer) is OperationObserver
            and getattr(observer.at, "__func__", None) is observer_at
            and OperationObserver.at is observer_at
        )

    def intact(owner):
        return type(owner) is cls and all(
            getattr(getattr(owner, name, None), "__func__", getattr(owner, name, None))
            is method
            for name, method in methods.items()
        )

    @contextmanager
    def capture(owner, reader=None, *, token=None):
        observation = _Observation()
        _OBSERVATIONS[observation] = dict(
            owner=owner,
            saved=None,
            blocked=False,
            publication_digest=hashlib.sha256(token.encode()).digest()
            if isinstance(token, str)
            else None,
        )
        if _ACTIVE.get() is not None or not intact(owner) or not pure_observer(owner):
            yield observation
            return
        token = _ACTIVE.set(observation)
        try:
            if reader is not None:
                validate_hold_reader(reader)
            yield observation
        finally:
            _ACTIVE.reset(token)

    def consume(
        observation, owner, *, token, repository, pull_request, head_sha, app_slug
    ):
        entry = (
            _OBSERVATIONS.pop(observation, None)
            if type(observation) is _Observation
            else None
        )
        if entry is None or entry.get("blocked") or entry["owner"] is not owner:
            return None
        saved = entry["saved"]
        if not isinstance(saved, dict) or not intact(owner) or not pure_observer(owner):
            return None
        scope, caller, ledger = saved["scope"], saved["caller"], saved["ledger"]
        from .command_finalization_plan import _underlying_http

        try:
            if (
                type(scope) is not saved["cls"]
                or any(
                    getattr(getattr(scope, name, None), "__func__", None) is not method
                    for name, method in saved["methods"].items()
                )
                or scope._read_window
                or (
                    scope.identity.repository,
                    scope.identity.pull_request,
                    scope.identity.repository_id,
                    scope.base_sha,
                    scope.head_sha,
                )
                != saved["scope_binding"]
                or scope.ledger is not ledger
                or (
                    getattr(ledger, "caller", None)
                    or getattr(ledger, "_startup_caller", None)
                )
                is not caller
                or caller is None
                or caller.run is not saved["run"]
                or hashlib.sha256(scope.metadata_token.encode()).digest()
                != saved["metadata_digest"]
                or hashlib.sha256(token.encode()).digest()
                != entry["publication_digest"]
                or scope.identity.repository != repository
                or scope.identity.pull_request != pull_request
                or scope.head_sha != head_sha
                or caller._checkpoint is not None
                or caller._effect_record is not saved["record"]
                or caller._root != saved["root"]
                or canonical_bytes(caller._effect_record.to_dict())
                != saved["record_bytes"]
                or canonical_bytes(caller.observations) != saved["observations"]
                or ledger.evidence_budget is not saved["budget"]
                or owner.evidence_budget is not saved["budget"]
                or _underlying_http(ledger.http, budget=saved["budget"])
                is not saved["transport"]
                or _underlying_http(owner.http, budget=saved["budget"])
                is not saved["transport"]
            ):
                return None
        except ReviewInputError:
            return None
        # Identical pure gate, including repository trust diagnostics. No HTTP,
        # cached eligibility or historical completion is substituted here.
        return owner._preflight_payload(
            json.loads(saved["payload"]),
            repository=repository,
            pull_request=pull_request,
            head_sha=head_sha,
            app_slug=app_slug,
        )

    return capture, consume


def install_hold_reader(cls):
    global _HOLD_SOURCE
    if _HOLD_SOURCE is not None:
        raise ReviewInputError("quarantine hold source already installed")
    _HOLD_SOURCE = (
        cls,
        {name: getattr(cls, name) for name in ("read", "attach", "require")},
    )


def validate_hold_reader(reader):
    active = _ACTIVE.get()
    entry = _OBSERVATIONS.get(active) if active is not None else None
    if entry is None:
        return
    if _HOLD_SOURCE is None:
        entry["blocked"] = True
        return
    cls, methods = _HOLD_SOURCE
    if type(reader) is not cls or any(
        getattr(getattr(reader, name, None), "__func__", None) is not method
        for name, method in methods.items()
    ):
        entry["blocked"] = True
