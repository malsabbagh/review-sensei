"""Negative optional-reader classification, never capacity or reader authority.

Installation leaves a transport-lifetime tombstone independent of removable
attributes and guard/ledger ownership. Only original constructor receipts permit
following the three supported wrapper links. No reset or removal API exists.
"""

import sys
import weakref
from types import FunctionType
from typing import Any

from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import require_instance_source, require_module_seal


def _registry():
    installed: dict[int, Any] = {}
    wrappers: dict[int, Any] = {}
    tables = (installed, wrappers)
    histories = [(), ()]
    sizes = [0, 0]
    weakref_module = weakref
    reference = weakref.ref

    def checked(which):
        if (
            type(tables) is not tuple
            or len(tables) != 2
            or tables[0] is not installed
            or tables[1] is not wrappers
            or type(histories) is not list
            or len(histories) != 2
            or type(sizes) is not list
            or len(sizes) != 2
            or weakref is not weakref_module
            or weakref.ref is not reference
        ):
            raise ReviewInputError("capacity transport membership storage changed")
        table, history, size = tables[which], histories[which], sizes[which]
        if (
            type(table) is not dict
            or type(history) is not tuple
            or type(size) is not int
            or len(table) != size
        ):
            raise ReviewInputError("capacity transport membership storage changed")
        for key, row in table.items():
            if (
                type(key) is not int
                or type(row) is not tuple
                or len(row) != 4
                or type(row[0]) is not int
                or row[0] != key
                or type(row[1]) is not reference
                or type(row[2]) is not int
                or not 0 <= row[2] < len(history)
                or history[row[2]] is not row
                or (which == 0 and row[3] is not None)
                or (
                    which == 1
                    and (
                        type(row[3]) is not tuple
                        or len(row[3]) != 2
                        or type(row[3][0]) is not reference
                        or type(row[3][1]) is not type
                    )
                )
            ):
                raise ReviewInputError("capacity transport membership entry changed")
        return table

    def find(which, value):
        table = checked(which)
        row = table.get(id(value))
        if row is None:
            return None
        actual = reference.__call__(row[1])
        if actual is None:  # An old lifetime is not a new object's identity.
            return None
        if actual is not value:
            raise ReviewInputError("capacity transport membership identity changed")
        return row

    def add(which, value, details):
        table = checked(which)
        key = id(value)
        if find(which, value) is not None:
            raise ReviewInputError("capacity transport membership cannot be replaced")
        row = (key, reference(value), len(histories[which]), details)
        histories[which] = (*histories[which], row)
        if key not in table:
            sizes[which] += 1
        table[key] = row

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

        require_module_seal(native_http)
        seen = set()
        for _ in range(16):
            if id(value) in seen:
                raise ReviewInputError("capacity wrapper membership cycle")
            seen.add(id(value))
            if find(0, value) is not None:
                return False
            cls = type(value)
            if cls is native_http.GitHubHttp:
                require_instance_source(
                    value,
                    methods=("__init__", "request", "_paginate", "repository_path"),
                )
                raw = object.__getattribute__(value, "__dict__")
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

    functions = (
        checked,
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
            tuple(
                (cell, cell.cell_contents)
                for cell in fn.__closure__ or ()
                if type(cell.cell_contents) is FunctionType
            ),
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
