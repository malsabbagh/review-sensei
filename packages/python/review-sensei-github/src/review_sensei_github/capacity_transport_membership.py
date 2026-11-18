"""Negative optional-reader classification, never capacity or reader authority.

Installation leaves a transport-lifetime tombstone independent of removable
attributes and guard/ledger ownership. A private C-owned root binds the current
immutable membership epoch; Python indices cannot erase a live member. Dead
weakrefs are compacted in batches, preserving every live row and irrevocably
revoking the old epoch. Only original constructor receipts permit
following the three supported wrapper links. No reset or removal API exists.
"""

import sys
import weakref
from _thread import RLock
from functools import lru_cache
from types import CellType, FunctionType
from typing import Any

from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import (
    require_instance_source,
    require_module_seal,
    require_original_class_identity,
)


def _cache_statistics(hits, misses, maxsize, size):
    return (hits, misses, maxsize, size)


def _registry():
    lock = RLock()
    lock_type = type(lock)
    acquire = lock_type.acquire
    release = lock_type.release
    busy = [False]
    reference = weakref.ref
    weakref_module = weakref
    cache_type: Any = type(lru_cache(maxsize=None)(lambda: None))
    cache_call = cache_type.__call__
    cache_info = cache_type.cache_info
    cache_clear = cache_type.cache_clear
    absent: type[Exception] = type("_AbsentCapacityMembership", (Exception,), {})
    retired: set[tuple[int, int]] = set()
    state: list[Any] = [None]
    factory_code = sys._getframe().f_code
    mint_code = None

    def enter():
        # Authenticate before invoking even the synchronization primitive. The
        # exact C reentrant lock allows us to reject same-thread reentry rather
        # than deadlock if a tracing callback attempts a registry operation.
        _require_sources()
        if type(lock) is not lock_type or type(busy) is not list or len(busy) != 1:
            raise ReviewInputError("capacity membership synchronization changed")
        acquire(lock)
        if busy[0] is not False:
            release(lock)
            raise ReviewInputError("capacity membership transition is reentrant")
        busy[0] = True

    def leave():
        busy[0] = False
        release(lock)

    def root_value():
        origin = sys._getframe(1)
        if origin.f_code is factory_code and state[0] is not None:
            return state[0]
        if (
            origin.f_code is compact.__code__
            and origin.f_globals is globals()
            and origin.f_locals.get("epoch") is state[0]
            and type(origin.f_locals.get("replacement")) is tuple
        ):
            return origin.f_locals["replacement"]
        raise ReviewInputError("capacity membership root cannot be reminted")

    root = cache_type(root_value, None, False, _cache_statistics)
    root.__wrapped__ = root_value

    def key_valid(key):
        return type(key) is tuple and len(key) == 2 and all(type(n) is int for n in key)

    def new_epoch(rows, previous=None):
        nonlocal mint_code
        frame = sys._getframe(1)
        if previous is None:
            if state[0] is not None or frame.f_code is not factory_code:
                raise ReviewInputError("capacity membership defining epoch changed")
        elif (
            frame.f_code is not compact.__code__
            or frame.f_locals.get("epoch") is not previous
            or state[0] is not previous
        ):
            raise ReviewInputError("capacity membership cleanup producer changed")
        marker = object()
        index: dict[tuple[int, int], Any] = {}
        handle: list[Any] = [None]

        def mint(key):
            if not key_valid(key):
                raise ReviewInputError("capacity membership cache key changed")
            origin = sys._getframe(1)
            if (
                origin.f_code is new_epoch.__code__
                and origin.f_locals.get("handle") is handle
            ):
                if key == (-1, 0):
                    return marker
                if origin.f_locals.get("key") == key:
                    return origin.f_locals["row"]
            if (
                origin.f_code is add.__code__
                and origin.f_locals.get("epoch") is handle[0]
                and state[0] is handle[0]
                and origin.f_locals.get("key") == key
                and row_valid(origin.f_locals.get("row"), key)
            ):
                return origin.f_locals["row"]
            if (
                key == (-2, 0)
                and origin.f_code is compact.__code__
                and origin.f_locals.get("epoch") is handle[0]
                and state[0] is handle[0]
            ):
                return marker
            # Queries cannot create entries, including after cache_clear.
            raise absent()

        cache = cache_type(mint, None, False, _cache_statistics)
        cache.__wrapped__ = mint
        if mint_code is None:
            mint_code = mint.__code__
        elif mint.__code__ is not mint_code:
            raise ReviewInputError("capacity membership cache producer changed")
        cache_call(cache, (-1, 0))
        for row in rows:
            key = row[:2]
            index[key] = cache_call(cache, key)
            if index[key] is not row:
                raise ReviewInputError("capacity membership cleanup row changed")
        bindings = tuple((cell, cell.cell_contents) for cell in mint.__closure__ or ())
        epoch = (cache, index, marker, mint, handle, bindings)
        handle[0] = epoch
        return epoch

    def quick():
        if (
            type(state) is not list
            or len(state) != 1
            or type(retired) is not set
            or weakref is not weakref_module
            or weakref.ref is not reference
        ):
            raise ReviewInputError("capacity transport membership storage changed")
        epoch = state[0]
        if (
            type(root) is not cache_type
            or type(vars(root)) is not dict
            or any(type(key) is not str for key in vars(root))
            or vars(root).get("__wrapped__") is not root_value
        ):
            raise ReviewInputError("capacity membership root source changed")
        if cache_call(root) is not epoch:
            raise ReviewInputError("capacity membership head is not the issued epoch")
        if type(epoch) is not tuple or len(epoch) != 6:
            raise ReviewInputError("capacity transport membership epoch changed")
        cache, index, marker, mint, handle, bindings = epoch
        if (
            type(cache) is not cache_type
            or type(index) is not dict
            or type(mint) is not FunctionType
            or mint.__code__ is not mint_code
            or mint.__globals__ is not globals()
            or mint.__defaults__ is not None
            or mint.__kwdefaults__ is not None
            or type(handle) is not list
            or len(handle) != 1
            or handle[0] is not epoch
            or type(bindings) is not tuple
            or any(
                type(pair) is not tuple
                or len(pair) != 2
                or type(pair[0]) is not CellType
                for pair in bindings
            )
            or any(cell.cell_contents is not original for cell, original in bindings)
            or type(vars(cache)) is not dict
            or any(type(key) is not str for key in vars(cache))
            or vars(cache).get("__wrapped__") is not mint
        ):
            raise ReviewInputError("capacity transport membership cache source changed")
        info = cache_info(cache)
        if (
            type(info) is not tuple
            or len(info) != 4
            or info[2] is not None
            or type(info[3]) is not int
            or info[3] != len(index) + 1
        ):
            raise ReviewInputError(
                "capacity transport membership index coverage changed"
            )
        try:
            if cache_call(cache, (-1, 0)) is not marker:
                raise ReviewInputError("capacity membership defining marker changed")
        except absent:
            raise ReviewInputError("capacity membership cache was reset") from None
        try:
            cache_call(cache, (-2, 0))
        except absent:
            return epoch
        raise ReviewInputError("capacity membership epoch was revoked")

    def row_valid(row, key):
        return (
            type(row) is tuple
            and len(row) == 5
            and type(row[0]) is int
            and type(row[1]) is int
            and row[:2] == key
            and type(row[2]) is reference
            and row[2].__callback__ is None
            and id(row[2]) == key[1]
            and type(row[4]) is reference
            and (
                row[3] is None
                if key[0] == 0
                else type(row[3]) is tuple
                and len(row[3]) == 2
                and type(row[3][0]) is reference
                and type(row[3][1]) is type
            )
        )

    def compact():
        origin = sys._getframe(1)
        if (
            origin.f_code not in (find.__code__, add.__code__)
            or origin.f_globals is not globals()
        ):
            raise ReviewInputError(
                "capacity cleanup requires original membership source"
            )
        epoch = quick()
        cache, index = epoch[:2]
        rows: list[Any] = []
        # Cardinality plus exact cached identity for every distinct key proves
        # complete index coverage. No caller-owned hash/equality is executed.
        for key, row in index.items():
            if not key_valid(key) or key[0] not in (0, 1) or not row_valid(row, key):
                raise ReviewInputError("capacity membership cleanup index changed")
            try:
                if cache_call(cache, key) is not row:
                    raise ReviewInputError(
                        "capacity membership cleanup identity changed"
                    )
            except absent:
                raise ReviewInputError(
                    "capacity membership cleanup row is absent"
                ) from None
            value = reference.__call__(row[2])
            if value is not None:
                if reference(value) is not row[2]:
                    raise ReviewInputError("capacity membership live identity changed")
                rows.append(row)
        replacement = new_epoch(tuple(rows), epoch)
        # After revocation an exception can only leave a rejecting old head.
        # Rewinding this head cannot erase the C-owned revocation entry.
        cache_call(cache, (-2, 0))
        cache_clear(root)
        if cache_call(root) is not replacement:
            raise ReviewInputError("capacity membership replacement was not issued")
        state[0] = replacement
        retired.clear()
        return replacement

    def find(which, value):
        enter()
        try:
            epoch = quick()
            if len(retired) >= max(64, len(epoch[1]) // 2):
                epoch = compact()
            try:
                canonical = reference(value)
            except TypeError:
                return None
            key = (which, id(canonical))
            try:
                row = cache_call(epoch[0], key)
            except absent:
                return None
            if not row_valid(row, key):
                raise ReviewInputError("capacity membership cached row changed")
            actual = reference.__call__(row[2])
            if actual is None:
                return None
            if actual is not value:
                raise ReviewInputError("capacity transport membership identity changed")
            return row
        finally:
            leave()

    def add(which, value, details):
        enter()
        try:
            frame = sys._getframe(1)
            if (
                frame.f_code
                not in (register_install.__code__, register_wrapper.__code__)
                or frame.f_globals is not globals()
            ):
                raise ReviewInputError(
                    "capacity membership append lacks original producer"
                )
            epoch = quick()
            if len(retired) >= max(64, len(epoch[1]) // 2):
                epoch = compact()
            canonical = reference(value)
            key = (which, id(canonical))
            try:
                previous = cache_call(epoch[0], key)
            except absent:
                previous = None
            if previous is not None:
                raise ReviewInputError(
                    "capacity transport membership cannot be replaced"
                )

            def dead(_reference):
                # A scheduling hint only. Cleanup separately proves every live row
                # against the immutable C cache before dropping any dead reference.
                retired.add(key)

            row = (which, id(canonical), canonical, details, reference(value, dead))
            retained = cache_call(epoch[0], key)
            if retained is not row:
                raise ReviewInputError("capacity membership append changed")
            epoch[1][key] = row
        finally:
            leave()

    def register_install(transport, guard):
        _require_sources()
        from . import quarantine_capacity

        require_module_seal(quarantine_capacity)
        frame = sys._getframe(1)
        if (
            frame.f_code is not quarantine_capacity.install_capacity.__code__
            or frame.f_globals is not vars(quarantine_capacity)
            or frame.f_locals.get("transport") is not transport
            or frame.f_locals.get("guard") is not guard
        ):
            raise ReviewInputError("capacity membership requires original installation")
        add(0, transport, None)

    def wrapper_kind(value):
        # Module dictionaries are read without invoking module __getattr__ or
        # instance properties. Source registry validates the exact original
        # class/export/methods, not the unrelated ReviewPublisher class.
        from . import publication, publication_recovery

        cls = type(value)
        supported = (
            vars(publication).get("_BudgetedReviewPagination"),
            vars(publication_recovery).get("PublicationBudgetHttp"),
            vars(publication_recovery).get("_PaginationObservationHttp"),
        )
        if not any(cls is original for original in supported):
            return None
        if type(cls) is not type:
            raise ReviewInputError("capacity wrapper class changed")
        raw = object.__getattribute__(value, "__dict__")
        if type(raw) is not dict or any(type(key) is not str for key in raw):
            raise ReviewInputError("capacity wrapper fields changed")
        require_instance_source(
            value, methods=("__init__", "request", "_paginate", "repository_path")
        )
        return cls

    def register_wrapper(value):
        _require_sources()
        cls = wrapper_kind(value)
        frame = sys._getframe(1)
        if (
            cls is None
            or frame.f_code is not vars(cls)["__init__"].__code__
            or frame.f_globals is not vars(sys.modules[cls.__module__])
            or frame.f_locals.get("self") is not value
        ):
            raise ReviewInputError("capacity wrapper requires original construction")
        raw = object.__getattribute__(value, "__dict__")
        if type(raw) is not dict or "transport" not in raw:
            raise ReviewInputError("capacity wrapper delegate is absent")
        delegate = raw["transport"]
        # PublicationBudgetHttp constructs an observation wrapper around its
        # original input. The other two constructors delegate directly.
        if cls.__name__ == "PublicationBudgetHttp":
            link = find(1, delegate)
            if link is None or reference.__call__(link[3][0]) is not frame.f_locals.get(
                "http"
            ):
                raise ReviewInputError("capacity wrapper construction chain changed")
        elif delegate is not frame.f_locals.get("http"):
            raise ReviewInputError("capacity wrapper construction input changed")
        add(1, value, (reference(delegate), cls))

    def never_installed(value):
        _require_sources()
        from . import http as native_http

        seen = set()
        for _ in range(16):
            if id(value) in seen:
                raise ReviewInputError("capacity wrapper membership cycle")
            seen.add(id(value))
            if find(0, value) is not None:
                return False
            cls = type(value)
            if cls is vars(native_http).get("GitHubHttp"):
                # Lifetime nonmembership is already established. This is only
                # original base identity, not a reader or execution capability;
                # ordinary request overrides remain the public generic seam.
                require_original_class_identity(
                    value, module=native_http, name="GitHubHttp"
                )
                raw = object.__getattribute__(value, "__dict__")
                if type(raw) is not dict or any(type(key) is not str for key in raw):
                    raise ReviewInputError("capacity transport fields changed")
                if (
                    type(raw) is not dict
                    or raw.get("_quarantine_capacity_guard") is not None
                ):
                    raise ReviewInputError(
                        "unregistered capacity transport was decorated"
                    )
                return True
            row = find(1, value)
            if row is None:
                return False  # Unknown wrappers retain the existing full path.
            if wrapper_kind(value) is not row[3][1]:
                raise ReviewInputError("capacity wrapper original class changed")
            raw = object.__getattribute__(value, "__dict__")
            delegate = reference.__call__(row[3][0])
            if (
                type(raw) is not dict
                or delegate is None
                or raw.get("transport") is not delegate
                or raw.get("_quarantine_capacity_guard") is not None
            ):
                raise ReviewInputError("capacity wrapper original delegate changed")
            value = delegate
        raise ReviewInputError("capacity wrapper membership depth exceeded")

    def was_installed(value):
        _require_sources()
        return find(0, value) is not None

    state[0] = new_epoch(())
    cache_call(root)
    functions = (
        enter,
        leave,
        root_value,
        key_valid,
        new_epoch,
        quick,
        row_valid,
        compact,
        find,
        add,
        wrapper_kind,
        register_install,
        register_wrapper,
        never_installed,
        was_installed,
    )
    definitions = tuple(
        (
            fn,
            fn.__code__,
            fn.__globals__,
            fn.__defaults__,
            fn.__kwdefaults__,
            tuple((cell, cell.cell_contents) for cell in fn.__closure__ or ()),
        )
        for fn in functions
    )
    return (
        register_install,
        register_wrapper,
        never_installed,
        was_installed,
        definitions,
    )


(
    register_install,
    register_wrapper,
    never_installed,
    was_installed,
    _REGISTRY_SOURCES,
) = _registry()
del _registry


def _require_sources():
    require_module_seal(sys.modules[__name__])
    for (
        function,
        code,
        namespace,
        defaults,
        keyword_defaults,
        closure_functions,
    ) in _REGISTRY_SOURCES:
        if (
            type(function) is not FunctionType
            or function.__code__ is not code
            or function.__globals__ is not namespace
            or function.__defaults__ is not defaults
            or function.__kwdefaults__ is not keyword_defaults
            or any(
                cell.cell_contents is not original
                for cell, original in closure_functions
            )
        ):
            raise ReviewInputError("capacity transport registry source changed")


from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
