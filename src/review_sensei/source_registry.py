"""Provider-neutral original module-definition identity seals.

Seals describe executable source, not permission, data authenticity, or budget.
Only the actual module loader's executing definition may register itself once.
"""

from __future__ import annotations

import inspect
import sys
from types import ModuleType
from typing import Any

from .errors import ReviewInputError


def _build_registry():
    registered: dict[Any, tuple[tuple[Any, str, Any, Any], ...]] = {}

    def normalize(value):
        return getattr(value, "__func__", value)

    def seal_module():
        current = inspect.currentframe()
        frame = current.f_back if current is not None else None
        if frame is None:
            raise ReviewInputError("original source definition frame is unavailable")
        name = frame.f_globals.get("__name__")
        if not isinstance(name, str):
            raise ReviewInputError("original source definition name is unavailable")
        module = sys.modules.get(name)
        spec = getattr(module, "__spec__", None)
        loader = getattr(spec, "loader", None)
        get_code = getattr(loader, "get_code", None)
        if (
            not isinstance(module, ModuleType)
            or vars(module) is not frame.f_globals
            or frame.f_locals is not frame.f_globals
            or frame.f_code.co_name != "<module>"
            or not getattr(spec, "_initializing", False)
            or not callable(get_code)
            or get_code(name) != frame.f_code
            or module in registered
        ):
            raise ReviewInputError("original source seal requires its defining module")
        bindings: list[tuple[Any, str, Any, Any]] = []
        for key, value in tuple(vars(module).items()):
            if key.isupper() and isinstance(
                value, (str, int, float, bool, tuple, frozenset)
            ):
                bindings.append((module, key, value, None))
                continue
            if not (inspect.isfunction(value) or inspect.isclass(value)) or not getattr(
                value, "__module__", ""
            ).startswith("review_sensei"):
                continue
            bindings.append((module, key, value, getattr(value, "__code__", None)))
            if inspect.isclass(value) and value.__module__ == name:
                for attribute in dir(value):
                    if attribute.startswith("__") and attribute not in {
                        "__init__",
                        "__new__",
                        "__call__",
                    }:
                        continue
                    member = normalize(getattr(value, attribute))
                    if isinstance(member, property):
                        bindings.append((value, attribute, member, None))
                        for accessor in ("fget", "fset", "fdel"):
                            function = getattr(member, accessor)
                            if function is not None:
                                bindings.append(
                                    (member, accessor, function, function.__code__)
                                )
                    elif inspect.isfunction(member):
                        bindings.append((value, attribute, member, member.__code__))
        registered[module] = tuple(bindings)

    def require_module_seal(module):
        bindings = registered.get(module) if isinstance(module, ModuleType) else None
        if bindings is None or sys.modules.get(module.__name__) is not module:
            raise ReviewInputError("original source module seal is unavailable")
        for owner, name, original, code in bindings:
            current = normalize(getattr(owner, name, None))
            if (
                current is not original
                or getattr(current, "__code__", None) is not code
            ):
                raise ReviewInputError("original module source changed")

    def require_instance_source(instance, *, methods):
        cls = type(instance)
        source_modules = []
        for base in cls.__mro__:
            if base is object:
                continue
            module = sys.modules.get(base.__module__)
            bindings = registered.get(module)
            if bindings is None or not any(
                owner is module and original is base
                for owner, _name, original, _code in bindings
            ):
                raise ReviewInputError("original source class is unavailable")
            require_module_seal(module)
            source_modules.append(module)
        if not source_modules:
            raise ReviewInputError("original source class is unavailable")
        if not isinstance(methods, tuple) or any(
            not isinstance(name, str)
            or name in instance.__dict__
            or normalize(getattr(instance, name, None))
            is not normalize(getattr(cls, name, None))
            or not callable(getattr(instance, name, None))
            for name in methods
        ):
            raise ReviewInputError("original source instance method changed")

    return seal_module, require_module_seal, require_instance_source


seal_module, require_module_seal, require_instance_source = _build_registry()
del _build_registry
seal_module()
